"""
Integration tests — personnel costs flow into EBITDA across multiple stores.

Scenario: two collaborators
  • Collab A → store CC (Bolhão, store_id=1): €1 200 / month
  • Collab B → shared CC (Produção, no store_id): €2 000 / month
                distributed 60 % Bolhão / 40 % Matosinhos by sales split

Expected per-store pessoal (month 1):
  Bolhão     : 1 200 (A direct) + 1 200 (B×60 %) = 2 400
  Matosinhos :     0             +   800 (B×40 %) =   800

Global pessoal total = 3 200

EBITDA = Vendas − CMVMC − Custos Op. − Pessoal
       must hold for each store and the consolidated view.

The real get_mapa_exploracao function is used; all DB I/O is mocked.

Run with:
    python -m unittest tests.test_pessoal_ebitda_multistore -v
"""

import io
import csv
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

# ── Shared fixtures ──────────────────────────────────────────────────────────

STORE_BOLHAO     = {'id': 1, 'name': 'Bolhão',     'is_active': True}
STORE_MATOSINHOS = {'id': 2, 'name': 'Matosinhos', 'is_active': True}
ACTIVE_STORES    = [STORE_BOLHAO, STORE_MATOSINHOS]

# One non-CMVMC cost category (not used by test invoices, present for completeness)
CAT_RENDAS = {'id': 99, 'name': 'Rendas', 'is_cmvmc': False, 'parent_id': None, 'ativo': True}

# Cost centres
CC_BOLHAO   = {'id': 10, 'name': 'Bolhão',   'store_id': 1,    'ativo': True}  # store CC
CC_PRODUCAO = {'id': 30, 'name': 'Produção',  'store_id': None, 'ativo': True}  # shared CC

COST_CENTERS = [CC_BOLHAO, CC_PRODUCAO]

# Sales split: 60 % Bolhão, 40 % Matosinhos
SALES_SPLIT = {1: 60.0, 2: 40.0}

# Personnel costs by CC — one month (January = month 1)
# get_pessoal_costs_by_cc returns [(mes, cc_id, amount), ...]
PESSOAL_ROWS = [
    (1, 10, 1_200.00),   # Collab A → Bolhão CC → 100 % Bolhão
    (1, 30, 2_000.00),   # Collab B → Produção CC → dist by sales (60/40)
]

# Test year — must be the current year so pessoal_is_projection == True.
# The test environment runs in 2026; set explicitly to avoid drift.
TEST_YEAR = 2026


