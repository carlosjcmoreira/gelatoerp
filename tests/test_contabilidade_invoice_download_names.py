"""Cost-center suffixes for individual Contabilidade invoice downloads."""

import unittest
from unittest.mock import patch

from tests.test_contabilidade_centros_custo import _make_app
from werkzeug.http import parse_options_header


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class TestInvoiceDownloadCostCenterQuery(unittest.TestCase):
    def test_reads_multi_allocations_or_the_legacy_center_and_sorts_unique_names(self):
        from db import contabilidade

        cursor = _Cursor(rows=[
            ('Matosinhos',),
            ('Bolhão',),
            ('bolhão',),
            ('Matosinhos ',),
        ])
        with patch.object(
            contabilidade, 'db_connection', return_value=_Connection(cursor)
        ):
            names = contabilidade.get_cont_invoice_cost_center_names(41)

        self.assertEqual(names, ['Bolhão', 'Matosinhos'])
        sql, params = cursor.calls[0]
        self.assertIn('FROM invoice_centros_custo icc', sql)
        self.assertIn('FROM invoices i', sql)
        self.assertIn('NOT EXISTS', sql)
        self.assertEqual(params, (41, 41))


class TestContabilidadeInvoiceDownloadNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()
        cls.app.config['TESTING'] = True

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 7,
                'username': 'contabilidade-tester',
                'acesso_contabilidade': True,
            }

    def _download(self, filename, cost_centers, data=b'%PDF-1.7 sample'):
        with patch('db.faturas.get_invoice_pdf', return_value=(data, filename)) as get_pdf, \
                patch(
                    'db.contabilidade.get_cont_invoice_cost_center_names',
                    return_value=cost_centers,
                ) as get_centers:
            response = self.client.get('/contabilidade/fatura/41/pdf?dl=1')
        return response, get_pdf, get_centers

    def _content_disposition(self, response):
        disposition, params = parse_options_header(response.headers['Content-Disposition'])
        return disposition, params

    def test_single_center_is_added_before_pdf_extension(self):
        response, get_pdf, get_centers = self._download('fatura.pdf', ['Matosinhos'])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/pdf')
        disposition, params = self._content_disposition(response)
        self.assertEqual(disposition, 'attachment')
        self.assertEqual(params.get('filename'), 'fatura_Matosinhos.pdf')
        get_pdf.assert_called_once_with(41)
        get_centers.assert_called_once_with(41)

    def test_multiple_centers_are_sorted_deduplicated_and_safe_for_headers(self):
        response, _get_pdf, _get_centers = self._download(
            '../../fatura".pdf',
            ['Matosinhos', 'Bolhão', 'Matosinhos', '../Bolhão', 'A/B'],
        )

        self.assertEqual(response.status_code, 200)
        disposition, params = self._content_disposition(response)
        self.assertEqual(disposition, 'attachment')
        filename = params.get('filename', '')
        self.assertFalse(filename.startswith('/'))
        self.assertNotIn('/', filename)
        self.assertNotIn('"', filename)
        header = response.headers['Content-Disposition']
        self.assertIn('Bolh%C3%A3o', header)
        self.assertIn('Matosinhos', header)
        self.assertLess(header.index('A_B'), header.index('Matosinhos'))

    def test_no_center_keeps_original_name_without_suffix(self):
        response, _get_pdf, _get_centers = self._download(
            'fatura original.pdf', []
        )

        self.assertEqual(response.status_code, 200)
        disposition, params = self._content_disposition(response)
        self.assertEqual(disposition, 'attachment')
        self.assertEqual(params.get('filename'), 'fatura original.pdf')

    def test_image_download_keeps_image_extension_and_inline_preview_is_unchanged(self):
        png_data = b'\x89PNG\r\n\x1a\nimage data'
        response, _get_pdf, _get_centers = self._download(
            'scan.pdf', ['Matosinhos'], data=png_data
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')
        _disposition, params = self._content_disposition(response)
        self.assertEqual(params.get('filename'), 'scan_Matosinhos.png')

        with patch('db.faturas.get_invoice_pdf', return_value=(png_data, 'scan.pdf')), \
                patch(
                    'db.contabilidade.get_cont_invoice_cost_center_names'
                ) as get_centers:
            inline = self.client.get('/contabilidade/fatura/41/pdf')

        self.assertEqual(inline.status_code, 200)
        self.assertEqual(inline.mimetype, 'image/png')
        disposition, inline_params = self._content_disposition(inline)
        self.assertEqual(disposition, 'inline')
        self.assertEqual(inline_params.get('filename'), 'scan.png')
        get_centers.assert_not_called()

    def test_missing_document_returns_404_without_looking_up_centers(self):
        with patch('db.faturas.get_invoice_pdf', return_value=(None, None)), \
                patch(
                    'db.contabilidade.get_cont_invoice_cost_center_names'
                ) as get_centers:
            response = self.client.get('/contabilidade/fatura/41/pdf?dl=1')

        self.assertEqual(response.status_code, 404)
        get_centers.assert_not_called()


if __name__ == '__main__':
    unittest.main()