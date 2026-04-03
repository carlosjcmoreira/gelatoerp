from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from flask_app.auth import perm_required
import sys, os
import json
from datetime import date, timedelta
import pandas as pd
import plotly.graph_objects as go
import plotly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    calculate_kpi_annual, calculate_kpi_by_day, get_target_by_month,
    get_gramas_gelado, add_gramas_gelado, get_consumo_gelado_mensal,
    get_vendas_produto_mensal,
)

eurokg_bp = Blueprint('eurokg', __name__)


MENU_ITEMS = [
    {'icon': '📊', 'label': 'Dashboard Euro/kg', 'url_endpoint': 'eurokg.dashboard'},
    {'icon': '📅', 'label': 'Resumo Mensal', 'url_endpoint': 'eurokg.resumo_mensal'},
    {'icon': '🧮', 'label': 'Consumo Teórico', 'url_endpoint': 'eurokg.consumo_teorico'},
    {'icon': '💶', 'label': 'Vendas por Produto', 'url_endpoint': 'eurokg.vendas_produto'},
]

MESES_PT_ABREV = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                  'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']


def _build_tabs(loja_filter, is_gestor, active):
    tabs = [
        {'id': 'dashboard', 'label': 'Dashboard', 'icon': '📊', 'url': url_for('eurokg.dashboard', loja=loja_filter)},
        {'id': 'resumo', 'label': 'Resumo Mensal', 'icon': '📅', 'url': url_for('eurokg.resumo_mensal', loja=loja_filter)},
        {'id': 'vendas', 'label': 'Vendas por Produto', 'icon': '💶', 'url': url_for('eurokg.vendas_produto', loja=loja_filter)},
    ]
    if is_gestor:
        tabs.append({'id': 'consumo', 'label': 'Consumo Teórico', 'icon': '🍦', 'url': url_for('eurokg.consumo_teorico', loja=loja_filter)})
    return tabs


@eurokg_bp.route('/')
@perm_required('acesso_eurokg')
def index():
    items = [{'icon': m['icon'], 'label': m['label'], 'url': url_for(m['url_endpoint'])} for m in MENU_ITEMS]
    return render_template('components/section_menu.html', items=items,
                           menu_title='📊 Euro/kg')


@eurokg_bp.route('/dashboard')
@perm_required('acesso_eurokg')
def dashboard():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter = request.args.get('loja', 'Global Porto' if is_gestor else None)
    if loja_filter == 'Global Porto':
        loja_db = None
    elif loja_filter:
        loja_db = loja_filter
    else:
        loja_db = None

    today = date.today()
    year_start = date(today.year, 1, 1)
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())

    annual_data = calculate_kpi_annual(today.year, loja_db)

    total_vendas_ytd = 0
    total_consumo_ytd = 0
    for m in range(1, today.month + 1):
        kpi_m = annual_data[m]
        total_vendas_ytd += kpi_m['vendas']
        total_consumo_ytd += kpi_m['consumo']
    kpi_ytd = total_vendas_ytd / total_consumo_ytd if total_consumo_ytd > 0 else 0

    kpi_month_data = annual_data[today.month]
    kpi_month = kpi_month_data['kpi']

    kpi_week_df = calculate_kpi_by_day(loja_db, week_start, today)
    if not kpi_week_df.empty:
        vendas_week = kpi_week_df['vendas'].sum()
        consumo_week = kpi_week_df['consumo_kg'].sum()
        kpi_week = vendas_week / consumo_week if consumo_week > 0 else 0
    else:
        kpi_week = 0

    target_atual = get_target_by_month(today.month)

    meses_pt = {
        1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril',
        5: 'Maio', 6: 'Junho', 7: 'Julho', 8: 'Agosto',
        9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'
    }
    month_name = meses_pt[today.month]

    data_inicio_30 = today - timedelta(days=30)
    kpi_df = calculate_kpi_by_day(loja_db, data_inicio_30, today)

    chart_json = None
    daily_details = []
    if not kpi_df.empty:
        kpi_df['data_str'] = pd.to_datetime(kpi_df['data']).dt.strftime('%d/%m')

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=kpi_df['data_str'].tolist(), y=kpi_df['kpi'].tolist(),
            mode='lines+markers', name='KPI Real',
            line=dict(color='#667eea', width=3), marker=dict(size=8),
            hovertemplate='%{x}<br>KPI: %{y:.2f} €/kg<extra></extra>'
        ))
        fig.add_trace(go.Scatter(
            x=kpi_df['data_str'].tolist(), y=kpi_df['target'].tolist(),
            mode='lines', name='Target',
            line=dict(color='#E74C3C', width=2, dash='dash'),
            hovertemplate='%{x}<br>Target: %{y:.2f} €/kg<extra></extra>'
        ))
        fig.update_layout(
            xaxis_title="Data", yaxis_title="€/kg", hovermode='x unified',
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=20, r=20, t=40, b=20),
            xaxis=dict(type='category', tickangle=-45)
        )
        fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.2)')
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.2)')
        chart_json = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

        if loja_db == 'Bolhão':
            detail_entrada_label = 'Transferências (kg)'
        elif loja_db == 'Matosinhos':
            detail_entrada_label = 'Prod - Transf (kg)'
        else:
            detail_entrada_label = 'Produção (kg)'

        for _, row in kpi_df.iterrows():
            d = row['data']
            if hasattr(d, 'strftime'):
                d_str = d.strftime('%d/%m/%Y')
            else:
                d_str = pd.to_datetime(d).strftime('%d/%m/%Y')
            daily_details.append({
                'data': d_str,
                'vendas': round(row['vendas'], 2),
                'stock_ini': round(row['stock_ini_kg'], 3),
                'entrada': round(row['entrada_kg'], 3),
                'stock_final': round(row['stock_final_kg'], 3),
                'quebras': round(row['quebras_kg'], 3),
                'consumo': round(row['consumo_kg'], 3),
                'kpi': round(row['kpi'], 2),
                'target': round(row['target'], 2)
            })
    else:
        detail_entrada_label = 'Produção (kg)'

    tabs = _build_tabs(loja_filter, is_gestor, 'dashboard')

    return render_template('eurokg/dashboard.html',
        active_tab='dashboard', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        kpi_ytd=kpi_ytd, kpi_month=kpi_month, kpi_week=kpi_week,
        target_atual=target_atual, month_name=month_name, year=today.year,
        chart_json=chart_json, daily_details=daily_details,
        detail_entrada_label=detail_entrada_label,
        delta_ytd=kpi_ytd - target_atual,
        delta_month=kpi_month - target_atual,
        delta_week=kpi_week - target_atual)


