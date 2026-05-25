import sys
import os
from datetime import date, datetime
from flask import Blueprint, render_template, url_for, request, redirect, flash, session
from flask_app.auth import perm_required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import get_contas_por_fornecedor, mark_payment_executed

financeiro_bp = Blueprint('financeiro', __name__)

FINANCEIRO_GROUPS = [
    {
        'label': 'Gestão de Pagamentos',
        'modules': [
            {'key': 'faturas',    'label': 'Documentos',       'icon': '📄', 'active': True,  'url': '/financeiro/faturas/'},
            {'key': 'credito',    'label': 'Crédito',           'icon': '💳', 'active': True,  'url': '/financeiro/credito/'},
            {'key': 'iva',        'label': 'IVA',               'icon': '📋', 'active': True,  'url': '/financeiro/pagamentos/iva'},
            {'key': 'liquidez',   'label': 'Tesouraria Previsional', 'icon': '🏦', 'active': True,  'url': '/financeiro/pagamentos/liquidez'},
            {'key': 'avencas',    'label': 'Avenças',           'icon': '🔁', 'active': True,  'url_func': 'avencas.index'},
            {'key': 'debitos',    'label': 'Débitos Diretos',   'icon': '🔄', 'active': True,  'url_func': 'cashflow.debitos'},
            {'key': 'salarios',   'label': 'Salários',          'icon': '👥', 'active': True,  'url_func': 'cashflow.salarios'},
        ],
    },
    {
        'label': 'Planeamento & Previsão',
        'modules': [
            {'key': 'dashboard_vendas', 'label': 'Dashboard de Vendas', 'icon': '📊', 'active': True,  'url_func': 'financeiro.dashboard_vendas'},
            {'key': 'vendas_diarias', 'label': 'Vendas Diárias',       'icon': '📅', 'active': True,  'url_func': 'financeiro.vendas_diarias'},
            {'key': 'variaveis_previsao', 'label': 'Variáveis de Previsão', 'icon': '🌡️', 'active': True,  'url_func': 'financeiro.variaveis_previsao'},
            {'key': 'previsao_30dias',   'label': 'Previsão 30 Dias',      'icon': '🔮', 'active': True,  'url_func': 'financeiro.previsao_30dias'},
        ],
    },
    {
        'label': 'Configuração',
        'modules': [
            {'key': 'centros_custo',             'label': 'Centros de Custo',      'icon': '🏷️', 'active': True,  'url_func': 'centros_custo.index'},
            {'key': 'categorias',                'label': 'Categorias de Custo',   'icon': '📂', 'active': True,  'url_func': 'categorias_custo.index'},
            {'key': 'distribuicao_centros_custo','label': 'Distribuição Centros de Custo', 'icon': '📊', 'active': True,  'url_func': 'financeiro.distribuicao_centros_custo'},
        ],
    },
]

PAYMENT_METHODS = [
    ('transferencia', 'Transferência Bancária'),
    ('cheque', 'Cheque'),
    ('debito_direto', 'Débito Direto'),
    ('numerario', 'Numerário'),
    ('outro', 'Outro'),
]


def _get_username():
    return session.get('user', {}).get('username', 'sistema')


@financeiro_bp.route('/')
@perm_required('acesso_gestor')
def index():
    from db.tiles import get_tile_visibility, seed_tile_config
    all_tiles = [{'id': m['key'], 'label': m['label']} for g in FINANCEIRO_GROUPS for m in g['modules']]
    seed_tile_config('financeiro', all_tiles)
    visibility = get_tile_visibility('financeiro')
    groups = []
    for g in FINANCEIRO_GROUPS:
        resolved = []
        for m in g['modules']:
            if not visibility.get(m['key'], True):
                continue
            entry = dict(m)
            if entry['active'] and entry.get('url_func'):
                entry['url'] = url_for(entry['url_func'])
            elif not entry.get('url'):
                entry['url'] = None
            resolved.append(entry)
        if resolved:
            groups.append({'label': g['label'], 'modules': resolved})
    return render_template('financeiro/index.html', groups=groups)


@financeiro_bp.route('/contas-fornecedor')
def contas_fornecedor():
    return redirect(url_for('faturas.index', view='fornecedor'))


