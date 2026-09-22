"""Tests for safe classification of purchasing spreadsheet origins."""

import unittest
from unittest.mock import MagicMock, patch


class TestComprasOriginClassification(unittest.TestCase):
    def test_matosinhos_is_an_internal_store_origin(self):
        from db.artigos import classify_compras_origin_label

        result = classify_compras_origin_label(" MATOSINHOS ")

        self.assertEqual(result["key"], "centro:matosinhos")
        self.assertEqual(result["tipo"], "centro_interno")
        self.assertEqual(result["store_name"], "Matosinhos")
        self.assertIsNone(result.get("supplier_id"))

    def test_moedas_is_an_operational_category_routed_by_matosinhos(self):
        from db.artigos import classify_compras_origin_label

        result = classify_compras_origin_label("Moedas")

        self.assertEqual(result["key"], "categoria:moedas")
        self.assertEqual(result["tipo"], "categoria_operacional")
        self.assertEqual(result["store_name"], "Matosinhos")
        self.assertIsNone(result.get("supplier_id"))

    def test_grafica_is_not_created_as_a_supplier(self):
        from db.artigos import classify_compras_origin_label

        result = classify_compras_origin_label("GRÁFICA")

        self.assertEqual(result["key"], "categoria:grafica")
        self.assertEqual(result["tipo"], "categoria_operacional")
        self.assertIsNone(result["store_name"])
        self.assertIsNone(result.get("supplier_id"))

    def test_unknown_label_stays_unresolved_with_stable_key(self):
        from db.artigos import classify_compras_origin_label

        first = classify_compras_origin_label("Fornecedor Novo")
        second = classify_compras_origin_label(" fornecedor   novo ")

        self.assertEqual(first["key"], second["key"])
        self.assertEqual(first["tipo"], second["tipo"])
        self.assertEqual(first["nome"], "Fornecedor Novo")
        self.assertEqual(second["rotulo_original"], "fornecedor novo")
        self.assertEqual(first["tipo"], "por_resolver")
        self.assertTrue(first["key"].startswith("por-resolver:"))
        self.assertNotIn("supplier_id", first)


class TestComprasOriginMigration(unittest.TestCase):
    def test_migration_creates_typed_origin_schema_and_uses_store_id(self):
        from db.schema import run_migrations_compras_origens

        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.__enter__ = lambda instance: instance
        conn.__exit__ = MagicMock(return_value=False)

        with patch("db.schema.db_connection", return_value=conn):
            run_migrations_compras_origens()

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        origin_table = next(sql for sql in statements if "CREATE TABLE IF NOT EXISTS compras_origens" in sql)
        self.assertIn("supplier_id INTEGER REFERENCES suppliers(id)", origin_table)
        self.assertIn("store_id INTEGER REFERENCES stores(id)", origin_table)
        self.assertIn("'centro_interno'", origin_table)
        self.assertIn("'categoria_operacional'", origin_table)
        self.assertIn("SELECT 'centro:matosinhos'", next(
            sql for sql in statements if "INSERT INTO compras_origens" in sql
        ))
        self.assertTrue(any("ALTER TABLE artigos_administrativos" in sql for sql in statements))
        conn.commit.assert_called_once()