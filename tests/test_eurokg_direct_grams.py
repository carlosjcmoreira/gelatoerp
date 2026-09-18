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
from db.pastelaria import get_consumo_gelado_mensal
from db.schema import _backfill_initial_product_dose_ranges


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
            cur.execute("""
                DELETE FROM vendas_detalhe
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
            self.product_id, 125, "fixa", "test", source="teste",
            effective_from=date.today(),
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

    def test_existing_dose_requires_an_effective_date_for_changes(self):
        old_start = date.today() - timedelta(days=10)
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO gramas_gelado_historico (
                    artigo, gramas, tipo_dose, valid_from
                ) VALUES (%s, 90, 'fixa', %s)
                RETURNING id
            """, (self.product, old_start))
            old_rule = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO produto_regra_dose_historico (
                    produto_vendas_config_id, regra_dose_id,
                    valid_from, created_by
                ) VALUES (%s, %s, %s, 'test')
            """, (self.product_id, old_rule, old_start))
            conn.commit()

        with self.assertRaisesRegex(ValueError, "Indique a data"):
            set_product_dose(
                self.product_id, 125, "fixa", "test", source="teste"
            )

        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT gramas, valid_from, valid_to
                FROM gramas_gelado_historico
                WHERE artigo=%s
            """, (self.product,))
            self.assertEqual(cur.fetchall(), [(90, old_start, None)])

    def test_first_dose_starts_at_first_recorded_sale(self):
        first_sale = date.today() - timedelta(days=30)
        second_sale = date.today() - timedelta(days=2)
        store_a = f"__DIRECT_STORE_A_{self.suffix}__"
        store_b = f"__DIRECT_STORE_B_{self.suffix}__"
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO vendas_detalhe (
                    data, loja, produto, quantidade, valor_euros,
                    produto_vendas_config_id
                ) VALUES
                    (%s, %s, %s, 2, 10, %s),
                    (%s, %s, %s, 3, 15, %s)
            """, (
                first_sale, store_a, self.product, self.product_id,
                second_sale, store_b, self.product, self.product_id,
            ))
            conn.commit()

        rule_id = set_product_dose(
            self.product_id, 100, "fixa", "test", source="teste"
        )

        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT gramas, valid_from, valid_to
                FROM gramas_gelado_historico
                WHERE id=%s
            """, (rule_id,))
            self.assertEqual(cur.fetchone(), (100, first_sale, None))
            cur.execute("""
                SELECT valid_from, valid_to
                FROM produto_regra_dose_historico
                WHERE produto_vendas_config_id=%s
            """, (self.product_id,))
            self.assertEqual(cur.fetchone(), (first_sale, None))

        result = get_consumo_gelado_mensal(store_a)
        self.assertEqual(len(result), 1)
        self.assertEqual(
            float(result.iloc[0]["consumo_kg"]),
            0.2,
        )

    def test_migration_extends_preexisting_first_rule_to_first_sale(self):
        first_sale = date.today() - timedelta(days=40)
        old_cutoff = date.today() - timedelta(days=5)
        store = f"__DIRECT_MIGRATION_STORE_{self.suffix}__"
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO vendas_detalhe (
                    data, loja, produto, quantidade, valor_euros,
                    produto_vendas_config_id
                ) VALUES (%s, %s, %s, 2, 10, %s)
            """, (
                first_sale, store, self.product, self.product_id,
            ))
            cur.execute("""
                INSERT INTO gramas_gelado_historico (
                    artigo, gramas, tipo_dose, valid_from
                ) VALUES (%s, 100, 'fixa', %s)
                RETURNING id
            """, (self.product, old_cutoff))
            rule_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO produto_regra_dose_historico (
                    produto_vendas_config_id, regra_dose_id,
                    valid_from, created_by
                ) VALUES (%s, %s, %s, 'test')
            """, (self.product_id, rule_id, old_cutoff))
            conn.commit()

        before = get_consumo_gelado_mensal(store)
        self.assertTrue(before.iloc[0]["consumo_incompleto"])

        with db_connection() as conn:
            cur = conn.cursor()
            self.assertEqual(
                _backfill_initial_product_dose_ranges(
                    cur, product_ids=[self.product_id]
                ),
                (1, 1),
            )
            self.assertEqual(
                _backfill_initial_product_dose_ranges(
                    cur, product_ids=[self.product_id]
                ),
                (0, 0),
            )
            conn.commit()

        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT valid_from
                FROM gramas_gelado_historico
                WHERE id=%s
            """, (rule_id,))
            self.assertEqual(cur.fetchone()[0], first_sale)
            cur.execute("""
                SELECT valid_from
                FROM produto_regra_dose_historico
                WHERE produto_vendas_config_id=%s
            """, (self.product_id,))
            self.assertEqual(cur.fetchone()[0], first_sale)

        after = get_consumo_gelado_mensal(store)
        self.assertFalse(after.iloc[0]["consumo_incompleto"])
        self.assertEqual(float(after.iloc[0]["consumo_kg"]), 0.2)

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
