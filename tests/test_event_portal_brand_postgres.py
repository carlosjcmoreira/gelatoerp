"""PostgreSQL integration coverage for durable public Events portal images."""

import os
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import psycopg2

from db import eventos, schema
from flask_app.services.event_portal_brand import validate_brand_form
from tests.test_event_portal_brand import _app


class EventPortalBrandPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise unittest.SkipTest("DATABASE_URL is required for PostgreSQL integration tests")

        cls.schema_name = f"test_event_portal_images_{uuid.uuid4().hex}"
        cls.admin_connection = psycopg2.connect(database_url)
        cls.admin_connection.autocommit = True
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{cls.schema_name}"')
            cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            cursor.execute("""
                CREATE TABLE stores (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    is_active BOOLEAN DEFAULT TRUE
                );
                CREATE TABLE events (id SERIAL PRIMARY KEY);
                CREATE TABLE event_quote_versions (id BIGSERIAL PRIMARY KEY);
                CREATE TABLE lead_requests (id BIGSERIAL PRIMARY KEY);
                CREATE TABLE event_resources (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(80) NOT NULL UNIQUE,
                    name VARCHAR(255) NOT NULL,
                    resource_type VARCHAR(80) NOT NULL DEFAULT 'equipment',
                    capacity_carapinas INTEGER,
                    capacity_flavors INTEGER,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE receitas_gelado (
                    id SERIAL PRIMARY KEY,
                    nome_corrente VARCHAR(255),
                    ativo BOOLEAN NOT NULL DEFAULT TRUE
                );
                INSERT INTO stores (name, is_active) VALUES ('Integration store', TRUE);
            """)

        cls.open_connections = []

        @contextmanager
        def isolated_connection():
            connection = psycopg2.connect(database_url)
            cls.open_connections.append(connection)
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            try:
                yield connection
            finally:
                if not connection.closed:
                    connection.close()

        cls.isolated_connection = staticmethod(isolated_connection)
        with patch("db.schema.db_connection", cls.isolated_connection):
            schema.run_migrations_eventos_customer_portal()

    @classmethod
    def tearDownClass(cls):
        for connection in cls.open_connections:
            if not connection.closed:
                connection.close()
        if getattr(cls, "admin_connection", None):
            with cls.admin_connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{cls.schema_name}" CASCADE')
            cls.admin_connection.close()

    def _save(self, logo_filename, logo_asset=None, resource_asset=None):
        resource = {
            "code": "cart",
            "name": "Carrinho",
            "resource_type": "equipment",
            "capacity_flavors": 4,
            "active": True,
            "image_url": "database:asset",
            "public_capacity_flavors": 4,
        }
        if resource_asset is not None:
            resource["image_asset"] = resource_asset
        with patch("db.eventos.db_connection", self.isolated_connection):
            eventos.save_event_configuration(
                validate_brand_form({"brand_name": "Integration brand"}),
                logo_filename,
                [resource],
                [],
                [],
                "integration-test",
                logo_asset=logo_asset,
            )

    def _fetch_image_rows(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT store_id, logo_filename, logo_data, logo_content_type
                    FROM event_portal_brand_configs
                """)
                logo = cursor.fetchone()
                cursor.execute("""
                    SELECT id, image_data, image_content_type
                    FROM event_resources WHERE code='cart'
                """)
                resource = cursor.fetchone()
        return logo, resource

    def test_durable_public_images_survive_real_postgres_upserts_and_http_reads(self):
        first_logo = {"payload": b"first-logo", "content_type": "image/png"}
        first_resource = {"payload": b"first-resource", "content_type": "image/webp"}
        self._save("first.png", first_logo, first_resource)
        logo, resource = self._fetch_image_rows()
        self.assertEqual(bytes(logo[2]), first_logo["payload"])
        self.assertEqual(logo[3], "image/png")
        self.assertEqual(bytes(resource[1]), first_resource["payload"])
        self.assertEqual(resource[2], "image/webp")

        second_logo = {"payload": b"second-logo", "content_type": "image/jpeg"}
        second_resource = {"payload": b"second-resource", "content_type": "image/png"}
        self._save("second.jpg", second_logo, second_resource)
        logo, resource = self._fetch_image_rows()
        store_id, resource_id = logo[0], resource[0]
        self.assertEqual(bytes(logo[2]), second_logo["payload"])
        self.assertEqual(logo[3], "image/jpeg")
        self.assertEqual(bytes(resource[1]), second_resource["payload"])
        self.assertEqual(resource[2], "image/png")

        self._save("second.jpg")
        preserved_logo, preserved_resource = self._fetch_image_rows()
        self.assertEqual(bytes(preserved_logo[2]), second_logo["payload"])
        self.assertEqual(bytes(preserved_resource[1]), second_resource["payload"])

        app = _app()
        with app.test_client() as client, \
             patch(
                 "flask_app.routes.eventos.db.get_public_portal_brand_logo",
                 side_effect=eventos.get_public_portal_brand_logo,
             ), \
             patch(
                 "flask_app.routes.eventos.db.get_public_event_resource_image",
                 side_effect=eventos.get_public_event_resource_image,
             ), \
             patch("db.eventos.db_connection", self.isolated_connection):
            logo_response = client.get(f"/eventos/pedido-evento/marca/{store_id}/logo")
            self.assertEqual(logo_response.status_code, 200)
            self.assertEqual(logo_response.data, second_logo["payload"])
            self.assertEqual(logo_response.content_type, "image/jpeg")
            self.assertTrue(logo_response.headers.get("ETag"))
            revalidated = client.get(
                f"/eventos/pedido-evento/marca/{store_id}/logo",
                headers={"If-None-Match": logo_response.headers["ETag"]},
            )
            self.assertEqual(revalidated.status_code, 304)

            resource_response = client.get(
                f"/eventos/pedido-evento/meios/{resource_id}/imagem"
            )
            self.assertEqual(resource_response.status_code, 200)
            self.assertEqual(resource_response.data, second_resource["payload"])
            self.assertEqual(resource_response.content_type, "image/png")

        self._save(None)
        removed_logo, preserved_resource = self._fetch_image_rows()
        self.assertIsNone(removed_logo[1])
        self.assertIsNone(removed_logo[2])
        self.assertIsNone(removed_logo[3])
        self.assertEqual(bytes(preserved_resource[1]), second_resource["payload"])

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE event_portal_brand_configs
                       SET logo_filename='missing-legacy.png'
                       WHERE store_id=%s""",
                    (store_id,),
                )
            connection.commit()

        with app.test_client() as client, \
             patch(
                 "flask_app.routes.eventos.db.get_public_portal_brand_logo",
                 side_effect=eventos.get_public_portal_brand_logo,
             ), \
             patch("db.eventos.db_connection", self.isolated_connection):
            missing = client.get(f"/eventos/pedido-evento/marca/{store_id}/logo")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()