import sys
import os
import logging
from datetime import date, datetime
from flask import Blueprint, render_template, url_for, request, redirect, flash, session
from flask_app.auth import perm_required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import get_contas_por_fornecedor, mark_payment_executed
from db.pagamentos import get_already_paid_invoices

logger = logging.getLogger(__name__)

financeiro_bp = Blueprint('financeiro', __name__)

FINANCEIRO_GROUPS = [
    {
        'label': 'Gestão de Pagamentos',
        'modules': [
            {'key': 'faturas',       'label': 'Documentos',        'icon': '📄', 'active': True,  'url': '/financeiro/faturas/'},
            {'key': 'fornecedores',  'label': 'Fornecedores',      'icon': '🏭', 'active': True,  'url': '/financeiro/faturas/fornecedores'},
            {'key': 'dashboard_faturas', 'label': 'Dashboard Faturas', 'icon': '📊', 'active': True,  'url': '/financeiro/faturas/dashboard'},
            {'key': 'credito',    'label': 'Crédito',           'icon': '💳', 'active': True,  'url': '/financeiro/credito/'},
            {'key': 'iva',        'label': 'IVA',               'icon': '📋', 'active': True,  'url': '/financeiro/pagamentos/iva'},
            {'key': 'liquidez',   'label': 'Tesouraria Previsional', 'icon': '🏦', 'active': True,  'url': '/financeiro/pagamentos/liquidez'},
            {'key': 'debitos',    'label': 'Custos Recorrentes', 'icon': '♻️', 'active': True,  'url_func': 'cashflow.debitos'},
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
            {'key': 'pl_por_loja',       'label': 'P&L por Loja',          'icon': '🏪', 'active': True,  'url_func': 'financeiro.pl_por_loja'},
            {'key': 'orcamento',         'label': 'Orçamento',             'icon': '🎯', 'active': True,  'url_func': 'financeiro.orcamento'},
            {'key': 'insights',          'label': 'Controlo de Custos',    'icon': '📈', 'active': True,  'url_func': 'financeiro.insights'},
        ],
    },
    {
        'label': 'B2B & Eventos',
        'modules': [
            {'key': 'clientes_b2b',    'label': 'Clientes',          'icon': '🏢', 'active': True,  'url_func': 'financeiro.clientes_b2b'},
            {'key': 'faturas_clientes','label': 'Faturas Clientes',   'icon': '🧾', 'active': True,  'url_func': 'financeiro.faturas_clientes'},
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
    from db.tiles import get_tile_visibility, seed_tile_config, get_tile_labels, get_tile_icons, get_module_labels
    all_tiles = [{'id': m['key'], 'label': m['label']} for g in FINANCEIRO_GROUPS for m in g['modules']]
    seed_tile_config('financeiro', all_tiles)
    visibility = get_tile_visibility('financeiro')
    labels = get_tile_labels('financeiro')
    icons = get_tile_icons('financeiro')
    custom_mod = get_module_labels().get('financeiro')
    groups = []
    for g in FINANCEIRO_GROUPS:
        resolved = []
        for m in g['modules']:
            if not visibility.get(m['key'], True):
                continue
            entry = dict(m)
            entry['label'] = labels.get(m['key']) or m['label']
            entry['icon'] = icons.get(m['key']) or m['icon']
            if entry['active'] and entry.get('url_func'):
                entry['url'] = url_for(entry['url_func'])
            elif not entry.get('url'):
                entry['url'] = None
            resolved.append(entry)
        if resolved:
            groups.append({'label': g['label'], 'modules': resolved})
    module_title = f'💰 {custom_mod}' if custom_mod else '💰 Financeiro'
    from db.faturas import count_invoices_sem_categoria
    sem_categoria_count = count_invoices_sem_categoria()
    return render_template('financeiro/index.html', groups=groups, module_title=module_title,
                           sem_categoria_count=sem_categoria_count)


@financeiro_bp.route('/contas-fornecedor')
def contas_fornecedor():
    return redirect(url_for('faturas.index', view='fornecedor'))


@financeiro_bp.route('/contas-fornecedor/liquidar', methods=['POST'])
@perm_required('acesso_gestor')
def liquidar_fornecedor():
    ids = request.form.getlist('invoice_ids', type=int)
    paid_date_str = request.form.get('paid_date', str(date.today()))
    payment_method = request.form.get('payment_method', 'transferencia')
    force_duplicate = request.form.get('force_duplicate', '0') == '1'

    try:
        paid_date = datetime.strptime(paid_date_str, '%Y-%m-%d').date()
    except ValueError:
        paid_date = date.today()

    if not ids:
        flash('Nenhum documento selecionado.', 'warning')
        return redirect(url_for('faturas.index', view='fornecedor'))

    # Guard: warn before re-paying already-paid invoices
    if not force_duplicate:
        already_paid = get_already_paid_invoices(ids)
        if already_paid:
            refs = ', '.join(
                f"{r['invoice_number'] or '(sem nº)'} — {r['supplier_name']}"
                for r in already_paid
            )
            flash(
                f'⚠️ {len(already_paid)} fatura(s) já estão pagas: {refs}. '
                'Marque "Confirmar mesmo assim" no modal para processar na mesma.',
                'warning',
            )
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
        except Exception as e:
            logger.error('liquidar_fornecedor inv=%s: %s', inv_id, e, exc_info=True)
            fail += 1

    if fail:
        flash(f'{ok} documento(s) liquidado(s). {fail} falharam (ver logs).', 'warning')
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
    from db.vendas_diarias import get_dashboard_vendas, get_dashboard_b2b
    import json
    data = get_dashboard_vendas()
    b2b_data = get_dashboard_b2b()
    return render_template(
        'financeiro/dashboard_vendas.html',
        data_json=json.dumps(data, ensure_ascii=False, default=str),
        b2b_json=json.dumps(b2b_data, ensure_ascii=False, default=str),
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


@financeiro_bp.route('/pl-por-loja')
@perm_required('acesso_financeiro')
def pl_por_loja():
    from db.mapa_exploracao import get_mapa_exploracao
    import io
    import csv as csv_mod
    from flask import Response

    today = date.today()
    try:
        ano = int(request.args.get('ano', today.year))
        if ano < 2000 or ano > 2100:
            ano = today.year
    except (ValueError, TypeError):
        ano = today.year

    store_id_raw = request.args.get('store_id', '').strip()
    store_id = int(store_id_raw) if store_id_raw.isdigit() else None

    mapa = get_mapa_exploracao(ano, store_id=store_id)

    # Pre-compute per-month cost totals so the template can reference them directly
    _months = list(range(1, 13))
    _tcm: dict = {}
    for _cat in mapa['categories']:
        _cd = mapa['costs'].get(_cat['id'], {})
        for _m in _months:
            _tcm[_m] = round(_tcm.get(_m, 0) + _cd.get(_m, 0), 2)
    # Include uncategorised invoices so EBITDA / Total Custos are complete
    for _m in _months:
        _tcm[_m] = round(_tcm.get(_m, 0) + mapa['costs_uncat'].get(_m, 0), 2)
    mapa['total_costs_monthly'] = _tcm

    if mapa['mode'] == 'consolidated':
        _stcm: dict = {}
        for _s in mapa['stores']:
            _sid = _s['id']
            _sm: dict = {}
            for _cat in mapa['categories']:
                _cd = mapa['store_costs'].get(_sid, {}).get(_cat['id'], {})
                for _m in _months:
                    _sm[_m] = round(_sm.get(_m, 0) + _cd.get(_m, 0), 2)
            # Uncategorised are global/unallocated — not added per-store to avoid double-count
            _stcm[_sid] = _sm
        mapa['store_total_costs_monthly'] = _stcm

    if request.args.get('export') == 'csv':
        output = io.StringIO()
        writer = csv_mod.writer(output)
        months = list(range(1, 13))
        MESES = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

        scope = mapa['store']['name'] if mapa['store'] else 'Consolidado'
        is_consolidated = mapa['mode'] == 'consolidated'
        writer.writerow([f'Mapa de Exploração {ano} — {scope}'])
        writer.writerow(['Linha'] + MESES + ['Total'])

        def _row(label, d):
            vals = [f"{d.get(m, 0):.2f}" for m in months]
            total = f"{sum(d.get(m, 0) for m in months):.2f}"
            writer.writerow([label] + vals + [total])

        def _gap_pct_row(label, d_actual, d_ref):
            """Variance row: (actual − ref) / ref × 100 — used for gap vs budget/prior-year."""
            row_vals = []
            for m in months:
                a = d_actual.get(m, 0)
                r = d_ref.get(m, 0)
                row_vals.append(f"{(a - r) / r * 100:.1f}%" if r else '')
            ta = sum(d_actual.get(m, 0) for m in months)
            tr = sum(d_ref.get(m, 0) for m in months)
            writer.writerow([label] + row_vals + [f"{(ta - tr) / tr * 100:.1f}%" if tr else ''])

        def _gap_eur_row(label, d_actual, d_ref):
            """Absolute euro gap row: actual − ref per month."""
            row_vals = []
            for m in months:
                a = d_actual.get(m, 0)
                r = d_ref.get(m, 0)
                if d_ref.get(m) is not None or d_actual.get(m) is not None:
                    row_vals.append(f"{a - r:+.2f}")
                else:
                    row_vals.append('')
            ta = sum(d_actual.get(m, 0) for m in months)
            tr = sum(d_ref.get(m, 0) for m in months)
            writer.writerow([label] + row_vals + [f"{ta - tr:+.2f}"])

        def _ratio_row(label, d_numerator, d_denominator):
            """Direct-ratio row: numerator / denominator × 100 — used for % of sales."""
            row_vals = []
            for m in months:
                n = d_numerator.get(m, 0)
                d = d_denominator.get(m, 0)
                row_vals.append(f"{n / d * 100:.1f}%" if d else '')
            tn = sum(d_numerator.get(m, 0) for m in months)
            td = sum(d_denominator.get(m, 0) for m in months)
            writer.writerow([label] + row_vals + [f"{tn / td * 100:.1f}%" if td else ''])

        today_m = mapa['today'].month
        is_current_year = (ano == mapa['today'].year)
        v_d = mapa['vendas']   # shorthand

        # ── VENDAS ─────────────────────────────────────────────────────────
        writer.writerow([])
        writer.writerow(['--- VENDAS ---'])
        _row('VENDAS — Real', v_d)
        _row('VENDAS — Orçamento', mapa['budget_vendas'])
        _gap_pct_row('VENDAS — Gap Orç. %', v_d, mapa['budget_vendas'])
        _gap_eur_row('VENDAS — Gap Orç. €', v_d, mapa['budget_vendas'])
        _row('VENDAS — Histórico AA', mapa['vendas_aa'])
        _gap_pct_row('VENDAS — Gap Hist. %', v_d, mapa['vendas_aa'])
        _gap_eur_row('VENDAS — Gap Hist. €', v_d, mapa['vendas_aa'])
        if is_current_year:
            # YTD running totals (only up to current month; '' for future months)
            ytd_a_vals = []
            ytd_h_vals = []
            ytd_pct_vals = []
            ytd_eur_vals = []
            ra, rh = 0.0, 0.0
            for m in months:
                ra += v_d.get(m, 0)
                rh += mapa['vendas_aa'].get(m, 0)
                if m <= today_m:
                    ytd_a_vals.append(f"{ra:.2f}")
                    ytd_h_vals.append(f"{rh:.2f}")
                    ytd_pct_vals.append(f"{(ra - rh) / rh * 100:.1f}%" if rh else '')
                    ytd_eur_vals.append(f"{ra - rh:+.2f}")
                else:
                    ytd_a_vals.append('')
                    ytd_h_vals.append('')
                    ytd_pct_vals.append('')
                    ytd_eur_vals.append('')
            ta = sum(v_d.get(m, 0) for m in months[:today_m])
            th = sum(mapa['vendas_aa'].get(m, 0) for m in months[:today_m])
            writer.writerow(['VENDAS — Acc YTD'] + ytd_a_vals + [f"{ta:.2f}"])
            writer.writerow(['VENDAS — Acc AA YTD'] + ytd_h_vals + [f"{th:.2f}"])
            writer.writerow(['VENDAS — Acc YTD vs AA %'] + ytd_pct_vals + [f"{(ta - th) / th * 100:.1f}%" if th else ''])
            writer.writerow(['VENDAS — Acc YTD vs AA €'] + ytd_eur_vals + [f"{ta - th:+.2f}"])
        if is_consolidated:
            for s in mapa['stores']:
                _row(f'  {s["name"]} — Real', mapa['store_vendas'].get(s['id'], {}))
                _row(f'  {s["name"]} — AA', mapa['store_vendas_aa'].get(s['id'], {}))

        # ── CMVMC ──────────────────────────────────────────────────────────
        writer.writerow([])
        writer.writerow(['--- CMVMC ---'])
        _row('CMVMC — Real', mapa['cmvmc'])
        _row('CMVMC — Orçamento', mapa['budget_cmvmc'])
        _gap_pct_row('CMVMC — Gap Orç. %', mapa['cmvmc'], mapa['budget_cmvmc'])
        _row('CMVMC — Histórico AA', mapa['cmvmc_aa'])
        _gap_pct_row('CMVMC — Gap Hist. %', mapa['cmvmc'], mapa['cmvmc_aa'])
        if is_consolidated:
            for s in mapa['stores']:
                _row(f'  {s["name"]} — Real', mapa['store_cmvmc'].get(s['id'], {}))
                _row(f'  {s["name"]} — AA', mapa['store_cmvmc_aa'].get(s['id'], {}))
            ua_c = mapa.get('unallocated_cmvmc', {})
            if ua_c:
                _row('  Não alocado', ua_c)

        # ── MARGEM BRUTA ───────────────────────────────────────────────────
        writer.writerow([])
        writer.writerow(['--- MARGEM BRUTA ---'])
        mb = {m: round(v_d.get(m, 0) - mapa['cmvmc'].get(m, 0), 2) for m in months}
        _row('MARGEM BRUTA', mb)
        _ratio_row('MARGEM BRUTA — % sobre Vendas', mb, v_d)

        # ── CUSTOS DE OPERAÇÃO ─────────────────────────────────────────────
        writer.writerow([])
        writer.writerow(['--- CUSTOS DE OPERAÇÃO ---'])
        tot = {m: 0.0 for m in months}
        for cat in mapa['categories']:
            cid = cat['id']
            cat_d = mapa['costs'].get(cid, {})
            cat_orc = mapa['budget_costs'].get(cid, {})
            cat_aa  = mapa['costs_aa'].get(cid, {})
            _row(cat['name'] + ' — Real', cat_d)
            _ratio_row(cat['name'] + ' — % sobre Vendas', cat_d, v_d)
            if cat_orc:
                _row(cat['name'] + ' — Orçamento', cat_orc)
            if cat_aa:
                _row(cat['name'] + ' — Histórico AA', cat_aa)
            if is_consolidated:
                for s in mapa['stores']:
                    sd = mapa['store_costs'].get(s['id'], {}).get(cid, {})
                    _row(f'  {s["name"]} — {cat["name"]}', sd)
                ua = mapa.get('unallocated_costs', {}).get(cid, {})
                if ua:
                    _row(f'  Não alocado — {cat["name"]}', ua)
            for m in months:
                tot[m] = round(tot[m] + cat_d.get(m, 0), 2)

        # Uncategorised invoices (included in Total Custos / EBITDA)
        uncat = mapa.get('costs_uncat', {})
        if uncat:
            _row('Sem categoria (⚠️ não classificadas)', uncat)
            uca_aa = mapa.get('costs_uncat_aa', {})
            if uca_aa:
                _row('Sem categoria — Histórico AA', uca_aa)
            for m in months:
                tot[m] = round(tot[m] + uncat.get(m, 0), 2)

        # ── TOTAL CUSTOS ───────────────────────────────────────────────────
        writer.writerow([])
        _row('TOTAL CUSTOS OP.', tot)

        # ── EBITDA ─────────────────────────────────────────────────────────
        writer.writerow([])
        writer.writerow(['--- EBITDA ---'])
        ebitda = {m: round(mb.get(m, 0) - tot.get(m, 0), 2) for m in months}
        _row('EBITDA', ebitda)
        _ratio_row('EBITDA — % sobre Vendas', ebitda, v_d)

        output.seek(0)
        filename = f"mapa_exploracao_{ano}_{scope.lower().replace(' ', '_')}.csv"
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    return render_template(
        'financeiro/pl_por_loja.html',
        mapa=mapa,
        ano=ano,
        store_id=store_id,
    )


@financeiro_bp.route('/insights')
@perm_required('acesso_financeiro')
def insights():
    from db.insights import get_financial_insights
    from db.stores import get_all_stores

    today = date.today()
    try:
        ano = int(request.args.get('ano', today.year))
        if ano < 2000 or ano > 2100:
            ano = today.year
    except (ValueError, TypeError):
        ano = today.year

    store_id_raw = request.args.get('store_id', '').strip()
    store_id = int(store_id_raw) if store_id_raw.isdigit() else None

    active_stores = [s for s in get_all_stores() if s['is_active']]
    data = get_financial_insights(ano, store_id=store_id)

    # Pass chart payload as a separate dict so the template can use |tojson
    # (Jinja's tojson HTML-escapes </script> etc., preventing script-breakout XSS)
    chart_payload = {
        'chart_labels':  data['chart_labels'],
        'vendas_series': data['vendas_series'],
        'vendas_aa_s':   data['vendas_aa_s'],
        'bgt_vendas_s':  data['bgt_vendas_s'],
        'cost_series':   data['cost_series'],
        'total_costs_s': data['total_costs_s'],
    }

    # Year-aware period label so KPI cards and the header are unambiguous
    MESES_PT_SHORT = ['', 'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                      'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
    current_m = data['current_m']
    if ano < today.year:
        period_label  = 'Anual'
        period_detail = f'Jan–Dez {ano} (ano completo)'
    elif ano > today.year:
        period_label  = 'Projetado'
        period_detail = f'Jan–Dez {ano} (ano futuro)'
    else:
        period_label  = 'YTD'
        period_detail = (f'Jan–{MESES_PT_SHORT[current_m]} {ano}'
                         f' (até {today.strftime("%d/%m/%Y")})')

    return render_template(
        'financeiro/insights.html',
        data=data,
        ano=ano,
        store_id=store_id,
        active_stores=active_stores,
        chart_payload=chart_payload,
        period_label=period_label,
        period_detail=period_detail,
    )


@financeiro_bp.route('/distribuicao-centros-custo', methods=['GET', 'POST'])
@perm_required('acesso_financeiro')
def distribuicao_centros_custo():
    from db.centros_custo import (
        get_cost_categories, get_all_allocations, save_allocation,
        get_sales_split_pct,
    )
    from db.stores import get_all_stores

    stores = [s for s in get_all_stores() if s['is_active']]
    categories = get_cost_categories(ativo_only=True)

    _VALID_MODOS = {'volume_vendas', 'tudo_loja', 'manual', 'igualitario'}

    if request.method == 'POST':
        for cat in categories:
            cid = cat['id']
            modo = request.form.get(f'modo_{cid}', 'volume_vendas')
            if modo not in _VALID_MODOS:
                modo = 'volume_vendas'
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

            elif modo == 'igualitario':
                # 50 % per active store — stored exactly like manual
                for store in stores:
                    store_pct[store['id']] = 50.0

            save_allocation(cid, modo, store_pct)

        flash('Configuração de distribuição guardada com sucesso.', 'success')
        return redirect(url_for('financeiro.distribuicao_centros_custo'))

    allocations = get_all_allocations()
    try:
        sales_split = get_sales_split_pct(months=12)
    except Exception:
        sales_split = {}

    return render_template(
        'financeiro/distribuicao_centros_custo.html',
        categories=categories,
        stores=stores,
        allocations=allocations,
        sales_split=sales_split,
    )


@financeiro_bp.route('/orcamento')
@perm_required('acesso_financeiro')
def orcamento():
    from db.orcamento import get_orcamento
    from db.centros_custo import get_cost_categories
    from db.stores import get_all_stores

    today = date.today()
    stores = [s for s in get_all_stores() if s['is_active']]
    categories = get_cost_categories(ativo_only=True)

    try:
        ano = int(request.args.get('ano', today.year))
    except (ValueError, TypeError):
        ano = today.year

    store_id_raw = request.args.get('store_id', '').strip()
    store_id = int(store_id_raw) if store_id_raw.isdigit() else None

    budget = get_orcamento(ano, store_id=store_id)

    anos = sorted({today.year - 1, today.year, today.year + 1, ano})
    meses_pt = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

    return render_template(
        'financeiro/orcamento.html',
        stores=stores,
        categories=categories,
        ano=ano,
        store_id=store_id,
        budget=budget,
        anos=anos,
        meses_pt=meses_pt,
    )


@financeiro_bp.route('/orcamento/cell', methods=['POST'])
@perm_required('acesso_financeiro')
def orcamento_cell():
    from flask import jsonify
    from db.orcamento import upsert_orcamento

    data = request.get_json(silent=True) or {}
    try:
        ano = int(data['ano'])
        mes = int(data['mes'])
        line_key = str(data['line_key']).strip()
        raw_val = str(data.get('valor', 0)).replace(',', '.').strip()
        valor = float(raw_val) if raw_val else 0.0
        store_id_raw = data.get('store_id')
        store_id = int(store_id_raw) if store_id_raw else None
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    import math as _math
    from db.orcamento import get_valid_line_keys
    if not (1 <= mes <= 12 and 2000 <= ano <= 2100):
        return jsonify({'ok': False, 'error': 'ano/mes inválido'}), 400
    if not line_key:
        return jsonify({'ok': False, 'error': 'line_key em branco'}), 400
    if not _math.isfinite(valor) or valor < 0:
        return jsonify({'ok': False, 'error': 'valor inválido (deve ser número finito ≥ 0)'}), 400
    if line_key not in get_valid_line_keys():
        return jsonify({'ok': False, 'error': f'line_key desconhecida: {line_key}'}), 400

    upsert_orcamento(ano, mes, store_id, line_key, valor)
    return jsonify({'ok': True})


@financeiro_bp.route('/orcamento/import', methods=['POST'])
@perm_required('acesso_financeiro')
def orcamento_import():
    import csv
    import io
    from db.orcamento import bulk_upsert_orcamento
    from db.stores import get_store_id_by_name

    f = request.files.get('csv_file')
    if not f:
        flash('Nenhum ficheiro enviado.', 'warning')
        return redirect(url_for('financeiro.orcamento'))

    try:
        content = f.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        rows = []
        errors = []
        import math as _math
        from db.orcamento import get_valid_line_keys
        valid_lks = get_valid_line_keys()  # fetch once outside the loop
        for i, row in enumerate(reader, start=2):
            try:
                ano_v = int(row['ano'])
                mes_v = int(row['mes'])
                loja  = (row.get('loja') or '').strip()
                lk    = (row.get('linha') or '').strip()
                valor_str = str(row.get('valor', 0)).replace(',', '.').strip()
                valor_v = float(valor_str) if valor_str else 0.0
            except Exception as exc:
                errors.append(f'Linha {i}: erro de conversão — {exc}')
                continue

            # Per-row range validation (before bulk persistence)
            if not (2000 <= ano_v <= 2100):
                errors.append(f'Linha {i}: ano inválido ({ano_v})')
                continue
            if not (1 <= mes_v <= 12):
                errors.append(f'Linha {i}: mês inválido ({mes_v})')
                continue
            if not _math.isfinite(valor_v) or valor_v < 0:
                errors.append(f'Linha {i}: valor inválido ("{row.get("valor")}")')
                continue
            if not lk:
                errors.append(f'Linha {i}: coluna "linha" em branco')
                continue
            if lk not in valid_lks:
                errors.append(
                    f'Linha {i}: line_key desconhecida "{lk}" '
                    f'(use vendas, cmvmc ou cat_<id>)'
                )
                continue

            sid = None
            if loja:
                sid = get_store_id_by_name(loja)
                if sid is None:
                    errors.append(f'Linha {i}: loja "{loja}" não encontrada')
                    continue

            rows.append({'ano': ano_v, 'mes': mes_v, 'store_id': sid,
                         'line_key': lk, 'valor_euros': valor_v})

        count = bulk_upsert_orcamento(rows)
        msg = f'{count} linha(s) importada(s) com sucesso.'
        if errors:
            msg += f' {len(errors)} erro(s): ' + '; '.join(errors[:5])
            flash(msg, 'warning')
        else:
            flash(msg, 'success')
    except Exception as exc:
        flash(f'Erro ao processar CSV: {exc}', 'danger')

    ano_arg = request.form.get('ano', str(date.today().year))
    sid_arg  = request.form.get('store_id', '')
    return redirect(url_for('financeiro.orcamento',
                            ano=ano_arg,
                            store_id=sid_arg or ''))


@financeiro_bp.route('/clientes-b2b', methods=['GET', 'POST'])
@perm_required('acesso_financeiro')
def clientes_b2b():
    from db.clientes_b2b import list_clientes, update_cliente
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update':
            cliente_id = int(request.form.get('cliente_id', 0))
            tipo = request.form.get('tipo', 'b2b')
            incluir_mapas = request.form.get('incluir_mapas') == '1'
            if tipo not in ('b2b', 'eventos'):
                tipo = 'b2b'
            update_cliente(cliente_id, tipo=tipo, incluir_mapas=incluir_mapas)
            flash('Cliente atualizado.', 'success')
        return redirect(url_for('financeiro.clientes_b2b'))
    tipo_filter = request.args.get('tipo', '')
    clientes = list_clientes(tipo=tipo_filter if tipo_filter else None)
    return render_template(
        'financeiro/clientes_b2b.html',
        clientes=clientes,
        tipo_filter=tipo_filter,
    )


@financeiro_bp.route('/faturas-clientes/<int:fatura_id>/status', methods=['POST'])
@perm_required('acesso_financeiro')
def faturas_clientes_set_status(fatura_id):
    from db.faturas_clientes import update_status
    status = request.form.get('status', '').strip()
    if status not in ('pendente', 'pago', 'vencido'):
        flash('Estado inválido.', 'error')
        return redirect(request.referrer or url_for('financeiro.faturas_clientes'))

    data_pagamento = None
    data_pagamento_str = request.form.get('data_pagamento', '').strip()
    if status == 'pago' and data_pagamento_str:
        try:
            data_pagamento = datetime.strptime(data_pagamento_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Data de pagamento inválida.', 'error')
            return redirect(request.referrer or url_for('financeiro.faturas_clientes'))

    found = update_status(fatura_id, status, data_pagamento=data_pagamento)
    if not found:
        flash('Fatura não encontrada ou anulada.', 'error')
    return redirect(request.referrer or url_for('financeiro.faturas_clientes'))


@financeiro_bp.route('/faturas-clientes', methods=['GET'])
@perm_required('acesso_financeiro')
def faturas_clientes():
    from db.faturas_clientes import list_faturas, count_faturas, get_summary_totals
    page = max(1, int(request.args.get('page', 1)))
    per_page = 50
    offset = (page - 1) * per_page
    cliente_id_str = request.args.get('cliente_id', '')
    data_inicio_str = request.args.get('data_inicio', '')
    data_fim_str = request.args.get('data_fim', '')
    incluir_anuladas = request.args.get('incluir_anuladas') == '1'

    cliente_id = int(cliente_id_str) if cliente_id_str.isdigit() else None
    data_inicio = None
    data_fim = None
    try:
        if data_inicio_str:
            data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        if data_fim_str:
            data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
    except ValueError:
        pass

    total = count_faturas(
        cliente_id=cliente_id, data_inicio=data_inicio, data_fim=data_fim,
        incluir_anuladas=incluir_anuladas,
    )
    faturas = list_faturas(
        cliente_id=cliente_id, data_inicio=data_inicio, data_fim=data_fim,
        incluir_anuladas=incluir_anuladas, limit=per_page, offset=offset,
    )
    summary = get_summary_totals(
        cliente_id=cliente_id, data_inicio=data_inicio, data_fim=data_fim,
    )
    from db.clientes_b2b import list_clientes
    clientes = list_clientes()
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        'financeiro/faturas_clientes.html',
        faturas=faturas,
        clientes=clientes,
        page=page,
        total_pages=total_pages,
        total=total,
        cliente_id=cliente_id,
        data_inicio=data_inicio_str,
        data_fim=data_fim_str,
        incluir_anuladas=incluir_anuladas,
        summary=summary,
    )
