"""
End-to-end access-control tests for the Compras module.

Run with: python -m unittest tests.test_compras_access -v

Tests register the **real** `compras_bp` (same URL rules and decorators as
production) inside a minimal Flask app, and patch only the database/template
calls that the route bodies need so that they reach 200 without a live DB.

Covers:
- User with acesso_compras=True reaches /compras/, /compras/faturas,
  /compras/nova-fatura (HTTP 200, no redirect).
- User without acesso_compras (and without acesso_administrativo / acesso_gestor)
  is redirected away from every compras route.
- Unauthenticated requests redirect to login.
- acesso_administrativo alone also grants access (alternative gate).
- acesso_gestor super-user flag still grants access.
- acesso_financeiro alone does NOT grant compras access.
"""

import unittest
import os
from unittest.mock import patch, MagicMock
from flask import Blueprint, Flask, render_template, url_for
from jinja2 import FileSystemLoader


# ---------------------------------------------------------------------------
# Build a minimal Flask app that registers the REAL compras blueprint
# ---------------------------------------------------------------------------

def _make_app():
    """Return a Flask test app with the real compras_bp registered."""
    from flask_app.routes.compras import compras_bp
    from flask_app.routes.faturas import faturas_bp

    app = Flask(__name__)
    app.secret_key = 'test-secret-key'
    app.config['TESTING'] = True

    # Stub blueprints required by auth redirect targets
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
    app.register_blueprint(faturas_bp, url_prefix='/financeiro/faturas')
    app.register_blueprint(compras_bp, url_prefix='/compras')

    return app


# ---------------------------------------------------------------------------
# Route-body mocks — applied for every test so DB is never contacted.
# "blocked" tests never reach the body; "allowed" tests get 200 stubs.
# ---------------------------------------------------------------------------

_DB_PATCHES = [
    # db.tiles (imported inside index() body)
    patch('db.tiles.seed_tile_config'),
    patch('db.tiles.get_tile_visibility',   return_value={}),
    patch('db.tiles.get_tile_labels',        return_value={}),
    patch('db.tiles.get_tile_icons',         return_value={}),
    patch('db.tiles.get_module_labels',      return_value={}),
    # faturas route (module-level imports in flask_app.routes.compras)
    patch('flask_app.routes.compras.count_invoices',            return_value=0),
    patch('flask_app.routes.compras.get_invoices',              return_value=[]),
    patch('flask_app.routes.compras.get_payment_methods_config',return_value=[]),
    patch('flask_app.routes.compras.get_distinct_supplier_names',return_value=[]),
    patch('flask_app.routes.compras.get_stores_list',           return_value=[]),
    # nova-fatura route (GET body)
    patch('flask_app.routes.compras.get_suppliers',             return_value=[]),
    patch('flask_app.routes.compras.get_cost_centers',          return_value=[]),
    patch('flask_app.routes.compras.get_cost_categories_tree',  return_value=[]),
    patch('flask_app.routes.compras.get_artigos_administrativos', return_value=[]),
    patch('flask_app.routes.compras.get_compras_origens',        return_value=[]),
    # article-count route
    patch('db.contagens_compras.list_submitted_counts', return_value=[]),
    patch('db.contagens_compras.get_count_origins', return_value=[]),
    # operational abastecimento route
    patch('db.abastecimento.list_weekly_orders', return_value=[]),
    patch('db.abastecimento.get_weekly_consolidation', return_value=[]),
    patch('db.abastecimento.list_urgent_orders', return_value=[]),
    patch('db.abastecimento.get_urgent_metrics', return_value={
        'volume_total': 0, 'orders_total': 0,
        'by_reason': [], 'by_store': [], 'by_article': [],
    }),
    # render_template — return a plain string so no template files are needed
    # Must be patched where it is *used* (imported into compras module), not at flask.templating
    patch('flask_app.routes.compras.render_template',           return_value='ok'),
]


# ---------------------------------------------------------------------------
# Shared user builder
# ---------------------------------------------------------------------------