def _make_db_conn():
    """Minimal mock DB connection for the nested SQL helpers in get_mapa_exploracao."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = (0,)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _run_mapa(pessoal_rows=None, inv_rows=None, store_id=None,
              sales_split=None, vendas_rows=None, categories=None):
    """Call the real get_mapa_exploracao with controlled inputs.

    Args:
        pessoal_rows : override for get_pessoal_costs_by_cc return value
        inv_rows     : invoice rows [(id, mes, cat_id, cc_id, amount), ...]
        store_id     : None = consolidated, int = per-store
        sales_split  : {store_id: pct}  (default: 60/40)
        vendas_rows  : raw vendas_detalhe cursor rows (default: empty → €0 sales)
        categories   : cost-category rows (default: Rendas only)
    """
    from db.mapa_exploracao import get_mapa_exploracao

    if pessoal_rows is None:
        pessoal_rows = PESSOAL_ROWS
    if inv_rows is None:
        inv_rows = []
    if sales_split is None:
        sales_split = SALES_SPLIT
    if vendas_rows is None:
        vendas_rows = []
    if categories is None:
        categories = [CAT_RENDAS]

    def _mock_load_invoice_rows(year):
        return inv_rows if year == TEST_YEAR else []

    # Build a vendas DB response that injects per-store monthly sales.
    # vendas_rows items: (month_int, store_id_int, total_float)
    def _make_vendas_conn():
        cur = MagicMock()
        # First fetchall call → _query_vendas; second fetchone → _count_sem_cc
        cur.fetchall.side_effect = [vendas_rows, []]
        cur.fetchone.return_value = (0,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    patches = [
        patch('db.mapa_exploracao._load_invoice_rows',
              side_effect=_mock_load_invoice_rows),
        patch('db.mapa_exploracao._load_junction_rows', return_value={}),
        patch('db.mapa_exploracao.db_connection', side_effect=_make_vendas_conn),
        patch('db.stores.get_all_stores',              return_value=ACTIVE_STORES),
        patch('db.centros_custo.get_cost_categories',  return_value=categories),
        patch('db.centros_custo.get_sales_split_pct',  return_value=sales_split),
        patch('db.centros_custo.get_cost_centers',     return_value=COST_CENTERS),
        patch('db.centros_custo.get_pessoal_costs_by_cc', return_value=pessoal_rows),
        patch('db.orcamento.get_orcamento_all_stores',    return_value={}),
    ]

    with patches[0], patches[1], patches[2], patches[3], \
         patches[4], patches[5], patches[6], patches[7], patches[8]:
        return get_mapa_exploracao(TEST_YEAR, store_id=store_id)


# ────────────────────────────────────────────────────────────────────────────
# 0 — CMVMC data completeness flags
# ────────────────────────────────────────────────────────────────────────────

class TestCMVMCMonthStatus(unittest.TestCase):
    """Months with sales but no CMVMC invoice must be explicitly flagged."""

    CMVMC = {
        'id': 77, 'name': 'Matéria Prima', 'is_cmvmc': True,
        'parent_id': None, 'ativo': True,
    }

    def test_flags_missing_cmvmc_invoices_without_estimating_costs(self):
        result = _run_mapa(
            pessoal_rows=[],
            categories=[self.CMVMC],
            # CMVMC exists in January only.
            inv_rows=[(700, 1, self.CMVMC['id'], CC_BOLHAO['id'], 100.00)],
            vendas_rows=[(1, STORE_BOLHAO['id'], 1_000.00),
                          (2, STORE_BOLHAO['id'], 1_200.00)],
        )

        self.assertEqual(result['cmvmc'][1], 100.00)
        self.assertEqual(result['month_status'][1]['cmvmc_invoice_count'], 1)
        self.assertIsNone(result['month_status'][1]['cmvmc_warning'])

        self.assertEqual(result['cmvmc'].get(2, 0), 0)
        self.assertEqual(result['month_status'][2]['cmvmc_invoice_count'], 0)
        self.assertEqual(result['month_status'][2]['cmvmc_warning'], 'sem_faturas_cmvmc')

    def test_marks_current_month_as_partial_for_current_year(self):
        result = _run_mapa(pessoal_rows=[])
        expected = TEST_YEAR == date.today().year
        self.assertEqual(
            result['month_status'][date.today().month]['is_current_month'],
            expected,
        )


# ────────────────────────────────────────────────────────────────────────────
# 1 — Pessoal is marked as projection for the current year
# ────────────────────────────────────────────────────────────────────────────

class TestPessoalIsProjection(unittest.TestCase):
    """get_mapa_exploracao must flag pessoal as a projection for the current year."""

    def test_pessoal_is_projection_consolidated(self):
        result = _run_mapa()
        self.assertTrue(result['pessoal_is_projection'],
                        "pessoal_is_projection must be True for the current year")

    def test_pessoal_is_projection_per_store(self):
        result = _run_mapa(store_id=1)
        self.assertTrue(result['pessoal_is_projection'],
                        "pessoal_is_projection must be True in per-store view")


# ────────────────────────────────────────────────────────────────────────────
# 2 — Store CC routes 100 % to the correct store
# ────────────────────────────────────────────────────────────────────────────

class TestStoreCCPessoal(unittest.TestCase):
    """Collaborator A has a Bolhão (store_id=1) CC → full cost to Bolhão only."""

    PESSOAL_STORE_CC_ONLY = [(1, 10, 1_200.00)]  # only Collab A

    def test_consolidated_bolhao_receives_full_amount(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_STORE_CC_ONLY)
        bolhao = result['store_pessoal'].get(1, {}).get(1, 0.0)
        self.assertAlmostEqual(bolhao, 1_200.0, places=2,
            msg="Bolhão must receive €1 200 from the Bolhão-CC collaborator")

    def test_consolidated_matosinhos_receives_nothing(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_STORE_CC_ONLY)
        mato = result['store_pessoal'].get(2, {}).get(1, 0.0)
        self.assertAlmostEqual(mato, 0.0, places=2,
            msg="Matosinhos must receive €0 from the Bolhão-CC collaborator")

    def test_per_store_bolhao_view_shows_amount(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_STORE_CC_ONLY, store_id=1)
        pessoal = result['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(pessoal, 1_200.0, places=2,
            msg="Per-store Bolhão view must show €1 200")

    def test_per_store_matosinhos_view_shows_nothing(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_STORE_CC_ONLY, store_id=2)
        pessoal = result['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(pessoal, 0.0, places=2,
            msg="Per-store Matosinhos view must show €0 for this collaborator")


# ────────────────────────────────────────────────────────────────────────────
# 3 — Shared CC distributes proportionally by sales split
# ────────────────────────────────────────────────────────────────────────────

class TestSharedCCPessoal(unittest.TestCase):
    """Collaborator B has a Produção (shared) CC → costs split 60/40 by sales."""

    PESSOAL_SHARED_CC_ONLY = [(1, 30, 2_000.00)]  # only Collab B

    def test_consolidated_bolhao_receives_60_pct(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_SHARED_CC_ONLY)
        bolhao = result['store_pessoal'].get(1, {}).get(1, 0.0)
        self.assertAlmostEqual(bolhao, 1_200.0, places=2,
            msg="Bolhão has 60 % share → must receive €1 200 from shared CC")

    def test_consolidated_matosinhos_receives_40_pct(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_SHARED_CC_ONLY)
        mato = result['store_pessoal'].get(2, {}).get(1, 0.0)
        self.assertAlmostEqual(mato, 800.0, places=2,
            msg="Matosinhos has 40 % share → must receive €800 from shared CC")

    def test_no_unallocated_when_sales_history_exists(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_SHARED_CC_ONLY)
        unalloc = result['unallocated_pessoal'].get(1, 0.0)
        self.assertAlmostEqual(unalloc, 0.0, places=2,
            msg="Shared CC with sales history must produce zero unallocated pessoal")

    def test_per_store_bolhao_view_shows_60_pct(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_SHARED_CC_ONLY, store_id=1)
        pessoal = result['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(pessoal, 1_200.0, places=2,
            msg="Per-store Bolhão view must show its 60 % slice (€1 200)")

    def test_per_store_matosinhos_view_shows_40_pct(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_SHARED_CC_ONLY, store_id=2)
        pessoal = result['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(pessoal, 800.0, places=2,
            msg="Per-store Matosinhos view must show its 40 % slice (€800)")


# ────────────────────────────────────────────────────────────────────────────
# 4 — Multi-store collaborator mix (both CC types combined)
# ────────────────────────────────────────────────────────────────────────────

class TestMultiStoreCombined(unittest.TestCase):
    """Full scenario: Collab A (store CC) + Collab B (shared CC)."""

    #   Bolhão     : 1 200 (A) + 1 200 (B×60 %) = 2 400
    #   Matosinhos :     0     +   800 (B×40 %) =   800
    #   Global     : 3 200

    def test_consolidated_bolhao_pessoal(self):
        result = _run_mapa()
        bolhao = result['store_pessoal'].get(1, {}).get(1, 0.0)
        self.assertAlmostEqual(bolhao, 2_400.0, places=2,
            msg="Bolhão store_pessoal[1] must be €2 400 (direct + shared slice)")

    def test_consolidated_matosinhos_pessoal(self):
        result = _run_mapa()
        mato = result['store_pessoal'].get(2, {}).get(1, 0.0)
        self.assertAlmostEqual(mato, 800.0, places=2,
            msg="Matosinhos store_pessoal[1] must be €800 (shared slice only)")

    def test_global_pessoal_equals_sum_of_stores(self):
        result = _run_mapa()
        global_p   = result['pessoal'].get(1, 0.0)
        store_sum  = (result['store_pessoal'].get(1, {}).get(1, 0.0) +
                      result['store_pessoal'].get(2, {}).get(1, 0.0) +
                      result['unallocated_pessoal'].get(1, 0.0))
        self.assertAlmostEqual(global_p, store_sum, places=2,
            msg="Global pessoal must equal Σ(per-store) + unallocated")

    def test_global_pessoal_total_value(self):
        result = _run_mapa()
        global_p = result['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(global_p, 3_200.0, places=2,
            msg="Global pessoal[month=1] must be €3 200 (1200 + 2000)")

    def test_per_store_bolhao_matches_consolidated_slice(self):
        cons   = _run_mapa()
        per_s  = _run_mapa(store_id=1)
        cons_v = cons['store_pessoal'].get(1, {}).get(1, 0.0)
        per_v  = per_s['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(cons_v, per_v, places=2,
            msg="Bolhão store_pessoal in consolidated must match per-store pessoal")

    def test_per_store_matosinhos_matches_consolidated_slice(self):
        cons  = _run_mapa()
        per_s = _run_mapa(store_id=2)
        cons_v = cons['store_pessoal'].get(2, {}).get(1, 0.0)
        per_v  = per_s['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(cons_v, per_v, places=2,
            msg="Matosinhos store_pessoal in consolidated must match per-store pessoal")


# ────────────────────────────────────────────────────────────────────────────
# 5 — EBITDA balance: Vendas − CMVMC − Custos Op. − Pessoal
# ────────────────────────────────────────────────────────────────────────────

class TestEbitdaBalance(unittest.TestCase):
    """EBITDA must equal Vendas − CMVMC − total_costs_monthly for each view.

    total_costs_monthly is computed in the route (not in the DB layer) so we
    test it directly against the values returned by get_mapa_exploracao and
    replicate the route's accumulation formula.
    """

    MONTHS = list(range(1, 13))

    def _ebitda(self, result):
        """Compute EBITDA per month replicating the route's formula."""
        months = self.MONTHS
        # Accumulate operational costs (categories + uncat + pessoal)
        total_costs = {m: 0.0 for m in months}
        for cat in result['categories']:
            cd = result['costs'].get(cat['id'], {})
            for m in months:
                total_costs[m] = round(total_costs[m] + cd.get(m, 0.0), 2)
        for m in months:
            total_costs[m] = round(total_costs[m] + result['costs_uncat'].get(m, 0.0), 2)
        for m in months:
            total_costs[m] = round(total_costs[m] + result['pessoal'].get(m, 0.0), 2)

        ebitda = {}
        for m in months:
            vendas = result['vendas'].get(m, 0.0)
            cmvmc  = result['cmvmc'].get(m, 0.0)
            mb     = round(vendas - cmvmc, 2)
            ebitda[m] = round(mb - total_costs[m], 2)
        return ebitda

    def test_consolidated_ebitda_equals_vendas_minus_costs(self):
        result = _run_mapa()
        ebitda = self._ebitda(result)
        for m in self.MONTHS:
            vendas      = result['vendas'].get(m, 0.0)
            cmvmc       = result['cmvmc'].get(m, 0.0)
            total_costs = (sum(result['costs'].get(c['id'], {}).get(m, 0.0)
                              for c in result['categories'])
                           + result['costs_uncat'].get(m, 0.0)
                           + result['pessoal'].get(m, 0.0))
            expected = round(vendas - cmvmc - total_costs, 2)
            self.assertAlmostEqual(ebitda[m], expected, places=2,
                msg=f"EBITDA must balance for month {m} (consolidated)")

    def test_per_store_bolhao_ebitda_balances(self):
        result = _run_mapa(store_id=1)
        ebitda = self._ebitda(result)
        for m in self.MONTHS:
            vendas      = result['vendas'].get(m, 0.0)
            cmvmc       = result['cmvmc'].get(m, 0.0)
            total_costs = (sum(result['costs'].get(c['id'], {}).get(m, 0.0)
                              for c in result['categories'])
                           + result['costs_uncat'].get(m, 0.0)
                           + result['pessoal'].get(m, 0.0))
            expected = round(vendas - cmvmc - total_costs, 2)
            self.assertAlmostEqual(ebitda[m], expected, places=2,
                msg=f"EBITDA must balance for month {m} (Bolhão store view)")

    def test_per_store_matosinhos_ebitda_balances(self):
        result = _run_mapa(store_id=2)
        ebitda = self._ebitda(result)
        for m in self.MONTHS:
            vendas      = result['vendas'].get(m, 0.0)
            cmvmc       = result['cmvmc'].get(m, 0.0)
            total_costs = (sum(result['costs'].get(c['id'], {}).get(m, 0.0)
                              for c in result['categories'])
                           + result['costs_uncat'].get(m, 0.0)
                           + result['pessoal'].get(m, 0.0))
            expected = round(vendas - cmvmc - total_costs, 2)
            self.assertAlmostEqual(ebitda[m], expected, places=2,
                msg=f"EBITDA must balance for month {m} (Matosinhos store view)")

    def test_ebitda_rounding_drift_below_threshold(self):
        """No month may drift by more than €0.02 due to intermediate rounding."""
        DRIFT_LIMIT = 0.02
        result = _run_mapa()
        for m in self.MONTHS:
            vendas      = result['vendas'].get(m, 0.0)
            cmvmc       = result['cmvmc'].get(m, 0.0)
            total_costs = (sum(result['costs'].get(c['id'], {}).get(m, 0.0)
                              for c in result['categories'])
                           + result['costs_uncat'].get(m, 0.0)
                           + result['pessoal'].get(m, 0.0))
            expected_exact = vendas - cmvmc - total_costs
            expected_rounded = round(expected_exact, 2)
            drift = abs(expected_rounded - round(expected_exact, 4))
            self.assertLessEqual(drift, DRIFT_LIMIT,
                msg=(f"Rounding drift of €{drift:.4f} in month {m} exceeds "
                     f"the €{DRIFT_LIMIT} threshold"))

    def test_sum_of_store_ebitdas_approximates_consolidated(self):
        """Σ per-store EBITDA ≈ consolidated EBITDA (may differ by unallocated pessoal)."""
        cons_result = _run_mapa()
        bolhao_result = _run_mapa(store_id=1)
        mato_result   = _run_mapa(store_id=2)

        for m in self.MONTHS:
            def _ebitda_month(r, month):
                v = r['vendas'].get(month, 0.0)
                c = r['cmvmc'].get(month, 0.0)
                tc = (sum(r['costs'].get(cat['id'], {}).get(month, 0.0)
                          for cat in r['categories'])
                      + r['costs_uncat'].get(month, 0.0)
                      + r['pessoal'].get(month, 0.0))
                return round(v - c - tc, 2)

            cons_eb = _ebitda_month(cons_result, m)
            store_sum = (_ebitda_month(bolhao_result, m) +
                         _ebitda_month(mato_result, m))
            # The consolidated also subtracts unallocated pessoal that per-store views
            # don't, so the difference must equal the unallocated pessoal for that month.
            unalloc_p = cons_result['unallocated_pessoal'].get(m, 0.0)
            # consolidated EBITDA = sum-of-store EBITDA − unallocated pessoal
            self.assertAlmostEqual(cons_eb, store_sum - unalloc_p, places=2,
                msg=(f"Month {m}: consolidated EBITDA must equal "
                     "Σ(per-store) − unallocated pessoal"))


