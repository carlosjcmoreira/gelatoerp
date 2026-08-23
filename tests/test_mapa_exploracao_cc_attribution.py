"""
Integration tests for CC→store attribution in get_mapa_exploracao.

All three tests call the *real* get_mapa_exploracao function; only the
database I/O is mocked (db_connection, the two module-level loaders, and
the helper functions imported inside get_mapa_exploracao).

Scenarios tested:
  1. FK wins — a CC with store_id=2 but name='Bolhão' (store 1's name) routes
     costs to store 2, not to the name-matched store.
  2. Name fallback — a CC with store_id=NULL and name='Bolhão' routes costs
     to the Bolhão store via the legacy name-match path.
  3. Rename-proof — changing a CC's name does not alter its store_id routing;
     both before and after the rename the same store receives all costs.

Run with:
    python -m unittest tests.test_mapa_exploracao_cc_attribution -v
"""

import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

STORE_BOLHAO     = {'id': 1, 'name': 'Bolhão',     'is_active': True}
STORE_MATOSINHOS = {'id': 2, 'name': 'Matosinhos', 'is_active': True}
ACTIVE_STORES    = [STORE_BOLHAO, STORE_MATOSINHOS]

# One non-CMVMC cost category used by all test invoices
CAT = {'id': 99, 'name': 'Rendas', 'is_cmvmc': False, 'parent_id': None, 'ativo': True}

# Sales split used when shared-CC distribution is exercised
SALES_SPLIT_50_50 = {1: 50.0, 2: 50.0}


def _make_db_conn():
    """Return a fresh mock context-manager connection with empty query results.

    Handles all three DB calls made inside get_mapa_exploracao that cannot
    be patched as module-level functions:
      • _query_vendas(year)  → cursor.fetchall() returns []
      • _count_sem_cc(year)  → cursor.fetchone() returns (0,)
    """
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = (0,)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _run_mapa(cost_centers, inv_rows_2026, store_id=None,
              sales_split=None, inv_rows_prior=None, junction_rows=None):
    """Call the real get_mapa_exploracao(2026, store_id) with controlled data.

    Args:
        cost_centers   list of CC dicts as returned by get_cost_centers()
        inv_rows_2026  list of raw invoice rows for year 2026:
                       [(inv_id, mes, cat_id, cc_id, amount_eur), ...]
                       Passed straight to _load_invoice_rows mock so that
                       _resolve_cc_slices processes them through real logic.
        store_id       passed to get_mapa_exploracao (None = consolidated)
        sales_split    dict {store_id: pct}; defaults to 50/50
        inv_rows_prior same format for the prior year (default: empty)
        junction_rows  dict {invoice_id: [(cc_id, pct), ...]}; defaults to empty
    """
    from db.mapa_exploracao import get_mapa_exploracao

    if sales_split is None:
        sales_split = SALES_SPLIT_50_50
    if inv_rows_prior is None:
        inv_rows_prior = []
    if junction_rows is None:
        junction_rows = {}

    def _mock_load_invoice_rows(year):
        return inv_rows_2026 if year == 2026 else inv_rows_prior

    patches = [
        # Module-level loaders (can be patched directly)
        patch('db.mapa_exploracao._load_invoice_rows',
              side_effect=_mock_load_invoice_rows),
        patch('db.mapa_exploracao._load_junction_rows', return_value=junction_rows),
        # db_connection used by the nested _query_vendas / _count_sem_cc
        patch('db.mapa_exploracao.db_connection', side_effect=_make_db_conn),
        # Helpers imported inside the function body
        patch('db.stores.get_all_stores',           return_value=ACTIVE_STORES),
        patch('db.centros_custo.get_cost_categories', return_value=[CAT]),
        patch('db.centros_custo.get_sales_split_pct', return_value=sales_split),
        patch('db.centros_custo.get_cost_centers',    return_value=cost_centers),
        patch('db.orcamento.get_orcamento_all_stores', return_value={}),
    ]

    with patches[0], patches[1], patches[2], patches[3], \
         patches[4], patches[5], patches[6], patches[7]:
        return get_mapa_exploracao(2026, store_id=store_id)


# ---------------------------------------------------------------------------
# Scenario 1 — store_id FK always wins, even when the CC name matches another store
# ---------------------------------------------------------------------------

