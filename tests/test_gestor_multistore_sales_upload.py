"""Regression for the store-code reversal observed in the 2026-02-02 export."""
import os
import unittest
import uuid
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import patch

from flask import Flask
from openpyxl import Workbook

from db.connection import db_connection
from flask_app.routes.gestor import gestor_bp


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "PostgreSQL integration test requires DATABASE_URL")
class MultiStoreSalesUploadTests(unittest.TestCase):
    def setUp(self):
        # Never replace real sales: these dates are unique to this test run.
        self.day = date(2300, 1, 1) + timedelta(days=uuid.uuid4().int % 100000)
        self.next_day = self.day + timedelta(days=1)
        self.last_day = self.day + timedelta(days=2)
        self.prefix = f"Teste multiloja {uuid.uuid4().hex}"
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(gestor_bp)
        self.pairs = [
            (self.day, "Matosinhos"), (self.day, "Bolhão"),
            (self.next_day, "Matosinhos"), (self.next_day, "Bolhão"),
            (self.last_day, "Matosinhos"),
        ]
        with db_connection() as conn:
            cur = conn.cursor()
            for day, store in self.pairs:
                cur.execute(
                    "SELECT COUNT(*) FROM vendas_detalhe WHERE data=%s AND loja=%s",
                    (day, store),
                )
                if cur.fetchone()[0]:
                    self.skipTest("Test date already contains sales; refusing to replace them")

    def tearDown(self):
        if not hasattr(self, "pairs"):
            return
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM vendas_detalhe WHERE produto LIKE %s", (self.prefix + "%",))
            cur.execute("DELETE FROM produtos_vendas_config WHERE produto LIKE %s", (self.prefix + "%",))
            conn.commit()

    def _export(self):
        workbook = Workbook()
        sheet = workbook.active
        headers = ["Data", "Produto", "Familia / Sub-Familia", "Quantidade",
                   "Valor Total S/IVA", "Valor Total C/IVA"]
        for code, day, count, total_qty, gross, net in (
            ("1", self.day, 7, 17, 61.90, 54.781),
            ("3", self.day, 26, 80, 353.30, 309.902),
            ("1", self.next_day, 1, 2, 9.00, 8.00),
        ):
            sheet.append([f"Loja : {code}"])
            sheet.append(headers)
            for i in range(count):
                sheet.append([
                    day.strftime("%d-%m-%Y"), f"{self.prefix} L{code} D{day} #{i}",
                    "Gelado /", total_qty - count + 1 if i == 0 else 1,
                    net - count + 1 if i == 0 else 1,
                    gross - count + 1 if i == 0 else 1,
                ])
            sheet.append(["Totais"])
        stream = BytesIO()
        workbook.save(stream)
        return stream.getvalue()

    def _upload(self, payload):
        # Exercise the actual POST route, including its code mapping and overlap
        # check. Avoid unrelated product synchronization and forecast threads.
        with patch("flask_app.routes.gestor.get_tabs", return_value=[]), \
             patch("flask_app.routes.gestor.db.sync_produtos_vendas_config"), \
             patch("flask_app.routes.gestor.invalidate_prefix"), \
             patch("flask_app.routes.gestor.threading.Thread"):
            with self.app.test_client() as client:
                with client.session_transaction() as session:
                    session["user"] = {"acesso_gestor": True}
                response = client.post(
                    "/vendas-detalhe",
                    data={"action": "upload_vendas",
                          "vendas_file": (BytesIO(payload), "vendas.xlsx")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 302)
                with client.session_transaction() as session:
                    messages = session.get("_flashes", [])
                self.assertTrue(
                    any(category == "success" and "34 registos" in text
                        for category, text in messages),
                    messages,
                )

    def _rows(self):
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT data, loja, produto, quantidade, valor_euros,
                          valor_sem_iva_euros
                   FROM vendas_detalhe WHERE (data, loja) IN (
                       (%s, %s), (%s, %s), (%s, %s), (%s, %s), (%s, %s))
                   ORDER BY data, loja, produto""",
                tuple(item for pair in self.pairs for item in pair),
            )
            return cur.fetchall()

    def _assert_imported(self, rows):
        expected = {
            (self.day, "Matosinhos"): (7, 17, 61.90, 54.781, "L1"),
            (self.day, "Bolhão"): (26, 80, 353.30, 309.902, "L3"),
            (self.next_day, "Matosinhos"): (1, 2, 9.00, 8.00, "L1"),
        }
        for (day, store), (count, qty, gross, net, code) in expected.items():
            matched = [r for r in rows if r[0] == day and r[1] == store]
            self.assertEqual(len(matched), count, (day, store))
            self.assertTrue(all(f" {code} " in r[2] for r in matched))
            self.assertAlmostEqual(sum(float(r[3]) for r in matched), qty)
            self.assertAlmostEqual(sum(float(r[4]) for r in matched), gross)
            self.assertAlmostEqual(sum(float(r[5]) for r in matched), net, places=3)

    def test_two_store_upload_and_replacement_preserve_other_pairs(self):
        payload = self._export()
        self._upload(payload)
        self._assert_imported(self._rows())

        # Simulate the swapped historic rows at the two overlapping pairs.
        # Rows on a date absent from the file, and the other store on a date
        # present in the file, must survive both replacements.
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM vendas_detalhe WHERE data=%s AND loja IN ('Matosinhos', 'Bolhão')",
                (self.day,),
            )
            for day, store, marker in (
                (self.day, "Matosinhos", "old L3"),
                (self.day, "Bolhão", "old L1"),
                (self.next_day, "Matosinhos", "old next"),
                (self.next_day, "Bolhão", "keep other store"),
                (self.last_day, "Matosinhos", "keep other day"),
            ):
                cur.execute(
                    """INSERT INTO vendas_detalhe
                       (data, loja, produto, quantidade, valor_euros)
                       VALUES (%s, %s, %s, 1, 1)""",
                    (day, store, f"{self.prefix} {marker}"),
                )
            conn.commit()

        self._upload(payload)
        rows = self._rows()
        self._assert_imported(rows)
        kept = [r[2] for r in rows if "keep " in r[2]]
        self.assertEqual(sorted(kept), sorted([
            f"{self.prefix} keep other store",
            f"{self.prefix} keep other day",
        ]))
        self.assertEqual(len(rows), 36)
        self.assertFalse(any("old " in r[2] for r in rows))

        self._upload(payload)
        self.assertEqual(self._rows(), rows)  # no duplicate rows on re-upload


if __name__ == "__main__":
    unittest.main()