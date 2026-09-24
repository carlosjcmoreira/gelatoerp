import unittest
from datetime import date
from unittest.mock import patch

from flask import Flask, session, url_for

from db.pastelaria import build_bolo_product_label
from flask_app.routes import pastelaria as pastelaria_routes


class PastelariaMenuTests(unittest.TestCase):
    def test_home_menu_passes_descriptions_and_keeps_tile_preferences(self):
        app = Flask(__name__)
        app.secret_key = 'test'
        app.register_blueprint(
            pastelaria_routes.pastelaria_bp,
            url_prefix='/pastelaria',
        )

        hidden_tile = 'transferir'
        custom_label = 'Contagens das lojas'
        custom_icon = '🧁'
        with app.test_request_context('/pastelaria/'):
            session['user'] = {'acesso_pastelaria': True}
            with patch(
                'db.tiles.get_tile_visibility',
                return_value={hidden_tile: False},
            ), patch(
                'db.tiles.get_tile_labels',
                return_value={'stock_balcao': custom_label},
            ), patch(
                'db.tiles.get_tile_icons',
                return_value={'stock_balcao': custom_icon},
            ), patch(
                'db.tiles.get_module_labels',
                return_value={'pastelaria': 'Pastelaria personalizada'},
            ), patch.object(
                pastelaria_routes,
                'render_template',
                return_value='menu',
            ) as render_menu:
                response = pastelaria_routes.index()

            self.assertEqual(response, 'menu')
            items = render_menu.call_args.kwargs['items']
            menu_title = render_menu.call_args.kwargs['menu_title']
            expected_descriptions = {
                url_for(tab['endpoint']): tab['description']
                for tab in pastelaria_routes.TABS
            }
            hidden_url = url_for('pastelaria.transferir')
            stock_url = url_for('pastelaria.stock_balcao')

        self.assertEqual(len(expected_descriptions), 8)
        self.assertTrue(all(expected_descriptions.values()))
        self.assertLessEqual(
            max(len(description) for description in expected_descriptions.values()),
            80,
        )
        self.assertEqual(len(items), 7)
        self.assertNotIn(hidden_url, {item['url'] for item in items})
        self.assertEqual(menu_title, '🍰 Pastelaria personalizada')
        stock_item = next(
            item for item in items
            if item['url'] == stock_url
        )
        self.assertEqual(stock_item['label'], custom_label)
        self.assertEqual(stock_item['icon'], custom_icon)
        for item in items:
            self.assertEqual(
                item['description'],
                expected_descriptions[item['url']],
            )


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

    def test_priority_print_template_is_a4_and_hides_editing_controls(self):
        with open(
            'flask_app/templates/pastelaria/plano_prioridade.html',
            encoding='utf-8',
        ) as template:
            html = template.read()
        self.assertIn('@page { size: A4 landscape;', html)
        self.assertIn('window.print()', html)
        self.assertIn('class="no-print"', html)
        with open(
            'flask_app/templates/pastelaria/planear.html',
            encoding='utf-8',
        ) as template:
            planning_html = template.read()
        self.assertNotIn('id="weekly-print"', planning_html)
        self.assertNotIn('Notas / controlo manual', planning_html)


class PastelariaTransferTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.app.register_blueprint(
            pastelaria_routes.pastelaria_bp,
            url_prefix='/pastelaria',
        )

    def test_transfer_delegates_the_whole_request_to_atomic_stock_batch(self):
        data = {
            'loja_destino': 'Bolhão',
            'data_prevista': '2026-09-01',
            'request_key': 'request-1',
            'produto_0': 'cake:Bolo — 22 cm — Chocolate',
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
                'get_produtos_pastelaria',
                return_value=['Bolo Individual'],
            ), patch.object(
                pastelaria_routes,
                'get_plano_do_dia_area',
                return_value=[{'produto': 'Bolo — 22 cm — Chocolate'}],
            ), patch.object(
                pastelaria_routes,
                'get_pastelaria_stock_options',
                return_value=[{
                    'identity_key': 'cake:Bolo — 22 cm — Chocolate',
                    'produto': 'Bolo — 22 cm — Chocolate',
                    'kind': 'cake',
                    'ativo': True,
                    'saldo': 12,
                    'saldo_inicial_confirmado': True,
                }],
            ), patch.object(
                pastelaria_routes,
                'criar_ordens_transferencia_pastelaria',
                return_value={'order_ids': [101], 'replayed': False},
            ) as create_batch:
                response = pastelaria_routes.transferir()

        self.assertEqual(response.status_code, 302)
        create_batch.assert_called_once()
        self.assertEqual(
            create_batch.call_args.kwargs['lines'],
            [{
                'identity_key': 'cake:Bolo — 22 cm — Chocolate',
                'quantidade': 8,
            }],
        )
        self.assertEqual(create_batch.call_args.kwargs['loja_destino'], 'Bolhão')
        self.assertEqual(create_batch.call_args.kwargs['request_key'], 'request-1')

    def test_transfer_rejects_forged_cake_configuration(self):
        data = {
            'loja_destino': 'Bolhão',
            'data_prevista': '2026-09-01',
            'request_key': 'request-2',
            'produto_0': 'cake:Bolo — 99 cm — Inventado',
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
                'get_pastelaria_stock_options',
                return_value=[],
            ), patch.object(
                pastelaria_routes,
                'criar_ordens_transferencia_pastelaria',
            ) as create_batch:
                response = pastelaria_routes.transferir()

        self.assertEqual(response.status_code, 302)
        create_batch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
