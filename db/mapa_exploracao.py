"""Mapa de Exploração — 12-month P&L aggregation.

Provides monthly sales, CMVMC, and cost-category data for a full calendar year,
merged with budget (orcamento) and prior-year figures.  Supports both a
per-store view (store_id given) and a consolidated view (store_id=None).

Cost allocation is driven by the Centro de Custo assigned to each invoice:
  - CCs whose name matches an active store (e.g. "Matosinhos", "Bolhão")
    → 100 % attributed to that store's P&L
  - All other CCs (Produção, Geral, Faturas Partilhadas, etc.)
    → distributed across stores in proportion to each store's share of sales
      revenue over the last 12 months (via get_sales_split_pct)
  - Invoices with no CC assigned at all → not attributed to any store;
    shown as "Não alocado" in the consolidated view
  - Invoices with no cost category (cat_id IS NULL) → global "Sem categoria"
    line, independent of CC

Junction-table semantics (`invoice_centros_custo`):
  - An invoice split 60 % (CC-M) / 40 % (CC-P) produces two allocation slices.
  - If junction rows sum to less than 100 %, the remainder is treated with
    the invoice's single-FK centro_custo_id (which may be NULL → unallocated).
  - This guarantees Σ allocated + unallocated = invoice.amount_eur for every
    invoice; no euro ever disappears.

Zero-sales-split guard:
  - If get_sales_split_pct() returns no history (e.g. brand-new data set) the
    distribution of a shared-CC invoice would produce all-zero store amounts.
    In that case the full slice is treated as unallocated rather than silently
    dropped.

CMVMC detection:
  - Cost categories with `is_cmvmc = TRUE` are classified as CMVMC and shown
    in the CMVMC row; all others go to Custos de Operação (no double-counting).
"""

import logging
from datetime import date
from db.connection import db_connection

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_invoice_rows(year):
    """→ [(id, mes, cat_id, legacy_cc_id, amount_eur), ...]"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT i.id,
                   EXTRACT(MONTH FROM i.issue_date)::int,
                   i.categoria_custo_id,
                   i.centro_custo_id,
                   COALESCE(i.amount_eur, 0)
            FROM invoices i
            WHERE EXTRACT(YEAR FROM i.issue_date) = %s
              AND i.status != 'cancelled'
        """, (year,))
        return cur.fetchall()


