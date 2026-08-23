"""Contract tests for the v2 Eventos foundation.

These tests deliberately use recording connections.  They protect domain rules
without requiring a developer machine to carry a copy of the production data.
"""

import unittest
from unittest.mock import patch

from flask import Flask

from db import eventos
from db import schema
from flask_app.routes.eventos import eventos_bp


class _Cursor:
    def __init__(self, results=()):
        self.results = list(results)
        self.queries = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.results.pop(0) if self.results else None

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_kwargs):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class EventPricingTests(unittest.TestCase):
    def test_calculate_quote_line_keeps_gross_and_derives_exact_vat(self):
        line = eventos.calculate_quote_line(1, '31.80', '0.13')

        self.assertEqual(str(line['unit_price_gross']), '31.80')
        self.assertEqual(str(line['total_gross']), '31.80')
        self.assertEqual(str(line['total_net']), '28.14')
        self.assertEqual(str(line['total_vat']), '3.66')

    def test_unknown_historical_vat_is_not_invented(self):
        line = eventos.calculate_quote_line(2, '10', None)

        self.assertEqual(str(line['total_gross']), '20.00')
        self.assertIsNone(line['total_net'])
        self.assertIsNone(line['total_vat'])

    def test_legacy_statuses_normalise_to_agreed_pipeline(self):
        self.assertEqual(eventos.normalize_event_status('lead'), 'novos')
        self.assertEqual(eventos.normalize_event_status('proposal_sent'), 'enviado')
        self.assertEqual(eventos.normalize_event_status('won'), 'adjudicado')
        self.assertEqual(eventos.normalize_event_status('lost'), 'rejeitado')


class EventTransitionTests(unittest.TestCase):
    def test_invalid_transition_is_refused_after_legacy_normalisation(self):
        cursor = _Cursor([{'status': 'lead'}])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            ok, message = eventos.transition_event_status(7, 'adjudicado')

        self.assertFalse(ok)
        self.assertIn("novos", message)
        self.assertEqual(len(cursor.queries), 1)

    def test_transition_writes_status_and_append_only_history(self):
        cursor = _Cursor([
            {
                'status': 'enviado',
                'event_date': None,
                'invoice_amount_eur': None,
                'expected_payment_date': None,
                'deposit_amount_eur': None,
            },
            {'total': 100},
            {'value_gross': 15},
        ])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            ok, message = eventos.transition_event_status(
                7, 'adjudicado', actor='equipa@example.test'
            )

        self.assertTrue(ok)
        self.assertEqual(message, 'OK')
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('UPDATE events SET status=', statements)
        self.assertIn('INSERT INTO event_history', statements)
        history_call = next(
            params for query, params in cursor.queries if 'INSERT INTO event_history' in query
        )
        self.assertEqual(history_call[2], 'enviado')
        self.assertEqual(history_call[3], 'adjudicado')
        self.assertEqual(history_call[4], 'equipa@example.test')

    def test_sinalizado_requires_a_validated_deposit(self):
        cursor = _Cursor([{
            'status': 'adjudicado',
            'event_date': None,
            'invoice_amount_eur': 100,
            'expected_payment_date': None,
            'deposit_amount_eur': 15,
            'deposit_received_at': None,
            'deposit_verified_by': None,
        }])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            ok, message = eventos.transition_event_status(7, 'sinalizado')

        self.assertFalse(ok)
        self.assertIn('comprovativo', message)


class EventDepositTests(unittest.TestCase):
    def test_deposit_cannot_be_lower_than_the_adjudicated_requirement(self):
        cursor = _Cursor([{
            'status': 'adjudicado',
            'deposit_amount_eur': 15,
            'invoice_amount_eur': 100,
        }])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'inferior'):
                eventos.validate_event_deposit(
                    7, '10', proof_reference='email confirmado', actor='equipa'
                )

    def test_validated_deposit_reserves_resources_and_records_status_transition(self):
        cursor = _Cursor([{
            'status': 'adjudicado', 'deposit_amount_eur': 15, 'invoice_amount_eur': 100,
        }, {'flavours': [], 'estimated_guests': None}])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            eventos.validate_event_deposit(7, '15', proof_reference='transferência', actor='equipa')

        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn("status = 'sinalizado'", statements)
        self.assertIn("SET status='reserved'", statements)
        self.assertGreaterEqual(statements.count('INSERT INTO event_history'), 2)


