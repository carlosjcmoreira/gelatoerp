import logging
import os
import sys
import threading
import pandas as pd
from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
from db.cache import invalidate_prefix

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db

import flask_app.services.gestor as gestor_svc
from flask_app.services import ServiceError
from db.credito import get_payment_methods_config, upsert_payment_method_config
from db.auth import log_user_action, get_user_audit_log

gestor_bp = Blueprint('gestor', __name__)

TABS = [
    {'id': 'upload_producao', 'label': 'Upload Produção', 'icon': '📤', 'url_endpoint': 'gestor.upload_producao'},
    {'id': 'upload_pesagem', 'label': 'Upload Pesagem', 'icon': '⚖️', 'url_endpoint': 'gestor.upload_pesagem'},
    {'id': 'vendas_detalhe', 'label': 'Vendas Detalhe', 'icon': '🛒', 'url_endpoint': 'gestor.vendas_detalhe'},
    {'id': 'config_vendas_diarias', 'label': 'Filtro Vendas Diárias', 'icon': '📅', 'url_endpoint': 'gestor.config_vendas_diarias'},
    {'id': 'alocacao_produtos', 'label': 'Alocação de Produtos', 'icon': '📋', 'url_endpoint': 'gestor.regras_negocio'},
    {'id': 'ajustes_producao', 'label': 'Ajustes Produção', 'icon': '🔧', 'url_endpoint': 'gestor.ajustes_producao'},
    {'id': 'motivos_quebra', 'label': 'Motivos Quebra', 'icon': '⚠️', 'url_endpoint': 'gestor.motivos_quebra'},
    {'id': 'produtos_rececao', 'label': 'Produtos de Venda', 'icon': '📦', 'url_endpoint': 'gestor.produtos_rececao'},
    {'id': 'receitas_eurokg', 'label': 'Sabores Euro/kg', 'icon': '🍦', 'url_endpoint': 'gestor.receitas_eurokg'},
    {'id': 'gestao_utilizadores', 'label': 'Utilizadores', 'icon': '👥', 'url_endpoint': 'gestor.gestao_utilizadores'},
    {'id': 'premio_eurokg', 'label': 'Prémio Euro/kg', 'icon': '🏆', 'url_endpoint': 'gestor.premio_eurokg'},
    {'id': 'gestao_lojas', 'label': 'Lojas', 'icon': '🏪', 'url_endpoint': 'gestor.gestao_lojas'},
    {'id': 'metodos_pagamento', 'label': 'Métodos de Pagamento', 'icon': '💳', 'url_endpoint': 'gestor.metodos_pagamento'},
    {'id': 'materiais', 'label': 'Catálogo de Materiais', 'icon': '🗂️', 'url_endpoint': 'gestor.materiais'},
    {'id': 'centros_custo', 'label': 'Centros de Custo', 'icon': '🏷️', 'url_endpoint': 'centros_custo.index'},
    {'id': 'categorias_custo', 'label': 'Categorias de Custo', 'icon': '📂', 'url_endpoint': 'categorias_custo.index'},
    {'id': 'gestao_tarefas', 'label': 'Gestão de Tarefas', 'icon': '✅', 'url_endpoint': 'gestor.gestao_tarefas'},
    {'id': 'configuracoes', 'label': 'Configurações', 'icon': '⚙️', 'url_endpoint': 'gestor.configuracoes'},
    {'id': 'gestao_tiles', 'label': 'Gestão de Tiles', 'icon': '🔲', 'url_endpoint': 'gestor.gestao_tiles'},
]

SECTION_ENDPOINT_MAP = {
    'alocacao_produtos': 'gestor.regras_negocio',
    'regras_negocio': 'gestor.regras_negocio',
    'ajustes_producao': 'gestor.ajustes_producao',
    'motivos_quebra': 'gestor.motivos_quebra',
    'coberturas': 'pastelaria.coberturas',
    'tipologias_pastelaria': 'pastelaria.tipologias',
    'produtos_pastelaria': 'pastelaria.produtos',
    'produtos_rececao': 'gestor.produtos_rececao',
    'receitas_eurokg': 'gestor.receitas_eurokg',
    'gestao_utilizadores': 'gestor.gestao_utilizadores',
    'materiais': 'gestor.materiais',
}

