import unittest
from unittest.mock import patch

import pandas as pd
from flask import Flask

from db import pastelaria
from db.confeitaria_stock import ConfeitariaStockError
from flask_app.routes.confeitaria import confeitaria_bp


class _Cursor:
    def __init__(self, rows=None, rowcount=1):
        self.rows = rows or []
        self.rowcount = rowcount
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class ConfeitariaProductCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../flask_app/templates')
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

    def _set_user(self, **permissions):
        with self.client.session_transaction() as session:
            session['user'] = {
                'username': 'gestor',
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

    def test_product_page_distinguishes_active_and_inactive_products(self):
        products = [
            {'id': 7, 'nome': 'Cookie Ativo', 'ativo': True},
            {'id': 8, 'nome': 'Cookie Arquivado', 'ativo': False},
        ]
        with (
            patch(
                'flask_app.routes.confeitaria._tabs_with_urls',
                return_value=[],
            ),
            patch(
                'flask_app.routes.confeitaria.get_all_produtos_confeitaria',
                return_value=products,
            ),
        ):
            response = self.client.get('/confeitaria/produtos')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Cookie Ativo', page)
        self.assertIn('Cookie Arquivado', page)
        self.assertIn('Ativo', page)
        self.assertIn('Inativo', page)
        self.assertIn('desativar_produto_conf', page)
        self.assertIn('reativar_produto_conf', page)

    def test_deactivate_and_reactivate_keep_the_same_product_id(self):
        for action, expected_active, expected_message in (
            ('desativar_produto_conf', False, 'desativado'),
            ('reativar_produto_conf', True, 'reativado'),
        ):
            with patch(
                'flask_app.routes.confeitaria.set_produto_confeitaria_ativo',
                return_value=True,
            ) as set_active:
                response = self.client.post('/confeitaria/produtos', data={
                    'action': action,
                    'produto_conf_id': '42',
                })

            self.assertEqual(response.status_code, 302)
            set_active.assert_called_once_with(42, expected_active)
            self.assertTrue(any(
                expected_message in message
                for message in self._take_flashes()
            ))

    def test_legacy_delete_action_is_a_soft_deactivation(self):
        with patch(
            'flask_app.routes.confeitaria.set_produto_confeitaria_ativo',
            return_value=True,
        ) as set_active:
            response = self.client.post('/confeitaria/produtos', data={
                'action': 'delete_produto_conf',
                'produto_conf_id': '42',
            })

        self.assertEqual(response.status_code, 302)
        set_active.assert_called_once_with(42, False)
        self.assertIn('desativado', ' '.join(self._take_flashes()))

    def test_duplicate_name_stays_a_warning_and_does_not_reactivate_any_row(self):
        with (
            patch(
                'flask_app.routes.confeitaria.add_produto_confeitaria',
                return_value=False,
            ) as add_product,
            patch(
                'flask_app.routes.confeitaria.set_produto_confeitaria_ativo',
            ) as set_active,
        ):
            response = self.client.post('/confeitaria/produtos', data={
                'action': 'add_produto_conf',
                'novo_prod_conf': '  Cookie Arquivado  ',
            })

        self.assertEqual(response.status_code, 302)
        add_product.assert_called_once_with('Cookie Arquivado')
        set_active.assert_not_called()
        self.assertIn('Produto já existe.', ' '.join(self._take_flashes()))

    def test_invalid_or_missing_product_ids_do_not_change_catalogue(self):
        for raw_id in ('abc', '0', '-2'):
            with patch(
                'flask_app.routes.confeitaria.set_produto_confeitaria_ativo'
            ) as set_active:
                response = self.client.post('/confeitaria/produtos', data={
                    'action': 'desativar_produto_conf',
                    'produto_conf_id': raw_id,
                })

            self.assertEqual(response.status_code, 302)
            set_active.assert_not_called()
            self.assertIn('inválido', ' '.join(self._take_flashes()))

        with patch(
            'flask_app.routes.confeitaria.set_produto_confeitaria_ativo',
            return_value=False,
        ) as set_active:
            response = self.client.post('/confeitaria/produtos', data={
                'action': 'reativar_produto_conf',
                'produto_conf_id': '99',
            })

        self.assertEqual(response.status_code, 302)
        set_active.assert_called_once_with(99, True)
        self.assertIn('não encontrado', ' '.join(self._take_flashes()))

    def test_catalogue_page_remains_permission_protected(self):
        self._set_user(acesso_confeitaria=False)
        with patch(
            'flask_app.routes.confeitaria.set_produto_confeitaria_ativo'
        ) as set_active:
            response = self.client.post('/confeitaria/produtos', data={
                'action': 'desativar_produto_conf',
                'produto_conf_id': '42',
            })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/test-home'))
        set_active.assert_not_called()

    def test_legacy_stock_page_keeps_history_but_does_not_write_or_delete(self):
        with (
            patch(
                'flask_app.routes.confeitaria._tabs_with_urls',
                return_value=[],
            ),
            patch(
                'db.pastelaria.add_contagem_stock',
            ) as add_count,
            patch(
                'db.pastelaria.delete_contagem_stock',
            ) as delete_count,
            patch(
                'flask_app.routes.confeitaria.get_ultimo_stock_balcao',
                return_value=[],
            ),
            patch(
                'flask_app.routes.confeitaria.get_contagem_stock_df',
                return_value=pd.DataFrame(),
            ),
        ):
            responses = [
                self.client.post('/confeitaria/stock-balcao', data={
                    'action': 'registar',
                    'data_contagem': '2026-09-29',
                    'loja': 'Matosinhos',
                    'produto': 'Cookie Ativo',
                    'quantidade': '4',
                }),
                self.client.post('/confeitaria/stock-balcao', data={
                    'action': 'eliminar',
                    'id_delete': '1',
                }),
            ]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        for response in responses:
            self.assertIn(
                'novas contagens físicas são registadas em Vendas',
                response.get_data(as_text=True),
            )
        add_count.assert_not_called()
        delete_count.assert_not_called()
        self.assertNotIn(
            'id_delete',
            responses[0].get_data(as_text=True),
        )

    def test_inactive_product_rejection_comes_from_audited_transfer_service(self):
        with (
            patch(
                'flask_app.routes.confeitaria.get_active_venda_stores',
                return_value=[{'name': 'Matosinhos'}],
            ),
            patch(
                'flask_app.routes.confeitaria.criar_ordens_transferencia_confeitaria',
                side_effect=ConfeitariaStockError(
                    'O produto Cookie Arquivado está inativo.'
                ),
            ) as create_batch,
        ):
            response = self.client.post('/confeitaria/transferir', data={
                'action': 'criar_transferencias',
                'request_key': 'f8b970aa-9500-4111-943d-fc1e81ec2fcf',
                'loja_destino': 'Matosinhos',
                'data_prevista': '2026-10-03',
                'qty_999': '1',
            })

        self.assertEqual(response.status_code, 302)
        create_batch.assert_called_once()
        self.assertEqual(
            create_batch.call_args.kwargs['lines'],
            [{
                'produto_confeitaria_id': 999,
                'quantidade': 1,
            }],
        )
        self.assertIn('inativo', ' '.join(self._take_flashes()))

    def test_inactive_product_is_not_offered_for_new_breakage(self):
        with (
            patch(
                'flask_app.routes.confeitaria.get_produtos_confeitaria',
                return_value=['Cookie Ativo'],
            ),
            patch(
                'flask_app.routes.confeitaria.add_quebra_area',
            ) as add_breakage,
        ):
            response = self.client.post('/confeitaria/registar-quebra', data={
                'data': '2026-09-29',
                'quantidade': '1',
                'produto': 'Cookie Arquivado',
            })

        self.assertEqual(response.status_code, 302)
        add_breakage.assert_not_called()
        self.assertIn('ativo', ' '.join(self._take_flashes()))


class ConfeitariaProductCatalogueDatabaseTests(unittest.TestCase):
    def test_state_change_updates_the_existing_catalogue_row_without_deleting(self):
        cursor = _Cursor(rowcount=1)
        connection = _Connection(cursor)
        with patch(
            'db.pastelaria.db_connection',
            return_value=connection,
        ):
            updated = pastelaria.set_produto_confeitaria_ativo(42, False)

        self.assertTrue(updated)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(len(cursor.queries), 1)
        query, params = cursor.queries[0]
        self.assertIn('UPDATE produtos_confeitaria SET ativo', query)
        self.assertNotIn('DELETE', query.upper())
        self.assertEqual(params, (False, 42))

    def test_legacy_delete_helper_deactivates_instead_of_deleting(self):
        with patch(
            'db.pastelaria.set_produto_confeitaria_ativo',
            return_value=True,
        ) as set_active:
            result = pastelaria.delete_produto_confeitaria(42)

        self.assertTrue(result)
        set_active.assert_called_once_with(42, False)

    def test_state_change_rejects_invalid_ids_and_types(self):
        with patch('db.pastelaria.db_connection') as connect:
            self.assertFalse(pastelaria.set_produto_confeitaria_ativo(0, False))
            self.assertFalse(pastelaria.set_produto_confeitaria_ativo(-1, True))
            self.assertFalse(pastelaria.set_produto_confeitaria_ativo(True, False))
            self.assertFalse(pastelaria.set_produto_confeitaria_ativo(42, 0))

        connect.assert_not_called()

    def test_catalogue_read_keeps_inactive_rows_for_history_and_manager(self):
        cursor = _Cursor(rows=[
            (7, 'Cookie Ativo', True),
            (8, 'Cookie Arquivado', False),
        ])
        connection = _Connection(cursor)
        with patch(
            'db.pastelaria.db_connection',
            return_value=connection,
        ):
            products = pastelaria.get_all_produtos_confeitaria()

        self.assertEqual(
            products,
            [
                {'id': 7, 'nome': 'Cookie Ativo', 'ativo': True},
                {'id': 8, 'nome': 'Cookie Arquivado', 'ativo': False},
            ],
        )
        self.assertIn('ORDER BY (ativo IS TRUE) DESC, nome', cursor.queries[0][0])


if __name__ == '__main__':
    unittest.main()