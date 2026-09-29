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
        'unmapped_product_details': [],
        'weighted_product_details': [],
        'mapped_theoretical_kg': None,
        'loja': 'Bolhão',
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

    def test_dashboard_uses_exact_30_day_window_and_keeps_audit_status_separate(self):
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
        self.assertIn('Auditoria física: Inválida', html)
        self.assertIn('Auditoria física incompleta ou inválida', html)
        self.assertIn('Consumo operacional', html)
        self.assertNotIn('negative_stock_residual', html)
        self.assertNotIn('20/09/2026', html)
        self.assertIn('KPI operacional (€/kg)', html)
        self.assertIn('Deslize horizontalmente', html)

    def test_partial_estimate_shows_missing_product_and_excluded_revenue(self):
        get_doseamento = self.mocks[-1]
        payload = doseamento_payload(
            'incomplete', ['unmapped_products']
        )
        payload.update({
            'mapped_theoretical_kg': 0.35,
            'unmapped_products': ['Cone de Baunilha'],
            'unmapped_product_details': [{
                'product': 'Cone de Baunilha',
                'quantity': 4,
                'revenue': 72.0,
                'sales_count': 4,
                'product_id': 101,
            }],
            'weighted_product_details': [{
                'product': 'Gelado ao peso',
                'quantity': 2,
                'revenue': 18.0,
                'sales_count': 2,
                'product_id': 202,
            }],
        })
        get_doseamento.return_value = payload
        response = self.client.get('/eurokg/dashboard')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('Auditoria física: Incompleta', html)
        self.assertIn('Teórico conhecido · Parcial', html)
        self.assertIn('0.35', html)
        self.assertIn('Cone de Baunilha', html)
        self.assertIn('72.00 €', html)
        self.assertIn('Ver doses e vendas ao peso', html)
        self.assertIn('Configurar esta dose', html)
        self.assertIn('produto_id=101', html)
        self.assertIn('loja=Bolh%C3%A3o', html)
        self.assertIn('Ver vendas ao peso', html)
        self.assertNotIn('produto_id=202', html)
        self.assertIn('data_inicio=2026-08-30', html)
        self.assertIn('data_fim=2026-09-28', html)
        self.assertNotIn('unmapped_products', html)

    def test_non_manager_sees_no_manager_only_product_configuration_link(self):
        payload = doseamento_payload(
            'incomplete', ['unmapped_products']
        )
        payload.update({
            'unmapped_products': ['Cone de Baunilha'],
            'unmapped_product_details': [{
                'product': 'Cone de Baunilha',
                'quantity': 4,
                'revenue': 72.0,
                'product_id': 101,
            }],
        })
        self.mocks[-1].return_value = payload

        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 11,
                'username': 'store-user',
                'acesso_gestor': False,
                'acesso_eurokg': True,
                'vendas_store_ids': [1],
            }

        with patch.object(
            eurokg, '_store_context',
            return_value=('Bolhão', 'Bolhão', [{'name': 'Bolhão'}]),
        ):
            response = self.client.get('/eurokg/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('produto_id=101', html)
        self.assertNotIn('Configurar esta dose', html)

    def test_global_estimate_sums_known_store_subtotals_and_keeps_local_gaps(self):
        estimate = eurokg._prepare_operational_estimate({
            'status': 'incomplete',
            'issues': ['unmapped_products'],
            'stores': [
                {
                    'loja': 'Bolhão',
                    'mapped_theoretical_kg': 0.4,
                    'unmapped_product_details': [{
                        'product': 'Cone',
                        'quantity': 2,
                        'revenue': 24,
                    }],
                },
                {
                    'loja': 'Matosinhos',
                    'mapped_theoretical_kg': None,
                    'unmapped_product_details': [],
                },
            ],
        }, 2.0, 50.0)

        self.assertEqual(estimate['theoretical_kg'], 0.4)
        self.assertAlmostEqual(estimate['variance_kg'], 1.6)
        self.assertEqual(estimate['yield_pct'], 20.0)
        self.assertEqual(estimate['revenue_per_kg'], 25.0)
        self.assertEqual(estimate['missing_products'][0]['store'], 'Bolhão')
        self.assertEqual(estimate['missing_products'][0]['revenue'], 24.0)

    def test_zero_operational_consumption_never_produces_division_metrics(self):
        estimate = eurokg._prepare_operational_estimate({
            'status': 'incomplete',
            'issues': ['unmapped_products'],
            'mapped_theoretical_kg': None,
            'unmapped_products': ['Unknown'],
        }, 0, 10)

        self.assertIsNone(estimate['theoretical_kg'])
        self.assertIsNone(estimate['variance_kg'])
        self.assertIsNone(estimate['yield_pct'])
        self.assertIsNone(estimate['revenue_per_kg'])


if __name__ == '__main__':
    unittest.main()
