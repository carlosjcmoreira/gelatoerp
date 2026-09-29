import unittest
from io import BytesIO
from unittest.mock import patch

from flask import Flask, session
from openpyxl import Workbook


def _build_invoice_workbook(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['Fornecedor', 'NIF', 'Fatura', 'Valor'])
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    stream.seek(0)
    return stream


class TestInvoiceExcelImportNifQualification(unittest.TestCase):
    def test_mixed_rows_preserve_nifs_and_report_invalid_rows(self):
        from flask_app.routes import faturas as invoice_routes

        workbook = _build_invoice_workbook([
            ['Fornecedor PT', '506454223', 'FT-PT-1', -10],
            ['Fornecedor PT', 'PT 506 454 223', 'FT-PT-2', -11],
            ['Fornecedor ES', 'ESB12345678', 'FT-ES', -12],
            ['Fornecedor sem NIF', None, 'FT-SEM-NIF', -13],
            ['Fornecedor PT inválido', 'PT501234567', 'FT-PT-INV', -14],
            ['Fornecedor sem país', 'ZZ12345678', 'FT-PAÍS-INV', -15],
        ])
        app = Flask(__name__)
        app.secret_key = 'test-secret'

        with app.test_request_context('/faturas/upload', method='POST'):
            session['user'] = {'username': 'test-user'}
            with patch.object(invoice_routes, 'upsert_supplier',
                              side_effect=[1, 1, 2]) as upsert_supplier, \
                    patch.object(invoice_routes, 'get_supplier_by_id',
                                 return_value={
                                     'name': 'Fornecedor canónico',
                                     'nif': None,
                                 }), \
                    patch.object(invoice_routes, 'get_invoice_status_labels_map',
                                 return_value={'pending_review': 'Pendente'}), \
                    patch.object(invoice_routes, 'create_invoice') as create_invoice, \
                    patch.object(invoice_routes, 'flash') as flash, \
                    patch.object(invoice_routes, 'url_for', return_value='/faturas'), \
                    patch.object(invoice_routes, 'redirect',
                                 side_effect=lambda url: f'redirect:{url}'):
                result = invoice_routes._handle_excel_import(workbook, ext='xlsx')

        self.assertEqual(result, 'redirect:/faturas')
        self.assertEqual(upsert_supplier.call_count, 3)
        self.assertEqual(create_invoice.call_count, 3)

        imported_nifs = [
            call.kwargs['nif'] for call in upsert_supplier.call_args_list
        ]
        self.assertEqual(
            imported_nifs,
            ['PT506454223', 'PT506454223', 'ESB12345678'],
        )
        invoice_calls = [call.args[0] for call in create_invoice.call_args_list]
        self.assertEqual(
            [data['supplier_nif'] for data in invoice_calls],
            ['PT506454223', 'PT506454223', 'ESB12345678'],
        )
        self.assertTrue(all(
            call.kwargs.get('validate_supplier_nif') is True
            for call in create_invoice.call_args_list
        ))
        self.assertEqual(
            [data['supplier_id'] for data in invoice_calls],
            [1, 1, 2],
        )

        message, category = flash.call_args.args
        self.assertEqual(category, 'warning')
        self.assertIn('3 faturas importadas', message)
        self.assertIn('3 linha(s) com erro', message)
        self.assertIn('Linha 5:', message)
        self.assertIn('Linha 6:', message)
        self.assertIn('Linha 7:', message)
        self.assertIn('obrigatório', message.lower())
        self.assertIn('inválido', message.lower())
        self.assertIn('indicativo', message.lower())


if __name__ == '__main__':
    unittest.main()