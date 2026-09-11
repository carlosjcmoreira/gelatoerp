"""Contracts for configurable pipeline views and consolidated event venues."""

import unittest
from unittest.mock import patch

from flask import Blueprint, Flask

from db import eventos, schema
from flask_app.routes.eventos import eventos_bp


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
             patch('flask_app.routes.eventos._get_tabs', return_value=[]), \
             patch('flask_app.routes.eventos.render_template', return_value='ok') as render:
            response = self.client.get('/eventos/locais')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.kwargs['incomplete_occurrences'], queue)

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
        ) as complete:
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

    def test_location_migration_is_additive_and_keeps_occurrence_text(self):
        cursor = _Cursor([(True,)])
        connection = _Connection(cursor)
        with patch('db.schema.db_connection', return_value=connection):
            schema.run_migrations_eventos_v2_foundation()
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_venues', statements)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_venue_history', statements)
        self.assertIn('ADD COLUMN IF NOT EXISTS venue_id', statements)
        self.assertIn('ON CONFLICT (name_key, address_key) DO NOTHING', statements)
        self.assertNotIn('DELETE FROM event_occurrences', statements)


if __name__ == '__main__':
    unittest.main()