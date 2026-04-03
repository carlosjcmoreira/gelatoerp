"""CRUD helpers for cost_centers, cost_categories, colaboradores, colaborador_centro_custo."""
import json
import logging

from psycopg2.extras import RealDictCursor

from db.core import db_connection, get_connection, release_connection

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
    """Recalculate derived fields and return (enriched_list, total_liq, total_imp)."""
    total_liq = 0.0
    total_imp = 0.0
    enriched = []
    for c in colaboradores:
        bruto = float(c.get('salario_bruto') or 0)
        premio = float(c.get('premio_bruto') or 0)
        irs_taxa = float(c.get('irs_taxa') or 0)
        irs_frac = irs_taxa / 100.0
        total_b = bruto + premio
        ss_trab = round(total_b * SS_TRAB, 2)
        ss_patr = round(total_b * SS_PATR, 2)
        irs_ret = round(total_b * irs_frac, 2)
        liq = round(total_b * (1 - SS_TRAB - irs_frac), 2)
        imp = round(ss_trab + ss_patr + irs_ret, 2)
        custo = round(total_b * (1 + SS_PATR), 2)
        c = dict(c)
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
                       centros: list = None) -> int:
    """Insert or update a colaborador and their centro_custo allocations.
    centros = [{'centro_custo_id': int, 'percentagem': float}, ...]"""
    with db_connection() as conn:
        cursor = conn.cursor()
        if colaborador_id:
            cursor.execute(
                '''UPDATE colaboradores
                   SET nome=%s, salario_bruto=%s, premio_bruto=%s, irs_taxa=%s,
                       data_inicio=%s, updated_at=NOW()
                   WHERE id=%s''',
                (nome.strip(), salario_bruto, premio_bruto, irs_taxa,
                 data_inicio or None, colaborador_id)
            )
            cid = colaborador_id
        else:
            cursor.execute(
                '''INSERT INTO colaboradores (nome, salario_bruto, premio_bruto, irs_taxa, data_inicio)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id''',
                (nome.strip(), salario_bruto, premio_bruto, irs_taxa, data_inicio or None)
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
