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

    def test_qualifies_portuguese_nifs_with_the_country_prefix(self):
        from db.faturas import qualify_supplier_nif

        self.assertEqual(qualify_supplier_nif('506454223'), 'PT506454223')
        self.assertEqual(qualify_supplier_nif('pt-506-454-223'), 'PT506454223')

    def test_foreign_nif_requires_a_recognized_prefix_and_keeps_it(self):
        from db.faturas import can_auto_match_supplier_nif, qualify_supplier_nif

        self.assertEqual(qualify_supplier_nif('es-b12345678'), 'ESB12345678')
        self.assertTrue(can_auto_match_supplier_nif('ESB12345678'))
        self.assertFalse(can_auto_match_supplier_nif('ZZ12345678'))
        with self.assertRaises(ValueError):
            qualify_supplier_nif('ZZ12345678')

    def test_missing_invalid_and_company_nifs_are_rejected(self):
        from db.faturas import qualify_supplier_nif

        for nif in (None, '', 'PT501234567', '5012345670',
                    '516388819', 'PT516388819', 'ES1'):
            with self.subTest(nif=nif), self.assertRaises(ValueError):
                qualify_supplier_nif(nif)


class TestQualifiedInvoicePersistence(unittest.TestCase):
    @patch('db.faturas.db_connection')
    def test_manual_finalization_stores_pt_prefix_for_legacy_supplier_nif(self, db_connection):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor
        db_connection.return_value.__enter__.return_value = conn
        cursor.fetchone.side_effect = [
            (10, 'Fornecedor', '506454223'),
            (91,),
        ]

        from db.faturas import create_invoice

        invoice_id = create_invoice({
            'supplier_id': 10,
            'supplier_name': 'Fornecedor',
            'supplier_nif': '506454223',
            'invoice_number': 'FT 1',
            'amount_eur': 12.5,
            'vat_amount_eur': 0,
            'issue_date': None,
            'due_date': None,
            'category': None,
            'onedrive_subfolder': None,
            'onedrive_path': None,
            'onedrive_web_url': None,
            'pdf_filename': None,
            'pdf_data': None,
            'status': 'pending_review',
            'ocr_confidence': None,
            'ocr_raw': None,
            'created_by': 'ana',
            'notes': None,
            'document_type': 'fatura',
            'source': 'manual',
            'centro_custo_id': None,
            'categoria_custo_id': None,
        }, validate_supplier_nif=True)

        self.assertEqual(invoice_id, 91)
        insert_call = next(
            call for call in cursor.execute.call_args_list
            if 'INSERT INTO invoices' in call.args[0]
        )
        self.assertEqual(insert_call.args[1]['supplier_nif'], 'PT506454223')

    @patch('db.faturas.db_connection')
    def test_ocr_finalization_stores_pt_prefix_for_legacy_supplier_nif(self, db_connection):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor
        db_connection.return_value.__enter__.return_value = conn
        cursor.fetchone.side_effect = [
            (10,),
            (10, 'Fornecedor', '506454223'),
            (10, 'fatura'),
            ('Fornecedor', '506454223', 10, 'draft', 'fatura'),
            ('Fornecedor',),
            ('Fornecedor',),
        ]

        from db.faturas import update_invoice

        update_invoice(91, {
            'supplier_id': 10,
            'supplier_name': 'Fornecedor',
            'supplier_nif': '506454223',
            'document_type': 'fatura',
            'status': 'pending_review',
        }, validate_supplier_nif=True)

        update_call = next(
            call for call in cursor.execute.call_args_list
            if str(call.args[0]).startswith('UPDATE invoices SET')
        )
        self.assertIn('PT506454223', update_call.args[1])


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

    def test_reviewed_invoice_preserves_a_confirmed_foreign_prefix(self):
        from flask_app.services import ServiceError
        from flask_app.services.faturas import save_reviewed_invoice

        with patch('database.get_invoice', return_value={
                'id': 4, 'status': 'draft', 'supplier_id': None,
                'supplier_name': 'Fornecedor ES', 'supplier_nif': 'ESB12345678',
        }), patch('database.upsert_supplier', return_value=9) as upsert_supplier, \
                patch('database.update_invoice') as update_invoice, \
                patch('database.link_invoices_to_supplier_by_name'), \
                patch('db.connection.db_connection') as db_connection:
            audit_conn = MagicMock()
            audit_conn.cursor.return_value = MagicMock()
            db_connection.return_value.__enter__.return_value = audit_conn

            result = save_reviewed_invoice(4, {
                'is_new_supplier': '1',
                'supplier_name': 'Fornecedor ES',
                'supplier_nif': 'es-b12345678',
                'supplier_payment_method': 'transferencia',
                'supplier_payment_terms': '30_dias',
                'document_type': 'fatura',
            }, changed_by='ana')

        self.assertIsNone(result.get('warning'))
        upsert_supplier.assert_called_once()
        self.assertEqual(upsert_supplier.call_args.kwargs['nif'], 'ESB12345678')
        update_call = update_invoice.call_args
        self.assertEqual(update_call.args[1]['supplier_nif'], 'ESB12345678')
        self.assertTrue(update_call.kwargs['validate_supplier_nif'])

    def test_missing_or_invalid_nif_blocks_new_supplier_creation(self):
        from flask_app.services import ServiceError
        from flask_app.services.faturas import save_reviewed_invoice

        for nif in ('', 'PT501234567'):
            with self.subTest(nif=nif), \
                    patch('database.get_invoice', return_value={
                        'id': 4, 'status': 'draft', 'supplier_id': None,
                        'supplier_name': 'Fornecedor', 'supplier_nif': None,
                    }), patch('database.upsert_supplier') as upsert_supplier, \
                    patch('database.update_invoice') as update_invoice:
                with self.assertRaises(ServiceError):
                    save_reviewed_invoice(4, {
                        'is_new_supplier': '1',
                        'supplier_name': 'Fornecedor',
                        'supplier_nif': nif,
                        'supplier_payment_method': 'transferencia',
                        'supplier_payment_terms': '30_dias',
                        'document_type': 'fatura',
                    }, changed_by='ana')
                upsert_supplier.assert_not_called()
                update_invoice.assert_not_called()


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