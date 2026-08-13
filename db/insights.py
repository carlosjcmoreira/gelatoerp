"""Financial Insights — KPIs, anomaly detection, budget deviation, and chart data.

Wraps get_mapa_exploracao() and derives higher-level signals needed by the
Insights Dashboard (/financeiro/insights).  All monetary values are rounded
to 2 decimal places; percentages are floats (e.g. 12.5 means 12.5 %).
"""
from datetime import date

MESES_PT_SHORT = ['', 'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                  'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
MESES_PT_LONG  = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                  'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']


def _detect_anomalies(cat, cat_d, cat_orc, current_m, store_name, store_id, out):
    """Append MoM and budget-breach anomaly dicts for one category × store to *out*."""
    for m in range(1, current_m + 1):
        curr = cat_d.get(m, 0)
        # MoM spike: >20 % increase over prior month
        if m > 1:
            prev = cat_d.get(m - 1, 0)
            if prev > 0 and curr > prev * 1.20:
                overshoot = round(curr - prev, 2)
                out.append({
                    'kind':       'mom',
                    'cat_name':   cat['name'],
                    'month':      m,
                    'month_lbl':  MESES_PT_SHORT[m],
                    'curr':       round(curr, 2),
                    'ref':        round(prev, 2),
                    'pct':        round((curr - prev) / prev * 100, 1),
                    'overshoot':  overshoot,
                    'desc':       f'+{overshoot:,.0f} € vs {MESES_PT_SHORT[m-1]}',
                    'store_name': store_name,
                    'store_id':   store_id,
                })
        # Budget breach: >15 % over the month's budget
        bgt_m = cat_orc.get(m)
        if bgt_m and bgt_m > 0 and curr > bgt_m * 1.15:
            overshoot = round(curr - bgt_m, 2)
            out.append({
                'kind':       'budget',
                'cat_name':   cat['name'],
                'month':      m,
                'month_lbl':  MESES_PT_SHORT[m],
                'curr':       round(curr, 2),
                'ref':        round(bgt_m, 2),
                'pct':        round((curr - bgt_m) / bgt_m * 100, 1),
                'overshoot':  overshoot,
                'desc':       f'+{overshoot:,.0f} € vs orçamento',
                'store_name': store_name,
                'store_id':   store_id,
            })


