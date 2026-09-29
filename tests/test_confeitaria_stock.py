import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from threading import Barrier
from unittest.mock import patch

import psycopg2

from db import confeitaria_stock, schema


class ConfeitariaStockValidationTests(unittest.TestCase):
    COUNT_DATE = date(2026, 9, 29)
    REQUEST_KEY = str(uuid.uuid4())

    def _movement(self, **overrides):
        payload = {
            'produto_confeitaria_id': 1,
            'tipo': 'saldo_inicial',
            'quantidade': 5,
            'data': self.COUNT_DATE,
            'responsavel': 'gestor-teste',
            'motivo': 'Conferência física inicial',
            'idempotency_key': self.REQUEST_KEY,
        }
        payload.update(overrides)
        return confeitaria_stock.register_confeitaria_stock_movement(
            **payload
        )

    def test_invalid_movement_values_fail_before_database_access(self):
        invalid_cases = (
            ({'tipo': 'saldo_inicial', 'quantidade': -1}, 'não pode ser negativo'),
            ({'tipo': 'correcao', 'quantidade': 0}, 'não pode ser zero'),
            ({'tipo': 'outro', 'quantidade': 1}, 'Tipo de movimento'),
            ({'quantidade': 1.5}, 'número inteiro'),
            ({'responsavel': '  '}, 'responsável'),
            ({'motivo': ''}, 'motivo'),
            ({'idempotency_key': 'não-é-uuid'}, 'chave de submissão'),
            ({'produto_confeitaria_id': True}, 'Produto'),
        )
        with patch('db.confeitaria_stock.db_connection') as connection:
            for overrides, expected_error in invalid_cases:
                with (
                    self.subTest(overrides=overrides),
                    self.assertRaisesRegex(
                        confeitaria_stock.ConfeitariaStockError,
                        expected_error,
                    ),
                ):
                    self._movement(**overrides)
        connection.assert_not_called()


