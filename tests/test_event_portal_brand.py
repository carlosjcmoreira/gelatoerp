"""Contracts for the single public Event form configuration."""

import io
import tempfile
import unittest
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

    def test_admin_can_open_single_form_editor_with_preview_and_public_link(self):
        brand = default_portal_brand()
        brand["store_id"] = 1
        with self.app.test_client() as client, \
              patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("db.tiles.get_tile_visibility", return_value={}), \
             patch("db.tiles.get_tile_labels", return_value={}), \
             patch("db.tiles.get_tile_icons", return_value={}):
            self._user(client, acesso_administrativo=True)
            response = client.get("/eventos/configuracao/portal-marca?store_id=999")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Formulário público de Eventos", page)
        self.assertIn("Pré-visualização", page)
        self.assertIn('href="/eventos/pedido-evento"', page)
        self.assertIn('target="_blank"', page)
        self.assertNotIn("store-picker", page)
        self.assertNotIn('name="store_id"', page)

    def test_brand_editor_post_requires_csrf_before_any_write(self):
        with self.app.test_client() as client, \
              patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=default_portal_brand()), \
              patch("flask_app.routes.eventos.db.save_public_portal_brand_config") as save_brand:
            self._user(client, acesso_administrativo=True)
            response = client.post("/eventos/configuracao/portal-marca", data={
                "brand_name": "Marca alterada",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn("página expirou", response.get_data(as_text=True))
        save_brand.assert_not_called()

    def test_saved_editor_values_update_the_single_public_configuration(self):
        brand = default_portal_brand()
        with self.app.test_client() as client, \
              patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
              patch("flask_app.routes.eventos.db.save_public_portal_brand_config") as save_brand, \
             patch("db.tiles.get_tile_visibility", return_value={}), \
             patch("db.tiles.get_tile_labels", return_value={}), \
             patch("db.tiles.get_tile_icons", return_value={}):
            self._user(client, acesso_administrativo=True)
            client.get("/eventos/configuracao/portal-marca")
            with client.session_transaction() as session:
                token = session["event_portal_brand_csrf"]
            response = client.post("/eventos/configuracao/portal-marca", data={
                "csrf_token": token, "brand_name": "Gelato da Praia",
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/eventos/configuracao/portal-marca")
        values = save_brand.call_args.args[0]
        self.assertEqual(values["brand_name"], "Gelato da Praia")
        self.assertIsNone(values["store_id"])

    def test_editor_disables_only_its_blur_submit_scroll(self):
        root = Path(__file__).parent.parent
        template = (root / "flask_app" / "templates" / "eventos" / "configuracao_portal_marca.html").read_text()
        base_template = (root / "flask_app" / "templates" / "base.html").read_text()

        self.assertIn('data-ios-submit-scroll="false"', template)
        self.assertIn("scope.dataset.iosSubmitScroll === 'false'", base_template)

    def test_public_request_uses_default_brand_and_server_sets_its_store(self):
        brand = default_portal_brand()
        brand.update({"store_id": 9, "brand_name": "Gelato da Praia", "primary_color": "#123456"})
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("flask_app.routes.eventos.db.get_portal_unavailable_dates", return_value=[]):
            response = client.get("/eventos/pedido-evento")
            self.assertIn("Gelato da Praia", response.get_data(as_text=True))
            csrf = client.session_transaction()
            with csrf as session:
                token = session["event_portal_csrf"]
            with patch("flask_app.routes.eventos.resolve_event_address", return_value={}), \
                 patch("flask_app.routes.eventos.db.create_portal_event_request", return_value={"event_id": 4, "access_code": "code"} ) as create_request, \
                 patch("flask_app.routes.eventos.db.get_event_resources", return_value=[]), \
                 patch("flask_app.routes.eventos.db.record_portal_access"):
                response = client.post("/eventos/pedido-evento", data={
                    "csrf_token": token, "event_type": "Aniversário", "estimated_guests": "20",
                    "servings_per_guest": "1", "flavours[]": "Morango",
                    "occurrence_date[]": "2099-01-01", "occurrence_start[]": "14:00",
                    "occurrence_venue[]": "Jardim", "occurrence_address[]": "Rua da Praia 1",
                    "client_name": "Cliente", "client_email": "cliente@example.com",
                    "client_phone": "912345678", "privacy_accepted": "1",
                    "brand_store_id": "999",
                })

        self.assertEqual(response.status_code, 302)
        submitted = create_request.call_args.args[0]
        self.assertEqual(submitted["brand_store_id"], 9)
        self.assertEqual(submitted["confirmation_message"], brand["confirmation_message"])


if __name__ == "__main__":
    unittest.main()