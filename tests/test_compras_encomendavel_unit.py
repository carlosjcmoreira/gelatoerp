"""Mock-only tests for the catalogue orderability rules."""

import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Blueprint, Flask
from werkzeug.datastructures import MultiDict

from db.artigos import ensure_artigo_encomendavel, update_artigo_administrativo
from db.contagens_compras import _catalogue_rows, _normalise_lines
from db.encomendas_semanais import (
    get_available_weekly_articles,
    submit_weekly_order,
)
from db.pedidos_urgentes import (
    _validate_orderable_articles,
    get_available_urgent_articles,
)
from flask_app.routes.compras import _parse_optional_form_boolean


class TestOrderabilityMatrix(unittest.TestCase):
    def test_weekly_and_urgent_share_active_and_orderable_rule(self):
        cases = (
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        )
        for active, orderable, allowed in cases:
            with self.subTest(active=active, orderable=orderable):
                article = {
                    "produto": "Artigo de teste",
                    "ativo": active,
                    "encomendavel": orderable,
                    # No confirmed supplier must not change either result.
                    "fornecedor_oficial_id": None,
                }
                if allowed:
                    ensure_artigo_encomendavel(article)
                else:
                    with self.assertRaises(ValueError):
                        ensure_artigo_encomendavel(article)

    def test_urgent_validation_uses_same_matrix_without_supplier_gate(self):
        for active, orderable, allowed in (
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        ):
            with self.subTest(active=active, orderable=orderable):
                cursor = MagicMock()
                article = {
                    "produto": "Artigo de teste",
                    "ativo": active,
                    "encomendavel": orderable,
                    "fornecedor_oficial_id": None,
                }
                with patch(
                    "db.pedidos_urgentes._article_catalog_rows",
                    return_value={5: article},
                ):
                    if allowed:
                        self.assertEqual(
                            _validate_orderable_articles(cursor, {5: {}}),
                            {5: article},
                        )
                    else:
                        with self.assertRaises(ValueError):
                            _validate_orderable_articles(cursor, {5: {}})

    def test_count_validation_uses_active_only_and_allows_unconfirmed_supplier(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{
            "id": 5,
            "produto": "Artigo ativo bloqueado para compra",
            "unidade": "un",
            "ativo": True,
            "encomendavel": False,
            "fornecedor_oficial_id": None,
            "fornecedor_oficial_nome": None,
        }]
        catalogue = _catalogue_rows(cursor)
        requested, accepted = _normalise_lines(
            cursor, [{"artigo_id": 5, "quantidade": "2"}]
        )
        self.assertIn(5, requested)
        self.assertIn(5, accepted)
        self.assertFalse(accepted[5]["encomendavel"])
        sql = cursor.execute.call_args_list[0].args[0]
        self.assertIn("a.ativo = TRUE", sql)
        self.assertNotIn("a.encomendavel", sql)

    def test_selectors_filter_on_both_flags_not_supplier(self):
        for module, function in (
            ("db.encomendas_semanais", get_available_weekly_articles),
            ("db.pedidos_urgentes", get_available_urgent_articles),
        ):
            connection = MagicMock()
            cursor = connection.cursor.return_value
            cursor.fetchall.return_value = [{
                "id": 5,
                "ativo": True,
                "encomendavel": True,
                "fornecedor_oficial_id": None,
            }]
            with self.subTest(module=module), patch(
                f"{module}.db_connection",
                side_effect=self._connection_context(connection),
            ):
                articles = function()
            self.assertEqual(articles[0]["fornecedor_oficial_id"], None)
            query = cursor.execute.call_args.args[0]
            self.assertIn("a.ativo = TRUE AND a.encomendavel = TRUE", query)

    @staticmethod
    def _connection_context(connection):
        @contextmanager
        def context():
            yield connection
        return context


