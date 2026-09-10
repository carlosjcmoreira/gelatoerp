"""Focused contracts for the public customer event portal."""

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.datastructures import FileStorage

from db import eventos
from flask_app.routes.eventos import eventos_bp
from flask_app.routes.eventos import _valid_phone
from flask_app.services import event_portal


class _RevisionCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        pass

    def fetchall(self):
        return self.rows


class PortalCalculationTests(unittest.TestCase):
    def test_flavour_calculator_enforces_serving_and_minimum_per_flavour(self):
        plan = eventos.calculate_portal_flavours(10, 2, ['Baunilha', 'Chocolate'])

        self.assertEqual(str(plan['total_kg']), '4.00')
        self.assertEqual(plan['grams_per_guest'], 120)
        self.assertEqual(plan['flavours'][0]['kg'], '2.00')

    def test_flavour_calculator_refuses_more_than_six_flavours(self):
        with self.assertRaisesRegex(ValueError, 'seis sabores'):
            eventos.calculate_portal_flavours(50, 1, list('abcdefg'))

    def test_quote_revision_changes_when_money_changes(self):
        baseline = [{
            'id': 1, 'descricao': 'Gelado', 'quantidade': 2,
            'preco_unitario': '31.80', 'taxa_iva': '0.13',
            'unit_price_gross': '31.80', 'total_net': '56.28',
            'total_vat': '7.32', 'total_gross': '63.60', 'total': '63.60',
        }]
        changed = [dict(baseline[0], total_gross='70.00', total='70.00')]

        first, _ = eventos._quote_revision(_RevisionCursor(baseline), 1)
        second, _ = eventos._quote_revision(_RevisionCursor(changed), 1)

        self.assertNotEqual(first, second)

    def test_mixed_ids_and_free_text_cannot_bypass_public_flavour_validation(self):
        with self.assertRaisesRegex(ValueError, 'Seleção de sabores inválida'):
            eventos.validate_portal_flavours(['12', 'Chocolate'])


class PortalUploadTests(unittest.TestCase):
    def _upload(self, filename, payload):
        return FileStorage(stream=io.BytesIO(payload), filename=filename)

    def test_upload_checks_file_signature_not_just_extension(self):
        with self.assertRaisesRegex(ValueError, 'conteúdo'):
            event_portal.validate_portal_proof(self._upload('proof.pdf', b'not-a-pdf'))

    def test_pdf_upload_is_saved_outside_public_tree(self):
        validated = event_portal.validate_portal_proof(
            self._upload('proof.pdf', b'%PDF-1.7\ncontents')
        )
        with tempfile.TemporaryDirectory() as root:
            metadata = event_portal.save_private_portal_proof(validated, root)
            self.assertTrue((__import__('pathlib').Path(root) / metadata['storage_name']).is_file())
            self.assertNotIn('static', metadata['storage_name'])

    def test_cleanup_removes_only_database_expired_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = __import__('pathlib').Path(root) / 'expired.pdf'
            path.write_bytes(b'old')
            with patch('flask_app.services.event_portal.event_db.expire_portal_files', return_value=['expired.pdf']):
                removed = event_portal.cleanup_expired_portal_proofs(root)
            self.assertEqual(removed, 1)
            self.assertFalse(path.exists())


class PortalGeocodingTests(unittest.TestCase):
    def test_cached_address_never_calls_public_geocoding_service(self):
        cached = {
            'latitude': '41.18', 'longitude': '-8.68', 'round_trip_km': '12.50',
            'provider': 'nominatim+osrm', 'failed': False,
        }
        with patch('flask_app.services.event_portal.event_db.get_portal_geocode_cache', return_value=cached), \
             patch('flask_app.services.event_portal.requests.get') as http_get:
            result = event_portal.resolve_event_address('Rua de Teste, Matosinhos')

        self.assertFalse(result['failed'])
        self.assertEqual(str(result['round_trip_km']), '12.50')
        http_get.assert_not_called()

    def test_geocoding_failure_returns_manual_review_without_raising(self):
        with patch('flask_app.services.event_portal.event_db.get_portal_geocode_cache', return_value=None), \
             patch('flask_app.services.event_portal.event_db.save_portal_geocode_cache') as save_cache, \
             patch(
                 'flask_app.services.event_portal.requests.get',
                 side_effect=event_portal.requests.Timeout('offline'),
             ):
            result = event_portal.resolve_event_address('Rua de Teste, Matosinhos')

        self.assertTrue(result['failed'])
        self.assertTrue(result['manual_review'])
        save_cache.assert_called_once()

    def test_address_suggestions_return_only_minimal_normalized_fields(self):
        response = unittest.mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'features': [{
                'geometry': {'coordinates': [-8.69, 41.18]},
                'properties': {
                    'name': 'Mercado', 'street': 'Rua Brito Capelo',
                    'housenumber': '1', 'postcode': '4450-073', 'city': 'Matosinhos',
                },
            }],
        }
        with patch('flask_app.services.event_portal.requests.get', return_value=response):
            suggestions = event_portal.suggest_event_addresses('Mercado Matosinhos')

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(set(suggestions[0]), {'label', 'latitude', 'longitude'})
        self.assertNotIn('properties', suggestions[0])


