"""
Regression tests — invoice search code path.

Verifies that:
1. _build_invoice_where with search= generates valid SQL that uses
   correlated-subquery aliases (_sup / _st) and does NOT reference bare
   table aliases (e.g. "st") that would cause a Postgres
   "missing FROM-clause entry for table" error.
2. get_invoices(search=...) executes without raising an exception and
   returns a list.
3. count_invoices(search=...) executes without raising an exception and
   returns a non-negative integer.
4. Both functions work correctly when a supplier filter is also active
   (supplier_name="Fornecedor X", search="text").

All DB I/O is mocked — no live database required.

Run with:
    python -m unittest tests.test_invoice_search -v
"""
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cursor(fetchall=None, fetchone=None):
    cur = MagicMock()
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.fetchone.return_value = fetchone if fetchone is not None else (0,)
    cur.rowcount = 0
    return cur


def _make_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# 1. _build_invoice_where — SQL shape checks
# ---------------------------------------------------------------------------

class TestBuildInvoiceWhereSearch(unittest.TestCase):
    """_build_invoice_where with search= must produce safe correlated-subquery SQL."""

    def _call(self, **kwargs):
        from db.faturas import _build_invoice_where
        return _build_invoice_where(**kwargs)

    def test_search_adds_where_clause(self):
        where_clause, params = self._call(search="acme")
        self.assertIn("WHERE", where_clause)
        self.assertTrue(any("acme" in str(p).lower() for p in params),
                        "Search pattern '%acme%' should appear in params")

    def test_search_uses_correlated_subquery_not_bare_alias(self):
        """The store match must use a correlated subquery (EXISTS …) not a bare JOIN alias.

        The old bug referenced table alias 'st' from an outer query — that caused
        'missing FROM-clause entry for table "st"'.  The fix uses _sup/_st inside
        the subquery.  We verify:
        - EXISTS keyword present (subquery form)
        - '_sup' or '_st' alias present (not the bare 'st' alias that caused the crash)
        - 'st.' does NOT appear as a bare reference (would indicate the old bug)
        """
        where_clause, params = self._call(search="test store")
        self.assertIn("EXISTS", where_clause,
                      "Store match should use a correlated subquery (EXISTS)")
        # The fix aliases the joined table as _st (and supplier as _sup)
        self.assertIn("_sup", where_clause,
                      "_sup subquery alias must appear in the store-match subquery")
        self.assertIn("_st", where_clause,
                      "_st subquery alias must appear in the store-match subquery")

    def test_search_no_bare_st_alias(self):
        """'st.' must not appear as a bare reference that would require an outer JOIN."""
        import re
        where_clause, params = self._call(search="anything")
        # Allow '_st.' (the safe alias) but flag bare 'st.' without an underscore prefix
        bare_st = re.search(r'(?<![_a-z])st\.', where_clause)
        self.assertIsNone(bare_st,
                          f"Found bare 'st.' alias in WHERE clause — this would crash Postgres: {where_clause!r}")

    def test_search_includes_supplier_name_match(self):
        """When supplier is not pinned, supplier_name LIKE must be in the clause."""
        where_clause, params = self._call(search="acme")
        self.assertIn("supplier_name", where_clause.lower())

    def test_search_with_supplier_pinned_skips_supplier_name_like(self):
        """When supplier_name filter is active, the top-level supplier_name LIKE is redundant
        and must be absent (search only covers invoice_number, notes, document_type, store)."""
        where_clause, params = self._call(search="inv-001", supplier_name="Acme Lda")
        # The general LOWER(i.supplier_name) LIKE %s match should be absent
        # because the supplier is already pinned; only invoice_number / notes / doc_type / store
        self.assertNotIn("i.supplier_name) LIKE", where_clause,
                         "Supplier-name LIKE should be omitted when supplier is pinned via filter")

    def test_search_correct_param_count_no_supplier(self):
        """Without a pinned supplier, 5 LIKE params (supplier, number, notes, doc_type, store)."""
        _, params_no_search = _build_where_helper()
        where_clause, params = _build_where_helper(search="hello")
        extra = len(params) - len(params_no_search)
        self.assertEqual(extra, 5,
                         f"Expected 5 extra params for search without supplier filter, got {extra}")

    def test_search_correct_param_count_with_supplier(self):
        """With a pinned supplier, 4 LIKE params (number, notes, doc_type, store)."""
        _, params_no_search = _build_where_helper(supplier_name="Acme")
        where_clause, params = _build_where_helper(search="hello", supplier_name="Acme")
        # subtract the 1 param from supplier_name filter itself
        supplier_params_baseline = len(params_no_search)
        extra = len(params) - supplier_params_baseline
        self.assertEqual(extra, 4,
                         f"Expected 4 extra params for search with supplier filter, got {extra}")


def _build_where_helper(**kwargs):
    from db.faturas import _build_invoice_where
    return _build_invoice_where(**kwargs)


# ---------------------------------------------------------------------------
# 1b. Cost-centre filters and sorting
# ---------------------------------------------------------------------------