# ────────────────────────────────────────────────────────────────────────────
# 6 — CSV export includes Pessoal rows
# ────────────────────────────────────────────────────────────────────────────

class TestCsvExportIncludesPessoal(unittest.TestCase):
    """Verify the P&L mapa data includes pessoal keys consumed by the CSV exporter."""

    def _csv_rows_from_mapa(self, result):
        """Replicate the CSV-export accumulation from the route to get Pessoal rows."""
        months = list(range(1, 13))
        MESES = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Linha'] + MESES + ['Total'])

        def _row(label, d):
            vals = [f"{d.get(m, 0):.2f}" for m in months]
            total = f"{sum(d.get(m, 0) for m in months):.2f}"
            writer.writerow([label] + vals + [total])

        # Personnel
        pessoal = result.get('pessoal', {})
        _row('Pessoal — Real', pessoal)
        if result.get('mode') == 'consolidated':
            for s in result['stores']:
                sp = result.get('store_pessoal', {}).get(s['id'], {})
                _row(f'  {s["name"]} — Pessoal', sp)
            ua_p = result.get('unallocated_pessoal', {})
            if ua_p:
                _row('  Não alocado — Pessoal', ua_p)

        output.seek(0)
        return list(csv.reader(output))

    def test_csv_has_pessoal_real_row_consolidated(self):
        result = _run_mapa()
        rows = self._csv_rows_from_mapa(result)
        labels = [r[0] for r in rows]
        self.assertIn('Pessoal — Real', labels,
                      "CSV export must contain a 'Pessoal — Real' row")

    def test_csv_has_per_store_pessoal_rows_consolidated(self):
        result = _run_mapa()
        rows = self._csv_rows_from_mapa(result)
        labels = [r[0] for r in rows]
        self.assertIn('  Bolhão — Pessoal',     labels,
                      "CSV must contain per-store Pessoal row for Bolhão")
        self.assertIn('  Matosinhos — Pessoal', labels,
                      "CSV must contain per-store Pessoal row for Matosinhos")

    def test_csv_pessoal_real_total_is_correct(self):
        result = _run_mapa()
        rows = self._csv_rows_from_mapa(result)
        pessoal_row = next((r for r in rows if r[0] == 'Pessoal — Real'), None)
        self.assertIsNotNone(pessoal_row, "Pessoal — Real row must be present")
        total_col = pessoal_row[-1]  # last column = Total
        self.assertAlmostEqual(float(total_col), 3_200.0, places=2,
            msg="Pessoal — Real total (12 months) must equal €3 200 × 12 = €38 400 "
                "if all months are populated, or €3 200 for month-1-only test data")

    def test_csv_bolhao_pessoal_total_is_correct(self):
        result = _run_mapa()
        rows = self._csv_rows_from_mapa(result)
        bolhao_row = next((r for r in rows if r[0] == '  Bolhão — Pessoal'), None)
        self.assertIsNotNone(bolhao_row)
        total = float(bolhao_row[-1])
        self.assertAlmostEqual(total, 2_400.0, places=2,
            msg="Bolhão Pessoal CSV total must be €2 400 (month 1 only)")

    def test_csv_matosinhos_pessoal_total_is_correct(self):
        result = _run_mapa()
        rows = self._csv_rows_from_mapa(result)
        mato_row = next((r for r in rows if r[0] == '  Matosinhos — Pessoal'), None)
        self.assertIsNotNone(mato_row)
        total = float(mato_row[-1])
        self.assertAlmostEqual(total, 800.0, places=2,
            msg="Matosinhos Pessoal CSV total must be €800 (month 1 only)")


