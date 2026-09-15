import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from db import plano


class FakeCursor:
    def __init__(self, fetchone_values=None, fetchall_value=None):
        self.fetchone_values = list(fetchone_values or [])
        self.fetchall_value = fetchall_value or []
        self.executions = []
        self.rowcount = 1

    def execute(self, query, params=None):
        self.executions.append((query, params))

    def fetchone(self):
        return self.fetchone_values.pop(0)

    def fetchall(self):
        return self.fetchall_value


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        self.commits += 1


def connection_factory(connection):
    @contextmanager
    def _connection():
        yield connection

    return _connection


class TransferenciasB2BTests(unittest.TestCase):
    def test_production_order_requires_external_entity_for_b2b(self):
        with self.assertRaisesRegex(ValueError, "entidade destinatária"):
            plano.criar_ordem_transferencia(
                date(2026, 8, 26),
                "Gelado",
                "Baunilha",
                5,
                destino_tipo="b2b",
                destino_nome=" ",
            )

    def test_production_order_persists_typed_b2b_destination(self):
        cursor = FakeCursor(fetchone_values=[(42,)])
        connection = FakeConnection(cursor)

        with patch.object(plano, "db_connection", connection_factory(connection)):
            order_id = plano.criar_ordem_transferencia(
                date(2026, 8, 26),
                "Gelado",
                "Baunilha",
                5,
                loja_destino="Bolhão",
                destino_tipo="b2b",
                destino_nome="  Sogrape  ",
                criado_por="producao",
            )

        self.assertEqual(order_id, 42)
        insert_params = cursor.executions[0][1]
        self.assertEqual(insert_params[6], "B2B")
        self.assertEqual(insert_params[10], "b2b")
        self.assertEqual(insert_params[11], "Sogrape")
        self.assertEqual(connection.commits, 1)

    def test_store_order_preserves_internal_destination_behaviour(self):
        cursor = FakeCursor(fetchone_values=[(43,)])
        connection = FakeConnection(cursor)

        with patch.object(plano, "db_connection", connection_factory(connection)):
            plano.criar_transferencia_entre_lojas(
                date(2026, 8, 26),
                "Chocolate",
                "Bolhão",
                "Matosinhos",
                2.5,
                "operador",
            )

        order_params = cursor.executions[1][1]
        self.assertEqual(order_params[4], "Matosinhos")
        self.assertEqual(order_params[8], "loja")
        self.assertIsNone(order_params[9])

    def test_store_order_persists_external_entity_without_fake_store_name(self):
        cursor = FakeCursor(fetchone_values=[(44,)])
        connection = FakeConnection(cursor)

        with patch.object(plano, "db_connection", connection_factory(connection)):
            plano.criar_transferencia_entre_lojas(
                date(2026, 8, 26),
                "Chocolate",
                "Bolhão",
                "",
                2.5,
                "operador",
                destino_tipo="b2b",
                destino_nome="Sogrape",
            )

        order_params = cursor.executions[1][1]
        self.assertEqual(order_params[4], "B2B")
        self.assertEqual(order_params[8], "b2b")
        self.assertEqual(order_params[9], "Sogrape")

    def test_b2b_order_cannot_create_internal_store_receipt(self):
        cursor = FakeCursor(
            fetchone_values=[
                ("Gelado", "Baunilha", "Baunilha", 5, "kg", "B2B", "b2b", None)
            ]
        )
        connection = FakeConnection(cursor)

        with patch.object(plano, "db_connection", connection_factory(connection)):
            confirmed = plano.confirmar_ordem_transferencia(45, "operador")

        self.assertFalse(confirmed)
        self.assertEqual(len(cursor.executions), 1)
        self.assertEqual(connection.commits, 0)

    def test_sunday_pastelaria_receipt_uses_count_date_lock(self):
        cursor = FakeCursor(fetchone_values=[
            ("Pastelaria", "Palito antigo", None, 5, "und", "Bolhão", "loja", 17)
        ])
        connection = FakeConnection(cursor)

        with (
            patch.object(plano, "db_connection", connection_factory(connection)),
            patch.object(plano, "date") as mocked_date,
        ):
            mocked_date.today.return_value = date(2026, 9, 6)
            confirmed = plano.confirmar_ordem_transferencia(46, "operador")

        self.assertTrue(confirmed)
        statements = [query for query, _ in cursor.executions]
        lock_index = next(
            index for index, query in enumerate(statements)
            if 'pg_advisory_xact_lock' in query
        )
        count_index = next(
            index for index, query in enumerate(statements)
            if 'INSERT INTO contagem_stock' in query
        )
        self.assertLess(lock_index, count_index)
        self.assertEqual(
            cursor.executions[lock_index][1],
            ('pastelaria-count:2026-09-06',),
        )
        count_params = cursor.executions[count_index][1]
        self.assertEqual(count_params[-2:], ('Pastelaria', 17))
        self.assertIn('produto_pastelaria_id', statements[count_index])
        self.assertNotIn('FROM produtos_pastelaria', statements[count_index])

    def test_order_reader_exposes_destination_type_and_name(self):
        row = (
            46,
            date(2026, 8, 26),
            "Gelado",
            "Baunilha",
            "Baunilha",
            5,
            "kg",
            "B2B",
            "pendente",
            "producao",
            None,
            None,
            None,
            date(2026, 8, 27),
            None,
            "batch",
            None,
            "b2b",
            "Sogrape",
        )
        cursor = FakeCursor(fetchall_value=[row])
        connection = FakeConnection(cursor)

        with patch.object(plano, "db_connection", connection_factory(connection)):
            orders = plano.get_ordens_transferencia()

        self.assertEqual(orders[0]["destino_tipo"], "b2b")
        self.assertEqual(orders[0]["destino_nome"], "Sogrape")


if __name__ == "__main__":
    unittest.main()