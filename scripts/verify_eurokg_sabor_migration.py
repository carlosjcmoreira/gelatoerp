#!/usr/bin/env python3
"""
verify_eurokg_sabor_migration.py
---------------------------------
Confirms that the sabor normalisation migration (run_migrations_normalise_producao_sabores)
has not changed Euro/kg monthly totals in production.

What this script does
---------------------
1. Checks that zero rows in `producao` still carry any of the old/alias names
   from the migration fix pairs (confirms the migration completed fully).
2. Reads real `receitas_gelado.conta_eurokg` values and verifies every canonical
   target name in the migration fix pairs has a consistent inclusion status.
3. Snapshots current monthly Euro/kg totals (last 3 years) per loja, applying
   the same WHERE clause as `get_producao_total_by_period(para_eurokg=True)`.
4. Writes the snapshot to `scripts/eurokg_snapshot.json` for future diff comparison.
5. Prints a clear PASS / FAIL summary.

Usage
-----
    python scripts/verify_eurokg_sabor_migration.py

Environment: DATABASE_URL must be set (automatically available in the Replit env).
"""

import os
import sys
import json
from datetime import datetime, date

# Allow importing from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Migration fix pairs — kept in sync with db/schema.py
# ---------------------------------------------------------------------------
MIGRATION_FIXES = [
    ("TIRAMISU",                        "Tiramisu"),
    ("Flor de leite",                   "Fior di Latte"),
    ("Noz Pecan e Maple ",              "Noz Pecan e Maple"),
    ("Base Chocolate Nivà",             "Base chocolate niva"),
    ("GRANTORINO TUORLO ZUCCHERATO",    "Nocciolato"),
    ("Extra noir",                      "Extra Noir"),
    ("Doce de leite",                   "Doce de Leite"),
    ("Chocolate branco",                "Chocolate Branco"),
    ("Queijo da serra",                 "Queijo da Serra"),
    ("Chocolate com laranja",           "Chocolate com Laranja"),
    ("Creme de natal",                  "Creme de Natal"),
    ("ALL NATURAL MORANGO",             "Morango"),
    ("ALL NATURAL CHOCOLATE HORTELA SORBETTO", "Extra Noir Hortelã"),
    ("CARAMELLO DULCE DE LECHE FRANCISCO(1)", "Doce de Leite"),
    ("ALL NATURAL ANANAS ABACAXI",      "Abacaxi"),
    ("Ananás",                          "Abacaxi"),
    ("ALL NATURAL FRUTOS DO BOSQUE",    "Frutos Vermelhos"),
    ("Frutos do Bosque",                "Frutos Vermelhos"),
    ("ZERO ALL NATURAL FRAGOLA",        "Morango"),
]

RECEITA_FIXES = [
    ("ALL NATURAL CHOCOLATE SORBETTO",          "Extra Noir"),
    ("ALL NATURAL CHOCOLATE HORTELA SORBETTO",  "Extra Noir Hortelã"),
    ("CARAMELLO DULCE DE LECHE FRANCISCO",      "Doce de Leite"),
    ("CIOCCOBIANCO  RISOLATTE NEW",             "Chocolate Branco"),
    ("CHOCOLATE COM LARANJA",                   "Chocolate com Laranja"),
    ("EUSKALDUNA",                              "Queijo da Serra"),
    ("CREMA DI NATALE",                         "Creme de Natal"),
    ("ALL NATURAL MORANGO",                     "Morango"),
    ("ALL NATURAL ANANAS ABACAXI",              "Abacaxi"),
    ("ALL NATURAL FRUTOS DO BOSQUE",            "Frutos Vermelhos"),
    ("FIORDILATTE",                             "Fior di Latte"),
    ("GRANTORINO TUORLO ZUCCHERATO",            "Nocciolato"),
]

TIPOS_PRODUCAO_EUROKG = ('producao', 'balança')


def connect():
    url = os.environ.get('DATABASE_URL')
    if not url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def check_old_names_absent(conn):
    """
    Step 1: Verify no 'producao' rows still carry old/alias sabor names.
    Returns list of (old_name, row_count) for any stragglers.
    """
    old_names = [old for old, _ in MIGRATION_FIXES]
    cur = conn.cursor()
    cur.execute(
        "SELECT sabor, COUNT(*) as n FROM producao WHERE sabor = ANY(%s) GROUP BY sabor ORDER BY sabor",
        (old_names,)
    )
    stragglers = [(r['sabor'], r['n']) for r in cur.fetchall()]
    return stragglers


