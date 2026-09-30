import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

from db.compras_envios import create_dispatch, create_receipt, get_shipment
from db.connection import db_connection
from db.pedidos_urgentes import (
    create_urgent_order,
    get_available_urgent_articles,
    lisboa_today,
)
from db.encomendas_semanais import (
    get_available_weekly_articles,
    get_or_create_weekly_order,
    get_weekly_consolidation,
    save_weekly_draft,
    submit_weekly_order,
)


class TestComprasEnvios(unittest.TestCase):
    actor = f"test-compras-envios-{os.getpid()}"

    @classmethod
    def setUpClass(cls):
        # Remove only records belonging to this test process, including rows
        # left by an interrupted previous run.
        cls._cleanup()
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM stores WHERE is_active = TRUE ORDER BY id"
            )
            cls.store_ids = [row[0] for row in cursor.fetchall()]
        articles = get_available_urgent_articles()
        cls.article = next(
            (article for article in articles
             if article.get("fornecedor_oficial_id") is not None),
            articles[0] if articles else None,
        )
        weekly_articles = get_available_weekly_articles()
        cls.weekly_article = next(
            (article for article in weekly_articles
             if article.get("fornecedor_oficial_id") is not None),
            weekly_articles[0] if weekly_articles else None,
        )
        if not cls.store_ids or not cls.article or not cls.weekly_article:
            raise unittest.SkipTest(
                "É necessária uma loja ativa e artigos ativos semeados."
            )

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    @classmethod
    def _cleanup(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM compras_pedidos_rececoes_linhas
                WHERE rececao_id IN (
                    SELECT id FROM compras_pedidos_rececoes
                    WHERE received_by = %s
                )
                """,
                (cls.actor,),
            )
            cursor.execute(
                "DELETE FROM compras_pedidos_rececoes WHERE received_by = %s",
                (cls.actor,),
            )
            cursor.execute(
                """
                DELETE FROM compras_pedidos_envios_linhas
                WHERE envio_id IN (
                    SELECT id FROM compras_pedidos_envios
                    WHERE created_by = %s
                )
                """,
                (cls.actor,),
            )
            cursor.execute(
                "DELETE FROM compras_pedidos_envios WHERE created_by = %s",
                (cls.actor,),
            )
            cursor.execute(
                "DELETE FROM compras_pedidos_urgentes WHERE submitted_by = %s",
                (cls.actor,),
            )
            # Weekly orders do not carry a direct actor on audit/version rows;
            # identify the disposable orders through their creator.
            cursor.execute(
                """
                DELETE FROM compras_encomendas_semanais_audit
                WHERE encomenda_id IN (
                    SELECT id FROM compras_encomendas_semanais
                    WHERE created_by = %s
                )
                """,
                (cls.actor,),
            )
            cursor.execute(
                """
                DELETE FROM compras_encomendas_semanais_versoes
                WHERE encomenda_id IN (
                    SELECT id FROM compras_encomendas_semanais
                    WHERE created_by = %s
                )
                """,
                (cls.actor,),
            )
            cursor.execute(
                """
                DELETE FROM compras_encomendas_semanais_linhas
                WHERE encomenda_id IN (
                    SELECT id FROM compras_encomendas_semanais
                    WHERE created_by = %s
                )
                """,
                (cls.actor,),
            )
            cursor.execute(
                "DELETE FROM compras_encomendas_semanais WHERE created_by = %s",
                (cls.actor,),
            )
            conn.commit()

    @staticmethod
    def _key():
        return str(uuid4())

    def _order(self, store_id=None, quantity="10", note=None):
        return create_urgent_order(
            store_id=store_id or self.store_ids[0],
            target_date=lisboa_today() + timedelta(days=1),
            lines=[{
                "artigo_id": self.article["id"],
                "quantidade": quantity,
            }],
            actor=self.actor,
            reason="procura_acima_previsto",
            observations=note or f"{self.actor}:{uuid4()}",
            today=lisboa_today(),
        )

    def _dispatch(self, order, quantity="10", request_key=None):
        line = order["linhas"][0]
        return create_dispatch(
            "urgente",
            order["id"],
            [{"pedido_linha_id": line["id"], "quantidade": quantity}],
            self.actor,
            request_key or self._key(),
        )

    def _receipt(
        self, shipment, quantity, request_key=None, store_id=None,
        observations=None,
    ):
        line = get_shipment(shipment["id"])["linhas"][0]
        return create_receipt(
            shipment["id"],
            store_id or self.store_ids[0],
            [{"envio_linha_id": line["id"], "quantidade_recebida": quantity}],
            self.actor,
            request_key or self._key(),
            observations=observations,
        )

    def test_partial_dispatch_totals_and_over_request_are_rejected(self):
        order = self._order()
        first = self._dispatch(order, "4")
        self.assertFalse(first["duplicate"])
        shipment = get_shipment(first["id"])
        self.assertEqual(shipment["quantidade_enviada_total"], 4.0)
        shipment_line = shipment["linhas"][0]
        order_line = order["linhas"][0]
        self.assertEqual(shipment_line["quantidade_pedida_snapshot"], 10.0)
        self.assertEqual(
            shipment_line["origem_nome_snapshot"],
            order_line["origem_nome_snapshot"],
        )
        self.assertEqual(
            shipment_line["fornecedor_oficial_nome_snapshot"],
            order_line["fornecedor_oficial_nome_snapshot"],
        )

        remaining = self._dispatch(order, "6")
        self.assertNotEqual(first["id"], remaining["id"])
        self.assertEqual(
            get_shipment(first["id"])["quantidade_enviada_total"], 4.0
        )
        with self.assertRaises(ValueError):
            self._dispatch(order, "1")

    def test_dispatch_rejects_quantities_that_would_be_rounded(self):
        order = self._order()
        with self.assertRaisesRegex(ValueError, "três casas decimais"):
            self._dispatch(order, "0.0009")

    def test_partial_receipt_totals_and_over_sent_are_rejected(self):
        order = self._order()
        shipment = self._dispatch(order)
        first = self._receipt(shipment, "4")
        self.assertEqual(first["status"], "parcial")
        self.assertEqual(
            get_shipment(shipment["id"])["quantidade_recebida_total"], 4.0
        )

        second = self._receipt(shipment, "6")
        self.assertEqual(second["status"], "confirmada")
        self.assertEqual(
            get_shipment(shipment["id"])["quantidade_recebida_total"], 10.0
        )
        with self.assertRaises(ValueError):
            self._receipt(shipment, "1")

    def test_observations_without_a_discrepancy_do_not_mark_a_problem(self):
        order = self._order()
        shipment = self._dispatch(order)
        result = self._receipt(
            shipment, "10", observations="Receção sem diferença."
        )
        self.assertEqual(result["status"], "confirmada")

    def test_dispatch_and_receipt_keys_are_idempotent_but_payload_is_immutable(self):
        order = self._order()
        dispatch_key = self._key()
        first = self._dispatch(order, "3", dispatch_key)
        duplicate = self._dispatch(order, "3", dispatch_key)
        self.assertEqual(first["id"], duplicate["id"])
        self.assertTrue(duplicate["duplicate"])
        with self.assertRaises(ValueError):
            self._dispatch(order, "2", dispatch_key)

        receipt_key = self._key()
        first_receipt = self._receipt(first, "1", receipt_key)
        duplicate_receipt = self._receipt(first, "1", receipt_key)
        self.assertEqual(first_receipt["id"], duplicate_receipt["id"])
        self.assertTrue(duplicate_receipt["duplicate"])
        with self.assertRaises(ValueError):
            self._receipt(first, "2", receipt_key)

    def test_foreign_line_and_store_boundaries_are_rejected(self):
        first_order = self._order(note="primeira linha")
        second_order = self._order(note="segunda linha")
        first_line = first_order["linhas"][0]
        with self.assertRaises(ValueError):
            create_dispatch(
                "urgente",
                first_order["id"],
                [{
                    "pedido_linha_id": second_order["linhas"][0]["id"],
                    "quantidade": "1",
                }],
                self.actor,
                self._key(),
            )

        shipment = self._dispatch(first_order)
        if len(self.store_ids) < 2:
            self.skipTest("Só existe uma loja ativa semeada para testar isolamento.")
        with self.assertRaises(ValueError):
            create_receipt(
                shipment["id"],
                self.store_ids[1],
                [{
                    "envio_linha_id": get_shipment(shipment["id"])["linhas"][0]["id"],
                    "quantidade_recebida": "1",
                }],
                self.actor,
                self._key(),
            )
        self.assertIsNone(get_shipment(shipment["id"], self.store_ids[1]))
        self.assertEqual(
            get_shipment(shipment["id"], self.store_ids[0])["linhas"][0]["pedido_linha_id"],
            first_line["id"],
        )

    def test_weekly_order_supports_partial_dispatch_and_receipt(self):
        # Choose a Sunday well beyond normal seeded cycles, then submit the
        # order using that Sunday as the service's required submission date.
        future = date.today() + timedelta(days=400)
        sunday = future + timedelta(days=(6 - future.weekday()) % 7)
        order = get_or_create_weekly_order(
            self.store_ids[0], sunday, actor=self.actor
        )
        draft = save_weekly_draft(
            order["id"],
            [{"artigo_id": self.weekly_article["id"], "quantidade": "10"}],
            self.actor,
            observacoes=f"{self.actor}:weekly",
        )
        submitted = submit_weekly_order(order["id"], self.actor, today=sunday)
        self.assertEqual(submitted["status"], "submetida")
        self.assertEqual(len(draft["linhas"]), 1)
        self.assertEqual(submitted["store_id"], self.store_ids[0])
        request_line = submitted["linhas"][0]
        self.assertEqual(
            request_line["fornecedor_oficial_id_snapshot"],
            self.weekly_article.get("fornecedor_oficial_id"),
        )
        self.assertEqual(
            request_line["fornecedor_oficial_nome_snapshot"],
            self.weekly_article.get("fornecedor_oficial_nome"),
        )
        self.assertIsNone(request_line["origem_id_snapshot"])
        self.assertIsNone(request_line["origem_tipo_snapshot"])
        self.assertIsNone(request_line["origem_nome_snapshot"])

        consolidated = get_weekly_consolidation(sunday)
        article_rows = [
            row for row in consolidated
            if row["artigo_id"] == self.weekly_article["id"]
        ]
        self.assertEqual(len(article_rows), 1)
        self.assertEqual(
            article_rows[0]["fornecedor_oficial_id_snapshot"],
            self.weekly_article.get("fornecedor_oficial_id"),
        )

        dispatch = create_dispatch(
            "semanal",
            submitted["id"],
            [{
                "pedido_linha_id": request_line["id"],
                "quantidade": "4",
            }],
            self.actor,
            self._key(),
        )
        shipment = get_shipment(dispatch["id"])
        self.assertEqual(shipment["quantidade_enviada_total"], 4.0)
        self.assertEqual(shipment["store_id"], self.store_ids[0])
        self.assertEqual(
            shipment["linhas"][0]["fornecedor_oficial_id_snapshot"],
            request_line["fornecedor_oficial_id_snapshot"],
        )
        self.assertIsNone(shipment["linhas"][0]["origem_tipo_snapshot"])
        receipt = create_receipt(
            dispatch["id"],
            self.store_ids[0],
            [{
                "envio_linha_id": shipment["linhas"][0]["id"],
                "quantidade_recebida": "2",
            }],
            self.actor,
            self._key(),
        )
        self.assertEqual(receipt["status"], "parcial")
        refreshed = get_shipment(dispatch["id"])
        self.assertEqual(refreshed["quantidade_recebida_total"], 2.0)
        self.assertEqual(refreshed["quantidade_pendente_total"], 2.0)

    def test_concurrent_dispatch_and_receipt_retries_are_idempotent(self):
        order = self._order(quantity="10", note="concorrencia")
        dispatch_key = self._key()
        with ThreadPoolExecutor(max_workers=2) as pool:
            dispatches = list(pool.map(
                lambda _: self._dispatch(order, "4", dispatch_key),
                range(2),
            ))
        self.assertEqual({item["id"] for item in dispatches}, {dispatches[0]["id"]})
        self.assertEqual(
            sorted(item["duplicate"] for item in dispatches),
            [False, True],
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM compras_pedidos_envios
                WHERE id = %s AND created_by = %s
                """,
                (dispatches[0]["id"], self.actor),
            )
            self.assertEqual(cursor.fetchone()[0], 1)

        receipt_key = self._key()
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(
                lambda _: self._receipt(dispatches[0], "2", receipt_key),
                range(2),
            ))
        self.assertEqual({item["id"] for item in receipts}, {receipts[0]["id"]})
        self.assertEqual(
            sorted(item["duplicate"] for item in receipts),
            [False, True],
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM compras_pedidos_rececoes
                WHERE id = %s AND received_by = %s
                """,
                (receipts[0]["id"], self.actor),
            )
            self.assertEqual(cursor.fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()