@eurokg_bp.route('/resumo')
@perm_required('acesso_eurokg')
def resumo_mensal():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter = request.args.get('loja', 'Global Porto' if is_gestor else None)
    if loja_filter == 'Global Porto':
        loja_db = None
    elif loja_filter:
        loja_db = loja_filter
    else:
        loja_db = None

    current_year = date.today().year
    meses_nomes = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                   'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

    if loja_db == 'Bolhão':
        entrada_label = 'Transferências (kg)'
    elif loja_db == 'Matosinhos':
        entrada_label = 'Prod - Transf (kg)'
    else:
        entrada_label = 'Produção (kg)'

    if loja_db == 'Bolhão':
        formula = "Consumo = Stock Início + Transferências - Stock Fim - Quebras | €/kg = Vendas Gelado / Consumo"
    elif loja_db == 'Matosinhos':
        formula = "Consumo = Stock Início + (Produção - Transferências) - Stock Fim - Quebras | €/kg = Vendas Gelado / Consumo"
    else:
        formula = "Consumo = Stock Início + Produção - Stock Fim - Quebras | €/kg = Vendas Gelado / Consumo"

    annual_resumo = calculate_kpi_annual(current_year, loja_db)
    resumo_rows = []
    total_consumo = 0
    total_vendas = 0
    total_entrada = 0
    total_quebras = 0

    for m in range(1, 13):
        kpi_data = annual_resumo[m]
        consumo_kg = kpi_data['consumo']
        vendas_eur = kpi_data['vendas']
        euro_kg = kpi_data['kpi']
        target = get_target_by_month(m)
        entrada_val = kpi_data.get('entrada', kpi_data['producao'])

        total_consumo += consumo_kg
        total_vendas += vendas_eur
        total_entrada += entrada_val
        total_quebras += kpi_data['quebras']

        resumo_rows.append({
            'mes': meses_nomes[m - 1],
            'stock_ini': round(kpi_data['stock_ini'], 2) if kpi_data['stock_ini'] > 0 else 0,
            'entrada': round(entrada_val, 2) if entrada_val > 0 else 0,
            'stock_final': round(kpi_data['stock_final'], 2) if kpi_data['stock_final'] > 0 else 0,
            'quebras': round(kpi_data['quebras'], 2) if kpi_data['quebras'] > 0 else 0,
            'consumo': round(consumo_kg, 2),
            'vendas': round(vendas_eur, 2),
            'euro_kg': round(euro_kg, 2),
            'target': target
        })

    total_euro_kg = total_vendas / total_consumo if total_consumo > 0 else 0

    tabs = _build_tabs(loja_filter, is_gestor, 'resumo')

    return render_template('eurokg/resumo_mensal.html',
        active_tab='resumo', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        current_year=current_year, entrada_label=entrada_label, formula=formula,
        resumo_rows=resumo_rows,
        total_entrada=round(total_entrada, 2),
        total_quebras=round(total_quebras, 2),
        total_consumo=round(total_consumo, 2),
        total_vendas=round(total_vendas, 2),
        total_euro_kg=round(total_euro_kg, 2))


