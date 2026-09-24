"""Tests for safe return navigation from the shared supplier directory."""

import unittest
import os
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from flask import Blueprint, Flask


def _user(**overrides):
    return {
        'id': 1,
        'username': 'testuser',
        'acesso_gestor': False,
        'acesso_administrativo': False,
        'acesso_compras': False,
        'acesso_financeiro': False,
        **overrides,
    }


def _app_with_supplier_route():
    from flask_app.routes.faturas import faturas_bp

    app = Flask(__name__, template_folder=os.path.abspath('flask_app/templates'))
    app.secret_key = 'test-secret-key'
    app.config['TESTING'] = True

    compras_bp = Blueprint('compras', __name__)

    @compras_bp.route('/')
    def index():
        return 'compras'

    @compras_bp.route('/faturas')
    def faturas():
        return 'compras documentos'

    financeiro_bp = Blueprint('financeiro', __name__)

    @financeiro_bp.route('/')
    def index():
        return 'financeiro'

    app.register_blueprint(compras_bp, url_prefix='/compras')
    app.register_blueprint(financeiro_bp, url_prefix='/financeiro')
    app.register_blueprint(faturas_bp, url_prefix='/financeiro/faturas')
    return app


class TestSupplierReturnOriginResolution(unittest.TestCase):
    def test_permission_safe_origin_fallbacks(self):
        from flask_app.routes.faturas import _resolve_supplier_return_origin

        cases = [
            (_user(acesso_compras=True), '', 'compras'),
            (_user(acesso_compras=True), 'compras_documentos', 'compras_documentos'),
            (_user(acesso_financeiro=True), '', 'documentos'),
            (_user(acesso_financeiro=True), 'financeiro', 'documentos'),
            (_user(acesso_financeiro=True), 'compras', 'documentos'),
            (_user(acesso_compras=True), 'https://example.com', 'compras'),
            (_user(acesso_gestor=True), 'financeiro', 'financeiro'),
            (_user(acesso_gestor=True), 'not-an-origin', 'financeiro'),
        ]

        for user, requested, expected in cases:
            with self.subTest(user=user, requested=requested):
                self.assertEqual(
                    _resolve_supplier_return_origin(user, requested),
                    expected,
                )


class TestSupplierReturnNavigationRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app_with_supplier_route()

    def _client(self, **user_overrides):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user'] = _user(**user_overrides)
        return client

    def test_post_save_keeps_validated_compras_origin(self):
        with patch('flask_app.routes.faturas.get_stores_list', return_value=[]), \
             patch('flask_app.routes.faturas.upsert_supplier'):
            response = self._client(acesso_compras=True).post(
                '/financeiro/faturas/fornecedores?origem=compras',
                data={'action': 'save', 'name': 'Fornecedor de teste'},
            )

        self.assertEqual(response.status_code, 302)
        location = urlparse(response.headers['Location'])
        self.assertEqual(location.path, '/financeiro/faturas/fornecedores')
        self.assertEqual(parse_qs(location.query), {'origem': ['compras']})

    def test_direct_finance_only_visit_uses_documents_fallback(self):
        with patch('flask_app.routes.faturas.get_stores_list', return_value=[]), \
             patch('flask_app.routes.faturas.get_suppliers_with_invoice_count', return_value=[]), \
             patch('db.centros_custo.get_cost_categories', return_value=[]), \
             patch('flask_app.routes.faturas.get_unlinked_supplier_names', return_value=[]), \
             patch('flask_app.routes.faturas.get_duplicate_supplier_suggestions', return_value=[]), \
             patch('flask_app.routes.faturas.get_all_supplier_aliases', return_value={}), \
             patch('flask_app.routes.faturas.get_cost_centers', return_value=[]), \
             patch(
                 'flask_app.routes.faturas.render_template',
                 side_effect=lambda _template, **context: context['return_url'],
             ):
            response = self._client(acesso_financeiro=True).get(
                '/financeiro/faturas/fornecedores?origem=https://example.com'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), '/financeiro/faturas/')

    def test_supplier_page_renders_back_link_and_form_actions_for_origin(self):
        with patch('flask_app.routes.faturas.get_stores_list', return_value=[]), \
             patch('flask_app.routes.faturas.get_suppliers_with_invoice_count', return_value=[]), \
             patch('db.centros_custo.get_cost_categories', return_value=[]), \
             patch('flask_app.routes.faturas.get_unlinked_supplier_names', return_value=[]), \
             patch('flask_app.routes.faturas.get_duplicate_supplier_suggestions', return_value=[]), \
             patch('flask_app.routes.faturas.get_all_supplier_aliases', return_value={}), \
             patch('flask_app.routes.faturas.get_cost_centers', return_value=[]):
            response = self._client(acesso_compras=True).get(
                '/financeiro/faturas/fornecedores?origem=compras'
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('href="/compras/" class="back-bar"', page)
        self.assertEqual(
            page.count('action="/financeiro/faturas/fornecedores?origem=compras"'),
            4,
        )

    def test_all_supplier_post_forms_keep_the_origin(self):
        with open(
            'flask_app/templates/financeiro/faturas/fornecedores.html',
            encoding='utf-8',
        ) as template:
            source = template.read()

        self.assertEqual(source.count('<form method="post" action="{{ supplier_post_url }}"'), 11)

    def test_compras_menu_contains_the_supplier_tile_with_origin(self):
        from flask_app.routes.compras import TABS

        suppliers_tile = next(tile for tile in TABS if tile['id'] == 'fornecedores')
        self.assertEqual(suppliers_tile['url_endpoint'], 'faturas.fornecedores')
        self.assertEqual(suppliers_tile['url_kwargs'], {'origem': 'compras'})


if __name__ == '__main__':
    unittest.main()