import unittest
import os
from unittest.mock import patch

from flask import Flask, session

from flask_app.routes import confeitaria as confeitaria_routes


class ConfeitariaTransferRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(
            __name__,
            template_folder=os.path.join(
                os.path.dirname(__file__),
                '..',
                'flask_app',
                'templates',
            ),
        )
        self.app.secret_key = 'test'
        self.app.register_blueprint(
            confeitaria_routes.confeitaria_bp,
            url_prefix='/confeitaria',
        )

    def _post(self, data, *, stores=None):
        with self.app.test_request_context(
            '/confeitaria/transferir',
            method='POST',
            data=data,
        ):
            session['user'] = {
                'username': 'operador',
                'acesso_confeitaria': True,
            }
            with patch.object(
                confeitaria_routes,
                'get_active_venda_stores',
                return_value=stores or [{'name': 'Bolhão'}],
            ), patch.object(
                confeitaria_routes,
                'criar_ordens_transferencia_confeitaria',
                return_value={'order_ids': [101, 102], 'replayed': False},
            ) as create_batch, patch.object(
                confeitaria_routes,
                'reconciliar_confeitaria_stock_corte',
                return_value=[],
            ) as reconcile:
                response = confeitaria_routes.transferir()
                flashes = list(session.get('_flashes', []))
        return response, create_batch, reconcile, flashes

    def test_positive_rows_are_sent_as_one_id_based_batch(self):
        response, create_batch, _, _ = self._post({
            'action': 'criar_transferencias',
            'request_key': '7c917604-284c-4eb3-8df9-6938569f0c93',
            'loja_destino': 'Bolhão',
            'data_prevista': '2026-10-03',
            'qty_12': '4',
            'qty_25': '0',
            'qty_31': '',
        })

        self.assertEqual(response.status_code, 302)
        create_batch.assert_called_once()
        self.assertEqual(
            create_batch.call_args.kwargs['lines'],
            [{
                'produto_confeitaria_id': 12,
                'quantidade': 4,
            }],
        )
        self.assertEqual(
            create_batch.call_args.kwargs['loja_destino'],
            'Bolhão',
        )
        self.assertEqual(
            create_batch.call_args.kwargs['data_prevista'].isoformat(),
            '2026-10-03',
        )

    def test_blank_and_zero_rows_create_no_batch_and_show_clear_message(self):
        response, create_batch, _, flashes = self._post({
            'action': 'criar_transferencias',
            'request_key': '7c917604-284c-4eb3-8df9-6938569f0c93',
            'loja_destino': 'Bolhão',
            'data_prevista': '2026-10-03',
            'qty_12': '',
            'qty_25': '0',
        })

        self.assertEqual(response.status_code, 302)
        create_batch.assert_not_called()
        self.assertTrue(any(
            category == 'info' and 'Nenhuma transferência criada' in message
            for category, message in flashes
        ))

    def test_negative_decimal_duplicate_and_malformed_lines_are_rejected(self):
        base = {
            'action': 'criar_transferencias',
            'request_key': '7c917604-284c-4eb3-8df9-6938569f0c93',
            'loja_destino': 'Bolhão',
            'data_prevista': '2026-10-03',
        }
        cases = [
            {'qty_12': '-1'},
            {'qty_12': '1.5'},
            {'qty_12': '1', 'qty_012': '1'},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                response, create_batch, _, flashes = self._post({
                    **base,
                    **fields,
                })
                self.assertEqual(response.status_code, 302)
                create_batch.assert_not_called()
                self.assertTrue(any(
                    category == 'error'
                    and 'número inteiro não negativo' in message
                    for category, message in flashes
                ))

    def test_invalid_destination_and_date_never_call_batch_service(self):
        cases = [
            {
                'loja_destino': 'Loja inativa',
                'data_prevista': '2026-10-03',
            },
            {
                'loja_destino': 'Bolhão',
                'data_prevista': '2026-02-30',
            },
        ]
        for values in cases:
            with self.subTest(values=values):
                response, create_batch, _, flashes = self._post({
                    'action': 'criar_transferencias',
                    'request_key': '7c917604-284c-4eb3-8df9-6938569f0c93',
                    'qty_12': '1',
                    **values,
                })
                self.assertEqual(response.status_code, 302)
                create_batch.assert_not_called()
                self.assertTrue(any(
                    category == 'error' for category, _message in flashes
                ))

    def test_cutover_requires_explicit_confirmation_before_service_call(self):
        data = {
            'action': 'reconciliar_saldos',
            'saldo_esperado_12': '8',
            'saldo_confirmado_12': '5',
        }
        response, _, reconcile, flashes = self._post(data)

        self.assertEqual(response.status_code, 302)
        reconcile.assert_not_called()
        self.assertTrue(any(
            category == 'error' and 'Confirme' in message
            for category, message in flashes
        ))

    def test_cutover_passes_fresh_manual_counts_and_snapshots(self):
        data = {
            'action': 'reconciliar_saldos',
            'confirmo_transferencias_legadas': '1',
            'saldo_esperado_12': '8',
            'saldo_confirmado_12': '5',
            'saldo_esperado_13': '0',
            'saldo_confirmado_13': '',
        }
        response, _, reconcile, _ = self._post(data)

        self.assertEqual(response.status_code, 302)
        reconcile.assert_called_once()
        self.assertEqual(
            reconcile.call_args.kwargs['lines'],
            [{
                'produto_confeitaria_id': 12,
                'saldo_confirmado': 5,
                'saldo_esperado': 8,
            }],
        )
        self.assertTrue(
            reconcile.call_args.kwargs['confirmacao_explicita']
        )

    def test_transfer_page_uses_responsive_audited_balance_tables(self):
        stock_options = [
            {
                'produto_confeitaria_id': 12,
                'produto': 'Bolo de maçã',
                'ativo': True,
                'saldo': 7,
                'saldo_inicial_confirmado': True,
                'transferencias_reconciliadas': True,
                'transferivel': True,
            },
            {
                'produto_confeitaria_id': 13,
                'produto': 'Tarte de amêndoa',
                'ativo': True,
                'saldo': 4,
                'saldo_inicial_confirmado': True,
                'transferencias_reconciliadas': False,
                'transferivel': False,
            },
            {
                'produto_confeitaria_id': 14,
                'produto': 'Broa doce',
                'ativo': True,
                'saldo': 0,
                'saldo_inicial_confirmado': False,
                'transferencias_reconciliadas': False,
                'transferivel': False,
            },
        ]
        with self.app.test_request_context('/confeitaria/transferir'):
            session['user'] = {
                'username': 'operador',
                'acesso_confeitaria': True,
            }
            with patch.object(
                confeitaria_routes,
                '_tabs_with_urls',
                return_value=[],
            ), patch.object(
                confeitaria_routes,
                'get_active_venda_stores',
                return_value=[{'name': 'Bolhão'}],
            ), patch.object(
                confeitaria_routes,
                'get_confeitaria_stock_options',
                return_value=stock_options,
            ):
                page = confeitaria_routes.transferir()

        self.assertIn('Saldo auditado disponível', page)
        self.assertIn('name="qty_12"', page)
        self.assertIn('name="saldo_confirmado_13"', page)
        self.assertIn('name="request_key"', page)
        self.assertIn('table-responsive', page)
        self.assertIn('transferências legadas posteriores', page)
        self.assertNotIn('Stock disponível (referência)', page)


if __name__ == '__main__':
    unittest.main()