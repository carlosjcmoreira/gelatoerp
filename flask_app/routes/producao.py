import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
from flask_app.analytics import queue_analytics_event
import sys, os

logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_producao_total_by_period, get_producao_daily_totals,
    get_producao_sabor_overview, get_sabores_list, get_quebras_df,
    add_quebra, delete_quebra, delete_quebras_bulk,
    get_plano_do_dia, update_producao_real,
    get_ordem_producao, mover_sabor_ordem, adicionar_sabor_ordem, remover_sabor_ordem,
    get_plano_producao, upsert_plano_producao, get_latest_bolhao_pesagem,
    marcar_sabor_no_plano, copiar_plano_dia_anterior,
    get_plano_producao_historico, get_pesagens_recentes,
    get_producao_manual_daily, get_producao_balanca_por_sabor,
    get_all_receitas_gelado, update_receita_gelado, update_receita_gelado_ativo,
    add_receita_gelado, reorder_ordem_producao,
    upsert_stock_producao, get_stock_producao_all, get_stock_producao,
    reduzir_stock_producao, add_transferencia, get_latest_pesagem_por_sabor,
    criar_ordem_transferencia, add_stock_producao,
    get_plano_ajuste_dia,
    get_eventos_adjudicados_para_producao, mark_production_alert_sent,
    get_event_production_requirements,
    get_latest_pesagem_por_sabor_all_lojas, get_pesagens_loja_range, set_stock_producao,
    get_stock_producao_by_loja, upsert_pesagem_matosinhos_inicio,
    get_active_venda_stores, get_or_create_pending_batch,
    update_stock_gelado, get_producao_history_by_day,
    delete_producao_record, get_pesagens_loja_3dias,
    add_stock_gelado,
    get_movimentos_stock_gelado,
    get_gelato_stock_rotation,
    add_producao,
)
from datetime import date, timedelta
import pandas as pd
import json
import plotly
import plotly.graph_objects as go

import flask_app.services.producao as producao_svc

producao_bp = Blueprint('producao', __name__)

def _get_sabores_plano():
    return get_sabores_list()
SABOR_ICONS = {
    "Coco": "🥥", "Stracciatella": "🍨", "Baunilha": "🌿",
    "Caramelo": "🍯", "Doce de leite": "🥛", "Ganache": "🍫",
    "Amendoim": "🥜", "Cremino": "🍮", "Pistacchio": "🫘",
    "Manga": "🥭", "Maracujá": "💛", "Framboesa": "🍓",
    "Açaí": "🫐", "Extra noir": "🖤", "Iogurte": "🥛",
    "Pistacchio V.": "🌱", "Ricota, Noz e Mel": "🍯",
    "Cheesecake": "🍰", "Café": "☕", "Bonet": "🍮",
    "Chocolate Branco": "🤍",
}

TABS = [
    {'id': 'pesagens_loja', 'label': 'Pesagens de Loja', 'icon': '⚖️', 'url_endpoint': 'producao.pesagens_loja'},
    {'id': 'registo_producao', 'label': 'Registo de Produção', 'icon': '📸', 'url_endpoint': 'producao.registo_producao'},
    {'id': 'transferir', 'label': 'Transferir para Loja', 'icon': '🔄', 'url_endpoint': 'producao.transferir'},
    {'id': 'ordem', 'label': 'Ordem de Produção', 'icon': '🔢', 'url_endpoint': 'producao.ordem'},
    {'id': 'por_sabor', 'label': 'Stock Gelado', 'icon': '🍨', 'url_endpoint': 'producao.por_sabor'},
    {'id': 'quebra', 'label': 'Registar Quebra de Produção', 'icon': '⚠️', 'url_endpoint': 'producao.registar_quebra'},
    {'id': 'dashboard', 'label': 'Dashboard Produção', 'icon': '📊', 'url_endpoint': 'producao.dashboard'},
    {'id': 'sabores_receitas', 'label': 'Sabores e Receitas', 'icon': '🍦', 'url_endpoint': 'producao.sabores_receitas'},
    {'id': 'movimentos_stock', 'label': 'Movimentos de Stock', 'icon': '📦', 'url_endpoint': 'producao.movimentos_stock'},
    {'id': 'rotacao_stock', 'label': 'Rotação de Stock', 'icon': '📈', 'url_endpoint': 'producao.rotacao_stock'},
]

