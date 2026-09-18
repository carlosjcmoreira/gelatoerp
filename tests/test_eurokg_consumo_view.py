import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pandas as pd
from flask import Blueprint, Flask
from werkzeug.datastructures import MultiDict

from flask_app.routes.eurokg import (
    _build_consumo_teorico_view,
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
            ([(11, 125.5, "fixa"), (12, None, "peso")], 0, []),
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
            ([(11, 125.0, "fixa")], 1, []),
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

        self.assertEqual(changes, [(11, 125.0, "fixa")])
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

        self.assertIn(">Artigo faturado</th>", template)
        self.assertIn(">Tipo</th>", template)
        self.assertIn(">Gramas por unidade</th>", template)
        self.assertIn("Guardar configurações", template)
        self.assertNotIn('for="type-{{ product.id }}"', template)
        self.assertNotIn('for="grams-{{ product.id }}"', template)

    def test_unknown_historical_rule_is_not_rendered_as_zero(self):
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
        self.assertIsNone(rows[0]["months"][0]["kg"])
        self.assertIsNone(totals_kg[0])

    def test_mixed_known_and_unknown_month_keeps_total_unknown(self):
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
        self.assertIsNone(totals_kg[0])

    def test_zero_net_quantity_stays_unknown_when_weight_is_missing(self):
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
        self.assertIsNone(rows[0]["months"][0]["kg"])
        self.assertIsNone(totals_kg[0])