def get_financial_insights(ano: int, store_id=None) -> dict:
    """Return all data needed by the Financial Insights Dashboard.

    Args:
        ano:      Calendar year (e.g. 2026).
        store_id: Active store PK, or None for the consolidated view.

    Returns a flat dict documented inline.
    """
    from db.mapa_exploracao import get_mapa_exploracao

    today     = date.today()
    mapa      = get_mapa_exploracao(ano, store_id=store_id)
    months    = list(range(1, 13))
    current_m = today.month if ano == today.year else 12
    ytd_ms    = list(range(1, current_m + 1))

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _ytd(d):
        return round(sum(d.get(m, 0) for m in ytd_ms), 2)

    # Total operating costs per month (including uncategorised)
    costs_monthly: dict = {}
    for cat in mapa['categories']:
        for m, v in mapa['costs'].get(cat['id'], {}).items():
            costs_monthly[m] = round(costs_monthly.get(m, 0) + v, 2)
    for m, v in mapa.get('costs_uncat', {}).items():
        costs_monthly[m] = round(costs_monthly.get(m, 0) + v, 2)

    # Total budgeted operating costs per month
    budget_costs_monthly: dict = {}
    for cat in mapa['categories']:
        for m, v in mapa['budget_costs'].get(cat['id'], {}).items():
            budget_costs_monthly[m] = round(budget_costs_monthly.get(m, 0) + v, 2)

    # ── KPI 1: YTD Revenue vs Budget ─────────────────────────────────────────
    ytd_vendas        = _ytd(mapa['vendas'])
    ytd_budget_vendas = _ytd(mapa['budget_vendas'])
    ytd_vendas_aa     = _ytd(mapa['vendas_aa'])
    kpi_rev_vs_bgt     = (ytd_vendas - ytd_budget_vendas) / ytd_budget_vendas * 100 \
                         if ytd_budget_vendas else None
    kpi_rev_vs_bgt_eur = round(ytd_vendas - ytd_budget_vendas, 2)
    kpi_rev_vs_aa      = (ytd_vendas - ytd_vendas_aa) / ytd_vendas_aa * 100 \
                         if ytd_vendas_aa else None

    # ── KPI 2: YTD EBITDA ────────────────────────────────────────────────────
    ytd_cmvmc  = _ytd(mapa['cmvmc'])
    ytd_mb     = round(ytd_vendas - ytd_cmvmc, 2)
    ytd_costs  = round(sum(costs_monthly.get(m, 0) for m in ytd_ms), 2)
    ytd_ebitda = round(ytd_mb - ytd_costs, 2)
    ytd_budget_ebitda = round(
        ytd_budget_vendas
        - _ytd(mapa['budget_cmvmc'])
        - sum(budget_costs_monthly.get(m, 0) for m in ytd_ms),
        2
    )
    kpi_ebitda_vs_bgt     = (ytd_ebitda - ytd_budget_ebitda) / abs(ytd_budget_ebitda) * 100 \
                            if ytd_budget_ebitda else None
    kpi_ebitda_vs_bgt_eur = round(ytd_ebitda - ytd_budget_ebitda, 2)
    ebitda_pct_vendas     = ytd_ebitda / ytd_vendas * 100 if ytd_vendas else None

    # ── KPI 3: Top cost category YTD as % of revenue ─────────────────────────
    cat_ytd  = {cat['id']: _ytd(mapa['costs'].get(cat['id'], {}))
                for cat in mapa['categories']}
    top_cat_id   = max(cat_ytd, key=cat_ytd.get) if cat_ytd else None
    top_cat_name = next((c['name'] for c in mapa['categories'] if c['id'] == top_cat_id), None)
    top_cat_eur  = cat_ytd.get(top_cat_id, 0)
    top_cat_pct  = top_cat_eur / ytd_vendas * 100 if ytd_vendas and top_cat_id else None

    # ── KPI 4: Biggest absolute MoM cost increase ─────────────────────────────
    max_mom_delta = 0.0
    max_mom_cat   = None
    max_mom_month = None
    for cat in mapa['categories']:
        cat_d = mapa['costs'].get(cat['id'], {})
        for m in range(2, current_m + 1):
            prev = cat_d.get(m - 1, 0)
            curr = cat_d.get(m, 0)
            delta = curr - prev
            if delta > max_mom_delta:
                max_mom_delta = delta
                max_mom_cat   = cat['name']
                max_mom_month = m

    # ── Budget deviation table ────────────────────────────────────────────────
    deviations = []
    for cat in mapa['categories']:
        cat_d   = mapa['costs'].get(cat['id'], {})
        cat_orc = mapa['budget_costs'].get(cat['id'], {})
        ytd_act = _ytd(cat_d)
        ytd_bgt = _ytd(cat_orc)
        if ytd_bgt > 0:
            dev_pct = (ytd_act - ytd_bgt) / ytd_bgt * 100
            dev_eur = ytd_act - ytd_bgt
            if dev_pct > 15:
                status = 'danger'
            elif dev_pct > 5:
                status = 'warning'
            elif dev_pct < -10:
                status = 'success'
            else:
                status = 'light'
            deviations.append({
                'name':       cat['name'],
                'ytd_actual': round(ytd_act, 2),
                'ytd_budget': round(ytd_bgt, 2),
                'dev_pct':    round(dev_pct, 1),
                'dev_eur':    round(dev_eur, 2),
                'status':     status,
            })
    deviations.sort(key=lambda x: x['dev_pct'], reverse=True)

    # ── Anomaly detection ─────────────────────────────────────────────────────
    # In consolidated mode, anomalies are computed PER STORE so that a spike
    # at one store hidden by another store's decrease is not missed, and each
    # alert carries an explicit store name / Mapa link.
    # In per-store mode, the single store is used.
    #
    # Checks:
    #   a) MoM spike: curr > prev × 1.20  (>20 % month-on-month increase)
    #   b) Budget breach: curr > budget × 1.15 (>15 % over the month's budget)
    #
    # Budget for each store in consolidated mode: store-specific value if set,
    # else global budget (same effective-budget logic as _bgt() in mapa_exploracao).

    def _build_per_store_budgets():
        """Return {store_id: {cat_id: {month: float}}} using per-store budgets only.

        No global fallback: under the new semantics Global = Σ stores, so
        merging the aggregate back into each store's budget would inflate
        anomaly thresholds for stores that have missing months.
        """
        from db.orcamento import get_orcamento_all_stores
        budget_all = get_orcamento_all_stores(ano)
        result = {}
        for s in mapa['stores']:
            sid = s['id']
            s_bgt = budget_all.get(sid, {})
            store_cat_bgt: dict = {}
            for cat in mapa['categories']:
                lk = f'cat_{cat["id"]}'
                store_cat_bgt[cat['id']] = dict(s_bgt.get(lk, {}))
            result[sid] = store_cat_bgt
        return result

    raw_anomalies: list = []

    if mapa['mode'] == 'consolidated':
        per_store_budgets = _build_per_store_budgets()
        for s in mapa['stores']:
            sid        = s['id']
            store_name = s['name']
            for cat in mapa['categories']:
                cat_d   = mapa['store_costs'].get(sid, {}).get(cat['id'], {})
                cat_orc = per_store_budgets.get(sid, {}).get(cat['id'], {})
                _detect_anomalies(cat, cat_d, cat_orc, current_m, store_name, sid, raw_anomalies)
    else:
        # Per-store view: single store already selected
        store_name = mapa['store']['name'] if mapa['store'] else 'Consolidado'
        store_sid  = mapa['store']['id']   if mapa['store'] else None
        for cat in mapa['categories']:
            cat_d   = mapa['costs'].get(cat['id'], {})
            cat_orc = mapa['budget_costs'].get(cat['id'], {})
            _detect_anomalies(cat, cat_d, cat_orc, current_m, store_name, store_sid, raw_anomalies)

    # De-duplicate by (store_id, cat_name, month): combine both trigger kinds
    by_key: dict = {}
    for a in raw_anomalies:
        key = (a.get('store_id'), a['cat_name'], a['month'])
        if key not in by_key:
            by_key[key] = dict(a)
        else:
            existing = by_key[key]
            if a['overshoot'] > existing['overshoot']:
                by_key[key] = dict(a)
                by_key[key]['kind'] = 'both'
                by_key[key]['desc'] = existing['desc'] + ' · ' + a['desc']
            else:
                existing['kind'] = 'both'
                existing['desc'] = existing['desc'] + ' · ' + a['desc']
    anomalies = sorted(by_key.values(), key=lambda x: x['overshoot'], reverse=True)[:10]

    # ── Chart data (JSON-serialisable) ───────────────────────────────────────
    chart_labels = [MESES_PT_SHORT[m] for m in months]

    vendas_series  = [round(mapa['vendas'].get(m, 0), 2)         for m in months]
    vendas_aa_s    = [round(mapa['vendas_aa'].get(m, 0), 2)       for m in months]
    bgt_vendas_s   = [round(mapa['budget_vendas'].get(m, 0), 0)   for m in months]

    # Cost trend series: one per category (non-zero only)
    cost_series = []
    for cat in mapa['categories']:
        cat_d = mapa['costs'].get(cat['id'], {})
        vals  = [round(cat_d.get(m, 0), 2) for m in months]
        if any(v > 0 for v in vals):
            cost_series.append({'name': cat['name'], 'data': vals})
    # Uncategorised
    uncat_vals = [round(mapa.get('costs_uncat', {}).get(m, 0), 2) for m in months]
    if any(v > 0 for v in uncat_vals):
        cost_series.append({'name': 'Sem categoria ⚠️', 'data': uncat_vals})

    total_costs_s = [round(costs_monthly.get(m, 0), 2) for m in months]

    return {
        'mapa':         mapa,
        'today':        today,
        'ano':          ano,
        'store_id':     store_id,
        'current_m':    current_m,
        # KPIs
        'ytd_vendas':           ytd_vendas,
        'ytd_budget_vendas':    ytd_budget_vendas,
        'ytd_vendas_aa':        ytd_vendas_aa,
        'kpi_rev_vs_bgt':       kpi_rev_vs_bgt,
        'kpi_rev_vs_bgt_eur':   kpi_rev_vs_bgt_eur,
        'kpi_rev_vs_aa':        kpi_rev_vs_aa,
        'ytd_ebitda':           ytd_ebitda,
        'ytd_budget_ebitda':    ytd_budget_ebitda,
        'kpi_ebitda_vs_bgt':    kpi_ebitda_vs_bgt,
        'kpi_ebitda_vs_bgt_eur': kpi_ebitda_vs_bgt_eur,
        'ebitda_pct_vendas':    ebitda_pct_vendas,
        'top_cat_name':         top_cat_name,
        'top_cat_pct':          top_cat_pct,
        'top_cat_eur':          top_cat_eur,
        'max_mom_delta':        round(max_mom_delta, 2),
        'max_mom_cat':          max_mom_cat,
        'max_mom_month':        max_mom_month,
        'max_mom_month_lbl':    MESES_PT_SHORT[max_mom_month] if max_mom_month else None,
        # Tables
        'deviations':   deviations,
        'anomalies':    anomalies,
        # Chart series (JSON-safe)
        'chart_labels':   chart_labels,
        'vendas_series':  vendas_series,
        'vendas_aa_s':    vendas_aa_s,
        'bgt_vendas_s':   bgt_vendas_s,
        'cost_series':    cost_series,
        'total_costs_s':  total_costs_s,
    }
