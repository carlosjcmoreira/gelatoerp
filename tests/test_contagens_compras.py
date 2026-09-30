import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from db.connection import db_connection
from db.contagens_compras import (
    get_available_count_articles,
    get_count_audit,
    get_count_history,
    get_count_draft,
    get_or_create_count_draft,
    lisboa_today,
    list_submitted_counts,
    normalise_count_date,
    save_count_draft,
    submit_count,
)


class TestContagensCompras(unittest.TestCase):
    actor = f"test-contagens-compras-{os.getpid()}"

    @classmethod
    def setUpClass(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM compras_contagens_artigos WHERE created_by = %s",
                (cls.actor,),
            )
            cursor.execute("SELECT id FROM stores ORDER BY id LIMIT 2")
            cls.store_ids = [row[0] for row in cursor.fetchall()]
        articles = get_available_count_articles()
        cls.article = next(
            (article for article in articles
             if article.get("fornecedor_oficial_id") is not None),
            articles[0] if articles else None,
        )
        if not cls.store_ids or not cls.article:
            raise unittest.SkipTest("Lojas ou catálogo de Compras não disponíveis.")

    @classmethod
    def tearDownClass(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM compras_contagens_artigos WHERE created_by = %s",
                (cls.actor,),
            )

    def _draft(self, store_id=None):
        return get_or_create_count_draft(
            store_id or self.store_ids[0], lisboa_today(), self.actor
        )

    def _submit(self, quantity="3"):
        draft = self._draft()
        save_count_draft(
            draft["id"],
            [{"artigo_id": self.article["id"], "quantidade": quantity}],
            self.actor,
        )
        return submit_count(draft["id"], self.actor)

    def test_snapshot_history_is_append_only(self):
        first = self._submit("3")
        second = self._submit("8")
        history = get_count_history(self.store_ids[0])
        own = [item for item in history if item["created_by"] == self.actor]
        quantities = {
            item["contagem_id"]: item["linhas"][0]["quantidade"]
            for item in own
            if item["contagem_id"] in (first["contagem_id"], second["contagem_id"])
        }
        self.assertEqual(
            set(quantities),
            {first["contagem_id"], second["contagem_id"]},
        )
        self.assertEqual(quantities[first["contagem_id"]], 3.0)
        self.assertEqual(quantities[second["contagem_id"]], 8.0)

    def test_submitted_snapshot_uses_direct_supplier_and_keeps_store_context(self):
        submitted = self._submit("3")
        history = get_count_history(self.store_ids[0])
        saved = next(
            row for row in history
            if row["contagem_id"] == submitted["contagem_id"]
        )
        line = saved["linhas"][0]

        self.assertEqual(saved["store_id"], self.store_ids[0])
        self.assertEqual(
            line["fornecedor_oficial_id"],
            self.article.get("fornecedor_oficial_id"),
        )
        self.assertEqual(
            line["fornecedor_oficial_nome"],
            self.article.get("fornecedor_oficial_nome"),
        )
        self.assertIsNone(line["origem_id"])
        self.assertIsNone(line["origem_tipo"])
        self.assertIsNone(line["origem_nome"])

    def test_repeated_submission_is_idempotent_and_audited_once(self):
        draft = self._draft()
        save_count_draft(
            draft["id"],
            [{"artigo_id": self.article["id"], "quantidade": "0"}],
            self.actor,
        )
        first = submit_count(draft["id"], self.actor)
        second = submit_count(draft["id"], self.actor)
        self.assertEqual(first["contagem_id"], second["contagem_id"])
        self.assertEqual(first["versao"], second["versao"])
        events = get_count_audit(draft["id"])
        self.assertEqual([event["event_type"] for event in events], ["submetida"])

    def test_concurrent_draft_open_returns_one_draft(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            drafts = list(pool.map(
                lambda _: self._draft(self.store_ids[1]),
                range(2),
            ))
        self.assertEqual({draft["id"] for draft in drafts}, {drafts[0]["id"]})

    def test_store_filter_isolation(self):
        first = self._submit("4")
        if len(self.store_ids) < 2:
            self.skipTest("Só existe uma loja semeada.")
        other_draft = self._draft(self.store_ids[1])
        save_count_draft(
            other_draft["id"],
            [{"artigo_id": self.article["id"], "quantidade": "9"}],
            self.actor,
        )
        second = submit_count(other_draft["id"], self.actor)
        first_rows = list_submitted_counts(store_id=self.store_ids[0])
        second_rows = list_submitted_counts(store_id=self.store_ids[1])
        self.assertIn(first["contagem_id"], {row["contagem_id"] for row in first_rows})
        self.assertNotIn(first["contagem_id"], {row["contagem_id"] for row in second_rows})
        self.assertIn(second["contagem_id"], {row["contagem_id"] for row in second_rows})

    def test_future_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            normalise_count_date(lisboa_today() + timedelta(days=1))

    def test_submitted_snapshot_survives_article_deactivation(self):
        snapshot = self._submit("6")
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE artigos_administrativos SET ativo = FALSE WHERE id = %s",
                (self.article["id"],),
            )
        try:
            historical = get_count_draft(snapshot["contagem_id"])
            self.assertEqual(historical["status"], "submetida")
            rows = get_count_history(self.store_ids[0])
            matching = next(
                row for row in rows if row["contagem_id"] == snapshot["contagem_id"]
            )
            self.assertEqual(matching["linhas"][0]["produto"], self.article["produto"])
        finally:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE artigos_administrativos SET ativo = TRUE WHERE id = %s",
                    (self.article["id"],),
                )


if __name__ == "__main__":
    unittest.main()