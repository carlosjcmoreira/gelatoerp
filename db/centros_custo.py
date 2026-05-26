"""CRUD helpers for cost_centers, cost_categories, colaboradores, colaborador_centro_custo."""
import json
import logging

from psycopg2.extras import RealDictCursor

from db.core import db_connection, get_connection, release_connection
from db.tabelas_cct import lookup_salario_cct
from db.tabelas_irs import lookup_irs

logger = logging.getLogger(__name__)

SS_TRAB = 0.11
SS_PATR = 0.2375


# ── Cost Centers ──────────────────────────────────────────────────────────────

def get_cost_centers(ativo_only: bool = False):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if ativo_only:
            cursor.execute(
                'SELECT * FROM cost_centers WHERE ativo = TRUE ORDER BY code'
            )
        else:
            cursor.execute('SELECT * FROM cost_centers ORDER BY code')
        return cursor.fetchall()


def get_cost_center(cc_id: int):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('SELECT * FROM cost_centers WHERE id = %s', (cc_id,))
        return cursor.fetchone()


def create_cost_center(code: str, name: str, description: str = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO cost_centers (code, name, description)
               VALUES (%s, %s, %s) RETURNING id''',
            (code.strip().upper(), name.strip(), description)
        )
        cc_id = cursor.fetchone()[0]
        conn.commit()
    return cc_id


def update_cost_center(cc_id: int, code: str, name: str, description: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''UPDATE cost_centers SET code=%s, name=%s, description=%s, updated_at=NOW()
               WHERE id=%s''',
            (code.strip().upper(), name.strip(), description, cc_id)
        )
        conn.commit()


def toggle_cost_center(cc_id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE cost_centers SET ativo=%s, updated_at=NOW() WHERE id=%s',
            (ativo, cc_id)
        )
        conn.commit()


# ── Cost Categories ───────────────────────────────────────────────────────────

