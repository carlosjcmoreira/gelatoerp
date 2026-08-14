"""
Regression test: get_produto_names_by_activity() sources products from both
vendas_detalhe and sales_historico.

Verifies that:
1. A product present only in sales_historico (historical/pre-Gestor data) with
   data only in prior years appears in the 'historic' list.
2. A product present in vendas_detalhe with current-year data appears in the
   'active' list.
3. A product present in sales_historico with current-year data is also treated
   as 'active' (not silently dropped to historic).
4. The returned lists are sorted alphabetically.

These checks guard against regressions where products from sales_historico are
accidentally excluded from the alias creation form's "Só em anos anteriores"
dropdown.

Run with:
    python -m pytest tests/test_produto_names_by_activity.py -v
"""

import datetime
import unittest
from unittest.mock import MagicMock, patch


def _make_cursor(rows):
    """Return a mock cursor whose fetchall() returns the given rows."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    return cur


def _make_conn(cursor):
    """Return a mock connection that yields the given cursor."""
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# The year used by get_produto_names_by_activity() to decide active vs historic
# is datetime.date.today().year.  We freeze it to 2026 so tests are
# deterministic regardless of when they run.
# ---------------------------------------------------------------------------
_CURRENT_YEAR = 2026
_PRIOR_YEAR = _CURRENT_YEAR - 1


class TestGetProdutoNamesByActivity(unittest.TestCase):
    """Unit tests for get_produto_names_by_activity() using a DB mock fixture."""

    def _call(self, db_rows):
        """
        Invoke get_produto_names_by_activity() with db_connection mocked to
        return *db_rows* as the result of the UNION ALL query.

        db_rows: list of (produto, max_year) tuples – matching what the SQL
                 SELECT returns after GROUP BY produto.
        """
        cur = _make_cursor(db_rows)
        conn = _make_conn(cur)

        frozen_today = datetime.date(_CURRENT_YEAR, 6, 15)

        with patch('db.vendas_diarias.db_connection', return_value=conn), \
             patch('datetime.date') as mock_date:
            # date.today() must return the frozen date; date.fromisocalendar()
            # and other class methods must still work normally.
            mock_date.today.return_value = frozen_today
            mock_date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)

            from db.vendas_diarias import get_produto_names_by_activity
            return get_produto_names_by_activity()

    # ------------------------------------------------------------------
    # Core scenario: historic-only product (exclusively in sales_historico)
    # ------------------------------------------------------------------

    def test_historic_only_product_appears_in_historic_list(self):
        """
        A product whose most-recent sale is in a prior year must appear in
        the 'historic' list — even when it only exists in sales_historico
        (i.e. it was never uploaded to vendas_detalhe).

        This is the primary regression scenario: before the data-merge fix,
        products exclusive to sales_historico were silently absent from the
        alias selector.
        """
        rows = [
            ('Produto Histórico',  _PRIOR_YEAR),   # only in sales_historico
        ]
        result = self._call(rows)

        self.assertIn('Produto Histórico', result['historic'],
                      "Product from sales_historico (prior year only) must be in 'historic'")
        self.assertNotIn('Produto Histórico', result['active'])

    # ------------------------------------------------------------------
    # Current-year product in vendas_detalhe
    # ------------------------------------------------------------------

    def test_current_year_product_appears_in_active_list(self):
        """
        A product with sales in the current year (in vendas_detalhe) must
        appear in the 'active' list, not 'historic'.
        """
        rows = [
            ('Produto Atual', _CURRENT_YEAR),
        ]
        result = self._call(rows)

        self.assertIn('Produto Atual', result['active'],
                      "Product with current-year data must be in 'active'")
        self.assertNotIn('Produto Atual', result['historic'])

    # ------------------------------------------------------------------
    # Combined fixture: both active and historic products
    # ------------------------------------------------------------------

    def test_mixed_fixture_active_and_historic_separated_correctly(self):
        """
        Given a mix of current-year and prior-year products (from both tables),
        the function must split them into 'active' and 'historic' without
        dropping any product.
        """
        rows = [
            # Active: current-year data in vendas_detalhe
            ('Morango',             _CURRENT_YEAR),
            ('Pistacchio',          _CURRENT_YEAR),
            # Historic: only in sales_historico, all data prior years
            ('Baunilha Antiga',     _PRIOR_YEAR),
            ('Chocolate Vintage',   _PRIOR_YEAR - 1),
        ]
        result = self._call(rows)

        # Active
        self.assertIn('Morango',    result['active'])
        self.assertIn('Pistacchio', result['active'])

        # Historic (from sales_historico)
        self.assertIn('Baunilha Antiga',   result['historic'],
                      "sales_historico-only product must appear in 'historic'")
        self.assertIn('Chocolate Vintage', result['historic'],
                      "sales_historico-only product must appear in 'historic'")

        # Nothing misclassified
        self.assertNotIn('Baunilha Antiga',   result['active'])
        self.assertNotIn('Chocolate Vintage', result['active'])
        self.assertNotIn('Morango',           result['historic'])
        self.assertNotIn('Pistacchio',        result['historic'])

    # ------------------------------------------------------------------
    # Historic product in sales_historico at exact current-year boundary
    # ------------------------------------------------------------------

    def test_product_with_current_year_data_in_historico_is_active(self):
        """
        A product present in sales_historico but with data in the current year
        must be treated as 'active' (max_year >= current_year).
        """
        rows = [
            ('Produto Historico Recente', _CURRENT_YEAR),
        ]
        result = self._call(rows)

        self.assertIn('Produto Historico Recente', result['active'])
        self.assertNotIn('Produto Historico Recente', result['historic'])

    # ------------------------------------------------------------------
    # Result lists are sorted alphabetically
    # ------------------------------------------------------------------

    def test_active_list_is_sorted(self):
        """The 'active' list must be sorted alphabetically."""
        rows = [
            ('Zebra', _CURRENT_YEAR),
            ('Amora', _CURRENT_YEAR),
            ('Mirtilo', _CURRENT_YEAR),
        ]
        result = self._call(rows)
        self.assertEqual(result['active'], sorted(result['active']),
                         "active list must be sorted alphabetically")

    def test_historic_list_is_sorted(self):
        """The 'historic' list must be sorted alphabetically."""
        rows = [
            ('Zebra Antiga',  _PRIOR_YEAR),
            ('Amora Antiga',  _PRIOR_YEAR),
            ('Mirtilo Antigo', _PRIOR_YEAR),
        ]
        result = self._call(rows)
        self.assertEqual(result['historic'], sorted(result['historic']),
                         "historic list must be sorted alphabetically")

    # ------------------------------------------------------------------
    # Empty DB
    # ------------------------------------------------------------------

    def test_empty_db_returns_empty_lists(self):
        """With no rows in either table, both lists must be empty."""
        result = self._call([])
        self.assertEqual(result['active'],   [])
        self.assertEqual(result['historic'], [])

    # ------------------------------------------------------------------
    # Return shape
    # ------------------------------------------------------------------

    def test_return_value_has_active_and_historic_keys(self):
        """The returned dict must always have 'active' and 'historic' keys."""
        result = self._call([])
        self.assertIn('active',   result)
        self.assertIn('historic', result)


if __name__ == '__main__':
    unittest.main()