@eurokg_bp.route('/consumo')
@perm_required('acesso_eurokg')
def consumo_teorico():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter = request.args.get('loja', 'Global Porto' if is_gestor else None)
    if loja_filter == 'Global Porto':
        loja_db = None
    elif loja_filter:
        loja_db = loja_filter
    else:
        loja_db = None

    consumo_df = get_consumo_gelado_mensal(loja_db)

    consumo_data = []
    meses_labels = []
    if not consumo_df.empty:
        from datetime import datetime
        meses = sorted(consumo_df['mes'].unique())
        meses_labels = [datetime.strptime(m, '%Y-%m').strftime('%b %Y') for m in meses]

        pivot_qty = consumo_df.pivot_table(index='produto', columns='mes', values='quantidade_vendida', aggfunc='sum', fill_value=0)
        pivot_kg = consumo_df.pivot_table(index='produto', columns='mes', values='consumo_kg', aggfunc='sum', fill_value=0)

        gramas_map = {}
        for _, row in consumo_df.drop_duplicates('produto').iterrows():
            gramas_map[row['produto']] = row['gramas_por_unidade']

        totals_qty = [0] * len(meses)
        totals_kg = [0] * len(meses)

        for produto in pivot_qty.index:
            row_data = {
                'produto': produto,
                'gramas': gramas_map.get(produto, 0),
                'months': []
            }
            for i, mes in enumerate(meses):
                qty = int(pivot_qty.loc[produto, mes]) if mes in pivot_qty.columns else 0
                kg = round(float(pivot_kg.loc[produto, mes]), 2) if mes in pivot_kg.columns else 0
                row_data['months'].append({'qty': qty, 'kg': kg})
                totals_qty[i] += qty
                totals_kg[i] += kg
            consumo_data.append(row_data)

        totals_kg = [round(k, 2) for k in totals_kg]
    else:
        totals_qty = []
        totals_kg = []

    gramas_list = get_gramas_gelado()

    tabs = _build_tabs(loja_filter, is_gestor, 'consumo')

    return render_template('eurokg/consumo_teorico.html',
        active_tab='consumo', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        consumo_data=consumo_data, meses_labels=meses_labels,
        totals_qty=totals_qty, totals_kg=totals_kg,
        gramas_list=gramas_list)


@eurokg_bp.route('/vendas-produto')
@perm_required('acesso_eurokg')
def vendas_produto():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter = request.args.get('loja', 'Global Porto' if is_gestor else None)
    if loja_filter == 'Global Porto':
        loja_db = None
    elif loja_filter:
        loja_db = loja_filter
    else:
        loja_db = None

    today = date.today()
    year = request.args.get('year', today.year, type=int)
    available_years = list(range(today.year, today.year - 4, -1))

    table = get_vendas_produto_mensal(year, loja_db)

    tabs = _build_tabs(loja_filter, is_gestor, 'vendas')

    return render_template('eurokg/vendas_produto.html',
        active_tab='vendas', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        year=year, available_years=available_years,
        table=table,
        meses_abrev=MESES_PT_ABREV,
        current_month=today.month,
    )


@eurokg_bp.route('/consumo/add_gramas', methods=['POST'])
@perm_required('acesso_gestor')
def add_gramas():
    artigo = request.form.get('artigo', '').strip()
    gramas_str = request.form.get('gramas', '0').replace(',', '.')
    loja_filter = request.form.get('loja_filter', 'Global Porto')

    try:
        gramas = float(gramas_str)
    except ValueError:
        gramas = 0

    if artigo and gramas > 0:
        if add_gramas_gelado(artigo, gramas):
            flash(f"Artigo '{artigo}' adicionado com {gramas}g", 'success')
        else:
            flash("Erro ao adicionar. O artigo pode já existir.", 'error')
    else:
        flash("Preencha todos os campos.", 'warning')

    return redirect(url_for('eurokg.consumo_teorico', loja=loja_filter))