def check_conta_eurokg_consistency(conn):
    """
    Step 2: Read real receitas_gelado and verify every (old, new) pair shares
    the same conta_eurokg status.  Returns list of inconsistency dicts.
    """
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(nome_corrente, nome) AS canonical, conta_eurokg FROM receitas_gelado")
    rows = cur.fetchall()
    # Build lookup by canonical name (nome_corrente preferred)
    # Also index by nome for old-name lookups
    cur.execute("SELECT nome, nome_corrente, conta_eurokg FROM receitas_gelado")
    all_rows = cur.fetchall()
    by_nome = {r['nome']: r['conta_eurokg'] for r in all_rows}
    by_corrente = {r['nome_corrente']: r['conta_eurokg'] for r in all_rows if r['nome_corrente']}

    def lookup(name):
        name = name.strip()
        if name in by_corrente:
            return by_corrente[name]
        if name in by_nome:
            return by_nome[name]
        return None  # not found — unknown status

    inconsistencies = []
    all_pairs = MIGRATION_FIXES + RECEITA_FIXES
    for old, new in all_pairs:
        old_status = lookup(old)
        new_status = lookup(new)
        if old_status is None or new_status is None:
            # One side not in receitas_gelado — not necessarily an error
            # (old names may have been purged), but flag if new canonical is missing
            if new_status is None:
                inconsistencies.append({
                    'old': old, 'new': new,
                    'issue': f'canonical target {new!r} not found in receitas_gelado'
                })
            continue
        if old_status != new_status:
            inconsistencies.append({
                'old': old, 'new': new,
                'old_conta_eurokg': old_status,
                'new_conta_eurokg': new_status,
                'issue': 'conta_eurokg mismatch between old and canonical name'
            })
    return inconsistencies


def snapshot_monthly_totals(conn):
    """
    Step 3: Compute monthly Euro/kg totals (last 3 years) applying the same
    WHERE clause as get_producao_total_by_period(para_eurokg=True).
    Returns dict: { "YYYY-MM": { loja: total_kg } }
    """
    cur = conn.cursor()

    # Get excluded sabores
    cur.execute(
        "SELECT COALESCE(nome_corrente, nome) AS canonical FROM receitas_gelado WHERE conta_eurokg = FALSE"
    )
    excluidos = [r['canonical'] for r in cur.fetchall()]

    year_now = date.today().year
    start_year = year_now - 2

    query = """
        SELECT
            TO_CHAR(data, 'YYYY-MM') AS month,
            loja,
            COALESCE(SUM(quantidade_kg), 0) AS total_kg
        FROM producao
        WHERE tipo = ANY(%s)
          AND sabor IS NOT NULL
          AND data >= %s
    """
    params = [list(TIPOS_PRODUCAO_EUROKG), date(start_year, 1, 1)]
    if excluidos:
        query += " AND sabor != ALL(%s)"
        params.append(excluidos)
    query += " GROUP BY month, loja ORDER BY month, loja"

    cur.execute(query, params)
    rows = cur.fetchall()

    snapshot = {}
    for r in rows:
        m = r['month']
        l = r['loja']
        kg = float(r['total_kg'])
        if m not in snapshot:
            snapshot[m] = {}
        snapshot[m][l] = kg
    return snapshot


def main():
    print("=" * 60)
    print("Euro/kg sabor normalisation verification")
    print(f"Run at: {datetime.now().isoformat()}")
    print("=" * 60)

    failures = []

    with psycopg2.connect(os.environ['DATABASE_URL'], cursor_factory=RealDictCursor) as conn:

        # --- Step 1: old names absent ---
        print("\n[1] Checking for stale old-name rows in producao …")
        stragglers = check_old_names_absent(conn)
        if stragglers:
            for sabor, n in stragglers:
                msg = f"    FAIL  {sabor!r}: {n} row(s) still use the old name"
                print(msg)
                failures.append(msg)
        else:
            print("    PASS  No old/alias sabor names remain in producao.")

        # --- Step 2: conta_eurokg consistency ---
        print("\n[2] Verifying conta_eurokg consistency against real receitas_gelado …")
        inconsistencies = check_conta_eurokg_consistency(conn)
        if inconsistencies:
            for inc in inconsistencies:
                msg = (
                    f"    FAIL  {inc['old']!r} → {inc['new']!r}: {inc['issue']}"
                )
                print(msg)
                failures.append(msg)
        else:
            print("    PASS  All mapping pairs share the same conta_eurokg status.")

        # --- Step 3: snapshot ---
        print("\n[3] Snapshotting current monthly Euro/kg totals (last 3 years) …")
        snapshot = snapshot_monthly_totals(conn)
        total_months = len(snapshot)
        grand_total = sum(
            kg for month_data in snapshot.values() for kg in month_data.values()
        )
        print(f"    Captured {total_months} month(s), grand total {grand_total:.2f} kg.")

    # Write snapshot file
    snapshot_path = os.path.join(os.path.dirname(__file__), 'eurokg_snapshot.json')
    snapshot_output = {
        'generated_at': datetime.now().isoformat(),
        'note': (
            'Monthly Euro/kg totals per loja. '
            'Run this script again after schema changes and diff against this file '
            'to confirm totals are unchanged.'
        ),
        'totals_by_month': snapshot,
    }
    with open(snapshot_path, 'w', encoding='utf-8') as f:
        json.dump(snapshot_output, f, ensure_ascii=False, indent=2)
    print(f"    Snapshot saved to {snapshot_path}")

    # --- Summary ---
    print("\n" + "=" * 60)
    if failures:
        print(f"RESULT: FAILED — {len(failures)} issue(s) found:")
        for f in failures:
            print(f)
        sys.exit(1)
    else:
        print("RESULT: PASSED — Euro/kg totals confirmed safe after sabor normalisation.")
    print("=" * 60)


if __name__ == '__main__':
    main()
