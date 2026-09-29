from datetime import date
import html as html_lib
from pathlib import Path
import re
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


def monthly_cell(html, month, column_index):
    match = re.search(
        rf'<tr[^>]*data-month="{re.escape(month)}"[^>]*>(.*?)</tr>',
        html,
        flags=re.DOTALL,
    )
    if not match:
        return None
    cells = re.findall(
        r"<(?:th|td)(?:\s[^>]*)?>(.*?)</(?:th|td)>",
        match.group(1),
        flags=re.DOTALL,
    )
    if column_index >= len(cells):
        return None
    return visible_text(cells[column_index])


def visible_text(markup):
    return " ".join(
        html_lib.unescape(re.sub(r"<[^>]+>", " ", markup)).split()
    )


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
            if start.month == 4:
                return doseamento_payload(
                    "invalid",
                    ["negative_stock_residual"],
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
        text = visible_text(html)
        self.assertIn("Calculado", text)
        self.assertIn("Parcial", text)
        self.assertIn("Auditoria física: Fiável", text)
        self.assertIn("Auditoria física: Incompleta", text)
        self.assertIn("Auditoria física: Inválida", text)
        self.assertNotIn("Ver motivos e dados", html)
        self.assertNotIn("Faltam pesagens comparáveis", text)
        self.assertIn("Cone de Baunilha", text)
        self.assertIn("Bolhão ·", text)
        self.assertIn("72.00 €", text)
        self.assertIn("Configurar esta dose", text)
        self.assertIn("produto_id=301", html)
        self.assertIn("data_inicio=2026-02-01", html)
        self.assertIn("data_fim=2026-02-28", html)
        self.assertIn("Ver vendas ao peso", text)
        self.assertNotIn("produto_id=302", html)
        self.assertNotIn("20/03/2026", text)
        self.assertNotIn("23/03/2026", text)
        self.assertIn("Auditoria física incompleta ou inválida", text)
        shared_note = "Auditoria física incompleta ou inválida; ver Pesagens."
        self.assertEqual(html.count(shared_note), 1)
        note_groups = re.findall(
            r'<li class="audit-note-group">(.*?)</li>', html, re.DOTALL
        )
        self.assertEqual(len(note_groups), 1)
        self.assertIn("Março, Abril", visible_text(note_groups[0]))
        self.assertIn("Abrir Pesagens", visible_text(note_groups[0]))
        self.assertNotIn("negative_stock_residual", text)
        self.assertIn("€/kg operacional", text)
        self.assertIn("Teórico conhecido (kg)", text)
        self.assertNotIn("Real auditado (kg)", text)
        self.assertIn("Deslize horizontalmente", text)
        self.assertIn('[data-bs-theme="dark"] .eurokg-shell', html)
        self.assertIn("@media (max-width:767.98px)", html)
        self.assertIn("font-variant-numeric:tabular-nums", html)
        self.assertIn("position:sticky; left:0", html)

        table_head = re.search(r"<thead>(.*?)</thead>", html, re.DOTALL).group(1)
        self.assertIn(
            "Mês Consumo e dose Receita Consumo operacional (kg) "
            "Teórico conhecido (kg) Desvio (kg) Rendimento (%) Vendas (€) "
            "€/kg operacional",
            visible_text(table_head),
        )
        self.assertNotIn(">Dados</th>", table_head)
        self.assertEqual(html.count('<col class="'), 7)
        self.assertIn('colspan="4"', table_head)
        self.assertIn('colspan="2"', table_head)

        january = re.search(
            r'<tr[^>]*data-month="Janeiro"[^>]*>.*?</tr>', html, re.DOTALL
        ).group(0)
        self.assertEqual(
            len(re.findall(r"<(?:th|td)(?:\s[^>]*)?>", january)),
            7,
        )
        self.assertNotRegex(january, r"<details[^>]*\bopen\b")
        self.assertNotIn("<details", january)
        january_detail = re.search(
            r'<details[^>]*data-month-details="Janeiro"[^>]*>(.*?)</details>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(january_detail)
        self.assertNotRegex(january_detail.group(0), r"<details[^>]*\bopen\b")
        self.assertNotIn("<ul", january_detail.group(1))
        for movement_label in (
            "Stock inicial (kg)", "Produção (kg)", "Stock final (kg)",
            "Quebras (kg)",
        ):
            self.assertIn(movement_label, january_detail.group(1))

        table_end = html.index("</table>")
        self.assertGreater(html.index("Dados e avisos"), table_end)
        for moved_explanation in (
            "Como ler o resumo", "Como ler:", "Como interpretar",
        ):
            self.assertGreater(html.index(moved_explanation), table_end)
        self.assertGreater(html.index("produto_id=301"), table_end)
        self.assertIn("Total anual — produção (kg):", text)
        self.assertIn("Total anual — quebras (kg):", text)
        footer = visible_text(
            re.search(r"<tfoot>(.*?)</tfoot>", html, re.DOTALL).group(1)
        )
        self.assertEqual(footer, "TOTAL 60 — — — 1200 20.00")

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
        self.assertIn("Auditoria física: Fiável", visible_text(html))

    def test_months_without_positive_consumption_do_not_break_summary(self):
        annual = annual_data()
        annual[2].update({"consumo": 0, "kpi": 0})
        annual[3].update({"consumo": -1, "kpi": 0})
        self.mocks[1].return_value = annual
        self.mocks[4].return_value = doseamento_payload("reliable")

        store_contexts = [
            ("Global Porto", None, [
                {"name": "Bolhão"}, {"name": "Matosinhos"},
            ]),
            ("Bolhão", "Bolhão", [{"name": "Bolhão"}]),
        ]
        for context in store_contexts:
            with self.subTest(loja=context[0]):
                self.mocks[0].return_value = context
                response = self.client.get("/eurokg/resumo")

                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertEqual(monthly_cell(html, "Janeiro", 6), "20.00")
                self.assertEqual(monthly_cell(html, "Fevereiro", 6), "—")
                self.assertEqual(monthly_cell(html, "Março", 6), "—")


if __name__ == "__main__":
    unittest.main()