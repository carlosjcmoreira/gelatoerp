import unittest
from unittest.mock import patch

from flask_app import google_sheets_sync as sheets


class GoogleSheetsSyncTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            ['Nome', 'Email', 'Data do evento', 'Estado'],
            ['Cliente A', 'a@example.test', '10/10/2026', 'Contactado'],
            ['Cliente B', 'b@example.test', '11/10/2026', 'Aceite'],
        ]

    def test_sync_batches_leads_and_reports_progress_without_exposing_rows(self):
        progress = []
        lead_results = [(10, True), (11, False)]
        with patch.object(sheets, 'fetch_sheet_rows', return_value=self.rows), \
             patch('database.upsert_leads_from_sheet_batch', return_value=lead_results) as batch, \
             patch('database.upsert_event_from_sheet') as event_upsert:
            result = sheets.sync_leads_from_sheet(progress.append)

        self.assertEqual(result, (1, 1, 0))
        batch.assert_called_once()
        self.assertEqual(event_upsert.call_count, 2)
        self.assertEqual(progress[-1]['phase'], 'complete')
        self.assertEqual(progress[-1]['processed_rows'], 2)
        self.assertIn('lead_upsert_ms', progress[-1]['timings'])
        self.assertNotIn('rows', progress[-1])

    def test_fetch_failure_is_raised_so_durable_job_becomes_failed(self):
        with patch.object(sheets, 'fetch_sheet_rows', side_effect=RuntimeError('secret provider body')):
            with self.assertRaises(RuntimeError):
                sheets.sync_leads_from_sheet()

    def test_pending_job_success_is_persisted(self):
        completed = {'id': 7, 'status': 'succeeded', 'total_rows': 2}
        with patch(
                 'database.claim_event_sheet_sync',
                 return_value={'id': 7, 'lease_token': 'lease-7'},
             ), \
             patch.object(sheets, 'sync_leads_from_sheet', return_value=(1, 1, 0)), \
             patch('database.update_event_sheet_sync_run') as update, \
             patch('database.get_event_sheet_sync_run', return_value=completed):
            result = sheets.run_pending_sheet_sync_once()

        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(any(
            call.kwargs.get('status') == 'succeeded'
            for call in update.call_args_list
        ))

    def test_pending_job_failure_stores_only_exception_type(self):
        failed = {'id': 8, 'status': 'failed'}
        with patch(
                 'database.claim_event_sheet_sync',
                 return_value={'id': 8, 'lease_token': 'lease-8'},
             ), \
             patch.object(
                 sheets, 'sync_leads_from_sheet',
                 side_effect=RuntimeError('token and provider response'),
             ), \
             patch('database.update_event_sheet_sync_run') as update, \
             patch('database.get_event_sheet_sync_run', return_value=failed):
            sheets.run_pending_sheet_sync_once()

        failure = next(
            call for call in update.call_args_list
            if call.kwargs.get('status') == 'failed'
        )
        self.assertEqual(failure.kwargs['error_summary'], 'RuntimeError')
        self.assertEqual(failure.kwargs['lease_token'], 'lease-8')

    def test_failed_lead_batch_falls_back_and_skips_only_bad_row(self):
        progress = []

        def one_by_one(row_id, _data):
            if row_id == '2':
                raise ValueError('bad row')
            return 11, True

        with patch.object(sheets, 'fetch_sheet_rows', return_value=self.rows), \
             patch('database.upsert_leads_from_sheet_batch', side_effect=ValueError), \
             patch('database.upsert_lead_from_sheet', side_effect=one_by_one), \
             patch('database.upsert_event_from_sheet') as event_upsert:
            result = sheets.sync_leads_from_sheet(progress.append)

        self.assertEqual(result, (1, 0, 1))
        self.assertEqual(event_upsert.call_count, 1)

    def test_lease_loss_before_event_row_stops_further_mutations(self):
        def reject_lease(info):
            if info.get('phase') == 'importing_events':
                raise RuntimeError('sheet sync lease lost')

        with patch.object(sheets, 'fetch_sheet_rows', return_value=self.rows), \
             patch('database.upsert_leads_from_sheet_batch',
                   return_value=[(10, True), (11, True)]), \
             patch('database.upsert_event_from_sheet') as event_upsert:
            with self.assertRaisesRegex(RuntimeError, 'lease lost'):
                sheets.sync_leads_from_sheet(reject_lease)

        event_upsert.assert_not_called()


if __name__ == '__main__':
    unittest.main()