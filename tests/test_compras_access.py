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

    def test_catalogue_explains_direct_supplier_and_historical_origin(self):
        with open(
            'flask_app/templates/compras/artigos.html',
            encoding='utf-8',
        ) as template:
            html = ' '.join(template.read().split())

        self.assertIn(
            'Pesquise, acrescente e corrija artigos disponíveis para encomendas.',
            html,
        )
        self.assertIn(
            'Fornecedor por confirmar:',
            html,
        )
        self.assertIn(
            'Artigos sem uma ligação direta a fornecedor continuam disponíveis para contagens e pedidos.',
            html,
        )
        self.assertIn(
            'A origem herdada fica apenas como registo histórico.',
            html,
        )
        self.assertIn(
            'Não usamos etiquetas antigas, centros internos ou categorias para deduzir fornecedores.',
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
        add_form = html.split(
            '<form method="post" class="row g-2 align-items-end"', 1
        )[1].split('</form>', 1)[0]
        self.assertIn('name="supplier_id"', add_form)
        self.assertNotIn('name="origem_id"', add_form)
        self.assertNotIn('name="fornecedor"', add_form)
        self.assertIn('data-bs-target="#fornecedorOficialModal"', html)
        self.assertIn('name="supplier_id"', html)
        self.assertIn('value="set_official_supplier"', html)
        self.assertIn('name="scroll_context"', html)
        self.assertIn('catalogueScrollContextPrefix', html)
        self.assertIn('scrollTo(context.x, context.y)', html)
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

    def test_new_article_posts_selected_supplier_id(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.add_artigo_administrativo',
            return_value=True,
        ) as add_article:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'add',
                    'supplier_id': '42',
                    'produto': 'Café em grão',
                    'marca': 'Garcias',
                    'unidade': 'kg',
                    'categoria_artigo': 'Bebidas e café',
                },
            )

        self.assertEqual(response.status_code, 302)
        add_article.assert_called_once_with(
            'Café em grão',
            supplier_id=42,
            marca='Garcias',
            unidade='kg',
            actor='testuser',
            categoria_artigo='Bebidas e café',
        )

    def test_new_article_preserves_catalogue_filters_and_scroll_context(self):
        self._set_session_user(_user(acesso_compras=True))
        token = 'abc123ef' * 4
        with patch(
            'flask_app.routes.compras.add_artigo_administrativo',
            return_value=True,
        ):
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'add',
                    'supplier_id': '42',
                    'produto': 'Farinha de trigo',
                    'q': 'Farinha',
                    'revisao': 'revisto',
                    'scroll_context': token,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('q=Farinha', response.location)
        self.assertIn('revisao=revisto', response.location)
        self.assertIn(f'scroll_context={token}', response.location)

    def test_catalogue_toggle_preserves_filters_and_scroll_context(self):
        self._set_session_user(_user(acesso_compras=True))
        token = '123abcde' * 4
        with patch(
            'flask_app.routes.compras.toggle_artigo_administrativo',
        ) as toggle_article:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'toggle',
                    'artigo_id': '18',
                    'ativo': '0',
                    'q': 'Farinha',
                    'revisao': 'por_rever',
                    'scroll_context': token,
                },
            )

        self.assertEqual(response.status_code, 302)
        toggle_article.assert_called_once_with(18, False)
        self.assertIn('q=Farinha', response.location)
        self.assertIn('revisao=por_rever', response.location)
        self.assertIn(f'scroll_context={token}', response.location)

    def test_legacy_catalogue_edits_keep_query_context_on_return(self):
        self._set_session_user(_user(acesso_compras=True))
        token = 'deafbeef' * 4
        return_context = (
            f'/compras/artigos?q=Farinha&revisao=revisto'
            f'&scroll_context={token}'
        )
        with patch(
            'flask_app.routes.compras.update_artigo_administrativo',
            return_value={
                'found': True, 'changed': True, 'origin_type': 'por_resolver',
            },
        ), patch(
            'flask_app.routes.compras.delete_artigo_administrativo',
        ) as delete_article:
            edit_response = self.client.post(
                return_context,
                data={
                    'action': 'edit',
                    'artigo_id': '18',
                    'produto': 'Farinha',
                },
            )
            delete_response = self.client.post(
                return_context,
                data={'action': 'delete', 'artigo_id': '18'},
            )

        for response in (edit_response, delete_response):
            self.assertEqual(response.status_code, 302)
            self.assertIn('q=Farinha', response.location)
            self.assertIn('revisao=revisto', response.location)
            self.assertIn(f'scroll_context={token}', response.location)
        delete_article.assert_called_once_with(18)

    def test_new_article_rejects_missing_or_malformed_supplier_selection(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.add_artigo_administrativo',
        ) as add_article:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'add',
                    'supplier_id': '²',
                    'produto': 'Café em grão',
                },
            )

        self.assertEqual(response.status_code, 302)
        add_article.assert_not_called()
        with self.client.session_transaction() as sess:
            flashes = sess.get('_flashes', [])
        self.assertIn(
            ('warning', 'Selecione um fornecedor e indique o produto.'),
            flashes,
        )

    def test_catalogue_shows_direct_supplier_and_read_only_history(self):
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
                'origem_revisao_estado': 'por_rever',
            },
            {
                'id': 103, 'fornecedor': 'Categoria', 'produto': 'Sem fornecedor',
                'ativo': True, 'origem_id': 3,
                'origem_tipo': 'categoria_operacional',
                'origem_supplier_id': None, 'origem_nome': 'Moedas',
                'origem_original': 'Categoria',
                'fornecedor_oficial_id': None,
                'fornecedor_oficial_nome': None,
                'origem_revisao_estado': 'por_rever',
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
            {
                'id': 105, 'fornecedor': 'GARCIAS, S.A.',
                'produto': 'Café em grão', 'ativo': True, 'origem_id': None,
                'origem_tipo': None, 'origem_supplier_id': None,
                'origem_nome': None, 'origem_original': None,
                'fornecedor_oficial_id': 42,
                'fornecedor_oficial_nome': 'GARCIAS, S.A.',
            },
        ]
        suppliers = [
            {'id': 10, 'name': 'Inocentro Legal, Lda.'},
            {'id': 20, 'name': 'DEGAR SRL'},
            {'id': 31, 'name': 'Fornecedor coincidente'},
            {'id': 42, 'name': 'GARCIAS, S.A.', 'nif': 'PT501141243'},
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
                'flask_app.routes.compras.get_suppliers',
                return_value=suppliers,
            ), patch(
                'flask_app.routes.compras.render_template',
                side_effect=render_template,
            ):
                response = self.client.get('/compras/artigos')
                pending_response = self.client.get(
                    '/compras/artigos?revisao=por_rever'
                )
        finally:
            self.app.jinja_loader = original_loader

        self.assertEqual(response.status_code, 200)
        html = ' '.join(response.get_data(as_text=True).split())
        pending_html = ' '.join(pending_response.get_data(as_text=True).split())
        self.assertIn('Inocentro Legal, Lda.', html)
        self.assertIn('Origem histórica', html)
        self.assertIn('Matosinhos', html)
        self.assertIn('Moedas', html)
        self.assertIn('Rever artigo', html)
        self.assertIn('name="review_action" value="pending"', html)
        self.assertIn('name="review_action" value="confirm"', html)
        self.assertIn('DEGAR SRL', html)
        self.assertIn('Fornecedor coincidente', html)
        self.assertIn('GARCIAS, S.A.', html)
        self.assertIn('PT501141243', html)
        self.assertIn('Fornecedor por confirmar', html)
        self.assertIn('data-fornecedor-id="10"', html)
        self.assertIn('Inocentro', html)
        self.assertNotIn('Etiqueta original Inocentro', html)
        self.assertNotIn('name="fornecedor_101"', html)
        self.assertNotIn('name="origem_id_101"', html)
        self.assertIn('name="categoria_artigo_101"', html)
        self.assertIn('artigo-detalhe-101', html)
        self.assertIn('name="action" value="bulk_edit"', html)
        self.assertIn('Guardar alterações', html)
        self.assertNotIn('<th>Origem</th>', html)
        self.assertIn('Produto interno', pending_html)
        self.assertIn('Sem fornecedor', pending_html)
        self.assertNotIn('Produto externo', pending_html)

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

    def test_catalogue_review_can_keep_pending_and_preserves_filter(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.review_artigo_origem',
            return_value={
                'found': True, 'changed': False, 'estado': 'por_rever',
                'supplier_id': 42, 'supplier_name': 'Fornecedor existente',
            },
        ) as review_origin:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'review_origin',
                    'artigo_id': '18',
                    'review_action': 'pending',
                    'supplier_id': '',
                    'q': 'Matosinhos',
                    'revisao': 'por_rever',
                    'scroll_context': 'a1b2c3d4' * 4,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('q=Matosinhos', response.location)
        self.assertIn('revisao=por_rever', response.location)
        self.assertIn(f'scroll_context={"a1b2c3d4" * 4}', response.location)
        self.assertEqual(review_origin.call_args.args, (18, None))
        self.assertEqual(review_origin.call_args.kwargs['actor'], 'testuser')
        with self.client.session_transaction() as sess:
            messages = [message for _, message in sess.get('_flashes', [])]
        self.assertTrue(any('ligação atual' in message for message in messages))

    def test_catalogue_review_confirms_registered_supplier(self):
        self._set_session_user(_user(acesso_compras=True))
        with patch(
            'flask_app.routes.compras.review_artigo_origem',
            return_value={
                'found': True, 'changed': True, 'estado': 'revisto',
                'supplier_id': 42, 'supplier_name': 'Fornecedor Canónico',
            },
        ) as review_origin:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'review_origin',
                    'artigo_id': '18',
                    'review_action': 'confirm',
                    'supplier_id': '42',
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(review_origin.call_args.args, (18, 42))
        self.assertEqual(review_origin.call_args.kwargs['actor'], 'testuser')
        with self.client.session_transaction() as sess:
            messages = [message for _, message in sess.get('_flashes', [])]
        self.assertTrue(any('a designação histórica foi preservada' in m for m in messages))

    def test_catalogue_supplier_link_preserves_filters_and_scroll_context(self):
        self._set_session_user(_user(acesso_compras=True))
        token = 'c0ffee42' * 4
        with patch(
            'flask_app.routes.compras.set_artigo_fornecedor_oficial',
            return_value={'changed': True},
        ) as set_supplier:
            response = self.client.post(
                '/compras/artigos',
                data={
                    'action': 'set_official_supplier',
                    'artigo_id': '18',
                    'supplier_id': '42',
                    'q': 'Café',
                    'revisao': 'revisto',
                    'scroll_context': token,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(set_supplier.call_args.args, (18, 42))
        self.assertIn('q=Caf%C3%A9', response.location)
        self.assertIn('revisao=revisto', response.location)
        self.assertIn(f'scroll_context={token}', response.location)

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

    def test_catalogue_edit_keeps_historical_label_read_only(self):
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
            (18, None, 'Produto'),
        )
        self.assertIsNone(update_article.call_args.kwargs['origem_id'])
        self.assertIsNone(
            update_article.call_args.kwargs['categoria_artigo']
        )

    def test_catalogue_edit_submits_selected_article_category(self):
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
                    'categoria_artigo': 'Bebidas e café',
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            update_article.call_args.kwargs['categoria_artigo'],
            'Bebidas e café',
        )

    def test_catalogue_bulk_save_submits_multiple_rows_once(self):
        self._set_session_user(_user(acesso_compras=True))
        from werkzeug.datastructures import MultiDict
        form_data = MultiDict([
            ('action', 'bulk_edit'),
            ('q', 'Café'),
            ('revisao', 'por_rever'),
            ('scroll_context', 'deadbeef' * 4),
            ('artigo_id', '18'),
            ('artigo_id', '19'),
            ('produto_18', 'Produto 18'),
            ('marca_18', ''),
            ('unidade_18', 'un'),
            ('categoria_artigo_18', 'Higiene e limpeza'),
            ('produto_19', 'Produto 19'),
            ('marca_19', 'Marca 19'),
            ('unidade_19', 'kg'),
            ('categoria_artigo_19', 'Bebidas e café'),
        ])
        with patch(
            'flask_app.routes.compras.update_artigos_administrativos_bulk',
            return_value={'updated': 2, 'unchanged': 0, 'unresolved': 1},
        ) as bulk_update:
            response = self.client.post('/compras/artigos', data=form_data)

        self.assertEqual(response.status_code, 302)
        self.assertIn('q=Caf%C3%A9', response.location)
        self.assertIn('revisao=por_rever', response.location)
        self.assertIn(f'scroll_context={"deadbeef" * 4}', response.location)
        self.assertEqual(
            bulk_update.call_args.args[0],
            [
                {
                    'artigo_id': 18,
                    'produto': 'Produto 18',
                    'marca': None,
                    'unidade': 'un',
                    'categoria_artigo': 'Higiene e limpeza',
                },
                {
                    'artigo_id': 19,
                    'produto': 'Produto 19',
                    'marca': 'Marca 19',
                    'unidade': 'kg',
                    'categoria_artigo': 'Bebidas e café',
                },
            ],
        )
        self.assertEqual(bulk_update.call_args.kwargs['actor'], 'testuser')

    def test_catalogue_bulk_validation_error_keeps_all_submitted_values(self):
        self._set_session_user(_user(acesso_compras=True))
        articles = [
            {
                'id': 18, 'fornecedor': 'Etiqueta antiga Café 18',
                'produto': 'Produto antigo 18', 'marca': None,
                'unidade': 'un', 'categoria_artigo': 'Por classificar',
                'origem_id': None, 'origem_nome': None,
                'origem_tipo': 'por_resolver',
                'origem_revisao_estado': 'por_rever', 'ativo': True,
            },
            {
                'id': 19, 'fornecedor': 'Etiqueta antiga Café 19',
                'produto': 'Produto antigo 19', 'marca': None,
                'unidade': 'un', 'categoria_artigo': 'Por classificar',
                'origem_id': None, 'origem_nome': None,
                'origem_tipo': 'por_resolver',
                'origem_revisao_estado': 'por_rever', 'ativo': True,
            },
        ]
        form_data = {
            'action': 'bulk_edit',
            'q': 'Café',
            'revisao': 'por_rever',
            'scroll_context': 'deadbeef' * 4,
            'artigo_id': ['18', '19'],
            'produto_18': 'Produto novo 18',
            'marca_18': '',
            'unidade_18': 'un',
            'categoria_artigo_18': 'Higiene e limpeza',
            'produto_19': 'Produto novo 19',
            'marca_19': '',
            'unidade_19': 'un',
            'categoria_artigo_19': 'Bebidas e café',
        }
        with patch(
            'flask_app.routes.compras.update_artigos_administrativos_bulk',
            side_effect=ValueError('Artigo #19: categoria inválida.'),
        ), patch(
            'flask_app.routes.compras.get_artigos_administrativos',
            return_value=articles,
        ), patch(
            'flask_app.routes.compras.get_suppliers',
            return_value=[],
        ), patch(
            'flask_app.routes.compras.render_template',
            return_value='catalogue',
        ) as render_catalogue:
            response = self.client.post('/compras/artigos', data=form_data)

        self.assertEqual(response.status_code, 200)
        kwargs = render_catalogue.call_args.kwargs
        self.assertEqual(kwargs['search'], 'café')
        self.assertEqual(kwargs['review_filter'], 'por_rever')
        self.assertEqual(kwargs['scroll_restore_token'], 'deadbeef' * 4)
        self.assertEqual(
            kwargs['bulk_error_message'],
            'Artigo #19: categoria inválida.',
        )
        self.assertEqual(
            kwargs['artigos'][0]['_bulk_edit']['categoria_artigo'],
            'Higiene e limpeza',
        )
        self.assertEqual(
            kwargs['artigos'][1]['_bulk_edit']['produto'],
            'Produto novo 19',
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

    def test_operational_supplier_filter_lists_canonical_suppliers(self):
        from flask_app.routes import compras as compras_routes

        self._set_session_user(_user(acesso_compras=True))
        compras_routes.render_template.reset_mock()
        suppliers = [{
            'id': 42,
            'name': 'Fornecedor Legal, Lda.',
            'common_name': 'Fornecedor Bolhão',
        }]
        with patch(
            'flask_app.routes.compras.get_suppliers',
            return_value=suppliers,
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
