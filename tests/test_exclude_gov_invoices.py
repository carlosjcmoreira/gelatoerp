"""
Smoke / unit tests — AT/SS tax documents excluded from Compras-facing and grouped views.

Verifies that:
1. _build_invoice_where appends the NIF exclusion clause when exclude_gov=True.
2. get_contas_por_fornecedor accepts exclude_gov and injects the exclusion SQL.
3. get_invoices / count_invoices pass exclude_gov through to _build_invoice_where.
4. The Financeiro route's SQL summary/detail group helpers are called with
   exclude_gov=True.
5. The Compras document-list view calls get_invoices / count_invoices with exclude_gov=True.

Run with:
    python -m unittest tests.test_exclude_gov_invoices -v
"""
import unittest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cursor(fetchall=None, fetchone=None):
    cur = MagicMock()
    cur.fetchall.return_value = fetchall or []
    cur.fetchone.return_value = fetchone
    cur.rowcount = 0
    return cur


def _make_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# 1. _build_invoice_where
# ---------------------------------------------------------------------------

class TestBuildInvoiceWhereExcludeGov(unittest.TestCase):
    """_build_invoice_where appends a NIF exclusion clause only when exclude_gov=True."""

    def _call(self, **kwargs):
        from db.faturas import _build_invoice_where, _GOV_NIFS
        return _build_invoice_where(**kwargs), _GOV_NIFS

    def test_exclude_gov_false_no_clause(self):
        (where_clause, params), _ = self._call(exclude_gov=False)
        self.assertNotIn('entidade_governamental', where_clause)
        self.assertNotIn('NOT IN', where_clause)

    def test_exclude_gov_true_adds_nif_clause(self):
        (where_clause, params), gov_nifs = self._call(exclude_gov=True)
        self.assertIn('supplier_nif', where_clause)
        self.assertIn('entidade_governamental', where_clause)
        self.assertIn(gov_nifs, params)

    def test_gov_nifs_contains_at_and_ss(self):
        """AT NIF (500757155) and SS NIF (506826066) must be in _GOV_NIFS."""
        from db.faturas import _GOV_NIFS
        self.assertIn('500757155', _GOV_NIFS)
        self.assertIn('506826066', _GOV_NIFS)


# ---------------------------------------------------------------------------
# 2. get_contas_por_fornecedor
# ---------------------------------------------------------------------------

class TestGetContasPorFornecedorExcludeGov(unittest.TestCase):
    """get_contas_por_fornecedor injects the gov exclusion SQL when exclude_gov=True."""

    def _run(self, exclude_gov: bool):
        cur = _make_cursor(fetchall=[])
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import get_contas_por_fornecedor
            get_contas_por_fornecedor(exclude_gov=exclude_gov)
        call_args = cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1] if len(call_args[0]) > 1 else []
        return sql, params

    def test_exclude_gov_false_no_nif_filter(self):
        sql, params = self._run(exclude_gov=False)
        self.assertNotIn('NOT IN', sql)
        self.assertNotIn('entidade_governamental', sql)

    def test_exclude_gov_true_includes_nif_clause(self):
        from db.faturas import _GOV_NIFS
        sql, params = self._run(exclude_gov=True)
        self.assertIn('supplier_nif', sql)
        self.assertIn('entidade_governamental', sql)
        self.assertIn(_GOV_NIFS, params)

    def test_exclude_gov_true_at_nif_in_params(self):
        """AT NIF 500757155 must be present in the tuple passed to the query."""
        _, params = self._run(exclude_gov=True)
        gov_tuple = next((p for p in params if isinstance(p, tuple)), None)
        self.assertIsNotNone(gov_tuple, "Expected _GOV_NIFS tuple in params")
        self.assertIn('500757155', gov_tuple)

    def test_exclude_gov_true_ss_nif_in_params(self):
        """SS NIF 506826066 must be present in the tuple passed to the query."""
        _, params = self._run(exclude_gov=True)
        gov_tuple = next((p for p in params if isinstance(p, tuple)), None)
        self.assertIsNotNone(gov_tuple, "Expected _GOV_NIFS tuple in params")
        self.assertIn('506826066', gov_tuple)


# ---------------------------------------------------------------------------
# 3. get_invoices / count_invoices pass-through
# ---------------------------------------------------------------------------