@financeiro_bp.route('/contas-fornecedor/liquidar', methods=['POST'])
@perm_required('acesso_gestor')
def liquidar_fornecedor():
    ids = request.form.getlist('invoice_ids', type=int)
    paid_date_str = request.form.get('paid_date', str(date.today()))
    payment_method = request.form.get('payment_method', 'transferencia')
    try:
        paid_date = datetime.strptime(paid_date_str, '%Y-%m-%d').date()
    except ValueError:
        paid_date = date.today()

    if not ids:
        flash('Nenhum documento selecionado.', 'warning')
        return redirect(url_for('faturas.index', view='fornecedor'))

    username = _get_username()
    method_label = dict(PAYMENT_METHODS).get(payment_method, payment_method)
    payment_note = f'Liquidação em lote via {method_label} ({paid_date.strftime("%d/%m/%Y")})'
    ok = 0
    fail = 0
    for inv_id in ids:
        try:
            mark_payment_executed(inv_id, paid_date, username, notes=payment_note)
            ok += 1
        except Exception:
            fail += 1

    if fail:
        flash(f'{ok} documento(s) liquidado(s). {fail} falharam.', 'warning')
    else:
        flash(f'{ok} documento(s) liquidado(s) via {method_label} em {paid_date.strftime("%d/%m/%Y")}.', 'success')

    return redirect(url_for('faturas.index', view='fornecedor'))


@financeiro_bp.route('/variaveis-previsao')
@perm_required('acesso_gestor')
def variaveis_previsao():
    from db.vendas_diarias import get_variaveis_previsao
    import json
    data = get_variaveis_previsao()
    return render_template(
        'financeiro/variaveis_previsao.html',
        data_json=json.dumps(data, ensure_ascii=False, default=str),
    )


@financeiro_bp.route('/previsao-30-dias')
@perm_required('acesso_gestor')
def previsao_30dias():
    from db.vendas_diarias import get_previsao_30dias, get_backtesting_rolling
    import json
    data = get_previsao_30dias()
    backtest = get_backtesting_rolling()
    return render_template(
        'financeiro/previsao_30dias.html',
        data_json=json.dumps(data, ensure_ascii=False, default=str),
        backtest_json=json.dumps(backtest, ensure_ascii=False, default=str),
    )


@financeiro_bp.route('/previsao-30-dias/config')
@perm_required('acesso_gestor')
def previsao_30dias_config():
    from flask import jsonify
    from db.forecast import get_meteo_config, get_wind_config
    lojas = ['Matosinhos', 'Bolhão']
    result = {}
    for loja in lojas:
        key = 'mat' if loja == 'Matosinhos' else 'bol'
        meteo = get_meteo_config(loja)
        wind  = get_wind_config(loja)
        updated_at = None
        for row in (meteo + wind):
            ts = row.get('updated_at')
            if ts:
                ts_str = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
                if updated_at is None or ts_str > updated_at:
                    updated_at = ts_str
        result[key] = {
            'loja': loja,
            'meteo': [
                {
                    'score_min': r['score_min'],
                    'score_max': r['score_max'],
                    'multiplicador': r['multiplicador'],
                    'updated_at': r['updated_at'].isoformat() if r.get('updated_at') and hasattr(r['updated_at'], 'isoformat') else (str(r['updated_at']) if r.get('updated_at') else None),
                }
                for r in meteo
            ],
            'wind': [
                {
                    'vento_min': r['vento_min'],
                    'vento_max': r['vento_max'],
                    'multiplicador': r['multiplicador'],
                    'updated_at': r['updated_at'].isoformat() if r.get('updated_at') and hasattr(r['updated_at'], 'isoformat') else (str(r['updated_at']) if r.get('updated_at') else None),
                }
                for r in wind
            ],
            'updated_at': updated_at,
        }
    return jsonify(result)


@financeiro_bp.route('/dashboard-vendas')
@perm_required('acesso_gestor')
def dashboard_vendas():
    from db.vendas_diarias import get_dashboard_vendas
    import json
    data = get_dashboard_vendas()
    return render_template(
        'financeiro/dashboard_vendas.html',
        data_json=json.dumps(data, ensure_ascii=False, default=str),
        cutoff=data.get('cutoff'),
        lojas=data.get('lojas', []),
    )


