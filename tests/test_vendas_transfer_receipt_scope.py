import unittest
from datetime import date
from unittest.mock import patch

from flask import Flask
from werkzeug.datastructures import MultiDict

from flask_app.routes.vendas import vendas_bp


class VendasTransferReceiptStoreScopeTests(unittest.TestCase):
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

    def _patch_store_context(self):
        return (
            patch(
                'flask_app.routes.vendas.get_store_by_id',
                side_effect=self.stores.get,
            ),
            patch(
                'flask_app.routes.vendas.get_vendas_module_stores',
                return_value=list(self.stores.values()),
            ),
            patch(
                'flask_app.routes.vendas._build_tabs',
                return_value=[],
            ),
        )

    def test_get_shows_confeitaria_pastelaria_and_mixed_pending_batches(self):
        self._set_user((self.BOLHAO_ID,))
        orders = [
            {
                'id': 101, 'batch_id': 'confeitaria-batch',
                'data': date(2026, 9, 14),
                'data_prevista': date(2026, 9, 16),
                'area_origem': 'Confeitaria', 'produto': 'Bolo de noz',
                'sabor': None, 'quantidade': 3.0, 'unidade': 'und',
                'loja_destino': 'Bolhão', 'status': 'confirmada',
                'rececao_estado': 'por_verificar', 'destino_tipo': 'loja',
                'criado_por': 'confeitaria',
            },
            {
                'id': 102, 'batch_id': 'confeitaria-batch',
                'data': date(2026, 9, 14),
                'data_prevista': date(2026, 9, 16),
                'area_origem': 'Confeitaria', 'produto': 'Tarte de maçã',
                'sabor': None, 'quantidade': 2.0, 'unidade': 'und',
                'loja_destino': 'Bolhão', 'status': 'confirmada',
                'rececao_estado': 'por_verificar', 'destino_tipo': 'loja',
                'criado_por': 'confeitaria',
            },
            {
                'id': 103, 'batch_id': 'pastelaria-batch',
                'data': date(2026, 9, 14),
                'data_prevista': date(2026, 9, 16),
                'area_origem': 'Pastelaria', 'produto': 'Pastel de nata',
                'sabor': None, 'quantidade': 24.0, 'unidade': 'und',
                'loja_destino': 'Bolhão', 'status': 'confirmada',
                'rececao_estado': 'por_verificar', 'destino_tipo': 'loja',
                'criado_por': 'pastelaria',
            },
            {
                'id': 104, 'batch_id': 'mixed-batch',
                'data': date(2026, 9, 14),
                'data_prevista': date(2026, 9, 16),
                'area_origem': 'Confeitaria', 'produto': 'Queque',
                'sabor': None, 'quantidade': 5.0, 'unidade': 'und',
                'loja_destino': 'Bolhão', 'status': 'confirmada',
                'rececao_estado': 'por_verificar', 'destino_tipo': 'loja',
                'criado_por': 'equipa',
            },
            {
                'id': 105, 'batch_id': 'mixed-batch',
                'data': date(2026, 9, 14),
                'data_prevista': date(2026, 9, 16),
                'area_origem': 'Pastelaria', 'produto': 'Croissant',
                'sabor': None, 'quantidade': 12.0, 'unidade': 'und',
                'loja_destino': 'Bolhão', 'status': 'confirmada',
                'rececao_estado': 'por_verificar', 'destino_tipo': 'loja',
                'criado_por': 'equipa',
            },
        ]
        patches = self._patch_store_context()
        with patches[0], patches[1], patches[2], patch(
            'flask_app.routes.vendas.get_ordens_transferencia',
            return_value=orders,
        ) as get_orders:
            response = self.client.get(
                f'/vendas/transferencias?loja_id={self.BOLHAO_ID}'
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('16/09/2026', page)
        self.assertIn('Confeitaria', page)
        self.assertIn('Pastelaria', page)
        self.assertIn('Misto', page)
        self.assertIn('Bolo de noz', page)
        self.assertIn('Tarte de maçã', page)
        self.assertIn('Pastel de nata', page)
        self.assertIn('Croissant', page)
        self.assertIn('3 und', page)
        self.assertIn('24 und', page)
        self.assertIn('name="ordem_ids" value="104,105"', page)
        get_orders.assert_called_once_with(loja_destino='Bolhão')

    def test_single_receipt_cannot_be_confirmed_from_another_store_context(self):
        self._set_user((self.MATOSINHOS_ID, self.BOLHAO_ID))
        patches = self._patch_store_context()
        with patches[0], patches[1], patches[2], patch(
            'flask_app.routes.vendas.get_ordem_transferencia_by_id',
            return_value={
                'id': 301,
                'loja_destino': 'Matosinhos',
                'destino_tipo': 'loja',
            },
        ), patch(
            'flask_app.routes.vendas.confirmar_ordens_transferencia_batch'
        ) as confirm:
            response = self.client.post(
                f'/vendas/transferencias?loja_id={self.BOLHAO_ID}',
                data={'action': 'confirmar', 'ordem_id': '301'},
            )

        self.assertEqual(response.status_code, 302)
        confirm.assert_not_called()

    def test_unresolvable_store_cannot_fall_back_to_bolhao_receipts(self):
        self._set_user((999,))
        patches = self._patch_store_context()
        with patches[0], patches[1], patches[2], patch(
            'flask_app.routes.vendas.get_ordens_transferencia'
        ) as get_orders:
            response = self.client.get('/vendas/transferencias')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/home'))
        get_orders.assert_not_called()

    def test_batch_action_uses_selected_store_and_rejects_malformed_ids(self):
        self._set_user((self.MATOSINHOS_ID, self.BOLHAO_ID))
        patches = self._patch_store_context()
        with patches[0], patches[1], patches[2], patch(
            'flask_app.routes.vendas.confirmar_ordens_transferencia_batch',
            return_value={'updated_count': 2, 'replayed': False},
        ) as confirm:
            response = self.client.post(
                f'/vendas/transferencias?loja_id={self.BOLHAO_ID}',
                data={
                    'action': 'confirmar_batch',
                    'ordem_ids': '401,402',
                },
            )

        self.assertEqual(response.status_code, 302)
        confirm.assert_called_once_with(
            [401, 402], 'Bolhão', 'test-user'
        )

        patches = self._patch_store_context()
        with patches[0], patches[1], patches[2], patch(
            'flask_app.routes.vendas.confirmar_ordens_transferencia_batch'
        ) as confirm:
            response = self.client.post(
                f'/vendas/transferencias?loja_id={self.BOLHAO_ID}',
                data={
                    'action': 'confirmar_batch',
                    'ordem_ids': '401,invalid,402',
                },
            )

        self.assertEqual(response.status_code, 302)
        confirm.assert_not_called()

    def test_problem_batch_action_preserves_reason_and_store_scope(self):
        self._set_user((self.MATOSINHOS_ID, self.BOLHAO_ID))
        patches = self._patch_store_context()
        with patches[0], patches[1], patches[2], patch(
            'flask_app.routes.vendas.reportar_problema_ordens_transferencia_batch',
            return_value={'updated_count': 2, 'replayed': False},
        ) as report:
            response = self.client.post(
                f'/vendas/transferencias?loja_id={self.BOLHAO_ID}',
                data={
                    'action': 'reportar_problema_batch',
                    'ordem_ids': '601,602',
                    'motivo_problema': 'Quantidade incorreta',
                },
            )

        self.assertEqual(response.status_code, 302)
        report.assert_called_once_with(
            [601, 602],
            'Bolhão',
            'test-user',
            'Quantidade incorreta',
        )

    def test_repeated_order_id_fields_are_rejected_as_a_whole(self):
        self._set_user((self.BOLHAO_ID,))
        patches = self._patch_store_context()
        with patches[0], patches[1], patches[2], patch(
            'flask_app.routes.vendas.confirmar_ordens_transferencia_batch'
        ) as confirm:
            response = self.client.post(
                f'/vendas/transferencias?loja_id={self.BOLHAO_ID}',
                data=MultiDict([
                    ('action', 'confirmar_batch'),
                    ('ordem_ids', '501'),
                    ('ordem_ids', '502'),
                ]),
            )

        self.assertEqual(response.status_code, 302)
        confirm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
