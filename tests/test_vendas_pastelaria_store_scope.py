import unittest
from contextlib import contextmanager, ExitStack
from datetime import date
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from flask import Flask

from flask_app.routes.vendas import vendas_bp


class PastelariaSundayCountStoreScopeTests(unittest.TestCase):
    COUNT_DATE = date(2026, 9, 6)
    SNAPSHOT_TOKEN = 'ab' * 16
    MATOSINHOS_ID = 11
    BOLHAO_ID = 22

    def setUp(self):
        self.app = Flask(__name__, template_folder='../flask_app/templates')
        self.app.secret_key = 'test'
        self.app.add_url_rule(
            '/home', endpoint='home.index', view_func=lambda: ''
        )
        self.app.add_url_rule(
            '/login', endpoint='auth.login', view_func=lambda: ''
        )
        self.app.add_url_rule(
            '/logout', endpoint='auth.logout', view_func=lambda: ''
        )
        self.app.register_blueprint(vendas_bp, url_prefix='/vendas')
        self.client = self.app.test_client()
        self.stores = {
            self.MATOSINHOS_ID: {
                'id': self.MATOSINHOS_ID,
                'name': 'Matosinhos',
                'store_type': 'loja',
                'requires_eod_weighing': True,
                'supports_vendas': True,
                'is_active': True,
            },
            self.BOLHAO_ID: {
                'id': self.BOLHAO_ID,
                'name': 'Bolhão',
                'store_type': 'loja',
                'requires_eod_weighing': True,
                'supports_vendas': True,
                'is_active': True,
            },
        }

    def _set_user(self, store_ids=(), gestor=False):
        with self.client.session_transaction() as session:
            session['user'] = {
                'username': 'test-user',
                'role': 'vendas',
                'acesso_gestor': gestor,
                'vendas_store_ids': list(store_ids),
            }

    def _grid(self, store_id):
        return {
            'products': [{
                'id': 70,
                'nome': 'Pastel de nata',
                'count': None,
                'counts': {store_id: None},
            }],
            'stores': [self.stores[store_id]],
            'completed': 0,
            'total': 1,
            'complete': False,
            'snapshot_token': self.SNAPSHOT_TOKEN,
        }

    @contextmanager
    def _patch_route(self):
        stack = ExitStack()
        with stack:
            mocks = {
                'get_grid': stack.enter_context(patch(
                    'flask_app.routes.vendas.get_pastelaria_sunday_count_grid',
                    side_effect=lambda _date, store_id: self._grid(store_id),
                )),
                'save_counts': stack.enter_context(patch(
                    'flask_app.routes.vendas.save_pastelaria_store_counts',
                    return_value=1,
                )),
                'save_legacy': stack.enter_context(patch(
                    'flask_app.routes.vendas.save_pastelaria_sunday_counts',
                    return_value=1,
                )),
                'build_tabs': stack.enter_context(patch(
                    'flask_app.routes.vendas._build_tabs',
                    return_value=[],
                )),
                'get_stores': stack.enter_context(patch(
                    'flask_app.routes.vendas.get_vendas_module_stores',
                    return_value=list(self.stores.values()),
                )),
                'get_store': stack.enter_context(patch(
                    'flask_app.routes.vendas.get_store_by_id',
                    side_effect=self.stores.get,
                )),
            }
            yield mocks

    def _post_data(self, store_id):
        return {
            'action': 'guardar_grelha',
            'loja_id': str(store_id),
            'data_contagem': self.COUNT_DATE.isoformat(),
            'snapshot_token': self.SNAPSHOT_TOKEN,
            'count_70': '',
        }

    def test_user_with_two_stores_opens_selected_store(self):
        self._set_user([self.MATOSINHOS_ID, self.BOLHAO_ID])

        for store_id in (self.BOLHAO_ID, self.MATOSINHOS_ID):
            with self.subTest(store_id=store_id), self._patch_route() as mocks:
                response = self.client.get(
                    '/vendas/contagem-pastelaria'
                    f'?loja_id={store_id}&data_contagem={self.COUNT_DATE}'
                )

            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn(
                f'Contagem Pastelaria — {self.stores[store_id]["name"]}',
                page,
            )
            self.assertIn(
                f'action="/vendas/contagem-pastelaria?loja_id={store_id}"',
                page,
            )
            self.assertIn(f'name="loja_id" value="{store_id}"', page)
            mocks['get_grid'].assert_called_once_with(self.COUNT_DATE, store_id)
            mocks['build_tabs'].assert_called_once_with(
                'contagem_pastelaria', store_id
            )

    def test_user_with_two_stores_saves_blanks_to_selected_store(self):
        self._set_user([self.MATOSINHOS_ID, self.BOLHAO_ID])

        for store_id in (self.BOLHAO_ID, self.MATOSINHOS_ID):
            with self.subTest(store_id=store_id), self._patch_route() as mocks:
                response = self.client.post(
                    '/vendas/contagem-pastelaria'
                    f'?loja_id={store_id}',
                    data=self._post_data(store_id),
                )

            self.assertEqual(response.status_code, 302)
            mocks['get_grid'].assert_called_once_with(self.COUNT_DATE, store_id)
            mocks['save_counts'].assert_called_once_with(
                self.COUNT_DATE,
                store_id,
                [(70, 0)],
                self.SNAPSHOT_TOKEN,
            )
            redirect_query = parse_qs(urlparse(response.location).query)
            self.assertEqual(redirect_query['loja_id'], [str(store_id)])
            self.assertEqual(
                redirect_query['data_contagem'],
                [self.COUNT_DATE.isoformat()],
            )

    def test_unassigned_store_is_rejected_for_get_and_post(self):
        self._set_user([self.MATOSINHOS_ID])

        requests = (
            lambda: self.client.get(
                '/vendas/contagem-pastelaria'
                f'?loja_id={self.BOLHAO_ID}&data_contagem={self.COUNT_DATE}'
            ),
            lambda: self.client.post(
                '/vendas/contagem-pastelaria'
                f'?loja_id={self.BOLHAO_ID}',
                data=self._post_data(self.BOLHAO_ID),
            ),
        )
        for make_request in requests:
            with (
                self.subTest(method=make_request.__name__),
                self._patch_route() as mocks,
            ):
                response = make_request()

            self.assertEqual(response.status_code, 403)
            mocks['get_grid'].assert_not_called()
            mocks['save_counts'].assert_not_called()
            mocks['save_legacy'].assert_not_called()

    def test_invalid_or_conflicting_store_ids_are_rejected_before_read(self):
        self._set_user([self.MATOSINHOS_ID, self.BOLHAO_ID])

        requests = (
            lambda: self.client.get(
                '/vendas/contagem-pastelaria?loja_id=not-a-number'
            ),
            lambda: self.client.post(
                '/vendas/contagem-pastelaria'
                f'?loja_id={self.BOLHAO_ID}',
                data=self._post_data(self.MATOSINHOS_ID),
            ),
            lambda: self.client.get(
                f'/vendas/contagem-pastelaria?loja_id={self.BOLHAO_ID}'
                f'&loja_id={self.MATOSINHOS_ID}'
            ),
        )
        for make_request in requests:
            with (
                self.subTest(request=make_request.__name__),
                self._patch_route() as mocks,
            ):
                response = make_request()

            self.assertEqual(response.status_code, 403)
            mocks['get_grid'].assert_not_called()
            mocks['save_counts'].assert_not_called()

    def test_manager_can_select_and_save_store(self):
        self._set_user(gestor=True)
        store_id = self.BOLHAO_ID
        url = (
            '/vendas/contagem-pastelaria'
            f'?loja_id={store_id}&data_contagem={self.COUNT_DATE}'
        )

        with self._patch_route() as get_mocks:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Contagem Pastelaria — Bolhão', page)
        self.assertIn(
            f'<option value="{store_id}" selected>',
            page,
        )
        get_mocks['get_grid'].assert_called_once_with(self.COUNT_DATE, store_id)
        get_mocks['build_tabs'].assert_called_once_with(
            'contagem_pastelaria', store_id
        )

        with self._patch_route() as post_mocks:
            response = self.client.post(
                f'/vendas/contagem-pastelaria?loja_id={store_id}',
                data=self._post_data(store_id),
            )

        self.assertEqual(response.status_code, 302)
        post_mocks['save_counts'].assert_called_once_with(
            self.COUNT_DATE,
            store_id,
            [(70, 0)],
            self.SNAPSHOT_TOKEN,
        )

    def test_missing_store_id_keeps_first_assigned_store_as_default(self):
        self._set_user([self.MATOSINHOS_ID, self.BOLHAO_ID])

        with self._patch_route() as mocks:
            response = self.client.get(
                '/vendas/contagem-pastelaria'
                f'?data_contagem={self.COUNT_DATE}'
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'Contagem Pastelaria — Matosinhos',
            response.get_data(as_text=True),
        )
        mocks['get_grid'].assert_called_once_with(
            self.COUNT_DATE, self.MATOSINHOS_ID
        )


if __name__ == '__main__':
    unittest.main()