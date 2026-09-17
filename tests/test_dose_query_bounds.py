from contextlib import contextmanager
from datetime import date
import unittest
from unittest.mock import patch

from db.doseamento import load_dose_sales_with_rules
from db.pastelaria import get_consumo_gelado_mensal


class RecordingCursor:
    def __init__(self):
        self.calls = []
        self.result_index = 0
        self.results = [
            [],  # sales
            [],  # aliases
            [],  # product configs
            [],  # dated associations
            [],  # dated rule history
        ]

    def execute(self, query, params=None):
        self.calls.append((" ".join(query.split()), list(params or [])))

    def fetchall(self):
        result = self.results[self.result_index]
        self.result_index += 1
        return result


class RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        return self._cursor


class DoseQueryBoundsTests(unittest.TestCase):
    def test_sales_and_rule_references_are_limited_to_requested_period(self):
        cursor = RecordingCursor()

        @contextmanager
        def connection():
            yield RecordingConnection(cursor)

        start = date(2025, 2, 1)
        end = date(2025, 2, 28)
        with patch("db.doseamento.db_connection", connection):
            load_dose_sales_with_rules(start, end, "Loja A")

        sales_query, sales_params = cursor.calls[0]
        association_query, association_params = cursor.calls[3]
        history_query, history_params = cursor.calls[4]
        self.assertIn("vd.data >= %s", sales_query)
        self.assertIn("vd.data <= %s", sales_query)
        self.assertEqual(sales_params, [start, end, "Loja A"])
        self.assertIn("prd.valid_from <= %s", association_query)
        self.assertIn(
            "prd.valid_to IS NULL OR prd.valid_to >= %s", association_query
        )
        self.assertEqual(association_params, [end, end, start, start])
        self.assertIn("valid_from <= %s", history_query)
        self.assertIn(
            "valid_to IS NULL OR valid_to >= %s", history_query
        )
        self.assertEqual(history_params, [end, start])

    def test_monthly_consumption_forwards_period_without_changing_contract(self):
        start = date(2025, 3, 1)
        end = date(2025, 3, 31)
        with patch(
            "db.doseamento.load_dose_sales_with_rules",
            return_value=([], []),
        ) as loader:
            result = get_consumo_gelado_mensal("Loja A", start, end)

        self.assertTrue(result.empty)
        loader.assert_called_once_with(
            data_inicio=start, data_fim=end, loja="Loja A"
        )
