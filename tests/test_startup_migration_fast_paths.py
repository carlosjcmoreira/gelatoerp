import unittest
from unittest.mock import patch

from db import schema


class _Cursor:
    def __init__(self, result=None):
        self.result = result
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.result


class _Connection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class StartupMigrationFastPathTests(unittest.TestCase):
    def test_cost_category_index_fast_path_requires_expected_unique_index(self):
        current_index = (
            True,
            True,
            True,
            "btree",
            None,
            2,
            2,
            "name",
            "COALESCE(parent_id::text, ''::text)",
        )
        cursor = _Cursor(current_index)

        self.assertTrue(schema._cost_categories_name_parent_index_is_current(cursor))
        self.assertEqual(len(cursor.queries), 1)

        stale_index = (*current_index[:3], "btree", "parent_id IS NOT NULL", *current_index[5:])
        self.assertFalse(
            schema._cost_categories_name_parent_index_is_current(_Cursor(stale_index))
        )

    def test_event_history_fast_path_only_accepts_restrictive_foreign_key(self):
        current = _Cursor(
            ("FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE RESTRICT",)
        )
        self.assertTrue(schema._event_history_foreign_key_is_restrictive(current))

        legacy = _Cursor(
            ("FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE",)
        )
        self.assertFalse(schema._event_history_foreign_key_is_restrictive(legacy))

    def test_transfer_origin_backfill_remains_available_for_deferred_startup(self):
        cursor = _Cursor()
        connection = _Connection(cursor)

        with patch("db.schema.db_connection", return_value=connection):
            schema.run_backfill_contagem_stock_transfer_origin()

        statement = cursor.queries[0][0]
        self.assertIn("UPDATE contagem_stock cs", statement)
        self.assertIn("candidate.counts_per_order=1", statement)
        self.assertIn("candidate.orders_per_count=1", statement)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)


if __name__ == "__main__":
    unittest.main()