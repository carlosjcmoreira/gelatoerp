import unittest
from datetime import date
from unittest.mock import patch

from db.doseamento import _preview_interval_changes, preview_historical_dose_csv


class HistoricalDosePreviewTests(unittest.TestCase):
    def test_normalizes_fixed_and_weight_rows(self):
        csv_data = (
            "\ufeffartigo,tipo_dose,gramas,data_efetiva,evidencia\n"
            "Copo Pequeno,fixa,\"95,5\",2024-01-01,Ficha técnica 2024\n"
            "Gelado ao peso,peso,,2024-02-01,Tabela assinada\n"
        )

        rows = preview_historical_dose_csv(csv_data)

        self.assertEqual(rows[0]["gramas"], "95.5")
        self.assertEqual(rows[0]["valid_from"], "2024-01-01")
        self.assertIsNone(rows[1]["gramas"])

    def test_requires_dated_evidence(self):
        csv_data = (
            "artigo,tipo_dose,gramas,data_efetiva,evidencia\n"
            "Copo,fixa,100,2024-01-01,\n"
        )

        with self.assertRaisesRegex(ValueError, "evidência"):
            preview_historical_dose_csv(csv_data)

    def test_rejects_duplicate_product_start_date(self):
        csv_data = (
            "artigo,tipo_dose,gramas,data_efetiva,evidencia\n"
            "Copo,fixa,100,2024-01-01,Ficha A\n"
            "copo,fixa,110,2024-01-01,Ficha B\n"
        )

        with self.assertRaisesRegex(ValueError, "repetidos"):
            preview_historical_dose_csv(csv_data)

    @patch("db.doseamento.date")
    def test_rejects_future_effective_date(self, mocked_date):
        mocked_date.today.return_value = date(2026, 9, 16)
        mocked_date.fromisoformat.side_effect = date.fromisoformat
        csv_data = (
            "artigo,tipo_dose,gramas,data_efetiva,evidencia\n"
            "Copo,fixa,100,2026-09-17,Ficha futura\n"
        )

        with self.assertRaisesRegex(ValueError, "futura"):
            preview_historical_dose_csv(csv_data)

    def test_preview_shows_non_overlapping_ranges_for_multiple_versions(self):
        rows = [
            {
                "artigo": "Copo", "tipo_dose": "fixa", "gramas": "90",
                "valid_from": "2024-01-01", "evidence_reference": "Ficha A",
            },
            {
                "artigo": "Copo", "tipo_dose": "fixa", "gramas": "100",
                "valid_from": "2025-01-01", "evidence_reference": "Ficha B",
            },
        ]
        current = [{
            "id": 10, "artigo": "Copo", "gramas": 110,
            "tipo_dose": "fixa", "valid_from": date(2026, 1, 1),
            "valid_to": None, "evidence_reference": None,
        }]

        changes = _preview_interval_changes(rows, current)

        self.assertEqual(changes[0]["valid_to"], "2024-12-31")
        self.assertEqual(changes[1]["valid_to"], "2025-12-31")
        self.assertFalse(changes[0]["shortens_imported_row"])
        self.assertTrue(changes[1]["shortens_imported_row"])

    def test_preview_rejects_existing_effective_date(self):
        rows = [{
            "artigo": "Copo", "tipo_dose": "fixa", "gramas": "90",
            "valid_from": "2024-01-01", "evidence_reference": "Ficha A",
        }]
        current = [{
            "id": 1, "artigo": "copo", "gramas": 80,
            "tipo_dose": "fixa", "valid_from": date(2024, 1, 1),
            "valid_to": date(2025, 1, 1), "evidence_reference": "Antiga",
        }]

        with self.assertRaisesRegex(ValueError, "Já existe"):
            _preview_interval_changes(rows, current)


if __name__ == "__main__":
    unittest.main()