class PortalContactValidationTests(unittest.TestCase):
    def test_portuguese_phone_is_normalized_to_e164(self):
        self.assertEqual(_valid_phone('+351 912 345 678'), '+351912345678')

    def test_phone_without_international_prefix_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'indicativo internacional'):
            _valid_phone('912 345 678')

    def test_invalid_portuguese_phone_length_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'português válido'):
            _valid_phone('+351 12345')


class PortalAccessRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), '..', 'flask_app', 'templates'),
        )
        self.app.config['SECRET_KEY'] = 'portal-test-secret'
        self.app.register_blueprint(eventos_bp, url_prefix='/eventos')

    def _csrf(self, client):
        with client.session_transaction() as session:
            return session['event_portal_csrf']

    def test_verified_code_access_opens_only_the_matching_request(self):
        portal_event = {
            'id': 42, 'event_name': 'Festa', 'event_type': 'privado',
            'next_date': None, 'occurrence_count': 1, 'status': 'novos',
        }
        with self.app.test_client() as client, \
             patch('flask_app.routes.eventos.db.record_portal_access'), \
             patch('flask_app.routes.eventos.db.verify_portal_request_access', return_value=42) as verify_access, \
              patch('flask_app.routes.eventos.db.get_portal_event_for_email', return_value=portal_event) as get_event, \
              patch('flask_app.routes.eventos.db.get_default_portal_brand', return_value={}):
            client.get('/eventos/portal-eventos')
            response = client.post(
                '/eventos/portal-eventos',
                data={
                    'csrf_token': self._csrf(client), 'email': 'Cliente@Example.com',
                    'access_code': 'private-code',
                },
            )
            self.assertEqual(response.status_code, 302)
            response = client.get('/eventos/portal-eventos/pedidos')

        self.assertEqual(response.status_code, 200)
        verify_access.assert_called_once_with('cliente@example.com', 'private-code')
        get_event.assert_called_once_with(42, 'cliente@example.com')

    def test_unverified_email_cannot_start_a_portal_session(self):
        with self.app.test_client() as client, \
             patch('flask_app.routes.eventos.db.verify_portal_request_access', return_value=None), \
              patch('flask_app.routes.eventos.db.record_portal_access') as record_access, \
              patch('flask_app.routes.eventos.db.get_default_portal_brand', return_value={}):
            client.get('/eventos/portal-eventos')
            response = client.post(
                '/eventos/portal-eventos',
                data={
                    'csrf_token': self._csrf(client), 'email': 'owner@example.com',
                    'access_code': 'wrong-code',
                },
            )
            with client.session_transaction() as session:
                verified = session.get('event_portal_verified')

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(verified)
        record_access.assert_not_called()

    def test_portal_event_cannot_be_read_without_matching_email_session(self):
        with self.app.test_client() as client, \
             patch('flask_app.routes.eventos.db.get_portal_event_for_email', return_value=None) as get_event:
            with client.session_transaction() as session:
                session['event_portal_email'] = 'owner@example.com'
                session['event_portal_access_until'] = 9999999999
                session['event_portal_verified'] = True
                session['event_portal_event_id'] = 12
            response = client.get('/eventos/portal-eventos/pedido/12')

        self.assertEqual(response.status_code, 302)
        get_event.assert_called_once_with(12, 'owner@example.com')

    def test_code_for_one_request_cannot_open_another_request_with_same_email(self):
        with self.app.test_client() as client, \
             patch('flask_app.routes.eventos.db.get_portal_event_for_email') as get_event:
            with client.session_transaction() as session:
                session['event_portal_email'] = 'owner@example.com'
                session['event_portal_access_until'] = 9999999999
                session['event_portal_verified'] = True
                session['event_portal_event_id'] = 12
            response = client.get('/eventos/portal-eventos/pedido/99')

        self.assertEqual(response.status_code, 302)
        get_event.assert_not_called()

    def test_post_without_csrf_does_not_start_email_access(self):
        with self.app.test_client() as client, \
              patch('flask_app.routes.eventos.db.record_portal_access') as record_access, \
              patch('flask_app.routes.eventos.db.get_default_portal_brand', return_value={}):
            response = client.post('/eventos/portal-eventos', data={'email': 'owner@example.com'})

        self.assertEqual(response.status_code, 200)
        record_access.assert_not_called()

    def test_availability_endpoint_returns_only_coarse_status_for_one_date(self):
        with self.app.test_client() as client, \
             patch('flask_app.routes.eventos.db.consume_portal_rate_limit', return_value=True), \
             patch('flask_app.routes.eventos.db.get_portal_date_status', return_value='limited'):
            client.get('/eventos/portal-eventos')
            response = client.get(
                '/eventos/pedido-evento/disponibilidade?date=2027-09-12',
                headers={'X-CSRF-Token': self._csrf(client)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'status': 'limited'})


if __name__ == '__main__':
    unittest.main()