def get_cost_categories(ativo_only: bool = False):
    """Return all categories ordered by parent then name.
    Top-level categories have parent_id IS NULL."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if ativo_only:
            cursor.execute(
                '''SELECT * FROM cost_categories
                   WHERE ativo = TRUE
                   ORDER BY COALESCE(parent_id, id), parent_id NULLS FIRST, name'''
            )
        else:
            cursor.execute(
                '''SELECT * FROM cost_categories
                   ORDER BY COALESCE(parent_id, id), parent_id NULLS FIRST, name'''
            )
        return cursor.fetchall()


def get_cost_categories_tree(ativo_only: bool = True):
    """Return categories as a nested structure: list of top-level dicts with 'children' key."""
    rows = get_cost_categories(ativo_only=ativo_only)
    top = [dict(r) for r in rows if r['parent_id'] is None]
    by_id = {r['id']: dict(r) for r in rows}
    for t in top:
        t['children'] = [c for c in [dict(r) for r in rows] if c['parent_id'] == t['id']]
    return top


def create_cost_category(name: str, parent_id: int = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO cost_categories (name, parent_id)
               VALUES (%s, %s) RETURNING id''',
            (name.strip(), parent_id or None)
        )
        cat_id = cursor.fetchone()[0]
        conn.commit()
    return cat_id


def update_cost_category(cat_id: int, name: str, parent_id: int = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE cost_categories SET name=%s, parent_id=%s, updated_at=NOW() WHERE id=%s',
            (name.strip(), parent_id or None, cat_id)
        )
        conn.commit()


def toggle_cost_category(cat_id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE cost_categories SET ativo=%s, updated_at=NOW() WHERE id=%s',
            (ativo, cat_id)
        )
        conn.commit()


# ── Colaboradores ─────────────────────────────────────────────────────────────

def get_colaboradores(ativo_only: bool = False):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if ativo_only:
            cursor.execute(
                '''SELECT c.*, COALESCE(
                       json_agg(json_build_object(
                           'centro_custo_id', a.centro_custo_id,
                           'percentagem', a.percentagem,
                           'code', cc.code,
                           'name', cc.name
                       )) FILTER (WHERE a.id IS NOT NULL), '[]'
                   ) AS centros
                   FROM colaboradores c
                   LEFT JOIN colaborador_centro_custo a ON a.colaborador_id = c.id
                   LEFT JOIN cost_centers cc ON cc.id = a.centro_custo_id
                   WHERE c.ativo = TRUE
                   GROUP BY c.id
                   ORDER BY c.nome'''
            )
        else:
            cursor.execute(
                '''SELECT c.*, COALESCE(
                       json_agg(json_build_object(
                           'centro_custo_id', a.centro_custo_id,
                           'percentagem', a.percentagem,
                           'code', cc.code,
                           'name', cc.name
                       )) FILTER (WHERE a.id IS NOT NULL), '[]'
                   ) AS centros
                   FROM colaboradores c
                   LEFT JOIN colaborador_centro_custo a ON a.colaborador_id = c.id
                   LEFT JOIN cost_centers cc ON cc.id = a.centro_custo_id
                   GROUP BY c.id
                   ORDER BY c.nome'''
            )
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get('centros'), str):
                try:
                    d['centros'] = json.loads(d['centros'])
                except Exception:
                    d['centros'] = []
            result.append(d)
        return result


def _calc_colabs(colaboradores: list) -> tuple:
    """Recalculate derived fields and return (enriched_list, total_liq, total_imp).

    When irs_override=False and categoria_profissional is set, the IRS rate is
    determined automatically via lookup_irs (AT 2025 tables).  Otherwise the
    stored irs_taxa value is used unchanged.
    """
    total_liq = 0.0
    total_imp = 0.0
    enriched = []
    for c in colaboradores:
        c = dict(c)

        cat = c.get('categoria_profissional') or 'outro'
        nivel = int(c.get('nivel_remuneratorio') or 1)
        irs_override = bool(c.get('irs_override', False))

        if cat and cat != 'outro':
            cct_base = lookup_salario_cct(cat, nivel)
            if cct_base > 0:
                c['salario_bruto'] = cct_base

        bruto = float(c.get('salario_bruto') or 0)
        premio = float(c.get('premio_bruto') or 0)
        total_b = bruto + premio

        if not irs_override and cat and cat != 'outro':
            estado_civil = c.get('estado_civil') or 'solteiro'
            num_dep = int(c.get('num_dependentes') or 0)
            irs_taxa = lookup_irs(total_b, estado_civil, num_dep)
            c['irs_taxa'] = irs_taxa
        else:
            irs_taxa = float(c.get('irs_taxa') or 0)

        irs_frac = irs_taxa / 100.0
        ss_trab = round(total_b * SS_TRAB, 2)
        ss_patr = round(total_b * SS_PATR, 2)
        irs_ret = round(total_b * irs_frac, 2)
        liq = round(total_b * (1 - SS_TRAB - irs_frac), 2)
        imp = round(ss_trab + ss_patr + irs_ret, 2)
        custo = round(total_b * (1 + SS_PATR), 2)
        c.update({
            'ss_trabalhador': ss_trab,
            'ss_patronal': ss_patr,
            'irs_retido': irs_ret,
            'salario_liq': liq,
            'impostos_dia15': imp,
            'custo_empresa': custo,
        })
        total_liq += liq
        total_imp += imp
        enriched.append(c)
    return enriched, round(total_liq, 2), round(total_imp, 2)


def get_colaboradores_calculados(ativo_only: bool = True):
    """Return colaboradores with derived salary fields (SS, IRS, etc.) calculated."""
    rows = get_colaboradores(ativo_only=ativo_only)
    enriched, _, _ = _calc_colabs(rows)
    return enriched


def upsert_colaborador(colaborador_id: int | None, nome: str,
                       salario_bruto: float, premio_bruto: float,
                       irs_taxa: float, data_inicio=None,
                       centros: list = None,
                       categoria_profissional: str = None,
                       nivel_remuneratorio: int = 1,
                       estado_civil: str = 'solteiro',
                       num_dependentes: int = 0,
                       irs_override: bool = False) -> int:
    """Insert or update a colaborador and their centro_custo allocations.
    centros = [{'centro_custo_id': int, 'percentagem': float}, ...]"""
    with db_connection() as conn:
        cursor = conn.cursor()
        if colaborador_id:
            cursor.execute(
                '''UPDATE colaboradores
                   SET nome=%s, salario_bruto=%s, premio_bruto=%s, irs_taxa=%s,
                       data_inicio=%s,
                       categoria_profissional=%s, nivel_remuneratorio=%s,
                       estado_civil=%s, num_dependentes=%s, irs_override=%s,
                       updated_at=NOW()
                   WHERE id=%s''',
                (nome.strip(), salario_bruto, premio_bruto, irs_taxa,
                 data_inicio or None,
                 categoria_profissional or 'outro', nivel_remuneratorio,
                 estado_civil, num_dependentes, irs_override,
                 colaborador_id)
            )
            cid = colaborador_id
        else:
            cursor.execute(
                '''INSERT INTO colaboradores
                       (nome, salario_bruto, premio_bruto, irs_taxa, data_inicio,
                        categoria_profissional, nivel_remuneratorio,
                        estado_civil, num_dependentes, irs_override)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                (nome.strip(), salario_bruto, premio_bruto, irs_taxa, data_inicio or None,
                 categoria_profissional or 'outro', nivel_remuneratorio,
                 estado_civil, num_dependentes, irs_override)
            )
            cid = cursor.fetchone()[0]

        if centros is not None:
            cursor.execute(
                'DELETE FROM colaborador_centro_custo WHERE colaborador_id = %s', (cid,)
            )
            for alloc in centros:
                cc_id = alloc.get('centro_custo_id')
                pct = float(alloc.get('percentagem', 0) or 0)
                if cc_id and pct > 0:
                    cursor.execute(
                        '''INSERT INTO colaborador_centro_custo (colaborador_id, centro_custo_id, percentagem)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (colaborador_id, centro_custo_id) DO UPDATE SET percentagem = EXCLUDED.percentagem''',
                        (cid, cc_id, pct)
                    )

        conn.commit()
    return cid


def toggle_colaborador(colaborador_id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE colaboradores SET ativo=%s, updated_at=NOW() WHERE id=%s',
            (ativo, colaborador_id)
        )
        conn.commit()


def migrate_colaboradores_from_json(json_str: str) -> int:
    """One-time migration: parse JSON list from cashflow_config and insert into colaboradores.
    Skips records that already exist (by nome). Returns number of records inserted."""
    try:
        data = json.loads(json_str or '[]')
    except Exception:
        return 0
    if not data:
        return 0

    inserted = 0
    with db_connection() as conn:
        cursor = conn.cursor()
        for c in data:
            nome = (c.get('nome') or '').strip()
            if not nome:
                continue
            bruto = float(c.get('salario_bruto') or 0)
            premio = float(c.get('premio_bruto') or 0)
            irs_taxa = float(c.get('irs_taxa') or 0)
            cursor.execute(
                'SELECT id FROM colaboradores WHERE nome = %s', (nome,)
            )
            if cursor.fetchone():
                continue
            cursor.execute(
                '''INSERT INTO colaboradores (nome, salario_bruto, premio_bruto, irs_taxa)
                   VALUES (%s, %s, %s, %s)''',
                (nome, bruto, premio, irs_taxa)
            )
            inserted += 1
        conn.commit()
    return inserted


# ── Cost Center Allocation (P&L store distribution) ────────────────────────

def get_sales_split_pct(months: int = 12) -> dict:
    """Return the sales-volume percentage split per store for the last *months* months.

    Aggregates `vendas_detalhe.valor_euros` by store name, joins with `stores`
    to resolve store_id, and returns `{store_id (int): pct (float)}` rounded to
    1 decimal place.  Percentages sum to 100.0 (within rounding).

    Returns an empty dict if there is no sales data in the period.
    """
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=months * 30)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id AS store_id, COALESCE(SUM(vd.valor_euros), 0) AS total_eur
            FROM stores s
            LEFT JOIN vendas_detalhe vd
                ON vd.loja = s.name
               AND vd.data >= %s
               AND vd.valor_euros IS NOT NULL
            WHERE s.is_active = TRUE
            GROUP BY s.id
        """, (cutoff,))
        rows = cursor.fetchall()

    if not rows:
        return {}

    totals = {r[0]: float(r[1]) for r in rows}
    grand_total = sum(totals.values())
    if grand_total <= 0:
        return {}

    pct = {sid: round(v / grand_total * 100, 1) for sid, v in totals.items()}

    # Correct rounding drift so values sum exactly to 100.0
    diff = round(100.0 - sum(pct.values()), 1)
    if diff != 0 and pct:
        largest = max(pct, key=pct.get)
        pct[largest] = round(pct[largest] + diff, 1)

    return pct

