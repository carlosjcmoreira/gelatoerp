import os
import unittest
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from unittest.mock import patch

import psycopg2

from db import confeitaria_stock, confeitaria_transfers, schema


class ConfeitariaTransferPostgresTests(unittest.TestCase):
    """Exercise transfer cutover, atomic batches, and reversals in isolation."""

    TODAY = date(2026, 9, 29)
    DELIVERY_DATE = date(2026, 10, 2)

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
        cls.schema_name = f'test_confeitaria_transfer_{uuid.uuid4().hex}'
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
                CREATE TABLE stores (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    store_type VARCHAR(20) NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)
            cursor.execute("""
                INSERT INTO stores (name, store_type, is_active)
                VALUES ('Bolhão', 'loja', TRUE)
            """)
            cursor.execute("""
                CREATE TABLE ordens_transferencia (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    area_origem VARCHAR(100) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    sabor VARCHAR(255),
                    quantidade REAL NOT NULL,
                    unidade VARCHAR(50) NOT NULL DEFAULT 'kg',
                    loja_destino VARCHAR(100) NOT NULL DEFAULT 'Bolhão',
                    status VARCHAR(50) NOT NULL DEFAULT 'pendente',
                    criado_por VARCHAR(100),
                    confirmado_por VARCHAR(100),
                    confirmado_em TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    destino_tipo VARCHAR(20) NOT NULL DEFAULT 'loja',
                    destino_nome VARCHAR(255),
                    data_prevista DATE,
                    batch_id VARCHAR(100),
                    rececao_estado VARCHAR(30),
                    loja_origem VARCHAR(100),
                    motivo_rejeicao TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE transferencias_eventos (
                    id SERIAL PRIMARY KEY,
                    ordem_id INTEGER NOT NULL
                        REFERENCES ordens_transferencia(id),
                    event_type VARCHAR(40) NOT NULL,
                    utilizador VARCHAR(100),
                    motivo TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE contagem_stock (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    loja VARCHAR(100) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    quantidade INTEGER NOT NULL,
                    tipo VARCHAR(30) NOT NULL,
                    origem VARCHAR(30) NOT NULL DEFAULT 'contagem',
                    produto_pastelaria_id INTEGER,
                    ordem_transferencia_id INTEGER
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX uq_test_receipt_per_order
                ON contagem_stock(ordem_transferencia_id)
                WHERE ordem_transferencia_id IS NOT NULL
            """)

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
            schema.run_migrations_confeitaria_stock_transfers()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'admin_connection', None):
            with cls.admin_connection.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{cls.schema_name}" CASCADE'
                )
            cls.admin_connection.close()

    def setUp(self):
        self.product_ids = [
            self._new_product('Produto A'),
            self._new_product('Produto B'),
        ]

    def _new_product(self, prefix):
        name = f'{prefix} {uuid.uuid4().hex[:10]}'
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO produtos_confeitaria (nome, ativo)
                    VALUES (%s, TRUE)
                    RETURNING id
                """, (name,))
                product_id = cursor.fetchone()[0]
            connection.commit()
        return product_id

    def _set_active(self, product_id, active):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'UPDATE produtos_confeitaria SET ativo=%s WHERE id=%s',
                    (active, product_id),
                )
            connection.commit()

    def _product_name(self, product_id):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT nome FROM produtos_confeitaria WHERE id=%s',
                    (product_id,),
                )
                return cursor.fetchone()[0]

    def _register_opening(self, product_id, quantity):
        with patch(
            'db.confeitaria_stock.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_stock.register_confeitaria_stock_movement(
                produto_confeitaria_id=product_id,
                tipo='saldo_inicial',
                quantidade=quantity,
                data=self.TODAY,
                responsavel='gestor-teste',
                motivo='Contagem inicial de teste',
                idempotency_key=str(uuid.uuid4()),
            )

    def _reconcile(self, product_id, target, *, expected=None):
        if expected is None:
            expected = self._balance(product_id)['saldo']
        with patch(
            'db.confeitaria_transfers.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_transfers.reconciliar_confeitaria_stock_corte(
                lines=[{
                    'produto_confeitaria_id': product_id,
                    'saldo_confirmado': target,
                    'saldo_esperado': expected,
                }],
                data=self.TODAY,
                responsavel='gestor-corte',
                confirmacao_explicita=True,
            )

    def _make_transferable(self, product_id, quantity):
        self._register_opening(product_id, quantity)
        self._reconcile(product_id, quantity)

    def _balance(self, product_id):
        with patch(
            'db.confeitaria_stock.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_stock.get_confeitaria_stock_balance(product_id)

    def _transfer(
        self, lines, *, request_key=None, destination='Bolhão',
        delivery_date=None, movement_date=None,
    ):
        with patch(
            'db.confeitaria_transfers.db_connection',
            self.isolated_connection,
        ):
            return confeitaria_transfers.criar_ordens_transferencia_confeitaria(
                data=movement_date or self.TODAY,
                loja_destino=destination,
                lines=lines,
                criado_por='operador-teste',
                data_prevista=delivery_date or self.DELIVERY_DATE,
                request_key=request_key or str(uuid.uuid4()),
            )

    def _count(self, table, where_sql='', params=()):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT COUNT(*) FROM {table} {where_sql}', params
                )
                return cursor.fetchone()[0]

    def test_cutover_uses_a_manual_current_count_and_requires_fresh_snapshot(self):
        product_id = self.product_ids[0]
        self._register_opening(product_id, 11)

        with patch(
            'db.confeitaria_transfers.db_connection',
            self.isolated_connection,
        ):
            with self.assertRaisesRegex(
                confeitaria_stock.ConfeitariaStockError,
                'mudou desde a abertura',
            ):
                confeitaria_transfers.reconciliar_confeitaria_stock_corte(
                    lines=[{
                        'produto_confeitaria_id': product_id,
                        'saldo_confirmado': 6,
                        'saldo_esperado': 10,
                    }],
                    data=self.TODAY,
                    responsavel='gestor-corte',
                    confirmacao_explicita=True,
                )

        self.assertEqual(self._balance(product_id)['saldo'], 11)
        result = self._reconcile(product_id, 6, expected=11)
        self.assertEqual(result[0]['saldo'], 6)
        balance = self._balance(product_id)
        self.assertEqual(balance['saldo'], 6)
        self.assertTrue(balance['transferencias_reconciliadas'])
        self.assertTrue(balance['transferivel'])

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT tipo, quantidade, motivo
                    FROM confeitaria_stock_movements
                    WHERE produto_confeitaria_id=%s
                    ORDER BY id DESC
                    LIMIT 1
                """, (product_id,))
                movement = cursor.fetchone()
        self.assertEqual(movement[0], 'reconciliacao_corte')
        self.assertEqual(movement[1], -5)
        self.assertIn('transferências legadas', movement[2])

    def test_batch_debits_all_lines_once_and_rejects_key_reuse(self):
        product_a, product_b = self.product_ids
        self._make_transferable(product_a, 10)
        self._make_transferable(product_b, 7)
        request_key = str(uuid.uuid4())
        lines = [
            {'produto_confeitaria_id': product_b, 'quantidade': 3},
            {'produto_confeitaria_id': product_a, 'quantidade': 4},
        ]

        result = self._transfer(lines, request_key=request_key)
        self.assertFalse(result['replayed'])
        self.assertEqual(len(result['order_ids']), 2)
        self.assertEqual(self._balance(product_a)['saldo'], 6)
        self.assertEqual(self._balance(product_b)['saldo'], 4)
        self.assertEqual(
            self._count(
                'ordens_transferencia',
                "WHERE area_origem='Confeitaria' AND batch_id=%s",
                (request_key,),
            ),
            2,
        )
        self.assertEqual(
            self._count(
                'confeitaria_stock_movements',
                "WHERE tipo='transferencia_saida' "
                "AND ordem_transferencia_id = ANY(%s)",
                (result['order_ids'],),
            ),
            2,
        )
        self.assertEqual(
            self._count(
                'contagem_stock',
                'WHERE ordem_transferencia_id = ANY(%s)',
                (result['order_ids'],),
            ),
            2,
        )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(DISTINCT batch_id),
                           COUNT(DISTINCT produto_confeitaria_id)
                    FROM ordens_transferencia
                    WHERE batch_id=%s AND area_origem='Confeitaria'
                """, (request_key,))
                self.assertEqual(cursor.fetchone(), (1, 2))

        replay = self._transfer(
            lines,
            request_key=request_key,
            movement_date=date(2026, 9, 30),
        )
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['order_ids'], result['order_ids'])
        self.assertEqual(
            self._count(
                'confeitaria_stock_movements',
                "WHERE tipo='transferencia_saida' "
                "AND ordem_transferencia_id = ANY(%s)",
                (result['order_ids'],),
            ),
            2,
        )
        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'utilizado com outros dados',
        ):
            self._transfer(
                [{'produto_confeitaria_id': product_a, 'quantidade': 5}],
                request_key=request_key,
            )

    def test_invalid_products_destinations_and_insufficient_batch_are_rejected(self):
        product_a, product_b = self.product_ids
        self._make_transferable(product_a, 8)
        self._make_transferable(product_b, 2)
        before_orders = self._count('ordens_transferencia')
        before_movements = self._count('confeitaria_stock_movements')

        cases = [
            (
                [{'produto_confeitaria_id': 999999, 'quantidade': 1}],
                'inválidos',
            ),
            (
                [{'produto_confeitaria_id': product_a, 'quantidade': 1.5}],
                'números inteiros',
            ),
            (
                [{'produto_confeitaria_id': product_a, 'quantidade': -1}],
                'superior a zero',
            ),
            (
                [
                    {'produto_confeitaria_id': product_a, 'quantidade': 1},
                    {'produto_confeitaria_id': product_a, 'quantidade': 1},
                ],
                'mais de uma vez',
            ),
        ]
        self._set_active(product_b, False)
        cases.append((
            [{'produto_confeitaria_id': product_b, 'quantidade': 1}],
            'inativo',
        ))
        for lines, message in cases:
            with self.subTest(lines=lines):
                with self.assertRaisesRegex(
                    confeitaria_stock.ConfeitariaStockError, message
                ):
                    self._transfer(lines)

        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError, 'Loja de destino'
        ):
            self._transfer(
                [{'produto_confeitaria_id': product_a, 'quantidade': 1}],
                destination='Loja arquivada',
            )

        self._set_active(product_b, True)
        with self.assertRaisesRegex(
            confeitaria_stock.ConfeitariaStockError,
            'Nenhuma ordem foi criada',
        ):
            self._transfer([
                {'produto_confeitaria_id': product_a, 'quantidade': 1},
                {'produto_confeitaria_id': product_b, 'quantidade': 3},
            ])
        self.assertEqual(self._count('ordens_transferencia'), before_orders)
        self.assertEqual(
            self._count('confeitaria_stock_movements'),
            before_movements,
        )

    def test_receipt_failure_rolls_back_request_orders_and_debits(self):
        product_a, product_b = self.product_ids
        self._make_transferable(product_a, 8)
        self._make_transferable(product_b, 8)
        request_key = str(uuid.uuid4())
        with (
            patch('db.confeitaria_transfers.db_connection', self.isolated_connection),
            patch(
                'db.plano._insert_transfer_receipt',
                side_effect=RuntimeError('receipt insert failed'),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, 'receipt insert failed'):
                confeitaria_transfers.criar_ordens_transferencia_confeitaria(
                    data=self.TODAY,
                    loja_destino='Bolhão',
                    lines=[
                        {'produto_confeitaria_id': product_a, 'quantidade': 2},
                        {'produto_confeitaria_id': product_b, 'quantidade': 2},
                    ],
                    criado_por='operador-teste',
                    data_prevista=self.DELIVERY_DATE,
                    request_key=request_key,
                )

        self.assertEqual(self._balance(product_a)['saldo'], 8)
        self.assertEqual(self._balance(product_b)['saldo'], 8)
        self.assertEqual(
            self._count(
                'ordens_transferencia',
                "WHERE area_origem='Confeitaria' AND batch_id=%s",
                (request_key,),
            ),
            0,
        )
        self.assertEqual(
            self._count(
                'confeitaria_stock_transfer_requests',
                'WHERE request_key=%s',
                (request_key,),
            ),
            0,
        )
        self.assertEqual(
            self._count(
                'confeitaria_stock_movements',
                "WHERE tipo='transferencia_saida' "
                "AND produto_confeitaria_id = ANY(%s)",
                ([product_a, product_b],),
            ),
            0,
        )

    def test_cancellation_restores_stock_at_most_once(self):
        product_id = self.product_ids[0]
        self._make_transferable(product_id, 9)
        result = self._transfer([{
            'produto_confeitaria_id': product_id,
            'quantidade': 4,
        }])
        order_id = result['order_ids'][0]
        self.assertEqual(self._balance(product_id)['saldo'], 5)

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET status='cancelada', confirmado_por='gestor',
                        motivo_rejeicao='Cancelamento elegível'
                    WHERE id=%s
                """, (order_id,))
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET status='confirmada'
                    WHERE id=%s
                """, (order_id,))
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET status='cancelada'
                    WHERE id=%s
                """, (order_id,))
            connection.commit()

        self.assertEqual(self._balance(product_id)['saldo'], 9)
        self.assertEqual(
            self._count(
                'confeitaria_stock_movements',
                "WHERE tipo='transferencia_anulacao' "
                "AND ordem_transferencia_id=%s",
                (order_id,),
            ),
            1,
        )
        self.assertEqual(
            self._count(
                'transferencias_eventos',
                "WHERE ordem_id=%s AND event_type='anulado'",
                (order_id,),
            ),
            1,
        )

    def test_competing_batches_cannot_overdraw_the_same_balance(self):
        product_id = self.product_ids[0]
        self._make_transferable(product_id, 5)
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=10)
            try:
                return self._transfer([{
                    'produto_confeitaria_id': product_id,
                    'quantidade': 4,
                }])
            except confeitaria_stock.ConfeitariaStockError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: submit(), range(2)))

        successes = [
            result for result in results if isinstance(result, dict)
        ]
        failures = [
            result for result in results
            if isinstance(result, confeitaria_stock.ConfeitariaStockError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self._balance(product_id)['saldo'], 1)
        self.assertEqual(
            self._count(
                'confeitaria_stock_movements',
                "WHERE tipo='transferencia_saida' "
                "AND produto_confeitaria_id=%s",
                (product_id,),
            ),
            1,
        )


if __name__ == '__main__':
    unittest.main()