def _load_junction_rows(invoice_ids):
    """→ {invoice_id: [(cc_id, percentagem), ...]}"""
    if not invoice_ids:
        return {}
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT invoice_id, centro_custo_id, percentagem
            FROM invoice_centros_custo
            WHERE invoice_id = ANY(%s)
        """, (list(invoice_ids),))
        idx: dict = {}
        for inv_id, cc_id, pct in cur.fetchall():
            idx.setdefault(inv_id, []).append((cc_id, float(pct)))
        return idx


def _resolve_cc_slices(invoice_rows, junction_idx):
    """Expand invoice rows into CC-tagged cost slices.

    For invoices with junction rows the amount is split according to the
    stored percentages.  Any unallocated remainder (total pct < 100) falls
    back to the invoice's legacy single-FK CC (which may be NULL).

    This ensures conservation: the sum of all returned slice amounts for a
    given invoice always equals invoice.amount_eur.

    Returns [(inv_id, mes, cat_id, cc_id, amount), ...]  — cc_id may be None.
    inv_id is included so downstream processors can track which invoice each
    slice came from (e.g. to count how many invoices produced a given outcome).
    """
    slices = []
    for inv_id, mes, cat_id, legacy_cc_id, amount_eur in invoice_rows:
        mes = int(mes)
        amount = float(amount_eur)
        junc = junction_idx.get(inv_id)

        if junc:
            total_pct = sum(pct for _, pct in junc)
            for cc_id, pct in junc:
                slices.append((inv_id, mes, cat_id, cc_id, amount * pct / 100.0))
            remainder = amount * (100.0 - total_pct) / 100.0
            if abs(remainder) > 0.001:
                # Remainder attributed to the legacy single-FK CC (or NULL)
                slices.append((inv_id, mes, cat_id, legacy_cc_id, remainder))
        else:
            # No junction rows → full amount on legacy FK (or NULL)
            slices.append((inv_id, mes, cat_id, legacy_cc_id, amount))

    return slices


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def get_mapa_exploracao(ano: int, store_id=None) -> dict:
    """Return all data needed to render the 12-month Mapa de Exploração.

    Args:
        ano:      Calendar year (e.g. 2026).
        store_id: Active store PK, or None for the consolidated view.

    All monetary values are floats rounded to 2 dp.
    Month keys are ints 1-12; missing months mean €0.
    """
    from db.stores import get_all_stores
    from db.centros_custo import (
        get_cost_categories, get_sales_split_pct, get_cost_centers,
        get_pessoal_costs_by_cc,
    )
    from db.orcamento import get_orcamento_all_stores

    today = date.today()
    ano_aa = ano - 1
    active_stores = [s for s in get_all_stores() if s['is_active']]
    store_ids = [s['id'] for s in active_stores]
    all_cats = [dict(c) for c in get_cost_categories(ativo_only=True)]

    cmvmc_cat_ids = {c['id'] for c in all_cats if c.get('is_cmvmc')}
    categories = [c for c in all_cats if c['id'] not in cmvmc_cat_ids]

    sales_split = get_sales_split_pct(months=12)
    budget_all = get_orcamento_all_stores(ano)

    # ── CC → store mapping (uses store_id FK; falls back to name-match for legacy) ─
    all_cost_centers = get_cost_centers(ativo_only=False)
    store_name_lower_to_id = {s['name'].lower(): s['id'] for s in active_stores}
    cc_to_store: dict = {}
    for cc in all_cost_centers:
        # Prefer the explicit FK link set by the manager
        sid = cc.get('store_id')
        if sid is not None and sid in {s['id'] for s in active_stores}:
            cc_to_store[cc['id']] = sid
        else:
            # Legacy fallback: name-based matching for CCs not yet assigned via FK
            matched_sid = store_name_lower_to_id.get((cc['name'] or '').lower())
            if matched_sid is not None:
                cc_to_store[cc['id']] = matched_sid

    # ── Load & resolve invoice cost slices ─────────────────────────────────

    def _get_slices(year):
        inv_rows = _load_invoice_rows(year)
        inv_ids = [r[0] for r in inv_rows]
        junc_idx = _load_junction_rows(inv_ids)
        return _resolve_cc_slices(inv_rows, junc_idx)

    inv_slices    = _get_slices(ano)
    inv_slices_aa = _get_slices(ano_aa)

    # ── Sales ───────────────────────────────────────────────────────────────

    def _query_vendas(year):
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

    def _struct_vendas(rows):
        d: dict = {}
        for mes, sid, total in rows:
            d.setdefault(int(sid), {})[int(mes)] = float(total)
        return d

    sv    = _struct_vendas(_query_vendas(ano))
    sv_aa = _struct_vendas(_query_vendas(ano_aa))

    def _global_vendas(sv_dict):
        g: dict = {}
        for sid in store_ids:
            for m, v in sv_dict.get(sid, {}).items():
                g[m] = round(g.get(m, 0.0) + v, 2)
        return g

    global_vendas    = _global_vendas(sv)
    global_vendas_aa = _global_vendas(sv_aa)

    # ── CC-based store distribution ─────────────────────────────────────────

    # True when at least one store has a non-zero sales share for distribution.
    has_sales_history = any(sales_split.values())

    def _dist(amount: float, cc_id):
        """Map one CC-tagged slice to ({store_id: amount}, shared_no_hist).

        store CC  → ({store_id: amount}, False)
        shared CC → proportional split; if no sales history → ({}, True)
        no CC     → ({}, False)

        The boolean distinguishes "truly no CC" from "shared CC but no
        sales history" so callers can surface the difference to users.
        """
        if cc_id is None:
            return {}, False
        target_sid = cc_to_store.get(cc_id)
        if target_sid is not None:
            return {target_sid: amount}, False
        # Shared CC: distribute by recent sales volume
        dist = {
            sid: round(amount * sales_split.get(sid, 0) / 100.0, 4)
            for sid in store_ids
        }
        # Guard: if all amounts are zero (no sales history), treat as unallocated
        if not any(dist.values()):
            return {}, True  # shared CC but no sales history to distribute by
        return dist, False

    def _process_slices(slices):
        """Accumulate slices into structured allocations.

        Returns:
            store_alloc              : {cat_id: {store_id: {mes: float}}}
            no_cc_by_cat             : {cat_id: {mes: float}}  — truly no CC (cc_id=None)
            shared_no_hist_by_cat    : {cat_id: {mes: float}}  — shared CC with no sales history
            costs_uncat              : {mes: float}            — cat_id=None invoices
            shared_no_hist_inv_ids   : set[int]               — invoice IDs behind shared_no_hist_by_cat
                                       (categorized slices only; excludes cat_id=None rows which
                                       go to costs_uncat rather than the "Não alocado" rows)
        """
        store_alloc: dict            = {}
        no_cc_by_cat: dict           = {}
        shared_no_hist_by_cat: dict  = {}
        costs_uncat: dict            = {}
        shared_no_hist_inv_ids: set  = set()

        for inv_id, mes, cat_id, cc_id, amount in slices:
            if cat_id is None:
                costs_uncat[mes] = round(costs_uncat.get(mes, 0.0) + amount, 4)
                continue

            dist, shared_no_hist = _dist(amount, cc_id)
            if not dist:
                if shared_no_hist:
                    d = shared_no_hist_by_cat.setdefault(cat_id, {})
                    shared_no_hist_inv_ids.add(inv_id)
                else:
                    d = no_cc_by_cat.setdefault(cat_id, {})
                d[mes] = round(d.get(mes, 0.0) + amount, 4)
            else:
                sa = store_alloc.setdefault(cat_id, {})
                for sid, v in dist.items():
                    ss = sa.setdefault(sid, {})
                    ss[mes] = round(ss.get(mes, 0.0) + v, 4)

        return store_alloc, no_cc_by_cat, shared_no_hist_by_cat, costs_uncat, shared_no_hist_inv_ids

    (store_alloc,    no_cc_inv,    shared_no_hist_inv,
     costs_uncat,    shared_no_hist_inv_ids)    = _process_slices(inv_slices)
    (store_alloc_aa, no_cc_inv_aa, shared_no_hist_inv_aa,
     costs_uncat_aa, _shared_no_hist_inv_ids_aa) = _process_slices(inv_slices_aa)

    # Merge shared-CC-no-history amounts into no_cc for P&L totals so that
    # Σ allocated + unallocated = invoice amount is conserved.
    # The distinction is surfaced via shared_cc_no_hist_invoice_count (derived
    # from shared_no_hist_inv_ids — the exact invoice IDs whose categorised slices
    # fell back to unallocated), not via separate amount rows.
    def _merge_into(target: dict, source: dict) -> None:
        for cat_id, months in source.items():
            for m, v in months.items():
                d = target.setdefault(cat_id, {})
                d[m] = round(d.get(m, 0.0) + v, 4)

    _merge_into(no_cc_inv,    shared_no_hist_inv)
    _merge_into(no_cc_inv_aa, shared_no_hist_inv_aa)

    # ── Personnel costs ─────────────────────────────────────────────────

    def _process_pessoal(rows):
        """Distribute personnel cost slices by store via _dist().

        Returns:
            store_pessoal : {store_id: {mes: float}}
            no_cc_pessoal : {mes: float}  — no-CC or shared-CC-no-history
        """
        sp: dict = {}
        no_cc: dict = {}
        for mes, cc_id, amount in rows:
            dist, _ = _dist(float(amount), cc_id)
            if not dist:
                no_cc[mes] = round(no_cc.get(mes, 0.0) + float(amount), 4)
            else:
                for sid, v in dist.items():
                    ds = sp.setdefault(sid, {})
                    ds[mes] = round(ds.get(mes, 0.0) + v, 4)
        return sp, no_cc

    # Personnel costs — projection only for the current calendar year.
    #
    # The data model has no effective-dated payroll / CC-allocation history and
    # no employment end date.  Deriving costs from the current active roster for
    # any month other than a live projection would produce fabricated figures:
    #   • Employees who left mid-year would be missing from already-closed months.
    #   • Employees who joined recently would appear in months before their start.
    #   • Past years would be reconstructed entirely from today's roster.
    #
    # For these reasons personnel costs are emitted ONLY when ano == today.year.
    # The template surfaces this as "Pessoal (Projeção)" with an explanatory badge.
    # pessoal_aa is always empty; the template's {% if mapa.pessoal_aa %} guard
    # will suppress the prior-year sub-row.
    # Follow-up task #709 tracks adding effective-dated history so that real
    # actuals can be shown for closed periods and prior years.

    pessoal_is_projection = (ano == today.year)

    if pessoal_is_projection:
        pessoal_rows = get_pessoal_costs_by_cc(ano)
        store_pessoal, no_cc_pessoal = _process_pessoal(pessoal_rows)

        def _global_pessoal(sp, no_cc_p):
            """Σ all stores + unallocated → global monthly pessoal totals."""
            g: dict = {}
            for sid in store_ids:
                for mes, v in sp.get(sid, {}).items():
                    g[mes] = round(g.get(mes, 0.0) + v, 4)
            for mes, v in no_cc_p.items():
                g[mes] = round(g.get(mes, 0.0) + v, 4)
            return {m: round(v, 2) for m, v in g.items()}

        global_pessoal = _global_pessoal(store_pessoal, no_cc_pessoal)
    else:
        store_pessoal = {sid: {} for sid in store_ids}
        no_cc_pessoal = {}
        global_pessoal = {}

    store_pessoal_aa = {sid: {} for sid in store_ids}
    global_pessoal_aa: dict = {}

    # ── Aggregation helpers ─────────────────────────────────────────────────

    def _global_cat(cat_id, sa, no_cc):
        """Global (cat, mes) total = Σ stores + no-CC."""
        g: dict = {}
        for sid in store_ids:
            for mes, v in sa.get(cat_id, {}).get(sid, {}).items():
                g[mes] = round(g.get(mes, 0.0) + v, 4)
        for mes, v in no_cc.get(cat_id, {}).items():
            g[mes] = round(g.get(mes, 0.0) + v, 4)
        return {m: round(v, 2) for m, v in g.items()}

    def _global_cmvmc_totals(sa, no_cc):
        g: dict = {}
        for cid in cmvmc_cat_ids:
            for mes, v in _global_cat(cid, sa, no_cc).items():
                g[mes] = round(g.get(mes, 0.0) + v, 4)
        return {m: round(v, 2) for m, v in g.items()}

    def _store_cmvmc(sid, sa):
        g: dict = {}
        for cid in cmvmc_cat_ids:
            for mes, v in sa.get(cid, {}).get(sid, {}).items():
                g[mes] = round(g.get(mes, 0.0) + v, 4)
        return {m: round(v, 2) for m, v in g.items()}

    global_cmvmc    = _global_cmvmc_totals(store_alloc,    no_cc_inv)
    global_cmvmc_aa = _global_cmvmc_totals(store_alloc_aa, no_cc_inv_aa)

    # Keep the raw invoice presence separate from the allocated CMVMC amount.
    # A zero CMVMC amount can be legitimate, but when there are sales and no
    # CMVMC invoice at all the gross margin is not comparable to closed months.
    cmvmc_invoice_ids_by_month: dict = {}
    for inv_id, mes, cat_id, _cc_id, _amount in inv_slices:
        if cat_id in cmvmc_cat_ids:
            cmvmc_invoice_ids_by_month.setdefault(int(mes), set()).add(inv_id)

    def _cmvmc_month_status(vendas, cmvmc):
        status = {}
        for mes in range(1, 13):
            sales_present = bool(vendas.get(mes, 0))
            invoice_count = len(cmvmc_invoice_ids_by_month.get(mes, set()))
            allocated_cmvmc = bool(cmvmc.get(mes, 0))
            warning = None
            if sales_present and not invoice_count:
                warning = 'sem_faturas_cmvmc'
            elif sales_present and not allocated_cmvmc:
                warning = 'sem_cmvmc_atribuido'
            status[mes] = {
                'is_current_month': ano == today.year and mes == today.month,
                'cmvmc_invoice_count': invoice_count,
                'cmvmc_warning': warning,
            }
        return status

    # ── Per-store view ──────────────────────────────────────────────────────

    if store_id is not None:
        sid = store_id
        store_obj = next((s for s in active_stores if s['id'] == sid), None)
        bgt = budget_all.get(sid, {})

        vendas    = {m: round(v, 2) for m, v in sv.get(sid, {}).items()}
        vendas_aa = {m: round(v, 2) for m, v in sv_aa.get(sid, {}).items()}
        cmvmc     = _store_cmvmc(sid, store_alloc)
        cmvmc_aa  = _store_cmvmc(sid, store_alloc_aa)

        costs = {
            cat['id']: {m: round(v, 2) for m, v in store_alloc.get(cat['id'], {}).get(sid, {}).items()}
            for cat in categories
        }
        costs_aa = {
            cat['id']: {m: round(v, 2) for m, v in store_alloc_aa.get(cat['id'], {}).get(sid, {}).items()}
            for cat in categories
        }

        return {
            'mode': 'store',
            'ano': ano, 'ano_aa': ano_aa, 'today': today,
            'store': store_obj, 'stores': active_stores,
            'categories': categories, 'cmvmc_cat_ids': list(cmvmc_cat_ids),
            'vendas': vendas, 'vendas_aa': vendas_aa,
            'cmvmc': cmvmc, 'cmvmc_aa': cmvmc_aa,
            'month_status': _cmvmc_month_status(vendas, cmvmc),
            'costs': costs, 'costs_aa': costs_aa,
            'costs_uncat': {m: round(v, 2) for m, v in costs_uncat.items()},
            'costs_uncat_aa': {m: round(v, 2) for m, v in costs_uncat_aa.items()},
            'budget_vendas': bgt.get('vendas', {}),
            'budget_cmvmc':  bgt.get('cmvmc', {}),
            'budget_costs':  {cat['id']: bgt.get(f'cat_{cat["id"]}', {}) for cat in categories},
            # Personnel costs — projection for current year only (see module docstring)
            'pessoal_is_projection': pessoal_is_projection,
            'pessoal':    {m: round(v, 2) for m, v in store_pessoal.get(sid, {}).items()},
            'pessoal_aa': {},
        }

    # ── Consolidated view ───────────────────────────────────────────────────
    cons_bgt = budget_all.get(None, {})

    global_costs    = {cat['id']: _global_cat(cat['id'], store_alloc,    no_cc_inv)    for cat in categories}
    global_costs_aa = {cat['id']: _global_cat(cat['id'], store_alloc_aa, no_cc_inv_aa) for cat in categories}

    store_cmvmc: dict    = {}
    store_cmvmc_aa: dict = {}
    store_costs: dict    = {}
    store_costs_aa: dict = {}
    for sid in store_ids:
        store_cmvmc[sid]    = _store_cmvmc(sid, store_alloc)
        store_cmvmc_aa[sid] = _store_cmvmc(sid, store_alloc_aa)
        store_costs[sid] = {
            cat['id']: {m: round(v, 2) for m, v in store_alloc.get(cat['id'], {}).get(sid, {}).items()}
            for cat in categories
        }
        store_costs_aa[sid] = {
            cat['id']: {m: round(v, 2) for m, v in store_alloc_aa.get(cat['id'], {}).get(sid, {}).items()}
            for cat in categories
        }

    # Unallocated = invoices with no CC (cc_id IS NULL) or shared-CC with no
    # sales history.  These appear as "Não alocado" rows in the consolidated view.
    unallocated_cmvmc: dict = {}
    for cid in cmvmc_cat_ids:
        for m, v in no_cc_inv.get(cid, {}).items():
            unallocated_cmvmc[m] = round(unallocated_cmvmc.get(m, 0.0) + v, 2)

    unallocated_costs: dict = {
        cat['id']: {m: round(v, 2) for m, v in no_cc_inv.get(cat['id'], {}).items()}
        for cat in categories
    }

    # Count invoices with no CC assigned at all (for the alert badge in the template).
    # Excludes cancelled and draft so the count matches the actionable list the badge
    # links to (pending_review, scheduled, paid only).
    def _count_sem_cc(year):
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*)
                FROM invoices i
                WHERE EXTRACT(YEAR FROM i.issue_date) = %s
                  AND i.status NOT IN ('cancelled', 'draft')
                  AND i.centro_custo_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM invoice_centros_custo icc
                      WHERE icc.invoice_id = i.id
                  )
            """, (year,))
            return int(cur.fetchone()[0])

    unallocated_invoice_count = _count_sem_cc(ano)

    # Count of invoices whose *categorised* slices fell back to unallocated
    # because their CC is shared (not per-store) and there was no sales history
    # to distribute by.  Derived directly from the resolved allocation slices so
    # it matches the amounts shown in the "Não alocado" rows exactly:
    #   • junction rows that total 100 % suppress the legacy CC, so those
    #     invoices are never in shared_no_hist_inv_ids even if the legacy CC
    #     is a shared CC.
    #   • cat_id=None invoices go to "Sem categoria", not "Não alocado", so
    #     they are excluded because _process_slices only adds to the set when
    #     cat_id is not None.
    shared_cc_no_hist_invoice_count = len(shared_no_hist_inv_ids)

    return {
        'mode': 'consolidated',
        'ano': ano, 'ano_aa': ano_aa, 'today': today,
        'store': None, 'stores': active_stores,
        'categories': categories, 'cmvmc_cat_ids': list(cmvmc_cat_ids),
        'vendas': global_vendas, 'vendas_aa': global_vendas_aa,
        'cmvmc': global_cmvmc, 'cmvmc_aa': global_cmvmc_aa,
        'month_status': _cmvmc_month_status(global_vendas, global_cmvmc),
        'costs': global_costs, 'costs_aa': global_costs_aa,
        'budget_vendas': cons_bgt.get('vendas', {}),
        'budget_cmvmc':  cons_bgt.get('cmvmc', {}),
        'budget_costs':  {cat['id']: cons_bgt.get(f'cat_{cat["id"]}', {}) for cat in categories},
        'store_vendas':    {sid: {m: round(v, 2) for m, v in sv.get(sid, {}).items()}    for sid in store_ids},
        'store_vendas_aa': {sid: {m: round(v, 2) for m, v in sv_aa.get(sid, {}).items()} for sid in store_ids},
        'store_cmvmc':     store_cmvmc,
        'store_cmvmc_aa':  store_cmvmc_aa,
        'store_costs':     store_costs,
        'store_costs_aa':  store_costs_aa,
        'unallocated_cmvmc': unallocated_cmvmc,
        'unallocated_costs': unallocated_costs,
        'unallocated_invoice_count': unallocated_invoice_count,
        'shared_cc_no_hist_invoice_count': shared_cc_no_hist_invoice_count,
        'costs_uncat':    {m: round(v, 2) for m, v in costs_uncat.items()},
        'costs_uncat_aa': {m: round(v, 2) for m, v in costs_uncat_aa.items()},
        # Personnel costs — projection for current year only (see module docstring)
        'pessoal_is_projection': pessoal_is_projection,
        'pessoal':           global_pessoal,
        'pessoal_aa':        {},
        'store_pessoal':     {sid: {m: round(v, 2) for m, v in store_pessoal.get(sid, {}).items()}    for sid in store_ids},
        'store_pessoal_aa':  {},
        'unallocated_pessoal': {m: round(v, 2) for m, v in no_cc_pessoal.items()},
    }
