from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from flask import Flask

from flask_app.routes import eurokg


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 28)


def annual_data():
    return {
        month: {
            "vendas": 100,
            "consumo": 5,
            "kpi": 20,
            "stock_ini": 8,
            "entrada": 2,
            "producao": 2,
            "stock_final": 4,
            "quebras": 1,
        }
        for month in range(1, 13)
    }


def doseamento_payload(status, issues=None, **overrides):
    payload = {
        "status": status,
        "issues": issues or [],
        "coverage_pct": 100 if status == "reliable" else 0,
        "mapped_theoretical_kg": 2 if status == "reliable" else None,
        "theoretical_kg": 2 if status == "reliable" else None,
        "real_kg": 2 if status == "reliable" else None,
        "variance_kg": 0 if status == "reliable" else None,
        "variance_pct": 0 if status == "reliable" else None,
        "yield_pct": 100 if status == "reliable" else None,
        "revenue_per_kg": 10 if status == "reliable" else None,
        "unmapped_products": [],
        "weighted_products": [],
        "unmapped_product_details": [],
        "weighted_product_details": [],
        "loja": "Bolhão",
        "coverage_gaps": [],
        "interval_diagnostics": [],
    }
    payload.update(overrides)
    return payload


class EurokgMonthlyQualityTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.app = Flask(
            __name__,
            template_folder=str(root / "flask_app" / "templates"),
            static_folder=str(root / "flask_app" / "static"),
        )
        self.app.secret_key = "monthly-quality-test"
        self.app.register_blueprint(eurokg.eurokg_bp, url_prefix="/eurokg")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user"] = {
                "id": 10,
                "username": "test-manager",
                "acesso_gestor": True,
                "acesso_eurokg": True,
            }

        self.store_context = patch.object(
            eurokg,
            "_store_context",
            return_value=(
                "Global Porto",
                None,
                [{"name": "Bolhão"}, {"name": "Matosinhos"}],
            ),
        )
        self.annual = patch.object(
            eurokg, "calculate_kpi_annual", return_value=annual_data()
        )
        self.target = patch.object(
            eurokg, "get_target_by_month", return_value=0
        )
        self.tabs = patch.object(eurokg, "_build_tabs", return_value=[])
        self.period = patch.object(eurokg, "get_doseamento_period")
        self.today = patch.object(eurokg, "date", FrozenDate)
        self.mocks = [
            patcher.start()
            for patcher in (
                self.store_context,
                self.annual,
                self.target,
                self.tabs,
                self.period,
                self.today,
            )
        ]
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for patcher in reversed((
            self.store_context,
            self.annual,
            self.target,
            self.tabs,
            self.period,
            self.today,
        )):
            patcher.stop()

    def test_monthly_summary_translates_and_discloses_month_quality(self):
        period = self.mocks[4]

        def monthly_payload(start, _end, _store, _stores):
            if start.month == 1:
                return doseamento_payload("reliable")
            if start.month == 2:
                return doseamento_payload(
                    "incomplete",
                    ["insufficient_coverage", "no_usable_intervals",
                     "insufficient_snapshots"],
                    unmapped_products=["Cone de Baunilha"],
                    mapped_theoretical_kg=1.35,
                    unmapped_product_details=[{
                        "product": "Cone de Baunilha",
                        "quantity": 4,
                        "revenue": 72,
                        "sales_count": 4,
                        "product_id": 301,
                    }],
                    weighted_products=["Gelado ao peso"],
                    weighted_product_details=[{
                        "product": "Gelado ao peso",
                        "quantity": 2,
                        "revenue": 18,
                        "sales_count": 2,
                        "product_id": 302,
                    }],
                    coverage_gaps=[{
                        "store": "Bolhão",
                        "flavor": "Baunilha",
                        "snapshot_count": 1,
                        "valid_intervals": 0,
                        "excluded_intervals": 0,
                        "coverage_pct": 0,
                        "issues": ["insufficient_snapshots"],
                    }],
                )
            if start.month == 3:
                return doseamento_payload(
                    "invalid",
                    ["negative_stock_residual", "insufficient_coverage"],
                    interval_diagnostics=[{
                        "store": "Matosinhos",
                        "flavor": "Chocolate",
                        "start_date": date(2026, 3, 20),
                        "end_date": date(2026, 3, 23),
                        "days": 3,
                        "usable": False,
                        "issues": ["negative_stock_residual"],
                        "flags": [],
                    }],
                )
            return doseamento_payload("reliable")

        period.side_effect = monthly_payload

        response = self.client.get("/eurokg/resumo")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(period.call_count, 9)
        self.assertEqual(period.call_args_list[0].args, (
            date(2026, 1, 1),
            date(2026, 1, 31),
            None,
            ["Bolhão", "Matosinhos"],
        ))
        self.assertEqual(period.call_args_list[-1].args, (
            date(2026, 9, 1),
            date(2026, 9, 28),
            None,
            ["Bolhão", "Matosinhos"],
        ))

        html = response.get_data(as_text=True)
        self.assertIn("Calculado", html)
        self.assertIn("Parcial", html)
        self.assertIn("Auditoria física: Fiável", html)
        self.assertIn("Auditoria física: Incompleta", html)
        self.assertIn("Auditoria física: Inválida", html)
        self.assertNotIn("Ver motivos e dados", html)
        self.assertNotIn("Faltam pesagens comparáveis", html)
        self.assertIn("Cone de Baunilha", html)
        self.assertIn("Bolhão ·", html)
        self.assertIn("72.00 €", html)
        self.assertIn("Configurar esta dose", html)
        self.assertIn("produto_id=301", html)
        self.assertIn("data_inicio=2026-02-01", html)
        self.assertIn("data_fim=2026-02-28", html)
        self.assertIn("Ver vendas ao peso", html)
        self.assertNotIn("produto_id=302", html)
        self.assertNotIn("20/03/2026", html)
        self.assertNotIn("23/03/2026", html)
        self.assertIn("Auditoria física incompleta ou inválida", html)
        self.assertNotIn("negative_stock_residual", html)
        self.assertIn("€/kg operacional", html)
        self.assertIn("Teórico conhecido (kg)", html)
        self.assertNotIn("Real auditado (kg)", html)
        self.assertIn("Deslize horizontalmente", html)
        self.assertIn('[data-bs-theme="dark"] .eurokg-shell', html)
        self.assertIn("@media (max-width:767.98px)", html)

    def test_store_view_passes_only_the_selected_store_to_month_calculation(self):
        self.mocks[0].return_value = (
            "Bolhão",
            "Bolhão",
            [{"name": "Bolhão"}],
        )
        period = self.mocks[4]
        period.return_value = doseamento_payload("reliable")

        response = self.client.get("/eurokg/resumo?loja=Bolhão")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(period.call_count, 9)
        self.assertTrue(all(
            call.args[2:] == ("Bolhão", ["Bolhão"])
            for call in period.call_args_list
        ))
        html = response.get_data(as_text=True)
        self.assertIn("Bolhão", html)
        self.assertIn("Auditoria física: Fiável", html)


if __name__ == "__main__":
    unittest.main()