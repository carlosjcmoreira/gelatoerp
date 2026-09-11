import unittest
from datetime import date
from unittest.mock import patch

from flask import Flask, session

from db.pastelaria import build_bolo_product_label
from flask_app.routes import pastelaria as pastelaria_routes


class PastelariaWeeklyPlanTests(unittest.TestCase):
    def test_produzir_is_removed_only_from_pastelaria_navigation(self):
        tab_ids = [tab['id'] for tab in pastelaria_routes.TABS]
        self.assertNotIn('produzir', tab_ids)

        from flask_app.routes.gestor import _TILE_MASTER
        self.assertNotIn(
            'produzir',
            [tile['id'] for tile in _TILE_MASTER['pastelaria']],
        )
        self.assertIn(
            'produzir',
            [tile['id'] for tile in _TILE_MASTER['confeitaria']],
        )

    def test_week_selection_is_normalised_to_monday(self):
        self.assertEqual(
            pastelaria_routes._week_start('2026-09-03'),
            date(2026, 8, 31),
        )
        self.assertEqual(
            pastelaria_routes._week_days(date(2026, 8, 31))[-1],
            date(2026, 9, 6),
        )

    def test_valid_cake_supports_three_flavours_and_optional_cover(self):
        config, error = pastelaria_routes.validate_bolo_configuration(
            '22 cm',
            ['Chocolate', 'Baunilha', 'Pistácio'],
            'Ganache',
            ['11 cm', '22 cm', '26 cm'],
            ['Chocolate', 'Baunilha', 'Pistácio'],
            ['Ganache'],
        )

        self.assertIsNone(error)
        self.assertEqual(config['tamanho'], '22 cm')
        self.assertEqual(
            config['sabores'],
            ['Chocolate', 'Baunilha', 'Pistácio'],
        )
        self.assertEqual(config['cobertura'], 'Ganache')

    def test_cake_rejects_repeated_flavours_and_invalid_size_or_cover(self):
        cases = (
            ('40 cm', ['Chocolate'], '', 'tamanho'),
            ('22 cm', ['Chocolate', 'chocolate'], '', 'repetir'),
            ('22 cm', ['Chocolate'], 'Cobertura inexistente', 'cobertura'),
            ('22 cm', [], '', 'entre 1 e 3'),
        )
        for size, flavours, cover, expected in cases:
            with self.subTest(size=size, flavours=flavours, cover=cover):
                config, error = pastelaria_routes.validate_bolo_configuration(
                    size,
                    flavours,
                    cover,
                    ['11 cm', '22 cm', '26 cm'],
                    ['Chocolate', 'Baunilha'],
                    ['Ganache'],
                )
                self.assertIsNone(config)
                self.assertIn(expected, error.lower())

    def test_cake_label_keeps_configurations_distinct(self):
        one = build_bolo_product_label(
            '22 cm', ['Chocolate', 'Baunilha'], 'Ganache'
        )
        two = build_bolo_product_label(
            '26 cm', ['Chocolate', 'Baunilha'], 'Ganache'
        )
        self.assertNotEqual(one, two)
        self.assertIn('22 cm', one)
        self.assertIn('Chocolate + Baunilha', one)
        self.assertIn('Ganache', one)

    def test_manual_weekly_plan_is_replaced_by_priority_plan(self):
        with open(
            'flask_app/templates/pastelaria/planear.html',
            encoding='utf-8',
        ) as template:
            html = template.read()
        self.assertIn('Planear Produção por Prioridade', html)
        self.assertIn('Planos históricos', html)
        self.assertNotIn('Plano semanal de Pastelaria', html)
        self.assertNotIn('Adicionar Bolo configurável', html)
        self.assertNotIn('Guardar plano semanal', html)


class PastelariaTransferTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.app.register_blueprint(
            pastelaria_routes.pastelaria_bp,
            url_prefix='/pastelaria',
        )

    def test_transfer_uses_requested_quantity_without_digital_stock(self):
        data = {
            'loja_destino': 'Bolhão',
            'data_prevista': '2026-09-01',
            'produto_0': 'Bolo — 22 cm — Chocolate',
            'qty_0': '8',
        }
        with self.app.test_request_context(
            '/pastelaria/transferir',
            method='POST',
            data=data,
        ):
            session['user'] = {
                'username': 'operador',
                'acesso_pastelaria': True,
            }
            with patch.object(
                pastelaria_routes,
                'get_active_venda_stores',
                return_value=[{'name': 'Bolhão'}],
            ), patch.object(
                pastelaria_routes,
                'get_or_create_pending_batch',
                return_value='batch-1',
            ), patch.object(
                pastelaria_routes,
                'criar_ordem_transferencia',
            ) as create_order, patch.object(
                pastelaria_routes,
                'get_produtos_pastelaria',
                return_value=['Bolo Individual'],
            ), patch.object(
                pastelaria_routes,
                'get_plano_do_dia_area',
                return_value=[{'produto': 'Bolo — 22 cm — Chocolate'}],
            ), patch.object(
                pastelaria_routes,
                'get_stock_producao_area_all',
                return_value=[],
            ), patch.object(
                pastelaria_routes,
                'get_ultimo_stock_balcao',
                return_value=[],
            ), patch.object(
                pastelaria_routes,
                'get_stock_producao_area',
            ) as get_stock, patch.object(
                pastelaria_routes,
                'reduzir_stock_producao_area',
            ) as reduce_stock:
                response = pastelaria_routes.transferir()

        self.assertEqual(response.status_code, 302)
        get_stock.assert_not_called()
        reduce_stock.assert_not_called()
        create_order.assert_called_once()
        args = create_order.call_args.args
        self.assertEqual(args[2], 'Bolo — 22 cm — Chocolate')
        self.assertEqual(args[3], 8)
        self.assertEqual(args[5], 'Bolhão')

    def test_transfer_rejects_forged_cake_configuration(self):
        data = {
            'loja_destino': 'Bolhão',
            'data_prevista': '2026-09-01',
            'produto_0': 'Bolo — 99 cm — Inventado',
            'qty_0': '1',
        }
        with self.app.test_request_context(
            '/pastelaria/transferir',
            method='POST',
            data=data,
        ):
            session['user'] = {
                'username': 'operador',
                'acesso_pastelaria': True,
            }
            with patch.object(
                pastelaria_routes,
                'get_active_venda_stores',
                return_value=[{'name': 'Bolhão'}],
            ), patch.object(
                pastelaria_routes,
                'get_produtos_pastelaria',
                return_value=['Bolo Individual'],
            ), patch.object(
                pastelaria_routes,
                'get_plano_do_dia_area',
                return_value=[],
            ), patch.object(
                pastelaria_routes,
                'get_stock_producao_area_all',
                return_value=[],
            ), patch.object(
                pastelaria_routes,
                'get_ultimo_stock_balcao',
                return_value=[],
            ), patch.object(
                pastelaria_routes,
                'get_or_create_pending_batch',
            ) as get_batch, patch.object(
                pastelaria_routes,
                'criar_ordem_transferencia',
            ) as create_order:
                response = pastelaria_routes.transferir()

        self.assertEqual(response.status_code, 302)
        get_batch.assert_not_called()
        create_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()