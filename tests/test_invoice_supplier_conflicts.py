"""Regression tests for safe supplier identity handling on invoices."""

import unittest
from unittest.mock import MagicMock, patch


class TestPortugueseNifValidation(unittest.TestCase):
    def test_valid_portuguese_nif_is_accepted_with_or_without_prefix(self):
        from db.faturas import is_valid_portuguese_nif

        self.assertTrue(is_valid_portuguese_nif('506454223'))
        self.assertTrue(is_valid_portuguese_nif('PT506454223'))

    def test_invalid_auchan_nif_is_not_safe_for_ocr_matching(self):
        from db.faturas import can_auto_match_supplier_nif, is_valid_portuguese_nif

        self.assertFalse(is_valid_portuguese_nif('PT501234567'))
        self.assertFalse(can_auto_match_supplier_nif('PT501234567'))

    def test_foreign_tax_identifier_is_not_rejected_by_portuguese_checksum(self):
        from db.faturas import can_auto_match_supplier_nif

        self.assertTrue(can_auto_match_supplier_nif('ESB12345678'))

    def test_malformed_bare_numeric_identifier_is_not_auto_matched(self):
        from db.faturas import can_auto_match_supplier_nif

        self.assertFalse(can_auto_match_supplier_nif('50123456'))
        self.assertFalse(can_auto_match_supplier_nif('5012345670'))


class TestCentralSupplierIdentityGuard(unittest.TestCase):
    def test_conflict_requires_explicit_confirmation(self):
        from db.faturas import (
            SupplierIdentityConflict,
            _canonicalize_invoice_supplier_data,
        )

        cursor = MagicMock()
        cursor.fetchone.return_value = (10, 'LACTOGAL, S.A.', 'PT500000000')

        with self.assertRaises(SupplierIdentityConflict):
            _canonicalize_invoice_supplier_data(cursor, {
                'supplier_id': 10,
                'supplier_name': 'Auchan Gondomar',
                'supplier_nif': '501234567',
            }, 10)

    def test_changing_linked_supplier_requires_confirmation_even_with_canonical_fields(self):
        from db.faturas import (
            SupplierIdentityConflict,
            _canonicalize_invoice_supplier_data,
        )

        cursor = MagicMock()
        cursor.fetchone.return_value = (10, 'LACTOGAL, S.A.', 'PT506454223')

        with self.assertRaises(SupplierIdentityConflict):
            _canonicalize_invoice_supplier_data(cursor, {
                'supplier_id': 10,
                'supplier_name': 'LACTOGAL, S.A.',
                'supplier_nif': 'PT506454223',
            }, 10, previous_supplier_id=1)

    def test_confirmation_records_one_canonical_identity(self):
        from db.faturas import _canonicalize_invoice_supplier_data

        cursor = MagicMock()
        cursor.fetchone.return_value = (10, 'LACTOGAL, S.A.', 'PT506454223')

        prepared = _canonicalize_invoice_supplier_data(cursor, {
            'supplier_id': 10,
            'supplier_name': 'Auchan Gondomar',
            'supplier_nif': '501234567',
            'supplier_conflict_confirmed': '1',
        }, 10)

        self.assertEqual(prepared['supplier_id'], 10)
        self.assertEqual(prepared['supplier_name'], 'LACTOGAL, S.A.')
        self.assertEqual(prepared['supplier_nif'], 'PT506454223')
        self.assertNotIn('supplier_conflict_confirmed', prepared)


class TestOcrSupplierMatching(unittest.TestCase):
    @patch('database.create_invoice', return_value=91)
    @patch('database.get_supplier_by_nif')
    @patch('flask_app.ocr_invoice.extract_invoice_fields')
    def test_pdf_with_invalid_nif_stays_unlinked(
            self, extract_fields, get_supplier_by_nif, create_invoice):
        extract_fields.return_value = {
            'supplier_name': 'Lactogal',
            'supplier_nif': 'PT501234567',
            'document_type_hint': 'fatura',
        }

        from flask_app.services.faturas import create_draft_from_pdf
        invoice_id = create_draft_from_pdf(b'%PDF-test', 'fatura.pdf', 'ana')

        self.assertEqual(invoice_id, 91)
        get_supplier_by_nif.assert_not_called()
        saved = create_invoice.call_args.args[0]
        self.assertIsNone(saved['supplier_id'])
        self.assertEqual(saved['supplier_nif'], 'PT501234567')

    @patch('database.create_invoice', return_value=92)
    @patch('database.get_supplier_by_nif')
    @patch('flask_app.ocr_invoice.extract_invoice_fields_from_image')
    def test_image_with_valid_nif_can_link_supplier(
            self, extract_fields, get_supplier_by_nif, create_invoice):
        extract_fields.return_value = {
            'supplier_name': 'Lactogal',
            'supplier_nif': 'PT506454223',
            'document_type_hint': 'fatura',
        }
        get_supplier_by_nif.return_value = {
            'id': 10,
            'name': 'LACTOGAL, S.A.',
            'nif': 'PT506454223',
        }

        from flask_app.services.faturas import create_draft_from_image
        invoice_id = create_draft_from_image(b'image', 'fatura.jpg', 'ana')

        self.assertEqual(invoice_id, 92)
        get_supplier_by_nif.assert_called_once_with('PT506454223')
        saved = create_invoice.call_args.args[0]
        self.assertEqual(saved['supplier_id'], 10)
        self.assertEqual(saved['supplier_name'], 'LACTOGAL, S.A.')


