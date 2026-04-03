from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
import sys, os
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
    {'id': 'criar_plano', 'label': 'Planear Produção', 'icon': '📋', 'url_endpoint': 'producao.criar_plano'},
    {'id': 'executar_plano', 'label': 'Produzir', 'icon': '▶️', 'url_endpoint': 'producao.executar_plano'},
    {'id': 'ajustar_plano', 'label': 'Ajustar Plano', 'icon': '✏️', 'url_endpoint': 'producao.ajustar_plano'},
    {'id': 'transferir', 'label': 'Transferir para Loja', 'icon': '🔄', 'url_endpoint': 'producao.transferir'},
    {'id': 'ordem', 'label': 'Ordem de Produção', 'icon': '🔢', 'url_endpoint': 'producao.ordem'},
    {'id': 'por_sabor', 'label': 'Stock Gelado', 'icon': '🍨', 'url_endpoint': 'producao.por_sabor'},
    {'id': 'quebra', 'label': 'Registar Quebra de Produção', 'icon': '⚠️', 'url_endpoint': 'producao.registar_quebra'},
    {'id': 'dashboard', 'label': 'Dashboard Produção', 'icon': '📊', 'url_endpoint': 'producao.dashboard'},
    {'id': 'receitas', 'label': 'Receitas de Gelado', 'icon': '📖', 'url_endpoint': 'producao.receitas'},
    {'id': 'sabores_ativos', 'label': 'Lista de Sabores', 'icon': '✅', 'url_endpoint': 'producao.sabores_ativos'},
]

def _tabs_with_urls():
    return [{'id': t['id'], 'label': t['label'], 'icon': t['icon'], 'url': url_for(t['url_endpoint'])} for t in TABS]


def _parse_decimal(s):
    try:
        return float(str(s).replace(',', '.').strip())
    except (ValueError, AttributeError):
        return 0.0


_PESAGEM_GRAMAS_THRESHOLD = 50.0


def _normalise_pesagem_kg(value: float) -> tuple[float, bool]:
    """Return (kg_value, converted).
    If value >= threshold it is almost certainly grams — divide by 1000.
    Legitimate daily balcão quantities stay well below 20 kg per flavour.
    """
    if value >= _PESAGEM_GRAMAS_THRESHOLD:
        return round(value / 1000.0, 6), True
    return value, False


def _format_date(val):
    if pd.isna(val) or val is None or val == '':
        return '-'
    try:
        return pd.to_datetime(val).strftime('%d/%m')
    except:
        return str(val)


@producao_bp.route('/')
@perm_required('acesso_producao')
def index():
    items = [{'icon': t['icon'], 'label': t['label'], 'url': url_for(t['url_endpoint'])} for t in TABS]
    return render_template('components/section_menu.html', items=items)


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


