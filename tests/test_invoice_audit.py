"""
Integration tests for the invoice audit log system.

Run with: python -m pytest tests/test_invoice_audit.py -v
or:        python -m unittest tests.test_invoice_audit -v

Tests verify:
- creation events are recorded with actor
- field edits are tracked with before/after values and actor
- deletion tombstone captures full prior history (survives CASCADE)
- deleted invoice history is retrievable via get_invoice_deletion_log
- actor attribution is never 'sistema' for authenticated mutations
"""

import json
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, call, patch, ANY


# ---------------------------------------------------------------------------
# Helpers to build a fake cursor / connection pair
# ---------------------------------------------------------------------------

def _make_cursor(fetchone_results=None, fetchall_results=None, rowcount=1):
    """Return a mock cursor whose .fetchone() / .fetchall() rotate through results."""
    cur = MagicMock()
    cur.rowcount = rowcount

    _fo = list(fetchone_results or [])
    _fa = list(fetchall_results or [])

    def _fetchone():
        return _fo.pop(0) if _fo else None

    def _fetchall():
        return _fa.pop(0) if _fa else []

    cur.fetchone.side_effect = _fetchone
    cur.fetchall.side_effect = _fetchall
    return cur


def _make_conn(cur):
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Test: create_invoice writes a 'criação' audit entry
# ---------------------------------------------------------------------------

class TestCreateInvoiceAudit(unittest.TestCase):

    @patch('db.faturas.db_connection')
    def test_creation_audit_entry_is_inserted(self, mock_db_conn):
        """create_invoice must insert a 'criação' row into invoice_audit_log."""
        cur = _make_cursor(fetchone_results=[(42,)])  # RETURNING id
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import create_invoice
        # Use status='draft' — the realistic initial state for OCR uploads;
        # 'pending_review' for a 'fatura' requires supplier_id (business rule).
        result = create_invoice({
            'supplier_id': None,
            'supplier_name': 'ACME Lda',
            'supplier_nif': None,
            'invoice_number': 'INV-001',
            'amount_eur': 100.00,
            'vat_amount_eur': 23.00,
            'issue_date': date(2026, 1, 1),
            'due_date': date(2026, 1, 31),
            'store_id': None,
            'category': None,
            'onedrive_subfolder': None,
            'onedrive_path': None,
            'onedrive_web_url': None,
            'pdf_filename': None,
            'pdf_data': None,
            'status': 'draft',
            'ocr_confidence': None,
            'ocr_raw': None,
            'created_by': 'alice',
            'notes': None,
            'document_type': 'fatura',
        })

        self.assertEqual(result, 42)

        # Find the audit INSERT call.
        # Note: 'criação' is a SQL literal embedded in args[0], not in the params tuple.
        audit_calls = [
            c for c in cur.execute.call_args_list
            if len(c.args) >= 1
            and 'invoice_audit_log' in str(c.args[0])
            and 'criação' in str(c.args[0])
        ]
        self.assertTrue(
            len(audit_calls) >= 1,
            "Expected at least one INSERT into invoice_audit_log with campo='criação'"
        )
        # Actor must be the caller ('alice'), not 'sistema'
        audit_params = audit_calls[0].args[1]
        self.assertIn('alice', audit_params,
                      f"Audit entry actor should be 'alice', got {audit_params}")


# ---------------------------------------------------------------------------
# Test: update_invoice tracks field changes with correct actor
# ---------------------------------------------------------------------------

class TestUpdateInvoiceAudit(unittest.TestCase):

    @patch('db.faturas.db_connection')
    def test_field_change_recorded_with_actor(self, mock_db_conn):
        """update_invoice must record a diff row for each changed field."""
        # Simulate:
        # 1. No status-requiring-supplier guard needed (amount_eur change only)
        # 2. Old values SELECT → returns old amount
        # 3. UPDATE
        # 4. Audit INSERT
        cur = _make_cursor(fetchone_results=[
            # _AUDIT_TRACKED SELECT (amount_eur old value)
            (50.00,),
        ])
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import update_invoice
        update_invoice(99, {'amount_eur': 150.00}, changed_by='bob')

        audit_calls = [
            c for c in cur.execute.call_args_list
            if len(c.args) >= 2
            and 'invoice_audit_log' in str(c.args[0])
            and 'amount_eur' in str(c.args[1])
        ]
        self.assertTrue(len(audit_calls) >= 1,
                        "Expected audit INSERT for amount_eur change")

        # Actor must be 'bob', not 'sistema'
        actor_in_args = any(
            'bob' in str(c.args[1]) for c in audit_calls
        )
        self.assertTrue(actor_in_args,
                        "Audit row actor should be 'bob'")

    @patch('db.faturas.db_connection')
    def test_no_audit_row_when_value_unchanged(self, mock_db_conn):
        """update_invoice must NOT insert audit rows when the value is identical."""
        cur = _make_cursor(fetchone_results=[
            (100.00,),  # old amount_eur = same as new
        ])
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import update_invoice
        update_invoice(99, {'amount_eur': 100.00}, changed_by='bob')

        audit_calls = [
            c for c in cur.execute.call_args_list
            if 'invoice_audit_log' in str(c.args[0])
            and 'INSERT' in str(c.args[0])
        ]
        # If INSERT happens, the old and new values must differ — but here they're equal
        for ac in audit_calls:
            params = ac.args[1] if len(ac.args) > 1 else ()
            if 'amount_eur' in str(params):
                old_v, new_v = params[2], params[3]
                self.assertNotEqual(old_v, new_v,
                    "Audit row inserted even though value did not change")


# ---------------------------------------------------------------------------
# Test: delete_invoice preserves full audit history in tombstone
# ---------------------------------------------------------------------------