@financeiro_bp.route('/vendas-diarias')
@perm_required('acesso_gestor')
def vendas_diarias():
    from db.vendas_diarias import get_vendas_diarias_yoy
    from db.connection import db_connection

    today = date.today()
    ano_atual = today.year

    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT year_val FROM (
                    SELECT EXTRACT(ISOYEAR FROM data)::int AS year_val FROM vendas_detalhe
                    UNION
                    SELECT EXTRACT(ISOYEAR FROM data)::int AS year_val FROM vendas
                    UNION
                    SELECT EXTRACT(ISOYEAR FROM data)::int AS year_val FROM sales_historico
                ) t ORDER BY year_val DESC
            """)
            anos_disponiveis = [r[0] for r in cur.fetchall()] or [ano_atual]
    except Exception:
        anos_disponiveis = [ano_atual]

    try:
        ano_sel = int(request.args.get('ano', ano_atual))
    except (ValueError, TypeError):
        ano_sel = ano_atual

    try:
        mes_sel_raw = request.args.get('mes', '')
        mes_sel = int(mes_sel_raw) if mes_sel_raw else None
    except (ValueError, TypeError):
        mes_sel = None

    try:
        semana_sel_raw = request.args.get('semana', '')
        semana_sel = int(semana_sel_raw) if semana_sel_raw else None
    except (ValueError, TypeError):
        semana_sel = None

    try:
        weekday_sel_raw = request.args.get('weekday', '')
        weekday_sel = int(weekday_sel_raw) if weekday_sel_raw else None
    except (ValueError, TypeError):
        weekday_sel = None

    rows = get_vendas_diarias_yoy(ano_sel, mes=mes_sel, iso_week=semana_sel, weekday=weekday_sel)

    def _sum(vals):
        data_pts = [v for v in vals if v is not None]
        return round(sum(data_pts), 2) if data_pts else None

    totais = {
        'total_atual':         _sum(r['total_atual']         for r in rows if not r['is_future']),
        'total_anterior':      _sum(r['total_anterior']      for r in rows),
        'bolhao_atual':        _sum(r['bolhao_atual']        for r in rows if not r['is_future']),
        'bolhao_anterior':     _sum(r['bolhao_anterior']     for r in rows),
        'matosinhos_atual':    _sum(r['matosinhos_atual']    for r in rows if not r['is_future']),
        'matosinhos_anterior': _sum(r['matosinhos_anterior'] for r in rows),
    }

    MESES_PT = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
    WEEKDAY_NAMES = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']

    return render_template(
        'financeiro/vendas_diarias.html',
        rows=rows,
        totais=totais,
        ano_sel=ano_sel,
        mes_sel=mes_sel,
        semana_sel=semana_sel,
        weekday_sel=weekday_sel,
        anos_disponiveis=anos_disponiveis,
        ano_anterior=ano_sel - 1,
        meses_pt=MESES_PT,
        weekday_names=WEEKDAY_NAMES,
        hoje=today,
    )


@financeiro_bp.route('/distribuicao-centros-custo', methods=['GET', 'POST'])
@perm_required('acesso_financeiro')
def distribuicao_centros_custo():
    from db.centros_custo import get_cost_categories, get_all_allocations, save_allocation
    from db.stores import get_all_stores

    stores = [s for s in get_all_stores() if s['is_active']]
    categories = get_cost_categories(ativo_only=True)

    if request.method == 'POST':
        for cat in categories:
            cid = cat['id']
            modo = request.form.get(f'modo_{cid}', 'volume_vendas')
            store_pct: dict = {}

            if modo == 'tudo_loja':
                sid_str = request.form.get(f'tudo_loja_store_{cid}')
                if sid_str:
                    try:
                        store_pct[int(sid_str)] = 100.0
                    except (ValueError, TypeError):
                        pass

            elif modo == 'manual':
                for store in stores:
                    sid = store['id']
                    val_str = request.form.get(f'pct_{cid}_{sid}', '0')
                    try:
                        store_pct[sid] = float(val_str)
                    except (ValueError, TypeError):
                        store_pct[sid] = 0.0

            save_allocation(cid, modo, store_pct)

        flash('Configuração de distribuição guardada com sucesso.', 'success')
        return redirect(url_for('financeiro.distribuicao_centros_custo'))

    allocations = get_all_allocations()
    return render_template(
        'financeiro/distribuicao_centros_custo.html',
        categories=categories,
        stores=stores,
        allocations=allocations,
    )