def get_all_allocations() -> dict:
    """Return allocation config indexed by categoria_custo_id.

    Returns:
        {cat_id: {'modo': str, 'stores': {store_id: percentagem}}}
        Categories with no rows default to {'modo': 'volume_vendas', 'stores': {}}.
    """
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT categoria_custo_id, store_id, percentagem, modo "
            "FROM cost_center_allocation ORDER BY categoria_custo_id, store_id NULLS FIRST"
        )
        rows = cursor.fetchall()

    result: dict = {}
    for r in rows:
        cid = r['categoria_custo_id']
        if cid not in result:
            result[cid] = {'modo': r['modo'], 'stores': {}}
        if r['store_id'] is not None:
            result[cid]['stores'][r['store_id']] = r['percentagem']
    return result


def get_pl_by_store(date_from=None, date_to=None) -> dict:
    """Compute a P&L distribution matrix by store for a date range.

    Aggregates ``invoices.amount_eur`` (non-cancelled) by ``categoria_custo_id``
    and distributes each category total across active stores according to the
    allocation config returned by :func:`get_all_allocations` and
    :func:`get_sales_split_pct`.

    Args:
        date_from: ``date`` or ``None`` – filter on ``issue_date >= date_from``.
        date_to:   ``date`` or ``None`` – filter on ``issue_date <= date_to``.

    Returns::

        {
          'stores':        [{'id': int, 'name': str}, ...],   # active stores
          'rows':          [                                   # one per category (+ uncat.)
              {
                'cat_id':        int | None,
                'cat_name':      str,
                'parent_id':     int | None,
                'total_eur':     float,
                'modo':          str,
                'store_amounts': {store_id: float},
                'unallocated':   float,   # portion not assigned to any store
              }
          ],
          'grand_total':   float,
          'store_totals':  {store_id: float},
          'sales_split':   {store_id: float},   # pct used for volume_vendas
        }
    """
    from db.stores import get_all_stores

    active_stores = [s for s in get_all_stores() if s['is_active']]
    store_ids = [s['id'] for s in active_stores]

    where_parts = ["i.status != 'cancelled'"]
    params: list = []
    if date_from:
        where_parts.append("i.issue_date >= %s")
        params.append(date_from)
    if date_to:
        where_parts.append("i.issue_date <= %s")
        params.append(date_to)
    where_sql = "WHERE " + " AND ".join(where_parts)

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT i.categoria_custo_id,
                   COALESCE(cc.name, 'Sem categoria') AS cat_name,
                   cc.parent_id,
                   COALESCE(SUM(i.amount_eur), 0) AS total_eur
            FROM invoices i
            LEFT JOIN cost_categories cc ON cc.id = i.categoria_custo_id
            {where_sql}
            GROUP BY i.categoria_custo_id, cc.name, cc.parent_id
            ORDER BY cc.name NULLS LAST
        """, params)
        raw_rows = cursor.fetchall()

    allocations = get_all_allocations()
    sales_split = get_sales_split_pct(months=12)

    def _resolve_store_pcts(cat_id, modo, stored_stores) -> dict:
        """Return {store_id: pct} summing to ~100 for this category."""
        if modo == 'volume_vendas':
            return {sid: sales_split.get(sid, 0.0) for sid in store_ids}
        if modo == 'igualitario':
            n = len(store_ids)
            equal = round(100.0 / n, 4) if n else 0.0
            return {sid: equal for sid in store_ids}
        if modo in ('tudo_loja', 'manual'):
            return {sid: float(stored_stores.get(sid, 0.0)) for sid in store_ids}
        return {}

    rows = []
    store_totals = {sid: 0.0 for sid in store_ids}
    grand_total = 0.0

    for raw in raw_rows:
        cat_id, cat_name, parent_id, total_eur = raw
        total_eur = float(total_eur)
        grand_total += total_eur

        alloc = allocations.get(cat_id, {'modo': 'volume_vendas', 'stores': {}})
        modo = alloc['modo']
        store_pcts = _resolve_store_pcts(cat_id, modo, alloc['stores'])

        store_amounts = {}
        allocated_sum = 0.0
        for sid in store_ids:
            pct = store_pcts.get(sid, 0.0)
            amount = round(total_eur * pct / 100.0, 2)
            store_amounts[sid] = amount
            allocated_sum += amount
            store_totals[sid] = round(store_totals[sid] + amount, 2)

        unallocated = round(total_eur - allocated_sum, 2)

        rows.append({
            'cat_id': cat_id,
            'cat_name': cat_name,
            'parent_id': parent_id,
            'total_eur': total_eur,
            'modo': modo,
            'store_amounts': store_amounts,
            'unallocated': unallocated,
        })

    return {
        'stores': active_stores,
        'rows': rows,
        'grand_total': round(grand_total, 2),
        'store_totals': {sid: round(v, 2) for sid, v in store_totals.items()},
        'sales_split': sales_split,
    }


def save_allocation(categoria_custo_id: int, modo: str, store_percentages: dict) -> None:
    """Persist allocation config for a cost category.

    Replaces all existing rows for the category atomically.

    Args:
        categoria_custo_id: cost_categories.id
        modo: 'volume_vendas' | 'tudo_loja' | 'manual' | 'igualitario'
        store_percentages: {store_id (int): percentagem (float)}
            - ignored for 'volume_vendas' (sentinel NULL row is written instead)
            - one entry for 'tudo_loja'
            - one entry per store for 'manual' or 'igualitario'
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM cost_center_allocation WHERE categoria_custo_id = %s",
            (categoria_custo_id,)
        )
        if modo == 'volume_vendas':
            cursor.execute(
                "INSERT INTO cost_center_allocation "
                "(categoria_custo_id, store_id, percentagem, modo) "
                "VALUES (%s, NULL, 0, 'volume_vendas')",
                (categoria_custo_id,)
            )
        elif not store_percentages:
            pass  # no rows to insert
        else:
            for sid, pct in store_percentages.items():
                cursor.execute(
                    "INSERT INTO cost_center_allocation "
                    "(categoria_custo_id, store_id, percentagem, modo) "
                    "VALUES (%s, %s, %s, %s)",
                    (categoria_custo_id, int(sid), float(pct), modo)
                )
        conn.commit()
