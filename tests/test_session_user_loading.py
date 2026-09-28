import unittest
from unittest.mock import patch

from flask import Blueprint, Flask, jsonify, session

from flask_app.app import _load_session_user
from flask_app.auth import login_required


def _make_app():
    from flask_app.routes.vendas import _get_compras_loja_store

    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.config['TESTING'] = True
    app.before_request(_load_session_user)

    auth_bp = Blueprint('auth', __name__)

    @auth_bp.route('/login')
    def login():
        return 'login', 200

    app.register_blueprint(auth_bp)

    @app.route('/vendas/compras-loja')
    @login_required
    def compras_loja():
        store = _get_compras_loja_store()
        return jsonify(store_id=store['id'])

    return app


class TestSessionUserLoading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()

    @staticmethod
    def _user(store_ids):
        return {
            'id': 7,
            'acesso_vendas': bool(store_ids),
            'acesso_gestor': False,
            'vendas_store_ids': store_ids,
        }

    def test_valid_token_refreshes_old_cookie_before_store_access(self):
        client = self.app.test_client()
        fresh_user = self._user([1, 2])
        with client.session_transaction() as current:
            current['user'] = self._user([1])
            current['token'] = 'valid-session'

        store = {
            'id': 2,
            'name': 'Bolhão',
            'is_active': True,
            'supports_vendas': True,
        }
        with patch(
            'db.auth.get_session_user', return_value=fresh_user
        ) as get_session_user, patch(
            'flask_app.routes.vendas.get_store_by_id', return_value=store
        ):
            response = client.get('/vendas/compras-loja?loja_id=2')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'store_id': 2})
        get_session_user.assert_called_once_with('valid-session')

    def test_one_store_user_still_cannot_open_another_store(self):
        client = self.app.test_client()
        with client.session_transaction() as current:
            current['user'] = self._user([1, 2])
            current['token'] = 'valid-session'

        with patch(
            'db.auth.get_session_user', return_value=self._user([1])
        ), patch('flask_app.routes.vendas.get_store_by_id') as get_store:
            response = client.get('/vendas/compras-loja?loja_id=2')

        self.assertEqual(response.status_code, 403)
        get_store.assert_not_called()

    def test_tokenless_cookie_user_must_log_in_again(self):
        client = self.app.test_client()
        with client.session_transaction() as current:
            current['user'] = self._user([1])
            current['public_portal_state'] = 'preserved'

        response = client.get('/vendas/compras-loja?loja_id=2')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/login'))
        with client.session_transaction() as current:
            self.assertNotIn('user', current)
            self.assertEqual(current['public_portal_state'], 'preserved')