class EventReceiptTests(unittest.TestCase):
    def test_full_receipt_marks_event_received_and_is_audited(self):
        cursor = _Cursor([{
            'status': 'faturado', 'invoice_amount_eur': 100, 'payment_amount_eur': 0,
        }])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            complete = eventos.record_event_receipt(
                7, '100', '2026-09-01', payment_method='transferencia', actor='equipa'
            )

        self.assertTrue(complete)
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn("payment_status=%s", statements)
        self.assertIn("INSERT INTO event_history", statements)

    def test_partial_receipt_keeps_event_faturado(self):
        cursor = _Cursor([{
            'status': 'faturado', 'invoice_amount_eur': 100, 'payment_amount_eur': 0,
        }])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            complete = eventos.record_event_receipt(
                7, '30', '2026-09-01', payment_method='transferencia', actor='equipa'
            )

        self.assertFalse(complete)

    def test_deposit_is_included_when_settling_the_remaining_balance(self):
        cursor = _Cursor([{
            'status': 'faturado', 'invoice_amount_eur': 100, 'payment_amount_eur': 15,
        }])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            complete = eventos.record_event_receipt(
                7, '85', '2026-09-01', payment_method='transferencia', actor='equipa'
            )

        self.assertTrue(complete)
        update_params = next(
            params for query, params in cursor.queries if 'UPDATE events SET payment_amount_eur' in query
        )
        self.assertEqual(str(update_params[0]), '100.00')

    def test_receipt_cannot_exceed_the_remaining_balance(self):
        cursor = _Cursor([{
            'status': 'faturado', 'invoice_amount_eur': 100, 'payment_amount_eur': 15,
        }])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'exceder o saldo'):
                eventos.record_event_receipt(
                    7, '86', '2026-09-01', payment_method='transferencia', actor='equipa'
                )


class EventArchiveTests(unittest.TestCase):
    def test_archive_cancels_event_and_keeps_append_only_history(self):
        cursor = _Cursor([{'status': 'enviado'}])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            archived = eventos.delete_event(7, actor='equipa@example.test')

        self.assertTrue(archived)
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn("UPDATE events SET", statements)
        self.assertIn("INSERT INTO event_history", statements)
        self.assertNotIn("DELETE FROM events", statements)


class EventImportCompatibilityTests(unittest.TestCase):
    def test_sheet_sync_does_not_overwrite_locally_sinalizado_event(self):
        cursor = _Cursor([(7, 'sinalizado')])
        connection = _Connection(cursor)
        sheet_data = {
            'event_name': 'Evento importado',
            'event_type': 'Festa',
            'event_date': None,
            'event_time': None,
            'event_end_time': None,
            'estimated_guests': 20,
            'venue': None,
            'venue_address': None,
            'client_name': 'Cliente',
            'client_email': None,
            'client_phone': None,
        }

        with patch('db.eventos.db_connection', return_value=connection), \
             patch('db.eventos._upsert_primary_occurrence') as upsert_occurrence, \
             patch('db.eventos._insert_event_history') as add_history:
            event_id, is_new = eventos.upsert_event_from_sheet(99, sheet_data, 'adjudicado')

        self.assertEqual(event_id, 7)
        self.assertFalse(is_new)
        self.assertFalse(upsert_occurrence.called)
        self.assertFalse(add_history.called)
        self.assertFalse(any(
            'UPDATE events SET' in query for query, _ in cursor.queries
        ))

    def test_lead_conversion_creates_occurrence_and_audit_in_one_transaction(self):
        lead = {
            'id': 5,
            'client_name': 'Cliente',
            'event_type': 'Festa',
            'event_date': None,
            'event_time': None,
            'event_end_time': None,
            'estimated_guests': 20,
            'venue': None,
            'venue_address': None,
            'client_email': None,
            'client_phone': None,
            'internal_notes': None,
        }
        cursor = _Cursor([lead, {'id': 17}])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection), \
             patch('db.eventos._upsert_primary_occurrence') as upsert_occurrence, \
             patch('db.eventos._insert_event_history') as add_history:
            event_id = eventos.convert_lead_to_event(5)

        self.assertEqual(event_id, 17)
        upsert_occurrence.assert_called_once()
        add_history.assert_called_once()
        self.assertEqual(connection.commits, 1)