def get_tabs():
    from db.tiles import get_tile_visibility, get_tile_labels
    visibility = get_tile_visibility('gestor')
    labels = get_tile_labels('gestor')
    return [
        {'id': t['id'], 'label': labels.get(t['id']) or t['label'], 'icon': t['icon'], 'url': url_for(t['url_endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]


def parse_decimal_input(val_str, default=0.0):
    if not val_str:
        return default
    s = str(val_str).replace(',', '.').strip()
    try:
        return float(s)
    except ValueError:
        return default


EUROKG_CHILD_IDS = {'premio_eurokg', 'receitas_eurokg', 'alocacao_produtos'}

@gestor_bp.route('/')
@perm_required('acesso_gestor')
def index():
    from db.tiles import get_tile_visibility, get_tile_labels, get_module_labels
    onedrive_configured = bool(db.get_system_config('onedrive_refresh_token'))
    visibility = get_tile_visibility('gestor')
    labels = get_tile_labels('gestor')
    custom_mod = get_module_labels().get('gestor')
    items = []
    eurokg_added = False
    for t in TABS:
        if not visibility.get(t['id'], True):
            continue
        if t['id'] in EUROKG_CHILD_IDS:
            if not eurokg_added:
                items.append({'icon': '💶', 'label': 'Euro/kg', 'url': url_for('gestor.eurokg_index')})
                eurokg_added = True
            continue
        item = {'icon': t['icon'], 'label': labels.get(t['id']) or t['label'], 'url': url_for(t['url_endpoint'])}
        if t['id'] == 'configuracoes':
            if onedrive_configured:
                item['badge'] = {'text': 'Ligado', 'cls': 'bg-success'}
            else:
                item['badge'] = {'text': 'Por configurar', 'cls': 'bg-secondary'}
        items.append(item)
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'👔 {custom_mod}' if custom_mod else '👔 Gestor')


@gestor_bp.route('/eurokg/')
@perm_required('acesso_gestor')
def eurokg_index():
    items = [
        {'icon': '🏆', 'label': 'Prémio Euro/kg', 'url': url_for('gestor.premio_eurokg')},
        {'icon': '🍦', 'label': 'Sabores Euro/kg', 'url': url_for('gestor.receitas_eurokg')},
        {'icon': '📋', 'label': 'Alocação de Produtos', 'url': url_for('gestor.regras_negocio')},
    ]
    return render_template('gestor/eurokg_index.html', items=items)


@gestor_bp.route('/upload-producao', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def upload_producao():
    tabs = get_tabs()
    loja_upload = request.args.get('loja', 'Matosinhos')
    prod_totals_list = db.get_producao_total_by_date(loja_upload)

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upload_csv':
            return _handle_upload_producao_csv(loja_upload)
        elif action == 'delete_producao':
            return _handle_delete_producao()
        elif action == 'manual_producao':
            return _handle_manual_producao()

    return render_template('gestor/upload_producao.html',
                           active_tab='upload_producao', tabs=tabs,
                           loja_upload=loja_upload,
                           prod_totals=prod_totals_list,
                           today=date.today().isoformat())


def _handle_upload_producao_csv(loja_upload):
    uploaded_file = request.files.get('csv_file')
    unit_is_grams = request.form.get('unit_is_grams') == 'on'
    loja = request.form.get('loja', loja_upload)

    if not uploaded_file or uploaded_file.filename == '':
        flash('Por favor, selecione um ficheiro CSV.', 'error')
        return redirect(url_for('gestor.upload_producao', loja=loja))

    try:
        result = gestor_svc.import_producao_csv(uploaded_file, loja, unit_is_grams)
        imported = result['imported']
        updated = result.get('updated', 0)
        skipped = result.get('skipped', 0)
        novas = result['novas_receitas']
        msg = f'{imported} registo(s) inserido(s), {updated} atualizado(s) e {skipped} ignorado(s) para {loja}.'
        if novas > 0:
            nomes = ', '.join(result['receitas_novas_nomes'])
            msg += f' {novas} receita(s) nova(s) adicionada(s) automaticamente: {nomes}'
        flash(msg, 'success')
    except ServiceError as e:
        flash(str(e), 'error')

    return redirect(url_for('gestor.upload_producao', loja=loja))


def _handle_delete_producao():
    data_inicio_str = request.form.get('data_del_inicio')
    data_fim_str = request.form.get('data_del_fim')
    loja = request.form.get('loja_del')
    try:
        data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
        if data_inicio > data_fim:
            flash('Data de início não pode ser posterior à data de fim.', 'warning')
        else:
            deleted = db.delete_producao_by_date_range(data_inicio, data_fim, loja)
            if deleted > 0:
                if data_inicio == data_fim:
                    flash(f'{deleted} registo(s) de produção eliminados para {loja} em {data_inicio.strftime("%d/%m/%Y")}!', 'success')
                else:
                    flash(f'{deleted} registo(s) de produção eliminados para {loja} entre {data_inicio.strftime("%d/%m/%Y")} e {data_fim.strftime("%d/%m/%Y")}!', 'success')
            else:
                flash('Nenhum registo encontrado para esse período e loja.', 'warning')
    except Exception as e:
        flash(f'Erro: {str(e)}', 'error')
    return redirect(url_for('gestor.upload_producao', loja=request.form.get('loja', 'Matosinhos')))


def _handle_manual_producao():
    data_str = request.form.get('data_prod')
    loja = request.form.get('loja_prod')
    qtd = parse_decimal_input(request.form.get('qtd_prod'))
    try:
        data_prod = datetime.strptime(data_str, '%Y-%m-%d').date()
        if qtd > 0:
            db.add_producao(data_prod, loja, qtd)
            flash(f'Produção de {qtd:.2f}kg registada para {loja}!', 'success')
        else:
            flash('Por favor, insira uma quantidade válida.', 'warning')
    except Exception as e:
        flash(f'Erro: {str(e)}', 'error')
    return redirect(url_for('gestor.upload_producao', loja=loja))


@gestor_bp.route('/upload-pesagem', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def upload_pesagem():
    tabs = get_tabs()
    loja_pesagem = request.args.get('loja', 'Matosinhos')

    if request.method == 'POST':
        return _handle_upload_pesagem_post(loja_pesagem)

    tipo_pesagem = 'fim' if loja_pesagem == 'Bolhão' else 'inicio'
    last_date, anterior, _ = db.get_pesagem_comparison(loja_pesagem, tipo_pesagem)
    last_pesagem = []
    if last_date and anterior:
        mapping = db.get_sabores_mapping()
        rev_map = {}
        for nr, nc in mapping.items():
            rev_map[nr] = nc
            rev_map[nc] = nc
        for s in sorted(anterior.keys()):
            last_pesagem.append({'sabor': rev_map.get(s, s), 'pesagem_kg': round(anterior[s], 3)})
        total_pesagem = round(sum(anterior.values()), 3)
    else:
        last_date = None
        total_pesagem = 0

    return render_template('gestor/upload_pesagem.html',
                           active_tab='upload_pesagem', tabs=tabs,
                           loja_pesagem=loja_pesagem,
                           last_date=last_date,
                           last_pesagem=last_pesagem,
                           total_pesagem=total_pesagem)


_PESAGEM_UPLOAD_LIMIAR_KG = 15.0


def _do_import_pesagem(entries: list, loja: str, tipo_pesagem: str) -> int:
    """Save a list of parsed pesagem entries to stock_gelado atomically.

    Each entry must have keys: data_iso (str ISO date), sabor (str), quantidade (float).
    Returns the number of rows inserted.
    """
    from database import add_stock_gelado_bulk
    bulk = [
        {
            'data': date.fromisoformat(e['data_iso']),
            'sabor': e['sabor'],
            'quantidade_kg': e['quantidade'],
            'tipo': tipo_pesagem,
        }
        for e in entries
    ]
    return add_stock_gelado_bulk(bulk, loja)


def _sign_pesagem_payload(payload_json: str) -> str:
    """Return HMAC-SHA256 hex digest of payload_json using the Flask secret key.

    Signing the hidden-field JSON payload prevents a user from altering the
    entries between the confirmation preview and the actual import — ensuring
    that what was reviewed is exactly what gets imported.
    """
    import hmac as _hmac
    import hashlib
    from flask import current_app
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode()
    return _hmac.new(key, payload_json.encode(), hashlib.sha256).hexdigest()


def _verify_pesagem_payload(payload_json: str, signature: str) -> bool:
    """Return True iff signature matches the HMAC of payload_json."""
    import hmac as _hmac
    expected = _sign_pesagem_payload(payload_json)
    return _hmac.compare_digest(expected, signature)


def _handle_upload_pesagem_post(loja_pesagem):
    uploaded_file = request.files.get('pesagem_file')
    loja = request.form.get('loja', loja_pesagem)

    if not uploaded_file or uploaded_file.filename == '':
        flash('Por favor, selecione um ficheiro Excel.', 'error')
        return redirect(url_for('gestor.upload_pesagem', loja=loja))

    try:
        df_pesagem = pd.read_excel(uploaded_file)
        sabor_col = df_pesagem.columns[0]
        date_cols = [c for c in df_pesagem.columns[1:] if isinstance(c, (pd.Timestamp, datetime))]
        if not date_cols:
            for c in df_pesagem.columns[1:]:
                try:
                    pd.to_datetime(c)
                    date_cols.append(c)
                except:
                    pass

        sabor_mapping_upload = {
            'Ricota, Noz/Mel': 'Ricota, Noz e Mel',
            'Extra Noir': 'Extra noir',
            'Doce de Leite': 'Doce de leite',
        }

        tipo_pesagem = 'fim' if loja == 'Bolhão' else 'inicio'
        skipped = 0

        all_dates_pesagem = set()
        for col in date_cols:
            try:
                all_dates_pesagem.add(pd.to_datetime(col).date())
            except Exception:
                continue

        existing_set = set()
        if all_dates_pesagem:
            min_d = min(all_dates_pesagem)
            max_d = max(all_dates_pesagem)
            existing_records = db.get_stock_gelado_df(loja=loja, tipo=tipo_pesagem, data_inicio=min_d, data_fim=max_d)
            for r in existing_records:
                existing_set.add((r['data'], r['sabor']))

        # First pass: parse all entries without writing to DB
        import json as _json
        entries = []
        for _, row in df_pesagem.iterrows():
            sabor_raw = str(row[sabor_col]).strip()
            sabor = sabor_mapping_upload.get(sabor_raw, sabor_raw)
            for col in date_cols:
                val = row[col]
                if pd.isna(val) or str(val).strip() == '-':
                    continue
                try:
                    quantidade = float(val)
                    if quantidade <= 0:
                        continue
                    data_val = pd.to_datetime(col).date()
                    if (data_val, sabor) in existing_set:
                        skipped += 1
                        continue
                    entries.append({
                        'sabor': sabor,
                        'data_iso': data_val.isoformat(),
                        'quantidade': round(quantidade, 3),
                        'suspeito': quantidade > _PESAGEM_UPLOAD_LIMIAR_KG,
                    })
                    existing_set.add((data_val, sabor))
                except:
                    continue

        if not entries:
            flash(f'Nenhum registo novo para importar ({skipped} já existentes, ignorados).', 'info')
            return redirect(url_for('gestor.upload_pesagem', loja=loja))

        # If any entry looks suspicious, render confirmation page inline.
        # All entry data is embedded in the page as hidden form fields so that
        # the confirmation POST carries the payload directly — no server-side
        # session/cache needed, which avoids cookie-size limits and gunicorn
        # multi-worker state issues.
        if any(e['suspeito'] for e in entries):
            entries_json = _json.dumps(entries)
            return render_template(
                'gestor/pesagem_confirmacao.html',
                active_tab='upload_pesagem',
                tabs=get_tabs(),
                loja=loja,
                entries=entries,
                skipped=skipped,
                limiar=_PESAGEM_UPLOAD_LIMIAR_KG,
                entries_json=entries_json,
                entries_sig=_sign_pesagem_payload(entries_json),
            )

        # No suspicious values — import atomically
        _do_import_pesagem(entries, loja, tipo_pesagem)
        flash(f'{len(entries)} registos importados com sucesso! ({skipped} já existentes, ignorados)', 'success')
        invalidate_prefix('kpi_annual')
        invalidate_prefix('kpi_monthly')
        invalidate_prefix('kpi_by_day')
    except Exception as e:
        flash(f'Erro ao processar ficheiro: {str(e)}', 'error')

    return redirect(url_for('gestor.upload_pesagem', loja=loja))


@gestor_bp.route('/pesagem-confirmacao', methods=['POST'])
@perm_required('acesso_gestor')
def pesagem_confirmacao():
    """Handle the confirmation step for suspicious pesagem imports.

    The upload POST renders the confirmation page inline, embedding all parsed
    entries as a JSON hidden field.  This route receives that hidden field back
    on confirm/cancel — no server-side shared state needed, works correctly
    across all gunicorn workers with no cookie-size constraints.
    """
    import json as _json
    loja = request.form.get('loja', 'Matosinhos')
    action = request.form.get('action', '')

    if action != 'confirm':
        flash('Importação cancelada.', 'info')
        return redirect(url_for('gestor.upload_pesagem', loja=loja))

    try:
        raw = request.form.get('entries_json', '[]')
        sig = request.form.get('entries_sig', '')
        if not _verify_pesagem_payload(raw, sig):
            flash('Assinatura inválida — os dados foram modificados. Por favor carregue o ficheiro novamente.', 'error')
            return redirect(url_for('gestor.upload_pesagem', loja=loja))
        entries_raw = _json.loads(raw)
        tipo_pesagem = 'fim' if loja == 'Bolhão' else 'inicio'
        entries = []
        for e in entries_raw:
            q = float(e['quantidade'])
            d = date.fromisoformat(str(e['data_iso']))
            s = str(e['sabor']).strip()
            if q > 0 and s:
                entries.append({
                    'sabor': s,
                    'data_iso': d.isoformat(),
                    'quantidade': round(q, 3),
                    'suspeito': q > _PESAGEM_UPLOAD_LIMIAR_KG,
                })
        if not entries:
            flash('Nenhum registo válido para importar.', 'error')
            return redirect(url_for('gestor.upload_pesagem', loja=loja))
        saved = _do_import_pesagem(entries, loja, tipo_pesagem)
        skipped = int(request.form.get('skipped', 0))
        flash(f'{saved} registos importados com sucesso! ({skipped} já existentes, ignorados)', 'success')
        invalidate_prefix('kpi_annual')
        invalidate_prefix('kpi_monthly')
        invalidate_prefix('kpi_by_day')
    except Exception as e:
        flash(f'Erro ao importar — nenhum registo foi guardado: {str(e)}', 'error')

    return redirect(url_for('gestor.upload_pesagem', loja=loja))


@gestor_bp.route('/vendas-detalhe', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def vendas_detalhe():
    tabs = get_tabs()
    loja_map_vendas = {'1': 'Matosinhos', '3': 'Bolhão'}

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'upload_vendas':
            return _handle_upload_vendas(loja_map_vendas)
        elif action == 'delete_vendas':
            return _handle_delete_vendas()

    loja_hist = request.args.get('loja_hist', 'Todas')
    loja_query = None if loja_hist == 'Todas' else loja_hist
    export_all = request.args.get('export_all') == '1'
    query_limit = None if export_all else 500
    _cols = {'id', 'data', 'loja', 'produto', 'categoria', 'quantidade', 'valor_euros'}
    vendas_list = [{k: v for k, v in r.items() if k in _cols}
                   for r in db.get_vendas_detalhe_df(loja_query, limit=query_limit)]

    return render_template('gestor/vendas_detalhe.html',
                           active_tab='vendas_detalhe', tabs=tabs,
                           vendas_list=vendas_list,
                           loja_hist=loja_hist,
                           export_all=export_all,
                           default_limit=500)


# Q1 2026 confirmed sales data gaps (identified 2026-04-06 via production DB query):
#   2026-02-02 Bolhão — PARTIAL import: only 4 gelado products / 49.50€ recorded
#     (full Monday typically 8+ products / ~170-280€). Use /eurokg/diagnostico-vendas
#     to confirm, then re-upload the correct Feb 02 XLSX for Bolhão.
#   2026-03-22 Bolhão — COMPLETELY ABSENT (0 rows) while Matosinhos had 1 601.54€.
#     Upload the missing March 22 XLSX for Bolhão.
_Q1_KNOWN_GAPS = [
    ('2026-02-02', 'Bolhão', 'partial'),
    ('2026-03-22', 'Bolhão', 'absent'),
]


def _handle_upload_vendas(loja_map_vendas):
    uploaded_file = request.files.get('vendas_file')
    if not uploaded_file or uploaded_file.filename == '':
        flash('Por favor, selecione um ficheiro.', 'error')
        return redirect(url_for('gestor.vendas_detalhe'))

    file_name = uploaded_file.filename.lower()
    try:
        if file_name.endswith('.xlsx'):
            from db.pastelaria import check_vendas_dates_have_data
            # Peek at (loja, date) pairs before parsing. Any (date, loja) that
            # already exists in the DB will be deleted atomically with the insert
            # (same transaction) so reimport is a safe replace, never a duplicate.
            dates_by_loja = gestor_svc.peek_dates_from_xlsx(uploaded_file, loja_map_vendas)
            pre_delete_pairs = []
            overlap_msgs = []
            for loja, dates in dates_by_loja.items():
                existing = check_vendas_dates_have_data(list(dates), loja)
                if existing:
                    for d in existing:
                        pre_delete_pairs.append((d, loja))
                    dates_fmt = ', '.join(d.strftime('%d/%m/%Y') for d in sorted(existing)[:5])
                    suffix = f' (+{len(existing) - 5} mais)' if len(existing) > 5 else ''
                    overlap_msgs.append(f'{loja}: {dates_fmt}{suffix}')
            if overlap_msgs:
                flash(
                    'Substituição de dados para ' + ' | '.join(overlap_msgs)
                    + '. Os dados do ficheiro substituem os registos anteriores.',
                    'info'
                )
            imported, skipped = gestor_svc.import_vendas_xlsx(
                uploaded_file, loja_map_vendas, pre_delete_pairs=pre_delete_pairs or None
            )
            db.sync_produtos_vendas_config()
            msg = f'{imported} registos importados com sucesso!'
            if skipped > 0:
                msg += f' ({skipped} ignorados sem loja identificada)'
            flash(msg, 'success')

        elif file_name.endswith('.csv'):
            loja_csv = request.form.get('loja_csv', 'Matosinhos')
            imported = gestor_svc.import_vendas_csv(uploaded_file, loja_csv)
            db.sync_produtos_vendas_config()
            flash(f'{imported} registos de vendas detalhadas importados com sucesso!', 'success')

        else:
            imported, skipped = gestor_svc.import_vendas_html(uploaded_file, loja_map_vendas)
            db.sync_produtos_vendas_config()
            msg = f'{imported} registos importados com sucesso!'
            if skipped > 0:
                msg += f' ({skipped} ignorados sem loja identificada)'
            flash(msg, 'success')

        invalidate_prefix('kpi_annual')
        invalidate_prefix('kpi_monthly')
        invalidate_prefix('kpi_by_day')

        if file_name.endswith('.xlsx'):
            _cal_lojas = set(dates_by_loja.keys()) or set(loja_map_vendas.values())
        elif file_name.endswith('.csv'):
            _cal_lojas = {request.form.get('loja_csv', 'Matosinhos')}
        else:
            _cal_lojas = set(loja_map_vendas.values())

        def _run_forecast_calibration(_lojas=_cal_lojas):
            try:
                from db.forecast import calibrate_meteo_multipliers, calibrate_wind_multipliers
                for _loja in _lojas:
                    try:
                        calibrate_meteo_multipliers(_loja, days=90)
                    except Exception:
                        pass
                    try:
                        calibrate_wind_multipliers(_loja, days=90)
                    except Exception:
                        pass
            except Exception:
                pass

        threading.Thread(target=_run_forecast_calibration, daemon=True).start()

    except ServiceError as e:
        flash(str(e), 'error')
    except Exception as e:
        flash(f'Erro ao ler ficheiro: {str(e)}', 'error')

    return redirect(url_for('gestor.vendas_detalhe'))


def _handle_delete_vendas():
    data_inicio_str = request.form.get('del_data_inicio')
    data_fim_str = request.form.get('del_data_fim')
    loja = request.form.get('del_loja')
    confirm = request.form.get('confirm_delete', '').strip().upper()

    if confirm != 'ELIMINAR':
        flash('Por favor, escreva ELIMINAR na caixa de confirmação.', 'warning')
        return redirect(url_for('gestor.vendas_detalhe'))

    try:
        data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
        loja_del = None if loja == 'Todas' else loja
        deleted = db.delete_vendas_detalhe_by_dates(data_inicio, data_fim, loja_del)
        if deleted > 0:
            flash(f'{deleted} registos eliminados com sucesso!', 'success')
        else:
            flash('Nenhum registo encontrado para eliminar.', 'warning')
    except Exception as e:
        flash(f'Erro: {str(e)}', 'error')

    return redirect(url_for('gestor.vendas_detalhe'))


@gestor_bp.route('/config-vendas-diarias', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def config_vendas_diarias():
    if request.method == 'POST':
        produtos = db.get_produtos_vendas_config()
        updates = [
            {'id': p['id'], 'conta': request.form.get(f'conta_{p["id"]}') == 'on'}
            for p in produtos
        ]
        db.update_conta_vendas_diarias_batch(updates)
        flash('Filtro de Vendas Diárias guardado com sucesso!', 'success')
        return redirect(url_for('gestor.config_vendas_diarias'))
    db.sync_produtos_vendas_config()
    return render_template('gestor/config_vendas_diarias.html',
                           produtos=db.get_produtos_vendas_config())


@gestor_bp.route('/config-vendas-diarias/toggle', methods=['POST'])
@perm_required('acesso_gestor')
def config_vendas_diarias_toggle():
    """AJAX endpoint: toggle conta_vendas_diarias for a single product."""
    try:
        produto_id = int(request.json.get('id'))
        conta = bool(request.json.get('conta'))
        db.update_conta_vendas_diarias_batch([{'id': produto_id, 'conta': conta}])
        return jsonify({'ok': True})
    except Exception as exc:
        logger.warning("config_vendas_diarias_toggle error: %s", exc)
        return jsonify({'ok': False, 'error': str(exc)}), 400


@gestor_bp.route('/regras-negocio', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def regras_negocio():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'regras_negocio')
    db.sync_produtos_vendas_config()
    return render_template('gestor/regras_negocio.html',
                           produtos_config=db.get_produtos_vendas_config())


@gestor_bp.route('/ajustes-producao', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def ajustes_producao():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'ajustes_producao')
    current_year = date.today().year
    meses_nomes = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                   'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
    return render_template('gestor/ajustes_producao.html',
                           ajustes=db.get_ajustes_producao(current_year),
                           current_year=current_year, meses_nomes=meses_nomes)


@gestor_bp.route('/motivos-quebra', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def motivos_quebra():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'motivos_quebra')
    return render_template('gestor/motivos_quebra.html',
                           motivos=db.get_all_motivos_quebra())


@gestor_bp.route('/coberturas')
@perm_required('acesso_gestor')
def coberturas():
    return redirect(url_for('pastelaria.coberturas'))


@gestor_bp.route('/tipologias-pastelaria')
@perm_required('acesso_gestor')
def tipologias_pastelaria():
    return redirect(url_for('pastelaria.tipologias'))


@gestor_bp.route('/produtos-pastelaria')
@perm_required('acesso_gestor')
def produtos_pastelaria():
    return redirect(url_for('pastelaria.produtos'))


@gestor_bp.route('/produtos-confeitaria')
@perm_required('acesso_gestor')
def produtos_confeitaria():
    return redirect(url_for('confeitaria.produtos'))


@gestor_bp.route('/produtos-rececao', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def produtos_rececao():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'produtos_rececao')
    return render_template('gestor/produtos_rececao.html',
                           produtos_rec=db.get_all_produtos_rececao())


@gestor_bp.route('/receitas-eurokg', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def receitas_eurokg():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'receitas_eurokg')
    receitas = db.get_all_receitas_gelado()
    com_sabor = [r for r in receitas if r.get('nome_corrente')]
    sem_sabor = [r for r in receitas if not r.get('nome_corrente')]
    return render_template('gestor/receitas_eurokg.html',
                           receitas_com_sabor=com_sabor,
                           receitas_sem_sabor=sem_sabor)


@gestor_bp.route('/gestao-utilizadores', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def gestao_utilizadores():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'gestao_utilizadores')
    return render_template('gestor/gestao_utilizadores.html',
                           users=db.get_all_users(),
                           lojas_venda=db.get_vendas_module_stores(),
                           audit_log=get_user_audit_log(limit=50))


@gestor_bp.route('/utilizadores/<int:user_id>/eliminar', methods=['POST'])
@perm_required('acesso_gestor')
def eliminar_utilizador(user_id: int):
    current_user = session['user']

    if user_id == current_user['id']:
        flash('Não pode eliminar a sua própria conta.', 'danger')
        return redirect(url_for('gestor.gestao_utilizadores'))

    all_users = db.get_all_users()
    target = next((u for u in all_users if u['id'] == user_id), None)
    if not target:
        flash('Utilizador não encontrado.', 'warning')
        return redirect(url_for('gestor.gestao_utilizadores'))

    if target.get('role') == 'admin' and db.count_admin_users() <= 1:
        flash('Não pode eliminar o único administrador ativo.', 'danger')
        return redirect(url_for('gestor.gestao_utilizadores'))

    confirm = request.form.get('confirm_text', '').strip()
    if confirm != 'ELIMINAR':
        flash('Confirmação inválida. Escreva ELIMINAR para confirmar a eliminação.', 'warning')
        return redirect(url_for('gestor.gestao_utilizadores'))

    username = target['username']
    try:
        db.delete_user_with_sessions(user_id)
    except Exception as exc:
        logger.error("Failed to delete user %s: %s", username, exc)
        flash(f"Não foi possível eliminar o utilizador '{username}'. Pode ter registos associados que impedem a eliminação.", 'danger')
        return redirect(url_for('gestor.gestao_utilizadores'))

    logger.info("User %s deleted by %s", username, current_user.get('username'))
    log_user_action(
        actor_id=current_user['id'],
        actor_username=current_user['username'],
        action_type='eliminado',
        target_user_id=user_id,
        target_username=username,
        details={
            'role': target.get('role'),
            'ativo': target.get('ativo'),
        },
    )
    flash(f"Utilizador '{username}' eliminado com sucesso.", 'success')
    return redirect(url_for('gestor.gestao_utilizadores'))


@gestor_bp.route('/utilizadores/<int:user_id>/toggle-ativo', methods=['POST'])
@perm_required('acesso_gestor')
def toggle_ativo_utilizador(user_id: int):
    current_user = session['user']

    if user_id == current_user['id']:
        flash('Não pode desativar a sua própria conta.', 'danger')
        return redirect(url_for('gestor.gestao_utilizadores'))

    all_users = db.get_all_users()
    target = next((u for u in all_users if u['id'] == user_id), None)
    if not target:
        flash('Utilizador não encontrado.', 'warning')
        return redirect(url_for('gestor.gestao_utilizadores'))

    new_ativo = not target['ativo']
    db.update_user(user_id, ativo=new_ativo)

    if not new_ativo:
        db.revoke_user_sessions(user_id)

    status = 'reativado' if new_ativo else 'desativado'
    logger.info("User %s %s by %s", target['username'], status, current_user.get('username'))
    log_user_action(
        actor_id=current_user['id'],
        actor_username=current_user['username'],
        action_type='ativado' if new_ativo else 'desativado',
        target_user_id=user_id,
        target_username=target['username'],
    )
    flash(f"Utilizador '{target['username']}' {status} com sucesso.", 'success')
    return redirect(url_for('gestor.gestao_utilizadores'))


def _handle_config_post(action, config_option):
    section = request.form.get('section', config_option)

    if action == 'save_regras':
        produtos_config = db.get_produtos_vendas_config()
        updates = []
        for p in produtos_config:
            pid = p['id']
            updates.append({
                'id': pid,
                'gelado_kpi': request.form.get(f'gelado_kpi_{pid}') == 'on',
                'pastelaria': request.form.get(f'pastelaria_{pid}') == 'on',
                'confeitaria': request.form.get(f'confeitaria_{pid}') == 'on',
            })
        db.update_produtos_vendas_config_batch(updates)
        flash('Alocação de Produtos atualizada com sucesso!', 'success')

    elif action == 'add_ajuste':
        mes = int(request.form.get('ajuste_mes', 1))
        kg = parse_decimal_input(request.form.get('ajuste_kg'))
        desc = request.form.get('ajuste_desc', '').strip() or None
        if kg > 0:
            current_year = date.today().year
            db.add_ajuste_producao(current_year, mes, kg, desc)
            meses = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                     'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
            flash(f'Ajuste de -{kg:.2f} kg adicionado a {meses[mes - 1]}!', 'success')
        else:
            flash('Insira uma quantidade válida.', 'warning')

    elif action == 'delete_ajuste':
        ajuste_id = int(request.form.get('ajuste_id'))
        db.delete_ajuste_producao(ajuste_id)
        flash('Ajuste eliminado!', 'success')

    elif action == 'add_motivo':
        nome = request.form.get('novo_motivo', '').strip()
        if nome:
            success = db.add_motivo_quebra(nome)
            flash(f"Motivo '{nome}' adicionado!" if success else 'Motivo já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_motivo':
        mid = int(request.form.get('motivo_id'))
        db.delete_motivo_quebra(mid)
        flash('Motivo eliminado!', 'success')

    elif action == 'add_cobertura':
        nome = request.form.get('nova_cobertura', '').strip()
        if nome:
            success = db.add_cobertura(nome)
            flash(f"Cobertura '{nome}' adicionada!" if success else 'Cobertura já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_cobertura':
        cid = int(request.form.get('cobertura_id'))
        db.delete_cobertura(cid)
        flash('Cobertura eliminada!', 'success')

    elif action == 'add_tipologia':
        nome = request.form.get('nova_tipologia', '').strip()
        if nome:
            success = db.add_tipologia_pastelaria(nome)
            flash(f"Tipologia '{nome}' adicionada!" if success else 'Tipologia já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_tipologia':
        tid = int(request.form.get('tipologia_id'))
        db.delete_tipologia_pastelaria(tid)
        flash('Tipologia eliminada!', 'success')

    elif action == 'save_produtos_past':
        produtos_past = db.get_all_produtos_pastelaria()
        for p in produtos_past:
            pid = p['id']
            tip_new = request.form.get(f'tipologia_{pid}', '')
            sabor_new = request.form.get(f'sabor_{pid}', '')
            cob_new = request.form.get(f'cobertura_{pid}', '')
            tip_orig = p.get('tipologia', '')
            sabor_orig = p.get('sabor', '') or ''
            cob_orig = p.get('cobertura', '') or ''
            if tip_new != tip_orig or sabor_new != sabor_orig or cob_new != cob_orig:
                db.update_produto_pastelaria(pid, tip_new, sabor_new, cob_new)
        flash('Alterações guardadas!', 'success')

    elif action == 'add_produto_past':
        tip = request.form.get('novo_tipologia', '')
        sabor = request.form.get('novo_sabor_past', '')
        cob = request.form.get('novo_cob_past', '')
        if tip:
            success = db.add_produto_pastelaria(tip, sabor, cob)
            flash('Produto adicionado!' if success else 'Produto já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, selecione uma tipologia.', 'warning')

    elif action == 'delete_produto_past':
        pid = int(request.form.get('produto_past_id'))
        db.delete_produto_pastelaria(pid)
        flash('Produto eliminado!', 'success')

    elif action == 'save_gelado_tip':
        gelado_tip = db.get_gelado_por_tipologia()
        for item in gelado_tip:
            gid = item['id']
            tip_new = request.form.get(f'gelado_tip_nome_{gid}', '')
            qtd_new = parse_decimal_input(request.form.get(f'gelado_tip_qtd_{gid}'))
            if tip_new != item['tipologia'] or qtd_new != item['quantidade_gelado_g']:
                db.update_gelado_por_tipologia(gid, tip_new, qtd_new)
        flash('Alterações guardadas!', 'success')

    elif action == 'add_gelado_tip':
        tip = request.form.get('nova_tip_gelado', '').strip()
        qtd = parse_decimal_input(request.form.get('nova_qtd_gelado'))
        if tip:
            success = db.add_gelado_por_tipologia(tip, qtd)
            flash(f"Tipologia '{tip}' adicionada!" if success else 'Tipologia já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira uma tipologia.', 'warning')

    elif action == 'delete_gelado_tip':
        gid = int(request.form.get('gelado_tip_id'))
        db.delete_gelado_por_tipologia(gid)
        flash('Tipologia eliminada!', 'success')

    elif action == 'add_produto_conf':
        nome = request.form.get('novo_prod_conf', '').strip()
        if nome:
            success = db.add_produto_confeitaria(nome)
            flash(f"Produto '{nome}' adicionado!" if success else 'Produto já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_produto_conf':
        pid = int(request.form.get('produto_conf_id'))
        db.delete_produto_confeitaria(pid)
        flash('Produto eliminado!', 'success')

    elif action == 'add_produto_rec':
        nome = request.form.get('novo_prod_rec', '').strip()
        tipo = request.form.get('tipo_prod_rec', 'embalagem')
        if nome:
            success = db.add_produto_rececao(nome, tipo)
            flash(f"Produto '{nome}' adicionado!" if success else 'Produto já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_produto_rec':
        pid = int(request.form.get('produto_rec_id'))
        db.delete_produto_rececao(pid)
        flash('Produto eliminado!', 'success')

    elif action == 'save_receitas':
        receitas = db.get_all_receitas_gelado()
        for r in receitas:
            rid = r['id']
            new_nome_corrente = request.form.get(f'sabor_{rid}', '').strip() or None
            new_ativo = request.form.get(f'ativo_{rid}') == 'on'
            new_conta_eurokg = request.form.get(f'conta_eurokg_{rid}') == 'on'
            old_nome_corrente = r.get('nome_corrente') or None
            old_ativo = r.get('ativo', True)
            old_conta_eurokg = r.get('conta_eurokg', True)
            if new_nome_corrente != old_nome_corrente:
                db.update_receita_gelado(rid, r['nome'], new_nome_corrente)
            if new_ativo != old_ativo:
                db.update_receita_gelado_ativo(rid, new_ativo)
            if new_conta_eurokg != old_conta_eurokg:
                db.update_receita_gelado_conta_eurokg(rid, new_conta_eurokg)
        flash('Receitas atualizadas com sucesso!', 'success')

    elif action == 'add_receita':
        nome = request.form.get('nova_receita', '').strip()
        nome_corrente = request.form.get('novo_sabor', '').strip() or None
        if nome:
            db.add_receita_gelado(nome, nome_corrente)
            flash(f"Receita '{nome}' adicionada!", 'success')
        else:
            flash('Por favor, insira o nome da receita.', 'warning')

    elif action == 'delete_receita':
        rid = int(request.form.get('receita_id'))
        db.delete_receita_gelado(rid)
        flash('Receita eliminada!', 'success')

    elif action == 'save_users_perms':
        current_user = session['user']
        users = db.get_all_users()
        lojas_venda = db.get_vendas_module_stores()
        loja_ids = [loja['id'] for loja in lojas_venda]
        _perm_fields = ['acesso_eurokg', 'acesso_producao', 'acesso_pastelaria', 'acesso_confeitaria',
                        'acesso_administrativo', 'acesso_gestor', 'acesso_financeiro', 'acesso_eventos',
                        'acesso_tarefas', 'ativo']
        before_map = {u['id']: u for u in users}
        updates = []
        for u in users:
            uid = u['id']
            vendas_store_ids = [
                sid for sid in loja_ids
                if request.form.get(f'venda_loja_{sid}_{uid}') == 'on'
            ]
            updates.append({
                'id': uid,
                'acesso_eurokg': request.form.get(f'acesso_eurokg_{uid}') == 'on',
                'acesso_producao': request.form.get(f'acesso_producao_{uid}') == 'on',
                'acesso_pastelaria': request.form.get(f'acesso_pastelaria_{uid}') == 'on',
                'acesso_confeitaria': request.form.get(f'acesso_confeitaria_{uid}') == 'on',
                'acesso_administrativo': request.form.get(f'acesso_administrativo_{uid}') == 'on',
                'acesso_gestor': request.form.get(f'acesso_gestor_{uid}') == 'on',
                'acesso_financeiro': request.form.get(f'acesso_financeiro_{uid}') == 'on',
                'acesso_eventos': request.form.get(f'acesso_eventos_{uid}') == 'on',
                'acesso_tarefas': request.form.get(f'acesso_tarefas_{uid}') == 'on',
                'ativo': request.form.get(f'ativo_{uid}') == 'on',
                'vendas_store_ids': vendas_store_ids,
            })
        db.update_user_permissoes_batch(updates)
        for upd in updates:
            uid = upd['id']
            before = before_map.get(uid, {})
            before_perms = {f: bool(before.get(f)) for f in _perm_fields}
            before_perms['vendas_store_ids'] = sorted(before.get('vendas_store_ids', []))
            after_perms = {f: upd.get(f, False) for f in _perm_fields}
            after_perms['vendas_store_ids'] = sorted(upd.get('vendas_store_ids', []))
            if before_perms != after_perms:
                log_user_action(
                    actor_id=current_user['id'],
                    actor_username=current_user['username'],
                    action_type='permissoes_alteradas',
                    target_user_id=uid,
                    target_username=before.get('username', str(uid)),
                    details={'antes': before_perms, 'depois': after_perms},
                )
        flash('Permissões atualizadas com sucesso!', 'success')

    elif action == 'add_user':
        current_user = session['user']
        username = request.form.get('novo_username', '').strip()
        password = request.form.get('nova_password', '')
        if username and password:
            success = db.add_user(username, password)
            if success:
                new_users = db.get_all_users()
                new_user = next((u for u in new_users if u['username'] == username), None)
                log_user_action(
                    actor_id=current_user['id'],
                    actor_username=current_user['username'],
                    action_type='criado',
                    target_user_id=new_user['id'] if new_user else None,
                    target_username=username,
                    details={'role': new_user['role'] if new_user else None},
                )
                flash(f"Utilizador '{username}' criado! Configure os acessos na tabela acima.", 'success')
            else:
                flash('Utilizador já existe.', 'warning')
        else:
            flash('Preencha o utilizador e a password.', 'warning')

    elif action == 'change_password':
        current_user = session['user']
        try:
            user_id = int(request.form.get('user_id'))
        except (TypeError, ValueError):
            flash('Utilizador inválido.', 'danger')
            return redirect(url_for('gestor.gestao_utilizadores'))
        new_pass = request.form.get('new_password', '')
        if new_pass:
            try:
                all_users = db.get_all_users()
                target = next((u for u in all_users if u['id'] == user_id), None)
                db.update_user_password(user_id, new_pass)
                if target:
                    log_user_action(
                        actor_id=current_user['id'],
                        actor_username=current_user['username'],
                        action_type='password_alterada',
                        target_user_id=user_id,
                        target_username=target['username'],
                        details={'alterado_por': current_user['username']},
                    )
                flash('Password alterada com sucesso!', 'success')
            except Exception as exc:
                import logging as _logging
                _logging.getLogger(__name__).exception('update_user_password failed for user_id=%s', user_id)
                flash('Erro ao alterar a password. Tente novamente.', 'danger')
        else:
            flash('Introduza a nova password.', 'warning')

    endpoint = SECTION_ENDPOINT_MAP.get(section, 'gestor.index')
    return redirect(url_for(endpoint))


@gestor_bp.route('/premio-eurokg')
@perm_required('acesso_gestor')
def premio_eurokg():
    from calendar import monthrange
    from datetime import date as _date

    today = _date.today()
    ano = int(request.args.get('ano', today.year))
    loja = request.args.get('loja', 'Porto (Global)')

    meses_nomes = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                   'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

    dados_meses = []
    erro = None

    try:
        for mes in range(1, 13):
            kpi = db.calculate_kpi_monthly(ano, mes, loja)
            target = db.get_target_by_month(mes)

            consumo = kpi['consumo']
            vendas = kpi['vendas']
            vendas_teoricas = round(consumo * target, 2) if consumo > 0 else 0.0
            ganho_extra = round(vendas - vendas_teoricas, 2)
            premio = round(max(0.0, ganho_extra * 0.10), 2)

            is_future = (ano > today.year) or (ano == today.year and mes >= today.month)

            dados_meses.append({
                'mes': mes,
                'mes_nome': meses_nomes[mes - 1],
                'consumo': round(consumo, 3),
                'vendas': round(vendas, 2),
                'target': round(target, 2),
                'vendas_teoricas': round(vendas_teoricas, 2),
                'ganho_extra': round(ganho_extra, 2),
                'premio': round(premio, 2),
                'is_future': is_future,
                'kpi_real': round(kpi['kpi'], 2),
                'colaboradores_elegiveis': 0,
                'premio_por_colaborador': 0.0,
            })
    except Exception as e:
        erro = str(e)

    return render_template('gestor/premio_eurokg.html',
                           dados_meses=dados_meses,
                           erro=erro,
                           ano_sel=ano,
                           loja_sel=loja,
                           meses_nomes=meses_nomes,
                           back_url=url_for('gestor.eurokg_index'))


@gestor_bp.route('/gestao-lojas', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def gestao_lojas():
    tabs = get_tabs()
    msg = None
    erro = None
    edit_store = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upsert_store':
            store_id = request.form.get('store_id') or None
            if store_id:
                store_id = int(store_id)
            name = request.form.get('name', '').strip()
            address = request.form.get('address', '').strip() or None
            latitude = request.form.get('latitude', '').strip() or None
            longitude = request.form.get('longitude', '').strip() or None
            store_type = request.form.get('store_type', 'loja').strip()
            is_active = request.form.get('is_active') == '1'
            receives_transfers = request.form.get('receives_transfers') == '1'
            requires_eod_weighing = request.form.get('requires_eod_weighing') == '1'
            shows_on_landing = request.form.get('shows_on_landing') == '1'
            pos_store_code = request.form.get('pos_store_code', '').strip() or None
            opened_at = request.form.get('opened_at', '').strip() or None

            if latitude:
                try:
                    latitude = float(latitude)
                except ValueError:
                    latitude = None
            if longitude:
                try:
                    longitude = float(longitude)
                except ValueError:
                    longitude = None

            if not name:
                erro = 'O nome da loja é obrigatório.'
            else:
                ok = db.upsert_store(store_id, name, address, latitude, longitude,
                                     store_type, is_active, receives_transfers,
                                     requires_eod_weighing, pos_store_code, opened_at,
                                     shows_on_landing)
                if ok:
                    msg = 'Loja guardada com sucesso.'
                else:
                    erro = 'Erro ao guardar loja. O nome pode já existir.'

        elif action == 'toggle_active':
            store_id = int(request.form.get('store_id'))
            is_active = request.form.get('is_active') == '1'
            db.toggle_store_active(store_id, is_active)
            msg = 'Estado da loja actualizado.'

        elif action == 'delete_store':
            store_id = int(request.form.get('store_id'))
            db.delete_store(store_id)
            msg = 'Loja eliminada.'

        return redirect(url_for('gestor.gestao_lojas', msg=msg, erro=erro))

    msg = request.args.get('msg')
    erro = request.args.get('erro')
    edit_id = request.args.get('edit')
    edit_store = None
    edit_store_aliases = []
    if edit_id:
        edit_store = db.get_store(int(edit_id))
        if edit_store:
            edit_store_aliases = db.get_store_aliases(int(edit_id))

    stores = db.get_all_stores()
    return render_template('gestor/gestao_lojas.html',
                           tabs=tabs,
                           stores=stores,
                           edit_store=edit_store,
                           edit_store_aliases=edit_store_aliases,
                           msg=msg,
                           erro=erro,
                           active_tab='gestao_lojas',
                           back_url=url_for('gestor.index'))


@gestor_bp.route('/gestao-lojas/<int:store_id>/aliases', methods=['POST'])
@perm_required('acesso_gestor')
def add_store_alias(store_id):
    alias_name = request.form.get('alias_name', '').strip() or None
    alias_code = request.form.get('alias_code', '').strip() or None
    if alias_name or alias_code:
        db.add_store_alias(store_id, alias_name, alias_code)
        flash('Alias adicionado com sucesso.', 'success')
    else:
        flash('Indique pelo menos um nome alternativo ou código alternativo.', 'warning')
    return redirect(url_for('gestor.gestao_lojas', edit=store_id))


@gestor_bp.route('/gestao-lojas/<int:store_id>/aliases/<int:alias_id>/delete', methods=['POST', 'DELETE'])
@perm_required('acesso_gestor')
def delete_store_alias(store_id, alias_id):
    db.delete_store_alias(alias_id, store_id=store_id)
    flash('Alias removido.', 'success')
    return redirect(url_for('gestor.gestao_lojas', edit=store_id))


@gestor_bp.route('/metodos-pagamento', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def metodos_pagamento():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'save':
            metodo = request.form.get('metodo', '').strip()
            label = request.form.get('label', '').strip()
            ativo = request.form.get('ativo') == 'on'
            try:
                taxa_raw = request.form.get('taxa_percentagem', '').strip()
                taxa = float(taxa_raw.replace(',', '.')) if taxa_raw else None
            except ValueError:
                taxa = None
            try:
                prazo_raw = request.form.get('prazo_dias', '').strip()
                prazo = int(prazo_raw) if prazo_raw else None
            except ValueError:
                prazo = None
            notas = request.form.get('notas', '').strip() or None
            if not metodo or not label:
                flash('Método e label são obrigatórios.', 'warning')
            else:
                try:
                    upsert_payment_method_config(metodo, label, ativo, taxa, prazo, notas)
                    flash(f'Método «{label}» guardado.', 'success')
                except Exception as e:
                    logger.error('Erro ao guardar método pagamento: %s', e)
                    flash('Não foi possível guardar. Tente novamente.', 'error')
        return redirect(url_for('gestor.metodos_pagamento'))

    metodos = get_payment_methods_config()
    return render_template('gestor/metodos_pagamento.html', metodos=metodos)


@gestor_bp.route('/configuracoes', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def configuracoes():
    _ONEDRIVE_DEFAULT_FOLDER = 'Scoopy/2. Contabilidade/Registo de Faturas'
    _ONEDRIVE_LEGACY_DEFAULTS = {
        'NivaPorto/Faturas',              # pre-Scoopy legacy alias
        'Scoopy/Faturas',                 # transitional alias
        'Niva Porto/2. Contabilidade/Registo de Faturas',  # pre-Scoopy default
    }

    if request.method == 'POST':
        folder = request.form.get('onedrive_folder', '').strip()
        db.set_system_config('onedrive_folder', folder or _ONEDRIVE_DEFAULT_FOLDER)
        flash('Configuração guardada com sucesso.', 'success')
        return redirect(url_for('gestor.configuracoes'))

    onedrive_user = db.get_system_config('onedrive_user_email') or ''
    onedrive_token = db.get_system_config('onedrive_refresh_token') or ''
    _stored_folder = (db.get_system_config('onedrive_folder') or '').strip('/')
    if not _stored_folder or _stored_folder in _ONEDRIVE_LEGACY_DEFAULTS:
        onedrive_folder = _ONEDRIVE_DEFAULT_FOLDER
        try:
            db.set_system_config('onedrive_folder', _ONEDRIVE_DEFAULT_FOLDER)
        except Exception:
            pass
    else:
        onedrive_folder = _stored_folder
    onedrive_connected = bool(onedrive_token)

    redirect_uri = url_for('gestor.onedrive_callback', _external=True)

    return render_template('gestor/configuracoes.html',
                           active_tab='configuracoes',
                           onedrive_connected=onedrive_connected,
                           onedrive_user=onedrive_user,
                           onedrive_folder=onedrive_folder,
                           redirect_uri=redirect_uri,
                           back_url=url_for('gestor.index'))


@gestor_bp.route('/configuracoes/onedrive-autorizar')
@perm_required('acesso_gestor')
def onedrive_autorizar():
    import secrets as _secrets
    from urllib.parse import urlencode

    client_id = os.environ.get('AZURE_CLIENT_ID', '')
    if not client_id:
        flash('AZURE_CLIENT_ID não configurado nos secrets do Replit.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    state = _secrets.token_urlsafe(24)
    session['_onedrive_state'] = state

    redirect_uri = url_for('gestor.onedrive_callback', _external=True)

    params = urlencode({
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': 'Files.ReadWrite offline_access User.Read',
        'state': state,
        'prompt': 'select_account',
    })
    auth_url = f'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{params}'
    return redirect(auth_url)


@gestor_bp.route('/onedrive/callback')
@perm_required('acesso_gestor')
def onedrive_callback():
    import requests as _requests

    error = request.args.get('error')
    if error:
        desc = request.args.get('error_description', error)
        flash(f'Autorização recusada: {desc}', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    state = request.args.get('state', '')
    if state != session.pop('_onedrive_state', None):
        flash('Estado inválido — tente novamente.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    code = request.args.get('code', '')
    if not code:
        flash('Código de autorização em falta.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    redirect_uri = url_for('gestor.onedrive_callback', _external=True)
    client_id = os.environ.get('AZURE_CLIENT_ID', '')
    client_secret = os.environ.get('AZURE_CLIENT_SECRET', '')

    token_resp = _requests.post(
        'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        data={
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
            'redirect_uri': redirect_uri,
            'scope': 'Files.ReadWrite offline_access User.Read',
        },
        timeout=20,
    )

    if not token_resp.ok:
        flash(f'Erro ao obter tokens: {token_resp.status_code} {token_resp.text[:200]}', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    token_data = token_resp.json()
    refresh_token = token_data.get('refresh_token', '')
    access_token = token_data.get('access_token', '')

    if not refresh_token:
        flash('Refresh token não recebido. Certifique-se que o scope "offline_access" está autorizado.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    user_email = ''
    if access_token:
        try:
            me_resp = _requests.get(
                'https://graph.microsoft.com/v1.0/me',
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=10,
            )
            if me_resp.ok:
                user_email = me_resp.json().get('userPrincipalName') or me_resp.json().get('mail', '')
        except Exception as e:
            logger.warning("Could not fetch user email after OAuth: %s", e)

    db.set_system_config('onedrive_refresh_token', refresh_token)
    db.set_system_config('onedrive_user_email', user_email)

    flash(f'OneDrive autorizado com sucesso! Conta: {user_email or "desconhecida"}', 'success')
    return redirect(url_for('gestor.configuracoes'))


@gestor_bp.route('/configuracoes/onedrive-desligar', methods=['POST'])
@perm_required('acesso_gestor')
def onedrive_desligar():
    refresh_token = db.get_system_config('onedrive_refresh_token') or ''

    if refresh_token:
        try:
            import requests as _requests
            _requests.post(
                'https://login.microsoftonline.com/common/oauth2/v2.0/logout',
                data={
                    'client_id': os.environ.get('AZURE_CLIENT_ID', ''),
                    'client_secret': os.environ.get('AZURE_CLIENT_SECRET', ''),
                    'token': refresh_token,
                    'token_type_hint': 'refresh_token',
                },
                timeout=10,
            )
        except Exception as e:
            logger.warning("Best-effort OneDrive token revocation failed (ignoring): %s", e)

    db.set_system_config('onedrive_refresh_token', None)
    db.set_system_config('onedrive_user_email', None)
    flash('Ligação OneDrive removida.', 'warning')
    return redirect(url_for('gestor.configuracoes'))


@gestor_bp.route('/configuracoes/testar-onedrive', methods=['POST'])
@perm_required('acesso_gestor')
def testar_onedrive():
    from flask_app.onedrive_archive import test_connection
    result = test_connection()
    return jsonify(result)


# ── Catálogo de Materiais ──────────────────────────────────────────────────────

@gestor_bp.route('/materiais', methods=['GET'])
@perm_required('acesso_gestor')
def materiais():
    from db.materiais import list_materiais, CATEGORIAS_MATERIAIS, UNIDADES_MATERIAIS
    lista = list_materiais()
    por_categoria = {}
    for m in lista:
        cat = m['categoria']
        por_categoria.setdefault(cat, []).append(m)
    return render_template(
        'gestor/materiais.html',
        tabs=get_tabs(),
        active_tab='materiais',
        materiais=lista,
        por_categoria=por_categoria,
        categorias=CATEGORIAS_MATERIAIS,
        unidades=UNIDADES_MATERIAIS,
    )


@gestor_bp.route('/materiais', methods=['POST'])
@perm_required('acesso_gestor')
def materiais_post():
    from db.materiais import (upsert_material, toggle_material_ativo,
                               CATEGORIAS_MATERIAIS, UNIDADES_MATERIAIS)
    action = request.form.get('action', '')

    if action == 'add_material':
        nome = request.form.get('nome', '').strip()
        unidade = request.form.get('unidade', 'un')
        categoria = request.form.get('categoria', 'Outro')
        fornecedor = request.form.get('fornecedor', '').strip() or None
        if not nome:
            flash('O nome do material é obrigatório.', 'warning')
        elif unidade not in UNIDADES_MATERIAIS:
            flash('Unidade inválida.', 'warning')
        elif categoria not in CATEGORIAS_MATERIAIS:
            flash('Categoria inválida.', 'warning')
        else:
            upsert_material(nome, unidade, categoria, fornecedor)
            flash(f"Material '{nome}' adicionado com sucesso!", 'success')

    elif action == 'edit_material':
        mid = int(request.form.get('material_id', 0) or 0)
        nome = request.form.get('nome', '').strip()
        unidade = request.form.get('unidade', 'un')
        categoria = request.form.get('categoria', 'Outro')
        fornecedor = request.form.get('fornecedor', '').strip() or None
        if not nome:
            flash('O nome do material é obrigatório.', 'warning')
        elif not mid:
            flash('Material inválido.', 'warning')
        elif unidade not in UNIDADES_MATERIAIS:
            flash('Unidade inválida.', 'warning')
        elif categoria not in CATEGORIAS_MATERIAIS:
            flash('Categoria inválida.', 'warning')
        else:
            upsert_material(nome, unidade, categoria, fornecedor, material_id=mid)
            flash(f"Material '{nome}' atualizado!", 'success')

    elif action == 'toggle_ativo':
        mid = int(request.form.get('material_id', 0))
        ativo = request.form.get('ativo') == 'true'
        toggle_material_ativo(mid, ativo)
        estado = 'ativado' if ativo else 'desativado'
        flash(f'Material {estado}.', 'success')

    return redirect(url_for('gestor.materiais'))


@gestor_bp.route('/gestao-tarefas', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def gestao_tarefas():
    from db.tarefas import (
        get_all_tarefas, get_all_active_stores,
        create_tarefa, update_tarefa, toggle_tarefa_ativa,
        bulk_delete_tarefas, bulk_update_tarefa_field,
        DIAS_SEMANA, FREQUENCIAS, TIPOS, EQUIPAS,
    )

    if request.method == 'POST':
        action = request.form.get('action', '')

        def _parse_tarefa_fields():
            nome = request.form.get('nome', '').strip()
            tipo = request.form.get('tipo', '')
            frequencia = request.form.get('frequencia', '') or None
            raw_dia_semana = request.form.get('dia_semana') or None
            raw_dia_mes = request.form.get('dia_mes') or None
            utilizador_id = request.form.get('utilizador_id') or None
            loja_id = request.form.get('loja_id') or None
            equipa = request.form.get('equipa', '') or None
            errors = []
            if not nome:
                errors.append('O nome da tarefa é obrigatório.')
            if tipo not in TIPOS:
                errors.append('Momento inválido (abertura ou fecho).')
            if frequencia is not None and frequencia not in FREQUENCIAS:
                errors.append('Frequência inválida.')
            if not utilizador_id:
                errors.append('É obrigatório atribuir um responsável.')
            if not loja_id:
                errors.append('É obrigatório associar uma loja.')
            if not equipa or equipa not in EQUIPAS:
                errors.append('É obrigatório selecionar uma equipa (Produção, Vendas, Logística ou Compras).')
            dia_semana = None
            dia_mes = None
            if frequencia == 'semanal':
                if raw_dia_semana is None:
                    errors.append('Selecione o dia da semana para tarefas semanais.')
                else:
                    try:
                        dia_semana = int(raw_dia_semana)
                        if not 0 <= dia_semana <= 6:
                            raise ValueError
                    except ValueError:
                        errors.append('Dia da semana inválido.')
            elif frequencia == 'mensal':
                if raw_dia_mes is None:
                    errors.append('Indique o dia do mês para tarefas mensais.')
                else:
                    try:
                        dia_mes = int(raw_dia_mes)
                        if not 1 <= dia_mes <= 31:
                            raise ValueError
                    except ValueError:
                        errors.append('Dia do mês deve estar entre 1 e 31.')
            uid = None
            if utilizador_id:
                try:
                    uid = int(utilizador_id)
                except (ValueError, TypeError):
                    errors.append('Responsável inválido.')
            lid = None
            if loja_id:
                try:
                    lid = int(loja_id)
                    valid_loja_ids = {s['id'] for s in get_all_active_stores()}
                    if lid not in valid_loja_ids:
                        errors.append('Loja selecionada não é válida ou está inativa.')
                        lid = None
                except (ValueError, TypeError):
                    errors.append('Loja inválida.')
            return nome, tipo, frequencia, dia_semana, dia_mes, uid, lid, equipa, errors

        if action == 'create':
            nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id, loja_id, equipa, errors = _parse_tarefa_fields()
            if errors:
                for e in errors:
                    flash(e, 'warning')
            else:
                create_tarefa(nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id, loja_id, equipa)
                flash(f"Tarefa '{nome}' criada com sucesso.", 'success')

        elif action == 'edit':
            tarefa_id = int(request.form.get('tarefa_id', 0))
            nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id, loja_id, equipa, errors = _parse_tarefa_fields()
            if errors:
                for e in errors:
                    flash(e, 'warning')
            else:
                update_tarefa(tarefa_id, nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id, loja_id, equipa)
                flash('Tarefa atualizada com sucesso.', 'success')

        elif action == 'toggle':
            tarefa_id = int(request.form.get('tarefa_id', 0))
            novo_estado = toggle_tarefa_ativa(tarefa_id)
            estado_label = 'ativada' if novo_estado else 'desativada'
            flash(f'Tarefa {estado_label}.', 'success')

        elif action == 'bulk_delete':
            raw_ids = request.form.getlist('tarefa_ids[]')
            ids = [int(i) for i in raw_ids if i.isdigit()]
            if ids:
                n = bulk_delete_tarefas(ids)
                flash(f'{n} tarefa(s) eliminada(s).', 'success')
            else:
                flash('Nenhuma tarefa selecionada.', 'warning')

        elif action == 'bulk_update':
            raw_ids = request.form.getlist('tarefa_ids[]')
            ids = [int(i) for i in raw_ids if i.isdigit()]
            field = request.form.get('bulk_field', '').strip()
            value = request.form.get('bulk_value', '').strip()
            if ids and field:
                try:
                    n = bulk_update_tarefa_field(ids, field, value)
                    flash(f'{n} tarefa(s) atualizadas.', 'success')
                except ValueError as exc:
                    flash(str(exc), 'warning')
            else:
                flash('Selecione tarefas e um campo para editar.', 'warning')

        elif action == 'bulk_create':
            nomes_list = request.form.getlist('nome[]')
            tipos_list = request.form.getlist('tipo[]')
            freqs_list = request.form.getlist('frequencia[]')
            lojas_list = request.form.getlist('loja_id[]')
            uids_list = request.form.getlist('utilizador_id[]')
            equipas_list = request.form.getlist('equipa[]')
            saved = 0
            for i, nome_raw in enumerate(nomes_list):
                nome_raw = nome_raw.strip()
                if not nome_raw:
                    continue
                t_tipo = tipos_list[i] if i < len(tipos_list) else ''
                if t_tipo not in TIPOS:
                    continue
                t_freq = (freqs_list[i] if i < len(freqs_list) else '') or None
                t_loja_str = lojas_list[i] if i < len(lojas_list) else ''
                t_uid_str = uids_list[i] if i < len(uids_list) else ''
                t_equipa = equipas_list[i] if i < len(equipas_list) else ''
                t_lid = int(t_loja_str) if t_loja_str.isdigit() else None
                t_uid = int(t_uid_str) if t_uid_str.isdigit() else None
                t_equipa = t_equipa if t_equipa in EQUIPAS else None
                create_tarefa(nome_raw, t_tipo, t_freq, None, None, t_uid, t_lid, t_equipa)
                saved += 1
            if saved:
                flash(f'{saved} tarefa(s) criada(s) com sucesso.', 'success')
            else:
                flash('Nenhuma tarefa válida para criar.', 'warning')

        return redirect(url_for('gestor.gestao_tarefas'))

    tarefas = get_all_tarefas()
    users = db.get_all_users()
    users_ativos = [u for u in users if u.get('ativo')]
    stores = get_all_active_stores()
    return render_template(
        'gestor/gestao_tarefas.html',
        tarefas=tarefas,
        users=users_ativos,
        stores=stores,
        dias_semana=DIAS_SEMANA,
        frequencias=FREQUENCIAS,
        tipos=TIPOS,
        equipas=EQUIPAS,
        back_url=url_for('gestor.index'),
    )


_TILE_MASTER = {
    'producao': [
        {'id': 'pesagens_loja',     'icon': '⚖️',  'default_label': 'Pesagens de Loja',            'description': 'Registo diário do stock de gelado em expositor por sabor no fim do dia'},
        {'id': 'registo_producao',  'icon': '📸',  'default_label': 'Registo de Produção',          'description': 'Lançar produção de gelado por sabor e quantidade (manual ou via OCR)'},
        {'id': 'transferir',        'icon': '🔄',  'default_label': 'Transferir para Loja',         'description': 'Criar ordens de transferência de gelado da produção para a loja'},
        {'id': 'ordem',             'icon': '🔢',  'default_label': 'Ordem de Produção',            'description': 'Planear as quantidades a produzir por sabor para o dia seguinte'},
        {'id': 'por_sabor',         'icon': '🍨',  'default_label': 'Stock Gelado',                 'description': 'Consultar o stock atual de gelado por sabor em cada loja'},
        {'id': 'quebra',            'icon': '⚠️',  'default_label': 'Registar Quebra de Produção',  'description': 'Registar perdas ou desperdícios de gelado com motivo justificativo'},
        {'id': 'dashboard',         'icon': '📊',  'default_label': 'Dashboard Produção',           'description': 'Resumo visual de produção, transferências e stock por período'},
        {'id': 'receitas',          'icon': '📖',  'default_label': 'Receitas de Gelado',           'description': 'Consultar e gerir as receitas e componentes de cada sabor'},
        {'id': 'sabores_ativos',    'icon': '✅',  'default_label': 'Sabores Ativos',               'description': 'Lista dos sabores em produção ativa e respetivas tipologias'},
    ],
    'vendas': [
        {'id': 'dashboard',         'icon': '📊',  'default_label': 'Resumo Diário',                'description': 'Resumo das vendas do dia por loja com totais e indicadores de performance'},
        {'id': 'transferencias',    'icon': '📦',  'default_label': 'Receção de Mercadoria',        'description': 'Confirmar a receção de transferências enviadas pela produção e pastelaria'},
        {'id': 'quebras',           'icon': '⚠️',  'default_label': 'Registar Quebras',             'description': 'Registar quebras de gelado, pastelaria e confeitaria com motivo justificativo'},
        {'id': 'pesagem',           'icon': '⚖️',  'default_label': 'Pesagem Fim de Dia',           'description': 'Registar o stock de gelado em expositor no fecho do dia por sabor'},
        {'id': 'fecho_caixa',       'icon': '💵',  'default_label': 'Fecho de Caixa',              'description': 'Lançar os totais do fecho de caixa diário por método de pagamento'},
        {'id': 'sabores_ativos',    'icon': '✅',  'default_label': 'Sabores Ativos',              'description': 'Lista dos sabores de gelado disponíveis e ativos em loja'},
        {'id': 'fecho_historico',   'icon': '📋',  'default_label': 'Histórico Caixa',             'description': 'Consultar, corrigir e auditar o histórico completo de fechos de caixa'},
    ],
    'pastelaria': [
        {'id': 'stock_balcao',      'icon': '📦',  'default_label': 'Visão de Stock',              'description': 'Ver o stock atual de pastelaria e confeitaria disponível em loja'},
        {'id': 'planear',           'icon': '📋',  'default_label': 'Planear Produção',            'description': 'Definir as quantidades a produzir por produto para o dia seguinte'},
        {'id': 'produzir',          'icon': '▶️',  'default_label': 'Produzir',                    'description': 'Registar a produção realizada de produtos de pastelaria e confeitaria'},
        {'id': 'transferir',        'icon': '🔄',  'default_label': 'Transferir para Loja',        'description': 'Enviar stock de pastelaria e confeitaria para a loja via ordem de transferência'},
        {'id': 'quebra',            'icon': '⚠️',  'default_label': 'Registar Quebra',             'description': 'Registar perdas de produtos de pastelaria e confeitaria com motivo'},
        {'id': 'reconciliacao',     'icon': '📊',  'default_label': 'Reconciliação',               'description': 'Acertar o stock de pastelaria e confeitaria com as contagens físicas em loja'},
        {'id': 'gerir_produtos',    'icon': '🍡',  'default_label': 'Gerir Produtos',              'description': 'Adicionar, editar e arquivar produtos de pastelaria e confeitaria'},
    ],
    'financeiro': [
        {'id': 'faturas',                     'icon': '📄',  'default_label': 'Documentos',                    'description': 'Gerir faturas de fornecedores, pagamentos, IVA e OCR automático de documentos'},
        {'id': 'credito',                     'icon': '💳',  'default_label': 'Crédito',                       'description': 'Acompanhar contratos de crédito, parcelas e responsabilidades financeiras'},
        {'id': 'iva',                         'icon': '📋',  'default_label': 'IVA',                           'description': 'Consultar e gerir períodos de IVA e valores a regularizar com o Estado'},
        {'id': 'liquidez',                    'icon': '🏦',  'default_label': 'Tesouraria Previsional',        'description': 'Previsão de cash flow a 13 semanas por semana e por loja'},
        {'id': 'avencas',                     'icon': '🔁',  'default_label': 'Avenças',                       'description': 'Gerir contratos de avença e pagamentos recorrentes fixos'},
        {'id': 'debitos',                     'icon': '🔄',  'default_label': 'Débitos Diretos',               'description': 'Registar e acompanhar débitos diretos bancários e respetivas datas de vencimento'},
        {'id': 'salarios',                    'icon': '👥',  'default_label': 'Salários',                      'description': 'Gerir folhas de salários, subsídios e encargos sociais mensais'},
        {'id': 'dashboard_vendas',            'icon': '📊',  'default_label': 'Dashboard de Vendas',           'description': 'Resumo visual de vendas consolidadas por loja e período temporal'},
        {'id': 'vendas_diarias',              'icon': '📅',  'default_label': 'Vendas Diárias',                'description': 'Ver e corrigir as vendas diárias registadas por loja e por produto'},
        {'id': 'variaveis_previsao',          'icon': '🌡️', 'default_label': 'Variáveis de Previsão',         'description': 'Configurar fatores meteorológicos e sazonais usados no modelo de previsão de vendas'},
        {'id': 'previsao_30dias',             'icon': '🔮',  'default_label': 'Previsão 30 Dias',              'description': 'Previsão de vendas para os próximos 30 dias com indicadores semanais e ajuste meteo'},
        {'id': 'pl_por_loja',                'icon': '🏪',  'default_label': 'P&L por Loja',                  'description': 'Resultados de exploração — receitas, custos e margem líquida — por loja e período'},
        {'id': 'centros_custo',              'icon': '🏷️', 'default_label': 'Centros de Custo',              'description': 'Alocar custos de faturas a centros de custo e consultar distribuição por categoria'},
        {'id': 'categorias',                 'icon': '📂',  'default_label': 'Categorias de Custo',           'description': 'Gerir as categorias e subcategorias usadas para classificar custos em faturas'},
        {'id': 'distribuicao_centros_custo', 'icon': '📊',  'default_label': 'Distribuição Centros de Custo', 'description': 'Configurar a distribuição percentual de custos entre os centros de custo definidos'},
    ],
    'gestor': [
        {'id': 'upload_producao',        'icon': '📤',  'default_label': 'Upload Produção',        'description': 'Carregar folha de produção em imagem para extração automática de dados via OCR'},
        {'id': 'upload_pesagem',         'icon': '⚖️',  'default_label': 'Upload Pesagem',         'description': 'Carregar folha de pesagem em imagem para extração automática de quantidades via OCR'},
        {'id': 'vendas_detalhe',         'icon': '🛒',  'default_label': 'Vendas Detalhe',         'description': 'Ver detalhe de vendas por produto e método de pagamento discriminado por loja'},
        {'id': 'config_vendas_diarias',  'icon': '📅',  'default_label': 'Filtro Vendas Diárias',  'description': 'Configurar quais produtos e categorias aparecem na vista de vendas diárias'},
        {'id': 'alocacao_produtos',      'icon': '📋',  'default_label': 'Alocação de Produtos',   'description': 'Definir regras de alocação de produtos por loja, categoria e tipologia'},
        {'id': 'ajustes_producao',       'icon': '🔧',  'default_label': 'Ajustes Produção',       'description': 'Corrigir manualmente registos de produção ou pesagem que contenham erros'},
        {'id': 'motivos_quebra',         'icon': '⚠️',  'default_label': 'Motivos Quebra',         'description': 'Gerir a lista de motivos disponíveis ao registar quebras de produto'},
        {'id': 'produtos_rececao',       'icon': '📦',  'default_label': 'Produtos de Venda',      'description': 'Gerir os artigos e produtos disponíveis para venda e receção em loja'},
        {'id': 'receitas_eurokg',        'icon': '🍦',  'default_label': 'Sabores Euro/kg',        'description': 'Configurar o custo por kg de cada sabor para cálculo do KPI de rentabilidade'},
        {'id': 'gestao_utilizadores',    'icon': '👥',  'default_label': 'Utilizadores',            'description': 'Criar, editar e definir permissões de acesso de cada utilizador da aplicação'},
        {'id': 'premio_eurokg',          'icon': '🏆',  'default_label': 'Prémio Euro/kg',         'description': 'Configurar e consultar o prémio de rentabilidade Euro/kg atribuído por loja'},
        {'id': 'gestao_lojas',           'icon': '🏪',  'default_label': 'Lojas',                   'description': 'Gerir as lojas registadas na aplicação e respetivas configurações operacionais'},
        {'id': 'metodos_pagamento',      'icon': '💳',  'default_label': 'Métodos de Pagamento',   'description': 'Gerir os métodos de pagamento disponíveis e ativos para fecho de caixa em loja'},
        {'id': 'materiais',              'icon': '🗂️', 'default_label': 'Catálogo de Materiais',  'description': 'Gerir o catálogo de materiais e matérias-primas utilizadas na produção'},
        {'id': 'gestao_tarefas',         'icon': '✅',  'default_label': 'Gestão de Tarefas',      'description': 'Criar, atribuir e acompanhar tarefas recorrentes e pontuais da equipa'},
        {'id': 'configuracoes',          'icon': '⚙️',  'default_label': 'Configurações',           'description': 'Configurar parâmetros globais da aplicação (fundo de caixa, feriados, etc.)'},
        {'id': 'gestao_tiles',           'icon': '🔲',  'default_label': 'Gestão de Tiles',        'description': 'Ativar, desativar e renomear os tiles de navegação em todos os módulos'},
        {'id': 'centros_custo',          'icon': '🏷️', 'default_label': 'Centros de Custo',        'description': 'Alocar custos a centros de custo e configurar as regras de distribuição'},
        {'id': 'categorias_custo',       'icon': '📂',  'default_label': 'Categorias de Custo',     'description': 'Gerir as categorias e subcategorias para classificar custos em faturas'},
    ],
}


_MODULE_DEFAULTS = {
    'producao':  'Produção',
    'pastelaria': 'Pastelaria',
    'vendas':    'Vendas',
    'gestor':    'Gestor',
    'financeiro': 'Financeiro',
}

_MODULE_EMOJIS = {
    'producao':  '🍦',
    'pastelaria': '🥐',
    'vendas':    '🛍️',
    'gestor':    '⚙️',
    'financeiro': '💰',
}


@gestor_bp.route('/gestao-tiles', methods=['GET'])
@perm_required('acesso_gestor')
def gestao_tiles():
    from db.tiles import get_all_tile_config, get_module_labels

    db_state = {(r['module'], r['tile_id']): r for r in get_all_tile_config()}
    module_custom_labels = get_module_labels()

    _ORDER = ['producao', 'pastelaria', 'vendas', 'gestor', 'financeiro']
    modules = {}
    for module_id in _ORDER:
        tile_defs = _TILE_MASTER.get(module_id, [])
        merged = []
        for t in tile_defs:
            db_row = db_state.get((module_id, t['id']), {})
            db_label = db_row.get('label', '')
            merged.append({
                'tile_id': t['id'],
                'icon': t['icon'],
                'label': db_label if db_label else t['default_label'],
                'default_label': t['default_label'],
                'description': t['description'],
                'visible': db_row.get('visible', True),
            })
        if merged:
            modules[module_id] = merged

    return render_template(
        'gestor/gestao_tiles.html',
        active_tab='gestao_tiles',
        modules=modules,
        module_custom_labels=module_custom_labels,
        module_defaults=_MODULE_DEFAULTS,
        module_emojis=_MODULE_EMOJIS,
        back_url=url_for('gestor.index'),
    )


@gestor_bp.route('/gestao-tiles/toggle', methods=['POST'])
@perm_required('acesso_gestor')
def gestao_tiles_toggle():
    from db.tiles import set_tile_visibility
    from flask import jsonify

    module = request.form.get('module', '').strip()
    tile_id = request.form.get('tile_id', '').strip()
    visible_str = request.form.get('visible', '')

    if not module or not tile_id or visible_str not in ('0', '1'):
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400

    visible = visible_str == '1'
    set_tile_visibility(module, tile_id, visible)
    return jsonify({'ok': True, 'module': module, 'tile_id': tile_id, 'visible': visible})


@gestor_bp.route('/gestao-tiles/rename', methods=['POST'])
@perm_required('acesso_gestor')
def gestao_tiles_rename():
    from db.tiles import set_tile_label
    from flask import jsonify

    module = request.form.get('module', '').strip()
    tile_id = request.form.get('tile_id', '').strip()
    label = request.form.get('label', '').strip()

    if not module or not tile_id or not label:
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400

    set_tile_label(module, tile_id, label)
    return jsonify({'ok': True, 'module': module, 'tile_id': tile_id, 'label': label})
