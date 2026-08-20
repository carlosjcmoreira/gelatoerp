"""
Regression tests for invoice list state across bulk edits and Compras filters.

Run with:
    python -m unittest tests.test_invoice_list_state -v
"""

import unittest
from unittest.mock import patch

from flask import Blueprint, Flask


def _user(**overrides):
    return {
        'id': 1,
        'username': 'testuser',
        'role': 'gestor',
        'acesso_gestor': True,
        'acesso_administrativo': False,
        'acesso_compras': True,
        **overrides,
    }


def _support_blueprints(app):
    auth_bp = Blueprint('auth', __name__)

    @auth_bp.route('/login')
    def login():
        return 'login', 200

    home_bp = Blueprint('home', __name__)

    @home_bp.route('/')
    def index():
        return 'home', 200

    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)


class TestBulkActionReturnUrl(unittest.TestCase):
    """Bulk edits must return to the exact list URL that initiated them."""

    @classmethod
    def setUpClass(cls):
        from flask_app.routes.faturas import faturas_bp

        cls.app = Flask(__name__)
        cls.app.secret_key = 'test-secret-key'
        cls.app.config['TESTING'] = True
        _support_blueprints(cls.app)
        cls.app.register_blueprint(faturas_bp, url_prefix='/financeiro/faturas')

    def test_assigning_cost_center_keeps_all_list_parameters(self):
        return_url = (
            '/financeiro/faturas/?sem_cc=1&centro_custo_id=8'
            '&categoria_custo_id=4&order_by=centro_custo_name'
            '&order_dir=asc&page=3&q=farinha'
        )
        with patch('flask_app.routes.faturas.update_invoice') as update:
            client = self.app.test_client()
            with client.session_transaction() as session:
                session['user'] = _user()

            response = client.post('/financeiro/faturas/bulk', data={
                'ids': ['101', '102'],
                'action': 'assign_field',
                'field': 'centro_custo_id',
                'value': '8',
                'return_url': return_url,
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], return_url)
        self.assertEqual(update.call_count, 2)

    def test_assigning_from_compras_returns_to_compras_list(self):
        return_url = (
            '/compras/faturas?sem_cc=1&categoria_custo_id=4'
            '&order_by=categoria_custo_name&order_dir=desc&page=2'
        )
        with patch('flask_app.routes.faturas.update_invoice'):
            client = self.app.test_client()
            with client.session_transaction() as session:
                session['user'] = _user()

            response = client.post('/financeiro/faturas/bulk', data={
                'ids': ['103'],
                'action': 'assign_field',
                'field': 'categoria_custo_id',
                'value': '4',
                'return_url': return_url,
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], return_url)

    def test_template_uses_relative_request_path_for_bulk_return_urls(self):
        with open('flask_app/templates/financeiro/faturas/index.html', encoding='utf-8') as template:
            source = template.read()

        self.assertNotIn('name="return_url" value="{{ request.url }}"', source)
        self.assertEqual(source.count('name="return_url" value="{{ request.full_path }}"'), 4)


class TestComprasFilterState(unittest.TestCase):
    """Compras must forward and remember the same cost filters as Financeiro."""

    @classmethod
    def setUpClass(cls):
        from flask_app.routes.compras import compras_bp

        cls.app = Flask(__name__)
        cls.app.secret_key = 'test-secret-key'
        cls.app.config['TESTING'] = True
        _support_blueprints(cls.app)
        cls.app.register_blueprint(compras_bp, url_prefix='/compras')

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = _user()

        self.patches = [
            patch('flask_app.routes.compras.count_invoices', return_value=0),
            patch('flask_app.routes.compras.get_invoices', return_value=[]),
            patch('flask_app.routes.compras.get_payment_methods_config', return_value=[]),
            patch('flask_app.routes.compras.get_distinct_supplier_names', return_value=[]),
            patch('flask_app.routes.compras.get_stores_list', return_value=[]),
            patch('flask_app.routes.compras.get_cost_centers', return_value=[]),
            patch('flask_app.routes.compras.get_cost_categories_tree', return_value=[]),
            patch('db.centros_custo.get_cost_categories', return_value=[]),
            patch('flask_app.routes.compras.render_template', return_value='ok'),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_cost_filters_and_sort_are_forwarded_and_saved(self):
        from flask_app.routes import compras as compras_route

        response = self.client.get(
            '/compras/faturas?sem_cc=1&centro_custo_id=8&categoria_custo_id=4'
            '&order_by=centro_custo_name&order_dir=asc'
        )

        self.assertEqual(response.status_code, 200)
        invoice_kwargs = compras_route.get_invoices.call_args.kwargs
        self.assertTrue(invoice_kwargs['sem_cc'])
        self.assertEqual(invoice_kwargs['centro_custo_id'], 8)
        self.assertEqual(invoice_kwargs['categoria_custo_id'], 4)
        self.assertEqual(invoice_kwargs['order_by'], 'centro_custo_name')
        self.assertEqual(invoice_kwargs['order_dir'], 'asc')

        template_kwargs = compras_route.render_template.call_args.kwargs
        self.assertTrue(template_kwargs['sem_cc_filter'])
        self.assertEqual(template_kwargs['centro_custo_filter'], 8)
        self.assertEqual(template_kwargs['categoria_custo_filter'], 4)

        with self.client.session_transaction() as session:
            self.assertEqual(session['faturas_filters']['sem_cc'], '1')
            self.assertEqual(session['faturas_filters']['centro_custo_id'], 8)
            self.assertEqual(session['faturas_filters']['categoria_custo_id'], 4)
            self.assertEqual(session['faturas_filters']['order_by'], 'centro_custo_name')
            self.assertEqual(session['faturas_filters']['order_dir'], 'asc')

    def test_clearing_a_cost_filter_does_not_restore_it_from_session(self):
        with self.client.session_transaction() as session:
            session['faturas_filters'] = {
                'sem_cc': '1',
                'centro_custo_id': 8,
                'categoria_custo_id': 4,
            }

        response = self.client.get(
            '/compras/faturas?sem_cc=0&clear_filter=centro_custo_id'
            '&clear_filter=categoria_custo_id'
        )

        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session['faturas_filters'], {})


if __name__ == '__main__':
    unittest.main()