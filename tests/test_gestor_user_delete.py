"""Regression tests for deleting a user from Gestor."""

import json
import os
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

from flask import Flask, render_template


class _DeleteButtonParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.onclick = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'button' and attributes.get('title') == 'Eliminar utilizador':
            self.onclick = attributes.get('onclick')


def _user(user_id, username, role='user'):
    return {'id': user_id, 'username': username, 'role': role, 'ativo': True,
            'vendas_store_ids': [], 'acesso_gestor': role == 'admin'}


class TestGestorUserDelete(unittest.TestCase):
    def setUp(self):
        from flask_app.routes.gestor import gestor_bp

        app = Flask(__name__, template_folder=os.path.abspath('flask_app/templates'))
        app.config['TESTING'] = True
        app.secret_key = 'test-only'
        app.register_blueprint(gestor_bp, url_prefix='/gestor')

        @app.route('/test-users')
        def test_users():
            return render_template(
                'gestor/_gestao_utilizadores.html',
                users=[_user(2, 'Ana "D\'Ávila" <teste>')],
                lojas_venda=[],
                audit_log=[],
            )

        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = _user(1, 'gestor', role='admin')

    def test_delete_button_passes_complete_username_to_modal(self):
        response = self.client.get('/test-users')
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        parser = _DeleteButtonParser()
        parser.feed(page)
        self.assertIsNotNone(parser.onclick)
        prefix = 'openDeleteModal(2, '
        self.assertTrue(parser.onclick.startswith(prefix))
        self.assertEqual(json.loads(parser.onclick[len(prefix):-1]),
                         'Ana "D\'Ávila" <teste>')
        self.assertIn('id="delete-form-2"', page)
        self.assertIn('id="delete-confirm-text"', page)

    def test_confirmed_delete_calls_atomic_delete_and_audits(self):
        with patch('flask_app.routes.gestor.db.get_all_users',
                   return_value=[_user(1, 'gestor', 'admin'), _user(2, 'Ana')]), \
             patch('flask_app.routes.gestor.db.delete_user_with_sessions') as delete, \
             patch('flask_app.routes.gestor.log_user_action') as audit:
            response = self.client.post(
                '/gestor/utilizadores/2/eliminar',
                data={'confirm_text': 'ELIMINAR'},
            )
        self.assertEqual(response.status_code, 302)
        delete.assert_called_once_with(2)
        audit.assert_called_once()
        with self.client.session_transaction() as session:
            self.assertIn('eliminado com sucesso', session['_flashes'][-1][1])

    def test_delete_failure_shows_error_without_recording_success(self):
        with patch('flask_app.routes.gestor.db.get_all_users',
                   return_value=[_user(2, 'Ana')]), \
             patch('flask_app.routes.gestor.db.delete_user_with_sessions',
                   side_effect=ValueError('database failure')), \
             patch('flask_app.routes.gestor.log_user_action') as audit:
            response = self.client.post(
                '/gestor/utilizadores/2/eliminar',
                data={'confirm_text': 'ELIMINAR'},
            )
        self.assertEqual(response.status_code, 302)
        audit.assert_not_called()
        with self.client.session_transaction() as session:
            self.assertIn('Não foi possível eliminar', session['_flashes'][-1][1])

    def test_guards_self_last_admin_and_invalid_confirmation(self):
        with patch('flask_app.routes.gestor.db.get_all_users',
                   return_value=[_user(1, 'gestor', 'admin'),
                                 _user(2, 'outro', 'admin'),
                                 _user(3, 'Ana')]), \
             patch('flask_app.routes.gestor.db.count_admin_users', return_value=1), \
             patch('flask_app.routes.gestor.db.delete_user_with_sessions') as delete:
            for user_id, confirmation in [(1, 'ELIMINAR'), (2, 'ELIMINAR'),
                                          (3, 'outro texto')]:
                with self.subTest(user_id=user_id):
                    response = self.client.post(
                        f'/gestor/utilizadores/{user_id}/eliminar',
                        data={'confirm_text': confirmation},
                    )
                    self.assertEqual(response.status_code, 302)
            delete.assert_not_called()

    def test_deactivation_still_revokes_sessions(self):
        with patch('flask_app.routes.gestor.db.get_all_users',
                   return_value=[_user(2, 'Ana')]), \
             patch('flask_app.routes.gestor.db.update_user') as update, \
             patch('flask_app.routes.gestor.db.revoke_user_sessions') as revoke, \
             patch('flask_app.routes.gestor.log_user_action'):
            response = self.client.post('/gestor/utilizadores/2/toggle-ativo')
        self.assertEqual(response.status_code, 302)
        update.assert_called_once_with(2, ativo=False)
        revoke.assert_called_once_with(2)


if __name__ == '__main__':
    unittest.main()