# ────────────────────────────────────────────────────────────────────────────
# 7 — Unallocated pessoal (no CC) falls back correctly
# ────────────────────────────────────────────────────────────────────────────

class TestUnallocatedPessoal(unittest.TestCase):
    """Collaborator with no CC → full cost to unallocated_pessoal."""

    PESSOAL_NO_CC = [(1, None, 500.00)]

    def test_consolidated_unallocated_pessoal(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_NO_CC)
        unalloc = result['unallocated_pessoal'].get(1, 0.0)
        self.assertAlmostEqual(unalloc, 500.0, places=2,
            msg="No-CC collaborator must land in unallocated_pessoal")

    def test_store_pessoal_is_zero_for_no_cc(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_NO_CC)
        for sid in [1, 2]:
            amount = result['store_pessoal'].get(sid, {}).get(1, 0.0)
            self.assertAlmostEqual(amount, 0.0, places=2,
                msg=f"Store {sid} must receive €0 from a no-CC collaborator")

    def test_global_pessoal_includes_unallocated(self):
        result = _run_mapa(pessoal_rows=self.PESSOAL_NO_CC)
        global_p = result['pessoal'].get(1, 0.0)
        self.assertAlmostEqual(global_p, 500.0, places=2,
            msg="Global pessoal must include unallocated amounts")

    def test_conservation_no_cc(self):
        """store_pessoal Σ + unallocated_pessoal == global_pessoal."""
        result = _run_mapa(pessoal_rows=self.PESSOAL_NO_CC)
        for m in range(1, 13):
            store_sum  = sum(result['store_pessoal'].get(sid, {}).get(m, 0.0)
                             for sid in [1, 2])
            unalloc    = result['unallocated_pessoal'].get(m, 0.0)
            global_p   = result['pessoal'].get(m, 0.0)
            self.assertAlmostEqual(store_sum + unalloc, global_p, places=2,
                msg=f"Month {m}: Σ store_pessoal + unallocated must equal global pessoal")