class TestOrderabilityFormAndCatalogueWrites(unittest.TestCase):
    def test_optional_boolean_parsing_is_explicit(self):
        self.assertTrue(_parse_optional_form_boolean(MultiDict({"flag": "1"}), "flag"))
        self.assertFalse(_parse_optional_form_boolean(MultiDict({"flag": "0"}), "flag"))
        self.assertIsNone(_parse_optional_form_boolean(MultiDict(), "flag"))
        with self.assertRaises(ValueError):
            _parse_optional_form_boolean(MultiDict({"flag": "maybe"}), "flag")

    @staticmethod
    def _connection_context(connection):
        @contextmanager
        def context():
            yield connection
        return context

    def test_individual_edit_audits_previous_new_actor_and_invalidates_after_commit(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (
            "Etiqueta antiga", "Produto", None, "un", None, None, None, None,
            None, None, "Por classificar", True,
        )
        with patch(
            "db.artigos.db_connection",
            side_effect=self._connection_context(connection),
        ), patch("db.artigos.invalidate_prefix") as invalidate:
            result = update_artigo_administrativo(
                8, None, "Produto", unidade="un", actor="gestor",
                encomendavel=False,
            )

        self.assertTrue(result["changed"])
        audit = next(
            call for call in cursor.execute.call_args_list
            if "artigos_administrativos_encomendavel_audit" in call.args[0]
        )
        self.assertEqual(audit.args[1], (8, True, False, "gestor"))
        connection.commit.assert_called_once()
        invalidate.assert_called_once_with("artigos_administrativos")

    def test_omitted_individual_field_preserves_disabled_value(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (
            "Etiqueta antiga", "Produto", None, "un", None, None, None, None,
            None, None, "Por classificar", False,
        )
        with patch(
            "db.artigos.db_connection",
            side_effect=self._connection_context(connection),
        ), patch("db.artigos.invalidate_prefix"):
            result = update_artigo_administrativo(
                8, None, "Produto novo", unidade="un", actor="gestor"
            )
        self.assertTrue(result["changed"])
        update = next(
            call for call in cursor.execute.call_args_list
            if call.args[0].lstrip().startswith("UPDATE artigos_administrativos")
        )
        self.assertFalse(update.args[1][5])
        self.assertFalse(any(
            "artigos_administrativos_encomendavel_audit" in call.args[0]
            for call in cursor.execute.call_args_list
        ))

    def test_bulk_edit_passes_explicit_false_and_preserves_omissions(self):
        from db.artigos import update_artigos_administrativos_bulk

        connection = MagicMock()
        changes = [
            {"artigo_id": 9, "produto": "Nove", "encomendavel": False},
            {"artigo_id": 3, "produto": "Três"},
        ]
        with patch(
            "db.artigos.db_connection",
            side_effect=self._connection_context(connection),
        ), patch(
            "db.artigos._update_artigo_administrativo_in_transaction",
            side_effect=[
                {"found": True, "changed": True, "origin_type": None},
                {"found": True, "changed": False, "origin_type": None},
            ],
        ) as update_row, patch("db.artigos.invalidate_prefix"):
            result = update_artigos_administrativos_bulk(changes, actor="gestor")

        self.assertEqual(result["updated"], 1)
        ordered_calls = update_row.call_args_list
        self.assertEqual(ordered_calls[0].args[1], 3)
        self.assertIsNone(ordered_calls[0].kwargs["encomendavel"])
        self.assertEqual(ordered_calls[1].args[1], 9)
        self.assertIs(ordered_calls[1].kwargs["encomendavel"], False)
        connection.commit.assert_called_once()


class TestWeeklySubmissionRevalidation(unittest.TestCase):
    @staticmethod
    def _connection_context(connection):
        @contextmanager
        def context():
            yield connection
        return context

    def test_revoked_draft_cannot_submit_and_keeps_its_lines(self):
        connection = MagicMock()
        order = {"id": 18, "status": "rascunho", "ciclo_domingo": date(2026, 9, 27)}
        line = {"id": 1, "artigo_id": 5, "produto_snapshot": "Farinha"}
        with patch(
            "db.encomendas_semanais.db_connection",
            side_effect=self._connection_context(connection),
        ), patch("db.encomendas_semanais._fetch_order", return_value=order), patch(
            "db.encomendas_semanais._fetch_lines", return_value=[line]
        ), patch(
            "db.encomendas_semanais._article_catalog_rows",
            return_value={5: {
                "id": 5, "produto": "Farinha", "ativo": True,
                "encomendavel": False,
            }},
        ):
            with self.assertRaisesRegex(ValueError, "novas encomendas"):
                submit_weekly_order(18, "gestor", today=date(2026, 9, 27))
        connection.commit.assert_not_called()
        self.assertEqual(line["artigo_id"], 5)

    def test_already_submitted_order_returns_before_catalogue_revalidation(self):
        connection = MagicMock()
        order = {"id": 18, "status": "submetida"}
        lines = [{"id": 1, "artigo_id": 5, "produto_snapshot": "Farinha"}]
        with patch(
            "db.encomendas_semanais.db_connection",
            side_effect=self._connection_context(connection),
        ), patch("db.encomendas_semanais._fetch_order", return_value=order), patch(
            "db.encomendas_semanais._fetch_lines", return_value=lines
        ), patch(
            "db.encomendas_semanais._article_catalog_rows"
        ) as catalogue:
            result = submit_weekly_order(18, "gestor")
        self.assertEqual(result["linhas"], lines)
        catalogue.assert_not_called()
        connection.commit.assert_called_once()


class TestCatalogueOrderabilityRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flask_app.routes.compras import compras_bp

        app = Flask(__name__)
        app.secret_key = "mock-catalogue-test"
        app.config["TESTING"] = True
        auth_bp = Blueprint("auth", __name__)
        home_bp = Blueprint("home", __name__)

        @auth_bp.route("/login")
        def login():
            return "login"

        @home_bp.route("/")
        def index():
            return "home"

        app.register_blueprint(auth_bp)
        app.register_blueprint(home_bp)
        app.register_blueprint(compras_bp, url_prefix="/compras")
        cls.app = app

    def test_catalogue_route_keeps_existing_permission_and_renders_field(self):
        article = {
            "id": 14,
            "produto": "Produto bloqueado para compra",
            "ativo": True,
            "encomendavel": False,
            "categoria_artigo": "Por classificar",
        }
        with self.app.test_client() as client:
            with client.session_transaction() as current:
                current["user"] = {"acesso_compras": True}
            with patch(
                "flask_app.routes.compras.get_artigos_administrativos",
                return_value=[article],
            ), patch(
                "flask_app.routes.compras.get_suppliers",
                return_value=[],
            ), patch(
                "flask_app.routes.compras.render_template",
                return_value="catalogue",
            ) as render:
                response = client.get("/compras/artigos")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(render.call_args.kwargs["artigos"][0]["encomendavel"])
        template = Path("flask_app/templates/compras/artigos.html").read_text()
        self.assertIn('name="encomendavel_{{ a.id }}"', template)
        self.assertIn("Ativo", template)
        self.assertIn("Inativo", template)

    def test_bulk_route_treats_zero_as_false_and_omission_as_unchanged(self):
        for include_field, expected in ((True, False), (False, None)):
            form = {
                "action": "bulk_edit",
                "artigo_id": "14",
                "produto_14": "Produto",
                "marca_14": "",
                "unidade_14": "un",
                "categoria_artigo_14": "Higiene e limpeza",
            }
            if include_field:
                form["encomendavel_14"] = "0"
            with self.subTest(include_field=include_field), self.app.test_client() as client:
                with client.session_transaction() as current:
                    current["user"] = {"acesso_compras": True}
                with patch(
                    "flask_app.routes.compras.update_artigos_administrativos_bulk",
                    return_value={"updated": 1, "unchanged": 0, "unresolved": 0},
                ) as update_bulk:
                    response = client.post("/compras/artigos", data=form)
            self.assertEqual(response.status_code, 302)
            change = update_bulk.call_args.args[0][0]
            self.assertEqual(change.get("encomendavel"), expected)


if __name__ == "__main__":
    unittest.main()