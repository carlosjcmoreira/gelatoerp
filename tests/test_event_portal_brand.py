"""Contracts for the single public Event form configuration."""

import io
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask
from PIL import Image

from db import eventos
from db import schema
from db import stores
from flask_app.routes.eventos import eventos_bp
from flask_app.services.event_portal_brand import (
    default_portal_brand, save_public_portal_logo, validate_brand_form,
    validate_portal_logo,
)
from werkzeug.security import check_password_hash


def _app():
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent.parent / "flask_app" / "templates"),
    )
    app.secret_key = "brand-test-secret"
    app.config["TESTING"] = True
    auth = Blueprint("auth", __name__)
    home = Blueprint("home", __name__)

    @auth.route("/login")
    def login():
        return "login"

    @home.route("/")
    def index():
        return "home"

    app.register_blueprint(auth)
    app.register_blueprint(home)
    app.register_blueprint(eventos_bp, url_prefix="/eventos")
    return app


class _Cursor:
    def __init__(self, results=()):
        self.queries = []
        self.results = list(results)

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.results.pop(0) if self.results else None


class _Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.commits = 0

    def cursor(self, **_kwargs):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class BrandValidationTests(unittest.TestCase):
    def test_lead_time_defaults_and_validation_boundaries(self):
        defaults = default_portal_brand()
        self.assertEqual(defaults["min_advance_days"], 0)
        self.assertTrue(defaults["short_notice_warning"])
        self.assertEqual(
            validate_brand_form({"min_advance_days": "0", "short_notice_warning": "Aviso"})[
                "min_advance_days"
            ],
            0,
        )
        self.assertEqual(
            validate_brand_form({"min_advance_days": "3650", "short_notice_warning": "Aviso"})[
                "min_advance_days"
            ],
            3650,
        )
        for value in ("-1", "3651", "1.5", "abc"):
            with self.assertRaises(ValueError):
                validate_brand_form({"min_advance_days": value, "short_notice_warning": "Aviso"})
        with self.assertRaises(ValueError):
            validate_brand_form({"min_advance_days": "1", "short_notice_warning": ""})

    def test_support_phone_and_history_code_are_validated_without_echoing_a_code(self):
        values = validate_brand_form({
            "support_phone": "+351 912 345 678",
            "portal_access_code": "novo-codigo_2026",
        })
        self.assertEqual(values["support_phone"], "+351912345678")
        self.assertEqual(values["portal_access_code"], "novo-codigo_2026")
        with self.assertRaisesRegex(ValueError, "indicativo internacional"):
            validate_brand_form({"support_phone": "912 345 678"})
        with self.assertRaisesRegex(ValueError, "código de consulta"):
            validate_brand_form({"portal_access_code": "abc"})
        editor = (
            Path(__file__).parent.parent / "flask_app" / "templates" /
            "eventos" / "configuracao.html"
        ).read_text()
        self.assertNotIn("080522", editor)

    def test_invalid_colour_is_rejected_and_privacy_is_not_configurable(self):
        with self.assertRaisesRegex(ValueError, "hexadecimal"):
            validate_brand_form({"brand_name": "Gelataria", "primary_color": "green"})

        values = validate_brand_form({"brand_name": "Gelataria", "primary_color": "#123456"})
        self.assertTrue(values["visible_fields"]["event_name"] is False)
        self.assertNotIn("privacy", values["visible_fields"])

    def test_logo_rejects_non_image_payload_and_saves_only_uuid_filename(self):
        fake = type("Upload", (), {"filename": "logo.png", "read": lambda _self, *_: b"#!/bin/sh"})()
        with self.assertRaisesRegex(ValueError, "imagem válida"):
            validate_portal_logo(fake)

        image = Image.new("RGB", (2, 2), "#112233")
        payload = io.BytesIO()
        image.save(payload, "PNG")
        upload = type("Upload", (), {
            "filename": "../../brand.png",
            "read": lambda _self, *_: payload.getvalue(),
        })()
        validated = validate_portal_logo(upload)
        with tempfile.TemporaryDirectory() as root:
            storage_name = save_public_portal_logo(validated, root)
            saved = Path(root) / "uploads" / "event_portal_brands" / storage_name
            self.assertTrue(saved.is_file())
            self.assertNotIn("brand", storage_name)
            self.assertTrue(storage_name.endswith(".png"))


