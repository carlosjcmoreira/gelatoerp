import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from db.connection import db_connection
from db.pedidos_urgentes import (
    create_urgent_order,
    get_available_urgent_articles,
    get_urgent_order,
    get_urgent_order_audit,
    list_urgent_orders,
    lisboa_today,
    normalise_reason,
    normalise_target_date,
    transition_urgent_order_status,
)


class TestPedidosUrgentes(unittest.TestCase):
    actor = f"test-pedidos-urgentes-{os.getpid()}"

    @classmethod
    def setUpClass(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM compras_pedidos_urgentes WHERE submitted_by = %s",
                (cls.actor,),
            )
            cursor.execute("SELECT id FROM stores ORDER BY id LIMIT 2")
            cls.store_ids = [row[0] for row in cursor.fetchall()]
        articles = get_available_urgent_articles()
        cls.coin_article = next(
            (article for article in articles if article["origem_catalogo_chave"] == "categoria:moedas"),
            None,
        )
        if not cls.store_ids or not cls.coin_article:
            raise unittest.SkipTest("Catálogo de moedas ou lojas não disponível.")

    @classmethod
    def tearDownClass(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM compras_pedidos_urgentes WHERE submitted_by = %s",
                (cls.actor,),
            )

    def _create(self, store_id=None, note="teste pedido urgente"):
        return create_urgent_order(
            store_id=store_id or self.store_ids[0],
            target_date=lisboa_today() + timedelta(days=1),
            lines=[{
                "artigo_id": self.coin_article["id"],
                "quantidade": "12",
            }],
            actor=self.actor,
            reason="procura_acima_previsto",
            observations=f"{self.actor}:{note}",
            today=lisboa_today(),
        )

    def test_coins_use_matosinhos_internal_origin_without_supplier(self):
        order = self._create()
        line = get_urgent_order(order["id"])["linhas"][0]
        self.assertEqual(line["origem_tipo_snapshot"], "centro_interno")
        self.assertEqual(line["origem_nome_snapshot"], "Matosinhos")
        self.assertIsNone(line["origem_supplier_id_snapshot"])

    def test_same_submission_is_idempotent(self):
        first = self._create(note="dedupe")
        second = self._create(note="dedupe")
        self.assertEqual(first["id"], second["id"])
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM compras_pedidos_urgentes
                WHERE submitted_by = %s AND observacoes = %s
                """,
                (self.actor, f"{self.actor}:dedupe"),
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_concurrent_same_submission_returns_one_request(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self._create(note="concorrente"),
                range(2),
            ))
        self.assertEqual({result["id"] for result in results}, {results[0]["id"]})

    def test_store_filter_does_not_cross_store_boundary(self):
        first = self._create(self.store_ids[0], note="loja um")
        if len(self.store_ids) < 2:
            self.skipTest("Só existe uma loja semeada.")
        second = self._create(self.store_ids[1], note="loja dois")
        first_ids = {order["id"] for order in list_urgent_orders(store_id=self.store_ids[0])}
        second_ids = {order["id"] for order in list_urgent_orders(store_id=self.store_ids[1])}
        self.assertIn(first["id"], first_ids)
        self.assertNotIn(second["id"], first_ids)
        self.assertIn(second["id"], second_ids)

    def test_states_are_audited_and_cannot_reopen(self):
        order = self._create(note="estado")
        prepared = transition_urgent_order_status(
            order["id"], "em_preparacao", self.actor
        )
        self.assertEqual(prepared["status"], "em_preparacao")
        completed = transition_urgent_order_status(
            order["id"], "concluida", self.actor
        )
        self.assertEqual(completed["status"], "concluida")
        with self.assertRaises(ValueError):
            transition_urgent_order_status(order["id"], "submetida", self.actor)
        events = get_urgent_order_audit(order["id"])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["submetida", "estado_alterado", "estado_alterado"],
        )

    def test_date_and_reason_validation(self):
        with self.assertRaises(ValueError):
            normalise_target_date(date(2020, 1, 1), today=date(2026, 9, 22))
        with self.assertRaises(ValueError):
            normalise_reason("outro")
        self.assertEqual(
            normalise_reason("outro", "falta extraordinária"),
            ("outro", "falta extraordinária"),
        )


if __name__ == "__main__":
    unittest.main()