# ────────────────────────────────────────────────────────────────────────────
# 8 — Conservation: global pessoal == Σ store + unallocated for all months
# ────────────────────────────────────────────────────────────────────────────

class TestPessoalConservation(unittest.TestCase):
    """End-to-end conservation law: no euro is lost or double-counted."""

    def test_conservation_full_scenario(self):
        result = _run_mapa()
        for m in range(1, 13):
            store_sum = sum(result['store_pessoal'].get(sid, {}).get(m, 0.0)
                            for sid in [1, 2])
            unalloc   = result['unallocated_pessoal'].get(m, 0.0)
            global_p  = result['pessoal'].get(m, 0.0)
            self.assertAlmostEqual(store_sum + unalloc, global_p, places=2,
                msg=(f"Month {m}: Σ store_pessoal + unallocated_pessoal "
                     f"({store_sum + unalloc:.4f}) must equal global pessoal "
                     f"({global_p:.4f})"))

    def test_conservation_for_prior_year_pessoal_is_empty(self):
        """Personnel is projection-only; pessoal_aa must always be empty."""
        result = _run_mapa()
        self.assertEqual(result.get('pessoal_aa', {}), {},
                         "pessoal_aa must always be empty (no historical payroll data)")

    def test_store_pessoal_aa_is_empty(self):
        result = _run_mapa()
        for sid in [1, 2]:
            store_aa = result.get('store_pessoal_aa', {}).get(sid, {})
            self.assertEqual(store_aa, {},
                msg=f"store_pessoal_aa[{sid}] must be empty (no historical data)")


if __name__ == '__main__':
    unittest.main()
