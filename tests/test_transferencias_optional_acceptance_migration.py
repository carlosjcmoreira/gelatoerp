import os
import unittest
import uuid
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import psycopg2

from db import plano, schema


class OptionalAcceptanceMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise unittest.SkipTest("DATABASE_URL is required")
        cls.database_url = database_url
        cls.admin = psycopg2.connect(database_url)
        cls.admin.autocommit = True
        cls.schema_name = f"test_transfer_optional_{uuid.uuid4().hex}"
        with cls.admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{cls.schema_name}"')
            cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            cursor.execute("""
                CREATE TABLE ordens_transferencia (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    area_origem VARCHAR(100) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    sabor VARCHAR(255),
                    quantidade REAL NOT NULL,
                    unidade VARCHAR(50) NOT NULL DEFAULT 'kg',
                    loja_destino VARCHAR(100) NOT NULL,
                    loja_origem VARCHAR(100),
                    status VARCHAR(50) NOT NULL DEFAULT 'pendente',
                    criado_por VARCHAR(100),
                    confirmado_por VARCHAR(100),
                    confirmado_em TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    data_prevista DATE,
                    batch_id VARCHAR(100),
                    destino_tipo VARCHAR(20) NOT NULL DEFAULT 'loja',
                    destino_nome VARCHAR(255),
                    produto_pastelaria_id INTEGER,
                    motivo_rejeicao TEXT
                );
                CREATE TABLE rececao_mercadoria (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    loja VARCHAR(100) NOT NULL,
                    tipo_produto VARCHAR(100) NOT NULL,
                    produto VARCHAR(255),
                    sabor VARCHAR(255),
                    lote VARCHAR(100),
                    quantidade REAL NOT NULL,
                    unidade VARCHAR(50) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE contagem_stock (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    loja VARCHAR(50) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    quantidade INTEGER NOT NULL,
                    tipo VARCHAR(50) NOT NULL,
                    origem VARCHAR(30) NOT NULL DEFAULT 'contagem',
                    produto_pastelaria_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE transferencias_eventos (
                    id SERIAL PRIMARY KEY,
                    ordem_id INTEGER NOT NULL
                        REFERENCES ordens_transferencia(id) ON DELETE CASCADE,
                    event_type VARCHAR(30) NOT NULL CHECK (
                        event_type IN ('criado', 'confirmado', 'rejeitado')
                    ),
                    utilizador VARCHAR(100),
                    motivo TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

        @contextmanager
        def isolated_connection():
            connection = psycopg2.connect(database_url)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f'SET search_path TO "{cls.schema_name}"')
                yield connection
            finally:
                connection.close()

        cls.isolated_connection = staticmethod(isolated_connection)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "admin", None):
            with cls.admin.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{cls.schema_name}" CASCADE'
                )
            cls.admin.close()

    def setUp(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE transferencias_eventos, rececao_mercadoria, "
                    "contagem_stock, ordens_transferencia RESTART IDENTITY CASCADE"
                )
                cursor.execute("""
                    INSERT INTO ordens_transferencia (
                        data, area_origem, produto, sabor, quantidade,
                        loja_destino, status, criado_por, created_at,
                        destino_tipo
                    ) VALUES (
                        '2026-09-10', 'Gelado', 'Baunilha', 'Baunilha', 3,
                        'Matosinhos', 'pendente', 'producao',
                        '2026-09-10 08:00', 'loja'
                    )
                """)
                cursor.execute("""
                    INSERT INTO ordens_transferencia (
                        data, area_origem, produto, sabor, quantidade,
                        loja_destino, status, criado_por, confirmado_por,
                        confirmado_em, created_at, destino_tipo
                    ) VALUES (
                        '2026-09-09', 'Gelado', 'Chocolate', 'Chocolate', 2,
                        'Bolhão', 'confirmada', 'producao', 'loja',
                        '2026-09-09 09:00', '2026-09-09 08:00', 'loja'
                    )
                """)
                cursor.execute("""
                    INSERT INTO rececao_mercadoria (
                        data, loja, tipo_produto, produto, sabor, lote,
                        quantidade, unidade, created_at
                    ) VALUES (
                        '2026-09-09', 'Bolhão', 'gelado', 'Chocolate',
                        'Chocolate', '', 2, 'kg', '2026-09-09 09:00'
                    )
                """)
                cursor.execute("""
                    INSERT INTO ordens_transferencia (
                        data, area_origem, produto, sabor, quantidade,
                        loja_destino, status, criado_por, created_at,
                        destino_tipo, destino_nome
                    ) VALUES (
                        '2026-09-11', 'Gelado', 'Limão', 'Limão', 4,
                        'B2B', 'pendente', 'producao',
                        '2026-09-11 08:00', 'b2b', 'Cliente'
                    )
                """)
                cursor.execute("""
                    INSERT INTO transferencias_eventos (
                        ordem_id, event_type, utilizador, created_at
                    )
                    SELECT id, 'criado', criado_por, created_at
                    FROM ordens_transferencia
                """)
                connection.commit()

    def _run_acceptance_migration(self):
        with patch("db.schema.db_connection", self.isolated_connection):
            schema.run_migrations_transferencias_aceitacao_opcional()

    def _create_receipt_batch(self, batch_id, areas, destination="Bolhão"):
        order_ids = []
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                for index, area in enumerate(areas, start=1):
                    cursor.execute("""
                        INSERT INTO ordens_transferencia (
                            data, area_origem, produto, quantidade, unidade,
                            loja_destino, status, criado_por, confirmado_por,
                            confirmado_em, data_prevista, batch_id,
                            destino_tipo, rececao_estado
                        )
                        VALUES (
                            '2026-09-14', %s, %s, %s, 'und',
                            %s, 'confirmada', 'origem', 'origem',
                            '2026-09-14 08:00', '2026-09-16', %s,
                            'loja', 'por_verificar'
                        )
                        RETURNING id
                    """, (
                        area,
                        f"{area} item {index}",
                        index,
                        destination,
                        batch_id,
                    ))
                    order_id = cursor.fetchone()[0]
                    order_ids.append(order_id)
                    cursor.execute("""
                        INSERT INTO transferencias_eventos (
                            ordem_id, event_type, utilizador
                        ) VALUES (%s, 'criado', 'origem')
                    """, (order_id,))
                connection.commit()
        return order_ids

    def _seed_physical_count(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO contagem_stock (
                        data, loja, produto, quantidade, tipo, origem
                    )
                    VALUES ('2026-09-14', 'Bolhão', 'Bolo de noz', 7,
                            'confeitaria', 'contagem')
                """)
                connection.commit()

    def _inventory_snapshot(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT data, loja, tipo_produto, produto, sabor, lote,
                           quantidade, unidade, ordem_transferencia_id
                    FROM rececao_mercadoria
                    ORDER BY id
                """)
                receipts = cursor.fetchall()
                cursor.execute("""
                    SELECT data, loja, produto, quantidade, tipo, origem,
                           ordem_transferencia_id
                    FROM contagem_stock
                    ORDER BY id
                """)
                counts = cursor.fetchall()
        return receipts, counts

    def _seed_transfer_order(
        self, order_id, status="confirmada", receipt_state="por_verificar",
        destination_type="loja", store="Matosinhos",
    ):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO ordens_transferencia (
                        id, data, area_origem, produto, sabor, quantidade,
                        unidade, loja_destino, status, criado_por,
                        destino_tipo, rececao_estado
                    ) VALUES (
                        %s, '2026-09-14', 'Gelado', %s, %s, 1,
                        'kg', %s, %s, 'teste', %s, %s
                    )
                """, (
                    order_id, f"Teste {order_id}", f"Teste {order_id}",
                    store, status, destination_type, receipt_state,
                ))
                connection.commit()

    def _restore_legacy_receipt_test_states(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET rececao_estado='aceite'
                    WHERE id=2
                """)
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET rececao_estado='nao_aplicavel'
                    WHERE id=3
                """)
                connection.commit()

    def test_z_legacy_receipt_regularization_is_scoped_audited_and_idempotent(self):
        self._run_acceptance_migration()
        self._restore_legacy_receipt_test_states()
        self._seed_transfer_order(1086)
        inventory_before = self._inventory_snapshot()

        with patch("db.plano.db_connection", self.isolated_connection):
            preview = plano.get_legacy_transfer_receipt_preview()
            self.assertEqual(preview["eligible_count"], 1)
            self.assertEqual(
                preview["eligible_by_store"],
                [{"store": "Matosinhos", "count": 1}],
            )
            self.assertEqual(preview["excluded_count"], 2)

            first = plano.regularize_legacy_transfer_receipts(
                preview["eligible_count"], preview["snapshot"], "gestor-teste"
            )
            replay = plano.regularize_legacy_transfer_receipts(
                preview["eligible_count"], preview["snapshot"], "gestor-teste"
            )

        self.assertEqual(first["updated_count"], 1)
        self.assertFalse(first["stale"])
        self.assertTrue(replay["stale"])
        self.assertEqual(replay["updated_count"], 0)
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT status, rececao_estado, aceite_por,
                           aceite_em IS NOT NULL
                    FROM ordens_transferencia WHERE id=1
                """)
                self.assertEqual(
                    cursor.fetchone(),
                    ("confirmada", "aceite", "gestor-teste", True),
                )
                cursor.execute("""
                    SELECT COUNT(*), MIN(utilizador), MIN(motivo)
                    FROM transferencias_eventos
                    WHERE ordem_id=1 AND event_type='aceite'
                """)
                event_count, actor, reason = cursor.fetchone()
                self.assertEqual(event_count, 1)
                self.assertEqual(actor, "gestor-teste")
                self.assertTrue(reason.startswith("Regularização administrativa:"))
                self.assertIn(
                    "Não constitui confirmação física da receção pela loja.",
                    reason,
                )
                cursor.execute("""
                    SELECT status, rececao_estado
                    FROM ordens_transferencia WHERE id=1086
                """)
                self.assertEqual(
                    cursor.fetchone(), ("confirmada", "por_verificar")
                )
        self.assertEqual(self._inventory_snapshot(), inventory_before)

    def test_z_legacy_receipt_regularization_rejects_changed_snapshot_even_if_count_matches(self):
        self._run_acceptance_migration()
        self._restore_legacy_receipt_test_states()
        self._seed_transfer_order(4)
        self._seed_transfer_order(
            5, status="pendente", receipt_state="por_verificar"
        )
        with patch("db.plano.db_connection", self.isolated_connection):
            preview = plano.get_legacy_transfer_receipt_preview()

        self.assertEqual(preview["eligible_count"], 2)
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET rececao_estado='aceite'
                    WHERE id=1
                """)
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET status='confirmada'
                    WHERE id=5
                """)
                connection.commit()

        with patch("db.plano.db_connection", self.isolated_connection):
            result = plano.regularize_legacy_transfer_receipts(
                preview["eligible_count"], preview["snapshot"], "gestor-teste"
            )

        self.assertTrue(result["stale"])
        self.assertEqual(result["current_count"], 2)
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, rececao_estado
                    FROM ordens_transferencia
                    WHERE id IN (4, 5)
                    ORDER BY id
                """)
                self.assertEqual(
                    cursor.fetchall(),
                    [(4, "por_verificar"), (5, "por_verificar")],
                )
                cursor.execute("""
                    SELECT COUNT(*) FROM transferencias_eventos
                    WHERE event_type='aceite' AND motivo LIKE
                        'Regularização administrativa:%'
                """)
                self.assertEqual(cursor.fetchone()[0], 0)

    def test_z_legacy_receipt_regularization_rolls_back_if_audit_insert_fails(self):
        self._run_acceptance_migration()
        self._restore_legacy_receipt_test_states()
        with patch("db.plano.db_connection", self.isolated_connection):
            preview = plano.get_legacy_transfer_receipt_preview()
            with patch(
                "db.plano._insert_evento",
                side_effect=RuntimeError("audit insert failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "audit insert failed"):
                    plano.regularize_legacy_transfer_receipts(
                        preview["eligible_count"],
                        preview["snapshot"],
                        "gestor-teste",
                    )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT rececao_estado, aceite_por
                    FROM ordens_transferencia WHERE id=1
                """)
                self.assertEqual(cursor.fetchone(), ("por_verificar", None))
                cursor.execute("""
                    SELECT COUNT(*) FROM transferencias_eventos
                    WHERE ordem_id=1 AND event_type='aceite'
                """)
                self.assertEqual(cursor.fetchone()[0], 0)

    def test_migration_is_repeatable_and_never_duplicates_stock(self):
        with patch("db.schema.db_connection", self.isolated_connection):
            schema.run_migrations_transferencias_aceitacao_opcional()
            schema.run_migrations_transferencias_aceitacao_opcional()

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT produto, status, rececao_estado, loja_origem
                    FROM ordens_transferencia ORDER BY id
                """)
                self.assertEqual(cursor.fetchall(), [
                    ("Baunilha", "confirmada", "por_verificar", None),
                    ("Chocolate", "confirmada", "aceite", None),
                    ("Limão", "confirmada", "nao_aplicavel", "Bolhão"),
                ])
                cursor.execute("""
                    SELECT o.produto, COUNT(r.id)
                    FROM ordens_transferencia o
                    LEFT JOIN rececao_mercadoria r
                      ON r.ordem_transferencia_id=o.id
                    GROUP BY o.id, o.produto ORDER BY o.id
                """)
                self.assertEqual(cursor.fetchall(), [
                    ("Baunilha", 1), ("Chocolate", 1), ("Limão", 0)
                ])
                cursor.execute("""
                    SELECT ordem_id, COUNT(*)
                    FROM transferencias_eventos
                    WHERE event_type='executado'
                    GROUP BY ordem_id ORDER BY ordem_id
                """)
                self.assertEqual(cursor.fetchall(), [(1, 1), (3, 1)])

    def test_new_execution_and_acceptance_keep_one_receipt(self):
        with patch("db.schema.db_connection", self.isolated_connection):
            schema.run_migrations_transferencias_aceitacao_opcional()
        with patch("db.plano.db_connection", self.isolated_connection):
            order_id = plano.criar_ordem_transferencia(
                date(2026, 9, 12), "Gelado", "Morango", 2.5,
                loja_destino="Matosinhos", sabor="Morango",
                criado_por="producao",
            )
            self.assertTrue(
                plano.confirmar_ordem_transferencia(order_id, "loja")
            )
            self.assertFalse(
                plano.confirmar_ordem_transferencia(order_id, "loja")
            )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT status, rececao_estado, confirmado_por, aceite_por
                    FROM ordens_transferencia WHERE id=%s
                """, (order_id,))
                self.assertEqual(
                    cursor.fetchone(),
                    ("confirmada", "aceite", "producao", "loja"),
                )
                cursor.execute("""
                    SELECT COUNT(*) FROM rececao_mercadoria
                    WHERE ordem_transferencia_id=%s
                """, (order_id,))
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("""
                    SELECT event_type, COUNT(*)
                    FROM transferencias_eventos
                    WHERE ordem_id=%s
                    GROUP BY event_type ORDER BY event_type
                """, (order_id,))
                self.assertEqual(cursor.fetchall(), [
                    ("aceite", 1), ("criado", 1), ("executado", 1)
                ])

    def test_z_mixed_origin_batches_are_accepted_once_without_stock_changes(self):
        self._run_acceptance_migration()
        self._seed_physical_count()
        confeitaria_ids = self._create_receipt_batch(
            "confeitaria-receipt", ["Confeitaria", "Confeitaria"]
        )
        pastelaria_ids = self._create_receipt_batch(
            "pastelaria-receipt", ["Pastelaria"]
        )
        mixed_ids = self._create_receipt_batch(
            "mixed-receipt", ["Confeitaria", "Pastelaria"]
        )
        all_order_ids = confeitaria_ids + pastelaria_ids + mixed_ids

        inventory_before = self._inventory_snapshot()

        with patch("db.plano.db_connection", self.isolated_connection):
            for batch_ids in (confeitaria_ids, pastelaria_ids, mixed_ids):
                first = plano.confirmar_ordens_transferencia_batch(
                    batch_ids, "Bolhão", "loja"
                )
                replay = plano.confirmar_ordens_transferencia_batch(
                    batch_ids, "Bolhão", "loja"
                )
                self.assertEqual(first['updated_count'], len(batch_ids))
                self.assertFalse(first['replayed'])
                self.assertEqual(replay['updated_count'], 0)
                self.assertTrue(replay['replayed'])

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, status, rececao_estado, aceite_por
                    FROM ordens_transferencia
                    WHERE id = ANY(%s)
                    ORDER BY id
                """, (all_order_ids,))
                self.assertEqual(
                    cursor.fetchall(),
                    [
                        (order_id, "confirmada", "aceite", "loja")
                        for order_id in sorted(all_order_ids)
                    ],
                )
                cursor.execute("""
                    SELECT ordem_id, COUNT(*)
                    FROM transferencias_eventos
                    WHERE ordem_id = ANY(%s) AND event_type = 'aceite'
                    GROUP BY ordem_id ORDER BY ordem_id
                """, (all_order_ids,))
                self.assertEqual(
                    cursor.fetchall(),
                    [(order_id, 1) for order_id in sorted(all_order_ids)],
                )
        self.assertEqual(self._inventory_snapshot(), inventory_before)

    def test_z_invalid_batch_ids_reject_every_order_before_any_receipt_proof(self):
        self._run_acceptance_migration()
        batch_ids = self._create_receipt_batch(
            "one-batch", ["Confeitaria", "Confeitaria"]
        )
        same_batch_foreign_store = self._create_receipt_batch(
            "one-batch", ["Pastelaria"], destination="Matosinhos"
        )
        other_batch = self._create_receipt_batch(
            "another-batch", ["Pastelaria"]
        )

        with patch("db.plano.db_connection", self.isolated_connection):
            invalid_batches = [
                [batch_ids[0]],  # Partial membership is not the pending batch.
                [batch_ids[0], batch_ids[1], batch_ids[0]],  # Duplicate ID.
                [batch_ids[0], same_batch_foreign_store[0]],
                [batch_ids[0], other_batch[0]],
                [batch_ids[0], "not-an-id"],
            ]
            for invalid_ids in invalid_batches:
                with self.subTest(ids=invalid_ids):
                    with self.assertRaises(ValueError):
                        plano.confirmar_ordens_transferencia_batch(
                            invalid_ids, "Bolhão", "loja"
                        )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                all_order_ids = (
                    batch_ids + same_batch_foreign_store + other_batch
                )
                cursor.execute("""
                    SELECT id, rececao_estado
                    FROM ordens_transferencia
                    WHERE id = ANY(%s)
                    ORDER BY id
                """, (all_order_ids,))
                self.assertEqual(
                    cursor.fetchall(),
                    [
                        (order_id, "por_verificar")
                        for order_id in sorted(all_order_ids)
                    ],
                )
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM transferencias_eventos
                    WHERE ordem_id = ANY(%s)
                      AND event_type IN ('aceite', 'problema_reportado')
                """, (all_order_ids,))
                self.assertEqual(cursor.fetchone()[0], 0)

    def test_z_problem_report_batch_is_once_only_and_keeps_inventory_unchanged(self):
        self._run_acceptance_migration()
        self._seed_physical_count()
        order_ids = self._create_receipt_batch(
            "problem-receipt", ["Confeitaria", "Pastelaria"]
        )

        inventory_before = self._inventory_snapshot()

        with patch("db.plano.db_connection", self.isolated_connection):
            first = plano.reportar_problema_ordens_transferencia_batch(
                order_ids, "Bolhão", "loja", "Quantidade incorreta"
            )
            replay = plano.reportar_problema_ordens_transferencia_batch(
                order_ids, "Bolhão", "loja", "Quantidade incorreta"
            )
            self.assertEqual(first['updated_count'], len(order_ids))
            self.assertFalse(first['replayed'])
            self.assertEqual(replay['updated_count'], 0)
            self.assertTrue(replay['replayed'])
            with self.assertRaises(ValueError):
                plano.reportar_problema_ordens_transferencia_batch(
                    order_ids, "Bolhão", "loja", "Outro motivo"
                )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, rececao_estado, motivo_problema, problema_por
                    FROM ordens_transferencia
                    WHERE id = ANY(%s)
                    ORDER BY id
                """, (order_ids,))
                self.assertEqual(
                    cursor.fetchall(),
                    [
                        (order_id, "problema", "Quantidade incorreta", "loja")
                        for order_id in sorted(order_ids)
                    ],
                )
                cursor.execute("""
                    SELECT ordem_id, COUNT(*)
                    FROM transferencias_eventos
                    WHERE ordem_id = ANY(%s)
                      AND event_type = 'problema_reportado'
                    GROUP BY ordem_id ORDER BY ordem_id
                """, (order_ids,))
                self.assertEqual(
                    cursor.fetchall(),
                    [(order_id, 1) for order_id in sorted(order_ids)],
                )
        self.assertEqual(self._inventory_snapshot(), inventory_before)


if __name__ == "__main__":
    unittest.main()