class TestCostCenterInvoiceFilters(unittest.TestCase):
    """Cost filters must use the same reliable query path as other columns."""

    def test_sem_cc_excludes_primary_and_split_cost_centers(self):
        from db.faturas import _build_invoice_where

        where_clause, _ = _build_invoice_where(sem_cc=True)

        self.assertIn("i.centro_custo_id IS NULL", where_clause)
        self.assertIn("NOT EXISTS", where_clause)
        self.assertIn("invoice_centros_custo", where_clause)

    def test_cost_category_filter_is_applied(self):
        from db.faturas import _build_invoice_where

        where_clause, params = _build_invoice_where(categoria_custo_id=42)

        self.assertIn("i.categoria_custo_id = %s", where_clause)
        self.assertIn(42, params)

    def test_cost_column_sort_keys_are_supported_by_get_invoices(self):
        cur = _make_cursor(fetchall=[])
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import get_invoices
            get_invoices(order_by='centro_custo_name')
        self.assertIn("ORDER BY centro_custo_name ASC", cur.execute.call_args[0][0])
        self.assertNotIn("LOWER(centro_custo_name)", cur.execute.call_args[0][0])

        cur = _make_cursor(fetchall=[])
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import get_invoices
            get_invoices(order_by='categoria_custo_name', order_dir='desc')
        self.assertIn("ORDER BY LOWER(ccat.name) DESC", cur.execute.call_args[0][0])


# ---------------------------------------------------------------------------
# 2. get_invoices — executes without exception, returns list
# ---------------------------------------------------------------------------

class TestGetInvoicesSearch(unittest.TestCase):
    """get_invoices(search=...) must not raise and must return a list."""

    def _run(self, **kwargs):
        cur = _make_cursor(fetchall=[])
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import get_invoices
            return get_invoices(**kwargs)

    def test_search_returns_list(self):
        result = self._run(search="acme")
        self.assertIsInstance(result, list)

    def test_search_empty_string_returns_list(self):
        result = self._run(search="")
        self.assertIsInstance(result, list)

    def test_search_with_supplier_pinned_returns_list(self):
        result = self._run(search="inv-001", supplier_name="Acme Lda")
        self.assertIsInstance(result, list)

    def test_search_sql_sent_to_db_contains_exists(self):
        """The SQL sent to the DB must use EXISTS for the store match, not a bare alias."""
        cur = _make_cursor(fetchall=[])
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import get_invoices
            get_invoices(search="bakery")
        executed_sql = cur.execute.call_args[0][0]
        self.assertIn("EXISTS", executed_sql,
                      "get_invoices SQL must use EXISTS for the store correlated subquery")

    def test_search_no_bare_st_alias_in_executed_sql(self):
        """The SQL actually executed must not contain a bare 'st.' alias."""
        import re
        cur = _make_cursor(fetchall=[])
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import get_invoices
            get_invoices(search="anything")
        executed_sql = cur.execute.call_args[0][0]
        bare_st = re.search(r'(?<![_a-z])st\.', executed_sql)
        self.assertIsNone(bare_st,
                          f"Bare 'st.' alias found in executed SQL — would crash Postgres: {executed_sql!r}")

    def test_search_with_supplier_names_list_returns_list(self):
        result = self._run(search="invoice", supplier_names=["Acme Lda", "Beta SA"])
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# 3. count_invoices — executes without exception, returns int >= 0
# ---------------------------------------------------------------------------

class TestCountInvoicesSearch(unittest.TestCase):
    """count_invoices(search=...) must not raise and must return a non-negative int."""

    def _run(self, **kwargs):
        cur = _make_cursor(fetchone=(0,))
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import count_invoices
            return count_invoices(**kwargs)

    def test_search_returns_int(self):
        result = self._run(search="acme")
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    def test_search_empty_string_returns_int(self):
        result = self._run(search="")
        self.assertIsInstance(result, int)

    def test_search_with_supplier_pinned_returns_int(self):
        result = self._run(search="inv-001", supplier_name="Acme Lda")
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    def test_search_sql_sent_to_db_contains_exists(self):
        cur = _make_cursor(fetchone=(0,))
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import count_invoices
            count_invoices(search="bakery")
        executed_sql = cur.execute.call_args[0][0]
        self.assertIn("EXISTS", executed_sql,
                      "count_invoices SQL must use EXISTS for the store correlated subquery")

    def test_search_no_bare_st_alias_in_executed_sql(self):
        import re
        cur = _make_cursor(fetchone=(0,))
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import count_invoices
            count_invoices(search="anything")
        executed_sql = cur.execute.call_args[0][0]
        bare_st = re.search(r'(?<![_a-z])st\.', executed_sql)
        self.assertIsNone(bare_st,
                          f"Bare 'st.' alias found in executed SQL — would crash Postgres: {executed_sql!r}")

    def test_search_with_supplier_names_list_returns_int(self):
        result = self._run(search="invoice", supplier_names=["Acme Lda", "Beta SA"])
        self.assertIsInstance(result, int)


if __name__ == '__main__':
    unittest.main()