class TestFKWinsOverName(unittest.TestCase):
    """
    CC has store_id=2 (Matosinhos) but its name is 'Bolhão' (store 1's name).

    The CC→store mapping must use the explicit FK; costs must flow to
    Matosinhos, not to Bolhão.
    """

    CC = {'id': 10, 'name': 'Bolhão', 'store_id': 2, 'ativo': True}
    # Invoice: id=1, month=3, cat=99, cc=10, €100
    INV_ROWS = [(1, 3, 99, 10, 100.0)]

    def test_consolidated_matosinhos_receives_full_amount(self):
        result = _run_mapa([self.CC], self.INV_ROWS)
        self.assertEqual(result['mode'], 'consolidated')
        mato_amount = result['store_costs'][2][99].get(3, 0.0)
        self.assertAlmostEqual(mato_amount, 100.0, places=2,
            msg="Matosinhos (id=2) must receive the full €100 via the store_id FK")

    def test_consolidated_bolhao_receives_nothing(self):
        result = _run_mapa([self.CC], self.INV_ROWS)
        bolhao_amount = result['store_costs'][1][99].get(3, 0.0)
        self.assertAlmostEqual(bolhao_amount, 0.0, places=2,
            msg="Bolhão (id=1) must receive €0 — its name should not override the FK")

    def test_per_store_matosinhos_view_shows_cost(self):
        result = _run_mapa([self.CC], self.INV_ROWS, store_id=2)
        self.assertEqual(result['mode'], 'store')
        amount = result['costs'][99].get(3, 0.0)
        self.assertAlmostEqual(amount, 100.0, places=2,
            msg="Per-store view for Matosinhos must show the full €100")

    def test_per_store_bolhao_view_shows_nothing(self):
        result = _run_mapa([self.CC], self.INV_ROWS, store_id=1)
        self.assertEqual(result['mode'], 'store')
        amount = result['costs'][99].get(3, 0.0)
        self.assertAlmostEqual(amount, 0.0, places=2,
            msg="Per-store view for Bolhão must show €0 — FK points elsewhere")


# ---------------------------------------------------------------------------
# Scenario 2 — store_id=NULL falls back to the CC's name
# ---------------------------------------------------------------------------

class TestNameFallback(unittest.TestCase):
    """
    CC has store_id=NULL. Its name matches 'Bolhão' (store id=1).

    The legacy name-match path must route the cost to Bolhão.
    """

    CC = {'id': 20, 'name': 'Bolhão', 'store_id': None, 'ativo': True}
    INV_ROWS = [(2, 5, 99, 20, 80.0)]

    def test_consolidated_bolhao_receives_full_amount(self):
        result = _run_mapa([self.CC], self.INV_ROWS)
        amount = result['store_costs'][1][99].get(5, 0.0)
        self.assertAlmostEqual(amount, 80.0, places=2,
            msg="Bolhão (id=1) must receive €80 via name-based fallback")

    def test_consolidated_matosinhos_receives_nothing(self):
        result = _run_mapa([self.CC], self.INV_ROWS)
        amount = result['store_costs'][2][99].get(5, 0.0)
        self.assertAlmostEqual(amount, 0.0, places=2,
            msg="Matosinhos must receive €0 — it has no FK claim and no name match")

    def test_unallocated_row_is_empty(self):
        result = _run_mapa([self.CC], self.INV_ROWS)
        unalloc = result['unallocated_costs'][99].get(5, 0.0)
        self.assertAlmostEqual(unalloc, 0.0, places=2,
            msg="No amount should fall into the unallocated row when name match succeeds")

    def test_per_store_bolhao_view_shows_cost(self):
        result = _run_mapa([self.CC], self.INV_ROWS, store_id=1)
        amount = result['costs'][99].get(5, 0.0)
        self.assertAlmostEqual(amount, 80.0, places=2,
            msg="Per-store view for Bolhão must show the full €80")


# ---------------------------------------------------------------------------
# Scenario 3 — renaming a CC does not break its store_id assignment
# ---------------------------------------------------------------------------

