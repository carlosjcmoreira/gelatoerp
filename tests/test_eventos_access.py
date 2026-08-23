"""Access-control contracts for the internal Eventos module."""

import unittest
from unittest.mock import patch

from flask import Blueprint, Flask

from flask_app.routes.eventos import TABS, _get_tabs, eventos_bp


def _make_app():
    app = Flask(__name__)
    app.secret_key = 'eventos-access-test'
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
    app.register_blueprint(eventos_bp, url_prefix='/eventos')
    return app


class EventosAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()

    def setUp(self):
        self.client = self.app.test_client()

    def _set_user(self, **overrides):
        user = {
            'id': 1,
            'username': 'eventos-user',
            'acesso_eventos': False,
            'acesso_gestor': False,
        }
        user.update(overrides)
        with self.client.session_transaction() as session:
            session['user'] = user

    def test_eventos_user_can_open_module_landing(self):
        self._set_user(acesso_eventos=True)
        with patch('db.tiles.get_module_labels', return_value={}), \
             patch('db.tiles.get_tile_visibility', return_value={}), \
             patch('db.tiles.get_tile_labels', return_value={}), \
             patch('db.tiles.get_tile_icons', return_value={}), \
             patch('flask_app.routes.eventos.render_template', return_value='ok'):
            response = self.client.get('/eventos/')

        self.assertEqual(response.status_code, 200)

    def test_gestor_master_can_open_module_landing(self):
        self._set_user(acesso_gestor=True)
        with patch('db.tiles.get_module_labels', return_value={}), \
             patch('db.tiles.get_tile_visibility', return_value={}), \
             patch('db.tiles.get_tile_labels', return_value={}), \
             patch('db.tiles.get_tile_icons', return_value={}), \
             patch('flask_app.routes.eventos.render_template', return_value='ok'):
            response = self.client.get('/eventos/')

        self.assertEqual(response.status_code, 200)

    def test_eventos_user_can_open_configuration_tile(self):
        self._set_user(acesso_eventos=True)
        with patch('flask_app.routes.eventos.db.get_event_resources', return_value=[]), \
             patch('flask_app.routes.eventos.db.get_event_pricing_settings', return_value=[]), \
             patch('db.tiles.get_tile_visibility', return_value={}), \
             patch('db.tiles.get_tile_labels', return_value={}), \
             patch('db.tiles.get_tile_icons', return_value={}), \
             patch('flask_app.routes.eventos.render_template', return_value='ok'):
            response = self.client.get('/eventos/configuracao')

        self.assertEqual(response.status_code, 200)

    def test_user_without_eventos_access_is_blocked_from_all_internal_get_routes(self):
        self._set_user()
        routes = [
            '/eventos/',
            '/eventos/dashboard',
            '/eventos/pipeline',
            '/eventos/calendario',
            '/eventos/evento/novo',
            '/eventos/evento/1',
            '/eventos/configuracao',
            '/eventos/leads',
            '/eventos/leads/nova',
            '/eventos/leads/1',
            '/eventos/artigos',
            '/eventos/clientes',
            '/eventos/clientes/search',
            '/eventos/clientes/1',
            '/eventos/recebimentos',
            '/eventos/recebimentos/novo',
            '/eventos/recebimentos/evento/1',
            '/eventos/recebimentos/evento/1/editar',
            '/eventos/recebimentos/evento/1/pagamento',
            '/eventos/backfill-iva',
        ]

        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, '/')

    def test_user_without_eventos_access_is_blocked_from_internal_post_route(self):
        self._set_user()
        response = self.client.post('/eventos/evento/1/quote', data={'action': 'save_version'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/')

    def test_financeiro_only_user_cannot_bypass_eventos_access_via_vat_backfill(self):
        self._set_user(acesso_financeiro=True)
        response = self.client.get('/eventos/backfill-iva')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/')

    def test_eventos_and_financeiro_user_can_open_vat_backfill(self):
        self._set_user(acesso_eventos=True, acesso_financeiro=True)
        with patch('flask_app.routes.eventos.db.get_won_events_missing_taxa_iva', return_value=[]), \
             patch('flask_app.routes.eventos.render_template', return_value='ok'):
            response = self.client.get('/eventos/backfill-iva')

        self.assertEqual(response.status_code, 200)

    def test_event_tabs_are_all_available_to_tile_management(self):
        from flask_app.routes.gestor import _TILE_MASTER

        configured_ids = {tile['id'] for tile in _TILE_MASTER['eventos']}
        self.assertEqual({tab['id'] for tab in TABS}, configured_ids)

    def test_formulario_tile_opens_the_admin_form_editor(self):
        with self.app.test_request_context('/eventos/'):
            with patch('db.tiles.get_tile_visibility', return_value={}), \
                 patch('db.tiles.get_tile_labels', return_value={}), \
                 patch('db.tiles.get_tile_icons', return_value={}):
                tabs = {tab['id']: tab for tab in _get_tabs()}

        self.assertEqual(tabs['formulario']['label'], 'Formulário')
        self.assertEqual(tabs['formulario']['url'], '/eventos/configuracao/portal-marca')

    def test_hidden_event_tile_remains_hidden_for_authorized_users(self):
        with self.app.test_request_context('/eventos/'):
            with patch('db.tiles.get_tile_visibility', return_value={'calendario': False}), \
                 patch('db.tiles.get_tile_labels', return_value={}), \
                 patch('db.tiles.get_tile_icons', return_value={}):
                visible_ids = {tab['id'] for tab in _get_tabs()}

        self.assertNotIn('calendario', visible_ids)
        self.assertIn('pipeline', visible_ids)


if __name__ == '__main__':
    unittest.main()