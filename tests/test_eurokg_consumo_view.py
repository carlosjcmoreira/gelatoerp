import unittest
from contextlib import ExitStack
from datetime import date
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
    root = Path(__file__).resolve().parents[1]
    app = Flask(
        __name__,
        template_folder=str(root / "flask_app" / "templates"),
    )
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

    def _patch_consumo_page(self, dose_products=None):
        if dose_products is None:
            dose_products = [{
                "id": 42,
                "produto": "Cone de Baunilha",
                "canonical_product": {
                    "id": 42,
                    "produto": "Cone de Baunilha",
                },
                "aliases": [],
                "dose_config_pendente": True,
                "tipo_dose": "fixa",
                "gramas": None,
                "valid_from": None,
                "first_sale": None,
                "dose_history_exists": False,
            }]
        patches = [
            patch(
                "flask_app.routes.eurokg._store_context",
                return_value=("Bolhão", "Bolhão", [{"name": "Bolhão"}]),
            ),
            patch(
                "flask_app.routes.eurokg._build_tabs",
                return_value=[],
            ),
            patch(
                "flask_app.routes.eurokg.get_consumo_gelado_mensal",
                return_value=pd.DataFrame(),
            ),
            patch(
                "flask_app.routes.eurokg._build_consumo_teorico_view",
                return_value=([], [], [], []),
            ),
            patch(
                "flask_app.routes.eurokg._consumo_incomplete_totals",
                return_value=[],
            ),
            patch(
                "flask_app.routes.eurokg.get_historical_dose_coverage",
                return_value=([], []),
            ),
            patch(
                "flask_app.routes.eurokg.get_historical_dose_preview",
                return_value=None,
            ),
            patch(
                "flask_app.routes.eurokg.get_dose_product_configuration_queue",
                return_value=(dose_products, []),
            ),
            patch(
                "flask_app.routes.eurokg.get_vendas_ao_peso_sem_peso_calculavel",
                return_value=[],
            ),
            patch(
                "flask_app.routes.eurokg.get_dose_alias_coverage_alerts",
                return_value=[],
            ),
        ]
        return patches

    def test_warning_focus_opens_the_stable_product_and_keeps_store_and_period(self):
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "test-manager",
                    "acesso_gestor": True,
                    "acesso_eurokg": True,
                }

            with ExitStack() as stack:
                for patcher in self._patch_consumo_page():
                    stack.enter_context(patcher)
                response = client.get(
                    "/eurokg/consumo?loja=Bolh%C3%A3o"
                    "&data_inicio=2026-08-01&data_fim=2026-08-31"
                    "&produto_id=42"
                )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="dose-product-42"', html)
        self.assertIn('data-dose-focus-target', html)
        self.assertIn("Artigo do aviso", html)
        self.assertIn('name="loja" value="Bolhão"', html)
        self.assertIn('value="2026-08-01"', html)
        self.assertIn('value="2026-08-31"', html)
        self.assertIn('name="produto_id" value="42"', html)
        self.assertIn('name="data_inicio" value="2026-08-01"', html)
        self.assertIn('name="data_fim" value="2026-08-31"', html)
        self.assertIn('<details data-dose-product-details data-dose-family open>', html)
        self.assertIn('data-dose-group="pending" open', html)
        self.assertIn("Não há artigos configurados.", html)
        self.assertIn("scrollIntoView", html)

    def test_dose_configuration_groups_keep_compact_rows_and_all_form_fields(self):
        products = [
            {
                "id": 42,
                "produto": "Cone de Baunilha",
                "canonical_product": {"id": 42, "produto": "Cone de Baunilha"},
                "aliases": [],
                "dose_config_pendente": True,
                "tipo_dose": "fixa",
                "gramas": None,
                "valid_from": None,
                "first_sale": date(2026, 2, 11),
                "dose_history_exists": False,
            },
            {
                "id": 43,
                "produto": "Taça Chocolate",
                "canonical_product": {"id": 43, "produto": "Taça Chocolate"},
                "aliases": [{"id": 84, "produto": "Taça Choc."}],
                "dose_config_pendente": False,
                "tipo_dose": "peso",
                "gramas": None,
                "valid_from": date(2026, 3, 10),
                "first_sale": date(2025, 9, 1),
                "dose_history_exists": True,
            },
        ]
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "test-manager",
                    "acesso_gestor": True,
                    "acesso_eurokg": True,
                }

            with ExitStack() as stack:
                for patcher in self._patch_consumo_page(products):
                    stack.enter_context(patcher)
                response = client.get("/eurokg/consumo")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        editor = html.split("data-dose-config-form", 1)[1].split("</form>", 1)[0]
        self.assertIn('<details class="mb-3" data-dose-group="pending" open>', editor)
        self.assertIn('<details class="mb-3" data-dose-group="configured">', editor)
        self.assertEqual(editor.count('class="table-responsive mt-2"'), 2)
        self.assertEqual(editor.count('style="min-width:720px"'), 2)
        self.assertEqual(editor.count("Faltam gramas"), 1)
        self.assertEqual(editor.count(">Configurado</span>"), 1)
        for field_name in ("product_id", "tipo_dose", "gramas", "data_efetiva"):
            self.assertEqual(editor.count(f'name="{field_name}"'), 2)
        self.assertNotIn('disabled', editor)
        self.assertIn('id="dose-product-42"', editor)
        self.assertIn('id="dose-product-43"', editor)
        self.assertIn('<option value="peso" selected>', editor)
        self.assertEqual(editor.count('name="gramas" value=""'), 2)
        self.assertIn("Primeira venda em 11/02/2026", editor)
        self.assertIn("Valor atual desde 10/03/2026", editor)
        self.assertIn("Origem histórica: 1 alias", editor)
        self.assertIn("obrigatória se alterar", editor)
        for product_id in (42, 43):
            row = editor.split(f'id="dose-product-{product_id}"', 1)[1].split("</tr>", 1)[0]
            self.assertIn("data-dose-product-details data-dose-family", row)
            self.assertNotIn("data-dose-product-details data-dose-family open", row)
            self.assertLess(
                row.index("data-dose-product-details"),
                row.index("Origem histórica:"),
            )
            self.assertLess(row.index("Origem histórica:"), row.index("</details>"))

    def test_warning_focus_opens_configured_group_and_product_details(self):
        product = {
            "id": 43,
            "produto": "Taça Chocolate",
            "canonical_product": {"id": 43, "produto": "Taça Chocolate"},
            "aliases": [],
            "dose_config_pendente": False,
            "tipo_dose": "peso",
            "gramas": None,
            "valid_from": date(2026, 3, 10),
            "first_sale": date(2025, 9, 1),
            "dose_history_exists": True,
        }
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "test-manager",
                    "acesso_gestor": True,
                    "acesso_eurokg": True,
                }

            with ExitStack() as stack:
                for patcher in self._patch_consumo_page([product]):
                    stack.enter_context(patcher)
                response = client.get("/eurokg/consumo?produto_id=43")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        editor = html.split("data-dose-config-form", 1)[1].split("</form>", 1)[0]
        self.assertIn('id="dose-product-43"', editor)
        self.assertIn('<details data-dose-product-details data-dose-family open>', editor)
        self.assertIn('<details class="mb-3" data-dose-group="configured" open>', editor)
        self.assertIn('data-dose-focus-target', editor)
        self.assertIn("Não há artigos pendentes de configuração.", editor)

    def test_non_manager_cannot_open_a_manager_product_focus(self):
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "store-user",
                    "acesso_gestor": False,
                    "acesso_eurokg": True,
                }

            with ExitStack() as stack:
                for patcher in self._patch_consumo_page():
                    stack.enter_context(patcher)
                response = client.get(
                    "/eurokg/consumo?loja=Bolh%C3%A3o&produto_id=42"
                )

        self.assertEqual(response.status_code, 403)

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

    def test_saving_focused_dose_preserves_warning_period_and_product(self):
        with self.app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = {
                    "username": "testuser",
                    "acesso_gestor": True,
                }

            with patch("flask_app.routes.eurokg.db_connection") as db_connection:
                conn = MagicMock(name="focused-dose-connection")
                db_connection.return_value.__enter__.return_value = conn
                with patch("flask_app.routes.eurokg.set_product_dose"):
                    response = client.post(
                        "/eurokg/consumo/configurar-produto",
                        data=MultiDict([
                            ("loja_filter", "Bolhão"),
                            ("data_inicio", "2026-08-01"),
                            ("data_fim", "2026-08-31"),
                            ("produto_id", "42"),
                            ("product_id", "42"),
                            ("tipo_dose", "fixa"),
                            ("gramas", "100"),
                        ]),
                    )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.location,
            "/eurokg/consumo?loja=Bolh%C3%A3o&data_inicio=2026-08-01"
            "&data_fim=2026-08-31&produto_id=42",
        )

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
        self.assertIn("data-dose-group=\"pending\"", template)
        self.assertIn("data-dose-group=\"configured\"", template)
        self.assertIn("data-dose-product-details", template)
        self.assertEqual(template.count("Faltam gramas"), 1)
        self.assertEqual(template.count(">Configurado</span>"), 1)
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