import unittest
from unittest.mock import patch

from flask import Blueprint, Flask, session


def _make_app():
    from flask_app.routes.vendas import vendas_bp

    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True

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

    def test_manager_context_uses_canonical_store_id_and_only_active_external_articles(self):
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
             patch('flask_app.routes.vendas.render_template', return_value='ok') as render:
            response = client.get('/vendas/compras-loja?loja_id=2&secao=urgente')

        self.assertEqual(response.status_code, 200)
        kwargs = render.call_args.kwargs
        self.assertEqual(kwargs['loja_id'], 2)
        self.assertEqual(kwargs['loja_nome'], 'Matosinhos')
        self.assertEqual([article['id'] for article in kwargs['artigos_disponiveis']], [1])
        self.assertEqual(kwargs['active_section'], 'urgente')

    def test_user_without_vendas_store_access_is_redirected(self):
        client = self._login({
            'acesso_vendas': False,
            'acesso_gestor': False,
            'vendas_store_ids': [],
        })
        response = client.get('/vendas/compras-loja')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/'))