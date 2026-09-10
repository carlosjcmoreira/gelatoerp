"""Focused contracts for the unified Events portal and persisted proposals."""

import os
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


if __name__ == "__main__":
    unittest.main()