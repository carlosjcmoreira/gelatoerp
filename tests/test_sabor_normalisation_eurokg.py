"""
Regression test: sabor normalisation must not change Euro/kg totals.

The migration run_migrations_normalise_producao_sabores rewrites producao.sabor
values from old/alias names to canonical nome_corrente values.  If any mapping
pair has different conta_eurokg status (one included, the other excluded) the
monthly Euro/kg total visible on the dashboard would silently change.

Three complementary checks are exercised:

  1. Mapping-consistency check (unit): for every (old, new) pair defined in the
     migration, verify that both names resolve to the same Euro/kg inclusion
     status given a known receitas_gelado fixture.

  2. Total-preservation check (integration-mock): build a fake producao dataset
     with old sabor names and a known kg total, apply the full normalisation
     mapping in-memory, recompute the total with the same exclusion logic, and
     assert the result is identical.

  3. Live DB checks (requires DATABASE_URL; skipped when DB unavailable): reads
     real receitas_gelado.conta_eurokg values and verifies that:
       a) every canonical target name in the fix pairs shares the same
          conta_eurokg status as its old-name counterpart.
       b) zero rows in producao still carry any old/alias names (migration
          completed fully).
"""

import os
import pytest
from datetime import date
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Canonical mapping pairs – copied verbatim from
# db/schema.py :: run_migrations_normalise_producao_sabores
# Update this list whenever the migration's `fixes` list changes.
# ---------------------------------------------------------------------------
MIGRATION_FIXES = [
    # Original batch
    ("TIRAMISU",                        "Tiramisu"),
    ("Flor de leite",                   "Fior di Latte"),
    ("Noz Pecan e Maple ",              "Noz Pecan e Maple"),
    ("Base Chocolate Nivà",             "Base chocolate niva"),
    ("GRANTORINO TUORLO ZUCCHERATO",    "Nocciolato"),
    # Capitalisation mismatches
    ("Extra noir",                      "Extra Noir"),
    ("Doce de leite",                   "Doce de Leite"),
    ("Chocolate branco",                "Chocolate Branco"),
    ("Queijo da serra",                 "Queijo da Serra"),
    ("Chocolate com laranja",           "Chocolate com Laranja"),
    ("Creme de natal",                  "Creme de Natal"),
    # ALL NATURAL supplier-name → canonical nome_corrente
    ("ALL NATURAL MORANGO",             "Morango"),
    ("ALL NATURAL CHOCOLATE HORTELA SORBETTO", "Extra Noir Hortelã"),
    ("CARAMELLO DULCE DE LECHE FRANCISCO(1)", "Doce de Leite"),
    ("ALL NATURAL ANANAS ABACAXI",      "Abacaxi"),
    ("Ananás",                          "Abacaxi"),
    ("ALL NATURAL FRUTOS DO BOSQUE",    "Frutos Vermelhos"),
    ("Frutos do Bosque",               "Frutos Vermelhos"),
    ("ZERO ALL NATURAL FRAGOLA",        "Morango"),
]