class TestDeleteInvoiceAudit(unittest.TestCase):

    @patch('db.faturas.db_connection')
    def test_tombstone_captures_prior_audit_and_deletion_event(self, mock_db_conn):
        """delete_invoice must write a tombstone with prior audit history + eliminação event."""

        prior_log_rows = [
            ('criação', None, 'fatura · ACME Lda · 100.0 €', 'alice', datetime(2026, 1, 1, 10, 0)),
            ('status',  'pending_review', 'scheduled', 'bob', datetime(2026, 1, 5, 14, 0)),
        ]

        cur = _make_cursor(
            fetchone_results=[
                # Snapshot SELECT
                ('INV-001', 'ACME Lda', 100.00, 'fatura', 'scheduled'),
            ],
            fetchall_results=[
                # invoice_audit_log SELECT
                prior_log_rows,
            ]
        )
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import delete_invoice
        delete_invoice(99, deleted_by='carol')

        # Find tombstone INSERT
        tombstone_calls = [
            c for c in cur.execute.call_args_list
            if 'invoice_deletion_log' in str(c.args[0])
            and 'INSERT' in str(c.args[0])
        ]
        self.assertTrue(len(tombstone_calls) == 1,
                        "Expected exactly one INSERT into invoice_deletion_log")

        params = tombstone_calls[0].args[1]
        # invoice_id
        self.assertEqual(params[0], 99)
        # deleted_by
        self.assertEqual(params[6], 'carol')
        # prior_audit_json must be valid JSON containing both prior rows + the eliminação event
        prior_audit = json.loads(params[7])
        self.assertIsInstance(prior_audit, list)
        event_types = [e['campo_alterado'] for e in prior_audit]
        self.assertIn('criação', event_types)
        self.assertIn('status', event_types)
        self.assertIn('eliminação', event_types,
                      "Tombstone JSON must include the 'eliminação' final event")

        # The eliminação event must be last and attribute correctly
        last = prior_audit[-1]
        self.assertEqual(last['campo_alterado'], 'eliminação')
        self.assertEqual(last['alterado_por'], 'carol')

    @patch('db.faturas.db_connection')
    def test_actual_delete_is_issued(self, mock_db_conn):
        """After writing the tombstone, delete_invoice must DELETE the row."""
        cur = _make_cursor(
            fetchone_results=[('INV-001', 'ACME', 100.00, 'fatura', 'pending_review')],
            fetchall_results=[[]]
        )
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import delete_invoice
        delete_invoice(77, deleted_by='dave')

        delete_calls = [
            c for c in cur.execute.call_args_list
            if 'DELETE FROM invoices' in str(c.args[0])
        ]
        self.assertEqual(len(delete_calls), 1)
        self.assertIn(77, delete_calls[0].args[1])


# ---------------------------------------------------------------------------
# Test: get_invoice_deletion_log retrieves tombstone and reconstructs timeline
# ---------------------------------------------------------------------------

class TestGetInvoiceDeletionLog(unittest.TestCase):

    @patch('db.faturas.db_connection')
    def test_returns_none_when_not_found(self, mock_db_conn):
        cur = _make_cursor(fetchone_results=[None])
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import get_invoice_deletion_log
        result = get_invoice_deletion_log(9999)
        self.assertIsNone(result)

    @patch('db.faturas.db_connection')
    def test_returns_tombstone_with_reconstructed_audit(self, mock_db_conn):
        sample_audit = [
            {'campo_alterado': 'criação', 'valor_anterior': None, 'valor_novo': 'fatura · ACME',
             'alterado_por': 'alice', 'alterado_em': '2026-01-01T10:00:00'},
            {'campo_alterado': 'eliminação', 'valor_anterior': 'pending_review', 'valor_novo': None,
             'alterado_por': 'carol', 'alterado_em': '2026-02-01T09:00:00'},
        ]
        cur = _make_cursor(fetchone_results=[(
            1,                          # id
            'INV-001',                  # invoice_number
            'ACME Lda',                 # supplier_name
            100.00,                     # amount_eur
            'fatura',                   # document_type
            'pending_review',           # status_at_deletion
            'carol',                    # deleted_by
            datetime(2026, 2, 1, 9, 0), # deleted_at
            json.dumps(sample_audit),   # prior_audit_json
        )])
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import get_invoice_deletion_log
        result = get_invoice_deletion_log(42)

        self.assertIsNotNone(result)
        self.assertEqual(result['invoice_id'], 42)
        self.assertEqual(result['deleted_by'], 'carol')
        self.assertEqual(len(result['prior_audit']), 2)
        self.assertEqual(result['prior_audit'][-1]['campo_alterado'], 'eliminação')


# ---------------------------------------------------------------------------
# Test: actor attribution is never 'sistema' in authenticated paths
# ---------------------------------------------------------------------------

class TestActorAttribution(unittest.TestCase):
    """Verify that the default 'sistema' is only used when no actor is provided."""

    @patch('db.faturas.db_connection')
    def test_explicit_actor_overrides_sistema(self, mock_db_conn):
        """When changed_by is supplied, it must appear in the audit row."""
        cur = _make_cursor(fetchone_results=[(99.00,)])
        conn = _make_conn(cur)
        mock_db_conn.return_value = conn

        from db.faturas import update_invoice
        update_invoice(1, {'amount_eur': 200.00}, changed_by='manager_user')

        actors_in_audit = [
            c.args[1][-1]  # last positional param = alterado_por
            for c in cur.execute.call_args_list
            if 'invoice_audit_log' in str(c.args[0])
            and 'INSERT' in str(c.args[0])
            and len(c.args) > 1
        ]
        for actor in actors_in_audit:
            self.assertEqual(actor, 'manager_user',
                             f"Audit row actor should be 'manager_user', got '{actor}'")


if __name__ == '__main__':
    unittest.main()
