import hashlib
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from threading import Barrier
from unittest.mock import patch

import psycopg2

import db.confeitaria as confeitaria_counts
import db.pastelaria as pastelaria
import db.schema as schema


class _Cursor:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.current = []
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        self.current = self.responses.pop(0) if self.responses else []

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return self.current


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        self.committed = True


class ConfeitariaCountStorageTests(unittest.TestCase):
    COUNT_DATE = date(2026, 9, 9)
    STORE_ID = 11
    PRODUCT_ID = 70

    @staticmethod
    def _token(count_date, store_id, product_id=None, row_id=None):
        latest = (
            f'{product_id}:{row_id}'
            if product_id is not None and row_id is not None else ''
        )
        return hashlib.md5(
            f'{count_date.isoformat()}:{store_id}:{latest}'.encode('utf-8')
        ).hexdigest()

    def test_grid_uses_stable_ids_and_date_scoped_snapshot(self):
        cursor = _Cursor([
            [],
            [{'id': self.STORE_ID, 'name': 'Matosinhos'}],
            [{'id': self.PRODUCT_ID, 'nome': 'Cookie de amêndoa'}],
            [{
                'id': 50,
                'produto_confeitaria_id': self.PRODUCT_ID,
                'quantidade': 6,
            }],
        ])
        connection = _Connection(cursor)
        with patch(
            'db.confeitaria.db_connection',
            return_value=connection,
        ):
            grid = confeitaria_counts.get_confeitaria_store_count_grid(
                self.COUNT_DATE, self.STORE_ID
            )

        self.assertEqual(grid['store']['id'], self.STORE_ID)
        self.assertEqual(grid['products'][0]['id'], self.PRODUCT_ID)
        self.assertEqual(grid['products'][0]['count'], 6)
        self.assertEqual(grid['completed'], 1)
        self.assertTrue(grid['complete'])
        self.assertEqual(
            grid['snapshot_token'],
            self._token(
                self.COUNT_DATE, self.STORE_ID, self.PRODUCT_ID, 50
            ),
        )
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn("cs.tipo='confeitaria' AND cs.origem='contagem'", statements)
        self.assertIn('cs.store_id=%s', statements)
        self.assertIn('cs.produto_confeitaria_id IS NOT NULL', statements)

    @patch('db.confeitaria.execute_values')
    def test_save_appends_complete_snapshot_with_audit_identity(self, bulk):
        existing = {
            'id': 50,
            'produto_confeitaria_id': self.PRODUCT_ID,
            'quantidade': 6,
        }
        cursor = _Cursor([
            [],
            [{'id': self.STORE_ID, 'name': 'Matosinhos'}],
            [existing],
            [{'id': self.PRODUCT_ID, 'nome': 'Cookie de amêndoa'}],
        ])
        connection = _Connection(cursor)
        token = self._token(
            self.COUNT_DATE, self.STORE_ID, self.PRODUCT_ID, 50
        )
        with (
            patch('db.confeitaria.db_connection', return_value=connection),
            patch('db.confeitaria.uuid4', return_value='batch-test-id'),
        ):
            saved = confeitaria_counts.save_confeitaria_store_counts(
                self.COUNT_DATE,
                self.STORE_ID,
                [(self.PRODUCT_ID, 0)],
                token,
                submitted_by='test-user',
            )

        self.assertEqual(saved, 1)
        self.assertTrue(connection.committed)
        bulk.assert_called_once()
        row = bulk.call_args.args[2][0]
        self.assertEqual(
            row,
            (
                self.COUNT_DATE,
                'Matosinhos',
                'Cookie de amêndoa',
                0,
                'confeitaria',
                'contagem',
                self.PRODUCT_ID,
                self.STORE_ID,
                'batch-test-id',
                'test-user',
            ),
        )
        self.assertIn(
            (f'confeitaria-count:{self.COUNT_DATE.isoformat()}',),
            [
                params for query, params in cursor.queries
                if 'pg_advisory_xact_lock' in query
            ],
        )
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertNotIn('DELETE FROM contagem_stock', statements)

    @patch('db.confeitaria.execute_values')
    def test_stale_snapshot_is_rejected_before_product_read_or_insert(self, bulk):
        current_row = {
            'id': 51,
            'produto_confeitaria_id': self.PRODUCT_ID,
            'quantidade': 6,
        }
        cursor = _Cursor([
            [],
            [{'id': self.STORE_ID, 'name': 'Matosinhos'}],
            [current_row],
        ])
        connection = _Connection(cursor)
        stale_token = self._token(
            self.COUNT_DATE, self.STORE_ID, self.PRODUCT_ID, 50
        )
        with patch('db.confeitaria.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'outro utilizador'):
                confeitaria_counts.save_confeitaria_store_counts(
                    self.COUNT_DATE,
                    self.STORE_ID,
                    [(self.PRODUCT_ID, 7)],
                    stale_token,
                    submitted_by='test-user',
                )

        bulk.assert_not_called()
        self.assertFalse(connection.committed)
        self.assertEqual(len(cursor.queries), 3)

    @patch('db.confeitaria.execute_values')
    def test_partial_active_product_set_is_rejected(self, bulk):
        cursor = _Cursor([
            [],
            [{'id': self.STORE_ID, 'name': 'Matosinhos'}],
            [],
            [
                {'id': self.PRODUCT_ID, 'nome': 'Cookie de amêndoa'},
                {'id': self.PRODUCT_ID + 1, 'nome': 'Tarte de maçã'},
            ],
        ])
        connection = _Connection(cursor)
        with patch('db.confeitaria.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'todas as contagens'):
                confeitaria_counts.save_confeitaria_store_counts(
                    self.COUNT_DATE,
                    self.STORE_ID,
                    [(self.PRODUCT_ID, 3)],
                    self._token(self.COUNT_DATE, self.STORE_ID),
                    submitted_by='test-user',
                )

        bulk.assert_not_called()
        self.assertFalse(connection.committed)

    def test_invalid_and_duplicate_values_are_rejected_without_database_access(self):
        with patch('db.confeitaria.db_connection') as connection:
            with self.assertRaisesRegex(ValueError, 'não negativos'):
                confeitaria_counts.save_confeitaria_store_counts(
                    self.COUNT_DATE,
                    self.STORE_ID,
                    [(self.PRODUCT_ID, -1)],
                    self._token(self.COUNT_DATE, self.STORE_ID),
                    submitted_by='test-user',
                )
            with self.assertRaisesRegex(ValueError, 'repetidos'):
                confeitaria_counts.save_confeitaria_store_counts(
                    self.COUNT_DATE,
                    self.STORE_ID,
                    [(self.PRODUCT_ID, 1), (self.PRODUCT_ID, 2)],
                    self._token(self.COUNT_DATE, self.STORE_ID),
                    submitted_by='test-user',
                )
        connection.assert_not_called()

    def test_submission_history_is_scoped_by_store_and_date(self):
        submission = {
            'submission_id': 'batch-test-id',
            'data': self.COUNT_DATE,
            'store_id': self.STORE_ID,
            'store_name': 'Matosinhos',
            'submitted_by': 'test-user',
            'submitted_at': None,
            'product_count': 2,
        }
        cursor = _Cursor([[submission]])
        with patch(
            'db.confeitaria.db_connection',
            return_value=_Connection(cursor),
        ):
            history = confeitaria_counts.get_confeitaria_count_submission_history(
                self.COUNT_DATE, self.STORE_ID
            )

        self.assertEqual(history, [submission])
        query, params = cursor.queries[0]
        self.assertIn("tipo='confeitaria' AND origem='contagem'", query)
        self.assertIn('submission_id IS NOT NULL', query)
        self.assertIn('store_id=%s AND data=%s', query)
        self.assertEqual(params, (self.STORE_ID, self.COUNT_DATE))

    def test_legacy_writer_cannot_create_text_only_confeitaria_counts(self):
        with (
            patch('db.pastelaria.db_connection') as connection,
            self.assertRaisesRegex(ValueError, 'grelha autorizada'),
        ):
            pastelaria.add_contagem_stock(
                self.COUNT_DATE,
                'Matosinhos',
                'Cookie de amêndoa',
                3,
                'confeitaria',
            )
        connection.assert_not_called()

    def test_confeitaria_count_rows_cannot_be_deleted_from_legacy_history(self):
        cursor = _Cursor([[(self.COUNT_DATE, 'confeitaria')]])
        connection = _Connection(cursor)
        with (
            patch('db.pastelaria.db_connection', return_value=connection),
            self.assertRaisesRegex(ValueError, 'apenas de consulta'),
        ):
            pastelaria.delete_contagem_stock(123)

        self.assertFalse(connection.committed)
        self.assertFalse(any(
            'DELETE FROM contagem_stock' in query
            for query, _ in cursor.queries
        ))

    def test_migration_adds_product_identity_without_backfilling_legacy_rows(self):
        cursor = _Cursor([[(True,)]])
        connection = _Connection(cursor)
        with patch('db.schema.db_connection', return_value=connection):
            schema.run_migrations_confeitaria_count_product_id()

        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertTrue(connection.committed)
        self.assertIn(
            'ADD COLUMN IF NOT EXISTS produto_confeitaria_id INTEGER',
            statements,
        )
        self.assertIn('REFERENCES produtos_confeitaria(id)', statements)
        self.assertIn('ON DELETE RESTRICT', statements)
        self.assertIn('idx_contagem_stock_confeitaria_product', statements)
        self.assertIn('idx_contagem_stock_confeitaria_submission', statements)
        self.assertNotIn('UPDATE contagem_stock', statements)


class ConfeitariaCountConcurrencyPostgresTests(unittest.TestCase):
    """Exercise simultaneous count submissions with independent sessions."""

    COUNT_DATE = date(2026, 9, 9)

    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            raise unittest.SkipTest(
                'DATABASE_URL is required for PostgreSQL integration tests'
            )
        try:
            cls.admin_connection = psycopg2.connect(database_url)
        except Exception as exc:
            raise unittest.SkipTest(
                f'PostgreSQL test database unavailable: {exc}'
            )

        cls.admin_connection.autocommit = True
        cls.schema_name = (
            f'test_confeitaria_count_concurrency_{uuid.uuid4().hex}'
        )
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{cls.schema_name}"')
            cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            cursor.execute("""
                CREATE TABLE stores (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    supports_vendas BOOLEAN NOT NULL DEFAULT TRUE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                );
                CREATE TABLE produtos_confeitaria (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR(255) NOT NULL,
                    ativo BOOLEAN NOT NULL DEFAULT TRUE
                );
                CREATE TABLE contagem_stock (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    loja VARCHAR(50) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    quantidade INTEGER NOT NULL,
                    tipo VARCHAR(50) NOT NULL,
                    origem VARCHAR(30) NOT NULL DEFAULT 'contagem',
                    produto_confeitaria_id INTEGER
                        REFERENCES produtos_confeitaria(id) ON DELETE RESTRICT,
                    store_id INTEGER REFERENCES stores(id) ON DELETE RESTRICT,
                    submission_id UUID,
                    submitted_by VARCHAR(100),
                    submitted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO stores (name) VALUES ('Confeitaria Teste')
                RETURNING id;
            """)
            cls.store_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO produtos_confeitaria (nome)
                VALUES ('Produto Teste')
                RETURNING id
            """)
            cls.product_id = cursor.fetchone()[0]

        @contextmanager
        def isolated_connection():
            connection = psycopg2.connect(database_url)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f'SET search_path TO "{cls.schema_name}"'
                    )
                yield connection
            finally:
                connection.close()

        cls.isolated_connection = staticmethod(isolated_connection)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'admin_connection', None):
            with cls.admin_connection.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{cls.schema_name}" CASCADE'
                )
            cls.admin_connection.close()

    def test_stock_overview_uses_stable_ids_and_does_not_change_counts(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO stores (name, is_active)
                    VALUES ('Loja Ativa Adicional', TRUE)
                    RETURNING id
                """)
                active_store_id = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO stores (name, is_active)
                    VALUES ('Loja Inativa', FALSE)
                    RETURNING id
                """)
                inactive_store_id = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO produtos_confeitaria (nome, ativo)
                    VALUES ('Produto Arquivado', FALSE)
                    RETURNING id
                """)
                inactive_product_id = cursor.fetchone()[0]

                rows = [
                    (
                        date(2026, 9, 20), 'Confeitaria Teste',
                        'Produto Teste', 8, self.product_id, self.store_id,
                    ),
                    (
                        date(2026, 9, 22), 'Confeitaria Teste',
                        'Produto Teste', 5, self.product_id, self.store_id,
                    ),
                    (
                        date(2026, 9, 21), 'Loja Ativa Adicional',
                        'Produto Teste', 4, self.product_id, active_store_id,
                    ),
                    (
                        date(2026, 9, 23), 'Loja Ativa Adicional',
                        'Produto Arquivado', 2, inactive_product_id,
                        active_store_id,
                    ),
                    (
                        date(2026, 9, 24), 'Loja Inativa',
                        'Produto Teste', 3, self.product_id,
                        inactive_store_id,
                    ),
                    (
                        date(2026, 9, 25), 'Confeitaria Teste',
                        'Produto Teste', 99, None, None,
                    ),
                ]
                cursor.executemany("""
                    INSERT INTO contagem_stock (
                        data, loja, produto, quantidade, tipo, origem,
                        produto_confeitaria_id, store_id
                    )
                    VALUES (%s, %s, %s, %s, 'confeitaria', 'contagem', %s, %s)
                """, rows)
                cursor.execute("""
                    INSERT INTO contagem_stock (
                        data, loja, produto, quantidade, tipo, origem,
                        produto_confeitaria_id, store_id
                    )
                    VALUES (
                        %s, 'Confeitaria Teste', 'Produto Teste', 101,
                        'confeitaria', 'importacao', %s, %s
                    )
                """, (
                    date(2026, 9, 26), self.product_id, self.store_id,
                ))
                cursor.execute("SELECT COUNT(*) FROM contagem_stock")
                count_before = cursor.fetchone()[0]
            connection.commit()

        with patch(
            'db.confeitaria.db_connection',
            self.isolated_connection,
        ):
            overview = confeitaria_counts.get_confeitaria_stock_count_overview(
                [self.store_id, active_store_id, inactive_store_id]
            )

        latest = {
            (
                row['produto_confeitaria_id'],
                row['store_id'],
            ): row
            for row in overview['latest_counts']
        }
        self.assertEqual(len(latest), 3)
        self.assertEqual(
            latest[(self.product_id, self.store_id)]['quantidade'], 5
        )
        self.assertEqual(
            latest[(self.product_id, self.store_id)]['data'],
            date(2026, 9, 22),
        )
        self.assertEqual(
            latest[(self.product_id, active_store_id)]['quantidade'], 4
        )
        self.assertEqual(
            latest[(inactive_product_id, active_store_id)]['quantidade'], 2
        )
        self.assertNotIn(
            (self.product_id, inactive_store_id),
            latest,
        )

        legacy = next(
            row for row in overview['history']
            if row['produto_confeitaria_id'] is None
        )
        self.assertEqual(legacy['produto_registado'], 'Produto Teste')
        self.assertIsNone(legacy['produto_atual'])
        inactive_product = next(
            row for row in overview['history']
            if row['produto_confeitaria_id'] == inactive_product_id
        )
        self.assertFalse(inactive_product['produto_ativo'])
        imported = next(
            row for row in overview['history']
            if row['origem'] == 'importacao'
        )
        self.assertEqual(imported['quantidade'], 101)
        self.assertEqual(
            latest[(self.product_id, self.store_id)]['quantidade'], 5
        )
        inactive_store_history = next(
            row for row in overview['history']
            if row['store_id'] == inactive_store_id
        )
        self.assertFalse(inactive_store_history['loja_ativa'])

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM contagem_stock")
                count_after = cursor.fetchone()[0]
        self.assertEqual(count_before, count_after)

    def test_simultaneous_replay_saves_only_one_snapshot(self):
        with patch(
            'db.confeitaria.db_connection',
            self.isolated_connection,
        ):
            grid = confeitaria_counts.get_confeitaria_store_count_grid(
                self.COUNT_DATE, self.store_id
            )

        ready = Barrier(2)

        @contextmanager
        def synchronized_connection():
            with self.isolated_connection() as connection:
                ready.wait(timeout=10)
                yield connection

        def submit(quantity):
            try:
                return confeitaria_counts.save_confeitaria_store_counts(
                    self.COUNT_DATE,
                    self.store_id,
                    [(self.product_id, quantity)],
                    grid['snapshot_token'],
                    submitted_by=f'user-{quantity}',
                )
            except ValueError as exc:
                return str(exc)

        with patch(
            'db.confeitaria.db_connection',
            synchronized_connection,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(submit, (4, 7)))

        self.assertEqual(sum(isinstance(result, int) for result in outcomes), 1)
        self.assertEqual(
            sum(isinstance(result, str) and 'outro utilizador' in result
                for result in outcomes),
            1,
        )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT quantidade, submitted_by
                    FROM contagem_stock
                    WHERE tipo='confeitaria' AND origem='contagem'
                      AND data=%s AND store_id=%s
                """, (self.COUNT_DATE, self.store_id))
                rows = cursor.fetchall()

        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0][0], (4, 7))


if __name__ == '__main__':
    unittest.main()