_BASE_USER = {
    'id': 1,
    'username': 'testuser',
    'role': 'producao',
    'nome': 'Test User',
    'acesso_eurokg': False,
    'acesso_producao': False,
    'acesso_vendas': False,
    'acesso_pastelaria': False,
    'acesso_confeitaria': False,
    'acesso_gestor': False,
    'acesso_administrativo': False,
    'acesso_financeiro': False,
    'acesso_eventos': False,
    'acesso_tarefas': False,
    'acesso_contabilidade': False,
    'acesso_compras': False,
    'vendas_store_ids': [],
    'loja_id': None,
}


def _user(**overrides):
    return {**_BASE_USER, **overrides}


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestComprasAccess(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()
        # Start all DB patches for the whole class
        cls._started_patches = []
        for p in _DB_PATCHES:
            p.start()
            cls._started_patches.append(p)

    @classmethod
    def tearDownClass(cls):
        for p in cls._started_patches:
            p.stop()

    def setUp(self):
        self.client = self.app.test_client()

    def _set_session_user(self, user: dict):
        with self.client.session_transaction() as sess:
            sess['user'] = user

    # ------------------------------------------------------------------
    # User with ONLY acesso_compras must reach every compras route (200)
    # ------------------------------------------------------------------

    def test_compras_index_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/ returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/')
        self.assertEqual(resp.status_code, 200,
                         "acesso_compras user should not be redirected from /compras/")

    def test_compras_index_passes_descriptions_and_keeps_tile_preferences(self):
        from flask_app.routes import compras as compras_routes

        self._set_session_user(_user(acesso_compras=True))
        hidden_tile = 'criar_ordem'
        custom_label = 'Artigos para encomenda'
        custom_icon = '⭐'
        with patch(
            'db.tiles.get_tile_visibility',
            return_value={hidden_tile: False},
        ), patch(
            'db.tiles.get_tile_labels',
            return_value={'artigos': custom_label},
        ), patch(
            'db.tiles.get_tile_icons',
            return_value={'artigos': custom_icon},
        ), patch(
            'db.tiles.get_module_labels',
            return_value={'compras': 'Compras personalizadas'},
        ), patch(
            'flask_app.routes.compras.render_template',
            return_value='ok',
        ) as render_menu:
            response = self.client.get('/compras/')

        self.assertEqual(response.status_code, 200)
        items = render_menu.call_args.kwargs['items']
        menu_title = render_menu.call_args.kwargs['menu_title']
        with self.app.test_request_context():
            expected_descriptions = {
                url_for(
                    tab['url_endpoint'],
                    **tab.get('url_kwargs', {}),
                ): tab['description']
                for tab in compras_routes.TABS
            }
            hidden_url = url_for('compras.criar_ordem')
            articles_url = url_for('compras.artigos')

        self.assertEqual(len(expected_descriptions), 9)
        self.assertTrue(all(expected_descriptions.values()))
        self.assertLessEqual(
            max(len(description) for description in expected_descriptions.values()),
            80,
        )
        self.assertEqual(len(items), 8)
        self.assertNotIn(hidden_url, {item['url'] for item in items})
        self.assertEqual(menu_title, '🛒 Compras personalizadas')
        article_item = next(
            item for item in items
            if item['url'] == articles_url
        )
        self.assertEqual(article_item['label'], custom_label)
        self.assertEqual(article_item['icon'], custom_icon)
        for item in items:
            self.assertEqual(
                item['description'],
                expected_descriptions[item['url']],
            )

    def test_catalogue_explains_origin_supplier_unit_and_brand(self):
        with open(
            'flask_app/templates/compras/artigos.html',
            encoding='utf-8',
        ) as template:
            html = ' '.join(template.read().split())

        self.assertIn(
            'Pesquise, acrescente e corrija artigos disponíveis para encomendas.',
            html,
        )
        self.assertIn('Origem por resolver:', html)
        self.assertIn(
            'A designação original foi preservada',
            html,
        )
        self.assertIn(
            'Os artigos ativos continuam disponíveis para contagens e pedidos das lojas enquanto esta validação estiver pendente.',
            html,
        )
        self.assertIn(
            'Use <strong>Confirmar fornecedor</strong> apenas para corrigir a origem;',
            html,
        )
        self.assertIn(
            'Origem pode ser fornecedor, centro interno ou categoria.',
            html,
        )
        self.assertIn(
            'Unidade é a medida usada nas quantidades (ex.: und, kg, cx), não a quantidade.',
            html,
        )
        self.assertIn(
            'Marca é opcional e pode ser diferente do fornecedor.',
            html,
        )
        self.assertIn('data-bs-target="#confirmarFornecedorModal"', html)
        self.assertIn('name="supplier_id"', html)
        self.assertIn('value="confirm_supplier"', html)
        self.assertIn("url_for('faturas.fornecedores', origem='compras')", html)

    def test_catalogue_passes_canonical_suppliers_to_resolution_dialog(self):
        self._set_session_user(_user(acesso_compras=True))
        suppliers = [{
            'id': 42,
            'name': 'Fornecedor Legal, Lda.',
            'common_name': 'Fornecedor',
            'nif': '501234567',
        }]
        with patch(
            'flask_app.routes.compras.get_suppliers',
            return_value=suppliers,
        ), patch(
            'flask_app.routes.compras.render_template',
            return_value='ok',
        ) as render_catalogue:
            response = self.client.get('/compras/artigos')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            render_catalogue.call_args.kwargs['suppliers'],
            suppliers,
        )

    def test_catalogue_renders_origin_and_supplier_relationships_by_id(self):
        self._set_session_user(_user(acesso_compras=True))
        articles = [
            {
                'id': 101, 'fornecedor': 'Inocentro', 'produto': 'Produto externo',
                'ativo': True, 'origem_id': 1, 'origem_tipo': 'fornecedor_externo',
                'origem_supplier_id': 10, 'origem_nome': 'Inocentro',
                'origem_original': 'Etiqueta original Inocentro',
                'fornecedor_oficial_id': 10,
                'fornecedor_oficial_nome': 'Inocentro Legal, Lda.',
            },
            {
                'id': 102, 'fornecedor': 'Matosinhos', 'produto': 'Produto interno',
                'ativo': True, 'origem_id': 2, 'origem_tipo': 'centro_interno',
                'origem_supplier_id': None, 'origem_nome': 'Matosinhos',
                'origem_original': 'Matosinhos',
                'fornecedor_oficial_id': 20,
                'fornecedor_oficial_nome': 'DEGAR SRL',
            },
            {
                'id': 103, 'fornecedor': 'Categoria', 'produto': 'Sem fornecedor',
                'ativo': True, 'origem_id': 3,
                'origem_tipo': 'categoria_operacional',
                'origem_supplier_id': None, 'origem_nome': 'Moedas',
                'origem_original': 'Categoria',
                'fornecedor_oficial_id': None,
                'fornecedor_oficial_nome': None,
            },
            {
                'id': 104, 'fornecedor': 'Texto com nome semelhante',
                'produto': 'IDs diferentes',
                'ativo': True, 'origem_id': 4,
                'origem_tipo': 'fornecedor_externo',
                'origem_supplier_id': 30, 'origem_nome': 'Fornecedor coincidente',
                'origem_original': 'Fornecedor coincidente',
                'fornecedor_oficial_id': 31,
                'fornecedor_oficial_nome': 'Fornecedor coincidente',
            },
        ]
        origins = [
            {'id': 1, 'nome': 'Inocentro', 'tipo': 'fornecedor_externo'},
            {'id': 2, 'nome': 'Matosinhos', 'tipo': 'centro_interno'},
            {'id': 3, 'nome': 'Moedas', 'tipo': 'categoria_operacional'},
            {
                'id': 4, 'nome': 'Fornecedor coincidente',
                'tipo': 'fornecedor_externo',
            },
        ]
        suppliers = [
            {'id': 10, 'name': 'Inocentro Legal, Lda.'},
            {'id': 20, 'name': 'DEGAR SRL'},
            {'id': 31, 'name': 'Fornecedor coincidente'},
        ]
        original_loader = self.app.jinja_loader
        self.app.jinja_loader = FileSystemLoader(
            os.path.abspath('flask_app/templates')
        )
        try:
            with patch(
                'flask_app.routes.compras.get_artigos_administrativos',
                return_value=articles,
            ), patch(
                'flask_app.routes.compras.get_compras_origens',
                return_value=origins,
            ), patch(
                'flask_app.routes.compras.get_suppliers',
                return_value=suppliers,
            ), patch(
                'flask_app.routes.compras.render_template',
                side_effect=render_template,
            ):
                response = self.client.get('/compras/artigos')
        finally:
            self.app.jinja_loader = original_loader

        self.assertEqual(response.status_code, 200)
        html = ' '.join(response.get_data(as_text=True).split())
        self.assertEqual(html.count('Mesmo fornecedor da origem'), 1)
        self.assertIn(
            '<details class="small mt-1"> <summary>Ver nome legal</summary> '
            '<div class="pt-1">Inocentro Legal, Lda.</div> </details>',
            html,
        )
        self.assertIn('Fornecedor externo', html)
        self.assertIn('Centro interno', html)
        self.assertIn('Categoria operacional', html)
        self.assertIn('DEGAR SRL', html)
        self.assertIn('Fornecedor coincidente', html)
        self.assertIn('Fornecedor por confirmar', html)
        self.assertIn('Texto do ficheiro / editar etiqueta', html)
        self.assertIn(
            'Texto original preservado: Etiqueta original Inocentro',
            html,
        )
        self.assertIn('name="fornecedor" value="Inocentro"', html)
        self.assertIn('name="fornecedor" value="Matosinhos"', html)

    def test_catalogue_confirms_supplier_using_submitted_id(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.confirm_artigo_fornecedor',
            return_value={
                'changed': True,
                'supplier_name': 'Fornecedor Canónico',
            },
        ) as confirm_supplier:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'confirm_supplier',
                    'artigo_id': '18',
                    'supplier_id': '42',
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            confirm_supplier.call_args.args,
            (18, 42),
        )
        self.assertEqual(
            confirm_supplier.call_args.kwargs['actor'],
            'testuser',
        )
        with self.client.session_transaction() as sess:
            messages = [message for _, message in sess.get('_flashes', [])]
        self.assertIn('Fornecedor "Fornecedor Canónico" confirmado.', messages)

    def test_catalogue_noop_save_has_accurate_feedback(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.update_artigo_administrativo',
            return_value={
                'found': True,
                'changed': False,
                'origin_type': 'por_resolver',
            },
        ):
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'edit',
                    'artigo_id': '18',
                    'origem_id': '8',
                    'fornecedor': 'CAFÉ ILLY',
                    'produto': 'Café Clássico',
                },
            )

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            flashes = sess.get('_flashes', [])
        self.assertIn(('info', 'Sem alterações.'), flashes)

    def test_catalogue_edit_submits_the_editable_label(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.update_artigo_administrativo',
            return_value={
                'found': True,
                'changed': True,
                'origin_type': 'centro_interno',
            },
        ) as update_article:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'edit',
                    'artigo_id': '18',
                    'origem_id': '8',
                    'fornecedor': 'Etiqueta corrigida',
                    'produto': 'Produto',
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            update_article.call_args.args[:3],
            (18, 'Etiqueta corrigida', 'Produto'),
        )

    def test_catalogue_edit_keeps_unresolved_status_in_feedback(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.update_artigo_administrativo',
            return_value={
                'found': True,
                'changed': True,
                'origin_type': 'por_resolver',
            },
        ):
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'edit',
                    'artigo_id': '18',
                    'origem_id': '8',
                    'fornecedor': 'CAFÉ ILLY',
                    'produto': 'Café Clássico Descafeinado',
                },
            )

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            flashes = sess.get('_flashes', [])
        self.assertIn(
            ('warning', 'Artigo guardado; origem ainda por resolver.'),
            flashes,
        )

    def test_compras_faturas_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/faturas returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/faturas')
        self.assertEqual(resp.status_code, 200,
                         "acesso_compras user should not be redirected from /compras/faturas")

    def test_compras_nova_fatura_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/nova-fatura returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/nova-fatura')
        self.assertEqual(resp.status_code, 200,
                         "acesso_compras user should not be redirected from /compras/nova-fatura")

    def test_compras_catalogue_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/artigos returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/artigos')
        self.assertEqual(resp.status_code, 200)

    def test_compras_article_counts_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/contagens-artigos returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/contagens-artigos')
        self.assertEqual(resp.status_code, 200)

    def test_compras_operational_view_accessible_with_acesso_compras(self):
        """acesso_compras=True → operational abastecimento view returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/operacao-abastecimento')
        self.assertEqual(resp.status_code, 200)

    def test_operational_supplier_filter_lists_canonical_suppliers_for_internal_origins(self):
        from flask_app.routes import compras as compras_routes

        self._set_session_user(_user(acesso_compras=True))
        compras_routes.render_template.reset_mock()
        suppliers = [{
            'id': 42,
            'name': 'Fornecedor Legal, Lda.',
            'common_name': 'Fornecedor Bolhão',
        }]
        origins = [{
            'id': 7,
            'nome': 'Matosinhos',
            'tipo': 'centro_interno',
            'supplier_id': None,
            'supplier_name': None,
        }]
        with patch(
            'flask_app.routes.compras.get_suppliers',
            return_value=suppliers,
        ), patch(
            'flask_app.routes.compras.get_compras_origens',
            return_value=origins,
        ):
            response = self.client.get(
                '/compras/operacao-abastecimento?fornecedor_id=42'
            )

        self.assertEqual(response.status_code, 200)
        kwargs = compras_routes.render_template.call_args.kwargs
        self.assertEqual(
            kwargs['supplier_options'],
            [{'id': 42, 'name': 'Fornecedor Bolhão — Fornecedor Legal, Lda.'}],
        )
        self.assertEqual(kwargs['supplier_id'], 42)

    # ------------------------------------------------------------------
    # User without any relevant perm must be redirected (3xx)
    # ------------------------------------------------------------------

    def test_compras_index_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/ redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "User without compras permission should be redirected from /compras/")

    def test_compras_faturas_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/faturas redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/faturas')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "User without compras permission should be redirected from /compras/faturas")

    def test_compras_nova_fatura_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/nova-fatura redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/nova-fatura')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "User without compras permission should be redirected from /compras/nova-fatura")

    def test_compras_catalogue_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/artigos redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/artigos')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))

    def test_compras_article_counts_blocked_without_perm(self):
        """No compras/admin/gestor perm → article counts redirect."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/contagens-artigos')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))

    def test_compras_operational_view_blocked_without_perm(self):
        """No compras/admin/gestor perm → operational view redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/operacao-abastecimento')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))

    def test_unauthenticated_user_is_redirected(self):
        """No session at all → GET /compras/ redirects to login."""
        resp = self.client.get('/compras/')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "Unauthenticated request should redirect")

    # ------------------------------------------------------------------
    # Regression: acesso_administrativo also grants access
    # ------------------------------------------------------------------

    def test_compras_accessible_with_acesso_administrativo(self):
        """acesso_administrativo=True (no acesso_compras) → /compras/ still 200."""
        self._set_session_user(_user(acesso_administrativo=True))
        resp = self.client.get('/compras/')
        self.assertEqual(resp.status_code, 200,
                         "acesso_administrativo should also grant /compras/ access")

    # ------------------------------------------------------------------
    # Regression: acesso_gestor super-user flag grants access everywhere
    # ------------------------------------------------------------------

    def test_compras_accessible_with_acesso_gestor(self):
        """acesso_gestor=True → /compras/ returns 200 (gestor bypasses all perm checks)."""
        self._set_session_user(_user(acesso_gestor=True))
        resp = self.client.get('/compras/')
        self.assertEqual(resp.status_code, 200,
                         "acesso_gestor should always grant /compras/ access")

    # ------------------------------------------------------------------
    # Explicit: acesso_financeiro alone does NOT grant compras access
    # ------------------------------------------------------------------

    def test_compras_blocked_with_only_acesso_financeiro(self):
        """acesso_financeiro=True but acesso_compras=False → /compras/ redirects."""
        self._set_session_user(_user(acesso_financeiro=True))
        resp = self.client.get('/compras/')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "acesso_financeiro alone must NOT grant /compras/ access")


if __name__ == '__main__':
    unittest.main()
