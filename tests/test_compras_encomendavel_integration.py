"""Database-backed coverage for new orderability behavior and migration repeats."""

import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date, timedelta
from threading import Event
from uuid import uuid4

from db.connection import db_connection
from db.contagens_compras import (
    get_available_count_articles,
    get_or_create_count_draft,
    save_count_draft,
)
from db.encomendas_semanais import (
    get_available_weekly_articles,
    get_or_create_weekly_order,
    get_weekly_order,
    save_weekly_draft,
    submit_weekly_order,
    amend_weekly_order,
)
from db.pedidos_urgentes import (
    create_urgent_order,
    get_available_urgent_articles,
    lisboa_today as urgent_today,
)


class TestComprasEncomendavelIntegration(unittest.TestCase):
    actor = f"test-orderability-{uuid4().hex[:24]}"

    @classmethod
    def setUpClass(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM stores WHERE is_active = TRUE ORDER BY id LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                raise unittest.SkipTest("É necessária uma loja ativa.")
            cls.store_id = row[0]
            cls.article_ids = []
            for suffix in ("historico", "novo"):
                cursor.execute(
                    """
                    INSERT INTO artigos_administrativos
                        (fornecedor, produto, ativo, encomendavel)
                    VALUES (%s, %s, TRUE, TRUE)
                    RETURNING id
                    """,
                    (
                        f"Fornecedor de teste {cls.actor}",
                        f"Artigo {suffix} {cls.actor}",
                    ),
                )
                cls.article_ids.append(cursor.fetchone()[0])
            conn.commit()

    @classmethod
    def tearDownClass(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM compras_pedidos_urgentes WHERE submitted_by = %s",
                (cls.actor,),
            )
            cursor.execute(
                """
                DELETE FROM compras_encomendas_semanais
                WHERE created_by = %s
                """,
                (cls.actor,),
            )
            cursor.execute(
                """
                DELETE FROM compras_contagens_artigos
                WHERE created_by = %s
                """,
                (cls.actor,),
            )
            cursor.execute(
                "DELETE FROM artigos_administrativos WHERE id = ANY(%s)",
                (cls.article_ids,),
            )
            conn.commit()

    def setUp(self):
        self._set_article(self.article_ids[0], active=True, orderable=True)
        self._set_article(self.article_ids[1], active=True, orderable=True)

    @classmethod
    def _set_article(cls, article_id, active, orderable):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE artigos_administrativos
                   SET ativo = %s, encomendavel = %s
                 WHERE id = %s
                """,
                (active, orderable, article_id),
            )
            conn.commit()

    def test_matrix_and_unconfirmed_supplier_do_not_change_count_or_order_rules(self):
        first_id = self.article_ids[0]
        for active, orderable, can_order, can_count in (
            (True, True, True, True),
            (True, False, False, True),
            (False, True, False, False),
            (False, False, False, False),
        ):
            with self.subTest(active=active, orderable=orderable):
                self._set_article(first_id, active, orderable)
                weekly_ids = {
                    row["id"] for row in get_available_weekly_articles()
                }
                urgent_ids = {
                    row["id"] for row in get_available_urgent_articles()
                }
                count_ids = {
                    row["id"] for row in get_available_count_articles()
                }
                self.assertEqual(first_id in weekly_ids, can_order)
                self.assertEqual(first_id in urgent_ids, can_order)
                self.assertEqual(first_id in count_ids, can_count)

                if active:
                    with db_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT fornecedor_oficial_id FROM artigos_administrativos "
                            "WHERE id = %s",
                            (first_id,),
                        )
                        self.assertIsNone(cursor.fetchone()[0])

    def test_clean_catalogue_migration_is_repeatable_after_manual_revocation(self):
        from db.schema import run_migrations_compras_catalogo

        article_id = self.article_ids[0]
        self._set_article(article_id, active=True, orderable=False)
        run_migrations_compras_catalogo()
        run_migrations_compras_catalogo()
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ativo, encomendavel, fornecedor_oficial_id "
                "FROM artigos_administrativos WHERE id = %s",
                (article_id,),
            )
            self.assertEqual(cursor.fetchone(), (True, False, None))

    def test_weekly_draft_rechecks_revocation_and_preserves_submitted_history(self):
        article_id, new_article_id = self.article_ids
        sunday = date(2099, 1, 4)
        order = get_or_create_weekly_order(
            self.store_id, sunday, actor=self.actor
        )
        save_weekly_draft(
            order["id"],
            [{"artigo_id": article_id, "quantidade": "2"}],
            self.actor,
        )

        self._set_article(article_id, active=True, orderable=False)
        with self.assertRaisesRegex(ValueError, "novas encomendas"):
            submit_weekly_order(order["id"], self.actor, today=sunday)
        still_draft = get_weekly_order(order["id"])
        self.assertEqual(still_draft["status"], "rascunho")
        self.assertEqual(still_draft["linhas"][0]["artigo_id"], article_id)

        self._set_article(article_id, active=True, orderable=True)
        submitted = submit_weekly_order(order["id"], self.actor, today=sunday)
        self.assertEqual(submitted["status"], "submetida")
        submitted_product_snapshot = submitted["linhas"][0]["produto_snapshot"]

        self._set_article(article_id, active=False, orderable=False)
        repeated = submit_weekly_order(order["id"], self.actor, today=sunday)
        self.assertEqual(repeated["status"], "submetida")
        self.assertEqual(repeated["linhas"][0]["artigo_id"], article_id)

        self._set_article(new_article_id, active=True, orderable=False)
        with self.assertRaisesRegex(ValueError, "novas encomendas"):
            amend_weekly_order(
                order["id"],
                [
                    {"artigo_id": article_id, "quantidade": "2"},
                    {"artigo_id": new_article_id, "quantidade": "1"},
                ],
                self.actor,
                "Teste de elegibilidade",
            )
        amended = amend_weekly_order(
            order["id"],
            [{"artigo_id": article_id, "quantidade": "2"}],
            self.actor,
            "Preservar linha submetida",
        )
        self.assertEqual(amended["linhas"][0]["artigo_id"], article_id)
        self.assertEqual(
            amended["linhas"][0]["produto_snapshot"],
            submitted_product_snapshot,
        )

    def test_count_draft_accepts_active_nonorderable_article_without_supplier(self):
        article_id = self.article_ids[0]
        self._set_article(article_id, active=True, orderable=False)
        draft = get_or_create_count_draft(
            self.store_id, urgent_today(), self.actor
        )
        saved = save_count_draft(
            draft["id"],
            [{"artigo_id": article_id, "quantidade": "3"}],
            self.actor,
        )
        self.assertEqual(saved["linhas"][0]["artigo_id"], article_id)

    def test_urgent_duplicate_retries_survive_later_revocation(self):
        article_id = self.article_ids[0]
        today = urgent_today()
        target = today + timedelta(days=1)
        parameters = {
            "store_id": self.store_id,
            "target_date": target,
            "lines": [{"artigo_id": article_id, "quantidade": "1"}],
            "actor": self.actor,
            "reason": "procura_acima_previsto",
            "observations": f"{self.actor}:repetição",
            "today": today,
        }
        original = create_urgent_order(**parameters)
        self._set_article(article_id, active=False, orderable=False)
        repeated = create_urgent_order(**parameters)
        self.assertEqual(repeated["id"], original["id"])

        parameters["observations"] = f"{self.actor}:nova encomenda"
        with self.assertRaises(ValueError):
            create_urgent_order(**parameters)

    def test_urgent_order_waits_for_eligibility_revocation_then_rejects(self):
        article_id = self.article_ids[0]
        today = urgent_today()
        target = today + timedelta(days=1)
        row_locked = Event()
        allow_revocation_commit = Event()
        order_started = Event()

        def revoke_eligibility():
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id FROM artigos_administrativos WHERE id = %s FOR UPDATE",
                    (article_id,),
                )
                cursor.execute(
                    "UPDATE artigos_administrativos SET encomendavel = FALSE "
                    "WHERE id = %s",
                    (article_id,),
                )
                row_locked.set()
                if not allow_revocation_commit.wait(timeout=10):
                    raise TimeoutError("A revogação não foi libertada.")
                conn.commit()

        def submit_new_order():
            order_started.set()
            return create_urgent_order(
                store_id=self.store_id,
                target_date=target,
                lines=[{"artigo_id": article_id, "quantidade": "1"}],
                actor=self.actor,
                reason="procura_acima_previsto",
                observations=f"{self.actor}:concorrência",
                today=today,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            revoke_future = executor.submit(revoke_eligibility)
            self.assertTrue(row_locked.wait(timeout=5))
            order_future = executor.submit(submit_new_order)
            try:
                self.assertTrue(order_started.wait(timeout=5))
                with self.assertRaises(TimeoutError):
                    order_future.result(timeout=0.5)
            finally:
                allow_revocation_commit.set()

            revoke_future.result(timeout=10)
            with self.assertRaisesRegex(ValueError, "novas encomendas"):
                order_future.result(timeout=10)


if __name__ == "__main__":
    unittest.main()