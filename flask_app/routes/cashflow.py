"""M3: Cash Flow 13 Semanas — Flask blueprint."""
import json
import logging
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from db.cashflow import get_cashflow_config, set_cashflow_config
from db.faturas import ONEDRIVE_SUBFOLDERS, get_suppliers
from db.centros_custo import (
    get_colaboradores, get_colaboradores_calculados,
    upsert_colaborador, toggle_colaborador,
    migrate_colaboradores_from_json,
    _calc_colabs, get_cost_centers,
)
from db.tabelas_cct import CCT_CATEGORIAS, CCT_SALARIOS, NIVEIS_LABELS, lookup_salario_cct
from db.tabelas_irs import lookup_irs, ESTADO_CIVIL_LABELS

SS_TRAB = 0.11
SS_PATR = 0.2375

logger = logging.getLogger(__name__)

cashflow_bp = Blueprint('cashflow', __name__)



@cashflow_bp.route('/configuracoes', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def configuracoes():
    if request.method == 'POST':
        threshold_str = request.form.get('alert_threshold_eur', '2000').replace(',', '.')
        try:
            threshold = float(threshold_str)
            set_cashflow_config('alert_threshold_eur', str(threshold))
            flash(f'Threshold de alerta actualizado: {threshold:,.0f}€', 'success')
        except (ValueError, TypeError):
            flash('Valor inválido para threshold.', 'danger')
        return redirect(url_for('cashflow.configuracoes'))

    cfg = get_cashflow_config()
    return render_template('financeiro/cashflow/configuracoes.html', cfg=cfg)


def _migrate_colabs_from_json_if_needed():
    """One-time migration: if the DB colaboradores table is empty but JSON config exists, migrate."""
    try:
        existing = get_colaboradores()
        if existing:
            return
        cfg = get_cashflow_config()
        json_str = cfg.get('salarios_colaboradores', '[]')
        n = migrate_colaboradores_from_json(json_str)
        if n:
            logger.info('Migrated %d colaboradores from JSON to DB table.', n)
    except Exception as exc:
        logger.warning('_migrate_colabs_from_json_if_needed failed: %s', exc)


@cashflow_bp.route('/salarios', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def salarios():
    _migrate_colabs_from_json_if_needed()

    if request.method == 'POST':
        action = request.form.get('action', 'save_global')

        if action == 'save_global':
            # Save global payment-day config only
            sal_imp_dia = request.form.get('salarios_impostos_dia', '15')
            sal_liq_dia = request.form.get('salarios_liquido_dia', '28')
            try:
                set_cashflow_config('salarios_impostos_dia', str(int(sal_imp_dia)))
                set_cashflow_config('salarios_liquido_dia',  str(int(sal_liq_dia)))
                # Recalculate aggregate totals from DB
                colabs = get_colaboradores_calculados(ativo_only=True)
                total_liq = round(sum(c.get('salario_liq', 0) for c in colabs), 2)
                total_imp = round(sum(c.get('impostos_dia15', 0) for c in colabs), 2)
                set_cashflow_config('salarios_liquido_eur',  str(total_liq))
                set_cashflow_config('salarios_impostos_eur', str(total_imp))
                # Keep JSON in sync for legacy cashflow forecast reader
                set_cashflow_config('salarios_colaboradores',
                                    json.dumps([{k: c.get(k) for k in
                                                 ('nome','salario_bruto','premio_bruto','irs_taxa',
                                                  'salario_liq','ss_patronal','ss_trabalhador',
                                                  'irs_retido','impostos_dia15','custo_empresa')}
                                                for c in colabs], ensure_ascii=False))
                flash('Configuração de dias de pagamento actualizada.', 'success')
            except (ValueError, TypeError) as exc:
                flash(f'Valores inválidos: {exc}', 'danger')

        elif action == 'upsert_colaborador':
            colab_id_raw = request.form.get('colab_id', '').strip()
            colab_id = int(colab_id_raw) if colab_id_raw else None
            nome = request.form.get('nome', '').strip()
            try:
                categoria = request.form.get('categoria_profissional', 'outro').strip() or 'outro'
                nivel = int(request.form.get('nivel_remuneratorio', '1') or 1)
                estado_civil = request.form.get('estado_civil', 'solteiro').strip() or 'solteiro'
                num_dep = int(request.form.get('num_dependentes', '0') or 0)
                irs_override_flag = request.form.get('irs_override', '0') == '1'

                if categoria != 'outro':
                    bruto = lookup_salario_cct(categoria, nivel)
                else:
                    bruto = float(request.form.get('salario_bruto', '0').replace(',', '.') or 0)

                premio = float(request.form.get('premio_bruto', '0').replace(',', '.') or 0)

                if irs_override_flag or categoria == 'outro':
                    irs = min(100.0, max(0.0, float(
                        request.form.get('irs_taxa', '0').replace(',', '.') or 0)))
                else:
                    irs = lookup_irs(bruto + premio, estado_civil, num_dep)

                data_inicio_s = request.form.get('data_inicio', '').strip() or None
                from datetime import datetime as _dt
                data_inicio = _dt.strptime(data_inicio_s, '%Y-%m-%d').date() if data_inicio_s else None

                # Cost center allocations
                cc_ids  = request.form.getlist('cc_id[]')
                cc_pcts = request.form.getlist('cc_pct[]')
                centros = []
                for ccid, pct in zip(cc_ids, cc_pcts):
                    if ccid and pct and pct.strip():
                        try:
                            pct_val = float(pct.replace(',', '.'))
                            if pct_val > 0:
                                centros.append({'centro_custo_id': int(ccid),
                                                'percentagem': pct_val})
                        except (ValueError, TypeError):
                            pass

                # Validate 100% rule if any allocations given
                if centros:
                    total_pct = sum(c['percentagem'] for c in centros)
                    if abs(total_pct - 100.0) > 0.5:
                        flash(f'A soma das percentagens dos centros de custo deve ser 100% (actual: {total_pct:.1f}%).', 'warning')
                        return redirect(url_for('cashflow.salarios'))

                if not nome:
                    flash('Nome do colaborador é obrigatório.', 'warning')
                else:
                    upsert_colaborador(colab_id, nome, bruto, premio, irs, data_inicio, centros,
                                       categoria_profissional=categoria,
                                       nivel_remuneratorio=nivel,
                                       estado_civil=estado_civil,
                                       num_dependentes=num_dep,
                                       irs_override=irs_override_flag)
                    # Sync aggregates
                    colabs = get_colaboradores_calculados(ativo_only=True)
                    total_liq = round(sum(c.get('salario_liq', 0) for c in colabs), 2)
                    total_imp = round(sum(c.get('impostos_dia15', 0) for c in colabs), 2)
                    set_cashflow_config('salarios_liquido_eur',  str(total_liq))
                    set_cashflow_config('salarios_impostos_eur', str(total_imp))
                    set_cashflow_config('salarios_colaboradores',
                                        json.dumps([{k: c.get(k) for k in
                                                     ('nome','salario_bruto','premio_bruto','irs_taxa',
                                                      'salario_liq','ss_patronal','ss_trabalhador',
                                                      'irs_retido','impostos_dia15','custo_empresa')}
                                                    for c in colabs], ensure_ascii=False))
                    label = 'actualizado' if colab_id else 'adicionado'
                    flash(f'Colaborador "{nome}" {label}.', 'success')
            except Exception as exc:
                flash(f'Erro: {exc}', 'danger')

        elif action == 'toggle_colaborador':
            colab_id = int(request.form.get('colab_id', 0))
            ativo = request.form.get('ativo', '0') == '1'
            try:
                toggle_colaborador(colab_id, ativo)
                colabs = get_colaboradores_calculados(ativo_only=True)
                total_liq = round(sum(c.get('salario_liq', 0) for c in colabs), 2)
                total_imp = round(sum(c.get('impostos_dia15', 0) for c in colabs), 2)
                set_cashflow_config('salarios_liquido_eur',  str(total_liq))
                set_cashflow_config('salarios_impostos_eur', str(total_imp))
                flash('Colaborador ' + ('activado.' if ativo else 'desactivado.'), 'success')
            except Exception as exc:
                flash(f'Erro: {exc}', 'danger')

        return redirect(url_for('cashflow.salarios'))

    try:
        cfg = get_cashflow_config()
    except Exception:
        flash('Erro ao carregar configuração. Por favor tente novamente.', 'danger')
        cfg = {}

    colaboradores = get_colaboradores_calculados(ativo_only=False)
    cost_centers  = get_cost_centers(ativo_only=True)

    return render_template('financeiro/cashflow/salarios.html',
                           cfg=cfg,
                           colaboradores=colaboradores,
                           cost_centers=cost_centers,
                           SS_TRAB=SS_TRAB,
                           SS_PATR=SS_PATR,
                           cct_categorias=CCT_CATEGORIAS,
                           cct_salarios=CCT_SALARIOS,
                           niveis_labels=NIVEIS_LABELS,
                           estado_civil_labels=ESTADO_CIVIL_LABELS)


@cashflow_bp.route('/salarios/irs-preview')
@perm_required('acesso_gestor')
def salarios_irs_preview():
    """AJAX: return CCT base salary and IRS rate for given params."""
    categoria = request.args.get('categoria', 'outro')
    try:
        nivel = max(1, min(5, int(request.args.get('nivel', 1) or 1)))
    except (ValueError, TypeError):
        nivel = 1
    estado_civil = request.args.get('estado_civil', 'solteiro')
    try:
        num_dep = max(0, int(request.args.get('num_dep', 0) or 0))
    except (ValueError, TypeError):
        num_dep = 0
    try:
        premio = max(0.0, float(request.args.get('premio', 0) or 0))
    except (ValueError, TypeError):
        premio = 0.0
    bruto_manual_str = request.args.get('bruto_manual', '')

    if categoria != 'outro':
        bruto = lookup_salario_cct(categoria, nivel)
    else:
        try:
            bruto = float(bruto_manual_str.replace(',', '.') or 0)
        except (ValueError, TypeError):
            bruto = 0.0

    total = bruto + premio
    irs = lookup_irs(total, estado_civil, num_dep)
    ss_trab = round(total * SS_TRAB, 2)
    ss_patr = round(total * SS_PATR, 2)
    irs_ret = round(total * irs / 100, 2)
    liq = round(total - ss_trab - irs_ret, 2)
    custo = round(total * (1 + SS_PATR), 2)

    return jsonify({
        'salario_bruto': round(bruto, 2),
        'total_bruto': round(total, 2),
        'irs_taxa': irs,
        'ss_trabalhador': ss_trab,
        'ss_patronal': ss_patr,
        'irs_retido': irs_ret,
        'salario_liq': liq,
        'custo_empresa': custo,
    })


@cashflow_bp.route('/debitos', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def debitos():
    from db.custos_recorrentes import (
        get_custos_recorrentes, create_custo_recorrente,
        update_custo_recorrente, delete_custo_recorrente,
        next_due_date_cr, FREQUENCIA_LABELS, TIPOLOGIA_LABELS,
    )
    today = date.today()

    _VALID_TIPOLOGIA  = {'fixo', 'variavel'}
    _VALID_FREQUENCIA = {'semanal', 'quinzenal', 'mensal', 'trimestral', 'semestral', 'anual'}

    def _parse_form():
        """Parse and validate common form fields. Returns (data_dict, errors_list)."""
        errors = []
        try:
            supplier_id = int(request.form.get('supplier_id', 0))
            if not supplier_id:
                raise ValueError
        except (ValueError, TypeError):
            supplier_id = None
            errors.append('Fornecedor é obrigatório.')

        tipologia = request.form.get('tipologia', '').strip()
        if tipologia not in _VALID_TIPOLOGIA:
            errors.append('Tipologia inválida.')

        frequencia = request.form.get('frequencia', '').strip()
        if frequencia not in _VALID_FREQUENCIA:
            errors.append('Frequência inválida.')

        data_str = request.form.get('data_cobranca', '').strip()
        try:
            from datetime import date as _date
            data_cobranca = _date.fromisoformat(data_str) if data_str else None
            if not data_cobranca:
                raise ValueError
        except ValueError:
            data_cobranca = None
            errors.append('Data de cobrança inválida.')

        try:
            cc_raw = request.form.get('centro_custo_id', '').strip()
            centro_custo_id = int(cc_raw) if cc_raw else None
        except (ValueError, TypeError):
            centro_custo_id = None

        valor = None
        valor_str = request.form.get('valor', '').replace(',', '.').strip()
        if valor_str:
            try:
                valor = float(valor_str)
                if valor < 0:
                    raise ValueError
            except ValueError:
                errors.append('Valor inválido.')
        elif tipologia == 'fixo':
            errors.append('Valor é obrigatório para custos fixos.')

        notas = request.form.get('notas', '').strip() or None

        return {
            'supplier_id': supplier_id,
            'tipologia': tipologia,
            'frequencia': frequencia,
            'data_cobranca': data_cobranca,
            'centro_custo_id': centro_custo_id,
            'valor': valor,
            'notas': notas,
        }, errors

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'create':
            data, errors = _parse_form()
            if errors:
                for e in errors:
                    flash(e, 'danger')
            else:
                create_custo_recorrente(**data)
                flash('Custo recorrente criado com sucesso.', 'success')
            return redirect(url_for('cashflow.debitos'))

        elif action == 'update':
            try:
                entry_id = int(request.form.get('entry_id', 0))
                if not entry_id:
                    raise ValueError
            except (ValueError, TypeError):
                flash('Entrada inválida.', 'danger')
                return redirect(url_for('cashflow.debitos'))
            data, errors = _parse_form()
            if errors:
                for e in errors:
                    flash(e, 'danger')
            else:
                update_custo_recorrente(entry_id, **data)
                flash('Custo recorrente actualizado.', 'success')
            return redirect(url_for('cashflow.debitos'))

        elif action == 'delete':
            try:
                entry_id = int(request.form.get('entry_id', 0))
            except (ValueError, TypeError):
                entry_id = 0
            if entry_id:
                delete_custo_recorrente(entry_id)
                flash('Custo recorrente eliminado.', 'success')
            return redirect(url_for('cashflow.debitos'))

    # GET
    raw = get_custos_recorrentes()
    entries = []
    for row in raw:
        e = dict(row)
        e['next_due'] = next_due_date_cr(e, today)
        # Ensure data_cobranca is a string for tojson serialisation
        if hasattr(e.get('data_cobranca'), 'isoformat'):
            e['data_cobranca_str'] = e['data_cobranca'].isoformat()
        else:
            e['data_cobranca_str'] = e.get('data_cobranca') or ''
        entries.append(e)

    suppliers = get_suppliers()
    cost_centers = get_cost_centers(ativo_only=True)

    return render_template(
        'financeiro/cashflow/debitos.html',
        entries=entries,
        suppliers=suppliers,
        cost_centers=cost_centers,
        frequencia_labels=FREQUENCIA_LABELS,
        tipologia_labels=TIPOLOGIA_LABELS,
        today=today,
    )


@cashflow_bp.route('/debitos/invoices/<int:supplier_id>')
@perm_required('acesso_gestor')
def debitos_invoices(supplier_id):
    from db.custos_recorrentes import get_last_invoices
    rows = get_last_invoices(supplier_id, limit=5)
    result = []
    for r in rows:
        result.append({
            'invoice_number': r['invoice_number'],
            'amount_eur': float(r['amount_eur']) if r['amount_eur'] is not None else None,
            'due_date':   r['due_date'].isoformat()   if r['due_date']   else None,
            'issue_date': r['issue_date'].isoformat() if r['issue_date'] else None,
            'status':     r['status'] or '',
        })
    return jsonify(result)


@cashflow_bp.route('/debitos/sugestao-valor')
@perm_required('acesso_gestor')
def debitos_sugestao_valor():
    from db.custos_recorrentes import suggest_valor
    try:
        supplier_id = int(request.args.get('supplier_id', 0))
        frequencia  = request.args.get('frequencia', '').strip()
    except (ValueError, TypeError):
        return jsonify({'error': 'Parâmetros inválidos'}), 400
    if not supplier_id or not frequencia:
        return jsonify({'valor': None, 'n_periodos': 0})
    result = suggest_valor(supplier_id, frequencia)
    return jsonify(result)
