import unittest
from contextlib import ExitStack, contextmanager
from datetime import date
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from flask import Flask

from flask_app.routes.vendas import (
    get_supported_vendas_tile_ids,
    vendas_bp,
)


class VendasConfeitariaCountRouteTests(unittest.TestCase):
    COUNT_DATE = date(2026, 9, 9)
    SNAPSHOT_TOKEN = 'cd' * 16
    MATOSINHOS_ID = 11
    BOLHAO_ID = 22
    INACTIVE_ID = 33
    DISABLED_ID = 44

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
                'store_type': 'producao',
                'requires_eod_weighing': False,
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
            self.INACTIVE_ID: {
                'id': self.INACTIVE_ID,
                'name': 'Encerrada',
                'store_type': 'loja',
                'requires_eod_weighing': True,
                'supports_vendas': True,
                'is_active': False,
            },
            self.DISABLED_ID: {
                'id': self.DISABLED_ID,
                'name': 'Sem Vendas',
                'store_type': 'loja',
                'requires_eod_weighing': True,
                'supports_vendas': False,
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
                'nome': 'Cookie de amêndoa',
                'count': None,
            }],
            'store': self.stores[store_id],
            'stores': [self.stores[store_id]],
            'completed': 0,
            'total': 1,
            'complete': False,
            'snapshot_token': self.SNAPSHOT_TOKEN,
        }

    @contextmanager
    def _patch_route(self, *, save_side_effect=None):
        stack = ExitStack()
        with stack:
            mocks = {
                'get_grid': stack.enter_context(patch(
                    'flask_app.routes.vendas.get_confeitaria_store_count_grid',
                    side_effect=lambda _date, store_id: self._grid(store_id),
                )),
                'save_counts': stack.enter_context(patch(
                    'flask_app.routes.vendas.save_confeitaria_store_counts',
                    side_effect=save_side_effect,
                    return_value=1,
                )),
                'get_history': stack.enter_context(patch(
                    'flask_app.routes.vendas.get_confeitaria_count_submission_history',
                    return_value=[],
                )),
                'build_tabs': stack.enter_context(patch(
                    'flask_app.routes.vendas._build_tabs',
                    return_value=[],
                )),
                'get_stores': stack.enter_context(patch(
                    'flask_app.routes.vendas.get_vendas_module_stores',
                    return_value=[
                        self.stores[self.MATOSINHOS_ID],
                        self.stores[self.BOLHAO_ID],
                        self.stores[self.INACTIVE_ID],
                        self.stores[self.DISABLED_ID],
                    ],
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

    def test_assigned_store_can_open_any_calendar_date(self):
        self._set_user([self.MATOSINHOS_ID, self.BOLHAO_ID])
        with self._patch_route() as mocks:
            response = self.client.get(
                '/vendas/contagem-confeitaria'
                f'?loja_id={self.MATOSINHOS_ID}&data_contagem={self.COUNT_DATE}'
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Contagem Confeitaria — Matosinhos', page)
        self.assertIn('table-responsive', page)
        self.assertIn('name="count_70"', page)
        mocks['get_grid'].assert_called_once_with(
            self.COUNT_DATE, self.MATOSINHOS_ID
        )
        mocks['build_tabs'].assert_called_once_with(
            'contagem_confeitaria', self.MATOSINHOS_ID
        )

    def test_assigned_store_saves_blank_as_zero_with_stable_scope(self):
        self._set_user([self.MATOSINHOS_ID, self.BOLHAO_ID])
        with self._patch_route() as mocks:
            response = self.client.post(
                '/vendas/contagem-confeitaria'
                f'?loja_id={self.BOLHAO_ID}',
                data=self._post_data(self.BOLHAO_ID),
            )

        self.assertEqual(response.status_code, 302)
        mocks['get_grid'].assert_called_once_with(
            self.COUNT_DATE, self.BOLHAO_ID
        )
        mocks['save_counts'].assert_called_once_with(
            self.COUNT_DATE,
            self.BOLHAO_ID,
            [(70, 0)],
            self.SNAPSHOT_TOKEN,
            submitted_by='test-user',
        )
        redirect_query = parse_qs(urlparse(response.location).query)
        self.assertEqual(redirect_query['loja_id'], [str(self.BOLHAO_ID)])
        self.assertEqual(
            redirect_query['data_contagem'],
            [self.COUNT_DATE.isoformat()],
        )

    def test_unassigned_store_is_rejected_before_count_access(self):
        self._set_user([self.MATOSINHOS_ID])
        requests = (
            lambda: self.client.get(
                '/vendas/contagem-confeitaria'
                f'?loja_id={self.BOLHAO_ID}&data_contagem={self.COUNT_DATE}'
            ),
            lambda: self.client.post(
                '/vendas/contagem-confeitaria'
                f'?loja_id={self.BOLHAO_ID}',
                data=self._post_data(self.BOLHAO_ID),
            ),
        )
        for make_request in requests:
            with self.subTest(request=make_request.__name__), self._patch_route() as mocks:
                response = make_request()
            self.assertEqual(response.status_code, 403)
            mocks['get_grid'].assert_not_called()
            mocks['save_counts'].assert_not_called()

    def test_malformed_and_conflicting_store_ids_are_rejected_before_read(self):
        self._set_user([self.MATOSINHOS_ID, self.BOLHAO_ID])
        requests = (
            lambda: self.client.get(
                '/vendas/contagem-confeitaria?loja_id=not-a-number'
            ),
            lambda: self.client.post(
                f'/vendas/contagem-confeitaria?loja_id={self.BOLHAO_ID}',
                data=self._post_data(self.MATOSINHOS_ID),
            ),
            lambda: self.client.get(
                '/vendas/contagem-confeitaria'
                f'?loja_id={self.BOLHAO_ID}&loja_id={self.MATOSINHOS_ID}'
            ),
        )
        for make_request in requests:
            with self.subTest(request=make_request.__name__), self._patch_route() as mocks:
                response = make_request()
            self.assertEqual(response.status_code, 403)
            mocks['get_grid'].assert_not_called()

    def test_manager_can_select_any_active_vendas_store(self):
        self._set_user(gestor=True)
        with self._patch_route() as mocks:
            response = self.client.get(
                '/vendas/contagem-confeitaria'
                f'?loja_id={self.MATOSINHOS_ID}&data_contagem={self.COUNT_DATE}'
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn(f'<option value="{self.MATOSINHOS_ID}" selected>', page)
        self.assertNotIn(f'<option value="{self.INACTIVE_ID}"', page)
        self.assertNotIn(f'<option value="{self.DISABLED_ID}"', page)
        mocks['get_grid'].assert_called_once_with(
            self.COUNT_DATE, self.MATOSINHOS_ID
        )
        mocks['get_history'].assert_called_once_with(
            self.COUNT_DATE, self.MATOSINHOS_ID
        )

    def test_inactive_or_vendas_disabled_store_is_rejected_even_for_manager(self):
        self._set_user(gestor=True)
        for store_id in (self.INACTIVE_ID, self.DISABLED_ID):
            with self.subTest(store_id=store_id), self._patch_route() as mocks:
                response = self.client.get(
                    f'/vendas/contagem-confeitaria?loja_id={store_id}'
                )
            self.assertEqual(response.status_code, 403)
            mocks['get_grid'].assert_not_called()

    def test_incomplete_or_stale_forms_do_not_lose_submitted_values(self):
        self._set_user([self.BOLHAO_ID])
        incomplete = self._post_data(self.BOLHAO_ID)
        incomplete.pop('count_70')
        with self._patch_route() as mocks:
            response = self.client.post(
                f'/vendas/contagem-confeitaria?loja_id={self.BOLHAO_ID}',
                data=incomplete,
            )
        self.assertEqual(response.status_code, 200)
        mocks['save_counts'].assert_not_called()
        self.assertIn('grelha recebida está incompleta', response.get_data(as_text=True))

        with self._patch_route(
            save_side_effect=ValueError('Esta grelha foi alterada por outro utilizador.')
        ) as mocks:
            response = self.client.post(
                f'/vendas/contagem-confeitaria?loja_id={self.BOLHAO_ID}',
                data={**self._post_data(self.BOLHAO_ID), 'count_70': '7'},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocks['get_grid'].call_count, 2)
        self.assertIn('value="7"', response.get_data(as_text=True))


class VendasConfeitariaTileTests(unittest.TestCase):
    def test_tile_is_canonical_and_only_for_active_vendas_stores(self):
        from flask_app.routes.gestor import _TILE_MASTER
        from flask_app.routes.vendas import TAB_DEFS

        tile = next(
            item for item in TAB_DEFS
            if item['id'] == 'contagem_confeitaria'
        )
        manager_tile = next(
            item for item in _TILE_MASTER['vendas']
            if item['id'] == 'contagem_confeitaria'
        )
        self.assertEqual(tile['label'], manager_tile['default_label'])
        eligible = {
            'store_type': 'producao',
            'is_active': True,
            'supports_vendas': True,
            'requires_eod_weighing': False,
        }
        self.assertIn(
            'contagem_confeitaria',
            get_supported_vendas_tile_ids(eligible),
        )
        self.assertNotIn(
            'contagem_confeitaria',
            get_supported_vendas_tile_ids({**eligible, 'is_active': False}),
        )
        self.assertNotIn(
            'contagem_confeitaria',
            get_supported_vendas_tile_ids({**eligible, 'supports_vendas': False}),
        )
        self.assertNotIn(
            'contagem_confeitaria',
            get_supported_vendas_tile_ids(None),
        )


if __name__ == '__main__':
    unittest.main()