"""M3: Cash Flow 13 Semanas — Flask blueprint."""
import json
import logging
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from db.cashflow import build_cashflow_13weeks, get_cashflow_config, set_cashflow_config
from db.faturas import ONEDRIVE_SUBFOLDERS
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


@cashflow_bp.route('/')
@perm_required('acesso_gestor')
def index():
    data = build_cashflow_13weeks()
    return render_template('financeiro/cashflow/index.html',
                           weeks=data['weeks'],
                           alerts=data['alerts'],
                           config=data['config'],
                           threshold=data['threshold'],
                           overdraft_total_plafond=data['overdraft_total_plafond'],
                           overdraft_utilizado=data['overdraft_utilizado'],
                           overdraft_disponivel=data['overdraft_disponivel'],
                           today=data['today'])


@cashflow_bp.route('/semana/<int:week_num>')
@perm_required('acesso_gestor')
def semana(week_num):
    data = build_cashflow_13weeks()
    weeks = data['weeks']
    if week_num < 1 or week_num > len(weeks):
        flash('Semana inválida.', 'warning')
        return redirect(url_for('cashflow.index'))
    week = weeks[week_num - 1]
    return render_template('financeiro/cashflow/semana.html',
                           week=week,
                           week_num=week_num,
                           alerts=[a for a in data['alerts'] if a.get('semana') == week_num],
                           threshold=data['threshold'],
                           overdraft_disponivel=data['overdraft_disponivel'],
                           today=data['today'])


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

                if categoria != 'outro' and not irs_override_flag:
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
                                       categoria_profissional=categoria if categoria != 'outro' else None,
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
    nivel = int(request.args.get('nivel', 1) or 1)
    estado_civil = request.args.get('estado_civil', 'solteiro')
    num_dep = int(request.args.get('num_dep', 0) or 0)
    premio = float(request.args.get('premio', 0) or 0)
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
    if request.method == 'POST':
        action = request.form.get('action', 'save_debitos')

        if action == 'save_debitos':
            labels = request.form.getlist('dd_label')
            valores = request.form.getlist('dd_valor')
            dias = request.form.getlist('dd_dia')
            centros = request.form.getlist('dd_centro_custo')
            debitos_list = []
            for lbl, val, dia, cc in zip(labels, valores, dias, centros):
                lbl = lbl.strip()
                cc = cc.strip() or None
                val_str = val.replace(',', '.')
                try:
                    valor = float(val_str)
                    dia_int = int(dia)
                    if lbl and valor > 0 and 1 <= dia_int <= 31:
                        debitos_list.append({
                            'label': lbl,
                            'valor_eur': valor,
                            'dia_debito': dia_int,
                            'centro_custo': cc,
                        })
                except (ValueError, TypeError):
                    pass
            set_cashflow_config('debitos_directos', json.dumps(debitos_list))
            flash(f'{len(debitos_list)} débito(s) directo(s) guardado(s).', 'success')
            return redirect(url_for('cashflow.debitos'))

        elif action == 'upload_invoice':
            pdf_file = request.files.get('invoice_pdf')
            if not pdf_file or not pdf_file.filename:
                return jsonify({'error': 'Nenhum ficheiro enviado.'}), 400
            if not pdf_file.filename.lower().endswith('.pdf'):
                return jsonify({'error': 'Apenas ficheiros PDF são suportados.'}), 400
            try:
                pdf_bytes = pdf_file.read()
                from flask_app.ocr_invoice import _get_anthropic_client
                import base64
                client = _get_anthropic_client()
                if not client:
                    return jsonify({'error': 'Serviço OCR não disponível.'}), 503

                pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')

                prompt = """Analisa este documento PDF que é uma factura de serviço público (electricidade, água, gás, telecomunicações, etc.).
Extrai os seguintes campos e devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "fornecedor": "nome do fornecedor/empresa",
  "valor_facturado_eur": 0.00,
  "periodo_inicio": "YYYY-MM-DD",
  "periodo_fim": "YYYY-MM-DD",
  "dias_periodo": 30,
  "estimativa_mensal_eur": 0.00
}

Regras:
- valor_facturado_eur: valor total a pagar nesta factura (com IVA se aplicável).
- periodo_inicio e periodo_fim: período de consumo/serviço cobrado nesta factura (não a data de emissão).
- dias_periodo: número de dias cobertos pelo período (calcula a partir das datas se possível).
- estimativa_mensal_eur: valor_facturado_eur dividido por dias_periodo e multiplicado por 30 (estimativa de custo mensal).
- Se um campo não for encontrado, usa null.
- Devolve APENAS o JSON, sem qualquer texto adicional."""

                message = client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=512,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }]
                )

                raw_text = message.content[0].text.strip()
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("```")[1]
                    if raw_text.startswith("json"):
                        raw_text = raw_text[4:]
                raw_text = raw_text.strip()

                parsed = json.loads(raw_text)

                def _safe_float(val):
                    if val is None:
                        return None
                    try:
                        return float(str(val).replace(',', '.'))
                    except (ValueError, TypeError):
                        return None

                def _safe_int(val):
                    if val is None:
                        return None
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        return None

                suggestion = {
                    'fornecedor': parsed.get('fornecedor') or None,
                    'valor_facturado_eur': _safe_float(parsed.get('valor_facturado_eur')),
                    'periodo_inicio': parsed.get('periodo_inicio') or None,
                    'periodo_fim': parsed.get('periodo_fim') or None,
                    'dias_periodo': _safe_int(parsed.get('dias_periodo')),
                    'estimativa_mensal_eur': _safe_float(parsed.get('estimativa_mensal_eur')),
                    'error': None,
                }
                return jsonify(suggestion)

            except json.JSONDecodeError as e:
                logger.error("OCR JSON parse error: %s", e)
                return jsonify({'error': 'Não foi possível interpretar a resposta do OCR.'}), 500
            except Exception as e:
                logger.error("Utility invoice OCR failed: %s", e)
                return jsonify({'error': 'Ocorreu um erro ao processar o ficheiro. Tente novamente.'}), 500

    cfg = get_cashflow_config()
    try:
        debitos_directos = json.loads(cfg.get('debitos_directos', '[]'))
    except Exception:
        debitos_directos = []

    return render_template('financeiro/cashflow/debitos.html',
                           cfg=cfg,
                           debitos_directos=debitos_directos,
                           onedrive_subfolders=ONEDRIVE_SUBFOLDERS)
