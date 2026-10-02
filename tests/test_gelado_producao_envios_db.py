"""PostgreSQL transaction tests. Run only from test_clean_database.py."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import os
import unittest
import uuid
from threading import Barrier
from unittest.mock import patch

import psycopg2

from db.connection import db_connection
from db import plano
from db.gelado_producao_envios import guardar_submissao_producao
from db.producao import (
    add_producao,
    delete_producao_by_date_range,
    delete_producao_record,
)


class GeladoProductionDispatchDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('TEST_CLEAN_DATABASE') != '1':
            raise unittest.SkipTest(
                'Execute estes testes apenas através de scripts/test_clean_database.py'
            )

    def setUp(self):
        self.sabor = f'Teste envio {uuid.uuid4().hex[:14]}'
        self.data = date(2097, 1, 1 + (uuid.uuid4().int % 20))
        self.tokens = []
        self.actor = f'teste-{uuid.uuid4().hex[:8]}'
        self.created_stores = []
        with db_connection() as conn:
            cursor = conn.cursor()
            for store_name, store_type in (
                ('Matosinhos', 'producao'),
                ('Bolhão', 'loja'),
                ('Mouzinho', 'loja'),
            ):
                cursor.execute("""
                    INSERT INTO stores (name, store_type, is_active)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (name) DO NOTHING
                    RETURNING name
                """, (store_name, store_type))
                created = cursor.fetchone()
                if created:
                    self.created_stores.append(created[0])
            cursor.execute("""
                SELECT name, COUNT(*)
                FROM stores
                WHERE name IN ('Matosinhos', 'Bolhão')
                GROUP BY name
            """)
            stores = dict(cursor.fetchall())
            self.assertEqual(stores.get('Matosinhos'), 1)
            self.assertEqual(stores.get('Bolhão'), 1)
            cursor.execute("""
                INSERT INTO receitas_gelado
                    (nome, nome_corrente, ativo, conta_eurokg)
                VALUES (%s, %s, TRUE, TRUE)
            """, (self.sabor, self.sabor))
            conn.commit()

    def tearDown(self):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM rececao_mercadoria
                WHERE data=%s AND sabor=%s
            """, (self.data, self.sabor))
            cursor.execute("""
                DELETE FROM ordens_transferencia
                WHERE sabor=%s
            """, (self.sabor,))
            cursor.execute("""
                DELETE FROM producao
                WHERE sabor=%s
            """, (self.sabor,))
            cursor.execute("""
                DELETE FROM stock_producao
                WHERE sabor=%s
            """, (self.sabor,))
            cursor.execute("""
                DELETE FROM plano_producao
                WHERE sabor=%s
            """, (self.sabor,))
            # The audit table is intentionally append-only; the disposable
            # database is dropped by the runner after the complete test suite.
            # Keep weighed stock and its immutable audit evidence in the
            # disposable database; the runner removes both with the database.
            if self.tokens:
                cursor.execute("""
                    DELETE FROM producao_submissoes
                    WHERE submission_id = ANY(%s::uuid[])
                """, (self.tokens,))
            cursor.execute(
                "DELETE FROM receitas_gelado WHERE nome=%s",
                (self.sabor,),
            )
            if self.created_stores:
                cursor.execute(
                    "DELETE FROM stores WHERE name = ANY(%s)",
                    (self.created_stores,),
                )
            conn.commit()

    def _token(self):
        token = str(uuid.uuid4())
        self.tokens.append(token)
        return token

    def _row(self, **overrides):
        row = {
            'sabor': self.sabor,
            'pesagem_mat': Decimal('0'),
            'pesagem_mat_explicit': False,
            'prod_bolhao': Decimal('20'),
            'prod_matosinhos': Decimal('0'),
            'prod_mouzinho': Decimal('0'),
            'prod_b2b': Decimal('0'),
        }
        row.update(overrides)
        return row

    def _save(self, token, rows=None):
        return guardar_submissao_producao(
            token, self.data, rows or [self._row()], 'manual', self.actor,
        )

    def _snapshot(self):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, origem_registo, producao_origem_id, quantidade,
                       loja_origem, loja_destino
                FROM ordens_transferencia
                WHERE data=%s AND sabor=%s
                ORDER BY id
            """, (self.data, self.sabor))
            orders = cursor.fetchall()
            cursor.execute("""
                SELECT id, quantidade, ordem_transferencia_id
                FROM rececao_mercadoria
                WHERE data=%s AND sabor=%s
                ORDER BY id
            """, (self.data, self.sabor))
            receipts = cursor.fetchall()
            cursor.execute("""
                SELECT id, loja, quantidade_kg, tipo
                FROM producao
                WHERE data=%s AND sabor=%s
                ORDER BY id
            """, (self.data, self.sabor))
            production = cursor.fetchall()
            cursor.execute("""
                SELECT loja, SUM(quantidade_kg)
                FROM stock_producao
                WHERE data=%s AND sabor=%s
                GROUP BY loja
            """, (self.data, self.sabor))
            stock = dict(cursor.fetchall())
        return orders, receipts, production, stock

    def test_automatic_and_manual_origins_replay_and_receipt_acceptance(self):
        token = self._token()
        first = self._save(token)
        replay = self._save(token)
        self.assertFalse(first['replayed'])
        self.assertTrue(replay['replayed'])

        manual_id = plano.criar_ordem_transferencia(
            self.data, 'Gelado', self.sabor, Decimal('5'),
            loja_destino='Bolhão', sabor=self.sabor,
            criado_por=self.actor, data_prevista=self.data,
            loja_origem='Matosinhos',
            origem_registo='stock_existente',
        )
        orders, receipts, production, stock = self._snapshot()
        self.assertEqual(len(orders), 2)
        self.assertEqual(sum(Decimal(str(row[3])) for row in orders), Decimal('25'))
        automatic = next(row for row in orders if row[1] == 'producao_dia')
        self.assertIsNotNone(automatic[2])
        self.assertEqual(automatic[3], 20)
        self.assertEqual(automatic[4], 'Matosinhos')
        self.assertEqual(automatic[5], 'Bolhão')
        self.assertEqual(
            next(row for row in orders if row[0] == manual_id)[1],
            'stock_existente',
        )
        self.assertEqual(
            next(row for row in orders if row[0] == manual_id)[4],
            'Matosinhos',
        )
        self.assertEqual(len(receipts), 2)
        self.assertEqual(len(production), 1)
        self.assertEqual(production[0][3], 'manual')
        self.assertEqual(stock, {})

        plano.confirmar_ordens_transferencia_batch(
            [automatic[0]], 'Bolhão', 'rececao-teste',
        )
        after_acceptance = self._snapshot()
        self.assertEqual(len(after_acceptance[1]), 2)
        self.assertEqual(
            sum(Decimal(str(row[1])) for row in after_acceptance[1]),
            Decimal('25'),
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT origem_registo, producao_origem_id
                FROM ordens_transferencia
                WHERE id=%s
            """, (automatic[0],))
            self.assertEqual(cursor.fetchone(), ('producao_dia', automatic[2]))
        history = plano.get_ordens_transferencia_with_events(
            origem_registo='producao_dia',
            data_inicio=self.data,
            data_fim=self.data,
        )
        self.assertEqual(
            [order['id'] for order in history['ordens']],
            [automatic[0]],
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM transferencias WHERE data=%s AND sabor=%s",
                (self.data, self.sabor),
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_two_distinct_equal_batches_create_two_transfers(self):
        self._save(self._token())
        self._save(self._token())
        orders, receipts, production, stock = self._snapshot()
        self.assertEqual(len(orders), 2)
        self.assertEqual(len(receipts), 2)
        self.assertEqual(len(production), 2)
        self.assertEqual(len({row[2] for row in orders}), 2)
        self.assertEqual(stock, {})

    def test_changed_content_cannot_reuse_a_submission_identity(self):
        token = self._token()
        self._save(token)
        with self.assertRaisesRegex(ValueError, 'outros valores'):
            self._save(token, [self._row(prod_bolhao=Decimal('21'))])
        orders, receipts, production, _stock = self._snapshot()
        self.assertEqual((len(orders), len(receipts), len(production)), (1, 1, 1))

    def test_explicit_weighing_effect_is_replayed_without_a_second_audit(self):
        token = self._token()
        row = self._row(
            pesagem_mat=Decimal('18.25'),
            pesagem_mat_explicit=True,
            prod_bolhao=Decimal('0'),
        )
        self._save(token, [row])
        replay = self._save(token, [row])
        self.assertTrue(replay['replayed'])
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*), MAX(quantidade_kg)
                FROM stock_gelado
                WHERE data=%s AND loja='Matosinhos' AND sabor=%s
                  AND tipo='inicio' AND is_active=TRUE
            """, (self.data, self.sabor))
            self.assertEqual(cursor.fetchone(), (1, Decimal('18.2500')))
            cursor.execute("""
                SELECT COUNT(*) FROM pesagem_audit_events
                WHERE event_date=%s AND actor_username=%s
            """, (self.data, self.actor))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_concurrent_replay_creates_only_one_order_and_receipt(self):
        token = self._token()
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=10)
            return self._save(token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: submit(), range(2)))
        self.assertEqual(sum(not result['replayed'] for result in results), 1)
        self.assertEqual(sum(result['replayed'] for result in results), 1)
        orders, receipts, production, _stock = self._snapshot()
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(production), 1)

    def test_failure_after_order_creation_rolls_back_the_complete_submission(self):
        token = self._token()
        from db import gelado_producao_envios

        create_order = gelado_producao_envios.criar_ordem_transferencia_cursor

        def fail_after_write(*args, **kwargs):
            create_order(*args, **kwargs)
            raise RuntimeError('injected receipt-stage failure')

        with patch(
            'db.gelado_producao_envios.criar_ordem_transferencia_cursor',
            side_effect=fail_after_write,
        ), self.assertRaisesRegex(RuntimeError, 'injected'):
            self._save(token)
        orders, receipts, production, stock = self._snapshot()
        self.assertEqual((orders, receipts, production, stock), ([], [], [], {}))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM producao_submissoes WHERE submission_id=%s",
                (token,),
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_existing_bolhao_stock_is_not_consumed_or_increased(self):
        token = self._token()
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO stock_producao (data, sabor, loja, quantidade_kg)
                VALUES (%s, %s, 'Bolhão', 7)
            """, (self.data, self.sabor))
            conn.commit()
        self._save(token)
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT quantidade_kg FROM stock_producao
                WHERE data=%s AND sabor=%s AND loja='Bolhão'
            """, (self.data, self.sabor))
            self.assertEqual(Decimal(str(cursor.fetchone()[0])), Decimal('7.0000'))

    def test_other_destinations_zero_and_generic_writers_do_not_auto_dispatch(self):
        token = self._token()
        result = self._save(token, [self._row(
            prod_bolhao=Decimal('0'),
            prod_matosinhos=Decimal('4'),
            prod_mouzinho=Decimal('3'),
            prod_b2b=Decimal('2'),
        )])
        self.assertEqual(result['automatic_orders'], [])
        orders, receipts, production, stock = self._snapshot()
        self.assertEqual(orders, [])
        self.assertEqual(receipts, [])
        self.assertEqual(len(production), 3)
        self.assertEqual(Decimal(str(stock['Matosinhos'])), Decimal('4.0000'))
        self.assertEqual(Decimal(str(stock['Mouzinho'])), Decimal('3.0000'))
        self.assertEqual(Decimal(str(stock['B2B'])), Decimal('2.0000'))

        no_effect_date = date(2098, 2, 2)
        plano.upsert_plano_producao(
            no_effect_date, self.sabor, 0, 8, 0,
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM ordens_transferencia
                WHERE data=%s AND sabor=%s
            """, (no_effect_date, self.sabor))
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_zero_historical_and_generic_production_do_not_create_orders(self):
        zero = self._row(
            prod_bolhao=Decimal('0'),
            prod_matosinhos=Decimal('0'),
            prod_mouzinho=Decimal('0'),
            prod_b2b=Decimal('0'),
        )
        result = self._save(self._token(), [zero])
        self.assertEqual(result['saved'], 0)

        add_producao(
            date(2000, 1, 1), 'Matosinhos', 2, tipo='balança', sabor=self.sabor,
        )
        add_producao(
            date(2098, 2, 3), 'Matosinhos', 3, tipo='manual', sabor=self.sabor,
        )
        plano.upsert_plano_producao(
            date(2098, 2, 4), self.sabor, 0, 5, 0,
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM ordens_transferencia WHERE sabor=%s",
                (self.sabor,),
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_linked_production_cannot_be_updated_or_deleted(self):
        self._save(self._token())
        _orders, _receipts, production, _stock = self._snapshot()
        production_id = production[0][0]
        with db_connection() as conn:
            cursor = conn.cursor()
            with self.assertRaises(psycopg2.IntegrityError):
                cursor.execute(
                    "UPDATE producao SET quantidade_kg=21 WHERE id=%s",
                    (production_id,),
                )
            conn.rollback()
        with db_connection() as conn:
            cursor = conn.cursor()
            with self.assertRaises(psycopg2.IntegrityError):
                cursor.execute("DELETE FROM producao WHERE id=%s", (production_id,))
            conn.rollback()
        with self.assertRaisesRegex(ValueError, 'ligado a um envio executado'):
            delete_producao_record(production_id)
        with self.assertRaisesRegex(ValueError, 'ligados a envios executados'):
            delete_producao_by_date_range(self.data, self.data, 'Bolhão')
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT quantidade_kg FROM producao WHERE id=%s",
                (production_id,),
            )
            self.assertEqual(cursor.fetchone()[0], 20)

    def test_unlinked_production_updates_still_return_new_values(self):
        add_producao(
            self.data, 'Matosinhos', 2, tipo='manual', sabor=self.sabor,
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE producao SET quantidade_kg=3
                WHERE data=%s AND loja='Matosinhos' AND sabor=%s
                RETURNING quantidade_kg
            """, (self.data, self.sabor))
            self.assertEqual(cursor.fetchone()[0], 3)
            conn.commit()


if __name__ == '__main__':
    unittest.main()