class BrandPersistenceTests(unittest.TestCase):
    def test_migration_creates_brand_storage_and_event_association_additively(self):
        class MigrationCursor(_Cursor):
            def __init__(self):
                super().__init__([(True,)])

        cursor = MigrationCursor()
        connection = _Connection(cursor)
        with patch("db.schema.db_connection", return_value=connection):
            schema.run_migrations_eventos_customer_portal()

        statements = "\n".join(sql for sql, _ in cursor.queries)
        self.assertIn("CREATE TABLE IF NOT EXISTS event_portal_brand_configs", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS brand_store_id", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS min_advance_days", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS short_notice_warning", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS logo_data BYTEA", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS image_data BYTEA", statements)

    def test_fallback_and_saved_brand_remain_isolated_by_store(self):
        fallback = eventos._portal_brand_from_row(None)
        first = eventos._portal_brand_from_row({"store_id": 1, "brand_name": "Gelato Norte"})
        second = eventos._portal_brand_from_row({"store_id": 2, "brand_name": "Gelato Sul"})

        self.assertEqual(fallback["brand_name"], "Scoopy")
        self.assertEqual(first["brand_name"], "Gelato Norte")
        self.assertEqual(second["brand_name"], "Gelato Sul")
        self.assertEqual(first["store_id"], 1)
        self.assertEqual(second["store_id"], 2)

    def test_save_persists_the_selected_store_and_only_one_default(self):
        cursor = _Cursor([(True,)])
        connection = _Connection(cursor)
        values = validate_brand_form({"brand_name": "Gelato Norte"})
        with patch("db.eventos.db_connection", return_value=connection):
            eventos.save_portal_brand_config(7, values, logo_filename="logo.png", is_default=True)

        statements = "\n".join(sql for sql, _ in cursor.queries)
        self.assertIn("UPDATE event_portal_brand_configs SET is_default=FALSE", statements)
        self.assertIn("INSERT INTO event_portal_brand_configs", statements)
        insert_params = cursor.queries[-1][1]
        self.assertEqual(insert_params[0], 7)
        self.assertEqual(insert_params[1], "Gelato Norte")
        self.assertEqual(insert_params[2], "logo.png")

    def test_public_save_reuses_the_active_default_without_exposing_store_choice(self):
        cursor = _Cursor([(7,)])
        connection = _Connection(cursor)
        values = validate_brand_form({"brand_name": "Gelato Norte"})

        with patch("db.eventos.db_connection", return_value=connection):
            eventos.save_public_portal_brand_config(values, logo_filename="logo.png")

        statements = "\n".join(sql for sql, _ in cursor.queries)
        self.assertIn("FOR UPDATE OF s", statements)
        self.assertIn("UPDATE event_portal_brand_configs SET is_default=FALSE", statements)
        self.assertIn("INSERT INTO event_portal_brand_configs", statements)
        insert_params = cursor.queries[-1][1]
        self.assertEqual(insert_params[0], 7)
        self.assertEqual(insert_params[1], "Gelato Norte")

    def test_unified_save_persists_logo_bytes_inside_its_transaction(self):
        from unittest.mock import MagicMock

        cursor = MagicMock()
        cursor.fetchone.return_value = (7,)
        cursor.fetchall.return_value = []
        connection = _Connection(cursor)
        logo = {
            "payload": b"durable-logo",
            "content_type": "image/png",
            "extension": "png",
        }
        with patch("db.eventos.db_connection", return_value=connection):
            eventos.save_event_configuration(
                validate_brand_form({"brand_name": "Gelato Norte"}),
                "asset.png", [], [], [], "team", logo_asset=logo,
            )

        insert = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO event_portal_brand_configs" in call.args[0]
        )
        self.assertIn("logo_data", insert.args[0])
        self.assertEqual(insert.args[1][3], b"durable-logo")
        self.assertEqual(insert.args[1][4], "image/png")

    def test_unified_save_hashes_a_replaced_history_code(self):
        from unittest.mock import MagicMock

        cursor = MagicMock()
        cursor.fetchone.return_value = (7, None)
        cursor.fetchall.return_value = []
        connection = _Connection(cursor)
        values = validate_brand_form({
            "brand_name": "Gelato Norte",
            "portal_access_code": "novo-codigo",
        })
        with patch("db.eventos.db_connection", return_value=connection):
            eventos.save_event_configuration(values, "asset.png", [], [], [], "team")

        insert = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO event_portal_brand_configs" in call.args[0]
        )
        self.assertNotIn("novo-codigo", insert.args[1])
        self.assertTrue(check_password_hash(insert.args[1][16], "novo-codigo"))

    def test_brand_save_checks_fresh_store_status_inside_transaction(self):
        cursor = _Cursor([(False,)])
        connection = _Connection(cursor)
        with patch("db.eventos.db_connection", return_value=connection), \
             self.assertRaisesRegex(ValueError, "loja ativa"):
            eventos.save_portal_brand_config(
                7, validate_brand_form({"brand_name": "Gelato Norte"}), is_default=True
            )

        statements = "\n".join(sql for sql, _ in cursor.queries)
        self.assertIn("SELECT is_active FROM stores", statements)
        self.assertNotIn("INSERT INTO event_portal_brand_configs", statements)

    def test_default_brand_store_cannot_be_deactivated_after_locking_store_first(self):
        cursor = _Cursor([(True,), (1,)])
        connection = _Connection(cursor)
        with patch("db.stores.db_connection", return_value=connection), \
             self.assertRaisesRegex(ValueError, "Defina outra marca"):
            stores.toggle_store_active(7, False)

        statements = "\n".join(sql for sql, _ in cursor.queries)
        self.assertIn("SELECT is_active FROM stores", statements)
        self.assertIn("event_portal_brand_configs", statements)
        self.assertNotIn("UPDATE stores SET is_active", statements)
        self.assertLess(
            next(i for i, (sql, _) in enumerate(cursor.queries) if "SELECT is_active" in sql),
            next(i for i, (sql, _) in enumerate(cursor.queries) if "event_portal_brand_configs" in sql),
        )


class BrandRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def _user(self, client, **values):
        user = {"username": "team", "acesso_eventos": True, "acesso_administrativo": False}
        user.update(values)
        with client.session_transaction() as session:
            session["user"] = user

    def test_brand_editor_is_admin_only(self):
        with self.app.test_client() as client:
            self._user(client)
            response = client.get("/eventos/configuracao/portal-marca")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")

    def test_legacy_brand_url_is_admin_only_and_redirects_to_unified_editor(self):
        with self.app.test_client() as client:
            self._user(client)
            response = client.get("/eventos/configuracao/portal-marca")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")

        brand = default_portal_brand()
        brand["store_id"] = 1
        with self.app.test_client() as client, \
              patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("db.tiles.get_tile_visibility", return_value={}), \
             patch("db.tiles.get_tile_labels", return_value={}), \
             patch("db.tiles.get_tile_icons", return_value={}):
            self._user(client, acesso_administrativo=True)
            response = client.get("/eventos/configuracao/portal-marca?store_id=999")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/eventos/configuracao")

    def test_unified_editor_post_requires_csrf_before_any_write(self):
        with self.app.test_client() as client, \
              patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=default_portal_brand()), \
               patch("flask_app.routes.eventos.db.save_event_configuration") as save_config:
            self._user(client, acesso_administrativo=True)
            response = client.post("/eventos/configuracao", data={
                "brand_name": "Marca alterada",
            })

        self.assertEqual(response.status_code, 302)
        save_config.assert_not_called()

    def test_unified_editor_valid_post_saves_once(self):
        brand = default_portal_brand()
        with self.app.test_client() as client, \
              patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("flask_app.routes.eventos.db.save_event_configuration") as save_config, \
             patch("db.tiles.get_tile_visibility", return_value={}), \
             patch("db.tiles.get_tile_labels", return_value={}), \
             patch("db.tiles.get_tile_icons", return_value={}):
            self._user(client, acesso_administrativo=True)
            client.get("/eventos/configuracao")
            with client.session_transaction() as session:
                token = session["event_portal_brand_csrf"]
            response = client.post("/eventos/configuracao", data={
                "csrf_token": token, "brand_name": "Gelato da Praia",
                "min_advance_days": "3650", "short_notice_warning": "Aviso configurado.",
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/eventos/configuracao")
        save_config.assert_called_once()
        values = save_config.call_args.args[0]
        self.assertEqual(values["brand_name"], "Gelato da Praia")
        self.assertEqual(values["min_advance_days"], 3650)
        self.assertEqual(values["short_notice_warning"], "Aviso configurado.")

    def test_unified_editor_post_is_forbidden_for_non_admin(self):
        with self.app.test_client() as client:
            self._user(client, acesso_administrativo=False)
            response = client.post("/eventos/configuracao", data={"brand_name": "Nope"})
        self.assertEqual(response.status_code, 403)

    def test_editor_disables_only_its_blur_submit_scroll(self):
        root = Path(__file__).parent.parent
        template = (root / "flask_app" / "templates" / "eventos" / "configuracao_portal_marca.html").read_text()
        base_template = (root / "flask_app" / "templates" / "base.html").read_text()

        self.assertIn('data-ios-submit-scroll="false"', template)
        self.assertIn("scope.dataset.iosSubmitScroll === 'false'", base_template)

    def test_deprecated_marketing_controls_are_absent_from_brand_editor(self):
        root = Path(__file__).parent.parent
        template = (
            root / "flask_app" / "templates" / "eventos" /
            "configuracao_portal_marca.html"
        ).read_text()
        self.assertNotIn("marketing_consent", template)

    def test_public_form_honours_hidden_resource_preferences(self):
        brand = default_portal_brand()
        brand["visible_fields"]["resource_preferences"] = False
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("flask_app.routes.eventos.db.get_event_resources", return_value=[{
                 "code": "cart", "name": "Carrinho", "capacity_flavors": 4,
                 "public_capacity_flavors": 4, "image_url": None,
                 "public_description": None,
             }]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]):
            response = client.get("/eventos/pedido-evento")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("03 · Estrutura", page)
        self.assertNotIn('value="cart"', page)

    def test_public_request_uses_default_brand_and_server_sets_its_store(self):
        brand = default_portal_brand()
        brand.update({"store_id": 9, "brand_name": "Gelato da Praia", "primary_color": "#123456"})
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("flask_app.routes.eventos.db.get_portal_unavailable_dates", return_value=[]):
            page = client.get("/eventos/pedido-evento").get_data(as_text=True)
            self.assertIn("Gelato da Praia", page)
            csrf = client.session_transaction()
            with csrf as session:
                token = session["event_portal_csrf"]
            submission_identifier = __import__("re").search(
                r'name="submission_identifier" value="([^"]+)"', page
            ).group(1)
            with patch("flask_app.routes.eventos.resolve_event_address", return_value={}), \
                 patch("flask_app.routes.eventos.db.create_portal_event_request", return_value={"event_id": 4, "access_code": "code"} ) as create_request, \
                 patch("flask_app.routes.eventos.db.get_event_resources", return_value=[]), \
                  patch("flask_app.routes.eventos.db.consume_portal_rate_limit", return_value=True), \
                 patch("flask_app.routes.eventos.db.record_portal_access"):
                response = client.post("/eventos/pedido-evento", data={
                    "csrf_token": token, "event_type": "Aniversário", "estimated_guests": "20",
                    "started_at": str(int(time.time() * 1000) - 2000),
                    "servings_per_guest": "1", "flavours[]": "12",
                    "occurrence_date[]": "2027-01-01", "occurrence_start[]": "14:00",
                    "occurrence_venue[]": "Jardim", "occurrence_address[]": "Rua da Praia 1",
                    "client_name": "Cliente", "client_email": "cliente@example.com",
                    "client_phone": "+351 912345678", "privacy_accepted": "1",
                     "brand_store_id": "999", "submission_identifier": submission_identifier,
                })

        self.assertEqual(response.status_code, 302)
        submitted = create_request.call_args.args[0]
        self.assertEqual(submitted["brand_store_id"], 9)
        self.assertEqual(submitted["confirmation_message"], brand["confirmation_message"])

    def test_durable_logo_route_serves_database_bytes_and_hides_missing_legacy_logo(self):
        payload = b"\x89PNG\r\n\x1a\nimage"
        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos.db.get_public_portal_brand_logo",
            return_value={
                "logo_data": payload,
                "logo_content_type": "image/png",
                "logo_filename": "old.png",
            },
        ):
            response = client.get("/eventos/pedido-evento/marca/9/logo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, payload)
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos.db.get_public_portal_brand_logo",
            return_value={
                "logo_data": None,
                "logo_content_type": None,
                "logo_filename": "missing.png",
            },
        ):
            response = client.get("/eventos/pedido-evento/marca/9/logo")
        self.assertEqual(response.status_code, 404)

    def test_public_form_uses_white_fields_and_database_image_routes(self):
        brand = default_portal_brand()
        brand.update({"store_id": 9, "logo_filename": "logo.png"})
        resource = {
            "id": 4, "code": "cart", "name": "Carrinho",
            "capacity_flavors": 4, "public_capacity_flavors": 4,
            "image_url": "database:asset", "public_description": None,
            "public_customer_requirements": None,
        }
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("flask_app.routes.eventos.db.get_event_resources", return_value=[resource]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]):
            response = client.get("/eventos/pedido-evento")
        page = response.get_data(as_text=True)
        self.assertIn("--p-field:#fff", page)
        self.assertIn("/eventos/pedido-evento/marca/9/logo", page)
        self.assertIn("/eventos/pedido-evento/meios/4/imagem", page)

    def test_editor_warns_when_legacy_logo_needs_to_be_uploaded_again(self):
        template = (
            Path(__file__).parent.parent / "flask_app" / "templates" /
            "eventos" / "configuracao.html"
        ).read_text()
        self.assertIn("not brand.logo_is_durable", template)
        self.assertIn("Carregue novamente o logótipo", template)


if __name__ == "__main__":
    unittest.main()