class TestRenamePreservesFK(unittest.TestCase):
    """
    CC starts with store_id=1 (Bolhão) and a given name.
    After 'rename' (simulated by changing the name field in the CC dict),
    the P&L result must be identical — the FK is the sole routing key.
    """

    INV_ROWS = [(3, 7, 99, 50, 300.0)]

    def _cc(self, name):
        return {'id': 50, 'name': name, 'store_id': 1, 'ativo': True}

    def _bolhao_amount(self, name):
        result = _run_mapa([self._cc(name)], self.INV_ROWS)
        return result['store_costs'][1][99].get(7, 0.0)

    def _mato_amount(self, name):
        result = _run_mapa([self._cc(name)], self.INV_ROWS)
        return result['store_costs'][2][99].get(7, 0.0)

    def test_original_name_routes_to_bolhao(self):
        self.assertAlmostEqual(self._bolhao_amount('Bolhão Centro'), 300.0, places=2)

    def test_after_rename_still_routes_to_bolhao(self):
        self.assertAlmostEqual(self._bolhao_amount('Qualquer Nome Novo'), 300.0, places=2,
            msg="Renaming the CC must not change its store attribution")

    def test_rename_to_other_store_name_does_not_hijack_route(self):
        """Renaming CC to 'Matosinhos' must NOT redirect costs to Matosinhos
        when store_id=1 (Bolhão) is already set."""
        self.assertAlmostEqual(self._bolhao_amount('Matosinhos'), 300.0, places=2,
            msg="FK=1 (Bolhão) must win even when name matches Matosinhos")
        self.assertAlmostEqual(self._mato_amount('Matosinhos'), 0.0, places=2,
            msg="Matosinhos must not receive any amount — FK points to Bolhão")

    def test_amount_is_identical_before_and_after_rename(self):
        names = ['Bolhão Centro', 'Renamed CC', 'Matosinhos', 'Produção', '']
        amounts = [self._bolhao_amount(n) for n in names]
        self.assertTrue(
            all(abs(a - 300.0) < 0.01 for a in amounts),
            f"Bolhão amount must be €300 for every CC name variant; got {list(zip(names, amounts))}"
        )


# ---------------------------------------------------------------------------
# Additional edge cases exercising the real function path
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def test_no_cc_invoice_goes_to_unallocated(self):
        """Invoice with cc_id=None must appear in unallocated_costs, not in store_costs."""
        inv_rows = [(4, 2, 99, None, 55.0)]
        result = _run_mapa([], inv_rows)
        unalloc = result['unallocated_costs'][99].get(2, 0.0)
        self.assertAlmostEqual(unalloc, 55.0, places=2)
        for sid in [1, 2]:
            store_amount = result['store_costs'][sid][99].get(2, 0.0)
            self.assertAlmostEqual(store_amount, 0.0, places=2)

    def test_shared_cc_distributes_by_sales_split(self):
        """CC with no FK and no name match is distributed proportionally."""
        cc = {'id': 30, 'name': 'Produção Geral', 'store_id': None, 'ativo': True}
        inv_rows = [(5, 1, 99, 30, 100.0)]
        result = _run_mapa([cc], inv_rows, sales_split={1: 60.0, 2: 40.0})
        store1 = result['store_costs'][1][99].get(1, 0.0)
        store2 = result['store_costs'][2][99].get(1, 0.0)
        self.assertAlmostEqual(store1, 60.0, places=2,
            msg="Store 1 has 60 % sales share → should receive €60")
        self.assertAlmostEqual(store2, 40.0, places=2,
            msg="Store 2 has 40 % sales share → should receive €40")

    def test_store_id_pointing_to_inactive_store_falls_back_to_name(self):
        """CC with store_id referencing an unknown/inactive store falls back to name match."""
        cc = {'id': 70, 'name': 'Bolhão', 'store_id': 999, 'ativo': True}
        inv_rows = [(6, 6, 99, 70, 120.0)]
        result = _run_mapa([cc], inv_rows)
        # 999 is not in active_stores → name 'Bolhão' → store 1
        amount = result['store_costs'][1][99].get(6, 0.0)
        self.assertAlmostEqual(amount, 120.0, places=2,
            msg="Name fallback must activate when store_id=999 is not an active store")

    def test_conservation_fk_cc_total_equals_invoice_amount(self):
        """Σ(store allocations) == invoice amount when a FK CC is used."""
        cc = {'id': 11, 'name': 'CC-X', 'store_id': 1, 'ativo': True}
        inv_rows = [(7, 4, 99, 11, 250.0)]
        result = _run_mapa([cc], inv_rows)
        total = sum(
            result['store_costs'][sid][99].get(4, 0.0)
            for sid in [1, 2]
        ) + result['unallocated_costs'][99].get(4, 0.0)
        self.assertAlmostEqual(total, 250.0, places=2,
            msg="Σ store costs + unallocated must equal the invoice amount")


