"""Mapa de Exploração — 12-month P&L aggregation.

Provides monthly sales, CMVMC, and cost-category data for a full calendar year,
merged with budget (orcamento) and prior-year figures.  Supports both a
per-store view (store_id given) and a consolidated view (store_id=None).

CMVMC detection: any cost_category whose name contains "matéria" or "cmvmc"
(case-insensitive) is classified as CMVMC and shown in the CMVMC row rather
than in the Custos de Operação section (avoiding double-counting).
"""

import logging
from datetime import date
from db.connection import db_connection

logger = logging.getLogger(__name__)

_CMVMC_PATTERNS = ('matéria', 'materia', 'cmvmc')


def _is_cmvmc(cat_name: str) -> bool:
    name_lower = (cat_name or '').lower()
    return any(p in name_lower for p in _CMVMC_PATTERNS)


def get_mapa_exploracao(ano: int, store_id=None) -> dict:
    """Return all data needed to render the 12-month Mapa de Exploração.

    Args:
        ano:      Calendar year (e.g. 2026).
        store_id: Active store PK, or None for the consolidated view.

    Returns a dict documented below.  All monetary values are floats rounded
    to 2 decimal places.  Month keys are ints 1-12.  Missing months mean €0.
    """
    from db.stores import get_all_stores
    from db.centros_custo import get_cost_categories, get_all_allocations, get_sales_split_pct
    from db.orcamento import get_orcamento_all_stores

    today = date.today()
    ano_aa = ano - 1
    active_stores = [s for s in get_all_stores() if s['is_active']]
    store_ids = [s['id'] for s in active_stores]
    all_cats = [dict(c) for c in get_cost_categories(ativo_only=True)]

    # Split categories: CMVMC vs operating costs
    cmvmc_cat_ids = {c['id'] for c in all_cats if _is_cmvmc(c['name'])}
    categories = [c for c in all_cats if c['id'] not in cmvmc_cat_ids]

    allocations = get_all_allocations()
    sales_split = get_sales_split_pct(months=12)
    budget_all = get_orcamento_all_stores(ano)

    # ── Raw DB queries ─────────────────────────────────────────────────────

    def _query_inv(year):
        """→ [(mes, cat_id, total_eur), ...]"""
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT EXTRACT(MONTH FROM i.issue_date)::int,
                       i.categoria_custo_id,
                       COALESCE(SUM(i.amount_eur), 0)
                FROM invoices i
                WHERE EXTRACT(YEAR FROM i.issue_date) = %s
                  AND i.status != 'cancelled'
                GROUP BY 1, 2
            """, (year,))
            return cur.fetchall()

    def _query_vendas(year):
        """→ [(mes, store_id, total_eur), ...]"""
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT EXTRACT(MONTH FROM vd.data)::int,
                       s.id,
                       COALESCE(SUM(vd.valor_euros), 0)
                FROM vendas_detalhe vd
                JOIN stores s ON LOWER(vd.loja) = LOWER(s.name)
                WHERE EXTRACT(YEAR FROM vd.data) = %s
                  AND s.is_active = TRUE
                  AND vd.valor_euros IS NOT NULL
                GROUP BY 1, 2
            """, (year,))
            return cur.fetchall()

    inv_rows    = _query_inv(ano)
    inv_aa_rows = _query_inv(ano_aa)
    v_rows      = _query_vendas(ano)
    v_aa_rows   = _query_vendas(ano_aa)

    # ── Structure raw rows ─────────────────────────────────────────────────

    def _struct_inv(rows):
        """{cat_id: {mes: float}}"""
        d: dict = {}
        for mes, cat_id, total in rows:
            d.setdefault(cat_id, {})[int(mes)] = float(total)
        return d

    def _struct_vendas(rows):
        """{store_id: {mes: float}}"""
        d: dict = {}
        for mes, sid, total in rows:
            d.setdefault(int(sid), {})[int(mes)] = float(total)
        return d

    inv      = _struct_inv(inv_rows)
    inv_aa   = _struct_inv(inv_aa_rows)
    sv       = _struct_vendas(v_rows)
    sv_aa    = _struct_vendas(v_aa_rows)

    # ── Global (all-store) vendas ──────────────────────────────────────────

    def _global_vendas(sv_dict):
        g: dict = {}
        for sid in store_ids:
            for m, v in sv_dict.get(sid, {}).items():
                g[m] = round(g.get(m, 0.0) + v, 2)
        return g

    global_vendas    = _global_vendas(sv)
    global_vendas_aa = _global_vendas(sv_aa)

    # Global CMVMC (sum of matching categories, no per-store allocation needed
    # for the global total)
    def _global_cmvmc(inv_dict):
        g: dict = {}
        for cid in cmvmc_cat_ids:
            for m, v in inv_dict.get(cid, {}).items():
                g[m] = round(g.get(m, 0.0) + v, 2)
        return g

    global_cmvmc    = _global_cmvmc(inv)
    global_cmvmc_aa = _global_cmvmc(inv_aa)

    # ── Allocation helpers ─────────────────────────────────────────────────

    def _pct(cat_id, sid):
        alloc = allocations.get(cat_id, {'modo': 'volume_vendas', 'stores': {}})
        modo  = alloc['modo']
        if modo == 'volume_vendas':
            return sales_split.get(sid, 0.0)
        if modo == 'igualitario':
            return round(100.0 / len(store_ids), 4) if store_ids else 0.0
        return float(alloc['stores'].get(sid, 0.0))   # tudo_loja / manual

    def _apply_pct(inv_dict, cat_id, sid):
        """{mes: float} for one category and one store."""
        p = _pct(cat_id, sid)
        return {m: round(v * p / 100.0, 2) for m, v in inv_dict.get(cat_id, {}).items()}

    # ── Budget merge ───────────────────────────────────────────────────────

    def _bgt(sid):
        """Return the store-specific budget only — no global fallback.

        Under the new semantics, Global = Σ stores.  Merging the aggregate
        global back into each store's budget would inflate the consolidated
        total whenever a store has missing months.  If a store has no budget
        set it contributes zero, which is the correct and visible behaviour.
        """
        return budget_all.get(sid, {}) if sid is not None else {}

    # ─────────────────────────────────────────────────────────────────────
    # Per-store view
    # ─────────────────────────────────────────────────────────────────────
    # ── Uncategorized invoices: cat_id IS NULL ────────────────────────────
    # Cannot be allocated to a specific store (no category → no allocation config).
    # Included in Total Custos / EBITDA as a "Sem categoria" line.
    costs_uncat    = {m: round(v, 2) for m, v in inv.get(None, {}).items()}
    costs_uncat_aa = {m: round(v, 2) for m, v in inv_aa.get(None, {}).items()}

    if store_id is not None:
        sid = store_id
        store_obj = next((s for s in active_stores if s['id'] == sid), None)
        bgt = _bgt(sid)

        vendas    = {m: round(v, 2) for m, v in sv.get(sid, {}).items()}
        vendas_aa = {m: round(v, 2) for m, v in sv_aa.get(sid, {}).items()}

        # CMVMC for this store: sum of allocated CMVMC-category invoices
        cmvmc: dict = {}
        cmvmc_aa: dict = {}
        for cid in cmvmc_cat_ids:
            for m, v in _apply_pct(inv, cid, sid).items():
                cmvmc[m] = round(cmvmc.get(m, 0.0) + v, 2)
            for m, v in _apply_pct(inv_aa, cid, sid).items():
                cmvmc_aa[m] = round(cmvmc_aa.get(m, 0.0) + v, 2)

        # Operating costs per category (non-CMVMC)
        costs    = {cat['id']: _apply_pct(inv,    cat['id'], sid) for cat in categories}
        costs_aa = {cat['id']: _apply_pct(inv_aa, cat['id'], sid) for cat in categories}

        budget_costs = {cat['id']: bgt.get(f'cat_{cat["id"]}', {}) for cat in categories}

        return {
            'mode': 'store',
            'ano': ano, 'ano_aa': ano_aa, 'today': today,
            'store': store_obj, 'stores': active_stores,
            'categories': categories, 'cmvmc_cat_ids': list(cmvmc_cat_ids),
            'vendas': vendas, 'vendas_aa': vendas_aa,
            'cmvmc': cmvmc, 'cmvmc_aa': cmvmc_aa,
            'costs': costs, 'costs_aa': costs_aa,
            'costs_uncat': costs_uncat, 'costs_uncat_aa': costs_uncat_aa,
            'budget_vendas': bgt.get('vendas', {}),
            'budget_cmvmc':  bgt.get('cmvmc', {}),
            'budget_costs':  budget_costs,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Consolidated view
    # ─────────────────────────────────────────────────────────────────────
    gbgt = budget_all.get(None, {})

    global_costs    = {cat['id']: dict(inv.get(cat['id'], {}))    for cat in categories}
    global_costs_aa = {cat['id']: dict(inv_aa.get(cat['id'], {})) for cat in categories}

    # Per-store breakdowns for consolidated sub-rows
    store_cmvmc: dict = {}
    store_cmvmc_aa: dict = {}
    store_costs: dict = {}
    store_costs_aa: dict = {}
    for sid in store_ids:
        sc: dict = {}
        sc_aa: dict = {}
        for cid in cmvmc_cat_ids:
            for m, v in _apply_pct(inv, cid, sid).items():
                sc[m] = round(sc.get(m, 0.0) + v, 2)
            for m, v in _apply_pct(inv_aa, cid, sid).items():
                sc_aa[m] = round(sc_aa.get(m, 0.0) + v, 2)
        store_cmvmc[sid]    = sc
        store_cmvmc_aa[sid] = sc_aa
        store_costs[sid]    = {cat['id']: _apply_pct(inv,    cat['id'], sid) for cat in categories}
        store_costs_aa[sid] = {cat['id']: _apply_pct(inv_aa, cat['id'], sid) for cat in categories}

    # ── Unallocated residuals: global total − Σ per-store allocations ──────
    # Exposed explicitly so the template shows a "Não alocado" row and the
    # user can see that consolidated totals = Σ stores + unallocated remainder.
    # A non-zero residual occurs when allocation percentages do not reach 100 %
    # (e.g. volume_vendas with zero sales history, manual allocations < 100 %).
    unallocated_cmvmc: dict = {}
    for m, total in global_cmvmc.items():
        alloc = sum(store_cmvmc[sid].get(m, 0) for sid in store_ids)
        res = round(total - alloc, 2)
        if abs(res) > 0.005:
            unallocated_cmvmc[m] = res

    unallocated_costs: dict = {}
    for cat in categories:
        cid = cat['id']
        cat_unalloc: dict = {}
        for m, total in global_costs.get(cid, {}).items():
            alloc = sum(store_costs[sid][cid].get(m, 0) for sid in store_ids)
            res = round(total - alloc, 2)
            if abs(res) > 0.005:
                cat_unalloc[m] = res
        unallocated_costs[cid] = cat_unalloc   # empty dict if fully allocated

    # ── Consolidated budget ───────────────────────────────────────────────────
    # budget_all[None] is already the month-by-month sum of all active stores,
    # computed by get_orcamento_all_stores().  Use it directly; do NOT merge
    # the aggregate back into individual store rows, which would double-count
    # whenever stores have missing months.
    cons_bgt = budget_all.get(None, {})

    return {
        'mode': 'consolidated',
        'ano': ano, 'ano_aa': ano_aa, 'today': today,
        'store': None, 'stores': active_stores,
        'categories': categories, 'cmvmc_cat_ids': list(cmvmc_cat_ids),
        'vendas': global_vendas, 'vendas_aa': global_vendas_aa,
        'cmvmc': global_cmvmc, 'cmvmc_aa': global_cmvmc_aa,
        'costs': global_costs, 'costs_aa': global_costs_aa,
        'budget_vendas': cons_bgt.get('vendas', {}),
        'budget_cmvmc':  cons_bgt.get('cmvmc', {}),
        'budget_costs':  {cat['id']: cons_bgt.get(f'cat_{cat["id"]}', {}) for cat in categories},
        # Sub-row breakdowns (per store, by allocation)
        'store_vendas':    {sid: {m: round(v,2) for m, v in sv.get(sid,{}).items()} for sid in store_ids},
        'store_vendas_aa': {sid: {m: round(v,2) for m, v in sv_aa.get(sid,{}).items()} for sid in store_ids},
        'store_cmvmc':     store_cmvmc,
        'store_cmvmc_aa':  store_cmvmc_aa,
        'store_costs':     store_costs,
        'store_costs_aa':  store_costs_aa,
        # Unallocated residuals (global − Σ stores); empty when fully allocated
        'unallocated_cmvmc': unallocated_cmvmc,
        'unallocated_costs': unallocated_costs,
        # Invoices with no cost category — cannot be allocated per-store
        'costs_uncat': costs_uncat, 'costs_uncat_aa': costs_uncat_aa,
    }