class EventResourcesTests(unittest.TestCase):
    def test_conflict_query_only_includes_active_reservations_and_events(self):
        cursor = _Cursor([[]])
        cursor.fetchone = lambda: None
        cursor.fetchall = lambda: []
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection):
            conflicts = eventos.get_resource_conflicts(
                3, '2026-09-12', '10:00', '14:00', exclude_occurrence_id=8
            )

        self.assertEqual(conflicts, [])
        query, params = cursor.queries[-1]
        self.assertIn("err.status IN ('requested', 'reserved')", query)
        self.assertIn("e.status NOT IN ('rejeitado', 'cancelado')", query)
        self.assertEqual(params[-2:], ('14:00', '10:00'))

    def test_reservation_conflict_requires_team_acknowledgement(self):
        cursor = _Cursor([{
            'event_id': 4,
            'event_date': '2026-09-12',
            'service_start_time': '10:00',
            'service_end_time': '14:00',
        }, None])
        connection = _Connection(cursor)

        with patch('db.eventos.db_connection', return_value=connection), \
             patch('db.eventos._get_resource_conflicts', return_value=[{'event_id': 99}]):
            saved, conflicts = eventos.reserve_event_resource(12, 3)

        self.assertFalse(saved)
        self.assertEqual(conflicts, [{'event_id': 99}])
        self.assertFalse(any(
            'INSERT INTO event_resource_reservations' in query
            for query, _ in cursor.queries
        ))
        self.assertTrue(any(
            'pg_advisory_xact_lock' in query for query, _ in cursor.queries
        ))

    def test_moving_reserved_occurrence_requires_explicit_conflict_acknowledgement(self):
        cursor = _Cursor()
        cursor.fetchall = lambda: [{'resource_id': 3}]

        with patch('db.eventos._get_resource_conflicts', return_value=[{'event_id': 99}]):
            with self.assertRaisesRegex(ValueError, 'conflito'):
                eventos._validate_occurrence_reservation_change(
                    cursor, 12, '2026-09-12', '10:00', '14:00'
                )

        self.assertTrue(any(
            'pg_advisory_xact_lock' in query for query, _ in cursor.queries
        ))

    def test_event_edit_stops_before_update_when_reserved_time_change_conflicts(self):
        cursor = _Cursor()
        connection = _Connection(cursor)
        data = {
            'event_name': 'Evento',
            'event_type': 'Festa',
            'event_date': None,
            'event_time': None,
            'event_end_time': None,
            'estimated_guests': None,
            'venue': None,
            'venue_address': None,
            'client_name': None,
            'client_email': None,
            'client_phone': None,
            'status': 'adjudicado',
            'loss_reason': None,
            'internal_notes': None,
        }

        with patch('db.eventos.db_connection', return_value=connection), \
             patch(
                 'db.eventos._upsert_primary_occurrence',
                 side_effect=ValueError('A nova data/horário entra em conflito'),
             ):
            with self.assertRaisesRegex(ValueError, 'conflito'):
                eventos.update_event(7, data)

        self.assertFalse(any('UPDATE events SET' in query for query, _ in cursor.queries))


class EventQuoteRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.register_blueprint(eventos_bp, url_prefix='/eventos')

    def test_edit_quote_item_does_not_depend_on_add_item_article_code(self):
        with self.app.test_client() as client, \
             patch('flask_app.routes.eventos.db.get_event', return_value={'status': 'novos'}), \
             patch('flask_app.routes.eventos.db.update_quote_item') as update_item:
            with client.session_transaction() as session:
                session['user'] = {'username': 'equipa', 'acesso_eventos': True}
            response = client.post(
                '/eventos/evento/7/quote',
                data={
                    'action': 'edit_item',
                    'item_id': '4',
                    'descricao': 'Serviço',
                    'quantidade': '1',
                    'preco_unitario': '120',
                    'taxa_iva': '23',
                },
            )

        self.assertEqual(response.status_code, 302)
        update_item.assert_called_once()

    def test_sent_quote_cannot_be_changed_without_returning_to_negotiation(self):
        with self.app.test_client() as client, \
             patch('flask_app.routes.eventos.db.get_event', return_value={'status': 'enviado'}), \
             patch('flask_app.routes.eventos.db.update_quote_item') as update_item:
            with client.session_transaction() as session:
                session['user'] = {'username': 'equipa', 'acesso_eventos': True}
            response = client.post(
                '/eventos/evento/7/quote',
                data={'action': 'edit_item', 'item_id': '4', 'descricao': 'Serviço'},
            )

        self.assertEqual(response.status_code, 302)
        update_item.assert_not_called()


class EventFoundationMigrationTests(unittest.TestCase):
    def test_migration_is_additive_and_backfills_without_legacy_deletion(self):
        cursor = _Cursor([(True,)])
        connection = _Connection(cursor)

        with patch('db.schema.db_connection', return_value=connection):
            schema.run_migrations_eventos_v2_foundation()

        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_occurrences', statements)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_resources', statements)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_resource_reservations', statements)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_pricing_settings', statements)
        self.assertIn('CREATE TABLE IF NOT EXISTS event_history', statements)
        self.assertIn("WHEN 'won' THEN 'adjudicado'", statements)
        self.assertIn('INSERT INTO event_occurrences', statements)
        self.assertNotIn('DROP TABLE eventos', statements)


if __name__ == '__main__':
    unittest.main()