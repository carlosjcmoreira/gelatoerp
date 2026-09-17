import os
import unittest
import uuid
from datetime import date, timedelta

from db.connection import db_connection
from db.doseamento import (
    get_dose_product_configuration_queue,
    set_product_dose,
    set_typology_dose,
)


@unittest.skipUnless(
    os.environ.get("DATABASE_URL"),
    "PostgreSQL integration test requires DATABASE_URL",
)
class EurokgDirectGramsTests(unittest.TestCase):
    def setUp(self):
        self.suffix = uuid.uuid4().hex
        self.product = f"__DIRECT_GRAMS_{self.suffix}__"
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO produtos_vendas_config (
                    produto, gelado_kpi, dose_config_pendente
                ) VALUES (%s, TRUE, TRUE)
                RETURNING id
            """, (self.product,))
            self.product_id = cur.fetchone()[0]
            conn.commit()

    def tearDown(self):
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE produtos_vendas_config
                SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                WHERE id=%s
            """, (self.product_id,))
            cur.execute("""
                DELETE FROM produto_regra_dose_historico
                WHERE produto_vendas_config_id=%s
            """, (self.product_id,))
            cur.execute(
                "DELETE FROM gramas_gelado_historico WHERE artigo=%s",
                (self.product,),
            )
            cur.execute(
                "DELETE FROM gramas_gelado WHERE artigo=%s",
                (self.product,),
            )
            cur.execute(
                "DELETE FROM produtos_vendas_config WHERE id=%s",
                (self.product_id,),
            )
            conn.commit()

    def test_queue_is_read_only_and_keeps_selected_product_pending(self):
        products, rules = get_dose_product_configuration_queue()
        row = next(item for item in products if item["id"] == self.product_id)
        self.assertTrue(row["dose_config_pendente"])
        self.assertEqual(rules, [])
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT gelado_kpi, dose_config_pendente
                FROM produtos_vendas_config WHERE id=%s
            """, (self.product_id,))
            self.assertEqual(cur.fetchone(), (True, True))

    def test_direct_grams_preserve_previous_period(self):
        yesterday = date.today() - timedelta(days=1)
        two_days_ago = yesterday - timedelta(days=1)
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO gramas_gelado_historico (
                    artigo, gramas, tipo_dose, valid_from
                ) VALUES (%s, 90, 'fixa', %s)
                RETURNING id
            """, (self.product, two_days_ago))
            old_rule = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO produto_regra_dose_historico (
                    produto_vendas_config_id, regra_dose_id,
                    valid_from, created_by
                ) VALUES (%s, %s, %s, 'test')
            """, (self.product_id, old_rule, two_days_ago))
            conn.commit()

        new_rule = set_product_dose(
            self.product_id, 125, "fixa", "test", source="teste"
        )
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT gramas, valid_from, valid_to
                FROM gramas_gelado_historico
                WHERE artigo=%s ORDER BY valid_from
            """, (self.product,))
            self.assertEqual(
                cur.fetchall(),
                [
                    (90, two_days_ago, yesterday),
                    (125, date.today(), None),
                ],
            )
            cur.execute("""
                SELECT regra_dose_id, valid_from, valid_to
                FROM produto_regra_dose_historico
                WHERE produto_vendas_config_id=%s
                ORDER BY valid_from
            """, (self.product_id,))
            self.assertEqual(
                cur.fetchall(),
                [
                    (old_rule, two_days_ago, yesterday),
                    (new_rule, date.today(), None),
                ],
            )
            cur.execute("""
                SELECT gelado_kpi, dose_config_pendente
                FROM produtos_vendas_config WHERE id=%s
            """, (self.product_id,))
            self.assertEqual(cur.fetchone(), (True, False))

    def test_same_day_typology_edit_replaces_direct_value(self):
        first_rule = set_product_dose(
            self.product_id, 100, "fixa", "test", source="Euro/kg"
        )
        second_rule, affected = set_typology_dose(
            self.product, 135, "test", source="Pastelaria"
        )
        self.assertEqual(affected, 1)
        self.assertNotEqual(first_rule, second_rule)
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT hist.id, hist.gramas
                FROM produto_regra_dose_historico prd
                JOIN gramas_gelado_historico hist
                  ON hist.id=prd.regra_dose_id
                WHERE prd.produto_vendas_config_id=%s
                  AND CURRENT_DATE BETWEEN prd.valid_from
                      AND COALESCE(prd.valid_to, 'infinity'::date)
            """, (self.product_id,))
            self.assertEqual(cur.fetchall(), [(second_rule, 135)])
