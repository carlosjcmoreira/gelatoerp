import unittest

import pandas as pd

from flask_app.routes.eurokg import _build_consumo_teorico_view


class ConsumoTeoricoViewTests(unittest.TestCase):
    def test_unknown_historical_rule_is_not_rendered_as_zero(self):
        frame = pd.DataFrame([
            {
                "produto": "Copo",
                "mes": "2026-01",
                "quantidade_vendida": 3,
                "gramas_por_unidade": float("nan"),
                "consumo_kg": float("nan"),
            },
        ])
        rows, _labels, _qty, totals_kg = _build_consumo_teorico_view(frame)
        self.assertIsNone(rows[0]["months"][0]["kg"])
        self.assertIsNone(totals_kg[0])

    def test_mixed_known_and_unknown_month_keeps_total_unknown(self):
        frame = pd.DataFrame([
            {
                "produto": "Copo",
                "mes": "2026-09",
                "quantidade_vendida": 2,
                "gramas_por_unidade": 100,
                "consumo_kg": 0.2,
            },
            {
                "produto": "Novo",
                "mes": "2026-09",
                "quantidade_vendida": 1,
                "gramas_por_unidade": float("nan"),
                "consumo_kg": float("nan"),
            },
        ])
        _rows, _labels, qty, totals_kg = _build_consumo_teorico_view(frame)
        self.assertEqual(qty, [3])
        self.assertIsNone(totals_kg[0])