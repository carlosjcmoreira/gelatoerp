import os
import unittest
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch
from psycopg2.extras import RealDictCursor

from db.pastelaria import (
    get_consumo_gelado_mensal,
    update_gramas_gelado,
    update_produtos_vendas_config_batch,
)
from db.connection import db_connection
from db.doseamento import (
    _import_historical_doses_tx,
    configure_dose_product,
    get_dose_product_configuration_queue,
    get_historical_dose_coverage,
    set_product_dose,
)
from db.schema import _backfill_alias_initial_product_dose_ranges


class _Cursor:
    def __init__(self, pending_results):
        self.pending_results = iter(pending_results)
        self.executions = []

    def execute(self, sql, params):
        self.executions.append((sql, params))

    def fetchone(self):
        return (next(self.pending_results),)


class _Connection:
    def __init__(self, pending_results):
        self.cursor_value = _Cursor(pending_results)
        self.committed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True


class DoseProductConfigTransitionTests(unittest.TestCase):
    def test_manual_gelado_selection_reports_product_queued_for_dose(self):
        connection = _Connection([True])

        @contextmanager
        def fake_connection():
            yield connection

        with patch("db.pastelaria.db_connection", fake_connection):
            queued = update_produtos_vendas_config_batch([{
                "id": 91,
                "gelado_kpi": True,
                "pastelaria": False,
                "confeitaria": False,
            }])

        self.assertEqual(queued, 1)
        self.assertTrue(connection.committed)
        sql, params = connection.cursor_value.executions[0]
        self.assertIn("dose_config_pendente = CASE", sql)
        self.assertIn("RETURNING dose_config_pendente", sql)
        self.assertEqual(params["id"], 91)
        self.assertTrue(params["gelado"])

    def test_manual_gelado_selection_with_rule_is_not_reported_pending(self):
        connection = _Connection([False])

        @contextmanager
        def fake_connection():
            yield connection

        with patch("db.pastelaria.db_connection", fake_connection):
            queued = update_produtos_vendas_config_batch([{
                "id": 92,
                "gelado_kpi": True,
                "pastelaria": False,
                "confeitaria": False,
            }])

        self.assertEqual(queued, 0)