class TestGetInvoicesExcludeGovPassthrough(unittest.TestCase):
    """get_invoices and count_invoices pass exclude_gov through to _build_invoice_where."""

    def _call_get_invoices(self, exclude_gov: bool):
        cur = _make_cursor(fetchall=[])
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import get_invoices
            get_invoices(exclude_gov=exclude_gov)
        call_args = cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1] if len(call_args[0]) > 1 else []
        return sql, params

    def _call_count_invoices(self, exclude_gov: bool):
        cur = _make_cursor(fetchone=(0,))
        conn = _make_conn(cur)
        with patch('db.faturas.db_connection', return_value=conn):
            from db.faturas import count_invoices
            count_invoices(exclude_gov=exclude_gov)
        call_args = cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1] if len(call_args[0]) > 1 else []
        return sql, params

    def test_get_invoices_exclude_gov_true_has_nif_clause(self):
        from db.faturas import _GOV_NIFS
        sql, params = self._call_get_invoices(exclude_gov=True)
        self.assertIn('supplier_nif', sql)
        self.assertIn(_GOV_NIFS, params)

    def test_get_invoices_exclude_gov_false_no_nif_clause(self):
        sql, params = self._call_get_invoices(exclude_gov=False)
        self.assertNotIn('NOT IN', sql)
        self.assertNotIn('entidade_governamental', sql)

    def test_count_invoices_exclude_gov_true_has_nif_clause(self):
        from db.faturas import _GOV_NIFS
        sql, params = self._call_count_invoices(exclude_gov=True)
        self.assertIn('supplier_nif', sql)
        self.assertIn(_GOV_NIFS, params)

    def test_count_invoices_exclude_gov_false_no_nif_clause(self):
        sql, params = self._call_count_invoices(exclude_gov=False)
        self.assertNotIn('NOT IN', sql)
        self.assertNotIn('entidade_governamental', sql)


# ---------------------------------------------------------------------------
# 4. Financeiro route group views call underlying functions with exclude_gov=True
# ---------------------------------------------------------------------------