class ConfeitariaStockLedgerPostgresTests(unittest.TestCase):
    """Exercise ledger constraints and concurrency in an isolated schema."""

    MOVEMENT_DATE = date(2026, 9, 29)

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
        cls.schema_name = f'test_confeitaria_stock_{uuid.uuid4().hex}'
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{cls.schema_name}"')
            cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            cursor.execute("""
                CREATE TABLE produtos_confeitaria (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR(255) NOT NULL UNIQUE,
                    ativo BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)
            cursor.execute("""
                CREATE TABLE stock_producao_confeitaria (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    quantidade INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (data, produto)
                )
            """)
            cursor.execute("""
                CREATE TABLE plano_producao_confeitaria (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    producao_estimada INTEGER DEFAULT 0,
                    producao_real INTEGER,
                    no_plano BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (data, produto)
                )
            """)
            cursor.execute("""
                INSERT INTO produtos_confeitaria (nome)
                VALUES ('Produto legado')
                RETURNING id
            """)
            cls.legacy_product_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO stock_producao_confeitaria (data, produto, quantidade)
                VALUES (%s, 'Produto legado', 17)
            """, (cls.MOVEMENT_DATE,))

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
        with patch('db.schema.db_connection', cls.isolated_connection):
            schema.run_migrations_confeitaria_stock_ledger()
            schema.run_migrations_confeitaria_stock_production()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'admin_connection', None):
            with cls.admin_connection.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{cls.schema_name}" CASCADE'
                )
            cls.admin_connection.close()

    def setUp(self):
        self.product_id, self.product_name = self._new_product()

    def _new_product(self):
        name = f'Produto teste {uuid.uuid4().hex[:12]}'
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO produtos_confeitaria (nome)
                    VALUES (%s)
                    RETURNING id
                """, (name,))
                product_id = cursor.fetchone()[0]
            connection.commit()
        return product_id, name

    def _register(
        self, product_id, tipo, quantity, *, key=None, reason='Teste de saldo',
    ):
        with patch(
            'db.confeitaria_stock.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_stock.register_confeitaria_stock_movement(
                produto_confeitaria_id=product_id,
                tipo=tipo,
                quantidade=quantity,
                data=self.MOVEMENT_DATE,
                responsavel='gestor-teste',
                motivo=reason,
                idempotency_key=key or str(uuid.uuid4()),
            )

    def _balance(self, product_id):
        with patch(
            'db.confeitaria_stock.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_stock.get_confeitaria_stock_balance(product_id)

    def _history(self, product_id):
        with patch(
            'db.confeitaria_stock.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_stock.get_confeitaria_stock_movements(
                produto_confeitaria_id=product_id
            )

    def _movement_count(self, product_id):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM confeitaria_stock_movements
                    WHERE produto_confeitaria_id=%s
                """, (product_id,))
                return cursor.fetchone()[0]

    def _create_plan(self, *, production_real=None):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO plano_producao_confeitaria (
                        data, produto, producao_estimada, producao_real,
                        no_plano
                    )
                    VALUES (%s, %s, 5, %s, TRUE)
                """, (
                    self.MOVEMENT_DATE, self.product_name, production_real,
                ))
            connection.commit()

    def _plan_real(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT producao_real
                    FROM plano_producao_confeitaria
                    WHERE data=%s AND produto=%s
                """, (self.MOVEMENT_DATE, self.product_name))
                row = cursor.fetchone()
        return row[0] if row else None

    def _legacy_quantity(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT quantidade
                    FROM stock_producao_confeitaria
                    WHERE data=%s AND produto=%s
                """, (self.MOVEMENT_DATE, self.product_name))
                row = cursor.fetchone()
        return row[0] if row else None

    def _record_production(self, production_real):
        with patch(
            'db.confeitaria_stock.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_stock.record_confeitaria_production_batch(
                data=self.MOVEMENT_DATE,
                production_values={self.product_name: production_real},
                responsavel='gestor-producao',
            )

    def test_migration_is_idempotent_and_does_not_import_legacy_stock(self):
        with patch('db.schema.db_connection', self.isolated_connection):
            schema.run_migrations_confeitaria_stock_ledger()
            schema.run_migrations_confeitaria_stock_ledger()
            schema.run_migrations_confeitaria_stock_production()
            schema.run_migrations_confeitaria_stock_production()

        with patch(
            'db.confeitaria_stock.db_connection',
            self.isolated_connection,
        ):
            balance = confeitaria_stock.get_confeitaria_stock_balance(
                self.legacy_product_id
            )
            options = confeitaria_stock.get_confeitaria_stock_options()

        legacy_product = next(
            row for row in options
            if row['produto_confeitaria_id'] == self.legacy_product_id
        )
        self.assertEqual(balance['saldo'], 0)
        self.assertFalse(balance['saldo_inicial_confirmado'])
        self.assertFalse(balance['transferivel'])
        self.assertEqual(legacy_product['saldo'], 0)

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT quantidade FROM stock_producao_confeitaria '
                    'WHERE produto=%s',
                    ('Produto legado',),
                )
                self.assertEqual(cursor.fetchone()[0], 17)
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM confeitaria_stock_movements
                    WHERE produto_confeitaria_id=%s
                """, (self.legacy_product_id,))
                self.assertEqual(cursor.fetchone()[0], 0)
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM pg_trigger trigger_row
                    JOIN pg_class relation
                      ON relation.oid = trigger_row.tgrelid
                    JOIN pg_namespace namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE trigger_row.tgname =
                              'trg_confeitaria_stock_movement_immutable'
                      AND relation.relname = 'confeitaria_stock_movements'
                      AND namespace.nspname = %s
                      AND NOT trigger_row.tgisinternal
                """, (self.schema_name,))
                self.assertEqual(cursor.fetchone()[0], 1)

    def test_opening_and_correction_derive_balance_by_stable_product_id(self):
        self._register(
            self.product_id,
            'saldo_inicial',
            10,
            reason='Contagem física inicial',
        )
        self._register(
            self.product_id,
            'correcao',
            -2,
            reason='Acerto após conferência',
        )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE produtos_confeitaria
                    SET nome='Produto teste renomeado'
                    WHERE id=%s
                """, (self.product_id,))
            connection.commit()

        balance = self._balance(self.product_id)
        history = self._history(self.product_id)
        self.assertEqual(balance['produto_confeitaria_id'], self.product_id)
        self.assertEqual(balance['produto'], 'Produto teste renomeado')
        self.assertEqual(balance['saldo'], 8)
        self.assertTrue(balance['saldo_inicial_confirmado'])
        self.assertTrue(balance['transferivel'])
        self.assertEqual(len(history), 2)
        self.assertEqual(
            {row['tipo'] for row in history},
            {'saldo_inicial', 'correcao'},
        )
        self.assertEqual(
            {row['produto'] for row in history},
            {self.product_name},
        )
        self.assertEqual(
            {row['responsavel'] for row in history},
            {'gestor-teste'},
        )
        self.assertIn(
            'Acerto após conferência',
            {row['motivo'] for row in history},
        )

    def test_replaying_one_request_does_not_duplicate_a_movement(self):
        request_key = str(uuid.uuid4())
        first = self._register(
            self.product_id,
            'saldo_inicial',
            5,
            key=request_key,
        )
        replay = self._register(
            self.product_id,
            'saldo_inicial',
            5,
            key=request_key,
        )

        self.assertFalse(first['replayed'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(first['movement_id'], replay['movement_id'])
        self.assertEqual(self._movement_count(self.product_id), 1)

        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'já foi usada',
        ):
            self._register(
                self.product_id,
                'saldo_inicial',
                6,
                key=request_key,
            )
        self.assertEqual(self._movement_count(self.product_id), 1)

    def test_unconfirmed_opening_and_overdrawn_corrections_write_nothing(self):
        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'Confirme primeiro',
        ):
            self._register(self.product_id, 'correcao', 2)
        self.assertEqual(self._movement_count(self.product_id), 0)

        self._register(self.product_id, 'saldo_inicial', 3)
        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'deixaria o saldo',
        ):
            self._register(self.product_id, 'correcao', -4)
        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'já foi confirmado',
        ):
            self._register(self.product_id, 'saldo_inicial', 7)

        self.assertEqual(self._movement_count(self.product_id), 1)
        self.assertEqual(self._balance(self.product_id)['saldo'], 3)

    def test_inactive_product_is_not_marked_transferable(self):
        opening_key = str(uuid.uuid4())
        self._register(
            self.product_id,
            'saldo_inicial',
            4,
            key=opening_key,
        )
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE produtos_confeitaria
                    SET ativo=FALSE
                    WHERE id=%s
                """, (self.product_id,))
            connection.commit()

        balance = self._balance(self.product_id)
        self.assertTrue(balance['saldo_inicial_confirmado'])
        self.assertFalse(balance['ativo'])
        self.assertFalse(balance['transferivel'])
        self.assertEqual(len(self._history(self.product_id)), 1)
        replay = self._register(
            self.product_id,
            'saldo_inicial',
            4,
            key=opening_key,
        )
        self.assertTrue(replay['replayed'])
        self.assertEqual(self._movement_count(self.product_id), 1)
        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'está inativo',
        ):
            self._register(self.product_id, 'correcao', 1)
        self.assertEqual(self._movement_count(self.product_id), 1)

    def test_production_credits_only_the_unrecorded_delta_on_repost(self):
        self._register(self.product_id, 'saldo_inicial', 10)
        self._create_plan()

        first = self._record_production(5)
        repost = self._record_production(5)

        self.assertEqual(first[0]['delta'], 5)
        self.assertTrue(first[0]['changed'])
        self.assertEqual(repost[0]['delta'], 0)
        self.assertFalse(repost[0]['changed'])
        self.assertEqual(self._plan_real(), 5)
        self.assertEqual(self._legacy_quantity(), 5)
        self.assertEqual(self._balance(self.product_id)['saldo'], 15)
        self.assertEqual(self._movement_count(self.product_id), 2)

        production = [
            movement for movement in self._history(self.product_id)
            if movement['tipo'] == 'producao'
        ]
        self.assertEqual(len(production), 1)
        self.assertEqual(production[0]['quantidade'], 5)
        self.assertEqual(production[0]['responsavel'], 'gestor-producao')
        self.assertEqual(production[0]['data'], self.MOVEMENT_DATE)
        self.assertIn('de 0 para 5', production[0]['motivo'])

    def test_positive_and_negative_production_corrections_write_only_deltas(self):
        self._register(self.product_id, 'saldo_inicial', 5)
        self._create_plan()

        self.assertEqual(self._record_production(4)[0]['delta'], 4)
        positive_correction = self._record_production(7)[0]
        negative_correction = self._record_production(5)[0]

        self.assertEqual(positive_correction['delta'], 3)
        self.assertEqual(negative_correction['delta'], -2)
        self.assertEqual(self._plan_real(), 5)
        self.assertEqual(self._legacy_quantity(), 5)
        self.assertEqual(self._balance(self.product_id)['saldo'], 10)
        self.assertEqual(
            [
                movement['quantidade']
                for movement in self._history(self.product_id)
                if movement['tipo'] == 'producao'
            ],
            [-2, 3, 4],
        )

    def test_nonzero_production_requires_opening_and_writes_nothing(self):
        self._create_plan()

        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'Confirme primeiro o saldo inicial',
        ):
            self._record_production(4)

        self.assertIsNone(self._plan_real())
        self.assertIsNone(self._legacy_quantity())
        self.assertEqual(self._movement_count(self.product_id), 0)

    def test_zero_real_production_is_recorded_without_a_stock_delta(self):
        self._create_plan()

        result = self._record_production(0)[0]

        self.assertTrue(result['changed'])
        self.assertEqual(result['delta'], 0)
        self.assertIsNone(result['movement_id'])
        self.assertEqual(self._plan_real(), 0)
        self.assertIsNone(self._legacy_quantity())
        self.assertEqual(self._movement_count(self.product_id), 0)

    def test_failure_in_ledger_insert_rolls_back_plan_and_legacy_stock(self):
        self._register(self.product_id, 'saldo_inicial', 10)
        self._create_plan()
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE FUNCTION reject_production_movement_for_test()
                    RETURNS TRIGGER
                    LANGUAGE plpgsql
                    AS $$
                    BEGIN
                        IF NEW.tipo = 'producao' THEN
                            RAISE EXCEPTION 'forced production movement failure';
                        END IF;
                        RETURN NEW;
                    END
                    $$
                """)
                cursor.execute("""
                    CREATE TRIGGER reject_production_movement_for_test
                    BEFORE INSERT ON confeitaria_stock_movements
                    FOR EACH ROW
                    EXECUTE FUNCTION reject_production_movement_for_test()
                """)
            connection.commit()

        try:
            with self.assertRaisesRegex(
                psycopg2.Error,
                'forced production movement failure',
            ):
                self._record_production(4)
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        DROP TRIGGER IF EXISTS
                            reject_production_movement_for_test
                            ON confeitaria_stock_movements
                    """)
                    cursor.execute(
                        'DROP FUNCTION IF EXISTS '
                        'reject_production_movement_for_test()'
                    )
                connection.commit()

        self.assertIsNone(self._plan_real())
        self.assertIsNone(self._legacy_quantity())
        self.assertEqual(self._movement_count(self.product_id), 1)

    def test_concurrent_reposts_credit_production_once(self):
        self._register(self.product_id, 'saldo_inicial', 10)
        self._create_plan()
        barrier = Barrier(2)

        @contextmanager
        def synchronized_connection():
            with self.isolated_connection() as connection:
                barrier.wait(timeout=10)
                yield connection

        with patch(
            'db.confeitaria_stock.db_connection',
            synchronized_connection,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda _index:
                        confeitaria_stock.record_confeitaria_production_batch(
                            data=self.MOVEMENT_DATE,
                            production_values={self.product_name: 6},
                            responsavel='gestor-producao',
                        ),
                    range(2),
                ))

        self.assertEqual(
            sum(result[0]['changed'] for result in results),
            1,
        )
        self.assertEqual(self._plan_real(), 6)
        self.assertEqual(self._legacy_quantity(), 6)
        self.assertEqual(self._balance(self.product_id)['saldo'], 16)
        self.assertEqual(self._movement_count(self.product_id), 2)

    def test_ledger_rows_cannot_be_updated_or_deleted(self):
        movement = self._register(
            self.product_id,
            'saldo_inicial',
            3,
        )
        for statement in (
            'UPDATE confeitaria_stock_movements SET motivo=%s WHERE id=%s',
            'DELETE FROM confeitaria_stock_movements WHERE id=%s',
        ):
            with (
                self.subTest(statement=statement),
                self.isolated_connection() as connection,
                connection.cursor() as cursor,
                self.assertRaises(psycopg2.Error),
            ):
                params = (
                    ('Motivo alterado', movement['movement_id'])
                    if statement.startswith('UPDATE') else
                    (movement['movement_id'],)
                )
                cursor.execute(statement, params)

        self.assertEqual(self._movement_count(self.product_id), 1)

    def test_concurrent_retries_insert_only_one_movement(self):
        request_key = str(uuid.uuid4())
        barrier = Barrier(2)

        @contextmanager
        def synchronized_connection():
            with self.isolated_connection() as connection:
                barrier.wait(timeout=10)
                yield connection

        def submit():
            return confeitaria_stock.register_confeitaria_stock_movement(
                produto_confeitaria_id=self.product_id,
                tipo='saldo_inicial',
                quantidade=5,
                data=self.MOVEMENT_DATE,
                responsavel='gestor-teste',
                motivo='Submissão concorrente',
                idempotency_key=request_key,
            )

        with patch(
            'db.confeitaria_stock.db_connection',
            synchronized_connection,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: submit(), range(2)))

        self.assertEqual(
            sum(result['replayed'] for result in results),
            1,
        )
        self.assertEqual(
            {result['movement_id'] for result in results},
            {results[0]['movement_id']},
        )
        self.assertEqual(self._movement_count(self.product_id), 1)

    def test_concurrent_corrections_cannot_make_balance_negative(self):
        self._register(self.product_id, 'saldo_inicial', 5)
        barrier = Barrier(2)

        @contextmanager
        def synchronized_connection():
            with self.isolated_connection() as connection:
                barrier.wait(timeout=10)
                yield connection

        def submit(_index):
            try:
                return confeitaria_stock.register_confeitaria_stock_movement(
                    produto_confeitaria_id=self.product_id,
                    tipo='correcao',
                    quantidade=-4,
                    data=self.MOVEMENT_DATE,
                    responsavel='gestor-teste',
                    motivo='Acerto concorrente',
                    idempotency_key=str(uuid.uuid4()),
                )
            except confeitaria_stock.ConfeitariaStockError as exc:
                return exc

        with patch(
            'db.confeitaria_stock.db_connection',
            synchronized_connection,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(submit, range(2)))

        successes = [
            result for result in results if isinstance(result, dict)
        ]
        failures = [
            result for result in results
            if isinstance(result, confeitaria_stock.ConfeitariaStockError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self._balance(self.product_id)['saldo'], 1)
        self.assertEqual(self._movement_count(self.product_id), 2)


if __name__ == '__main__':
    unittest.main()