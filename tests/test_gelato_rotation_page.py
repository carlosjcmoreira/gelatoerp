import unittest
from datetime import date
from unittest.mock import patch

from flask import Flask, render_template

from flask_app.routes import producao


class GelatoRotationPageTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask('flask_app')
        self.app.secret_key = 'test'
        self.app.add_url_rule('/login', endpoint='auth.login', view_func=lambda: '')
        self.app.add_url_rule('/', endpoint='home.index', view_func=lambda: '')
        self.app.register_blueprint(producao.producao_bp, url_prefix='/producao')

    def test_tile_is_registered_in_navigation_and_manager_config(self):
        from flask_app.routes.gestor import _TILE_MASTER

        tab = next(item for item in producao.TABS if item['id'] == 'rotacao_stock')
        self.assertEqual(tab['url_endpoint'], 'producao.rotacao_stock')
        self.assertIn(
            'rotacao_stock',
            {item['id'] for item in _TILE_MASTER['producao']},
        )

    def test_default_period_is_last_30_calendar_days(self):
        start, end, valid = producao._rotation_period({}, date(2026, 9, 16))
        self.assertEqual(start, date(2026, 8, 18))
        self.assertEqual(end, date(2026, 9, 16))
        self.assertFalse(valid)

    def test_invalid_period_returns_safe_default(self):
        args = {'data_inicio': '2026-09-20', 'data_fim': '2026-09-01'}
        start, end, valid = producao._rotation_period(args, date(2026, 9, 16))
        self.assertEqual((start, end), (date(2026, 8, 18), date(2026, 9, 16)))
        self.assertFalse(valid)

    def test_excluded_only_intervals_still_show_weighings_and_anomaly(self):
        rotation = {
            'stores': [{'id': 1, 'name': 'Bolhão', 'is_production': False}],
            'rows': [{
                'sabor': 'Baunilha',
                'stores': {
                    'Bolhão': {
                        'first_observation': None,
                        'last_observation': None,
                        'valid_intervals': 0,
                        'excluded_intervals': 1,
                        'issues': ['unknown_transfer_destination'],
                    },
                },
            }],
        }

        summary = producao._rotation_store_summaries(rotation)['Bolhão']

        self.assertTrue(summary['has_comparable_weighings'])
        self.assertEqual(summary['anomaly_count'], 1)

    def test_intervals_are_attached_to_their_store_and_flavor_cell(self):
        interval = {'store': 'Bolhão', 'sabor': 'Baunilha', 'usable': True}
        rotation = {
            'intervals': [interval],
            'rows': [{
                'sabor': 'Baunilha',
                'stores': {'Bolhão': {}},
            }],
        }
        producao._attach_rotation_intervals(rotation)
        self.assertEqual(
            rotation['rows'][0]['stores']['Bolhão']['intervals'],
            [interval],
        )

    @patch('flask_app.routes.producao._tabs_with_urls', return_value=[])
    @patch('flask_app.routes.producao.render_template')
    @patch('flask_app.routes.producao.get_gelato_stock_rotation')
    def test_route_uses_domain_result_and_production_permission(
        self, get_rotation, render_template, _tabs
    ):
        get_rotation.return_value = {
            'stores': [{'id': 1, 'name': 'Matosinhos', 'is_production': True}],
            'rows': [],
            'intervals': [{'usable': False}],
            'coverage': {'cells_total': 0, 'cells_with_value': 0, 'by_store': {}},
            'unresolved': {},
        }
        with self.app.test_request_context(
            '/producao/rotacao-stock?data_inicio=2026-09-01&data_fim=2026-09-15'
        ):
            from flask import session
            session['user'] = {'acesso_producao': True}
            producao.rotacao_stock()

        get_rotation.assert_called_once_with(date(2026, 9, 1), date(2026, 9, 15))
        context = render_template.call_args.kwargs
        self.assertEqual(context['active_tab'], 'rotacao_stock')
        self.assertEqual(context['excluded_intervals'], 1)

    def test_user_without_production_access_is_redirected(self):
        with self.app.test_request_context('/producao/rotacao-stock'):
            from flask import session
            session['user'] = {'acesso_producao': False}
            response = producao.rotacao_stock()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/')

    def test_template_renders_dynamic_stores_dash_audit_and_anomaly(self):
        rotation = {
            'stores': [
                {'id': 1, 'name': 'Matosinhos', 'is_production': True},
                {'id': 2, 'name': 'Loja Nova', 'is_production': False},
            ],
            'rows': [{
                'sabor': 'Baunilha',
                'stores': {
                    'Matosinhos': {
                        'average_daily_kg': 0.0,
                        'days_observed': 3,
                        'valid_intervals': 1,
                        'excluded_intervals': 1,
                        'coverage_pct': 50.0,
                        'confidence': 'low',
                        'flags': [],
                        'issues': ['negative_stock_residual'],
                        'first_observation': date(2026, 9, 10),
                        'last_observation': date(2026, 9, 13),
                    },
                    'Loja Nova': {
                        'average_daily_kg': None,
                        'days_observed': 0,
                        'valid_intervals': 0,
                        'excluded_intervals': 0,
                        'coverage_pct': 0.0,
                        'confidence': 'none',
                        'flags': [],
                        'issues': ['insufficient_snapshots'],
                        'first_observation': None,
                        'last_observation': None,
                    },
                },
            }],
            'coverage': {
                'cells_total': 2,
                'cells_with_value': 1,
                'by_store': {
                    'Matosinhos': {'high': 0, 'medium': 0, 'low': 1, 'none': 0},
                    'Loja Nova': {'high': 0, 'medium': 0, 'low': 0, 'none': 1},
                },
            },
            'unresolved': {'transfer_flavor': 2},
        }
        valid_interval = {
            'store': 'Matosinhos',
            'sabor': 'Baunilha',
            'start_date': date(2026, 9, 10),
            'end_date': date(2026, 9, 12),
            'days': 2,
            'opening_kg': 10.0,
            'production_kg': 2.0,
            'inbound_kg': 1.0,
            'outbound_kg': 3.0,
            'breakage_kg': 0.5,
            'closing_kg': 7.5,
            'consumption_kg': 2.0,
            'issues': [],
            'flags': [],
            'usable': True,
        }
        excluded_interval = {
            **valid_interval,
            'start_date': date(2026, 9, 12),
            'end_date': date(2026, 9, 13),
            'consumption_kg': None,
            'issues': ['unknown_transfer_destination'],
            'usable': False,
        }
        rotation['rows'][0]['stores']['Matosinhos']['intervals'] = [
            valid_interval,
            excluded_interval,
        ]
        rotation['rows'][0]['stores']['Loja Nova']['intervals'] = []
        with self.app.test_request_context('/producao/rotacao-stock'):
            html = render_template(
                'producao/rotacao_stock.html',
                active_tab='rotacao_stock',
                tabs=[],
                rotation=rotation,
                data_inicio='2026-09-01',
                data_fim='2026-09-15',
                excluded_intervals=1,
                issue_labels=producao._ROTATION_ISSUE_LABELS,
                flag_labels=producao._ROTATION_FLAG_LABELS,
                load_error=False,
                quick_periods=[('7 dias', '2026-09-10')],
                today_iso='2026-09-16',
                store_summaries={
                    'Matosinhos': {'has_comparable_weighings': True, 'anomaly_count': 1},
                    'Loja Nova': {'has_comparable_weighings': False, 'anomaly_count': 0},
                },
                anomaly_issues=producao._ROTATION_ANOMALY_ISSUES,
                unresolved_labels=producao._ROTATION_UNRESOLVED_LABELS,
                unresolved_total=2,
            )

        self.assertIn('Loja Nova', html)
        self.assertIn('Sem pesagens', html)
        self.assertIn('Sem base suficiente', html)
        self.assertIn('Intervalos válidos:</strong> 0', html)
        self.assertIn('Confiança:</strong> Sem confiança calculável', html)
        self.assertIn('0.000', html)
        self.assertIn('Dados incoerentes', html)
        self.assertIn('position: sticky', html)
        self.assertIn('Intervalos auditáveis', html)
        self.assertIn('10.000 stock inicial', html)
        self.assertIn('2.000 produção', html)
        self.assertIn('Consumo usado:</strong> 2.000 kg', html)
        self.assertIn('Motivo da exclusão:', html)
        self.assertIn('Destino de transferência não identificado', html)
        self.assertIn('2 registo(s) de origem', html)
        self.assertIn('Transferências com sabor não identificado', html)

    def test_template_surfaces_unresolved_sources_without_matrix_rows(self):
        with self.app.test_request_context('/producao/rotacao-stock'):
            html = render_template(
                'producao/rotacao_stock.html',
                active_tab='rotacao_stock',
                tabs=[],
                rotation={
                    'stores': [],
                    'rows': [],
                    'coverage': {
                        'cells_total': 0,
                        'cells_with_value': 0,
                        'by_store': {},
                    },
                    'unresolved': {'stock_flavor': 3},
                },
                data_inicio='2026-09-01',
                data_fim='2026-09-15',
                excluded_intervals=0,
                issue_labels=producao._ROTATION_ISSUE_LABELS,
                flag_labels=producao._ROTATION_FLAG_LABELS,
                load_error=False,
                quick_periods=[('7 dias', '2026-09-10')],
                today_iso='2026-09-16',
                store_summaries={},
                anomaly_issues=producao._ROTATION_ANOMALY_ISSUES,
                unresolved_labels=producao._ROTATION_UNRESOLVED_LABELS,
                unresolved_total=3,
            )

        self.assertIn('3 registo(s) de origem', html)
        self.assertIn('Pesagens com sabor não identificado', html)
        self.assertIn('Ainda não existem pesagens suficientes', html)


if __name__ == '__main__':
    unittest.main()