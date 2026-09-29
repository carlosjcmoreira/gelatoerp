import unittest
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import patch

from flask import Flask

from db.confeitaria_stock import ConfeitariaStockError
from flask_app.routes.confeitaria import TABS, confeitaria_bp


class ConfeitariaStockRouteTests(unittest.TestCase):
    COUNT_DATE = date(2026, 9, 29)
    REQUEST_KEY = 'd6cd5083-1e77-4dc0-9eb2-90a83fcfbe76'

    def setUp(self):
        self.app = Flask(
            __name__,
            template_folder='../flask_app/templates',
        )
        self.app.secret_key = 'test'
        self.app.register_blueprint(confeitaria_bp, url_prefix='/confeitaria')
        self.app.add_url_rule(
            '/test-home', endpoint='home.index', view_func=lambda: 'home'
        )
        self.app.add_url_rule(
            '/test-login', endpoint='auth.login', view_func=lambda: 'login'
        )
        self.client = self.app.test_client()
        self._set_user(acesso_confeitaria=True)
        self.options = [
            {
                'produto_confeitaria_id': 7,
                'produto_id': 7,
                'produto': 'Cookie Ativo',
                'ativo': True,
                'saldo': 0,
                'saldo_inicial_confirmado': False,
                'transferivel': False,
            },
            {
                'produto_confeitaria_id': 8,
                'produto_id': 8,
                'produto': 'Cookie Arquivado',
                'ativo': False,
                'saldo': 3,
                'saldo_inicial_confirmado': True,
                'transferivel': False,
            },
        ]
        self.movements = [{
            'id': 15,
            'produto_confeitaria_id': 8,
            'produto': 'Cookie Arquivado',
            'tipo': 'saldo_inicial',
            'tipo_label': 'Saldo inicial',
            'quantidade': 3,
            'data': self.COUNT_DATE,
            'responsavel': 'gestor-antigo',
            'motivo': 'Saldo confirmado antes de desativar',
            'idempotency_key': self.REQUEST_KEY,
            'created_at': datetime(2026, 9, 29, 10, 0),
        }]
        self.legacy_stock = [{
            'produto': 'Cookie Ativo — registo antigo',
            'quantidade': 11,
        }]

    def _set_user(self, **permissions):
        with self.client.session_transaction() as session:
            session['user'] = {
                'username': 'gestor-confeitaria',
                'role': 'gestor',
                **permissions,
            }

    def _take_flashes(self):
        with self.client.session_transaction() as session:
            messages = [
                message for _category, message in session.get('_flashes', [])
            ]
            session.pop('_flashes', None)
        return messages

    @contextmanager
    def _page_data(self):
        with (
            patch(
                'flask_app.routes.confeitaria._tabs_with_urls',
                return_value=[],
            ),
            patch(
                'flask_app.routes.confeitaria.get_confeitaria_stock_options',
                return_value=self.options,
            ),
            patch(
                'flask_app.routes.confeitaria.get_confeitaria_stock_movements',
                return_value=self.movements,
            ),
            patch(
                'flask_app.routes.confeitaria.get_stock_producao_area_all',
                return_value=self.legacy_stock,
            ),
        ):
            yield

    def _post_data(self, **overrides):
        data = {
            'produto_confeitaria_id': '7',
            'tipo': 'saldo_inicial',
            'quantidade': '0',
            'data': self.COUNT_DATE.isoformat(),
            'motivo': 'Contagem física de produção',
            'idempotency_key': self.REQUEST_KEY,
        }
        data.update(overrides)
        return data

    def test_get_shows_audited_legacy_and_inactive_history_separately(self):
        with self._page_data():
            response = self.client.get('/confeitaria/stock-producao')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Saldo auditado por produto', page)
        self.assertIn('Saldo digital antigo', page)
        self.assertIn('Cookie Ativo', page)
        self.assertIn('Cookie Ativo — registo antigo', page)
        self.assertIn('Cookie Arquivado', page)
        self.assertIn('Saldo confirmado antes de desativar', page)
        self.assertIn('gestor-antigo', page)
        self.assertIn('11', page)
        self.assertIn(
            'transferências ainda não geram',
            page,
        )
        self.assertIn('disabled', page)

    def test_production_stock_is_registered_in_confeitaria_navigation(self):
        with (
            patch('db.tiles.get_tile_visibility', return_value={}),
            patch('db.tiles.get_tile_labels', return_value={}),
            patch('db.tiles.get_tile_icons', return_value={}),
            patch('db.tiles.get_module_labels', return_value={}),
        ):
            response = self.client.get('/confeitaria/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('/confeitaria/stock-producao',
                      response.get_data(as_text=True))
        self.assertIn(
            'stock_producao',
            {tile['id'] for tile in TABS},
        )

    def test_post_accepts_zero_opening_and_uses_server_author(self):
        result = {
            'produto': 'Cookie Ativo',
            'saldo': 0,
            'replayed': False,
        }
        with patch(
            'flask_app.routes.confeitaria.register_confeitaria_stock_movement',
            return_value=result,
        ) as register:
            response = self.client.post(
                '/confeitaria/stock-producao',
                data=self._post_data(),
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith(
            '/confeitaria/stock-producao'
        ))
        register.assert_called_once_with(
            produto_confeitaria_id=7,
            tipo='saldo_inicial',
            quantidade=0,
            data=self.COUNT_DATE,
            responsavel='gestor-confeitaria',
            motivo='Contagem física de produção',
            idempotency_key=self.REQUEST_KEY,
        )

    def test_post_submits_signed_correction_with_reason_and_key(self):
        with patch(
            'flask_app.routes.confeitaria.register_confeitaria_stock_movement',
            return_value={
                'produto': 'Cookie Ativo',
                'saldo': 4,
                'replayed': False,
            },
        ) as register:
            response = self.client.post(
                '/confeitaria/stock-producao',
                data=self._post_data(
                    tipo='correcao',
                    quantidade='-2',
                    motivo='Quebra detetada na conferência',
                ),
            )

        self.assertEqual(response.status_code, 302)
        register.assert_called_once_with(
            produto_confeitaria_id=7,
            tipo='correcao',
            quantidade=-2,
            data=self.COUNT_DATE,
            responsavel='gestor-confeitaria',
            motivo='Quebra detetada na conferência',
            idempotency_key=self.REQUEST_KEY,
        )

    def test_invalid_quantity_does_not_write_and_keeps_retry_key(self):
        with (
            patch(
                'flask_app.routes.confeitaria.register_confeitaria_stock_movement'
            ) as register,
            self._page_data(),
        ):
            response = self.client.post(
                '/confeitaria/stock-producao',
                data=self._post_data(quantidade='2.5'),
            )

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('quantidade inteira', page)
        self.assertIn(self.REQUEST_KEY, page)
        register.assert_not_called()

    def test_missing_idempotency_key_is_rejected_without_writing(self):
        data = self._post_data()
        data.pop('idempotency_key')
        with (
            patch(
                'flask_app.routes.confeitaria.register_confeitaria_stock_movement'
            ) as register,
            self._page_data(),
        ):
            response = self.client.post(
                '/confeitaria/stock-producao',
                data=data,
            )

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('O formulário expirou', page)
        self.assertNotIn(self.REQUEST_KEY, page)
        register.assert_not_called()

    def test_service_validation_error_keeps_existing_history_visible(self):
        with (
            patch(
                'flask_app.routes.confeitaria.register_confeitaria_stock_movement',
                side_effect=ConfeitariaStockError(
                    'O produto está inativo e não aceita novos movimentos.'
                ),
            ) as register,
            self._page_data(),
        ):
            response = self.client.post(
                '/confeitaria/stock-producao',
                data=self._post_data(produto_confeitaria_id='8'),
            )

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('O produto está inativo', page)
        self.assertIn('Saldo confirmado antes de desativar', page)
        register.assert_called_once()

    def test_module_permission_protects_get_and_post(self):
        self._set_user(acesso_confeitaria=False)
        with patch(
            'flask_app.routes.confeitaria.register_confeitaria_stock_movement'
        ) as register:
            get_response = self.client.get('/confeitaria/stock-producao')
            post_response = self.client.post(
                '/confeitaria/stock-producao',
                data=self._post_data(),
            )

        for response in (get_response, post_response):
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers['Location'].endswith('/test-home'))
        register.assert_not_called()

    def test_replayed_request_reports_that_no_movement_was_added(self):
        with patch(
            'flask_app.routes.confeitaria.register_confeitaria_stock_movement',
            return_value={
                'produto': 'Cookie Ativo',
                'saldo': 6,
                'replayed': True,
            },
        ):
            response = self.client.post(
                '/confeitaria/stock-producao',
                data=self._post_data(quantidade='6'),
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('não foi duplicado', ' '.join(self._take_flashes()))

    def test_production_page_keeps_saved_values_editable_including_zero(self):
        plan = [
            {
                'produto': 'Cookie Ativo',
                'estimado': 3,
                'real': 0,
            },
            {
                'produto': 'Cookie Arquivado',
                'estimado': 5,
                'real': 7,
            },
        ]
        with (
            patch(
                'flask_app.routes.confeitaria._tabs_with_urls',
                return_value=[],
            ),
            patch(
                'flask_app.routes.confeitaria.get_plano_do_dia_area',
                return_value=plan,
            ),
        ):
            response = self.client.get('/confeitaria/produzir')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('2/2 registados', page)
        self.assertIn('name="real_Cookie Ativo"', page)
        self.assertIn('name="real_Cookie Arquivado"', page)
        self.assertIn('value="0"', page)
        self.assertIn('value="7"', page)

    def test_production_post_saves_only_values_entered_for_plan_products(self):
        plan = [
            {'produto': 'Cookie Ativo'},
            {'produto': 'Cookie Arquivado'},
        ]
        with (
            patch(
                'flask_app.routes.confeitaria.get_plano_do_dia_area',
                return_value=plan,
            ),
            patch(
                'flask_app.routes.confeitaria.record_confeitaria_production_batch',
                return_value=[
                    {'changed': True},
                    {'changed': False},
                ],
            ) as record,
        ):
            response = self.client.post(
                '/confeitaria/produzir',
                data={
                    'real_Cookie Ativo': '0',
                    'real_Cookie Arquivado': '8',
                    'real_NotInPlan': '99',
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith(
            '/confeitaria/produzir'
        ))
        record.assert_called_once_with(
            data=date.today(),
            production_values={
                'Cookie Ativo': 0,
                'Cookie Arquivado': 8,
            },
            responsavel='gestor-confeitaria',
        )

    def test_invalid_production_quantity_does_not_call_atomic_service(self):
        with (
            patch(
                'flask_app.routes.confeitaria.get_plano_do_dia_area',
                return_value=[{'produto': 'Cookie Ativo'}],
            ),
            patch(
                'flask_app.routes.confeitaria.record_confeitaria_production_batch'
            ) as record,
        ):
            response = self.client.post(
                '/confeitaria/produzir',
                data={'real_Cookie Ativo': '3.5'},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('número inteiro', ' '.join(self._take_flashes()))
        record.assert_not_called()

    def test_production_page_requires_confeitaria_permission(self):
        self._set_user(acesso_confeitaria=False)
        with (
            patch(
                'flask_app.routes.confeitaria.get_plano_do_dia_area'
            ) as get_plan,
            patch(
                'flask_app.routes.confeitaria.record_confeitaria_production_batch'
            ) as record,
        ):
            response = self.client.post(
                '/confeitaria/produzir',
                data={'real_Cookie Ativo': '4'},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/test-home'))
        get_plan.assert_not_called()
        record.assert_not_called()

    def test_stock_overview_uses_dynamic_store_ids_and_keeps_dates_separate(self):
        stores = [
            {
                'id': 11,
                'name': 'Matosinhos',
                'store_type': 'producao',
                'requires_eod_weighing': True,
            },
            {
                'id': 24,
                'name': 'Loja Nova',
                'store_type': 'loja',
                'requires_eod_weighing': True,
            },
        ]
        products = [
            {
                'produto_confeitaria_id': 7,
                'produto': 'Cookie Ativo',
                'ativo': True,
                'saldo': 14,
            },
            {
                'produto_confeitaria_id': 8,
                'produto': 'Cookie Arquivado',
                'ativo': False,
                'saldo': 3,
            },
        ]
        count_data = {
            'latest_counts': [
                {
                    'id': 101,
                    'produto_confeitaria_id': 7,
                    'store_id': 11,
                    'data': date(2026, 9, 22),
                    'quantidade': 4,
                },
                {
                    'id': 102,
                    'produto_confeitaria_id': 7,
                    'store_id': 24,
                    'data': date(2026, 9, 28),
                    'quantidade': 3,
                },
                {
                    'id': 103,
                    'produto_confeitaria_id': 8,
                    'store_id': 24,
                    'data': date(2026, 9, 28),
                    'quantidade': 2,
                },
            ],
            'history': [
                {
                    'id': 90,
                    'data': date(2026, 9, 8),
                    'loja_registada': 'Loja Nova',
                    'store_id': 24,
                    'loja_atual': 'Loja Nova',
                    'loja_ativa': True,
                    'produto_registado': 'Cookie antigo',
                    'produto_confeitaria_id': 8,
                    'produto_atual': 'Cookie Arquivado',
                    'produto_ativo': False,
                    'quantidade': 2,
                    'submitted_by': 'operador-confeitaria',
                    'submitted_at': datetime(2026, 9, 8, 18, 42, 9),
                },
                {
                    'id': 89,
                    'data': date(2026, 8, 9),
                    'loja_registada': 'Matosinhos',
                    'store_id': None,
                    'loja_atual': None,
                    'loja_ativa': None,
                    'produto_registado': 'Cookie Ativo',
                    'produto_confeitaria_id': None,
                    'produto_atual': None,
                    'produto_ativo': None,
                    'quantidade': 9,
                    'submitted_by': None,
                    'submitted_at': None,
                },
                {
                    'id': 88,
                    'data': date(2026, 9, 3),
                    'loja_registada': 'Matosinhos',
                    'store_id': 11,
                    'loja_atual': 'Matosinhos',
                    'loja_ativa': True,
                    'origem': 'importacao',
                    'produto_registado': 'Cookie Ativo',
                    'produto_confeitaria_id': 7,
                    'produto_atual': 'Cookie Ativo',
                    'produto_ativo': True,
                    'quantidade': 1,
                },
            ],
        }
        legacy_stock = [{'produto': 'Cookie Ativo', 'quantidade': 12}]

        with (
            patch(
                'flask_app.routes.confeitaria._tabs_with_urls',
                return_value=[],
            ),
            patch(
                'flask_app.routes.confeitaria.get_vendas_module_stores',
                return_value=stores,
            ),
            patch(
                'flask_app.routes.confeitaria.get_confeitaria_stock_count_overview',
                return_value=count_data,
            ) as get_counts,
            patch(
                'flask_app.routes.confeitaria.get_confeitaria_stock_options',
                return_value=products,
            ),
            patch(
                'flask_app.routes.confeitaria.get_stock_producao_area_all',
                return_value=legacy_stock,
            ),
            patch(
                'flask_app.routes.confeitaria.register_confeitaria_stock_movement'
            ) as register_movement,
        ):
            response = self.client.get('/confeitaria/stock-balcao')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Saldo de produção auditado', page)
        self.assertIn('Loja Nova', page)
        self.assertIn('Matosinhos', page)
        self.assertNotIn('Bolhão', page)
        self.assertIn('22/09/2026', page)
        self.assertIn('28/09/2026', page)
        self.assertIn('as contagens não são somadas', page)
        self.assertIn('Produto inativo', page)
        self.assertIn('Registo antigo sem ID de produto', page)
        self.assertIn('Origem: importacao', page)
        self.assertIn('Registado por', page)
        self.assertIn('Registado em', page)
        self.assertIn('operador-confeitaria', page)
        self.assertIn('08/09/2026 18:42:09', page)
        self.assertGreaterEqual(page.count('Não disponível'), 2)
        self.assertIn('Saldo de produção antigo (legado)', page)
        self.assertIn('d-none d-lg-block', page)
        self.assertIn('d-lg-none', page)
        get_counts.assert_called_once_with([11, 24])
        register_movement.assert_not_called()

    def test_stock_overview_still_requires_confeitaria_permission(self):
        self._set_user(acesso_confeitaria=False)
        with patch(
            'flask_app.routes.confeitaria.get_confeitaria_stock_count_overview'
        ) as get_counts:
            response = self.client.get('/confeitaria/stock-balcao')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/test-home'))
        get_counts.assert_not_called()


if __name__ == '__main__':
    unittest.main()