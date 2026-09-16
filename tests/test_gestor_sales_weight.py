import math
from datetime import date
from io import BytesIO
import unittest
from unittest.mock import patch

from flask_app.services.gestor import (
    import_vendas_csv,
    import_vendas_html,
    parse_sales_quantity,
    parse_weight_kg,
)
from db.pastelaria import _apply_vendas_weight_rules


class SalesWeightParsingTests(unittest.TestCase):
    def test_null_weight_remains_unknown_for_old_uploads(self):
        self.assertIsNone(parse_weight_kg(None))
        self.assertIsNone(parse_weight_kg(""))

    def test_signed_kg_supports_returns(self):
        self.assertEqual(parse_weight_kg("-0,375", "kg"), -0.375)

    def test_incompatible_unit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unidade de peso incompatível"):
            parse_weight_kg("500", "g")

    def test_non_finite_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "número finito"):
            parse_weight_kg(math.inf, "kg")

    def test_non_numeric_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "inválido"):
            parse_weight_kg("sem peso", "kg")

    def test_zero_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "não pode ser zero"):
            parse_weight_kg(0, "kg")

    def test_non_finite_sales_quantity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "número finito"):
            parse_sales_quantity(math.nan)

    @patch("database.add_venda_detalhe_batch")
    @patch("database.apply_vendas_weight_rules", side_effect=lambda rows: rows)
    def test_csv_passes_signed_kg_to_sales_detail(self, _apply_rules, add_batch):
        add_batch.return_value = 1
        contents = (
            "Data,Produto,Categoria,Quantidade,Valor,Peso Vendido,Unidade Peso\n"
            "2026-09-01,Gelado ao peso,Gelado,-1,-8.50,\"-0,375\",kg\n"
        ).encode()

        imported = import_vendas_csv(BytesIO(contents), "Matosinhos")

        self.assertEqual(imported, 1)
        record = add_batch.call_args.args[0][0]
        self.assertEqual(record["peso_vendido_kg"], -0.375)
        self.assertEqual(record["quantidade"], -1)

    @patch("database.add_venda_detalhe_batch")
    @patch("database.apply_vendas_weight_rules", side_effect=lambda rows: rows)
    def test_csv_rejects_incompatible_weight_unit(self, _apply_rules, add_batch):
        contents = (
            "Data,Produto,Categoria,Quantidade,Valor,Peso Vendido,Unidade Peso\n"
            "2026-09-01,Gelado ao peso,Gelado,1,8.50,375,g\n"
        ).encode()

        with self.assertRaisesRegex(ValueError, "Unidade de peso incompatível"):
            import_vendas_csv(BytesIO(contents), "Matosinhos")
        add_batch.assert_not_called()

    @patch("database.add_venda_detalhe_batch")
    @patch("database.apply_vendas_weight_rules", side_effect=lambda rows: rows)
    def test_csv_rejects_grams_declared_in_weight_header(
        self, _apply_rules, add_batch
    ):
        contents = (
            "Data,Produto,Categoria,Quantidade,Valor,Peso Vendido (g)\n"
            "2026-09-01,Gelado ao peso,Gelado,1,8.50,375\n"
        ).encode()

        with self.assertRaisesRegex(ValueError, "Unidade de peso incompatível"):
            import_vendas_csv(BytesIO(contents), "Matosinhos")
        add_batch.assert_not_called()

    @patch("database.add_venda_detalhe_batch")
    @patch("database.apply_vendas_weight_rules")
    def test_real_html_shape_uses_decimal_quantity_as_weight(
        self, apply_rules, add_batch
    ):
        def mark_weight(rows):
            enriched = [dict(row) for row in rows]
            enriched[0]["peso_vendido_kg"] = enriched[0]["quantidade"]
            return enriched

        apply_rules.side_effect = mark_weight
        add_batch.return_value = 1
        html = """
        <table>
          <tr><td>Loja: 1</td></tr>
          <tr>
            <th>Data</th><th>Código</th><th>Produto</th>
            <th>Familia / Sub-Familia</th><th>Quantidade</th>
            <th>Valor Total S/IVA</th><th>Valor Total</th>
          </tr>
          <tr>
            <td>01-09-2026</td><td>99</td><td>Copo "a peso"</td>
            <td>Copos /</td><td>1,950</td><td>17,00€</td><td>19,50€</td>
          </tr>
        </table>
        """.encode()

        imported, skipped = import_vendas_html(
            BytesIO(html), {"1": "Matosinhos"}
        )

        self.assertEqual((imported, skipped), (1, 0))
        record = add_batch.call_args.args[0][0]
        self.assertEqual(record["quantidade"], 1.95)
        self.assertEqual(record["peso_vendido_kg"], 1.95)

    def test_historical_weight_rule_copies_decimal_quantity_to_kg(self):
        records = [{
            "data": date(2026, 9, 1),
            "produto": 'Copo "a peso"',
            "quantidade": 1.95,
            "peso_vendido_kg": None,
        }]
        history = [{
            "artigo": 'Copo "a peso"',
            "tipo_dose": "peso",
            "valid_from": date(2026, 8, 1),
            "valid_to": None,
        }]

        enriched = _apply_vendas_weight_rules(records, history)

        self.assertEqual(enriched[0]["peso_vendido_kg"], 1.95)
        self.assertIsNone(records[0]["peso_vendido_kg"])

    def test_fixed_rule_does_not_copy_quantity_to_weight(self):
        records = [{
            "data": date(2026, 9, 1),
            "produto": "Copo Pequeno",
            "quantidade": 2.0,
            "peso_vendido_kg": None,
        }]
        history = [{
            "artigo": "Copo Pequeno",
            "tipo_dose": "fixa",
            "valid_from": date(2026, 8, 1),
            "valid_to": None,
        }]

        enriched = _apply_vendas_weight_rules(records, history)

        self.assertIsNone(enriched[0]["peso_vendido_kg"])

    def test_longer_fixed_rule_wins_over_shorter_weight_rule(self):
        records = [{
            "data": date(2026, 9, 1),
            "produto": "Copo Grande",
            "quantidade": 2.0,
            "peso_vendido_kg": None,
        }]
        history = [
            {
                "artigo": "Copo",
                "tipo_dose": "peso",
                "valid_from": date(2026, 8, 1),
                "valid_to": None,
            },
            {
                "artigo": "Copo Grande",
                "tipo_dose": "fixa",
                "gramas": 250,
                "valid_from": date(2026, 8, 1),
                "valid_to": None,
            },
        ]

        enriched = _apply_vendas_weight_rules(records, history)

        self.assertIsNone(enriched[0]["peso_vendido_kg"])

    def test_auto_derived_weight_rejects_non_finite_quantity(self):
        records = [{
            "data": date(2026, 9, 1),
            "produto": 'Copo "a peso"',
            "quantidade": math.nan,
            "peso_vendido_kg": None,
        }]
        history = [{
            "artigo": 'Copo "a peso"',
            "tipo_dose": "peso",
            "valid_from": date(2026, 8, 1),
            "valid_to": None,
        }]

        with self.assertRaisesRegex(ValueError, "finito"):
            _apply_vendas_weight_rules(records, history)


if __name__ == "__main__":
    unittest.main()