from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from flask import Flask
import pandas as pd

from flask_app.routes import eurokg


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 28)


def doseamento_payload(status, issues=None):
    return {
        'status': status,
        'issues': issues or [],
        'issue_guidance': [],
        'coverage_pct': 75.0,
        'mapped_sales_pct': 99.8,
        'unmapped_products': [],
        'weighted_products': [],
        'coverage_gaps': [],
        'interval_diagnostics': [{
            'store': 'Bolhão',
            'flavor': 'Baunilha',
            'start_date': date(2026, 9, 20),
            'end_date': date(2026, 9, 24),
            'days': 4,
            'usable': False,
            'issues': ['negative_stock_residual'],
            'flags': [],
        }],
        'theoretical_kg': None,
        'real_kg': None,
        'variance_kg': None,
        'variance_pct': None,
        'yield_pct': None,
        'revenue_per_kg': None,
    }


class EurokgDashboardQualityTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.app = Flask(
            __name__,
            template_folder=str(root / 'flask_app' / 'templates'),
            static_folder=str(root / 'flask_app' / 'static'),
        )
        self.app.secret_key = 'dashboard-quality-test'
        self.app.register_blueprint(eurokg.eurokg_bp, url_prefix='/eurokg')
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 10,
                'username': 'test-manager',
                'acesso_gestor': True,
                'acesso_eurokg': True,
            }

        self.daily = pd.DataFrame([{
            'data': date(2026, 9, 28),
            'vendas': 50.0,
            'consumo_kg': 2.0,
            'stock_ini_kg': 10.0,
            'entrada_kg': 2.0,
            'stock_final_kg': 9.0,
            'quebras_kg': 0.0,
            'kpi': 25.0,
        }])
        self.patches = [
            patch.object(eurokg, 'date', FrozenDate),
            patch.object(eurokg, '_store_context',
                         return_value=('Global Porto', None, [])),
            patch.object(eurokg, '_build_tabs', return_value=[]),
            patch.object(eurokg, 'calculate_kpi_annual', return_value={
                month: {'vendas': 0, 'consumo': 0, 'kpi': 0}
                for month in range(1, 13)
            }),
            patch.object(eurokg, 'calculate_kpi_by_day',
                         return_value=self.daily.copy()),
            patch.object(eurokg, 'get_target_by_month', return_value=0),
            patch.object(eurokg, 'get_doseamento_period'),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for item in reversed(self.patches):
            item.stop()

    def test_dashboard_uses_exact_30_day_window_and_translates_invalid_evidence(self):
        get_doseamento = self.mocks[-1]
        get_doseamento.return_value = doseamento_payload(
            'invalid', ['negative_stock_residual']
        )

        response = self.client.get('/eurokg/dashboard')

        self.assertEqual(response.status_code, 200)
        get_doseamento.assert_called_once_with(
            date(2026, 8, 30), date(2026, 9, 28), None, []
        )
        html = response.get_data(as_text=True)
        self.assertIn('Inválido', html)
        self.assertIn('não interprete o KPI diário como validação', html)
        self.assertIn('Bolhão · Baunilha', html)
        self.assertIn('20/09/2026', html)
        self.assertIn('Os movimentos não conciliam com as pesagens', html)
        self.assertNotIn('negative_stock_residual', html)
        self.assertIn('KPI operacional (€/kg)', html)
        self.assertIn('Deslize horizontalmente', html)

    def test_reliable_and_incomplete_states_render_portuguese_guidance(self):
        get_doseamento = self.mocks[-1]
        for status, issues, expected in [
            ('reliable', [], 'Fiável'),
            ('incomplete', ['unmapped_products'], 'Incompleto'),
        ]:
            with self.subTest(status=status):
                payload = doseamento_payload(status, issues)
                if status == 'incomplete':
                    payload['unmapped_products'] = ['Cone de Baunilha']
                    payload['coverage_gaps'] = [{
                        'store': 'Bolhão',
                        'flavor': 'Baunilha',
                        'snapshot_count': 1,
                        'valid_intervals': 0,
                        'excluded_intervals': 0,
                        'coverage_pct': 0,
                        'issues': ['insufficient_snapshots'],
                    }]
                get_doseamento.return_value = payload
                response = self.client.get('/eurokg/dashboard')
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, html)
                if status == 'incomplete':
                    self.assertIn(
                        'Há produtos vendidos sem dose histórica associada',
                        html,
                    )
                    self.assertIn('Cone de Baunilha', html)
                    self.assertIn('Bolhão · Baunilha', html)
                    self.assertIn('1 pesagens', html)
                self.assertNotIn('unmapped_products', html)


if __name__ == '__main__':
    unittest.main()