def _make_app():
    """Return a Flask test app with TESTING=True and no CSRF."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from flask_app.app import create_app
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SECRET_KEY'] = 'test-secret'
    return app


def _set_financeiro_session(client):
    """Set a session that passes the acesso_financeiro permission check."""
    with client.session_transaction() as sess:
        # auth.py checks user.get('acesso_financeiro') directly — not a 'perms' list
        sess['user'] = {'username': 'test_fin', 'acesso_financeiro': True}


def _set_compras_session(client):
    """Set a session that passes the acesso_compras permission check."""
    with client.session_transaction() as sess:
        sess['user'] = {'username': 'test_cpr', 'acesso_compras': True}


class TestFinanceiroGroupViewsExcludeGov(unittest.TestCase):
    """
    The Financeiro route's Por Fornecedor, Por Centro de Custo, and Por Categoria
    views must invoke their query helpers with exclude_gov=True so AT/SS tax entities
    are absent from grouped results.
    """

    def test_fornecedor_view_calls_get_contas_with_exclude_gov(self):
        """view=fornecedor must call get_contas_por_fornecedor(exclude_gov=True)."""
        app = _make_app()
        with app.test_client() as client:
            _set_financeiro_session(client)
            # get_contas_por_fornecedor is imported from `database` into the route
            # module's namespace; patching that binding is sufficient.
            # get_paid_counts_by_supplier is locally imported inside the function so
            # patch it at the db.faturas source level.
            with patch('flask_app.routes.faturas.get_contas_por_fornecedor',
                       return_value=[]) as mock_fn, \
                 patch('flask_app.routes.faturas.get_confirming_contracts', return_value=[]), \
                 patch('flask_app.routes.faturas.get_payment_methods_config', return_value=[]), \
                 patch('flask_app.routes.faturas.get_invoice_status_labels_map', return_value={}), \
                 patch('db.faturas.get_paid_counts_by_supplier', return_value={}), \
                 patch('flask_app.routes.faturas.render_template', return_value=''):
                client.get('/financeiro/faturas/?view=fornecedor')
                self.assertTrue(
                    any(c.kwargs.get('exclude_gov') is True for c in mock_fn.call_args_list),
                    f"get_contas_por_fornecedor calls: {mock_fn.call_args_list}"
                )

    def test_centro_custo_view_calls_group_queries_with_exclude_gov(self):
        """view=centro_custo must exclude government rows in both bounded queries."""
        app = _make_app()
        with app.test_client() as client:
            _set_financeiro_session(client)
            with patch('flask_app.routes.faturas.get_invoice_group_summaries',
                       return_value=[]) as mock_summary, \
                 patch('flask_app.routes.faturas.get_confirming_contracts', return_value=[]), \
                 patch('flask_app.routes.faturas.get_payment_methods_config', return_value=[]), \
                 patch('flask_app.routes.faturas.get_cost_centers', return_value=[]), \
                 patch('flask_app.routes.faturas.render_template', return_value=''):
                client.get('/financeiro/faturas/?view=centro_custo')
                self.assertTrue(mock_summary.call_args.kwargs['exclude_gov'])

    def test_categoria_custo_view_calls_group_queries_with_exclude_gov(self):
        """view=categoria_custo must exclude government rows in both bounded queries."""
        app = _make_app()
        with app.test_client() as client:
            _set_financeiro_session(client)
            with patch('flask_app.routes.faturas.get_invoice_group_summaries',
                       return_value=[]) as mock_summary, \
                 patch('flask_app.routes.faturas.get_confirming_contracts', return_value=[]), \
                 patch('flask_app.routes.faturas.get_payment_methods_config', return_value=[]), \
                 patch('flask_app.routes.faturas.render_template', return_value=''):
                client.get('/financeiro/faturas/?view=categoria_custo')
                self.assertTrue(mock_summary.call_args.kwargs['exclude_gov'])


class TestGroupedViewPagination(unittest.TestCase):
    def test_out_of_range_supplier_page_redirects_to_last_page(self):
        app = _make_app()
        with app.test_client() as client:
            _set_financeiro_session(client)
            with patch(
                'flask_app.routes.faturas.get_contas_por_fornecedor',
                side_effect=[[], [{'total_groups': 41}]],
            ) as mock_groups, \
                 patch('flask_app.routes.faturas.get_confirming_contracts', return_value=[]), \
                 patch('flask_app.routes.faturas.get_payment_methods_config', return_value=[]):
                response = client.get(
                    '/financeiro/faturas/?view=fornecedor&group_page=999',
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 302)
        self.assertIn('group_page=3', response.headers['Location'])
        self.assertEqual(mock_groups.call_count, 2)


# ---------------------------------------------------------------------------
# 5. Compras document-list uses exclude_gov=True
# ---------------------------------------------------------------------------

class TestComprasDocumentListExcludesGov(unittest.TestCase):
    """
    The Compras /compras/faturas view must call get_invoices and count_invoices
    with exclude_gov=True so AT/SS tax rows never appear in Compras-facing results.
    """

    def test_compras_faturas_calls_get_invoices_with_exclude_gov(self):
        """The Compras invoice list must call get_invoices(exclude_gov=True)."""
        app = _make_app()
        with app.test_client() as client:
            _set_compras_session(client)
            with patch('flask_app.routes.compras.get_invoices', return_value=[]) as mock_gi, \
                 patch('flask_app.routes.compras.count_invoices', return_value=0), \
                 patch('flask_app.routes.compras.get_payment_methods_config', return_value=[]), \
                 patch('flask_app.routes.compras.get_distinct_supplier_names', return_value=[]), \
                 patch('flask_app.routes.compras.get_stores_list', return_value=[]), \
                 patch('flask_app.routes.compras.get_invoice_status_labels_map', return_value={}), \
                 patch('flask_app.routes.compras.render_template', return_value=''):
                client.get('/compras/faturas')
                self.assertTrue(
                    any(c.kwargs.get('exclude_gov') is True for c in mock_gi.call_args_list),
                    f"get_invoices calls: {mock_gi.call_args_list}"
                )

    def test_compras_faturas_calls_count_invoices_with_exclude_gov(self):
        """The Compras invoice count must use exclude_gov=True."""
        app = _make_app()
        with app.test_client() as client:
            _set_compras_session(client)
            with patch('flask_app.routes.compras.count_invoices', return_value=0) as mock_ci, \
                 patch('flask_app.routes.compras.get_invoices', return_value=[]), \
                 patch('flask_app.routes.compras.get_payment_methods_config', return_value=[]), \
                 patch('flask_app.routes.compras.get_distinct_supplier_names', return_value=[]), \
                 patch('flask_app.routes.compras.get_stores_list', return_value=[]), \
                 patch('flask_app.routes.compras.get_invoice_status_labels_map', return_value={}), \
                 patch('flask_app.routes.compras.render_template', return_value=''):
                client.get('/compras/faturas')
                self.assertTrue(
                    any(c.kwargs.get('exclude_gov') is True for c in mock_ci.call_args_list),
                    f"count_invoices calls: {mock_ci.call_args_list}"
                )


if __name__ == '__main__':
    unittest.main()