class TestResolveCCSlices(unittest.TestCase):
    """Junction rows must preserve every invoice euro during expansion."""

    def setUp(self):
        from db.mapa_exploracao import _resolve_cc_slices
        self.resolve = _resolve_cc_slices

    def test_100_percent_junction_split_uses_only_junction_ccs(self):
        rows = [(101, 4, 99, 999, 250.0)]
        junction = {101: [(10, 60.0), (20, 40.0)]}

        slices = self.resolve(rows, junction)

        self.assertEqual(
            slices,
            [
                (101, 4, 99, 10, 150.0),
                (101, 4, 99, 20, 100.0),
            ],
        )
        self.assertAlmostEqual(sum(item[-1] for item in slices), 250.0, places=6)
        self.assertNotIn(999, [item[3] for item in slices])

    def test_partial_junction_split_remainder_falls_back_to_legacy_cc(self):
        rows = [(102, 6, 99, 30, 250.0)]
        junction = {102: [(10, 60.0)]}

        slices = self.resolve(rows, junction)

        self.assertEqual(
            slices,
            [
                (102, 6, 99, 10, 150.0),
                (102, 6, 99, 30, 100.0),
            ],
        )
        self.assertAlmostEqual(sum(item[-1] for item in slices), 250.0, places=6)

    def test_mixed_store_and_shared_ccs_preserve_the_full_amount(self):
        rows = [(103, 8, 99, None, 125.0)]
        junction = {103: [(10, 40.0), (20, 60.0)]}

        slices = self.resolve(rows, junction)

        self.assertEqual([item[3] for item in slices], [10, 20])
        self.assertEqual([item[-1] for item in slices], [50.0, 75.0])
        self.assertAlmostEqual(sum(item[-1] for item in slices), 125.0, places=6)

    def test_cc_less_remainder_is_retained_as_none(self):
        rows = [(104, 9, 99, None, 80.0)]
        junction = {104: [(10, 75.0)]}

        slices = self.resolve(rows, junction)

        self.assertEqual(
            slices,
            [
                (104, 9, 99, 10, 60.0),
                (104, 9, 99, None, 20.0),
            ],
        )
        self.assertAlmostEqual(sum(item[-1] for item in slices), 80.0, places=6)


class TestProcessSlicesConservation(unittest.TestCase):
    """The complete P&L path must account for every invoice allocation."""

    COST_CENTERS = [
        {'id': 10, 'name': 'CC Bolhão', 'store_id': 1, 'ativo': True},
        {'id': 20, 'name': 'Faturas Partilhadas', 'store_id': None, 'ativo': True},
        {'id': 30, 'name': 'CC Matosinhos', 'store_id': 2, 'ativo': True},
    ]

    # All invoices are in month 3 so the total can be checked in one P&L cell.
    # The first four paths are:
    #   • 100 % junction split across store + shared CCs
    #   • partial junction split with legacy-CC remainder
    #   • partial junction split with a CC-less remainder
    #   • cat_id=None, which belongs to the global "Sem categoria" line
    INV_ROWS = [
        (201, 3, 99, 30, 100.0),
        (202, 3, 99, 30, 250.0),
        (203, 3, 99, None, 80.0),
        (204, 3, None, None, 70.0),
    ]
    JUNCTION_ROWS = {
        201: [(10, 40.0), (20, 60.0)],
        202: [(20, 60.0)],
        203: [(10, 75.0)],
    }

    def test_allocated_unallocated_and_uncategorized_sum_to_all_invoices(self):
        result = _run_mapa(
            self.COST_CENTERS,
            self.INV_ROWS,
            sales_split={1: 25.0, 2: 75.0},
            junction_rows=self.JUNCTION_ROWS,
        )

        allocated = sum(
            result['store_costs'][sid][99].get(3, 0.0)
            for sid in [1, 2]
        )
        unallocated = result['unallocated_costs'][99].get(3, 0.0)
        uncategorized = result['costs_uncat'].get(3, 0.0)

        self.assertAlmostEqual(allocated, 410.0, places=2)
        self.assertAlmostEqual(unallocated, 20.0, places=2)
        self.assertAlmostEqual(uncategorized, 70.0, places=2)
        self.assertAlmostEqual(
            allocated + unallocated + uncategorized,
            sum(row[-1] for row in self.INV_ROWS),
            places=2,
            msg="Σ allocated + unallocated + Sem categoria must equal invoice totals",
        )

    def test_mixed_and_partial_paths_reach_the_expected_destinations(self):
        result = _run_mapa(
            self.COST_CENTERS,
            self.INV_ROWS,
            sales_split={1: 25.0, 2: 75.0},
            junction_rows=self.JUNCTION_ROWS,
        )

        # All categorized invoices are aggregated in the same month:
        # invoices 201 + 202 exercise mixed/shared/legacy paths, while invoice
        # 203 contributes its allocated 75 % to store 1.
        self.assertAlmostEqual(result['store_costs'][1][99].get(3, 0.0), 55.0 + 37.5 + 60.0, places=2)
        self.assertAlmostEqual(result['store_costs'][2][99].get(3, 0.0), 45.0 + 112.5 + 100.0, places=2)
        # Invoice 203: 25 % remains genuinely unallocated.
        self.assertAlmostEqual(result['unallocated_costs'][99].get(3, 0.0), 20.0, places=2)


if __name__ == '__main__':
    unittest.main()
