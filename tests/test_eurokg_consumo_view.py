import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pandas as pd
from flask import Blueprint, Flask
from werkzeug.datastructures import MultiDict

from flask_app.routes.eurokg import (
    _build_consumo_teorico_view,
    _consumo_incomplete_totals,
    _consumo_family_parts,
    _parse_product_dose_batch,
    eurokg_bp,
)


def _make_eurokg_test_app():
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
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


class ConsumoTeoricoViewTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_eurokg_test_app()

    def test_batch_parser_keeps_valid_rows_and_weight_rows(self):
        form = MultiDict([
            ("product_id", "11"),
            ("product_id", "12"),
            ("tipo_dose", "fixa"),
            ("tipo_dose", "peso"),
            ("gramas", "125,5"),
            ("gramas", ""),
        ])

        self.assertEqual(
            _parse_product_dose_batch(form),
            ([(11, 125.5, "fixa", None), (12, None, "peso", None)], 0, []),
        )

    def test_batch_parser_skips_empty_fixed_row(self):
        form = MultiDict([
            ("product_id", "11"),
            ("product_id", "12"),
            ("tipo_dose", "fixa"),
            ("tipo_dose", "fixa"),
            ("gramas", "125"),
            ("gramas", ""),
        ])

        self.assertEqual(
            _parse_product_dose_batch(form),
            ([(11, 125.0, "fixa", None)], 1, []),
        )

    def test_batch_parser_reports_invalid_row_without_discarding_valid_row(self):
        form = MultiDict([
            ("product_id", "11"),
            ("product_id", "12"),
            ("tipo_dose", "fixa"),
            ("tipo_dose", "fixa"),
            ("gramas", "125"),
            ("gramas", "abc"),
        ])

        changes, skipped_empty, invalid_rows = _parse_product_dose_batch(form)

        self.assertEqual(changes, [(11, 125.0, "fixa", None)])
        self.assertEqual(skipped_empty, 0)
        self.assertEqual(invalid_rows, [
            "Artigo 12: indique um valor de gramas válido."
        ])

    def test_authenticated_batch_post_saves_fixed_and_weight_rows(self):
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "testuser",
                    "acesso_gestor": True,
                }

            with patch("flask_app.routes.eurokg.db_connection") as db_connection:
                conn = MagicMock(name="batch-connection")
                db_connection.return_value.__enter__.return_value = conn
                with patch(
                    "flask_app.routes.eurokg.set_product_dose"
                ) as set_product_dose:
                    response = client.post(
                        "/eurokg/consumo/configurar-produto",
                        data=MultiDict([
                            ("loja_filter", "Global Porto"),
                            ("product_id", "11"),
                            ("product_id", "12"),
                            ("tipo_dose", "fixa"),
                            ("tipo_dose", "peso"),
                            ("gramas", "125,5"),
                            ("gramas", ""),
                        ]),
                    )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.location,
            "/eurokg/consumo?loja=Global+Porto",
        )
        self.assertEqual(
            set_product_dose.call_args_list,
            [
                call(
                    11, 125.5, "fixa", "testuser",
                    source="Euro/kg", conn=conn
                ),
                call(
                    12, None, "peso", "testuser",
                    source="Euro/kg", conn=conn
                ),
            ],
        )
        conn.commit.assert_called_once_with()
        conn.rollback.assert_not_called()

    def test_unexpected_batch_failure_rolls_back_all_rows(self):
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "testuser",
                    "acesso_gestor": True,
                }

            with patch("flask_app.routes.eurokg.db_connection") as db_connection:
                conn = MagicMock(name="batch-connection")
                db_connection.return_value.__enter__.return_value = conn
                with patch(
                    "flask_app.routes.eurokg.set_product_dose",
                    side_effect=[None, RuntimeError("database unavailable")],
                ) as set_product_dose:
                    with self.assertRaisesRegex(
                        RuntimeError, "database unavailable"
                    ):
                        client.post(
                            "/eurokg/consumo/configurar-produto",
                            data=MultiDict([
                                ("loja_filter", "Global Porto"),
                                ("product_id", "11"),
                                ("product_id", "12"),
                                ("tipo_dose", "fixa"),
                                ("tipo_dose", "peso"),
                                ("gramas", "125,5"),
                                ("gramas", ""),
                            ]),
                        )

        self.assertEqual(set_product_dose.call_count, 2)
        conn.rollback.assert_called_once_with()
        conn.commit.assert_not_called()

    def test_authenticated_batch_post_saves_valid_row_and_warns_about_invalid_row(self):
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "testuser",
                    "acesso_gestor": True,
                }

            with patch("flask_app.routes.eurokg.db_connection") as db_connection:
                conn = MagicMock(name="batch-connection")
                db_connection.return_value.__enter__.return_value = conn
                with patch(
                    "flask_app.routes.eurokg.set_product_dose"
                ) as set_product_dose:
                    response = client.post(
                        "/eurokg/consumo/configurar-produto",
                        data=MultiDict([
                            ("loja_filter", "Global Porto"),
                            ("product_id", "11"),
                            ("product_id", "12"),
                            ("tipo_dose", "fixa"),
                            ("tipo_dose", "fixa"),
                            ("gramas", "125,5"),
                            ("gramas", "abc"),
                        ]),
                    )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set_product_dose.call_args_list,
            [call(
                11, 125.5, "fixa", "testuser",
                source="Euro/kg", conn=conn
            )],
        )
        conn.commit.assert_called_once_with()
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "testuser",
                    "acesso_gestor": True,
                }
            with patch("flask_app.routes.eurokg.db_connection") as db_connection:
                db_connection.return_value.__enter__.return_value = MagicMock()
                with patch("flask_app.routes.eurokg.set_product_dose"):
                    client.post(
                        "/eurokg/consumo/configurar-produto",
                        data=MultiDict([
                            ("loja_filter", "Global Porto"),
                            ("product_id", "11"),
                            ("product_id", "12"),
                            ("tipo_dose", "fixa"),
                            ("tipo_dose", "fixa"),
                            ("gramas", "125,5"),
                            ("gramas", "abc"),
                        ]),
                    )
            with client.session_transaction() as session:
                flashes = session.get("_flashes", [])
        self.assertTrue(any(category == "success" for category, _ in flashes))
        self.assertTrue(any(category == "warning" for category, _ in flashes))

        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "testuser",
                    "acesso_gestor": True,
                }
            with patch("flask_app.routes.eurokg.db_connection") as db_connection:
                conn = MagicMock(name="empty-batch-connection")
                db_connection.return_value.__enter__.return_value = conn
                with patch(
                    "flask_app.routes.eurokg.set_product_dose"
                ) as set_product_dose:
                    response = client.post(
                        "/eurokg/consumo/configurar-produto",
                        data=MultiDict([
                            ("loja_filter", "Global Porto"),
                            ("product_id", "11"),
                            ("product_id", "12"),
                            ("tipo_dose", "fixa"),
                            ("tipo_dose", "fixa"),
                            ("gramas", ""),
                            ("gramas", ""),
                        ]),
                    )

        self.assertEqual(response.status_code, 302)
        set_product_dose.assert_not_called()
        db_connection.assert_not_called()

    def test_configuration_table_has_one_submit_button_and_shared_headers(self):
        template = Path(
            "flask_app/templates/eurokg/consumo_teorico.html"
        ).read_text(encoding="utf-8")

        self.assertIn(">Família canónica</th>", template)
        self.assertIn(">Tipo</th>", template)
        self.assertIn(">Gramas por unidade</th>", template)
        self.assertIn("Guardar configurações", template)
        self.assertIn("Origem histórica:", template)
        self.assertIn("data-dose-family", template)
        self.assertIn("(ID {{ alias.id }})", template)
        self.assertNotIn('name="alias_id"', template)
        self.assertNotIn('for="type-{{ product.id }}"', template)
        self.assertNotIn('for="grams-{{ product.id }}"', template)

    def test_consumption_table_removes_grams_and_has_accessible_family_toggle(self):
        template = Path(
            "flask_app/templates/eurokg/consumo_teorico.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("<th>Gramas/Un.</th>", template)
        self.assertIn("Vendas ao peso sem peso calculável", template)
        self.assertIn(">Data</th>", template)
        self.assertIn(">Loja</th>", template)
        self.assertIn(">Artigo</th>", template)
        self.assertIn(">Quantidade</th>", template)
        self.assertIn("não altera vendas automaticamente", template)
        self.assertIn("data-consumo-family-toggle", template)
        self.assertIn('aria-expanded="false"', template)
        self.assertIn('aria-controls="{{ item.group_id }}"', template)
        self.assertIn('<tbody id="{{ item.group_id }}"', template)
        self.assertIn('data-consumo-family-details', template)
        self.assertIn('hidden>', template)

    def test_manager_page_distinguishes_alias_identity_from_missing_dose(self):
        template = Path(
            "flask_app/templates/eurokg/consumo_teorico.html"
        ).read_text(encoding="utf-8")

        self.assertIn("Identidade de alias bloqueada", template)
        self.assertIn("Identidade ambígua", template)
        self.assertIn("Alias inválido", template)
        self.assertIn("Faltam gramas", template)
        self.assertIn("não são feitas correspondências aproximadas", template)
        self.assertIn("dose_alias_alerts", template)
        self.assertIn("Mostrar apenas consumos incompletos", template)
        self.assertIn("data-consumo-filter-incompletos", template)
        self.assertIn("aparecem provisoriamente como 0", template)
        self.assertNotIn("else '—'", template)

    def test_repeated_normalized_first_token_builds_collapsible_family(self):
        frame = pd.DataFrame([
            {
                "produto": "Bíscoito   Gelado",
                "mes": "2026-01",
                "quantidade_vendida": 2,
                "gramas_por_unidade": 100,
                "consumo_kg": 0.2,
                "consumo_incompleto": False,
            },
            {
                "produto": "biscoito Bolt",
                "mes": "2026-01",
                "quantidade_vendida": 3,
                "gramas_por_unidade": 100,
                "consumo_kg": 0.3,
                "consumo_incompleto": False,
            },
        ])

        rows, _labels, totals_qty, totals_kg = _build_consumo_teorico_view(frame)

        self.assertEqual(len(rows), 1)
        group = rows[0]
        self.assertEqual(group["kind"], "group")
        self.assertEqual(group["family"], "Biscoito")
        self.assertEqual(group["product_count"], 2)
        self.assertEqual(
            [product["produto"] for product in group["products"]],
            ["biscoito Bolt", "Bíscoito   Gelado"],
        )
        self.assertEqual(group["months"], [{
            "qty": 5,
            "kg": 0.5,
            "incomplete": False,
        }])
        self.assertEqual(totals_qty, [5])
        self.assertEqual(totals_kg, [0.5])

    def test_named_families_ignore_case_accents_and_extra_spaces(self):
        cases = {
            "  BÍSCOITO   Gelado": ("biscoito", "Biscoito"),
            "nIVOTTO Chocolate": ("nivotto", "Nivotto"),
            "CAIXA  0,5L": ("caixa", "Caixa"),
            "Pálito Morango": ("palito", "Palito"),
        }

        for product_name, expected in cases.items():
            with self.subTest(product_name=product_name):
                self.assertEqual(_consumo_family_parts(product_name), expected)

    def test_family_kg_adds_known_variants_and_zero_for_unknown_variants(self):
        frame = pd.DataFrame([
            {
                "produto": "Palito Chocolate",
                "mes": "2026-01",
                "quantidade_vendida": 2,
                "gramas_por_unidade": 100,
                "consumo_kg": 0.2,
                "consumo_incompleto": False,
            },
            {
                "produto": "PALITO Morango",
                "mes": "2026-01",
                "quantidade_vendida": 1,
                "gramas_por_unidade": float("nan"),
                "consumo_kg": float("nan"),
                "consumo_incompleto": True,
            },
        ])

        rows, _labels, totals_qty, totals_kg = _build_consumo_teorico_view(frame)

        self.assertEqual(rows[0]["months"], [{
            "qty": 3,
            "kg": 0.2,
            "incomplete": True,
        }])
        self.assertEqual(totals_qty, [3])
        self.assertEqual(totals_kg, [0.2])

    def test_incomplete_consumption_is_kept_on_product_and_family_months(self):
        frame = pd.DataFrame([
            {
                "produto": "Palito Chocolate",
                "mes": "2026-01",
                "quantidade_vendida": 2,
                "gramas_por_unidade": 100,
                "consumo_kg": 0.2,
                "consumo_incompleto": False,
            },
            {
                "produto": "PALITO Morango",
                "mes": "2026-01",
                "quantidade_vendida": 1,
                "gramas_por_unidade": float("nan"),
                "consumo_kg": float("nan"),
                "consumo_incompleto": True,
            },
        ])

        rows, _labels, _qty, _kg = _build_consumo_teorico_view(frame)

        group = rows[0]
        self.assertTrue(group["months"][0]["incomplete"])
        self.assertEqual(
            {
                product["produto"]: product["months"][0]["incomplete"]
                for product in group["products"]
            },
            {
                "PALITO Morango": True,
                "Palito Chocolate": False,
            },
        )
        self.assertTrue(group["incomplete"])
        self.assertFalse(group["products"][0]["incomplete"])
        self.assertTrue(group["products"][1]["incomplete"])

    def test_known_zero_is_not_marked_as_incomplete(self):
        frame = pd.DataFrame([{
            "produto": "Copo",
            "mes": "2026-01",
            "quantidade_vendida": 0,
            "gramas_por_unidade": 100,
            "consumo_kg": 0,
            "consumo_incompleto": False,
        }])

        rows, _labels, _qty, _kg = _build_consumo_teorico_view(frame)

        self.assertEqual(rows[0]["months"], [{
            "qty": 0,
            "kg": 0,
            "incomplete": False,
        }])

    def test_totals_keep_monthly_incomplete_signal_separate_from_kg(self):
        frame = pd.DataFrame([
            {
                "produto": "Copo",
                "mes": "2026-01",
                "quantidade_vendida": 2,
                "consumo_kg": 0.2,
                "consumo_incompleto": False,
            },
            {
                "produto": "Novo",
                "mes": "2026-01",
                "quantidade_vendida": 1,
                "consumo_kg": float("nan"),
                "consumo_incompleto": True,
            },
            {
                "produto": "Copo",
                "mes": "2026-02",
                "quantidade_vendida": 0,
                "consumo_kg": 0,
                "consumo_incompleto": False,
            },
        ])

        _rows, _labels, _qty, totals_kg = _build_consumo_teorico_view(frame)

        self.assertEqual(totals_kg, [0.2, 0])
        self.assertEqual(_consumo_incomplete_totals(frame), [True, False])

    def test_single_product_family_stays_an_individual_row(self):
        frame = pd.DataFrame([{
            "produto": "Caixa 0,5L",
            "mes": "2026-01",
            "quantidade_vendida": 1,
            "gramas_por_unidade": 500,
            "consumo_kg": 0.5,
            "consumo_incompleto": False,
        }])

        rows, _labels, _totals_qty, _totals_kg = (
            _build_consumo_teorico_view(frame)
        )

        self.assertEqual(rows[0]["kind"], "product")
        self.assertEqual(rows[0]["produto"], "Caixa 0,5L")

    def test_batch_parser_accepts_an_effective_date_for_existing_rule(self):
        form = MultiDict([
            ("product_id", "11"),
            ("tipo_dose", "fixa"),
            ("gramas", "125"),
            ("data_efetiva", "2026-01-15"),
        ])

        changes, skipped_empty, invalid_rows = _parse_product_dose_batch(form)

        self.assertEqual(changes[0][:3], (11, 125.0, "fixa"))
        self.assertEqual(changes[0][3].isoformat(), "2026-01-15")
        self.assertEqual(skipped_empty, 0)
        self.assertEqual(invalid_rows, [])

    def test_batch_parser_rejects_future_effective_date(self):
        form = MultiDict([
            ("product_id", "11"),
            ("tipo_dose", "fixa"),
            ("gramas", "125"),
            ("data_efetiva", "2999-01-15"),
        ])

        _changes, _skipped_empty, invalid_rows = _parse_product_dose_batch(form)

        self.assertEqual(len(invalid_rows), 1)
        self.assertIn("não pode ser futura", invalid_rows[0])

    def test_unknown_historical_rule_is_rendered_as_provisional_zero(self):
        frame = pd.DataFrame([
            {
                "produto": "Copo",
                "mes": "2026-01",
                "quantidade_vendida": 3,
                "gramas_por_unidade": float("nan"),
                "consumo_kg": float("nan"),
            },
        ])
        rows, _labels, _qty, totals_kg = _build_consumo_teorico_view(frame)
        self.assertEqual(rows[0]["months"][0]["kg"], 0)
        self.assertTrue(rows[0]["months"][0]["incomplete"])
        self.assertEqual(totals_kg[0], 0)

    def test_mixed_known_and_unknown_month_keeps_known_total(self):
        frame = pd.DataFrame([
            {
                "produto": "Copo",
                "mes": "2026-09",
                "quantidade_vendida": 2,
                "gramas_por_unidade": 100,
                "consumo_kg": 0.2,
            },
            {
                "produto": "Novo",
                "mes": "2026-09",
                "quantidade_vendida": 1,
                "gramas_por_unidade": float("nan"),
                "consumo_kg": float("nan"),
            },
        ])
        _rows, _labels, qty, totals_kg = _build_consumo_teorico_view(frame)
        self.assertEqual(qty, [3])
        self.assertEqual(totals_kg, [0.2])

    def test_zero_net_quantity_with_missing_weight_is_provisional_zero(self):
        frame = pd.DataFrame([
            {
                "produto": "Gelado ao peso",
                "mes": "2026-09",
                "quantidade_vendida": 0,
                "gramas_por_unidade": float("nan"),
                "consumo_kg": float("nan"),
                "consumo_incompleto": True,
            },
        ])
        rows, _labels, qty, totals_kg = _build_consumo_teorico_view(frame)
        self.assertEqual(qty, [0])
        self.assertEqual(rows[0]["months"][0]["kg"], 0)
        self.assertTrue(rows[0]["months"][0]["incomplete"])
        self.assertEqual(totals_kg[0], 0)