"""Contracts for configurable pipeline views and consolidated event venues."""

import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from flask import Blueprint, Flask

from db import eventos, schema
from flask_app.routes.eventos import eventos_bp
from flask_app.services import event_portal


class _Cursor:
    def __init__(self, results=()):
        self.results = list(results)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.results.pop(0) if self.results else None

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def cursor(self, **_kwargs):
        return self.cursor_instance

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _app():
    app = Flask(__name__)
    app.secret_key = 'pipeline-test'
    home = Blueprint('home', __name__)

    @home.route('/')
    def index():
        return 'home'

    app.register_blueprint(home)
    app.register_blueprint(eventos_bp, url_prefix='/eventos')
    return app


class PipelineLocationContracts(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = {'id': 7, 'username': 'eventos', 'acesso_eventos': True}

    def test_pipeline_accepts_safe_sort_and_provides_saved_columns(self):
        with patch('flask_app.routes.eventos.db.get_events', return_value=[]), \
             patch('flask_app.routes.eventos.db.get_event_resources', return_value=[]), \
             patch('flask_app.routes.eventos.db.get_pipeline_view_preferences',
                   return_value=['client', 'event']), \
             patch('flask_app.google_sheets_sync.get_sheet_sync_status', return_value=None), \
             patch('flask_app.routes.eventos._get_tabs', return_value=[]), \
             patch('flask_app.routes.eventos.render_template', return_value='ok') as render:
            response = self.client.get('/eventos/pipeline?sort=budget&direction=desc')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.kwargs['sort_by'], 'budget')
        self.assertEqual(render.call_args.kwargs['sort_direction'], 'desc')
        self.assertEqual(render.call_args.kwargs['pipeline_preferences'], ['client', 'event'])

    def test_pipeline_rejects_unknown_sort_column(self):
        with patch('flask_app.routes.eventos.db.get_events', return_value=[]) as get_events, \
             patch('flask_app.routes.eventos.db.get_event_resources', return_value=[]), \
             patch('flask_app.routes.eventos.db.get_pipeline_view_preferences', return_value=None), \
             patch('flask_app.google_sheets_sync.get_sheet_sync_status', return_value=None), \
             patch('flask_app.routes.eventos._get_tabs', return_value=[]), \
             patch('flask_app.routes.eventos.render_template', return_value='ok'):
            self.client.get('/eventos/pipeline?sort=DROP+TABLE&direction=desc')
        self.assertEqual(get_events.call_args.kwargs['sort_by'], 'date')

    def test_saved_columns_are_scoped_to_the_current_user(self):
        with patch('flask_app.routes.eventos.db.save_pipeline_view_preferences') as save:
            response = self.client.post(
                '/eventos/pipeline/preferencias',
                json={'columns': ['client', 'event', 'client', 'invalid']},
            )
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with('7', ['client', 'event'])

    def test_event_panel_renders_summary_with_existing_event_data(self):
        event = {'id': 4, 'status': 'novos', 'event_name': 'Festa', 'quote_total': 30}
        with patch('flask_app.routes.eventos.db.get_event', return_value=event), \
             patch('flask_app.routes.eventos.db.get_quote_items', return_value=[]), \
             patch('flask_app.routes.eventos.db.get_event_quote_totals', return_value={}), \
             patch('flask_app.routes.eventos.db.get_event_history', return_value=[]), \
             patch('flask_app.routes.eventos.db.get_event_primary_venue', return_value=None), \
             patch('flask_app.routes.eventos.render_template', return_value='panel') as render:
            response = self.client.get('/eventos/evento/4/painel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.args[0], 'eventos/_event_panel.html')
        self.assertEqual(render.call_args.kwargs['event'], event)

    def test_venue_list_and_detail_require_eventos_permission(self):
        with self.client.session_transaction() as session:
            session['user'] = {'id': 7, 'username': 'eventos', 'acesso_eventos': False}
        self.assertEqual(self.client.get('/eventos/locais').status_code, 302)
        self.assertEqual(self.client.get('/eventos/locais/1').status_code, 302)

    def test_venue_list_provides_the_incomplete_location_review_queue(self):
        queue = [{'id': 9, 'candidate_venues': []}]
        with patch('flask_app.routes.eventos.db.get_event_venues', return_value=[]), \
             patch('flask_app.routes.eventos.db.get_incomplete_venue_occurrences',
                   return_value=queue), \
             patch('flask_app.routes.eventos.db.get_event_venue_geocode_status',
                   return_value={'validated': 0, 'pending': 0, 'failed': 0, 'total': 0}), \
             patch('flask_app.routes.eventos._get_tabs', return_value=[]), \
             patch('flask_app.routes.eventos.render_template', return_value='ok') as render:
            response = self.client.get('/eventos/locais')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.kwargs['incomplete_occurrences'], queue)

    def test_manager_can_run_and_retry_resumable_geocode_batches(self):
        with self.client.session_transaction() as session:
            session['event_locations_csrf'] = 'location-token'
        with patch(
            'flask_app.routes.eventos.validate_historical_event_venues',
            return_value={'processed': 2, 'succeeded': 1, 'failed': 1, 'busy': False},
        ) as validate:
            response = self.client.post('/eventos/locais', data={
                'csrf_token': 'location-token',
                'action': 'retry_failed_venues',
            })
        self.assertEqual(response.status_code, 302)
        validate.assert_called_once_with(retry_failed=True, actor='eventos')

    def test_incomplete_location_actions_use_explicit_audited_operations(self):
        with self.client.session_transaction() as session:
            session['event_locations_csrf'] = 'location-token'
        with patch(
            'flask_app.routes.eventos.db.link_incomplete_event_occurrence_to_venue'
        ) as link:
            response = self.client.post('/eventos/locais', data={
                'csrf_token': 'location-token',
                'action': 'link_incomplete_occurrence',
                'occurrence_id': '9',
                'venue_id': '3',
            })
        self.assertEqual(response.status_code, 302)
        link.assert_called_once_with(9, 3, 'eventos')

        with patch(
            'flask_app.routes.eventos.db.complete_event_occurrence_venue',
            return_value=3,
        ) as complete, patch(
            'flask_app.routes.eventos.resolve_event_address',
            return_value={
                'latitude': 38.72, 'longitude': -9.14,
                'provider': 'nominatim', 'failed': False,
            },
        ):
            response = self.client.post('/eventos/locais', data={
                'csrf_token': 'location-token',
                'action': 'complete_incomplete_occurrence',
                'occurrence_id': '9',
                'name': 'Quinta da Serra',
                'address': 'Rua Principal 1',
            })
        self.assertEqual(response.status_code, 302)
        complete.assert_called_once()
        self.assertEqual(complete.call_args.args[0], 9)
        self.assertEqual(complete.call_args.args[2], 'eventos')

        with patch(
            'flask_app.routes.eventos.db.dismiss_incomplete_event_occurrence'
        ) as dismiss:
            response = self.client.post('/eventos/locais', data={
                'csrf_token': 'location-token',
                'action': 'dismiss_incomplete_occurrence',
                'occurrence_id': '10',
            })
        self.assertEqual(response.status_code, 302)
        dismiss.assert_called_once_with(10, 'eventos')

    def test_venue_mutations_reject_missing_csrf_token(self):
        with patch(
            'flask_app.routes.eventos.db.link_incomplete_event_occurrence_to_venue'
        ) as link:
            response = self.client.post('/eventos/locais', data={
                'action': 'link_incomplete_occurrence',
                'occurrence_id': '9',
                'venue_id': '3',
            })
        self.assertEqual(response.status_code, 302)
        link.assert_not_called()

    def test_venue_save_requires_name_and_address(self):
        with self.assertRaisesRegex(ValueError, 'nome e a localização'):
            eventos.save_event_venue({'name': 'Sem morada'})

    def test_incomplete_venue_queue_only_includes_unlinked_missing_identities(self):
        cursor = _Cursor()
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            eventos.get_incomplete_venue_occurrences()
        statement = cursor.queries[0][0]
        self.assertIn('eo.venue_id IS NULL', statement)
        self.assertIn("NULLIF(BTRIM(COALESCE(eo.venue, '')), '') IS NULL", statement)
        self.assertIn("NULLIF(BTRIM(COALESCE(eo.venue_address, '')), '') IS NULL", statement)
        self.assertIn('json_agg(', statement)
        self.assertIn('LEFT JOIN event_venues candidate', statement)
        self.assertIn('venue_review_dismissed_at IS NULL', statement)

    def test_dismissing_incomplete_location_is_atomic_and_audited(self):
        cursor = _Cursor([{
            'event_id': 12, 'venue': 'Texto antigo', 'venue_address': None,
        }])
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            eventos.dismiss_incomplete_event_occurrence(9, actor='gestor')
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('venue_review_dismissed_at=NOW()', statements)
        self.assertIn('venue_id IS NULL', statements)
        self.assertIn('venue_review_dismissed', statements)
        self.assertIn('event_history', statements)

    def test_pipeline_query_has_whitelisted_ordering(self):
        cursor = _Cursor()
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            eventos.get_events(sort_by='budget', sort_direction='desc')
        query = cursor.queries[-1][0]
        self.assertIn('ORDER BY quote_total DESC', query)

    def test_manual_location_link_is_audited_without_editing_historical_text(self):
        cursor = _Cursor([{'event_id': 12}])
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            eventos.link_event_occurrence_to_venue(9, 3, actor='gestor')
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('SET venue_id=%s', statements)
        self.assertIn('event_venue_history', statements)
        self.assertIn('event_history', statements)
        self.assertNotIn('SET venue=', statements)

    def test_manual_location_link_rejects_forged_or_already_linked_occurrence(self):
        cursor = _Cursor()
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'não está disponível'):
                eventos.link_event_occurrence_to_venue(9, 3, actor='gestor')
        statement = cursor.queries[0][0]
        self.assertIn('eo.venue_id IS NULL', statement)
        self.assertIn('eo.venue', statement)
        self.assertIn('eo.venue_address', statement)

    def test_incomplete_location_link_rejects_any_occurrence_outside_review_queue(self):
        cursor = _Cursor()
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'não está disponível'):
                eventos.link_incomplete_event_occurrence_to_venue(9, 3, actor='gestor')
        statement = cursor.queries[0][0]
        self.assertIn('eo.venue_id IS NULL', statement)
        self.assertIn("NULLIF(BTRIM(COALESCE(eo.venue, '')), '') IS NULL", statement)
        self.assertIn("NULLIF(BTRIM(COALESCE(eo.venue_address, '')), '') IS NULL", statement)

    def test_completing_incomplete_location_keeps_historical_text_and_is_audited(self):
        cursor = _Cursor([
            {'id': 9, 'event_id': 12, 'venue': 'Quinta', 'venue_address': None},
            {'id': 3},
        ])
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            venue_id = eventos.complete_event_occurrence_venue(
                9, {'name': 'Quinta da Serra', 'address': 'Rua Principal 1'}, actor='gestor'
            )
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertEqual(venue_id, 3)
        self.assertIn('FOR UPDATE', statements)
        self.assertIn('SET venue_id=%s', statements)
        self.assertIn('incomplete_occurrence_completed', statements)
        self.assertIn('event_venue_history', statements)
        self.assertIn('event_history', statements)
        self.assertNotIn('SET venue=', statements)

    def test_venue_save_persists_only_resolved_mapping_coordinates(self):
        cursor = _Cursor([{'id': 3}])
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            venue_id = eventos.save_event_venue({
                'name': 'Quinta da Serra', 'address': 'Rua Principal 1',
                'geocode_address': 'Rua Principal 1',
                'latitude': '38.7200', 'longitude': '-9.1400',
                'geocode_provider': 'nominatim', 'geocode_failed': False,
            }, actor='gestor')
        self.assertEqual(venue_id, 3)
        insert = next(query for query, _ in cursor.queries if 'INSERT INTO event_venues' in query)
        self.assertIn('latitude, longitude', insert)
        self.assertIn('geocode_provider, geocode_failed', insert)

    def test_venue_address_change_clears_coordinates_without_matching_resolution(self):
        cursor = _Cursor([
            {
                'address_key': 'rua antiga 1', 'latitude': 38.7,
                'longitude': -9.1, 'geocode_provider': 'nominatim',
                'geocode_failed': False,
            },
            {'id': 3},
        ])
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            eventos.save_event_venue({
                'name': 'Quinta da Serra', 'address': 'Rua Nova 2',
            }, venue_id=3)
        update_params = next(
            params for query, params in cursor.queries
            if 'UPDATE event_venues SET' in query
        )
        self.assertEqual(update_params[-5:-1], (None, None, None, False))

    def test_batch_geocoding_is_limited_and_retry_bypasses_failed_cache(self):
        venues = [
            {'id': 1, 'address': 'Rua Um 1, Porto', 'address_key': 'rua um 1, porto'},
            {'id': 2, 'address': 'Rua Dois 2, Porto', 'address_key': 'rua dois 2, porto'},
        ]
        results = [
            {'latitude': 41.1, 'longitude': -8.6, 'provider': 'nominatim', 'failed': False},
            {'failed': True, 'reason': 'not_found'},
        ]
        with patch.object(event_portal.event_db, 'get_event_venues_for_geocoding',
                          return_value=venues) as select, \
             patch.object(event_portal.event_db, 'event_venue_geocode_batch_lock',
                          return_value=nullcontext(True)), \
             patch.object(event_portal, 'resolve_event_address',
                          side_effect=results) as resolve, \
             patch.object(event_portal.event_db, 'save_event_venue_geocode_result',
                          return_value=True) as save:
            outcome = event_portal.validate_historical_event_venues(
                retry_failed=True, actor='gestor', batch_size=2,
                request_interval=0, sleep=lambda _: None,
            )
        self.assertEqual(outcome, {
            'processed': 2, 'succeeded': 1, 'failed': 1, 'busy': False,
        })
        select.assert_called_once_with(limit=2, retry_failed=True)
        self.assertTrue(resolve.call_args_list[0].kwargs['force_refresh'])
        self.assertEqual(save.call_count, 2)

    def test_concurrent_batch_does_not_call_mapping_provider(self):
        with patch.object(event_portal.event_db, 'event_venue_geocode_batch_lock',
                          return_value=nullcontext(False)), \
             patch.object(event_portal, 'resolve_event_address') as resolve:
            outcome = event_portal.validate_historical_event_venues()
        self.assertTrue(outcome['busy'])
        self.assertEqual(outcome['processed'], 0)
        resolve.assert_not_called()

    def test_failed_geocode_clears_coordinates_and_is_audited(self):
        cursor = _Cursor([{'id': 3}])
        connection = _Connection(cursor)
        with patch('db.eventos.db_connection', return_value=connection):
            saved = eventos.save_event_venue_geocode_result(
                3, 'rua principal', {'failed': True, 'reason': 'not_found'},
                actor='gestor',
            )
        self.assertTrue(saved)
        update_params = cursor.queries[0][1]
        self.assertEqual(update_params[:4], (None, None, None, True))
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('address_key=%s', statements)
        self.assertIn('geocode_failed', statements)

    def test_location_migration_is_additive_and_keeps_occurrence_text(self):
        cursor = _Cursor([(True,)])
        connection = _Connection(cursor)
        with patch('db.schema.db_connection', return_value=connection):
            schema.run_migrations_eventos_v2_foundation()
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_venues', statements)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_venue_history', statements)
        self.assertIn('ADD COLUMN IF NOT EXISTS venue_id', statements)
        self.assertIn('venue_review_dismissed_at', statements)
        self.assertIn('ADD COLUMN IF NOT EXISTS latitude', statements)
        self.assertIn('ADD COLUMN IF NOT EXISTS email_key', statements)
        self.assertIn('ON CONFLICT (name_key, address_key) DO NOTHING', statements)
        self.assertNotIn('DELETE FROM event_occurrences', statements)

    def test_client_consolidation_uses_exact_email_and_preserves_consent(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [(4,)]
        client_id = eventos._sync_event_client_cursor(
            cursor, 12, 'Cliente Histórico', ' CLIENTE@Example.com ',
            '+351 912 345 678', marketing_consent=True,
        )
        self.assertEqual(client_id, 4)
        statements = '\n'.join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn('WHERE email_key=%s', statements)
        self.assertIn('marketing_consent=marketing_consent OR %s', statements)
        self.assertIn('WHERE id=%s AND client_id IS NULL', statements)

    def test_client_consolidation_leaves_ambiguous_identity_unlinked(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [(4,), (5,)]
        client_id = eventos._sync_event_client_cursor(
            cursor, 12, 'Cliente', 'cliente@example.com', None,
        )
        self.assertIsNone(client_id)
        statements = '\n'.join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertNotIn('UPDATE events SET client_id=', statements)

    def test_manual_client_writes_replace_normalized_identity_keys(self):
        create_cursor = _Cursor([(6,)])
        with patch(
            'db.eventos.db_connection',
            return_value=_Connection(create_cursor),
        ):
            client_id = eventos.create_event_client({
                'name': 'Cliente Manual',
                'email': ' Cliente@Example.com ',
                'phone': '+351 912 345 678',
                'marketing_consent': False,
                'notes': None,
            })
        self.assertEqual(client_id, 6)
        create_params = create_cursor.queries[0][1]
        self.assertEqual(create_params['email_key'], 'cliente@example.com')
        self.assertEqual(create_params['phone_key'], '351912345678')

        update_cursor = _Cursor()
        with patch(
            'db.eventos.db_connection',
            return_value=_Connection(update_cursor),
        ):
            eventos.update_event_client(6, {
                'name': 'Cliente Manual',
                'email': 'novo@example.com',
                'phone': '',
                'marketing_consent': False,
                'notes': None,
            })
        update_params = update_cursor.queries[0][1]
        self.assertEqual(update_params['email_key'], 'novo@example.com')
        self.assertIsNone(update_params['phone_key'])
        self.assertIn('email_key=%(email_key)s', update_cursor.queries[0][0])


if __name__ == '__main__':
    unittest.main()