def _tabs_with_urls():
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons
    visibility = get_tile_visibility('producao')
    labels = get_tile_labels('producao')
    icons = get_tile_icons('producao')
    return [
        {'id': t['id'], 'label': labels.get(t['id']) or t['label'], 'icon': icons.get(t['id']) or t['icon'], 'url': url_for(t['url_endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]


def _parse_decimal(s):
    try:
        return float(str(s).replace(',', '.').strip())
    except (ValueError, AttributeError):
        return 0.0


def _format_date(val):
    if pd.isna(val) or val is None or val == '':
        return '-'
    try:
        return pd.to_datetime(val).strftime('%d/%m')
    except:
        return str(val)


def _iso_date(val):
    if val is None or val == '':
        return ''
    try:
        if pd.isna(val):
            return ''
    except Exception:
        pass
    try:
        return pd.to_datetime(val).strftime('%Y-%m-%d')
    except:
        return ''


@producao_bp.route('/')
@perm_required('acesso_producao')
def index():
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons, get_module_labels
    visibility = get_tile_visibility('producao')
    labels = get_tile_labels('producao')
    icons = get_tile_icons('producao')
    custom_mod = get_module_labels().get('producao')
    items = [
        {'icon': icons.get(t['id']) or t['icon'], 'label': labels.get(t['id']) or t['label'], 'url': url_for(t['url_endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'🍦 {custom_mod}' if custom_mod else '🍦 Produção Gelado')


@producao_bp.route('/dashboard')
@perm_required('acesso_producao')
def dashboard():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    prod_semana = get_producao_total_by_period("Matosinhos", week_start, today, para_eurokg=False)
    prod_mes = get_producao_total_by_period("Matosinhos", month_start, today, para_eurokg=False)

    data_inicio_str = request.args.get('data_inicio', str(today - timedelta(days=30)))
    data_fim_str = request.args.get('data_fim', str(today))
    try:
        data_inicio = date.fromisoformat(data_inicio_str)
        data_fim = date.fromisoformat(data_fim_str)
    except ValueError:
        data_inicio = today - timedelta(days=30)
        data_fim = today

    balanca_daily = get_producao_daily_totals(None, data_inicio, data_fim, para_eurokg=False)
    manual_daily = get_producao_manual_daily(data_inicio, data_fim)
    balanca_sabor = get_producao_balanca_por_sabor(data_inicio, data_fim)

    chart_json = None
    has_balanca = not balanca_daily.empty
    has_manual = len(manual_daily) > 0
    if has_balanca or has_manual:
        fig = go.Figure()
        if has_balanca:
            balanca_daily['data_str'] = pd.to_datetime(balanca_daily['data']).dt.strftime('%d/%m')
            fig.add_trace(go.Scatter(
                x=balanca_daily['data_str'], y=balanca_daily['total_kg'],
                mode='lines+markers', name='Sistema Balança',
                line=dict(color='#27AE60', width=3), marker=dict(size=8),
                fill='tozeroy', fillcolor='rgba(39, 174, 96, 0.15)'
            ))
        if has_manual:
            manual_dates = [r['data'].strftime('%d/%m') for r in manual_daily]
            manual_vals = [r['total_kg'] for r in manual_daily]
            fig.add_trace(go.Scatter(
                x=manual_dates, y=manual_vals,
                mode='lines+markers', name='Registo Manual',
                line=dict(color='#2980B9', width=3, dash='dot'), marker=dict(size=8),
                fill='tozeroy', fillcolor='rgba(41, 128, 185, 0.10)'
            ))
        fig.update_layout(
            xaxis_title="Data", yaxis_title="Produção (kg)",
            hovermode='x unified', legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            margin=dict(l=20, r=20, t=40, b=20),
            xaxis=dict(type='category', tickangle=-45)
        )
        fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.2)')
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.2)')
        chart_json = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

    _dias_pt = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']
    dias_7_raw = [today - timedelta(days=i) for i in range(6, -1, -1)]
    dias_7 = [{'date': d, 'label': _dias_pt[d.weekday()], 'fmt': d.strftime('%d/%m'), 'is_today': d == today}
              for d in dias_7_raw]
    d_inicio = dias_7[0]['date']
    hist_rows = get_plano_producao_historico(d_inicio, today)
    plano_pivot = {}
    for r in hist_rows:
        s = r['sabor']
        if s not in plano_pivot:
            plano_pivot[s] = {}
        plano_pivot[s][r['data']] = {'est': r['est'], 'real': r['real'], 'mode': 'plan'}
    ordem = get_ordem_producao()
    sabores_plano = [s['sabor'] for s in ordem if s['sabor'] in plano_pivot]
    sabores_plano += sorted(s for s in plano_pivot if s not in sabores_plano)

    pesagens = get_pesagens_recentes(3)

    return render_template('producao/dashboard.html',
                           active_tab='dashboard', tabs=_tabs_with_urls(),
                           prod_semana=prod_semana, prod_mes=prod_mes,
                           chart_json=chart_json,
                           data_inicio=data_inicio_str, data_fim=data_fim_str,
                           balanca_sabor=balanca_sabor,
                           dias_7=dias_7, plano_pivot=plano_pivot,
                           sabores_plano=sabores_plano,
                           pesagens=pesagens)


@producao_bp.route('/stock-gelado/ajustar', methods=['POST'])
@perm_required('acesso_producao')
def stock_gelado_ajustar():
    changes = 0
    for key, val in request.form.items():
        for prefix, loja in (('prod_mat_', 'Matosinhos'), ('prod_bol_', 'Bolhão'),
                              ('prod_mou_', 'Mouzinho'), ('prod_b2b_', 'B2B')):
            if key.startswith(prefix):
                sabor = key[len(prefix):]
                qty = _parse_decimal(val)
                set_stock_producao(sabor, loja, qty)
                changes += 1
                break
    if changes:
        flash(f"Stock de produção atualizado ({changes} entrada(s)).", "success")
    else:
        flash("Nenhuma alteração detetada.", "info")
    return redirect(url_for('producao.por_sabor'))


@producao_bp.route('/por-sabor', methods=['GET'])
@perm_required('acesso_producao')
def por_sabor():
    today = date.today()

    overview_df = get_producao_sabor_overview()
    stock_prod = get_stock_producao_all(today)

    stock_prod_map = {}
    for sp in stock_prod:
        key = sp['sabor']
        if key not in stock_prod_map:
            stock_prod_map[key] = {}
        stock_prod_map[key][sp['loja']] = sp['quantidade_kg']

    all_sabores = set(stock_prod_map.keys())
    if not overview_df.empty:
        all_sabores.update(overview_df['sabor'].tolist())

    rows = []
    for sabor in sorted(all_sabores):
        sp = stock_prod_map.get(sabor, {})
        prod_mat = sp.get('Matosinhos', 0)
        prod_bol = sp.get('Bolhão', 0)
        prod_mou = sp.get('Mouzinho', 0)
        prod_b2b = sp.get('B2B', 0)

        loja_mat = 0
        loja_bol = 0
        data_mat = None
        data_bol = None
        if not overview_df.empty and sabor in overview_df['sabor'].values:
            r = overview_df[overview_df['sabor'] == sabor].iloc[0]
            if pd.notna(r.get('stock_matosinhos')) and r['stock_matosinhos'] is not None:
                loja_mat = float(r['stock_matosinhos'])
            if pd.notna(r.get('data_stock_matosinhos')) and r['data_stock_matosinhos'] is not None:
                data_mat = r['data_stock_matosinhos']
            if pd.notna(r.get('stock_bolhao')) and r['stock_bolhao'] is not None:
                loja_bol = float(r['stock_bolhao'])
            if pd.notna(r.get('data_stock_bolhao')) and r['data_stock_bolhao'] is not None:
                data_bol = r['data_stock_bolhao']

        total = prod_mat + prod_bol + prod_mou + prod_b2b + loja_mat + loja_bol

        rows.append({
            'sabor': sabor,
            'prod_mat': prod_mat,
            'prod_bol': prod_bol,
            'prod_mou': prod_mou,
            'prod_b2b': prod_b2b,
            'loja_mat': loja_mat,
            'loja_mat_data': _format_date(data_mat) if data_mat else '',
            'loja_bol': loja_bol,
            'loja_bol_data': _format_date(data_bol) if data_bol else '',
            'total': total,
        })

    rows.sort(key=lambda x: x['total'], reverse=True)

    totals = {
        'prod_mat': sum(r['prod_mat'] for r in rows),
        'prod_bol': sum(r['prod_bol'] for r in rows),
        'prod_mou': sum(r['prod_mou'] for r in rows),
        'prod_b2b': sum(r['prod_b2b'] for r in rows),
        'loja_mat': sum(r['loja_mat'] for r in rows),
        'loja_bol': sum(r['loja_bol'] for r in rows),
        'total': sum(r['total'] for r in rows),
    }

    return render_template('producao/por_sabor.html',
                           active_tab='por_sabor', tabs=_tabs_with_urls(),
                           rows=rows, totals=totals)


@producao_bp.route('/registar-quebra', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def registar_quebra():
    if request.method == 'POST':
        data_quebra = date.fromisoformat(request.form['data'])
        quantidade = _parse_decimal(request.form.get('quantidade', '0'))
        sabor = request.form.get('sabor', '')
        lote = request.form.get('lote', '')
        motivo = request.form.get('motivo', '')
        if quantidade > 0 and sabor and sabor != "Sem sabores disponíveis":
            add_quebra(data_quebra, "Matosinhos", quantidade, motivo, sabor, lote if lote else None)
            lote_text = f" (Lote: {lote})" if lote else ""
            flash(f"Quebra de {quantidade}kg de {sabor}{lote_text} registada com sucesso!", "success")
        else:
            flash("Por favor, preencha os campos obrigatórios: Data, Quantidade e Sabor.", "error")
        return redirect(url_for('producao.registar_quebra'))

    sabores = get_sabores_list()
    historico_inicio = date.today() - timedelta(days=90)
    quebras_df = get_quebras_df("Matosinhos", data_inicio=historico_inicio)
    quebras = []
    if not quebras_df.empty:
        cols = ['id', 'data', 'sabor', 'lote', 'quantidade_kg', 'motivo']
        cols = [c for c in cols if c in quebras_df.columns]
        for _, r in quebras_df.iterrows():
            row = {}
            for c in cols:
                val = r.get(c)
                if c == 'data' and val is not None:
                    try:
                        row[c] = pd.to_datetime(val).strftime('%d/%m/%Y')
                    except:
                        row[c] = str(val)
                else:
                    row[c] = val
            quebras.append(row)

    return render_template('producao/registar_quebra.html',
                           active_tab='quebra', tabs=_tabs_with_urls(),
                           sabores=sabores, quebras=quebras, today=str(date.today()))


@producao_bp.route('/eliminar-quebra', methods=['POST'])
@perm_required('acesso_producao')
def eliminar_quebra():
    id_to_delete = int(request.form['id'])
    delete_quebra(id_to_delete)
    flash("Registo eliminado!", "success")
    return redirect(url_for('producao.registar_quebra'))


@producao_bp.route('/eliminar-quebras-bulk', methods=['POST'])
@perm_required('acesso_producao')
def eliminar_quebras_bulk():
    ids_raw = request.form.getlist('ids')
    ids = [int(i) for i in ids_raw if i.isdigit()]
    deleted = delete_quebras_bulk(ids)
    flash(f"{deleted} registo(s) eliminado(s)!", "success")
    return redirect(url_for('producao.registar_quebra'))


_PESAGENS_LOJA_ORDER = ['Bolhão', 'Matosinhos', 'Mouzinho']


_PESAGENS_DAYS_OPTIONS = [1, 3, 7, 14, 30]


@producao_bp.route('/pesagens-loja', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def pesagens_loja():
    try:
        days = int(request.args.get('days', 1))
    except (ValueError, TypeError):
        days = 1
    if days not in _PESAGENS_DAYS_OPTIONS:
        days = 1

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'edit':
            try:
                from datetime import date as _date, datetime as _datetime
                stock_id = int(request.form.get('stock_id', 0))
                kg = float(request.form.get('kg', 0))
                loja = request.form.get('loja', '').strip() or None
                data_str = request.form.get('data', '').strip()
                nova_data = None
                if data_str:
                    nova_data = _datetime.strptime(data_str, '%Y-%m-%d').date()
                    if nova_data > _date.today():
                        flash("A data não pode ser no futuro.", "danger")
                        return redirect(url_for('producao.pesagens_loja', days=days))
                if stock_id and kg >= 0:
                    update_stock_gelado(stock_id, kg, loja, nova_data=nova_data)
                    flash("Pesagem atualizada com sucesso.", "success")
                else:
                    flash("Dados inválidos.", "danger")
            except (ValueError, TypeError):
                flash("Erro ao processar os dados.", "danger")
        elif action == 'edit_3d':
            try:
                from datetime import date as _date, datetime as _datetime
                updated = 0
                created = 0
                for i in range(3):
                    sid_str = request.form.get(f'stock_id_{i}', '').strip()
                    kg_str = request.form.get(f'kg_{i}', '').strip()
                    if not kg_str:
                        continue
                    kg = float(kg_str.replace(',', '.'))
                    if kg < 0:
                        continue
                    if sid_str and int(sid_str) > 0:
                        update_stock_gelado(int(sid_str), kg, loja=None, nova_data=None)
                        updated += 1
                    else:
                        sabor_i = request.form.get(f'sabor_{i}', '').strip()
                        loja_i = request.form.get(f'loja_{i}', '').strip()
                        date_iso_i = request.form.get(f'date_iso_{i}', '').strip()
                        if sabor_i and loja_i and date_iso_i:
                            record_date = _datetime.strptime(date_iso_i, '%Y-%m-%d').date()
                            if record_date > _date.today():
                                flash(f"Data inválida para nova pesagem (slot {i+1}).", "danger")
                                continue
                            add_stock_gelado(record_date, loja_i, sabor_i, kg, 'fim')
                            created += 1
                parts = []
                if updated:
                    parts.append(f"{updated} pesagem(ns) atualizada(s)")
                if created:
                    parts.append(f"{created} pesagem(ns) criada(s)")
                if parts:
                    flash(', '.join(parts) + ' com sucesso.', 'success')
                else:
                    flash("Nenhuma pesagem para processar.", "info")
            except (ValueError, TypeError):
                flash("Erro ao processar os dados.", "danger")
        return redirect(url_for('producao.pesagens_loja', days=days))

    if days == 1:
        pesagens_by_loja = get_latest_pesagem_por_sabor_all_lojas()
        known = set(pesagens_by_loja.keys())
        lojas = [l for l in _PESAGENS_LOJA_ORDER if l in known or l in ('Bolhão', 'Matosinhos')] + [
            l for l in sorted(known) if l not in _PESAGENS_LOJA_ORDER
        ]
        all_sabores = set()
        for loja_data in pesagens_by_loja.values():
            all_sabores.update(loja_data.keys())
        all_sabores = sorted(all_sabores)

        rows = []
        for sabor in all_sabores:
            row = {'sabor': sabor, 'lojas': {}}
            for loja in lojas:
                entry = pesagens_by_loja.get(loja, {}).get(sabor)
                if entry:
                    row['lojas'][loja] = {
                        'id': entry['id'],
                        'kg': entry['kg'],
                        'data': _format_date(entry['data']) if entry['data'] else '-',
                        'data_iso': _iso_date(entry['data']),
                    }
                else:
                    row['lojas'][loja] = None
            rows.append(row)

        return render_template('producao/pesagens_loja.html',
                               active_tab='pesagens_loja', tabs=_tabs_with_urls(),
                               lojas=lojas, rows=rows,
                               days=days, days_options=_PESAGENS_DAYS_OPTIONS,
                               history_rows=None)
    elif days == 3:
        user = session.get('user', {})
        loja_id = user.get('loja_id')
        user_loja = 'Bolhão'
        if loja_id:
            from db.auth import get_store_by_id
            store = get_store_by_id(loja_id)
            if store and store.get('name'):
                user_loja = store['name']
        selected_loja = request.args.get('loja', '').strip() or user_loja
        if selected_loja not in _PESAGENS_LOJA_ORDER:
            selected_loja = user_loja
        three_day = get_pesagens_loja_3dias(selected_loja)
        return render_template('producao/pesagens_loja.html',
                               active_tab='pesagens_loja', tabs=_tabs_with_urls(),
                               lojas=[], rows=[],
                               days=days, days_options=_PESAGENS_DAYS_OPTIONS,
                               history_rows=None,
                               three_day=three_day, user_loja=selected_loja,
                               all_lojas=_PESAGENS_LOJA_ORDER)
    else:
        raw = get_pesagens_loja_range(days)
        history_rows = [
            {
                'id': r['id'],
                'loja': r['loja'],
                'sabor': r['sabor'],
                'kg': r['kg'],
                'data': _format_date(r['data']) if r['data'] else '-',
                'data_iso': _iso_date(r['data']),
            }
            for r in raw
        ]
        # Group by date for the expandable day view
        from itertools import groupby as _groupby
        history_grouped = []
        for date_iso, grp in _groupby(history_rows, key=lambda r: r['data_iso']):
            grp_list = list(grp)
            history_grouped.append({
                'date_iso': date_iso,
                'date_fmt': grp_list[0]['data'],
                'total_kg': round(sum(r['kg'] for r in grp_list), 3),
                'rows': grp_list,
            })
        return render_template('producao/pesagens_loja.html',
                               active_tab='pesagens_loja', tabs=_tabs_with_urls(),
                               lojas=[], rows=[],
                               days=days, days_options=_PESAGENS_DAYS_OPTIONS,
                               history_rows=history_rows,
                               history_grouped=history_grouped)


@producao_bp.route('/registo-producao', methods=['GET'])
@perm_required('acesso_producao')
def registo_producao():
    historico_dias = get_producao_history_by_day(30)
    return render_template('producao/registo_producao.html',
                           active_tab='registo_producao', tabs=_tabs_with_urls(),
                           today=str(date.today()),
                           historico_dias=historico_dias)


@producao_bp.route('/registo-producao/eliminar', methods=['POST'])
@perm_required('acesso_producao')
def registo_producao_eliminar():
    try:
        record_id = int(request.form['id'])
    except (KeyError, ValueError):
        flash("ID inválido.", "danger")
        return redirect(url_for('producao.registo_producao'))
    deleted = delete_producao_record(record_id)
    if deleted:
        flash("Registo eliminado com sucesso.", "success")
    else:
        flash("Registo não encontrado.", "warning")
    return redirect(url_for('producao.registo_producao'))


@producao_bp.route('/registo-producao/ocr', methods=['POST'])
@perm_required('acesso_producao')
def registo_producao_ocr():
    from flask_app.ocr_producao import extract_producao_sheet
    uploaded = request.files.get('foto')
    if not uploaded or uploaded.filename == '':
        flash("Por favor, selecione uma imagem.", "warning")
        return redirect(url_for('producao.registo_producao'))

    image_bytes = uploaded.read()
    filename = uploaded.filename or 'upload.jpg'

    result = extract_producao_sheet(image_bytes, filename)

    user_date = request.form.get('data_producao', '').strip()
    data_ocr = result.get('date') or user_date or str(date.today())
    if user_date:
        data_ocr = user_date

    if result.get('error') and not result.get('sabores'):
        flash(f"OCR não conseguiu ler a imagem: {result['error']} — preencha os valores manualmente.", "warning")

    session['ocr_producao_data'] = {
        'date': data_ocr,
        'sabores': result.get('sabores', {}),
        'confidence': result.get('ocr_confidence', 0.0),
        'error': result.get('error'),
        'manual_entry': False,
    }

    return redirect(url_for('producao.registo_producao_confirmar'))


@producao_bp.route('/registo-producao/manual', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def registo_producao_manual():
    if request.method == 'POST':
        data_str = request.form.get('data_producao', str(date.today())).strip()
    else:
        data_str = request.args.get('data', str(date.today())).strip()
    try:
        date.fromisoformat(data_str)
    except ValueError:
        data_str = str(date.today())
    session['ocr_producao_data'] = {
        'date': data_str,
        'sabores': {},
        'confidence': 0.0,
        'error': None,
        'manual_entry': True,
    }
    return redirect(url_for('producao.registo_producao_confirmar'))


@producao_bp.route('/registo-producao/confirmar', methods=['GET'])
@perm_required('acesso_producao')
def registo_producao_confirmar():
    ocr_data = session.get('ocr_producao_data')
    if not ocr_data:
        flash("Nenhum dado de produção para confirmar. Carregue uma imagem primeiro.", "warning")
        return redirect(url_for('producao.registo_producao'))

    sabores_all = get_sabores_list()
    ocr_sabores = ocr_data.get('sabores', {})
    ocr_date = ocr_data.get('date', str(date.today()))
    confidence = ocr_data.get('confidence', 0.0)
    ocr_error = ocr_data.get('error')
    manual_entry = ocr_data.get('manual_entry', False)
    no_ocr_values = not bool(ocr_sabores)

    rows = []
    for sabor in sabores_all:
        vals = ocr_sabores.get(sabor, {})
        rows.append({
            'sabor': sabor,
            'pesagem_mat': vals.get('pesagem_mat', 0.0),
            'prod_bolhao': vals.get('prod_bolhao', 0.0),
            'prod_matosinhos': vals.get('prod_matosinhos', 0.0),
            'prod_mouzinho': vals.get('prod_mouzinho', 0.0),
            'prod_b2b': vals.get('prod_b2b', 0.0),
            'ocr_confidence': vals.get('confidence') if ocr_sabores.get(sabor) else None,
        })

    for sabor in ocr_sabores:
        if sabor not in sabores_all:
            vals = ocr_sabores[sabor]
            rows.append({
                'sabor': sabor,
                'pesagem_mat': vals.get('pesagem_mat', 0.0),
                'prod_bolhao': vals.get('prod_bolhao', 0.0),
                'prod_matosinhos': vals.get('prod_matosinhos', 0.0),
                'prod_mouzinho': vals.get('prod_mouzinho', 0.0),
                'prod_b2b': vals.get('prod_b2b', 0.0),
                'ocr_confidence': vals.get('confidence'),
            })

    return render_template('producao/registo_producao_confirmar.html',
                           active_tab='registo_producao', tabs=_tabs_with_urls(),
                           rows=rows, ocr_date=ocr_date,
                           confidence=confidence, ocr_error=ocr_error,
                           manual_entry=manual_entry,
                           no_ocr_values=no_ocr_values,
                           today=str(date.today()))


@producao_bp.route('/registo-producao/guardar', methods=['POST'])
@perm_required('acesso_producao')
def registo_producao_guardar():
    username = session.get('user', {}).get('username', 'system')
    data_str = request.form.get('data', str(date.today()))
    try:
        data_prod = date.fromisoformat(data_str)
    except ValueError:
        data_prod = date.today()

    sabores_form = {}
    for key, val in request.form.items():
        for prefix, field in (
            ('pesagem_mat_', 'pesagem_mat'),
            ('prod_bolhao_', 'prod_bolhao'),
            ('prod_matosinhos_', 'prod_matosinhos'),
            ('prod_mouzinho_', 'prod_mouzinho'),
            ('prod_b2b_', 'prod_b2b'),
        ):
            if key.startswith(prefix):
                sabor = key[len(prefix):]
                sabores_form.setdefault(sabor, {})[field] = _parse_decimal(val)
                if field == 'pesagem_mat':
                    sabores_form[sabor]['pesagem_mat_explicit'] = val.strip() != ''
                break

    ocr_session = session.get('ocr_producao_data', {})
    manual_entry = ocr_session.get('manual_entry', True)
    tipo_registo = 'manual' if manual_entry else 'ocr'

    saved = 0
    sabores_para_ordens = {}
    for sabor, vals in sabores_form.items():
        pesagem_mat = vals.get('pesagem_mat', 0.0)
        prod_bol = vals.get('prod_bolhao', 0.0)
        prod_mat = vals.get('prod_matosinhos', 0.0)
        prod_mou = vals.get('prod_mouzinho', 0.0)
        prod_b2b = vals.get('prod_b2b', 0.0)
        pesagem_mat_explicit = vals.get('pesagem_mat_explicit', False)

        if all(v == 0 for v in [pesagem_mat, prod_bol, prod_mat, prod_mou, prod_b2b]):
            if not pesagem_mat_explicit:
                continue

        upsert_plano_producao(
            data_prod, sabor,
            pesagem_matosinhos=pesagem_mat,
            producao_estimada_bolhao=prod_bol,
            producao_estimada_matosinhos=prod_mat,
            producao_estimada_outros=prod_b2b,
            producao_estimada_mouzinho=prod_mou,
        )

        if pesagem_mat_explicit:
            upsert_pesagem_matosinhos_inicio(data_prod, sabor, pesagem_mat)

        for loja, qty in (('Bolhão', prod_bol), ('Matosinhos', prod_mat),
                          ('Mouzinho', prod_mou), ('B2B', prod_b2b)):
            if qty > 0:
                add_stock_producao(data_prod, sabor, loja, qty)
                add_producao(data_prod, loja, qty, tipo=tipo_registo, sabor=sabor)

        for loja, qty in (('Bolhão', prod_bol), ('Mouzinho', prod_mou)):
            if qty > 0:
                sabores_para_ordens.setdefault(sabor, {})[loja] = qty

        saved += 1

    session.pop('ocr_producao_data', None)

    if not saved:
        flash("Nenhuma alteração guardada.", "info")
        return redirect(url_for('producao.registo_producao'))

    flash(f"Produção registada: {saved} sabor(es).", "success")

    if sabores_para_ordens:
        session['producao_ordens_pendentes'] = {
            'data': str(data_prod),
            'sabores': {s: {loja: qty for loja, qty in lojas.items()}
                        for s, lojas in sabores_para_ordens.items()},
            'saved': saved,
        }
        return redirect(url_for('producao.registo_producao_ordens'))

    return redirect(url_for('producao.por_sabor'))


@producao_bp.route('/registo-producao/ordens', methods=['GET'])
@perm_required('acesso_producao')
def registo_producao_ordens():
    pendentes = session.get('producao_ordens_pendentes')
    if not pendentes:
        flash("Nenhuma produção recente para criar ordens.", "warning")
        return redirect(url_for('producao.por_sabor'))

    sabores_ordens = pendentes.get('sabores', {})
    data_prod = pendentes.get('data', str(date.today()))
    saved = pendentes.get('saved', 0)

    lojas_com_producao = sorted({
        loja
        for lojas in sabores_ordens.values()
        for loja in lojas
        if lojas[loja] > 0
    })

    return render_template(
        'producao/registo_producao_ordens.html',
        active_tab='registo_producao', tabs=_tabs_with_urls(),
        sabores_ordens=sabores_ordens,
        lojas_com_producao=lojas_com_producao,
        data_prod=data_prod,
        saved=saved,
    )


@producao_bp.route('/registo-producao/criar-ordens', methods=['POST'])
@perm_required('acesso_producao')
def registo_producao_criar_ordens():
    import psycopg2
    username = session.get('user', {}).get('username', 'system')
    data_str = request.form.get('data', str(date.today()))
    try:
        data_prod = date.fromisoformat(data_str)
    except ValueError:
        data_prod = date.today()

    ordens = 0
    avisos = []
    try:
        for key, val in request.form.items():
            if not key.startswith('ordem_'):
                continue
            parts = key[len('ordem_'):].split('_', 1)
            if len(parts) != 2:
                continue
            loja, sabor = parts
            qty = _parse_decimal(val)
            if qty <= 0:
                continue
            if loja not in ('Bolhão', 'Mouzinho'):
                continue
            disponivel = get_stock_producao(data_prod, sabor, loja)
            if disponivel <= 0:
                avisos.append(f"{sabor} ({loja}): sem stock disponível, ordem ignorada.")
                continue
            if qty > disponivel:
                avisos.append(
                    f"{sabor} ({loja}): pedido {qty:.3f} kg mas disponível {disponivel:.3f} kg — ordem criada para o disponível."
                )
                qty = disponivel
            reduzir_stock_producao(data_prod, sabor, loja, qty)
            criar_ordem_transferencia(
                data_prod, 'Gelado', sabor, qty, 'kg', loja,
                sabor=sabor, criado_por=username, data_prevista=data_prod,
            )
            ordens += 1
    except psycopg2.DatabaseError:
        raise
    except ValueError as exc:
        logger.warning("Erro de validação ao criar ordens: %s", exc)
        flash(f"Dados inválidos: {exc}. Verifique os valores e tente novamente.", "warning")
        return redirect(url_for('producao.por_sabor'))
    except Exception as exc:
        logger.error("Erro inesperado ao criar ordens de transferência: %s", exc, exc_info=True)
        flash("Não foi possível criar as ordens — erro inesperado. Tente novamente.", "danger")
        return redirect(url_for('producao.por_sabor'))

    session.pop('producao_ordens_pendentes', None)

    for aviso in avisos:
        flash(aviso, "warning")
    if ordens:
        flash(f"{ordens} ordem(ns) de transferência criada(s).", "success")
    elif not avisos:
        flash("Nenhuma ordem criada.", "info")

    return redirect(url_for('producao.por_sabor'))


@producao_bp.route('/criar-plano')
@perm_required('acesso_producao')
def criar_plano():
    sabores_data = []
    sabores_plano = _get_sabores_plano()
    for i, s in enumerate(sabores_plano):
        sabores_data.append({
            'nome': s,
            'icon': SABOR_ICONS.get(s, '🍦'),
            'idx': i,
        })
    return render_template('producao/criar_plano.html',
                           active_tab='criar_plano', tabs=_tabs_with_urls(),
                           sabores=sabores_data)


@producao_bp.route('/criar-plano/<sabor>', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def criar_plano_sabor(sabor):
    today = date.today()

    if request.method == 'POST':
        action = request.form.get('action', '')
        if action in ('save', 'add_to_plan'):
            pesagem_mat = _parse_decimal(request.form.get('pesagem_matosinhos', '0'))
            est_bolhao = _parse_decimal(request.form.get('estimada_bolhao', '0'))
            est_matosinhos = _parse_decimal(request.form.get('estimada_matosinhos', '0'))
            est_outros = _parse_decimal(request.form.get('estimada_outros', '0'))
            est_mouzinho = _parse_decimal(request.form.get('estimada_mouzinho', '0'))
            add_to_plan = action == 'add_to_plan'
            producao_svc.guardar_plano_sabor(
                today, sabor, pesagem_mat, est_bolhao, est_matosinhos,
                est_outros, est_mouzinho, add_to_plan=add_to_plan,
            )
            if add_to_plan:
                flash(f"{sabor} adicionado ao plano!", "success")
                return redirect(url_for('producao.criar_plano'))
            flash(f"Plano para {sabor} guardado!", "success")
            return redirect(url_for('producao.criar_plano_sabor', sabor=sabor))

    kg_bolhao, data_bolhao = get_latest_bolhao_pesagem(sabor)
    plano = get_plano_producao(today, sabor)

    data_bolhao_fmt = None
    bolhao_days_ago = None
    if data_bolhao:
        data_bolhao_fmt = data_bolhao.strftime('%d/%m/%Y')
        bolhao_days_ago = (today - data_bolhao).days

    icon = SABOR_ICONS.get(sabor, '🍦')

    return render_template('producao/criar_plano_sabor.html',
                           active_tab='criar_plano', tabs=_tabs_with_urls(),
                           sabor=sabor, icon=icon,
                           kg_bolhao=kg_bolhao, data_bolhao_fmt=data_bolhao_fmt,
                           bolhao_days_ago=bolhao_days_ago,
                           plano=plano, today=str(today))


@producao_bp.route('/executar-plano', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def executar_plano():
    if request.method == 'POST':
        data_str = request.form.get('data', str(date.today()))
    else:
        data_str = request.args.get('data', str(date.today()))
    try:
        data_plano = date.fromisoformat(data_str)
    except ValueError:
        data_plano = date.today()

    if request.method == 'POST':
        # Build {sabor: {real_b, real_m, real_mou, real_outros}} from form and delegate to service
        sabores_reais = {}
        for key, val in request.form.items():
            for prefix, field in (('real_b_', 'real_b'), ('real_m_', 'real_m'),
                                  ('real_mou_', 'real_mou'), ('real_outros_', 'real_outros')):
                if key.startswith(prefix):
                    sabor = key[len(prefix):]
                    sabores_reais.setdefault(sabor, {})[field] = _parse_decimal(val)
                    break

        result = producao_svc.executar_plano_dia(data_plano, sabores_reais)
        registos = result['registos']

        if registos > 0:
            flash(f"{registos} sabor(es) registado(s).", "success")
        else:
            flash("Nenhuma alteração.", "info")
        return redirect(url_for('producao.executar_plano', data=str(date.today())))

    entradas = get_plano_do_dia(data_plano)
    for e in entradas:
        e['icon'] = SABOR_ICONS.get(e['sabor'], '🍦')
        has_real = (e['real_bolhao'] is not None and e['real_bolhao'] > 0) or \
                   (e['real_matosinhos'] is not None and e['real_matosinhos'] > 0) or \
                   (e['real_mouzinho'] is not None and e['real_mouzinho'] > 0) or \
                   (e['real_outros'] is not None and e['real_outros'] > 0)
        e['has_real'] = has_real

    n_total = len(entradas)
    n_done = sum(1 for e in entradas if e['has_real'])

    eventos_do_dia = get_eventos_adjudicados_para_producao(data_plano)
    for ev in eventos_do_dia:
        if not ev.get('production_alert_sent'):
            mark_production_alert_sent(ev['id'])

    return render_template('producao/executar_plano.html',
                           active_tab='executar_plano', tabs=_tabs_with_urls(),
                           entradas=entradas, data_plano=str(data_plano),
                           today=str(date.today()),
                           n_total=n_total, n_done=n_done,
                           eventos_do_dia=eventos_do_dia,
                           necessidades_eventos=get_event_production_requirements(data_plano))


@producao_bp.route('/copiar-plano', methods=['POST'])
@perm_required('acesso_producao')
def copiar_plano():
    today = date.today()
    count = copiar_plano_dia_anterior(today)
    if count > 0:
        flash(f"Plano copiado: {count} sabor(es) adicionado(s) para hoje.", "success")
    else:
        flash("Não foi encontrado nenhum plano anterior para copiar.", "warning")
    return redirect(url_for('producao.executar_plano'))


@producao_bp.route('/ajustar-plano', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def ajustar_plano():
    today = date.today()
    data_str = request.args.get('data', str(today))
    try:
        data_plano = date.fromisoformat(data_str)
    except ValueError:
        data_plano = today

    if request.method == 'POST':
        data_str_form = request.form.get('data', str(today))
        try:
            data_plano = date.fromisoformat(data_str_form)
        except ValueError:
            data_plano = today
        entradas = get_plano_ajuste_dia(data_plano)
        ajustes = {}
        for e in entradas:
            sabor = e['sabor']
            pesagem_mat = _parse_decimal(request.form.get(f'pesagem_{sabor}', str(e['pesagem_matosinhos'])))
            ajustes[sabor] = {
                'pesagem_mat': pesagem_mat,
                'est_bol': _parse_decimal(request.form.get(f'est_bol_{sabor}', str(e['estimado_bolhao']))),
                'est_mat': _parse_decimal(request.form.get(f'est_mat_{sabor}', str(e['estimado_matosinhos']))),
                'est_outros': _parse_decimal(request.form.get(f'est_outros_{sabor}', str(e['estimado_outros']))),
                'est_mou': _parse_decimal(request.form.get(f'est_mou_{sabor}', str(e['estimado_mouzinho']))),
            }
        saved = producao_svc.ajustar_plano_dia(data_plano, ajustes)
        if saved:
            flash(f"Ajustes guardados para {saved} sabor(es).", "success")
        return redirect(url_for('producao.ajustar_plano', data=str(data_plano)))

    entradas = get_plano_ajuste_dia(data_plano)
    for e in entradas:
        e['icon'] = SABOR_ICONS.get(e['sabor'], '🍦')

    return render_template('producao/ajustar_plano.html',
                           active_tab='ajustar_plano', tabs=_tabs_with_urls(),
                           entradas=entradas, data_plano=str(data_plano),
                           today=str(today))


@producao_bp.route('/transferir', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def transferir():
    today = date.today()

    if request.method == 'POST':
        import psycopg2
        ordens_count = 0
        username = session.get('user', {}).get('username', '')
        data_prevista_str = request.form.get('data_prevista', '')
        data_prevista = None
        if data_prevista_str:
            try:
                from datetime import datetime
                data_prevista = datetime.strptime(data_prevista_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        destino_tipo = request.form.get('destino_tipo', 'loja').strip().lower()
        loja_destino = request.form.get('loja_destino', 'Bolhão').strip()
        destino_nome = request.form.get('destino_nome', '').strip()
        active_store_names = {s['name'] for s in get_active_venda_stores()}
        if destino_tipo == 'b2b':
            if not destino_nome:
                flash("Indique a entidade destinatária para a transferência B2B.", "warning")
                return redirect(url_for('producao.transferir'))
            if len(destino_nome) > 255:
                flash("A entidade destinatária não pode ter mais de 255 caracteres.", "warning")
                return redirect(url_for('producao.transferir'))
            loja_destino = 'B2B'
        elif destino_tipo == 'loja':
            destino_nome = None
            if loja_destino not in active_store_names:
                flash("Loja de destino inválida.", "warning")
                return redirect(url_for('producao.transferir'))
        else:
            flash("Tipo de destino inválido.", "warning")
            return redirect(url_for('producao.transferir'))
        try:
            batch_id = get_or_create_pending_batch(
                today, 'Gelado', loja_destino, destino_tipo, destino_nome
            )
            stock_loja = loja_destino
            if destino_tipo == 'b2b':
                stock_loja = 'Bolhão'
            import re as _re
            form_pairs = []
            for key in request.form:
                m = _re.match(r'^produto_(\d+)$', key)
                if m:
                    n = int(m.group(1))
                    form_pairs.append((n, request.form[key], request.form.get(f'qty_{n}', '')))
            for _, sabor, qty_str in sorted(form_pairs, key=lambda x: x[0]):
                if not sabor:
                    continue
                qty = _parse_decimal(qty_str)
                if qty <= 0:
                    continue

                stock_disponivel = get_stock_producao(today, sabor, stock_loja)
                other_loja = 'Matosinhos' if stock_loja == 'Bolhão' else 'Bolhão'

                if qty > stock_disponivel:
                    deficit = qty - stock_disponivel
                    stock_other = get_stock_producao(today, sabor, other_loja)
                    transferir_de_other = min(deficit, stock_other)
                    if transferir_de_other > 0:
                        reduzir_stock_producao(today, sabor, other_loja, transferir_de_other)
                        add_stock_producao(today, sabor, stock_loja, transferir_de_other)
                        stock_disponivel += transferir_de_other

                if qty > stock_disponivel:
                    qty = stock_disponivel
                if qty > 0:
                    reduced = reduzir_stock_producao(today, sabor, stock_loja, qty)
                    if reduced:
                        if destino_tipo == 'loja':
                            add_transferencia(today, sabor, loja_destino, qty)
                        criar_ordem_transferencia(
                            today, 'Gelado', sabor, qty, 'kg', loja_destino,
                            sabor=sabor, criado_por=username,
                            data_prevista=data_prevista, batch_id=batch_id,
                            destino_tipo=destino_tipo, destino_nome=destino_nome,
                            loja_origem=stock_loja if destino_tipo == 'b2b' else None,
                        )
                        ordens_count += 1
        except psycopg2.DatabaseError:
            raise
        except ValueError as exc:
            logger.warning("Erro de validação na transferência: %s", exc)
            flash(f"Dados inválidos: {exc}. Verifique os valores e tente novamente.", "warning")
            return redirect(url_for('producao.transferir'))
        except Exception as exc:
            logger.error("Erro inesperado na transferência: %s", exc, exc_info=True)
            flash("Não foi possível registar a transferência — erro inesperado. Tente novamente.", "danger")
            return redirect(url_for('producao.transferir'))
        if ordens_count > 0:
            queue_analytics_event(
                'production_transfer_created',
                destination_type=destino_tipo,
                order_count=ordens_count,
            )
            flash(f"{ordens_count} ordem(ns) de transferência criada(s)!", "success")
        else:
            flash("Nenhuma transferência registada. Verifique as quantidades.", "info")
        return redirect(url_for('producao.transferir'))

    lojas_venda = get_active_venda_stores()
    stock_prod = get_stock_producao_all(today)
    pesagem_bolhao = get_latest_pesagem_por_sabor('Bolhão')
    pesagem_matosinhos = get_latest_pesagem_por_sabor('Matosinhos')

    sabores_set = set()
    for sp in stock_prod:
        sabores_set.add(sp['sabor'])

    cards = []
    for sabor in sorted(sabores_set):
        icon = SABOR_ICONS.get(sabor, '🍦')
        stock_b = next((sp['quantidade_kg'] for sp in stock_prod if sp['sabor'] == sabor and sp['loja'] == 'Bolhão'), 0)
        stock_m = next((sp['quantidade_kg'] for sp in stock_prod if sp['sabor'] == sabor and sp['loja'] == 'Matosinhos'), 0)
        balcao_b = pesagem_bolhao.get(sabor, {}).get('kg', 0)
        balcao_m = pesagem_matosinhos.get(sabor, {}).get('kg', 0)
        data_b = pesagem_bolhao.get(sabor, {}).get('data')
        data_m = pesagem_matosinhos.get(sabor, {}).get('data')
        cards.append({
            'sabor': sabor,
            'icon': icon,
            'stock_prod_bolhao': stock_b,
            'stock_prod_matosinhos': stock_m,
            'balcao_bolhao': balcao_b,
            'balcao_matosinhos': balcao_m,
            'data_pesagem_bolhao': data_b.strftime('%d/%m') if data_b else '-',
            'data_pesagem_matosinhos': data_m.strftime('%d/%m') if data_m else '-',
        })

    return render_template('producao/transferir.html',
                           active_tab='transferir', tabs=_tabs_with_urls(),
                           cards=cards, lojas_venda=lojas_venda, today=str(today))


@producao_bp.route('/ordem')
@perm_required('acesso_producao')
def ordem():
    ordem_list = get_ordem_producao()
    for item in ordem_list:
        item['icon'] = SABOR_ICONS.get(item['sabor'], '🍦')

    todos_sabores = get_sabores_list()
    sabores_na_ordem = {item['sabor'] for item in ordem_list}
    sabores_disponiveis = [s for s in todos_sabores if s not in sabores_na_ordem]

    return render_template('producao/ordem.html',
                           active_tab='ordem', tabs=_tabs_with_urls(),
                           ordem=ordem_list, sabores_disponiveis=sabores_disponiveis)


@producao_bp.route('/ordem/reorder', methods=['POST'])
@perm_required('acesso_producao')
def ordem_reorder():
    data = request.get_json()
    if data and 'order' in data:
        reorder_ordem_producao(data['order'])
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 400


@producao_bp.route('/ordem/mover', methods=['POST'])
@perm_required('acesso_producao')
def ordem_mover():
    sabor_id = int(request.form['sabor_id'])
    direcao = request.form['direcao']
    mover_sabor_ordem(sabor_id, direcao)
    return redirect(url_for('producao.ordem'))


@producao_bp.route('/ordem/adicionar', methods=['POST'])
@perm_required('acesso_producao')
def ordem_adicionar():
    sabor = request.form['sabor']
    after_id = request.form.get('after_id')
    if after_id:
        adicionar_sabor_ordem(sabor, int(after_id))
    else:
        adicionar_sabor_ordem(sabor)
    return redirect(url_for('producao.ordem'))


@producao_bp.route('/ordem/remover', methods=['POST'])
@perm_required('acesso_producao')
def ordem_remover():
    sabor_id = int(request.form['sabor_id'])
    remover_sabor_ordem(sabor_id)
    return redirect(url_for('producao.ordem'))


@producao_bp.route('/sabores-receitas', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def sabores_receitas():
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'add_receita':
            nome = request.form.get('nova_receita', '').strip()
            corrente = request.form.get('novo_corrente', '').strip()
            if nome and corrente:
                success = add_receita_gelado(nome, corrente)
                if success:
                    flash('Sabor adicionado!', 'success')
                else:
                    flash('Já existe um sabor com esse nome de sistema.', 'warning')
            else:
                flash('Preencha os dois campos para adicionar um sabor.', 'warning')
        else:
            changes = 0
            receitas_list = get_all_receitas_gelado()
            for r in receitas_list:
                new_nome_corrente = request.form.get(f'nome_corrente_{r["id"]}', '').strip() or None
                new_ativo = request.form.get(f'ativo_{r["id"]}') == 'on'
                nome_corrente_changed = new_nome_corrente != (r['nome_corrente'] or None)
                ativo_changed = new_ativo != r['ativo']
                if nome_corrente_changed:
                    update_receita_gelado(r['id'], r['nome'], new_nome_corrente)
                if ativo_changed:
                    update_receita_gelado_ativo(r['id'], new_ativo)
                if nome_corrente_changed or ativo_changed:
                    changes += 1
            if changes > 0:
                flash(f"{changes} sabor(es) atualizado(s)!", "success")
            else:
                flash("Nenhuma alteração detetada.", "info")

        return redirect(url_for('producao.sabores_receitas'))

    receitas_list = get_all_receitas_gelado()
    receitas_list.sort(key=lambda r: (not r['ativo'], (r['nome_corrente'] or r['nome']).lower()))
    return render_template('producao/sabores_receitas.html',
                           active_tab='sabores_receitas', tabs=_tabs_with_urls(),
                           receitas=receitas_list)


@producao_bp.route('/sabores-receitas/save-row', methods=['POST'])
@perm_required('acesso_producao')
def sabores_receitas_save_row():
    """AJAX endpoint — save a single recipe row.

    Expects JSON: {id: int, nome_corrente: str|null, ativo: bool}
    Returns JSON: {ok: true} or {ok: false, error: "..."}
    """
    from flask import jsonify
    try:
        data = request.get_json(force=True, silent=True) or {}
        row_id = int(data.get('id', 0))
        if not row_id:
            return jsonify({'ok': False, 'error': 'id inválido'}), 400

        nome_corrente = (data.get('nome_corrente') or '').strip() or None
        ativo = bool(data.get('ativo', False))

        update_receita_gelado_ativo(row_id, ativo)
        # Fetch current nome so we don't overwrite it with None accidentally
        receitas = get_all_receitas_gelado()
        current = next((r for r in receitas if r['id'] == row_id), None)
        if current is None:
            return jsonify({'ok': False, 'error': 'sabor não encontrado'}), 404
        update_receita_gelado(row_id, current['nome'], nome_corrente)

        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


# ── Legacy redirects — keep old URLs working ─────────────────────────────────

@producao_bp.route('/receitas', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def receitas():
    return redirect(url_for('producao.sabores_receitas'), 301)


@producao_bp.route('/sabores-ativos', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def sabores_ativos():
    return redirect(url_for('producao.sabores_receitas'), 301)


@producao_bp.route('/movimentos-stock')
@perm_required('acesso_producao')
def movimentos_stock():
    today = date.today()
    data_fim_str = request.args.get('data_fim', str(today))
    data_inicio_str = request.args.get('data_inicio', str(today - timedelta(days=7)))
    sabor_filtro = request.args.get('sabor', '').strip() or None
    loja_param = request.args.get('loja', '').strip()

    try:
        data_fim = date.fromisoformat(data_fim_str)
    except ValueError:
        data_fim = today
    try:
        data_inicio = date.fromisoformat(data_inicio_str)
    except ValueError:
        data_inicio = today - timedelta(days=7)

    active_stores = get_active_venda_stores()
    loja_names = [s['name'] for s in active_stores]
    if loja_param not in loja_names:
        loja_param = loja_names[0] if loja_names else 'Bolhão'

    all_sabores = get_sabores_list()

    try:
        ledger = get_movimentos_stock_gelado(loja_param, data_inicio, data_fim, sabor_filtro)
    except Exception:
        logger.exception('Erro ao obter movimentos de stock para loja=%s', loja_param)
        ledger = {}

    ledger_items = sorted(ledger.items(), key=lambda x: x[0])

    return render_template(
        'producao/movimentos_stock.html',
        active_tab='movimentos_stock',
        tabs=_tabs_with_urls(),
        loja=loja_param,
        lojas=loja_names,
        data_inicio=data_inicio_str,
        data_fim=data_fim_str,
        sabor_filtro=sabor_filtro or '',
        all_sabores=all_sabores,
        ledger_items=ledger_items,
    )


_ROTATION_ISSUE_LABELS = {
    'insufficient_snapshots': 'Pesagens comparáveis insuficientes',
    'no_intervals_in_period': 'Sem intervalos completos neste período',
    'negative_stock_residual': 'Movimentos incoerentes: o consumo calculado seria negativo',
    'manual_only_production': 'Existe produção de controlo sem produção física importada',
    'duplicate_snapshot': 'Existem pesagens duplicadas',
    'unassigned_transfer_origin': 'Origem de transferência não identificada',
    'unknown_transfer_destination': 'Destino de transferência não identificado',
    'unknown_transfer_status': 'Estado de transferência não reconhecido',
    'missing_transfer_confirmation_date': 'Transferência confirmada sem data de confirmação',
    'ambiguous_transfer_overlap': 'Sobreposição ambígua entre fontes de transferência',
    'invalid_snapshot_quantity': 'Pesagem com quantidade inválida',
    'invalid_production_quantity': 'Produção com quantidade inválida',
    'invalid_transfer_quantity': 'Transferência com quantidade inválida',
    'invalid_receipt_quantity': 'Receção com quantidade inválida',
    'invalid_breakage_quantity': 'Quebra com quantidade inválida',
    'unresolved_snapshot_identity': 'Loja ou sabor de uma pesagem não identificado',
    'unresolved_production_identity': 'Loja ou sabor de uma produção não identificado',
    'unresolved_transfer_identity': 'Loja ou sabor de uma transferência não identificado',
    'unresolved_receipt_identity': 'Loja ou sabor de uma receção não identificado',
    'unresolved_breakage_identity': 'Loja ou sabor de uma quebra não identificado',
    'invalid_snapshot_order': 'Ordem temporal das pesagens inválida',
}

_ROTATION_FLAG_LABELS = {
    'long_interval': 'Intervalo superior a 3 dias',
    'inferred_transfer_origin': 'Origem da transferência inferida',
    'legacy_transfer_source': 'Movimento recuperado da fonte histórica',
    'receipt_transfer_source': 'Saída recuperada do registo de receções',
}

_ROTATION_UNRESOLVED_LABELS = {
    'ambiguous_transfer_overlap': 'Sobreposições ambíguas entre fontes de transferência',
    'breakage_flavor': 'Quebras com sabor não identificado',
    'breakage_quantity': 'Quebras com quantidade inválida',
    'breakage_store': 'Quebras com loja não identificada',
    'duplicate_snapshot': 'Pesagens duplicadas',
    'pending_transfer_ignored': 'Ordens pendentes ignoradas (não são movimento físico)',
    'production_flavor': 'Produções com sabor não identificado',
    'production_quantity': 'Produções com quantidade inválida',
    'production_store': 'Produções com loja não identificada',
    'production_type': 'Produções com tipo não reconhecido',
    'receipt_flavor': 'Receções com sabor não identificado',
    'receipt_quantity': 'Receções com quantidade inválida',
    'receipt_store': 'Receções com loja não identificada',
    'stock_flavor': 'Pesagens com sabor não identificado',
    'stock_quantity': 'Pesagens com quantidade inválida',
    'stock_store': 'Pesagens com loja não identificada',
    'transfer_confirmation_date': 'Transferências confirmadas sem data de confirmação',
    'transfer_destination': 'Transferências com destino não identificado',
    'transfer_flavor': 'Transferências com sabor não identificado',
    'transfer_origin': 'Transferências com origem não identificada',
    'transfer_quantity': 'Transferências com quantidade inválida',
    'transfer_status': 'Transferências com estado não reconhecido',
}

_ROTATION_ANOMALY_ISSUES = (
    set(_ROTATION_ISSUE_LABELS)
    - {'insufficient_snapshots', 'no_intervals_in_period'}
)


def _rotation_period(args, today):
    default_start = today - timedelta(days=29)
    try:
        start = date.fromisoformat(args.get('data_inicio', ''))
        end = date.fromisoformat(args.get('data_fim', ''))
    except (TypeError, ValueError):
        return default_start, today, False
    if start > end or end > today or (end - start).days > 366:
        return default_start, today, False
    return start, end, True


def _rotation_store_summaries(rotation):
    summaries = {}
    for store in rotation.get('stores', []):
        cells = [
            row.get('stores', {}).get(store['name'])
            for row in rotation.get('rows', [])
        ]
        cells = [cell for cell in cells if cell]
        summaries[store['name']] = {
            'has_comparable_weighings': any(
                cell.get('first_observation') is not None
                or cell.get('last_observation') is not None
                or cell.get('valid_intervals', 0) > 0
                or cell.get('excluded_intervals', 0) > 0
                for cell in cells
            ),
            'anomaly_count': sum(
                bool(set(cell.get('issues', ())) & _ROTATION_ANOMALY_ISSUES)
                for cell in cells
            ),
        }
    return summaries


def _attach_rotation_intervals(rotation):
    by_cell = {}
    for interval in rotation.get('intervals', []):
        by_cell.setdefault(
            (interval.get('store'), interval.get('sabor')),
            [],
        ).append(interval)
    for row in rotation.get('rows', []):
        for store_name, cell in row.get('stores', {}).items():
            cell['intervals'] = by_cell.get((store_name, row.get('sabor')), [])


@producao_bp.route('/rotacao-stock')
@perm_required('acesso_producao')
def rotacao_stock():
    today = date.today()
    data_inicio, data_fim, valid_period = _rotation_period(request.args, today)
    if request.args and not valid_period:
        flash('Período inválido. A mostrar os últimos 30 dias.', 'warning')

    load_error = False
    try:
        rotation = get_gelato_stock_rotation(data_inicio, data_fim)
    except Exception:
        logger.exception(
            'Erro ao calcular rotação de stock entre %s e %s',
            data_inicio,
            data_fim,
        )
        load_error = True
        rotation = {
            'stores': [],
            'rows': [],
            'intervals': [],
            'coverage': {
                'cells_total': 0,
                'cells_with_value': 0,
                'by_store': {},
            },
            'unresolved': {},
        }

    excluded_intervals = sum(
        1 for interval in rotation.get('intervals', [])
        if not interval.get('usable')
    )
    _attach_rotation_intervals(rotation)
    store_summaries = _rotation_store_summaries(rotation)
    return render_template(
        'producao/rotacao_stock.html',
        active_tab='rotacao_stock',
        tabs=_tabs_with_urls(),
        rotation=rotation,
        data_inicio=data_inicio.isoformat(),
        data_fim=data_fim.isoformat(),
        excluded_intervals=excluded_intervals,
        issue_labels=_ROTATION_ISSUE_LABELS,
        flag_labels=_ROTATION_FLAG_LABELS,
        load_error=load_error,
        quick_periods=[
            ('7 dias', (today - timedelta(days=6)).isoformat()),
            ('30 dias', (today - timedelta(days=29)).isoformat()),
            ('90 dias', (today - timedelta(days=89)).isoformat()),
        ],
        today_iso=today.isoformat(),
        store_summaries=store_summaries,
        anomaly_issues=_ROTATION_ANOMALY_ISSUES,
        unresolved_labels=_ROTATION_UNRESOLVED_LABELS,
        unresolved_total=sum(rotation.get('unresolved', {}).values()),
    )