@producao_bp.route('/por-sabor')
@perm_required('acesso_producao')
def por_sabor():
    today = date.today()
    overview_df = get_producao_sabor_overview()
    stock_prod = get_stock_producao_all(today)

    stock_prod_map = {}
    for sp in stock_prod:
        key = sp['sabor']
        if key not in stock_prod_map:
            stock_prod_map[key] = {'Matosinhos': 0, 'Bolhão': 0}
        stock_prod_map[key][sp['loja']] = sp['quantidade_kg']

    all_sabores = set(stock_prod_map.keys())
    if not overview_df.empty:
        all_sabores.update(overview_df['sabor'].tolist())

    rows = []
    for sabor in sorted(all_sabores):
        prod_mat = stock_prod_map.get(sabor, {}).get('Matosinhos', 0)
        prod_bol = stock_prod_map.get(sabor, {}).get('Bolhão', 0)

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

        total = prod_mat + prod_bol + loja_mat + loja_bol

        rows.append({
            'sabor': sabor,
            'prod_mat': prod_mat,
            'prod_bol': prod_bol,
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
    quebras_df = get_quebras_df("Matosinhos")
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
            pesagem_mat_raw = _parse_decimal(request.form.get('pesagem_matosinhos', '0'))
            pesagem_mat, converted = _normalise_pesagem_kg(pesagem_mat_raw)
            if converted:
                flash(
                    f'Pesagem Matosinhos convertida de {pesagem_mat_raw:g} g → {pesagem_mat:.3f} kg.',
                    'info',
                )
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
        quebras_count = result['quebras']

        msgs = []
        if registos > 0:
            msgs.append(f"{registos} sabor(es) registado(s)")
        if quebras_count > 0:
            msgs.append(f"{quebras_count} quebra(s) de produção criada(s)")
        if msgs:
            flash(". ".join(msgs) + ".", "success")
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
                           eventos_do_dia=eventos_do_dia)


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
        converted_sabores = []
        for e in entradas:
            sabor = e['sabor']
            pesagem_mat_raw = _parse_decimal(request.form.get(f'pesagem_{sabor}', str(e['pesagem_matosinhos'])))
            pesagem_mat_norm, was_converted = _normalise_pesagem_kg(pesagem_mat_raw)
            if was_converted:
                converted_sabores.append(f'{sabor}: {pesagem_mat_raw:g} g → {pesagem_mat_norm:.3f} kg')
            ajustes[sabor] = {
                'pesagem_mat': pesagem_mat_norm,
                'est_bol': _parse_decimal(request.form.get(f'est_bol_{sabor}', str(e['estimado_bolhao']))),
                'est_mat': _parse_decimal(request.form.get(f'est_mat_{sabor}', str(e['estimado_matosinhos']))),
                'est_outros': _parse_decimal(request.form.get(f'est_outros_{sabor}', str(e['estimado_outros']))),
                'est_mou': _parse_decimal(request.form.get(f'est_mou_{sabor}', str(e['estimado_mouzinho']))),
            }
        saved = producao_svc.ajustar_plano_dia(data_plano, ajustes)
        if converted_sabores:
            flash(
                'Pesagens convertidas de gramas para kg: ' + '; '.join(converted_sabores),
                'info',
            )
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
        ordens_count = 0
        idx = 0
        username = session.get('user', {}).get('username', '')
        data_prevista_str = request.form.get('data_prevista', '')
        data_prevista = None
        if data_prevista_str:
            try:
                from datetime import datetime
                data_prevista = datetime.strptime(data_prevista_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        while True:
            sabor = request.form.get(f'sabor_{idx}')
            loja = 'Bolhão'
            qty_str = request.form.get(f'qty_{idx}', '')
            if sabor is None:
                break
            idx += 1
            qty = _parse_decimal(qty_str)
            if qty <= 0:
                continue

            stock_disponivel = get_stock_producao(today, sabor, loja)

            if loja == 'Bolhão' and qty > stock_disponivel:
                deficit = qty - stock_disponivel
                stock_mat = get_stock_producao(today, sabor, 'Matosinhos')
                transferir_de_mat = min(deficit, stock_mat)
                if transferir_de_mat > 0:
                    reduzir_stock_producao(today, sabor, 'Matosinhos', transferir_de_mat)
                    add_stock_producao(today, sabor, 'Bolhão', transferir_de_mat)
                    stock_disponivel += transferir_de_mat

            if qty > stock_disponivel:
                qty = stock_disponivel
            if qty > 0:
                reduced = reduzir_stock_producao(today, sabor, loja, qty)
                if reduced:
                    add_transferencia(today, sabor, loja, qty)
                    criar_ordem_transferencia(today, 'Gelado', sabor, qty, 'kg', loja, sabor=sabor, criado_por=username, data_prevista=data_prevista)
                    ordens_count += 1
        if ordens_count > 0:
            flash(f"{ordens_count} ordem(ns) de transferência criada(s)!", "success")
        else:
            flash("Nenhuma transferência registada. Verifique as quantidades.", "info")
        return redirect(url_for('producao.transferir'))

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
                           cards=cards, today=str(today))


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


@producao_bp.route('/receitas', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def receitas():
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'add_receita':
            nome = request.form.get('nova_receita', '').strip()
            corrente = request.form.get('novo_corrente', '').strip()
            if nome and corrente:
                success = add_receita_gelado(nome, corrente)
                if success:
                    flash('Receita adicionada!', 'success')
                else:
                    flash('Receita já existe.', 'warning')
            else:
                flash('Preencha ambos os campos.', 'warning')
        else:
            changes = 0
            receitas_list = get_all_receitas_gelado()
            for r in receitas_list:
                new_val = request.form.get(f'nome_corrente_{r["id"]}', '')
                if new_val != (r['nome_corrente'] or ''):
                    update_receita_gelado(r['id'], r['nome'], new_val)
                    changes += 1
            if changes > 0:
                flash(f"{changes} receita(s) atualizada(s)!", "success")
            else:
                flash("Nenhuma alteração detetada.", "info")

        return redirect(url_for('producao.receitas'))

    receitas_list = get_all_receitas_gelado()
    return render_template('producao/receitas.html',
                           active_tab='receitas', tabs=_tabs_with_urls(),
                           receitas=receitas_list)


@producao_bp.route('/sabores-ativos', methods=['GET', 'POST'])
@perm_required('acesso_producao')
def sabores_ativos():
    if request.method == 'POST':
        changes = 0
        receitas_list = get_all_receitas_gelado()
        for r in receitas_list:
            if not r['nome_corrente'] or not str(r['nome_corrente']).strip():
                continue
            new_ativo = request.form.get(f'ativo_{r["id"]}') == 'on'
            if new_ativo != r['ativo']:
                update_receita_gelado_ativo(r['id'], new_ativo)
                changes += 1
        if changes > 0:
            flash(f"{changes} sabor(es) atualizado(s)!", "success")
        else:
            flash("Nenhuma alteração detetada.", "info")
        return redirect(url_for('producao.sabores_ativos'))

    receitas_list = get_all_receitas_gelado()
    receitas_filtered = [r for r in receitas_list if r['nome_corrente'] and str(r['nome_corrente']).strip()]
    receitas_filtered.sort(key=lambda r: (not r['ativo'], r['nome_corrente'].lower()))
    return render_template('producao/sabores_ativos.html',
                           active_tab='sabores_ativos', tabs=_tabs_with_urls(),
                           receitas=receitas_filtered)
