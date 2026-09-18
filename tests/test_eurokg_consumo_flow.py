import os
import unittest
import uuid
from datetime import date, timedelta

from flask import Blueprint, Flask
from werkzeug.datastructures import MultiDict

from db.connection import db_connection
from db.doseamento import get_historical_dose_coverage
from db.pastelaria import get_consumo_gelado_mensal
from flask_app.routes.eurokg import (
    _build_consumo_teorico_view,
    eurokg_bp,
)


def _make_flow_app():
    app = Flask(__name__, template_folder="../flask_app/templates")
    app.secret_key = "eurokg-flow-test"
    app.config["TESTING"] = True

    auth_bp = Blueprint("auth", __name__)

    @auth_bp.route("/login")
    def login():
        return "login", 200

    home_bp = Blueprint("home", __name__)

    @home_bp.route("/")
    def index():
        return "home", 200

    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(eurokg_bp, url_prefix="/eurokg")
    return app


@unittest.skipUnless(
    os.environ.get("DATABASE_URL"),
    "PostgreSQL integration test requires DATABASE_URL",
)
class EurokgConsumptionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_flow_app()

    def setUp(self):
        suffix = uuid.uuid4().hex
        self.family = f"Fluxo{suffix}"
        self.product_a = f"{self.family} Chocolate"
        self.product_b = f"{self.family} Morango"
        self.product_single = f"Isolado{suffix}"
        self.products = [
            self.product_a,
            self.product_b,
            self.product_single,
        ]
        month_start = date.today().replace(day=1)
        previous_month_end = month_start - timedelta(days=1)
        self.first_sale = previous_month_end.replace(day=1)
        self.effective_from = previous_month_end.replace(
            day=min(15, previous_month_end.day)
        )
        self.later_sale = previous_month_end.replace(
            day=min(20, previous_month_end.day)
        )
        self.product_ids = {}
        with db_connection() as conn:
            cur = conn.cursor()
            for product in self.products:
                cur.execute("""
                    INSERT INTO produtos_vendas_config (
                        produto, gelado_kpi, dose_config_pendente
                    ) VALUES (%s, TRUE, TRUE)
                    RETURNING id
                """, (product,))
                self.product_ids[product] = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO vendas_detalhe (
                    data, loja, produto, quantidade, valor_euros,
                    produto_vendas_config_id
                ) VALUES
                    (%s, 'Bolhão', %s, 2, 10, %s),
                    (%s, 'Bolhão', %s, 1, 5, %s),
                    (%s, 'Bolhão', %s, 3, 12, %s),
                    (%s, 'Matosinhos', %s, 4, 20, %s),
                    (%s, 'Matosinhos', %s, 5, 25, %s)
            """, (
                self.first_sale, self.product_a,
                self.product_ids[self.product_a],
                self.later_sale, self.product_a,
                self.product_ids[self.product_a],
                self.first_sale, self.product_b,
                self.product_ids[self.product_b],
                self.first_sale, self.product_a,
                self.product_ids[self.product_a],
                self.first_sale, self.product_single,
                self.product_ids[self.product_single],
            ))
            conn.commit()

    def tearDown(self):
        ids = list(self.product_ids.values())
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM vendas_detalhe "
                "WHERE produto_vendas_config_id=ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM produto_regra_dose_historico "
                "WHERE produto_vendas_config_id=ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM gramas_gelado_historico WHERE artigo=ANY(%s)",
                (self.products,),
            )
            cur.execute(
                "DELETE FROM gramas_gelado WHERE artigo=ANY(%s)",
                (self.products,),
            )
            cur.execute(
                "DELETE FROM produtos_vendas_config WHERE id=ANY(%s)",
                (ids,),
            )
            conn.commit()

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user"] = {
                "username": "eurokg-flow-test",
                "acesso_gestor": True,
                "acesso_eurokg": True,
            }
        return client

    def _post_config(self, rows):
        form = MultiDict([("loja_filter", "Global Porto")])
        for product, dose_type, grams, effective_from in rows:
            form.add("product_id", str(self.product_ids[product]))
            form.add("tipo_dose", dose_type)
            form.add("gramas", grams)
            form.add("data_efetiva", effective_from)
        with self._client() as client:
            response = client.post(
                "/eurokg/consumo/configurar-produto",
                data=form,
            )
            with client.session_transaction() as session:
                flashes = list(session.get("_flashes", []))
        return response, flashes

    def _product_view(self, store):
        frame = get_consumo_gelado_mensal(
            store,
            data_inicio=self.first_sale,
            data_fim=self.later_sale,
        )
        frame = frame[frame["produto"].isin(self.products)]
        return _build_consumo_teorico_view(frame)

    def test_complete_configuration_grouping_and_store_flow(self):
        response, flashes = self._post_config([
            (self.product_a, "fixa", "100", ""),
            (self.product_b, "fixa", "", ""),
            (self.product_single, "fixa", "200", ""),
        ])

        self.assertEqual(response.status_code, 302)
        self.assertTrue(any(category == "success" for category, _ in flashes))
        self.assertTrue(any(
            category == "info" and "sem alteração" in message
            for category, message in flashes
        ))

        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT pvc.produto, hist.gramas, prd.valid_from
                FROM produtos_vendas_config pvc
                LEFT JOIN produto_regra_dose_historico prd
                  ON prd.produto_vendas_config_id=pvc.id
                LEFT JOIN gramas_gelado_historico hist
                  ON hist.id=prd.regra_dose_id
                WHERE pvc.id=ANY(%s)
                ORDER BY pvc.produto
            """, (list(self.product_ids.values()),))
            configured = {row[0]: row[1:] for row in cur.fetchall()}
        self.assertEqual(
            configured[self.product_a],
            (100, self.first_sale),
        )
        self.assertEqual(
            configured[self.product_single],
            (200, self.first_sale),
        )
        self.assertEqual(configured[self.product_b], (None, None))

        bolhao_rows, _months, bolhao_qty, bolhao_kg = self._product_view(
            "Bolhão"
        )
        self.assertEqual(bolhao_qty, [6])
        self.assertEqual(bolhao_kg, [0.3])
        bolhao_group = next(
            row for row in bolhao_rows if row["kind"] == "group"
        )
        self.assertEqual(bolhao_group["product_count"], 2)
        self.assertEqual(bolhao_group["months"], [{
            "qty": 6,
            "kg": 0.3,
            "incomplete": True,
        }])

        matosinhos_rows, _months, matosinhos_qty, matosinhos_kg = (
            self._product_view("Matosinhos")
        )
        self.assertEqual(matosinhos_qty, [9])
        self.assertEqual(matosinhos_kg, [1.4])
        self.assertTrue(all(
            row["kind"] == "product" for row in matosinhos_rows
        ))
        self.assertNotIn(
            self.product_b,
            {
                row["produto"] for row in matosinhos_rows
                if row["kind"] == "product"
            },
        )

        global_rows, _months, global_qty, global_kg = self._product_view(None)
        self.assertEqual(global_qty, [15])
        self.assertEqual(global_kg, [1.7])
        self.assertEqual(
            sum(
                row["months"][0]["qty"]
                for row in global_rows
            ),
            global_qty[0],
        )

        coverage_stores = {
            "recovered": f"__FLOW_FULL_{uuid.uuid4().hex}__",
            "partial": f"__FLOW_PARTIAL_{uuid.uuid4().hex}__",
            "unknown": f"__FLOW_NONE_{uuid.uuid4().hex}__",
        }
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO vendas_detalhe (
                    data, loja, produto, quantidade,
                    produto_vendas_config_id
                ) VALUES
                    (%s, %s, %s, 1, %s),
                    (%s, %s, %s, 1, %s),
                    (%s, %s, %s, 1, %s),
                    (%s, %s, %s, 1, %s)
            """, (
                self.first_sale, coverage_stores["recovered"],
                self.product_a, self.product_ids[self.product_a],
                self.first_sale, coverage_stores["partial"],
                self.product_a, self.product_ids[self.product_a],
                self.first_sale, coverage_stores["partial"],
                self.product_b, self.product_ids[self.product_b],
                self.first_sale, coverage_stores["unknown"],
                self.product_b, self.product_ids[self.product_b],
            ))
            conn.commit()
        for expected_status, store in coverage_stores.items():
            coverage, _audits = get_historical_dose_coverage(
                store,
                data_inicio=self.first_sale,
                data_fim=self.later_sale,
            )
            self.assertEqual(coverage[0]["status"], expected_status)

        response, flashes = self._post_config([
            (self.product_a, "fixa", "150", ""),
        ])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any(category == "error" for category, _ in flashes))
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT hist.gramas
                FROM produto_regra_dose_historico prd
                JOIN gramas_gelado_historico hist
                  ON hist.id=prd.regra_dose_id
                WHERE prd.produto_vendas_config_id=%s
            """, (self.product_ids[self.product_a],))
            self.assertEqual(cur.fetchall(), [(100,)])

        response, flashes = self._post_config([
            (
                self.product_a,
                "fixa",
                "150",
                self.effective_from.isoformat(),
            ),
        ])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any(category == "success" for category, _ in flashes))
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT hist.gramas, prd.valid_from, prd.valid_to
                FROM produto_regra_dose_historico prd
                JOIN gramas_gelado_historico hist
                  ON hist.id=prd.regra_dose_id
                WHERE prd.produto_vendas_config_id=%s
                ORDER BY prd.valid_from
            """, (self.product_ids[self.product_a],))
            self.assertEqual(cur.fetchall(), [
                (
                    100,
                    self.first_sale,
                    self.effective_from - timedelta(days=1),
                ),
                (150, self.effective_from, None),
            ])

        bolhao_rows, _months, bolhao_qty, bolhao_kg = self._product_view(
            "Bolhão"
        )
        self.assertEqual(bolhao_qty, [6])
        self.assertEqual(bolhao_kg, [0.35])
        group = next(row for row in bolhao_rows if row["kind"] == "group")
        product_a = next(
            child for child in group["products"]
            if child["produto"] == self.product_a
        )
        self.assertEqual(product_a["months"], [{
            "qty": 3,
            "kg": 0.35,
            "incomplete": False,
        }])

        with self._client() as client:
            responses = {
                store: client.get(
                    "/eurokg/consumo",
                    query_string={
                        "loja": store,
                        "data_inicio": self.first_sale.isoformat(),
                        "data_fim": self.later_sale.isoformat(),
                    },
                )
                for store in ("Global Porto", "Bolhão", "Matosinhos")
            }
        self.assertTrue(all(
            response.status_code == 200 for response in responses.values()
        ))
        upper_tables = {
            store: response.data.split(b"<hr>", 1)[0]
            for store, response in responses.items()
        }
        self.assertIn(
            self.product_b.encode(),
            upper_tables["Bolhão"],
        )
        self.assertNotIn(
            self.product_b.encode(),
            upper_tables["Matosinhos"],
        )
        self.assertIn(
            self.product_single.encode(),
            upper_tables["Matosinhos"],
        )
        for response in responses.values():
            self.assertNotIn(b"Gramas/Un.", response.data)
            self.assertIn(b'data-consumo-family-toggle', response.data)
        self.assertIn(
            b'data-consumo-incompleto',
            responses["Bolhão"].data,
        )
        self.assertNotIn(
            b'data-consumo-incompleto',
            responses["Matosinhos"].data,
        )


if __name__ == "__main__":
    unittest.main()