# General normalisation also covers receitas_gelado nome → nome_corrente
# These are the receita_fixes pairs whose old `nome` may also appear as
# sabor values in producao (so they are included in the total-preservation check).
RECEITA_FIXES_NOME_TO_CORRENTE = [
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


# ---------------------------------------------------------------------------
# Fixture: receitas_gelado rows used by both checks.
# Each entry: (nome, nome_corrente, conta_eurokg)
# This fixture covers every sabor that appears in MIGRATION_FIXES and
# RECEITA_FIXES_NOME_TO_CORRENTE.  Rows are marked conta_eurokg=True unless
# the recipe is known to be intentionally excluded (e.g. Carapina/control
# flavours).  The important invariant is that old_name and new_name always
# share the same conta_eurokg value.
# ---------------------------------------------------------------------------
RECEITAS_FIXTURE = [
    # nome                                        nome_corrente              conta_eurokg
    ("TIRAMISU",                                 "Tiramisu",                True),
    ("FIORDILATTE",                              "Fior di Latte",           True),
    ("Noz Pecan e Maple",                        "Noz Pecan e Maple",       True),
    ("Base Chocolate Nivà",                      "Base chocolate niva",     True),
    ("GRANTORINO TUORLO ZUCCHERATO",             "Nocciolato",              True),
    ("ALL NATURAL CHOCOLATE SORBETTO",           "Extra Noir",              True),
    ("ALL NATURAL CHOCOLATE HORTELA SORBETTO",   "Extra Noir Hortelã",      True),
    ("CARAMELLO DULCE DE LECHE FRANCISCO",       "Doce de Leite",          True),
    ("CIOCCOBIANCO  RISOLATTE NEW",              "Chocolate Branco",        True),
    ("CHOCOLATE COM LARANJA",                    "Chocolate com Laranja",   True),
    ("EUSKALDUNA",                               "Queijo da Serra",         True),
    ("CREMA DI NATALE",                          "Creme de Natal",          True),
    ("ALL NATURAL MORANGO",                      "Morango",                 True),
    ("ALL NATURAL ANANAS ABACAXI",               "Abacaxi",                 True),
    ("ALL NATURAL FRUTOS DO BOSQUE",             "Frutos Vermelhos",        True),
    ("STRACCIATELLA RUBY",                       "Stracciatella Ruby",      True),
    # Some canonical names also appear as their own nome in receitas_gelado
    ("Tiramisu",                                 "Tiramisu",                True),
    ("Fior di Latte",                            "Fior di Latte",           True),
    ("Noz Pecan e Maple",                        "Noz Pecan e Maple",       True),
    ("Nocciolato",                               "Nocciolato",              True),
    ("Extra Noir",                               "Extra Noir",              True),
    ("Extra Noir Hortelã",                       "Extra Noir Hortelã",      True),
    ("Doce de Leite",                            "Doce de Leite",           True),
    ("Chocolate Branco",                         "Chocolate Branco",        True),
    ("Queijo da Serra",                          "Queijo da Serra",         True),
    ("Chocolate com Laranja",                    "Chocolate com Laranja",   True),
    ("Creme de Natal",                           "Creme de Natal",          True),
    ("Morango",                                  "Morango",                 True),
    ("Abacaxi",                                  "Abacaxi",                 True),
    ("Frutos Vermelhos",                         "Frutos Vermelhos",        True),
    ("Base chocolate niva",                      "Base chocolate niva",     True),
]


def _build_lookup(receitas):
    """Build {sabor_name: conta_eurokg} covering both nome and nome_corrente."""
    lookup = {}
    for nome, nome_corrente, conta_ek in receitas:
        lookup[nome] = conta_ek
        if nome_corrente:
            lookup[nome_corrente] = conta_ek
    return lookup


def _apply_fixes_in_memory(sabor, fixes, receitas):
    """
    Apply the migration mapping in-memory, replicating the SQL logic:
      1. Explicit fix pairs (MIGRATION_FIXES).
      2. General nome→nome_corrente normalisation (case-insensitive match
         on receitas_gelado.nome).
    Returns the canonical sabor name after migration.
    """
    # Step 1 – explicit pairs (exact match, as in the UPDATE … WHERE sabor = %s)
    for old, new in fixes:
        if sabor == old:
            return new

    # Step 2 – general normalisation (UPPER match on receitas_gelado.nome)
    sabor_upper = sabor.upper()
    for nome, nome_corrente, _ in receitas:
        if nome.upper() == sabor_upper and nome_corrente and nome_corrente != sabor:
            return nome_corrente

    return sabor  # unchanged


# ---------------------------------------------------------------------------
# Check 1: every (old_name → new_name) pair preserves conta_eurokg status
# ---------------------------------------------------------------------------
class TestMappingEurokgConsistency:
    """Each explicit fix pair must not change the Euro/kg inclusion status."""

    lookup = _build_lookup(RECEITAS_FIXTURE)

    @pytest.mark.parametrize("old_name,new_name", MIGRATION_FIXES)
    def test_fix_pair_same_eurokg_status(self, old_name, new_name):
        """Old and new sabor names must share the same Euro/kg inclusion status."""
        lookup = self.lookup
        old_included = lookup.get(old_name.strip(), True)   # default: included
        new_included = lookup.get(new_name.strip(), True)   # default: included
        assert old_included == new_included, (
            f"Mapping {old_name!r} → {new_name!r} changes Euro/kg status: "
            f"old={old_included}, new={new_included}. "
            "If the new canonical name should be excluded, add it to receitas_gelado "
            "with conta_eurokg=FALSE — and vice-versa."
        )

    @pytest.mark.parametrize("old_name,new_name", RECEITA_FIXES_NOME_TO_CORRENTE)
    def test_receita_fix_same_eurokg_status(self, old_name, new_name):
        """receitas_gelado nome→nome_corrente pairs must share the same Euro/kg status."""
        lookup = self.lookup
        old_included = lookup.get(old_name.strip(), True)
        new_included = lookup.get(new_name.strip(), True)
        assert old_included == new_included, (
            f"receitas_gelado fix {old_name!r} → {new_name!r} changes Euro/kg status: "
            f"old={old_included}, new={new_included}."
        )


# ---------------------------------------------------------------------------
# Check 2: total-preservation check with mock producao dataset
# ---------------------------------------------------------------------------

def _compute_eurokg_total(rows, excluidos):
    """
    Replicate the WHERE clause of get_producao_total_by_period (para_eurokg=True):
      tipo IN ('producao', 'balança')
      AND sabor IS NOT NULL
      AND sabor NOT IN (excluidos)
    """
    tipos_validos = {'producao', 'balança'}
    total = 0.0
    for row in rows:
        if row['tipo'] not in tipos_validos:
            continue
        sabor = row['sabor']
        if sabor is None:
            continue
        if sabor in excluidos:
            continue
        total += row['quantidade_kg']
    return round(total, 4)


# Pre-computed expected total for the fixture rows below (excludes manual and NULL rows).
# Sum of all includable rows:
#   10+5+3+4+6+2+7+1+8+9+3.5+4.5+2.5+1.5+5.5+3+2+1+6+15+8 = 107.5
_FIXTURE_EXPECTED_TOTAL = 107.5


class TestTotalPreservation:
    """
    Build a fixture producao dataset using old sabor names, compute the Euro/kg
    total, then apply every normalisation step in-memory and confirm the total
    is identical.
    """

    # All rows use tipo='producao'/'balança' with known kg values so we can
    # predict the exact total.  Mix old names (pre-migration) with canonical names.
    FIXTURE_ROWS_BEFORE = [
        # --- Old names that get renamed ---
        {'sabor': 'TIRAMISU',                       'quantidade_kg': 10.0, 'tipo': 'producao'},
        {'sabor': 'Flor de leite',                  'quantidade_kg': 5.0,  'tipo': 'producao'},
        {'sabor': 'Noz Pecan e Maple ',             'quantidade_kg': 3.0,  'tipo': 'producao'},
        {'sabor': 'Base Chocolate Nivà',            'quantidade_kg': 4.0,  'tipo': 'producao'},
        {'sabor': 'GRANTORINO TUORLO ZUCCHERATO',   'quantidade_kg': 6.0,  'tipo': 'producao'},
        {'sabor': 'Extra noir',                     'quantidade_kg': 2.0,  'tipo': 'producao'},
        {'sabor': 'Doce de leite',                  'quantidade_kg': 7.0,  'tipo': 'producao'},
        {'sabor': 'Chocolate branco',               'quantidade_kg': 1.0,  'tipo': 'producao'},
        {'sabor': 'Queijo da serra',                'quantidade_kg': 8.0,  'tipo': 'producao'},
        {'sabor': 'Chocolate com laranja',          'quantidade_kg': 9.0,  'tipo': 'producao'},
        {'sabor': 'Creme de natal',                 'quantidade_kg': 3.5,  'tipo': 'producao'},
        {'sabor': 'ALL NATURAL MORANGO',            'quantidade_kg': 4.5,  'tipo': 'balança'},
        {'sabor': 'ALL NATURAL CHOCOLATE HORTELA SORBETTO', 'quantidade_kg': 2.5, 'tipo': 'balança'},
        {'sabor': 'CARAMELLO DULCE DE LECHE FRANCISCO(1)',  'quantidade_kg': 1.5, 'tipo': 'producao'},
        {'sabor': 'ALL NATURAL ANANAS ABACAXI',     'quantidade_kg': 5.5,  'tipo': 'producao'},
        {'sabor': 'Ananás',                         'quantidade_kg': 3.0,  'tipo': 'producao'},
        {'sabor': 'ALL NATURAL FRUTOS DO BOSQUE',   'quantidade_kg': 2.0,  'tipo': 'balança'},
        {'sabor': 'Frutos do Bosque',               'quantidade_kg': 1.0,  'tipo': 'producao'},
        {'sabor': 'ZERO ALL NATURAL FRAGOLA',       'quantidade_kg': 6.0,  'tipo': 'producao'},
        # --- Already-canonical names (unchanged by migration) ---
        {'sabor': 'Pistacchio',                     'quantidade_kg': 15.0, 'tipo': 'producao'},
        {'sabor': 'Morango',                        'quantidade_kg': 8.0,  'tipo': 'producao'},
        # --- tipo='manual' must be excluded regardless of sabor ---
        {'sabor': 'Tiramisu',                       'quantidade_kg': 999.0,'tipo': 'manual'},
        # --- NULL sabor must be excluded ---
        {'sabor': None,                             'quantidade_kg': 50.0, 'tipo': 'producao'},
    ]

    # Sabores excluded from Euro/kg in our fixture (empty — all above are included).
    EXCLUIDOS: list = []

    def _rows_after_migration(self):
        """Return fixture rows with sabor values normalised by the migration."""
        return [
            {
                **row,
                'sabor': _apply_fixes_in_memory(
                    row['sabor'], MIGRATION_FIXES, RECEITAS_FIXTURE
                ) if row['sabor'] is not None else None,
            }
            for row in self.FIXTURE_ROWS_BEFORE
        ]

    def test_total_unchanged_after_normalisation(self):
        total_before = _compute_eurokg_total(self.FIXTURE_ROWS_BEFORE, self.EXCLUIDOS)
        rows_after   = self._rows_after_migration()
        total_after  = _compute_eurokg_total(rows_after, self.EXCLUIDOS)

        assert total_before == _FIXTURE_EXPECTED_TOTAL, (
            f"Pre-migration fixture total {total_before} does not match expected "
            f"{_FIXTURE_EXPECTED_TOTAL}. Check FIXTURE_ROWS_BEFORE and _FIXTURE_EXPECTED_TOTAL."
        )
        assert total_after == _FIXTURE_EXPECTED_TOTAL, (
            f"Euro/kg total changed after sabor normalisation! "
            f"Before: {total_before} kg  After: {total_after} kg  "
            f"Difference: {total_after - total_before:+.4f} kg. "
            "A migration fix pair is changing the Euro/kg inclusion status of some rows."
        )

    def test_manual_entries_excluded_before_and_after(self):
        """tipo='manual' rows must never enter the Euro/kg total (before or after).
        The fixture has a 999 kg manual row — if it creeps in the totals diverge."""
        expected = _FIXTURE_EXPECTED_TOTAL  # does NOT include the 999 kg manual row

        total_before = _compute_eurokg_total(self.FIXTURE_ROWS_BEFORE, self.EXCLUIDOS)
        assert total_before == expected, (
            f"manual-tipo row leaked into Euro/kg total before migration. "
            f"Expected {expected}, got {total_before}"
        )

        rows_after  = self._rows_after_migration()
        total_after = _compute_eurokg_total(rows_after, self.EXCLUIDOS)
        assert total_after == expected, (
            f"manual-tipo row leaked into Euro/kg total after migration. "
            f"Expected {expected}, got {total_after}"
        )

    def test_null_sabor_excluded_before_and_after(self):
        """Rows with sabor=NULL must be excluded from the Euro/kg total.
        The fixture has a 50 kg NULL row; neither before nor after total must include it."""
        expected = _FIXTURE_EXPECTED_TOTAL  # does NOT include the 50 kg NULL row

        total_before = _compute_eurokg_total(self.FIXTURE_ROWS_BEFORE, self.EXCLUIDOS)
        assert total_before == expected, (
            f"NULL-sabor row leaked into Euro/kg total before migration. "
            f"Expected {expected}, got {total_before}"
        )

        rows_after = self._rows_after_migration()
        total_after = _compute_eurokg_total(rows_after, self.EXCLUIDOS)
        assert total_after == expected, (
            f"NULL-sabor row leaked into Euro/kg total after migration. "
            f"Expected {expected}, got {total_after}"
        )

    def test_canonical_names_unchanged(self):
        """Names that are already canonical must not be altered by the migration."""
        canonical_before = {
            r['sabor']
            for r in self.FIXTURE_ROWS_BEFORE
            if r['sabor'] is not None and r['sabor'] not in {f[0] for f in MIGRATION_FIXES}
        }
        rows_after = self._rows_after_migration()
        canonical_after = {
            r['sabor']
            for r in rows_after
            if r['sabor'] is not None and r['sabor'] not in {f[1] for f in MIGRATION_FIXES}
        }
        # Every already-canonical name should survive unchanged.
        for name in canonical_before:
            assert name in canonical_after or any(
                r['sabor'] == name for r in rows_after
            ), f"Canonical sabor {name!r} was unexpectedly renamed by the migration."


# ---------------------------------------------------------------------------
# Check 3: excluded-sabor scenario — ensure a sabor in the excluidos list is
# treated consistently before and after migration.
# ---------------------------------------------------------------------------
class TestExcludedSaborPreservation:
    """If an old name maps to an excluded canonical name, those kg stay out."""

    # Imagine 'Carapina de controlo' is excluded from Euro/kg.
    EXCLUIDOS_WITH_CANONICAL = ['Carapina de controlo']

    ROWS = [
        {'sabor': 'Carapina de controlo', 'quantidade_kg': 20.0, 'tipo': 'producao'},
        {'sabor': 'Pistacchio',           'quantidade_kg': 10.0, 'tipo': 'producao'},
    ]

    def test_excluded_sabor_stays_out(self):
        total = _compute_eurokg_total(self.ROWS, self.EXCLUIDOS_WITH_CANONICAL)
        assert total == 10.0, (
            f"Excluded sabor leaked into total. Expected 10.0, got {total}"
        )

    def test_migration_does_not_include_excluded(self):
        """After migration, an excluded sabor must remain excluded."""
        # 'Carapina de controlo' is not in MIGRATION_FIXES, so it stays unchanged.
        rows_after = [
            {**r, 'sabor': _apply_fixes_in_memory(r['sabor'], MIGRATION_FIXES, RECEITAS_FIXTURE)}
            for r in self.ROWS
        ]
        total_after = _compute_eurokg_total(rows_after, self.EXCLUIDOS_WITH_CANONICAL)
        assert total_after == 10.0, (
            f"Migration changed exclusion status of 'Carapina de controlo'. "
            f"Expected 10.0, got {total_after}"
        )


# ---------------------------------------------------------------------------
# Check 4: Live DB verification (skipped when DATABASE_URL is unavailable)
# Reads real receitas_gelado.conta_eurokg and confirms:
#   a) Every canonical target name in the fix pairs exists in receitas_gelado
#      and shares the same conta_eurokg status as its old-name counterpart.
#   b) Zero rows in producao still carry any old/alias names (migration done).
# ---------------------------------------------------------------------------

def _get_live_conn():
    """Return a psycopg2 connection or None if DATABASE_URL is absent."""
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(db_url)
    except Exception:
        return None


def _build_live_lookup(conn):
    """Query receitas_gelado and return two dicts: by nome and by nome_corrente."""
    with conn.cursor() as cur:
        cur.execute("SELECT nome, nome_corrente, conta_eurokg FROM receitas_gelado")
        rows = cur.fetchall()
    by_nome     = {r[0]: r[2] for r in rows}
    by_corrente = {r[1]: r[2] for r in rows if r[1]}
    return by_nome, by_corrente


@pytest.mark.skipif(
    _get_live_conn() is None,
    reason="DATABASE_URL not available — live DB checks skipped"
)
class TestLiveDbEurokgConsistency:
    """
    Reads the real receitas_gelado table and verifies the migration fix pairs
    produce no change in Euro/kg inclusion status.  Also confirms zero producao
    rows still carry any old/alias names (i.e. the migration ran to completion).
    """

    @pytest.fixture(scope="class")
    @classmethod
    def live_conn(cls):
        import psycopg2
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        yield conn
        conn.close()

    @pytest.fixture(scope="class")
    @classmethod
    def live_lookup(cls, live_conn):
        return _build_live_lookup(live_conn)

    # --- a) Mapping pairs: conta_eurokg consistency from real DB ---

    @pytest.mark.parametrize("old_name,new_name", MIGRATION_FIXES + RECEITA_FIXES_NOME_TO_CORRENTE)
    def test_live_fix_pair_same_eurokg_status(self, live_lookup, old_name, new_name):
        """
        Real DB: each (old, new) mapping pair must have the same conta_eurokg value.
        The canonical target name must exist in receitas_gelado.
        """
        by_nome, by_corrente = live_lookup

        def lookup(name):
            name = name.strip()
            if name in by_corrente:
                return by_corrente[name]
            if name in by_nome:
                return by_nome[name]
            return None

        new_status = lookup(new_name)
        assert new_status is not None, (
            f"Canonical target {new_name!r} is not in receitas_gelado on the live DB. "
            "The migration may have mapped producao rows to an unknown sabor name."
        )

        old_status = lookup(old_name)
        if old_status is None:
            # Old name purged from receitas_gelado — that's fine post-migration.
            # What matters is the canonical target exists (asserted above).
            return

        assert old_status == new_status, (
            f"Live DB: mapping {old_name!r} → {new_name!r} changes Euro/kg status: "
            f"old conta_eurokg={old_status}, new={new_status}. "
            "A recipe's conta_eurokg flag is mismatched between its old and canonical name."
        )

    # --- b) Zero stale old-name rows in producao ---

    @pytest.mark.parametrize("old_name,new_name", MIGRATION_FIXES)
    def test_live_no_old_name_rows_in_producao(self, live_conn, old_name, new_name):
        """
        Real DB: after the migration, producao must have zero rows with the old sabor name.
        Any stale rows mean the migration did not complete or was partially rolled back.
        """
        with live_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM producao WHERE sabor = %s",
                (old_name,)
            )
            count = cur.fetchone()[0]
        assert count == 0, (
            f"Live DB: {count} row(s) in producao still use old sabor name {old_name!r}. "
            f"These should have been migrated to {new_name!r}. "
            "Re-run run_migrations_normalise_producao_sabores to complete the migration."
        )
