import os
import threading
import unittest
import uuid

from db.connection import db_connection
from db.doseamento import (
    confirm_historical_dose_preview,
    create_historical_dose_preview,
)


@unittest.skipUnless(
    os.environ.get("DATABASE_URL"),
    "PostgreSQL integration test requires DATABASE_URL",
)
class HistoricalDoseConcurrencyTests(unittest.TestCase):
    def test_stale_concurrent_preview_cannot_overlap_or_duplicate_audit(self):
        article = "__DOSE_CONCURRENCY_" + uuid.uuid4().hex + "__"
        actor = "sistema:test-dose-concurrency"
        contents = (
            "artigo,tipo_dose,gramas,data_efetiva,evidencia\n"
            f"{article},fixa,90,2024-01-01,Prova temporária\n"
        )
        previews = []
        batch_ids = []
        try:
            previews = [
                create_historical_dose_preview(contents, actor, "a.csv"),
                create_historical_dose_preview(contents, actor, "b.csv"),
            ]
            barrier = threading.Barrier(2)
            outcomes = []

            def confirm(preview):
                barrier.wait()
                try:
                    batch_id = confirm_historical_dose_preview(
                        preview["id"], actor
                    )
                    outcomes.append(("ok", batch_id))
                except ValueError as exc:
                    outcomes.append(("stale", str(exc)))

            threads = [
                threading.Thread(target=confirm, args=(preview,))
                for preview in previews
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sorted(value[0] for value in outcomes), ["ok", "stale"])
            batch_ids = [value[1] for value in outcomes if value[0] == "ok"]

            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*)
                    FROM gramas_gelado_historico
                    WHERE artigo = %s
                """, (article,))
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("""
                    SELECT COUNT(*)
                    FROM gramas_gelado_import_audit
                    WHERE id = ANY(%s::uuid[])
                """, (batch_ids,))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE artigo = %s",
                    (article,),
                )
                cur.execute("""
                    DELETE FROM gramas_gelado_import_audit
                    WHERE created_by = %s AND source_name IN ('a.csv', 'b.csv')
                      AND rows_json::text LIKE %s
                """, (actor, "%" + article + "%"))
                for preview in previews:
                    cur.execute(
                        "DELETE FROM gramas_gelado_import_preview WHERE id = %s",
                        (preview["id"],),
                    )
                conn.commit()


if __name__ == "__main__":
    unittest.main()