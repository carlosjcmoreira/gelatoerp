import unittest
from contextlib import ExitStack
from unittest.mock import patch

from flask import Blueprint, Flask, session
from jinja2 import FileSystemLoader


def _make_app():
    from flask_app.routes.vendas import vendas_bp

    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True
    app.jinja_loader = FileSystemLoader('flask_app/templates')

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
    app.register_blueprint(vendas_bp, url_prefix='/vendas')
    return app


class TestComprasLoja(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()

    def _login(self, user):
        with self.app.test_client() as client:
            with client.session_transaction() as current:
                current['user'] = user
            return client

    @staticmethod
    def _store(store_id, name='Bolhão'):
        return {
            'id': store_id,
            'name': name,
            'store_type': 'loja',
            'is_active': True,
            'supports_vendas': True,
            'requires_eod_weighing': True,
        }

    def test_tile_is_canonical_and_supported_by_store_profiles(self):
        from flask_app.routes.gestor import _TILE_MASTER
        from flask_app.routes.vendas import TAB_DEFS, get_supported_vendas_tile_ids

        tile = next(tile for tile in TAB_DEFS if tile['id'] == 'compras_loja')
        self.assertEqual(tile['label'], 'Compras da Loja')
        self.assertEqual(tile['endpoint'], 'vendas.compras_loja')
        self.assertEqual(
            next(tile for tile in _TILE_MASTER['vendas'] if tile['id'] == 'compras_loja')['default_label'],
            'Compras da Loja',
        )
        self.assertIn('compras_loja', get_supported_vendas_tile_ids({
            'store_type': 'loja',
            'requires_eod_weighing': True,
        }))
        self.assertIn('compras_loja', get_supported_vendas_tile_ids({
            'store_type': 'producao',
            'requires_eod_weighing': False,
        }))

    def test_store_user_cannot_switch_compras_loja_by_query_string(self):
        client = self._login({
            'acesso_vendas': True,
            'acesso_gestor': False,
            'vendas_store_ids': [1],
        })
        with patch('flask_app.routes.vendas.get_store_by_id', return_value=self._store(1)), \
             patch('flask_app.routes.vendas.render_template', return_value='ok'):
            response = client.get('/vendas/compras-loja?loja_id=2')
        self.assertEqual(response.status_code, 403)

    def test_manager_context_uses_canonical_store_id_and_all_active_articles(self):
        client = self._login({
            'acesso_vendas': False,
            'acesso_gestor': True,
            'vendas_store_ids': [],
        })
        articles = [
            {
                'id': 1, 'produto': 'Farinha', 'fornecedor': 'Fornecedor A',
                'unidade': 'kg', 'ativo': True,
                'origem_tipo': 'fornecedor_externo', 'origem_supplier_id': 10,
            },
            {
                'id': 2, 'produto': 'Interno', 'fornecedor': 'Matosinhos',
                'unidade': 'kg', 'ativo': True,
                'origem_tipo': 'centro_interno', 'origem_supplier_id': None,
            },
            {
                'id': 3, 'produto': 'Inativo', 'fornecedor': 'Fornecedor A',
                'unidade': 'kg', 'ativo': False,
                'origem_tipo': 'fornecedor_externo', 'origem_supplier_id': 10,
            },
        ]
        with patch(
            'flask_app.routes.vendas.get_store_by_id',
            side_effect=lambda store_id: self._store(store_id, 'Matosinhos'),
        ), patch('flask_app.routes.vendas._build_tabs', return_value=[]), \
             patch('db.artigos.get_artigos_administrativos', return_value=articles), \
             patch('db.pedidos_urgentes.get_available_urgent_articles', return_value=[]), \
             patch('db.pedidos_urgentes.list_urgent_orders', return_value=[]), \
             patch('db.encomendas_semanais.get_weekly_order_for_store', return_value=None), \
             patch('flask_app.routes.vendas.render_template', return_value='ok') as render:
            response = client.get('/vendas/compras-loja?loja_id=2&secao=urgente')

        self.assertEqual(response.status_code, 200)
        kwargs = render.call_args.kwargs
        self.assertEqual(kwargs['loja_id'], 2)
        self.assertEqual(kwargs['loja_nome'], 'Matosinhos')
        self.assertEqual([article['id'] for article in kwargs['artigos_disponiveis']], [1, 2])
        self.assertEqual(kwargs['active_section'], 'urgente')

    @staticmethod
    def _active_articles():
        return [
            {
                'id': 10, 'produto': 'Farinha confirmada', 'unidade': 'kg',
                'ativo': True, 'origem_id': 1, 'origem_nome': 'Fornecedor A',
                'origem_tipo': 'fornecedor_externo', 'origem_ativa': True,
                'source_origin_id': 1, 'source_origin_name': 'Fornecedor A',
                'source_origin_type': 'fornecedor_externo', 'origin_active': True,
                'fornecedor_oficial_nome': 'Fornecedor A',
            },
            {
                'id': 11, 'produto': 'Caixa Matosinhos', 'unidade': 'un',
                'ativo': True, 'origem_id': 2, 'origem_nome': 'Matosinhos',
                'origem_tipo': 'centro_interno', 'origem_ativa': True,
                'source_origin_id': 2, 'source_origin_name': 'Matosinhos',
                'source_origin_type': 'centro_interno', 'origin_active': True,
                'fornecedor_oficial_nome': 'Fornecedor Interno',
            },
            {
                'id': 12, 'produto': 'Impressão Gráfica', 'unidade': 'un',
                'ativo': True, 'origem_id': 3, 'origem_nome': 'Gráfica',
                'origem_tipo': 'categoria_operacional', 'origem_ativa': True,
                'source_origin_id': 3, 'source_origin_name': 'Gráfica',
                'source_origin_type': 'categoria_operacional', 'origin_active': True,
                'fornecedor_oficial_nome': 'Fornecedor Gráfica',
            },
            {
                'id': 13, 'produto': 'Artigo por validar', 'unidade': 'un',
                'ativo': True, 'origem_id': None, 'origem_nome': None,
                'origem_tipo': 'por_resolver', 'origem_ativa': None,
                'source_origin_id': None, 'source_origin_name': None,
                'source_origin_type': 'por_resolver', 'origin_active': None,
                'fornecedor_oficial_nome': None,
            },
            {
                'id': 14, 'produto': 'Artigo inativo', 'unidade': 'un',
                'ativo': False, 'origem_id': 1, 'origem_nome': 'Fornecedor A',
                'origem_tipo': 'fornecedor_externo', 'origem_ativa': True,
                'source_origin_id': 1, 'source_origin_name': 'Fornecedor A',
                'source_origin_type': 'fornecedor_externo', 'origin_active': True,
                'fornecedor_oficial_nome': 'Fornecedor A',
            },
        ]

    def _render_store_section(
        self, client, section, weekly_status='rascunho', store_id=2
    ):
        articles = self._active_articles()
        active_articles = [article for article in articles if article['ativo']]
        order_lines = []
        if weekly_status == 'submetida':
            order_lines = [{
                'id': 501, 'artigo_id': 10,
                'produto_snapshot': 'Farinha histórica',
                'unidade_snapshot': 'kg', 'quantidade': 3,
                'observacoes': None, 'origem_nome_snapshot': 'Fornecedor A',
                'origem_tipo_snapshot': 'fornecedor_externo',
                'fornecedor_oficial_nome_snapshot': 'Fornecedor A',
            }]
        weekly_order = {
            'id': 50, 'status': weekly_status, 'linhas': order_lines,
            'observacoes': None,
        }
        count_draft = {
            'id': 70, 'status': 'rascunho', 'store_id': store_id, 'linhas': []
        }
        with ExitStack() as stack:
            stack.enter_context(patch(
                'flask_app.routes.vendas.get_store_by_id',
                side_effect=lambda requested_id: self._store(
                    int(requested_id),
                    'Matosinhos' if int(requested_id) == 1 else 'Bolhão',
                ),
            ))
            stack.enter_context(patch(
                'flask_app.routes.vendas._build_tabs', return_value=[]
            ))
            stack.enter_context(patch(
                'db.artigos.get_artigos_administrativos',
                return_value=articles,
            ))
            stack.enter_context(patch(
                'db.encomendas_semanais.get_or_create_weekly_order',
                return_value=weekly_order,
            ))
            stack.enter_context(patch(
                'db.encomendas_semanais.get_available_weekly_articles',
                return_value=active_articles,
            ))
            stack.enter_context(patch(
                'db.compras_envios.list_shipments_for_order',
                return_value=[],
            ))
            stack.enter_context(patch(
                'db.pedidos_urgentes.get_available_urgent_articles',
                return_value=active_articles,
            ))
            stack.enter_context(patch(
                'db.pedidos_urgentes.list_urgent_orders',
                return_value=[],
            ))
            stack.enter_context(patch(
                'db.encomendas_semanais.get_weekly_order_for_store',
                return_value=None,
            ))
            stack.enter_context(patch(
                'db.contagens_compras.get_or_create_count_draft',
                return_value=count_draft,
            ))
            stack.enter_context(patch(
                'db.contagens_compras.get_count_draft',
                return_value=count_draft,
            ))
            stack.enter_context(patch(
                'db.contagens_compras.get_available_count_articles',
                return_value=active_articles,
            ))
            stack.enter_context(patch(
                'db.contagens_compras.get_count_history',
                return_value=[],
            ))
            response = client.get(
                f'/vendas/compras-loja?loja_id={store_id}&secao={section}'
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_data(as_text=True)

    def test_real_weekly_html_shows_all_active_article_classes(self):
        client = self._login({
            'acesso_vendas': True, 'acesso_gestor': False,
            'vendas_store_ids': [1, 2],
        })
        html = self._render_store_section(client, 'semanal')
        self.assertIn('Encomenda para a semana seguinte', html)
        self.assertIn('Submeter no domingo', html)
        self.assertIn('quantidade_11', html)
        self.assertIn('quantidade_12', html)
        self.assertIn('quantidade_13', html)
        self.assertIn('Fornecedor por confirmar', html)
        self.assertNotIn('Artigo inativo', html)

    def test_user_assigned_both_stores_can_open_each_store_page(self):
        client = self._login({
            'acesso_vendas': True, 'acesso_gestor': False,
            'vendas_store_ids': [1, 2],
        })

        self._render_store_section(client, 'semanal', store_id=1)
        self._render_store_section(client, 'semanal', store_id=2)

    def test_real_urgent_html_renders_form_instead_of_weekly_empty_state(self):
        client = self._login({
            'acesso_vendas': True, 'acesso_gestor': False,
            'vendas_store_ids': [2],
        })
        html = self._render_store_section(client, 'urgente')
        self.assertIn('Pedido urgente — exceção de abastecimento', html)
        self.assertIn('name="data_pretendida"', html)
        self.assertIn('name="motivo"', html)
        self.assertIn('name="quantidade_12"', html)
        self.assertNotIn('Em preparação</span>', html)

    def test_real_count_html_renders_form_and_preserves_zero_rule(self):
        client = self._login({
            'acesso_vendas': True, 'acesso_gestor': False,
            'vendas_store_ids': [2],
        })
        html = self._render_store_section(client, 'contagem')
        self.assertIn('Contagem de artigos de Compras', html)
        self.assertIn('name="contagem_id" value="70"', html)
        self.assertIn('name="quantidade_13"', html)
        self.assertIn('O valor zero é uma contagem válida.', html)
        self.assertNotIn('Artigo inativo', html)

    def test_submitted_weekly_html_shows_immutable_snapshot_and_supplier(self):
        client = self._login({
            'acesso_vendas': True, 'acesso_gestor': False,
            'vendas_store_ids': [2],
        })
        html = self._render_store_section(
            client, 'semanal', weekly_status='submetida'
        )
        self.assertIn('Farinha histórica', html)
        self.assertIn('Fornecedor A', html)
        self.assertNotIn('name="quantidade_10"', html)

    def test_user_without_vendas_store_access_is_redirected(self):
        client = self._login({
            'acesso_vendas': False,
            'acesso_gestor': False,
            'vendas_store_ids': [],
        })
        response = client.get('/vendas/compras-loja')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/'))