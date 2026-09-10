"""Focused contracts for the unified Events portal and persisted proposals."""

import os
import hashlib
import re
import secrets
import time
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

from db import eventos as event_db
from db.eventos import validate_portal_customer, validate_portal_nif
from flask_app import google_sheets_sync as sheets
from flask_app.routes.eventos import eventos_bp
from flask_app.services.event_quote_pdf import proposal_view_model, render_quote_pdf


ROOT = Path(__file__).parent.parent


class CustomerAndSheetContracts(unittest.TestCase):
    def test_short_notice_threshold_is_inclusive_and_checks_all_occurrences(self):
        warning = "Data muito próxima."
        threshold = date.today() + timedelta(days=7)
        self.assertIsNone(event_db.portal_short_notice_warning(
            [{"event_date": threshold}], 7, warning
        ))
        self.assertEqual(event_db.portal_short_notice_warning(
            [{"event_date": threshold - timedelta(days=1)}], 7, warning
        ), warning)
        self.assertEqual(event_db.portal_short_notice_warning(
            [{"event_date": threshold}, {"event_date": threshold - timedelta(days=1)}],
            7, warning
        ), warning)
        self.assertIsNone(event_db.portal_short_notice_warning(
            [{"event_date": threshold.isoformat()}], 7, warning
        ))

    def test_customer_validation_requires_company_and_valid_nif(self):
        self.assertEqual(validate_portal_nif("501 234 560"), "501234560")
        self.assertEqual(
            validate_portal_customer("empresa", "  ACME  ", "501234560"),
            ("empresa", "ACME", "501234560"),
        )
        with self.assertRaises(ValueError):
            validate_portal_customer("empresa", "", "501234560")
        with self.assertRaises(ValueError):
            validate_portal_customer("empresa", "ACME", "501234567")
        self.assertEqual(validate_portal_customer("particular", "", ""), ("particular", None, None))

    def test_sheet_header_mapping_keeps_customer_type_company_and_nif(self):
        headers = ["Nome", "Tipo de Cliente", "Empresa", "NIF"]
        row = ["Joana", "Empresa", "ACME Lda", "501234560"]
        mapped = sheets._map_row_with_headers(headers, row, 7)
        self.assertEqual(mapped["customer_type"], "empresa")
        self.assertEqual(mapped["company_name"], "ACME Lda")
        self.assertEqual(mapped["nif"], "501234560")

    def test_default_quote_tax_lookup_has_no_portal_request_dependencies(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (Decimal("0.13"),)
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("db.eventos.db_connection", return_value=context):
            self.assertEqual(
                event_db.get_default_quote_taxa_iva("gelado_kg"),
                Decimal("0.13"),
            )

    def test_particular_request_rejects_pending_service_before_writing(self):
        data = {
            "client_email": "cliente@example.com",
            "customer_type": "particular",
            "privacy_accepted": True,
            "estimated_guests": 20,
            "servings_per_guest": 1,
            "flavours": [1],
            "occurrences": [{
                "event_date": "2026-10-01",
                "estimated_km": Decimal("5"),
                "service_mode": "pending",
            }],
        }
        with patch("db.eventos.validate_portal_flavours", return_value=["Baunilha"]), \
             patch("db.eventos.calculate_portal_flavours", return_value={
                 "guests": 20, "scoops": 1, "grams_per_guest": 70,
                 "flavours": [{"name": "Baunilha", "kg": "2.00"}],
                 "total_kg": Decimal("2.00"),
             }), patch("db.eventos.db_connection") as db_connection:
            with self.assertRaisesRegex(ValueError, "tipo de serviço válido"):
                event_db.create_portal_event_request(data)
        db_connection.assert_not_called()

    def test_missing_distance_downgrades_particular_to_manual_quote(self):
        self.assertFalse(event_db._portal_auto_quote_eligible(
            "particular", [{"estimated_km": None}]
        ))
        self.assertTrue(event_db._portal_auto_quote_eligible(
            "particular", [{"estimated_km": Decimal("5.2")}]
        ))
        self.assertFalse(event_db._portal_auto_quote_eligible(
            "empresa", [{"estimated_km": Decimal("5.2")}]
        ))

    def test_primary_occurrence_updates_replace_existing_values(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"id": 10}
        data = {
            "event_date": "2026-11-02",
            "venue": "Novo local",
            "venue_address": "Nova morada",
            "event_time": "15:00",
            "event_end_time": "18:00",
            "internal_notes": "Novas notas",
        }
        with patch(
            "db.eventos._validate_occurrence_reservation_change",
            return_value=[],
        ):
            event_db._upsert_primary_occurrence(cursor, 5, data)

        upsert_sql = cursor.execute.call_args_list[-1].args[0]
        self.assertIn("event_date = EXCLUDED.event_date", upsert_sql)
        self.assertIn("venue = EXCLUDED.venue", upsert_sql)
        self.assertNotIn("COALESCE(event_occurrences.event_date", upsert_sql)

    def test_event_update_preserves_customer_metadata_when_omitted(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        context = MagicMock()
        context.__enter__.return_value = connection
        data = {
            "event_name": "Empresa",
            "event_type": "Corporativo",
            "event_date": None,
            "event_time": "",
            "estimated_guests": 20,
            "venue": "Porto",
            "venue_address": "Rua Teste",
            "client_name": "Cliente",
            "client_email": "cliente@example.com",
            "client_phone": "+351912345678",
            "status": "novos",
            "loss_reason": None,
            "internal_notes": "",
        }

        with patch("db.eventos.db_connection", return_value=context), \
             patch("db.eventos._upsert_primary_occurrence", return_value=[]), \
             patch("db.eventos._insert_event_history"):
            event_db.update_event(5, data)

        update_sql, parameters = cursor.execute.call_args_list[-1].args
        self.assertIn(
            "customer_type=COALESCE(%(customer_type)s, customer_type)",
            update_sql,
        )
        self.assertIsNone(parameters["customer_type"])
        self.assertIsNone(parameters["company_name"])
        self.assertIsNone(parameters["nif"])


class ProposalRenderingContracts(unittest.TestCase):
    def _event(self):
        return {
            "client_name": "Cliente",
            "company_name": None,
            "customer_type": "particular",
            "event_name": "Festa",
            "event_type": "Aniversário",
            "occurrences_public": [],
            "flavours": [{"name": "Baunilha", "kg": "2.00"}],
            "resource_requirements_snapshot": {"carrinho": {"width_cm": "10"}},
            "logistics_message": "Montagem incluída",
            "quote_items_public": [{"descricao": "Gelado", "quantidade": "2", "total_gross": "20.00"}],
            "quote_totals": {"total_net": "17.70", "total_vat": "2.30", "total_gross": "20.00"},
            "quote_revision": "rev-1",
            "quote_created_at": None,
        }

    def test_view_model_uses_persisted_snapshot_and_pdf_is_reportlab_pdf(self):
        model = proposal_view_model(self._event())
        self.assertEqual(model["event_name"], "Festa")
        self.assertEqual(model["items"][0]["quantidade"], "2")
        self.assertTrue(render_quote_pdf(self._event()).startswith(b"%PDF"))

    def test_staff_quote_version_persists_complete_proposal_snapshot(self):
        item = {
            "id": 1,
            "descricao": "Gelado",
            "quantidade": Decimal("2"),
            "preco_unitario": Decimal("10"),
            "taxa_iva": Decimal("0.13"),
            "unit_price_gross": Decimal("10"),
            "total_net": Decimal("17.70"),
            "total_vat": Decimal("2.30"),
            "total_gross": Decimal("20"),
            "total": Decimal("20"),
        }
        event = {
            "event_name": "Festa congelada",
            "event_type": "Aniversário",
            "event_date": None,
            "event_time": None,
            "event_end_time": None,
            "estimated_guests": 20,
            "client_name": "Cliente",
            "client_email": "cliente@example.com",
            "client_phone": "+351912345678",
            "customer_type": "particular",
            "company_name": None,
            "nif": None,
            "venue": "Porto",
            "venue_address": "Rua Teste",
            "internal_notes": None,
        }
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[item], [item], []]
        cursor.fetchone.side_effect = [
            {"next_version": 1},
            event,
            {"flavours": [], "resource_requirements_snapshot": {}},
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("db.eventos.db_connection", return_value=context), \
             patch("db.eventos._insert_event_history"):
            version = event_db.create_quote_version(5, "Envio", "equipa")

        self.assertEqual(version, 1)
        insert_call = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO event_quote_versions" in call.args[0]
        )
        self.assertIn("proposal_snapshot", insert_call.args[0])
        snapshot_json = insert_call.args[1][5]
        self.assertIn("Festa congelada", snapshot_json)
        self.assertIn("cliente@example.com", snapshot_json)


class ProposalPdfAccessContracts(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=os.path.join(ROOT, "flask_app", "templates"))
        self.app.secret_key = "pdf-test"
        self.app.register_blueprint(eventos_bp, url_prefix="/eventos")

    def _event(self, customer_type="particular"):
        return {
            "id": 5,
            "customer_type": customer_type,
            "client_name": "Cliente",
            "company_name": None,
            "nif": None,
            "status": "enviado",
            "estimated_guests": 20,
            "occurrences_public": event_db._restore_proposal_occurrences([{
                "event_date": "2026-10-01",
                "service_start_time": "14:30:00",
                "service_end_time": "17:30:00",
                "venue": "Porto",
                "venue_address": "Rua Teste",
            }]),
            "flavours": [{"name": "Baunilha", "kg": "2.00"}],
            "resource_requirements_snapshot": {},
            "logistics_message": "Deslocação incluída.",
            "public_message": None,
            "quote_items_public": [{
                "descricao": "Gelado",
                "quantidade": Decimal("1"),
                "total_gross": Decimal("10.00"),
            }],
            "quote_totals": {"total_net": 8, "total_vat": 2, "total_gross": 10},
            "quote_revision": "rev-1",
            "quote_version_number": 1,
            "quote_created_at": None,
            "event_name": "Teste",
            "event_type": "Festa",
            "portal_brand": {
                "brand_name": "Scoopy",
                "logo_filename": None,
                "primary_color": "#198f83",
                "accent_color": "#33a99b",
                "background_color": "#fffaf5",
                "text_color": "#172321",
                "button_color": "#198f83",
                "button_text_color": "#ffffff",
            },
        }

    def test_pdf_route_requires_session_and_matching_event(self):
        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos._portal_login_required", return_value=None
        ):
            response = client.get("/eventos/portal-eventos/pedido/5/pdf")
        self.assertEqual(response.status_code, 302)

        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos._portal_login_required", return_value="a@example.com"
        ), patch("flask_app.routes.eventos._portal_event_id", return_value=6), patch(
            "flask_app.routes.eventos.db.get_portal_event_for_email"
        ) as get_event:
            response = client.get("/eventos/portal-eventos/pedido/5/pdf")
        self.assertEqual(response.status_code, 404)
        get_event.assert_not_called()

    def test_pdf_route_blocks_empresa_and_returns_pdf_for_particular(self):
        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos._portal_login_required", return_value="a@example.com"
        ), patch("flask_app.routes.eventos._portal_event_id", return_value=5), patch(
            "flask_app.routes.eventos.db.get_portal_event_for_email",
            return_value=self._event("empresa"),
        ):
            self.assertEqual(client.get("/eventos/portal-eventos/pedido/5/pdf").status_code, 404)

        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos._portal_login_required", return_value="a@example.com"
        ), patch("flask_app.routes.eventos._portal_event_id", return_value=5), patch(
            "flask_app.routes.eventos.db.get_portal_event_for_email",
            return_value=self._event("particular"),
        ), patch("flask_app.routes.eventos.db.record_portal_access"):
            response = client.get("/eventos/portal-eventos/pedido/5/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF"))

    def test_portal_detail_renders_dates_restored_from_proposal_snapshot(self):
        event = self._event("particular")
        event["short_notice_warning"] = "Data muito próxima."
        self.assertEqual(event["occurrences_public"][0]["event_date"].year, 2026)
        self.assertEqual(event["occurrences_public"][0]["service_start_time"].hour, 14)

        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos._portal_login_required",
            return_value="a@example.com",
        ), patch(
            "flask_app.routes.eventos._portal_event_id", return_value=5
        ), patch(
            "flask_app.routes.eventos.db.get_portal_event_for_email",
            return_value=event,
        ), patch("flask_app.routes.eventos.db.record_portal_access"):
            response = client.get("/eventos/portal-eventos/pedido/5")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"01/10/2026", response.data)
        self.assertIn(b"14:30", response.data)
        self.assertIn("Data muito próxima.".encode(), response.data)

    def test_particular_manual_quote_fallback_shows_contact_message_not_zero_total(self):
        event = self._event("particular")
        event["status"] = "novos"
        event["quote_items_public"] = []
        event["quote_revision"] = None
        event["public_message"] = (
            "Não foi possível calcular automaticamente a deslocação; "
            "a nossa equipa irá contactar para preparar a proposta."
        )

        with self.app.test_client() as client, patch(
            "flask_app.routes.eventos._portal_login_required",
            return_value="a@example.com",
        ), patch(
            "flask_app.routes.eventos._portal_event_id", return_value=5
        ), patch(
            "flask_app.routes.eventos.db.get_portal_event_for_email",
            return_value=event,
        ), patch("flask_app.routes.eventos.db.record_portal_access"):
            response = client.get("/eventos/portal-eventos/pedido/5")

        self.assertEqual(response.status_code, 200)
        self.assertIn("irá contactar".encode(), response.data)
        self.assertNotIn(b"Total", response.data)