class TestReviewedInvoiceConfirmation(unittest.TestCase):
    @patch('database.update_invoice')
    @patch('database.get_supplier_by_id')
    @patch('database.get_invoice')
    def test_supplier_change_without_confirmation_does_not_write(
            self, get_invoice, get_supplier_by_id, update_invoice):
        get_invoice.return_value = {
            'id': 4,
            'status': 'draft',
            'supplier_id': 1,
            'supplier_name': 'Auchan Gondomar',
            'supplier_nif': 'PT501234567',
        }
        get_supplier_by_id.return_value = {
            'id': 10,
            'name': 'LACTOGAL, S.A.',
            'nif': 'PT506454223',
        }

        from flask_app.services import ServiceError
        from flask_app.services.faturas import save_reviewed_invoice

        with self.assertRaises(ServiceError):
            save_reviewed_invoice(4, {
                'existing_supplier_id': '10',
                'supplier_name': 'LACTOGAL, S.A.',
                'supplier_nif': 'PT506454223',
                'document_type': 'fatura',
                'supplier_conflict_confirmed': '',
            }, changed_by='ana')

        update_invoice.assert_not_called()

    @patch('database.get_invoice_pdf')
    @patch('database.upsert_supplier')
    @patch('database.update_invoice')
    @patch('database.get_supplier_by_id')
    @patch('database.get_invoice')
    def test_conflict_is_rejected_before_supplier_creation_or_archive(
            self, get_invoice, get_supplier_by_id, update_invoice,
            upsert_supplier, get_invoice_pdf):
        get_invoice.return_value = {
            'id': 4,
            'status': 'draft',
            'supplier_id': 1,
            'supplier_name': 'Auchan Gondomar',
            'supplier_nif': 'PT501234567',
        }
        get_supplier_by_id.return_value = {
            'id': 1,
            'name': 'Auchan Gondomar',
            'nif': 'PT501234567',
        }

        from flask_app.services import ServiceError
        from flask_app.services.faturas import save_reviewed_invoice

        with self.assertRaises(ServiceError):
            save_reviewed_invoice(4, {
                'is_new_supplier': '1',
                'supplier_name': 'LACTOGAL, S.A.',
                'supplier_nif': 'PT506454223',
                'supplier_payment_method': 'transferencia',
                'supplier_payment_terms': '30_dias',
                'document_type': 'fatura',
                'onedrive_subfolder': 'Lacticínios',
                'supplier_conflict_confirmed': '',
            }, changed_by='ana')

        upsert_supplier.assert_not_called()
        update_invoice.assert_not_called()
        get_invoice_pdf.assert_not_called()


class TestInvoiceConflictExposure(unittest.TestCase):
    @patch('db.faturas.db_connection')
    def test_get_invoice_exposes_nif_only_conflict(self, db_connection):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor
        db_connection.return_value.__enter__.return_value = conn
        row = [None] * 41
        row[0] = 17
        row[1] = 3
        row[2] = 'Fornecedor Legal'
        row[3] = 'PT501234567'
        row[13] = 'pending_review'
        row[38] = 'Fornecedor Legal'
        row[39] = 'Fornecedor Legal'
        row[40] = 'PT506454223'
        cursor.fetchone.return_value = tuple(row)

        from db.faturas import get_invoice

        invoice = get_invoice(17)

        self.assertEqual(invoice['supplier_legal_nif'], 'PT506454223')
        self.assertTrue(invoice['supplier_identity_conflict'])


if __name__ == '__main__':
    unittest.main()