import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from flask import Flask

from flask_app.routes.vendas import vendas_bp


class PastelariaBlankSundayCountTests(unittest.TestCase):
    SNAPSHOT_TOKEN = '81' * 16
    COUNT_DATE = date(2026, 9, 6)

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
        self.store = {
            'id': 1,
            'name': 'Bolhão',
            'store_type': 'loja',
            'requires_eod_weighing': True,
        }
        with self.client.session_transaction() as session:
            session['user'] = {
                'username': 'bolhao',
                'role': 'vendas',
                'vendas_store_ids': [1],
            }

    def _grid(self, product_ids=(10, 11)):
        return {
            'products': [
                {
                    'id': product_id,
                    'nome': f'Produto {product_id}',
                    'count': None,
                    'counts': {1: None},
                }
                for product_id in product_ids
            ],
            'stores': [self.store],
            'completed': 0,
            'total': len(product_ids),
            'complete': False,
            'snapshot_token': self.SNAPSHOT_TOKEN,
        }

    def _form(self, **quantities):
        return {
            'action': 'guardar_grelha',
            'data_contagem': self.COUNT_DATE.isoformat(),
            'snapshot_token': self.SNAPSHOT_TOKEN,
            **{
                f'count_{product_id}': value
                for product_id, value in quantities.items()
            },
        }

    @contextmanager
    def _patch_route(self, grids, save_side_effect=None):
        with (
            patch(
                'flask_app.routes.vendas.get_store_by_id',
                return_value=self.store,
            ),
            patch(
                'flask_app.routes.vendas.get_pastelaria_sunday_count_grid',
                side_effect=grids,
            ) as get_grid,
            patch(
                'flask_app.routes.vendas.save_pastelaria_store_counts',
                return_value=2,
                side_effect=save_side_effect,
            ) as save_counts,
            patch('flask_app.routes.vendas._build_tabs', return_value=[]),
            patch(
                'flask_app.routes.vendas.get_vendas_module_stores',
                return_value=[],
            ),
        ):
            yield get_grid, save_counts

    def test_blank_and_explicit_zero_values_are_saved_as_zero(self):
        with self._patch_route([self._grid()]) as (_get_grid, save_counts):
            response = self.client.post(
                '/vendas/contagem-pastelaria',
                data=self._form(**{'10': '', '11': '  '}),
            )

        self.assertEqual(response.status_code, 302)
        save_counts.assert_called_once_with(
            self.COUNT_DATE,
            1,
            [(10, 0), (11, 0)],
            self.SNAPSHOT_TOKEN,
            submitted_by='bolhao',
        )

    def test_blanks_mix_with_zero_and_positive_quantities(self):
        grid = self._grid((10, 11, 12, 13))
        with self._patch_route([grid]) as (_get_grid, save_counts):
            response = self.client.post(
                '/vendas/contagem-pastelaria',
                data=self._form(**{
                    '10': '',
                    '11': '0',
                    '12': '5',
                    '13': '  ',
                }),
            )

        self.assertEqual(response.status_code, 302)
        save_counts.assert_called_once_with(
            self.COUNT_DATE,
            1,
            [(10, 0), (11, 0), (12, 5), (13, 0)],
            self.SNAPSHOT_TOKEN,
            submitted_by='bolhao',
        )

    def test_omitted_field_is_rejected_without_partial_save(self):
        grid = self._grid()
        fresh_grid = self._grid()
        rendered = {}

        def capture(_template, **context):
            rendered.update(context)
            return 'count form'

        with (
            self._patch_route([grid, fresh_grid]) as (get_grid, save_counts),
            patch(
                'flask_app.routes.vendas.render_template',
                side_effect=capture,
            ),
        ):
            response = self.client.post(
                '/vendas/contagem-pastelaria',
                data=self._form(**{'10': '3'}),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('grelha recebida está incompleta', rendered['error'])
        self.assertEqual(
            rendered['count_grid']['products'][0]['count'],
            3,
        )
        get_grid.assert_called()
        save_counts.assert_not_called()

    def test_negative_fractional_and_non_numeric_values_are_rejected(self):
        for invalid_value in ('-1', '1.5', 'abc'):
            with self.subTest(value=invalid_value):
                rendered = {}

                def capture(_template, **context):
                    rendered.update(context)
                    return 'count form'

                with (
                    self._patch_route([
                        self._grid((10,)),
                        self._grid((10,)),
                    ]) as (_get_grid, save_counts),
                    patch(
                        'flask_app.routes.vendas.render_template',
                        side_effect=capture,
                    ),
                ):
                    response = self.client.post(
                        '/vendas/contagem-pastelaria',
                        data=self._form(**{'10': invalid_value}),
                    )

                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    'números inteiros não negativos',
                    rendered['error'],
                )
                save_counts.assert_not_called()

    def test_stale_snapshot_still_rejects_save_and_keeps_zero(self):
        rendered = {}

        def capture(_template, **context):
            rendered.update(context)
            return 'count form'

        with (
            self._patch_route(
                [self._grid(), self._grid()],
                save_side_effect=ValueError(
                    'Esta grelha foi alterada por outro utilizador.'
                ),
            ) as (_get_grid, save_counts),
            patch(
                'flask_app.routes.vendas.render_template',
                side_effect=capture,
            ),
        ):
            response = self.client.post(
                '/vendas/contagem-pastelaria',
                data=self._form(**{'10': '', '11': '4'}),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            rendered['count_grid']['products'][0]['count'],
            0,
        )
        self.assertIn('alterada por outro utilizador', rendered['error'])
        save_counts.assert_called_once_with(
            self.COUNT_DATE,
            1,
            [(10, 0), (11, 4)],
            self.SNAPSHOT_TOKEN,
            submitted_by='bolhao',
        )

    def test_count_form_explains_blank_as_zero_and_does_not_require_values(self):
        with (
            self._patch_route([self._grid((10,))]),
        ):
            response = self.client.get(
                '/vendas/contagem-pastelaria'
                f'?data_contagem={self.COUNT_DATE.isoformat()}'
            )

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'Campos vazios serão guardados como 0.',
            page,
        )
        self.assertIn('id="count-status"', page)
        quantity_input = page.split('name="count_10"', 1)[0].rsplit('<input', 1)[1]
        self.assertNotIn('required', quantity_input)
        self.assertNotIn('border-warning', quantity_input)


if __name__ == '__main__':
    unittest.main()