class UnifiedTemplateContracts(unittest.TestCase):
    def test_configuration_and_portal_templates_expose_required_controls(self):
        config = (ROOT / "flask_app/templates/eventos/configuracao.html").read_text()
        portal = (ROOT / "flask_app/templates/eventos/portal_request.html").read_text()
        self.assertEqual(config.count('type="submit"'), 1)
        self.assertIn('name="csrf_token"', config)
        self.assertIn('name="customer_type"', portal)
        self.assertIn('name="company_name"', portal)
        self.assertIn('name="nif"', portal)
        self.assertIn("Requisitos para o cliente", config)
        self.assertIn("data-requirements", portal)
        self.assertIn('event.persisted', portal)
        self.assertIn("button.disabled=false", portal)


class PortalSubmissionRecoveryContracts(unittest.TestCase):
    def setUp(self):
        self.app = Flask(
            __name__, template_folder=os.path.join(ROOT, "flask_app", "templates")
        )
        self.app.secret_key = "submission-recovery"
        self.app.register_blueprint(eventos_bp, url_prefix="/eventos")

    def _brand(self):
        return {
            "store_id": 9, "brand_name": "Scoopy", "logo_filename": None,
            "primary_color": "#167C70", "accent_color": "#35A394",
            "background_color": "#FFF8F2", "text_color": "#173B38",
            "button_color": "#167C70", "button_text_color": "#FFFFFF",
            "form_title": "Eventos", "form_intro": "Intro",
            "confirmation_message": "Recebido", "contact_text": "Contacto",
            "min_advance_days": 0, "short_notice_warning": "Aviso",
            "field_labels": {}, "visible_fields": {},
        }

    def _post_data(self, token, started_at=None):
        data = {
            "csrf_token": token, "event_type": "Aniversário",
            "submission_identifier": (
                self._issued_submission_identifier(token) if token else ""
            ),
            "customer_type": "particular", "estimated_guests": "20",
            "servings_per_guest": "1", "flavours[]": "12",
            "occurrence_date[]": "2027-01-01", "occurrence_start[]": "14:00",
            "occurrence_venue[]": "Jardim",
            "occurrence_address[]": "Rua da Praia 1",
            "client_name": "Cliente", "client_email": "cliente@example.com",
            "client_phone": "+351 912345678", "privacy_accepted": "1",
        }
        if started_at is not None:
            data["started_at"] = str(started_at)
        return data

    def _issued_submission_identifier(self, csrf_token):
        with self.app.test_request_context():
            from flask_app.routes.eventos import _submission_serializer
            return _submission_serializer().dumps({
                "nonce": secrets.token_urlsafe(32),
                "csrf": hashlib.sha256(csrf_token.encode("utf-8")).hexdigest(),
            })

    def test_missing_and_old_timestamps_are_accepted(self):
        for started_at in (None, int(time.time() * 1000) - 48 * 60 * 60 * 1000):
            with self.subTest(started_at=started_at), self.app.test_client() as client, \
                 patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=self._brand()), \
                 patch("flask_app.routes.eventos.db.get_event_resources", return_value=[]), \
                 patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]), \
                 patch("flask_app.routes.eventos.resolve_event_address", return_value={}), \
                 patch("flask_app.routes.eventos.db.consume_portal_rate_limit", return_value=True), \
                 patch("flask_app.routes.eventos.db.create_portal_event_request",
                       return_value={"event_id": 4, "access_code": "code"}):
                client.get("/eventos/pedido-evento")
                with client.session_transaction() as session:
                    token = session["event_portal_csrf"]
                response = client.post(
                    "/eventos/pedido-evento",
                    data=self._post_data(token, started_at),
                )
            self.assertEqual(response.status_code, 302)

    def test_auxiliary_failures_after_creation_still_redirect_to_created_request(self):
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=self._brand()), \
             patch("flask_app.routes.eventos.db.get_event_resources", side_effect=[[], RuntimeError("resources")]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]), \
             patch("flask_app.routes.eventos.resolve_event_address", return_value={}), \
             patch("flask_app.routes.eventos.db.consume_portal_rate_limit", return_value=True), \
             patch("flask_app.routes.eventos.db.create_portal_event_request",
                   return_value={"event_id": 4, "access_code": "code"}), \
             patch("flask_app.routes.eventos.db.record_portal_access",
                   side_effect=RuntimeError("audit")):
            client.get("/eventos/pedido-evento")
            with client.session_transaction() as session:
                token = session["event_portal_csrf"]
            response = client.post(
                "/eventos/pedido-evento",
                data=self._post_data(token, int(time.time() * 1000) - 2000),
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/eventos/portal-eventos/pedido/4"))

    def test_rendered_forms_receive_distinct_signed_submission_identifiers(self):
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=self._brand()), \
             patch("flask_app.routes.eventos.db.get_event_resources", return_value=[]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]):
            first = client.get("/eventos/pedido-evento").get_data(as_text=True)
            second = client.get("/eventos/pedido-evento").get_data(as_text=True)
        pattern = r'name="submission_identifier" value="([^"]+)"'
        first_identifier = re.search(pattern, first).group(1)
        second_identifier = re.search(pattern, second).group(1)
        self.assertNotEqual(first_identifier, second_identifier)

    def test_double_click_and_network_retry_redirect_to_original_event(self):
        created = {
            "event_id": 44, "access_code": "first-code", "replayed": False,
        }
        replayed = {
            "event_id": 44, "access_code": "first-code", "replayed": True,
        }
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=self._brand()), \
             patch("flask_app.routes.eventos.db.get_event_resources", return_value=[]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]), \
             patch("flask_app.routes.eventos.resolve_event_address", return_value={}), \
             patch("flask_app.routes.eventos.db.consume_portal_rate_limit", return_value=True), \
             patch("flask_app.routes.eventos.db.record_portal_access"), \
             patch("flask_app.routes.eventos.queue_analytics_event") as analytics, \
             patch("flask_app.routes.eventos.db.create_portal_event_request",
                   side_effect=[created, replayed]) as create:
            client.get("/eventos/pedido-evento")
            with client.session_transaction() as session:
                csrf = session["event_portal_csrf"]
            data = self._post_data(csrf)
            first = client.post("/eventos/pedido-evento", data=data)
            retry = client.post("/eventos/pedido-evento", data=data)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(
            create.call_args_list[0].args[0]["submission_identifier"],
            create.call_args_list[1].args[0]["submission_identifier"],
        )
        self.assertTrue(first.location.endswith("/eventos/portal-eventos/pedido/44"))
        self.assertTrue(retry.location.endswith("/eventos/portal-eventos/pedido/44"))
        analytics.assert_called_once()
        with client.session_transaction() as session:
            self.assertEqual(session["event_portal_access_code_once"], "first-code")

    def test_submission_identifier_cannot_be_replayed_from_another_session(self):
        with self.app.test_client() as first_client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=self._brand()), \
             patch("flask_app.routes.eventos.db.get_event_resources", return_value=[]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]):
            page = first_client.get("/eventos/pedido-evento").get_data(as_text=True)
        identifier = re.search(
            r'name="submission_identifier" value="([^"]+)"', page
        ).group(1)

        with self.app.test_client() as other_client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=self._brand()), \
             patch("flask_app.routes.eventos.db.get_event_resources", return_value=[]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours", return_value=[]), \
             patch("flask_app.routes.eventos.db.create_portal_event_request") as create:
            other_client.get("/eventos/pedido-evento")
            with other_client.session_transaction() as session:
                csrf = session["event_portal_csrf"]
            data = self._post_data(csrf)
            data["submission_identifier"] = identifier
            response = other_client.post("/eventos/pedido-evento", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("não pertence a esta sessão".encode(), response.data)
        create.assert_not_called()

    def test_concurrent_retry_is_serialized_and_database_identifier_is_unique(self):
        implementation = (ROOT / "db/eventos.py").read_text()
        schema = (ROOT / "db/schema.py").read_text()
        lock = "pg_advisory_xact_lock(hashtextextended(%s, 0))"
        self.assertIn(lock, implementation)
        self.assertLess(
            implementation.index(lock),
            implementation.index("WHERE submission_identifier = %s"),
        )
        self.assertIn(
            "uq_event_portal_requests_submission_identifier", schema
        )
        self.assertIn(
            "ON event_portal_requests (submission_identifier)", schema
        )

    def test_validation_error_restores_repeated_and_selectable_form_data(self):
        data = self._post_data(None, None)
        data.update({
            "customer_type": "empresa", "company_name": "Empresa Teste",
            "nif": "invalid", "duration_minutes": "240",
            "servings_per_guest": "2", "service_mode": "client_serves",
            "referral_source": "Recomendação", "resource_preferences[]": "cart",
            "flavours[]": "12", "privacy_accepted": "1",
        })
        data["occurrence_date[]"] = ["2027-01-01", "2027-01-02"]
        data["occurrence_start[]"] = ["14:00", "16:00"]
        data["occurrence_venue[]"] = ["Jardim", "Salão"]
        data["occurrence_address[]"] = ["Rua Um", "Rua Dois"]
        brand = self._brand()
        brand["visible_fields"] = {
            "event_name": True, "duration": True, "service_mode": True,
            "resource_preferences": True, "referral_source": True,
        }
        with self.app.test_client() as client, \
             patch("flask_app.routes.eventos.db.get_default_portal_brand", return_value=brand), \
             patch("flask_app.routes.eventos.db.get_event_resources", return_value=[{
                 "id": 5, "code": "cart", "name": "Carrinho",
                 "capacity_flavors": 4, "public_capacity_flavors": 4,
                 "image_url": None, "public_description": None,
                 "public_customer_requirements": None,
             }]), \
             patch("flask_app.routes.eventos.db.get_portal_flavours",
                   return_value=[{"id": 12, "nome_corrente": "Baunilha"}]), \
             patch("flask_app.routes.eventos.resolve_event_address", return_value={}), \
             patch("flask_app.routes.eventos.db.consume_portal_rate_limit", return_value=True):
            client.get("/eventos/pedido-evento")
            with client.session_transaction() as session:
                data["csrf_token"] = session["event_portal_csrf"]
                data["submission_identifier"] = self._issued_submission_identifier(
                    session["event_portal_csrf"]
                )
            response = client.post("/eventos/pedido-evento", data=data)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="2027-01-01"', page)
        self.assertIn('value="2027-01-02"', page)
        self.assertIn('value="Salão"', page)
        self.assertIn('value="Rua Dois"', page)
        self.assertIn('value="cart" checked', page)
        self.assertIn('value="12" checked', page)
        self.assertIn('value="240" selected', page)
        self.assertIn('value="client_serves" selected', page)
        self.assertIn('value="Recomendação"', page)
        self.assertIn('id="privacy" checked', page)


if __name__ == "__main__":
    unittest.main()