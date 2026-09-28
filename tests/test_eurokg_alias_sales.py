import os
import unittest
import uuid
from datetime import date

from db.connection import db_connection
from db.cache import invalidate_prefix
from db.producao import (
    calculate_kpi_annual,
    calculate_kpi_monthly,
    get_vendas_filtradas_df,
    get_vendas_produto_mensal,
)


@unittest.skipUnless(
    os.environ.get("DATABASE_URL"),
    "PostgreSQL integration test requires DATABASE_URL",
)
class EurokgAliasSalesTests(unittest.TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex
        self.suffix = suffix
        self.store_by_index = {
            0: "Bolhão",
            1: "Matosinhos",
        }
        confirmed_pairs = [
            ("Copo Mini", "Copo Piccolo"),
            ("Copo Pequeno (2 Sabores)", "Copo Classico"),
            ("Copo Medio (3 Sabores)", "Copo Grande"),
            ("Copo Grande (4 Sabores)", "Copo Maxi"),
            ("Cone Pequeno (2 Sabores)", "Cone Classico"),
            ("Cone Medio (3 Sabores)", "Cone Grande"),
        ]
        self.pairs = [
            (f"{old} [{suffix}]", f"{current} [{suffix}]")
            for old, current in confirmed_pairs
        ]
        self.ineligible_old = f"Alias destino não elegível [{suffix}]"
        self.ineligible_target = f"Produto não elegível [{suffix}]"
        self.cycle_a = f"Alias ciclo A [{suffix}]"
        self.cycle_b = f"Alias ciclo B [{suffix}]"
        self.missing_old = f"Alias destino inexistente [{suffix}]"
        self.unmapped_product = f"Produto sem alias [{suffix}]"
        self.config_ids = {}
        self.alias_names = [
            old for old, _current in self.pairs
        ] + [
            self.ineligible_old, self.cycle_a, self.cycle_b, self.missing_old,
        ]

        with db_connection() as conn:
            cur = conn.cursor()
            products = [
                (old, False) for old, _current in self.pairs
            ] + [
                (current, True) for _old, current in self.pairs
            ] + [
                (self.ineligible_old, False),
                (self.ineligible_target, False),
                (self.cycle_a, True),
                (self.cycle_b, False),
                (self.missing_old, False),
                (self.unmapped_product, False),
            ]
            for product, eligible in products:
                cur.execute("""
                    INSERT INTO produtos_vendas_config (produto, gelado_kpi)
                    VALUES (%s, %s)
                    RETURNING id
                """, (product, eligible))
                self.config_ids[product] = cur.fetchone()[0]

            aliases = [
                (old, current) for old, current in self.pairs
            ] + [
                (self.ineligible_old, self.ineligible_target),
                (self.cycle_a, self.cycle_b),
                (self.cycle_b, self.cycle_a),
                (self.missing_old, f"Produto ausente [{suffix}]"),
            ]
            cur.executemany("""
                INSERT INTO produtos_vendas_aliases (nome_antigo, nome_atual)
                VALUES (%s, %s)
            """, aliases)
            conn.commit()

    def tearDown(self):
        config_ids = list(self.config_ids.values())
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM vendas_detalhe "
                "WHERE produto_vendas_config_id=ANY(%s)",
                (config_ids,),
            )
            cur.execute(
                "DELETE FROM produtos_vendas_aliases WHERE nome_antigo=ANY(%s)",
                (self.alias_names,),
            )
            cur.execute(
                "DELETE FROM produtos_vendas_config WHERE id=ANY(%s)",
                (config_ids,),
            )
            conn.commit()

    def _insert_test_sales(self):
        expected_by_store = {"Bolhão": 0, "Matosinhos": 0}
        expected_by_month = {month: 0 for month in range(1, 6)}
        expected_by_store_month = {
            store: {month: 0 for month in range(1, 6)}
            for store in expected_by_store
        }
        with db_connection() as conn:
            cur = conn.cursor()
            for index, (old, _current) in enumerate(self.pairs):
                month = min(index + 1, 5)
                store = self.store_by_index[index % 2]
                value = (index + 1) * 10
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade, valor_euros,
                        produto_vendas_config_id
                    ) VALUES (%s, %s, %s, 1, %s, %s)
                """, (
                    date(2026, month, 10), store, old, value,
                    self.config_ids[old],
                ))
                expected_by_store[store] += value
                expected_by_month[month] += value
                expected_by_store_month[store][month] += value

            # An ordinary canonical-name sale remains eligible beside its
            # historical alias and must also be counted exactly once.
            canonical = self.pairs[0][1]
            cur.execute("""
                INSERT INTO vendas_detalhe (
                    data, loja, produto, quantidade, valor_euros,
                    produto_vendas_config_id
                ) VALUES (%s, 'Bolhão', %s, 1, 7, %s)
            """, (date(2026, 1, 10), canonical, self.config_ids[canonical]))
            expected_by_store["Bolhão"] += 7
            expected_by_month[1] += 7
            expected_by_store_month["Bolhão"][1] += 7

            excluded_sales = [
                (self.ineligible_old, "Bolhão", 1000),
                (self.cycle_a, "Matosinhos", 2000),
                (self.missing_old, "Bolhão", 3000),
                (self.unmapped_product, "Matosinhos", 4000),
            ]
            for product, store, value in excluded_sales:
                cur.execute("""
                    INSERT INTO vendas_detalhe (
                        data, loja, produto, quantidade, valor_euros,
                        produto_vendas_config_id
                    ) VALUES (%s, %s, %s, 1, %s, %s)
                """, (
                    date(2026, 5, 20), store, product, value,
                    self.config_ids[product],
                ))
            conn.commit()
        return expected_by_store, expected_by_month, expected_by_store_month

    @staticmethod
    def _summary_revenues():
        stores = (None, "Bolhão", "Matosinhos")
        invalidate_prefix("kpi_annual")
        annual = {
            store: calculate_kpi_annual(2026, store)
            for store in stores
        }
        invalidate_prefix("kpi_monthly")
        monthly = {
            (month, store): calculate_kpi_monthly(2026, month, store)
            for month in range(1, 6)
            for store in stores
        }
        return annual, monthly

    @staticmethod
    def _sales_by_store(dataframe):
        if dataframe.empty:
            return {}
        return dataframe.groupby("loja")["valor_euros"].sum().to_dict()

    def test_explicit_aliases_feed_daily_and_monthly_sales_once(self):
        start = date(2026, 1, 1)
        end = date(2026, 5, 31)
        before_daily = get_vendas_filtradas_df(
            "gelado_kpi", data_inicio=start, data_fim=end
        )
        before_by_store = self._sales_by_store(before_daily)
        before_global = float(before_daily["valor_euros"].sum())
        before_annual, before_monthly = self._summary_revenues()

        (
            expected_by_store,
            expected_by_month,
            expected_by_store_month,
        ) = self._insert_test_sales()

        after_daily = get_vendas_filtradas_df(
            "gelado_kpi", data_inicio=start, data_fim=end
        )
        after_by_store = self._sales_by_store(after_daily)
        after_global = float(after_daily["valor_euros"].sum())

        for store, expected in expected_by_store.items():
            self.assertAlmostEqual(
                float(after_by_store.get(store, 0))
                - float(before_by_store.get(store, 0)),
                expected,
            )
        self.assertAlmostEqual(after_global - before_global, sum(expected_by_store.values()))
        self.assertEqual(sum(expected_by_store.values()), sum(expected_by_month.values()))

        after_annual, after_monthly = self._summary_revenues()
        for store in (None, "Bolhão", "Matosinhos"):
            expected = (
                sum(expected_by_store.values())
                if store is None else expected_by_store[store]
            )
            self.assertAlmostEqual(
                sum(
                    after_annual[store][month]["vendas"]
                    - before_annual[store][month]["vendas"]
                    for month in range(1, 13)
                ),
                expected,
            )
        for month in range(1, 6):
            for store in (None, "Bolhão", "Matosinhos"):
                expected = (
                    expected_by_month[month]
                    if store is None
                    else expected_by_store_month[store][month]
                )
                self.assertAlmostEqual(
                    after_monthly[(month, store)]["vendas"]
                    - before_monthly[(month, store)]["vendas"],
                    expected,
                )

        for store in ("Bolhão", "Matosinhos"):
            monthly = get_vendas_produto_mensal(2026, store)
            store_index = 0 if store == "Bolhão" else 1
            expected_total = expected_by_store[store]
            self.assertAlmostEqual(
                monthly["grand_total"]
                - sum(
                    float(value)
                    for product, value in monthly["totais_produto"].items()
                    if not product.endswith(f"[{self.suffix}]")
                ),
                expected_total,
            )
            self.assertAlmostEqual(
                sum(
                    monthly["data"][current][month]
                    for index, (_old, current) in enumerate(self.pairs)
                    if index % 2 == store_index
                    for month in [min(index + 1, 5)]
                ),
                expected_total,
            )

        # Check the expected month buckets, including May, against the isolated
        # test products in the product/month breakdown.
        global_monthly = get_vendas_produto_mensal(2026)
        for month, expected in expected_by_month.items():
            test_products_total = sum(
                product_months[month]
                for product, product_months in global_monthly["data"].items()
                if product.endswith(f"[{self.suffix}]")
            )
            self.assertAlmostEqual(test_products_total, expected)


if __name__ == "__main__":
    unittest.main()