@unittest.skipUnless(
    os.environ.get("DATABASE_URL"),
    "PostgreSQL integration test requires DATABASE_URL",
)
class DoseProductConfigIntegrationTests(unittest.TestCase):
    def test_first_dose_covers_sales_from_exact_alias_family(self):
        suffix = uuid.uuid4().hex
        old_name_a = "__ALIAS_OLD_A_" + suffix + "__"
        old_name_b = "__ALIAS_OLD_B_" + suffix + "__"
        canonical_name = "__ALIAS_CANONICAL_" + suffix + "__"
        store = "__ALIAS_STORE_" + suffix + "__"
        product_ids = []
        first_sale = date.today().replace(month=1, day=10)
        second_sale = date.today().replace(month=2, day=10)
        canonical_sale = date.today().replace(month=5, day=10)
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                for name, selected in (
                    (old_name_a, False),
                    (old_name_b, False),
                    (canonical_name, True),
                ):
                    cur.execute("""
                        INSERT INTO produtos_vendas_config (
                            produto, gelado_kpi, dose_config_pendente
                        ) VALUES (%s, %s, TRUE)
                        RETURNING id
                    """, (name, selected))
                    product_ids.append(cur.fetchone()[0])
                cur.execute("""
                    INSERT INTO produtos_vendas_aliases
                        (nome_antigo, nome_atual)
                    VALUES (%s, %s), (%s, %s)
                """, (
                    old_name_a, canonical_name,
                    old_name_b, canonical_name,
                ))
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES
                        (%s, %s, %s, 2, %s),
                        (%s, %s, %s, 3, %s),
                        (%s, %s, %s, 4, %s)
                """, (
                    first_sale, store, old_name_a, product_ids[0],
                    second_sale, store, old_name_b, product_ids[1],
                    canonical_sale, store, canonical_name, product_ids[2],
                ))
                conn.commit()

            set_product_dose(
                product_ids[2], 100, "fixa", "sistema:test"
            )

            queue, _rules = get_dose_product_configuration_queue()
            queued_product = next(
                row for row in queue if row["id"] == product_ids[2]
            )
            self.assertEqual(
                queued_product["canonical_product"],
                {
                    "id": product_ids[2],
                    "produto": canonical_name,
                    "is_canonical": True,
                },
            )
            self.assertEqual(
                queued_product["aliases"],
                [
                    {
                        "id": product_ids[0],
                        "produto": old_name_a,
                        "is_canonical": False,
                    },
                    {
                        "id": product_ids[1],
                        "produto": old_name_b,
                        "is_canonical": False,
                    },
                ],
            )

            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT hist.gramas, prd.valid_from
                    FROM produto_regra_dose_historico prd
                    JOIN gramas_gelado_historico hist
                      ON hist.id=prd.regra_dose_id
                    WHERE prd.produto_vendas_config_id=%s
                """, (product_ids[2],))
                self.assertEqual(cur.fetchone(), (100, first_sale))

            frame = get_consumo_gelado_mensal(
                store,
                data_inicio=first_sale,
                data_fim=canonical_sale,
            )
            self.assertEqual(
                frame[["produto", "mes", "quantidade_vendida",
                       "consumo_kg", "consumo_incompleto"]].to_dict("records"),
                [
                    {
                        "produto": canonical_name,
                        "mes": first_sale.strftime("%Y-%m"),
                        "quantidade_vendida": 2,
                        "consumo_kg": 0.2,
                        "consumo_incompleto": False,
                    },
                    {
                        "produto": canonical_name,
                        "mes": second_sale.strftime("%Y-%m"),
                        "quantidade_vendida": 3,
                        "consumo_kg": 0.3,
                        "consumo_incompleto": False,
                    },
                    {
                        "produto": canonical_name,
                        "mes": canonical_sale.strftime("%Y-%m"),
                        "quantidade_vendida": 4,
                        "consumo_kg": 0.4,
                        "consumo_incompleto": False,
                    },
                ],
            )
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM vendas_detalhe "
                    "WHERE produto_vendas_config_id=ANY(%s)",
                    (product_ids,),
                )
                if product_ids:
                    cur.execute(
                        "DELETE FROM produto_regra_dose_historico "
                        "WHERE produto_vendas_config_id=ANY(%s)",
                        ([product_ids[2]],),
                    )
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=ANY(%s)",
                        (product_ids,),
                    )
                cur.execute("""
                    DELETE FROM produtos_vendas_aliases
                    WHERE nome_antigo IN (%s, %s)
                """, (old_name_a, old_name_b))
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE artigo=%s",
                    (canonical_name,),
                )
                cur.execute(
                    "DELETE FROM gramas_gelado WHERE artigo=%s",
                    (canonical_name,),
                )
                conn.commit()

    def test_alias_migration_recedes_existing_canonical_first_rule(self):
        suffix = uuid.uuid4().hex
        old_name = "__ALIAS_MIGRATION_OLD_" + suffix + "__"
        canonical_name = "__ALIAS_MIGRATION_CANONICAL_" + suffix + "__"
        store = "__ALIAS_MIGRATION_STORE_" + suffix + "__"
        product_ids = []
        rule_id = association_id = None
        first_sale = date.today().replace(month=1, day=10)
        rule_start = date.today().replace(month=5, day=10)
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                for name, selected in (
                    (old_name, False),
                    (canonical_name, True),
                ):
                    cur.execute("""
                        INSERT INTO produtos_vendas_config (
                            produto, gelado_kpi, dose_config_pendente
                        ) VALUES (%s, %s, FALSE)
                        RETURNING id
                    """, (name, selected))
                    product_ids.append(cur.fetchone()[0])
                cur.execute("""
                    INSERT INTO produtos_vendas_aliases
                        (nome_antigo, nome_atual)
                    VALUES (%s, %s)
                """, (old_name, canonical_name))
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES (%s, %s, %s, 1, %s)
                """, (first_sale, store, old_name, product_ids[0]))
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 100, 'fixa', %s)
                    RETURNING id
                """, (canonical_name, rule_start))
                rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produto_regra_dose_historico (
                        produto_vendas_config_id, regra_dose_id,
                        valid_from, created_by
                    ) VALUES (%s, %s, %s, 'sistema:test')
                    RETURNING id
                """, (product_ids[1], rule_id, rule_start))
                association_id = cur.fetchone()[0]
                conn.commit()

            with db_connection() as conn:
                cur = conn.cursor()
                self.assertEqual(
                    _backfill_alias_initial_product_dose_ranges(cur),
                    (1, 1),
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
                    WHERE id=%s
                """, (association_id,))
                self.assertEqual(cur.fetchone()[0], first_sale)

            with db_connection() as conn:
                cur = conn.cursor()
                self.assertEqual(
                    _backfill_alias_initial_product_dose_ranges(cur),
                    (0, 0),
                )
                conn.rollback()
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM vendas_detalhe WHERE loja=%s", (store,)
                )
                if product_ids:
                    cur.execute(
                        "DELETE FROM produto_regra_dose_historico "
                        "WHERE produto_vendas_config_id=ANY(%s)",
                        (product_ids,),
                    )
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=ANY(%s)",
                        (product_ids,),
                    )
                cur.execute("""
                    DELETE FROM produtos_vendas_aliases
                    WHERE nome_antigo=%s
                """, (old_name,))
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE id=%s",
                    (rule_id,),
                )
                conn.commit()

    def test_deactivation_closes_mapping_and_reactivation_returns_to_queue(self):
        suffix = uuid.uuid4().hex
        article = "__DEACTIVATE_RULE_" + suffix + "__"
        product_name = "__DEACTIVATE_PRODUCT_" + suffix + "__"
        store = "__DEACTIVATE_STORE_" + suffix + "__"
        product_id = None
        yesterday = date.today() - timedelta(days=1)
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from, valid_to
                    ) VALUES (%s, 100, 'fixa', %s, CURRENT_DATE)
                    RETURNING id
                """, (article, yesterday))
                rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 110, 'fixa', CURRENT_DATE + 1)
                """, (article,))
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                conn.commit()
            configure_dose_product(product_id, rule_id, "sistema:test")

            update_produtos_vendas_config_batch([{
                "id": product_id, "gelado_kpi": False,
                "pastelaria": False, "confeitaria": False,
            }])
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*) FROM produto_regra_dose_historico
                    WHERE produto_vendas_config_id=%s
                      AND valid_from >= CURRENT_DATE
                """, (product_id,))
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES
                        (%s, %s, %s, 1, %s),
                        (CURRENT_DATE, %s, %s, 1, %s)
                """, (
                    yesterday, store, product_name, product_id,
                    store, product_name, product_id,
                ))
                conn.commit()

            result = get_consumo_gelado_mensal(store)
            rows = result[result["produto"] == product_name]
            self.assertEqual(float(rows.iloc[0]["quantidade_vendida"]), 1)

            queued = update_produtos_vendas_config_batch([{
                "id": product_id, "gelado_kpi": True,
                "pastelaria": False, "confeitaria": False,
            }])
            self.assertEqual(queued, 1)
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT gelado_kpi, dose_config_pendente,
                           EXISTS (
                               SELECT 1
                               FROM produto_regra_dose_historico prd
                               WHERE prd.produto_vendas_config_id=%s
                                 AND CURRENT_DATE BETWEEN prd.valid_from
                                     AND COALESCE(
                                         prd.valid_to, 'infinity'::date
                                     )
                           )
                    FROM produtos_vendas_config WHERE id=%s
                """, (product_id, product_id))
                self.assertEqual(cur.fetchone(), (True, True, False))
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM vendas_detalhe WHERE loja=%s", (store,)
                )
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE artigo=%s",
                    (article,),
                )
                conn.commit()

    def test_backdated_new_family_version_rebinds_later_association(self):
        suffix = uuid.uuid4().hex
        article_a = "__BACKDATE_A_" + suffix + "__"
        article_b = "__BACKDATE_B_" + suffix + "__"
        product_name = "__BACKDATE_PRODUCT_" + suffix + "__"
        product_id = batch_id = sale_id = None
        two_days_ago = date.today() - timedelta(days=2)
        yesterday = date.today() - timedelta(days=1)
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 100, 'fixa', %s) RETURNING id
                """, (article_a, two_days_ago))
                rule_a = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 200, 'fixa', %s) RETURNING id
                """, (article_b, two_days_ago))
                rule_b = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                conn.commit()

            configure_dose_product(product_id, rule_a, "sistema:test")
            configure_dose_product(product_id, rule_b, "sistema:test")
            with db_connection() as conn:
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES (CURRENT_DATE, 'Teste', %s, 1.8, %s)
                    RETURNING id
                """, (product_name, product_id))
                sale_id = cur.fetchone()["id"]
                batch_id = _import_historical_doses_tx(cur, [{
                    "artigo": article_b,
                    "gramas": None,
                    "tipo_dose": "peso",
                    "valid_from": yesterday.isoformat(),
                    "evidence_reference": "teste retroativo",
                }], "sistema:test", "backdated-switch.csv")
                conn.commit()

            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT hist.artigo, hist.gramas,
                           prd.valid_from, prd.valid_to
                    FROM produto_regra_dose_historico prd
                    JOIN gramas_gelado_historico hist
                      ON hist.id=prd.regra_dose_id
                    WHERE prd.produto_vendas_config_id=%s
                    ORDER BY prd.valid_from
                """, (product_id,))
                rows = cur.fetchall()
                self.assertEqual(rows[0][0], article_a)
                self.assertEqual(rows[0][3], yesterday)
                self.assertEqual(rows[1][0], article_b)
                self.assertIsNone(rows[1][1])
                self.assertEqual(rows[1][2], date.today())
                self.assertIsNone(rows[1][3])
                cur.execute(
                    "SELECT peso_vendido_kg FROM vendas_detalhe WHERE id=%s",
                    (sale_id,),
                )
                self.assertEqual(float(cur.fetchone()[0]), 1.8)
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                if sale_id is not None:
                    cur.execute(
                        "DELETE FROM vendas_detalhe WHERE id=%s", (sale_id,)
                    )
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                if batch_id is not None:
                    cur.execute(
                        "DELETE FROM gramas_gelado_import_audit WHERE id=%s",
                        (batch_id,),
                    )
                cur.execute("""
                    DELETE FROM gramas_gelado_historico
                    WHERE artigo IN (%s, %s)
                """, (article_a, article_b))
                conn.commit()

    def test_coverage_keeps_unmapped_sale_in_denominator(self):
        suffix = uuid.uuid4().hex
        article = "__COVERAGE_RULE_" + suffix + "__"
        product_name = "__COVERAGE_PRODUCT_" + suffix + "__"
        store = "__COVERAGE_STORE_" + suffix + "__"
        product_id = None
        yesterday = date.today() - timedelta(days=1)
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 100, 'fixa', %s) RETURNING id
                """, (article, yesterday))
                rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produto_regra_dose_historico (
                        produto_vendas_config_id, regra_dose_id,
                        valid_from, valid_to, created_by
                    ) VALUES (%s, %s, %s, NULL, 'sistema:test')
                    RETURNING id
                """, (product_id, rule_id, yesterday))
                association_id = cur.fetchone()[0]
                cur.execute("""
                    UPDATE produtos_vendas_config SET gelado_kpi=TRUE
                    WHERE id=%s
                """, (product_id,))
                cur.execute("""
                    UPDATE produto_regra_dose_historico SET valid_to=%s
                    WHERE id=%s
                """, (yesterday, association_id))
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES
                        (%s, %s, %s, 1, %s),
                        (CURRENT_DATE, %s, %s, 1, %s)
                """, (
                    yesterday, store, product_name, product_id,
                    store, product_name, product_id,
                ))
                conn.commit()

            coverage, _audits = get_historical_dose_coverage(store)
            current = next(
                row for row in coverage
                if row["month"] == date.today().strftime("%Y-%m")
            )
            self.assertEqual(current["total"], 2)
            self.assertEqual(current["covered"], 1)
            self.assertEqual(current["status"], "partial")
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM vendas_detalhe WHERE loja=%s", (store,)
                )
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE artigo=%s",
                    (article,),
                )
                conn.commit()

    def test_product_can_return_to_a_previously_used_rule(self):
        suffix = uuid.uuid4().hex
        article_a = "__RETURN_A_" + suffix + "__"
        article_b = "__RETURN_B_" + suffix + "__"
        product_name = "__RETURN_PRODUCT_" + suffix + "__"
        product_id = None
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 100, 'fixa', %s) RETURNING id
                """, (article_a, date.today() - timedelta(days=2)))
                rule_a = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 200, 'fixa', %s) RETURNING id
                """, (article_b, date.today() - timedelta(days=2)))
                rule_b = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produto_regra_dose_historico (
                        produto_vendas_config_id, regra_dose_id,
                        valid_from, valid_to, created_by
                    ) VALUES
                        (%s, %s, %s, %s, 'sistema:test'),
                        (%s, %s, CURRENT_DATE, NULL, 'sistema:test')
                """, (
                    product_id, rule_a, date.today() - timedelta(days=2),
                    date.today() - timedelta(days=1),
                    product_id, rule_b,
                ))
                cur.execute("""
                    UPDATE produtos_vendas_config SET gelado_kpi=TRUE
                    WHERE id=%s
                """, (product_id,))
                conn.commit()

            configure_dose_product(product_id, rule_a, "sistema:test")

            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*)
                    FROM produto_regra_dose_historico
                    WHERE produto_vendas_config_id=%s AND regra_dose_id=%s
                """, (product_id, rule_a))
                self.assertEqual(cur.fetchone()[0], 2)
                cur.execute("""
                    SELECT regra_dose_id
                    FROM produto_regra_dose_historico
                    WHERE produto_vendas_config_id=%s
                      AND CURRENT_DATE BETWEEN valid_from
                          AND COALESCE(valid_to, 'infinity'::date)
                """, (product_id,))
                self.assertEqual(cur.fetchone()[0], rule_a)
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                cur.execute("""
                    DELETE FROM gramas_gelado_historico
                    WHERE artigo IN (%s, %s)
                """, (article_a, article_b))
                conn.commit()

    def test_expired_association_keeps_new_sale_visible_as_unknown(self):
        suffix = uuid.uuid4().hex
        article = "__EXPIRY_RULE_" + suffix + "__"
        product_name = "__EXPIRY_PRODUCT_" + suffix + "__"
        product_id = None
        yesterday = date.today().fromordinal(date.today().toordinal() - 1)
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 100, 'fixa', %s) RETURNING id
                """, (article, yesterday))
                rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                conn.commit()
            configure_dose_product(product_id, rule_id, "sistema:test")
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE produto_regra_dose_historico
                    SET valid_to=%s
                    WHERE produto_vendas_config_id=%s
                """, (yesterday, product_id))
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES
                        (%s, 'Teste Expiração', %s, 1, %s),
                        (CURRENT_DATE, 'Teste Expiração', %s, 1, %s)
                """, (
                    yesterday, product_name, product_id,
                    product_name, product_id,
                ))
                conn.commit()

            result = get_consumo_gelado_mensal("Teste Expiração")
            product_rows = result[result["produto"] == product_name]
            self.assertEqual(len(product_rows), 2)
            self.assertEqual(
                float(product_rows["quantidade_vendida"].sum()), 2
            )
            unknown = product_rows[product_rows["consumo_incompleto"]]
            self.assertEqual(float(unknown.iloc[0]["quantidade_vendida"]), 1)
            pending, _rules = get_dose_product_configuration_queue()
            pending_row = next(row for row in pending if row["id"] == product_id)
            self.assertTrue(pending_row["dose_config_pendente"])
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT gelado_kpi, dose_config_pendente
                    FROM produtos_vendas_config WHERE id=%s
                """, (product_id,))
                self.assertEqual(cur.fetchone(), (True, False))
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    DELETE FROM vendas_detalhe WHERE loja='Teste Expiração'
                      AND produto=%s
                """, (product_name,))
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE artigo=%s",
                    (article,),
                )
                conn.commit()

    def test_confirmed_product_alias_uses_canonical_dated_dose(self):
        suffix = uuid.uuid4().hex
        article = "__ALIAS_RULE_" + suffix + "__"
        old_name = "__ALIAS_OLD_" + suffix + "__"
        canonical_name = "__ALIAS_CURRENT_" + suffix + "__"
        unrelated_name = "__NOT_GELATO_" + suffix + "__"
        old_product_id = canonical_product_id = unrelated_product_id = None
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 125, 'fixa', CURRENT_DATE)
                    RETURNING id
                """, (article,))
                rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s), (%s), (%s) RETURNING id, produto
                """, (old_name, canonical_name, unrelated_name))
                ids = {name: row_id for row_id, name in cur.fetchall()}
                old_product_id = ids[old_name]
                canonical_product_id = ids[canonical_name]
                unrelated_product_id = ids[unrelated_name]
                cur.execute("""
                    INSERT INTO produtos_vendas_aliases (nome_antigo, nome_atual)
                    VALUES (%s, %s)
                """, (old_name, canonical_name))
                conn.commit()
            configure_dose_product(
                canonical_product_id, rule_id, "sistema:test"
            )
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES
                        (CURRENT_DATE, 'Teste Alias Dose', %s, 4, %s),
                        (CURRENT_DATE, 'Teste Alias Dose', %s, 99, %s)
                """, (
                    old_name, old_product_id,
                    unrelated_name, unrelated_product_id,
                ))
                conn.commit()

            result = get_consumo_gelado_mensal("Teste Alias Dose")
            self.assertEqual(len(result), 1)
            self.assertEqual(result.iloc[0]["produto"], canonical_name)
            self.assertEqual(
                float(result.iloc[0]["quantidade_vendida"]), 4
            )
            self.assertEqual(float(result.iloc[0]["consumo_kg"]), 0.5)
            coverage, _audits = get_historical_dose_coverage(
                "Teste Alias Dose"
            )
            self.assertEqual(coverage[0]["total"], 1)
            self.assertEqual(coverage[0]["covered"], 1)
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM vendas_detalhe WHERE loja='Teste Alias Dose'"
                )
                cur.execute(
                    "DELETE FROM produtos_vendas_aliases WHERE nome_antigo=%s",
                    (old_name,),
                )
                for config_id in (
                    canonical_product_id, old_product_id, unrelated_product_id
                ):
                    if config_id is not None:
                        cur.execute("""
                            UPDATE produtos_vendas_config
                            SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                            WHERE id=%s
                        """, (config_id,))
                        cur.execute("""
                            DELETE FROM produto_regra_dose_historico
                            WHERE produto_vendas_config_id=%s
                        """, (config_id,))
                        cur.execute(
                            "DELETE FROM produtos_vendas_config WHERE id=%s",
                            (config_id,),
                        )
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE artigo=%s",
                    (article,),
                )
                conn.commit()

    def test_reassociation_preserves_history_and_old_family_import_stays_detached(self):
        suffix = uuid.uuid4().hex
        old_article = "__OLD_FAMILY_" + suffix + "__"
        new_article = "__NEW_FAMILY_" + suffix + "__"
        product_name = "__SWITCH_PRODUCT_" + suffix + "__"
        product_id = batch_id = None
        yesterday = date.today().fromordinal(date.today().toordinal() - 1)
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from, valid_to
                    ) VALUES (%s, 100, 'fixa', %s, CURRENT_DATE)
                    RETURNING id
                """, (old_article, yesterday))
                old_rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 120, 'fixa', CURRENT_DATE + 1)
                """, (old_article,))
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 200, 'fixa', CURRENT_DATE) RETURNING id
                """, (new_article,))
                new_rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                conn.commit()

            configure_dose_product(product_id, old_rule_id, "sistema:test")
            configure_dose_product(product_id, new_rule_id, "sistema:test")

            with db_connection() as conn:
                cur = conn.cursor(cursor_factory=RealDictCursor)
                batch_id = _import_historical_doses_tx(cur, [{
                    "artigo": old_article,
                    "gramas": "110",
                    "tipo_dose": "fixa",
                    "valid_from": date.today().isoformat(),
                    "evidence_reference": "teste de mudança",
                }], "sistema:test", "switch.csv")
                conn.commit()
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT hist.artigo, prd.valid_from, prd.valid_to
                    FROM produto_regra_dose_historico prd
                    JOIN gramas_gelado_historico hist
                      ON hist.id=prd.regra_dose_id
                    WHERE prd.produto_vendas_config_id=%s
                    ORDER BY prd.valid_from
                """, (product_id,))
                rows = cur.fetchall()
                self.assertEqual(rows[0], (old_article, yesterday, yesterday))
                self.assertEqual(rows[1][0], new_article)
                self.assertEqual(rows[1][1], date.today())
                self.assertEqual(len(rows), 2)
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                if batch_id is not None:
                    cur.execute(
                        "DELETE FROM gramas_gelado_import_audit WHERE id=%s",
                        (batch_id,),
                    )
                cur.execute("""
                    DELETE FROM gramas_gelado_historico
                    WHERE artigo IN (%s, %s)
                """, (old_article, new_article))
                conn.commit()

    def test_manual_selection_queues_then_explicit_mapping_activates(self):
        product_name = "__MANUAL_GELADO_" + uuid.uuid4().hex + "__"
        product_id = None
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id FROM gramas_gelado_historico
                    WHERE CURRENT_DATE BETWEEN valid_from
                        AND COALESCE(valid_to, 'infinity'::date)
                    ORDER BY id LIMIT 1
                """)
                rule = cur.fetchone()
                if not rule:
                    self.skipTest("No current dose rule available")
                rule_id = rule[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                conn.commit()

            queued = update_produtos_vendas_config_batch([{
                "id": product_id,
                "gelado_kpi": True,
                "pastelaria": False,
                "confeitaria": False,
            }])
            self.assertEqual(queued, 1)
            pending, _rules = get_dose_product_configuration_queue()
            self.assertIn(product_id, {row["id"] for row in pending})

            configure_dose_product(product_id, rule_id, "sistema:test")

            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT gelado_kpi, dose_config_pendente
                    FROM produtos_vendas_config WHERE id=%s
                """, (product_id,))
                self.assertEqual(cur.fetchone(), (True, False))
                cur.execute("""
                    SELECT COUNT(*) FROM produto_regra_dose_historico
                    WHERE produto_vendas_config_id=%s
                      AND CURRENT_DATE BETWEEN valid_from
                          AND COALESCE(valid_to, 'infinity'::date)
                """, (product_id,))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            if product_id is not None:
                with db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                    conn.commit()

    def test_weight_mapping_backfills_already_imported_quantity(self):
        product_name = "__MANUAL_WEIGHT_" + uuid.uuid4().hex + "__"
        article = product_name + "_RULE"
        product_id = sale_id = None
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, NULL, 'peso', CURRENT_DATE)
                    RETURNING id
                """, (article,))
                rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES (CURRENT_DATE, 'Teste', %s, 1.75, %s)
                    RETURNING id
                """, (product_name, product_id))
                sale_id = cur.fetchone()[0]
                conn.commit()

            update_produtos_vendas_config_batch([{
                "id": product_id, "gelado_kpi": True,
                "pastelaria": False, "confeitaria": False,
            }])
            configure_dose_product(product_id, rule_id, "sistema:test")
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT peso_vendido_kg FROM vendas_detalhe WHERE id=%s",
                    (sale_id,),
                )
                self.assertEqual(float(cur.fetchone()[0]), 1.75)
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                if sale_id is not None:
                    cur.execute(
                        "DELETE FROM vendas_detalhe WHERE id=%s", (sale_id,)
                    )
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                cur.execute(
                    "DELETE FROM gramas_gelado_historico WHERE artigo=%s",
                    (article,),
                )
                conn.commit()

    def test_same_day_rule_edit_preserves_current_product_mapping(self):
        suffix = uuid.uuid4().hex
        article = "__EDIT_RULE_" + suffix + "__"
        renamed = article + "_NEW"
        product_name = "__EDIT_PRODUCT_" + suffix + "__"
        grams_id = product_id = sale_id = None
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO gramas_gelado (artigo, gramas)
                    VALUES (%s, 100) RETURNING id
                """, (article,))
                grams_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from
                    ) VALUES (%s, 100, 'fixa', CURRENT_DATE)
                    RETURNING id
                """, (article,))
                rule_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto)
                    VALUES (%s) RETURNING id
                """, (product_name,))
                product_id = cur.fetchone()[0]
                conn.commit()
            configure_dose_product(product_id, rule_id, "sistema:test")
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade,
                        produto_vendas_config_id
                    ) VALUES (CURRENT_DATE, 'Teste', %s, 2.25, %s)
                    RETURNING id
                """, (product_name, product_id))
                sale_id = cur.fetchone()[0]
                conn.commit()

            self.assertTrue(
                update_gramas_gelado(grams_id, renamed, 120, "peso")
            )
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*)
                    FROM produto_regra_dose_historico prd
                    JOIN gramas_gelado_historico hist
                      ON hist.id=prd.regra_dose_id
                    WHERE prd.produto_vendas_config_id=%s
                      AND hist.artigo=%s
                      AND %s BETWEEN prd.valid_from
                          AND COALESCE(prd.valid_to, 'infinity'::date)
                """, (product_id, renamed, date.today()))
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute(
                    "SELECT peso_vendido_kg FROM vendas_detalhe WHERE id=%s",
                    (sale_id,),
                )
                self.assertEqual(float(cur.fetchone()[0]), 2.25)
        finally:
            with db_connection() as conn:
                cur = conn.cursor()
                if sale_id is not None:
                    cur.execute(
                        "DELETE FROM vendas_detalhe WHERE id=%s", (sale_id,)
                    )
                if product_id is not None:
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                        WHERE id=%s
                    """, (product_id,))
                    cur.execute("""
                        DELETE FROM produto_regra_dose_historico
                        WHERE produto_vendas_config_id=%s
                    """, (product_id,))
                    cur.execute(
                        "DELETE FROM produtos_vendas_config WHERE id=%s",
                        (product_id,),
                    )
                if grams_id is not None:
                    cur.execute(
                        "DELETE FROM gramas_gelado WHERE id=%s", (grams_id,)
                    )
                cur.execute("""
                    DELETE FROM gramas_gelado_historico
                    WHERE artigo IN (%s, %s)
                """, (article, renamed))
                conn.commit()


if __name__ == "__main__":
    unittest.main()