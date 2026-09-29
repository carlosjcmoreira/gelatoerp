"""Selected Contabilidade invoice ZIP validation and content."""

import unittest
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

from tests.test_contabilidade_centros_custo import _make_app


def _record(invoice_id, filename='fatura.pdf', data=b'%PDF-1.7 sample', centers=None):
    return {
        'id': invoice_id,
        'pdf_data': data,
        'pdf_filename': filename,
        'cost_center_names': centers or [],
    }


def _metadata_records(records):
    return [
        {
            'id': record['id'],
            'pdf_filename': record['pdf_filename'],
            'pdf_size': len(record['pdf_data']) if record['pdf_data'] else None,
            'cost_center_names': record['cost_center_names'],
        }
        for record in records
    ]


def _data_records(records):
    return [
        {'id': record['id'], 'pdf_data': record['pdf_data']}
        for record in records
    ]


@contextmanager
def _patch_records(records):
    with patch(
        'db.contabilidade.get_cont_invoice_zip_metadata',
        return_value=_metadata_records(records),
    ) as get_metadata, patch(
        'db.contabilidade.get_cont_invoice_zip_data',
        return_value=_data_records(records),
    ) as get_data:
        yield get_metadata, get_data


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


class TestContabilidadeInvoiceZipQuery(unittest.TestCase):
    def test_fetches_only_contabilidade_invoices_and_cost_center_names(self):
        from db import contabilidade

        cursor = _Cursor(rows=[
            (31, 'a.pdf', 8, ['Matosinhos']),
        ])
        with patch.object(
            contabilidade, 'db_connection', return_value=_Connection(cursor)
        ):
            records = contabilidade.get_cont_invoice_zip_metadata([31])

        self.assertEqual(records, [{
            'id': 31,
            'pdf_filename': 'a.pdf',
            'pdf_size': 8,
            'cost_center_names': ['Matosinhos'],
        }])
        sql, params = cursor.calls[0]
        self.assertIn('i.id = ANY(%s)', sql)
        self.assertIn('octet_length(i.pdf_data)', sql)
        self.assertIn("i.status NOT IN ('draft', 'cancelled')", sql)
        self.assertIn('invoice_centros_custo', sql)
        self.assertEqual(params, ([31],))

    def test_document_data_query_stays_inside_the_accounting_list(self):
        from db import contabilidade

        cursor = _Cursor(rows=[(31, b'%PDF-1.7')])
        with patch.object(
            contabilidade, 'db_connection', return_value=_Connection(cursor)
        ):
            records = contabilidade.get_cont_invoice_zip_data([31])

        self.assertEqual(records, [{'id': 31, 'pdf_data': b'%PDF-1.7'}])
        sql, params = cursor.calls[0]
        self.assertIn('i.id = ANY(%s)', sql)
        self.assertIn("i.status NOT IN ('draft', 'cancelled')", sql)
        self.assertEqual(params, ([31],))

    def test_empty_id_list_does_not_query_database(self):
        from db import contabilidade

        with patch.object(contabilidade, 'db_connection') as db_connection:
            self.assertEqual(contabilidade.get_cont_invoice_zip_metadata([]), [])
            self.assertEqual(contabilidade.get_cont_invoice_zip_data([]), [])
        db_connection.assert_not_called()


class TestContabilidadeInvoiceZipRoute(unittest.TestCase):
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

    def _post(self, invoice_ids, zip_name='Faturas', **extra):
        payload = {'invoice_ids': invoice_ids, 'zip_name': zip_name}
        payload.update(extra)
        return self.client.post('/contabilidade/faturas/zip', json=payload)

    def test_multiple_documents_include_cost_centers_and_disambiguate_duplicate_names(self):
        records = [
            _record(32, 'fatura.pdf', b'%PDF-1.7 second', ['Matosinhos']),
            _record(31, 'fatura.pdf', b'%PDF-1.7 first', ['Matosinhos']),
        ]
        with _patch_records(records) as (get_metadata, get_data):
            response = self._post([32, 31], 'Faturas Setembro.zip')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/zip')
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertEqual(
            response.headers['X-Download-Filename'],
            'Faturas%20Setembro.zip',
        )
        get_metadata.assert_called_once_with([32, 31])
        get_data.assert_called_once_with([32, 31])

        with ZipFile(BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), [
                'Faturas Setembro/fatura_Matosinhos.pdf',
                'Faturas Setembro/fatura_Matosinhos_32.pdf',
            ])
            self.assertEqual(
                archive.read('Faturas Setembro/fatura_Matosinhos.pdf'),
                b'%PDF-1.7 first',
            )
            self.assertEqual(
                archive.read('Faturas Setembro/fatura_Matosinhos_32.pdf'),
                b'%PDF-1.7 second',
            )

    def test_malicious_zip_name_is_sanitized_for_folder_and_download(self):
        with _patch_records([_record(31, '../source.pdf', centers=['A/B'])]):
            response = self._post([31], '../../Faturas')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Download-Filename'], 'Faturas.zip')
        with ZipFile(BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ['Faturas/source_A_B.pdf'])
            self.assertFalse(any(name.startswith('/') or '..' in name for name in archive.namelist()))

    def test_rejects_empty_invalid_duplicate_and_oversized_id_selections(self):
        with patch('db.contabilidade.get_cont_invoice_zip_metadata') as get_records:
            cases = [
                ([], 400),
                (['not-an-id'], 400),
                ([-1], 400),
                ([True], 400),
                (['9' * 5000], 400),
                ([31, 31], 400),
                ([1] * 51, 413),
            ]
            for ids, expected_status in cases:
                with self.subTest(ids=ids[:3], expected_status=expected_status):
                    response = self._post(ids)
                    self.assertEqual(response.status_code, expected_status)
                    self.assertEqual(response.mimetype, 'application/json')
                    self.assertIn('error', response.json)
            get_records.assert_not_called()

    def test_rejects_empty_traversal_only_and_overlong_zip_names_before_lookup(self):
        with patch('db.contabilidade.get_cont_invoice_zip_metadata') as get_records:
            for name in ('', '../../', 'x' * 81):
                with self.subTest(name=name[:12]):
                    response = self._post([31], name)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('error', response.json)
            get_records.assert_not_called()

    def test_rejects_invoices_outside_the_accounting_list(self):
        with patch(
            'db.contabilidade.get_cont_invoice_zip_metadata', return_value=[]
        ):
            response = self._post([999])

        self.assertEqual(response.status_code, 400)
        self.assertIn('não pertencem', response.json['error'])

    def test_missing_document_returns_error_without_partial_zip(self):
        with _patch_records([
            _record(31, data=b'%PDF-1.7 present'),
            _record(32, data=None),
        ]) as (_get_metadata, get_data):
            response = self._post([31, 32])

        self.assertEqual(response.status_code, 422)
        self.assertIn('não foi criado ZIP', response.json['error'])
        get_data.assert_not_called()

    def test_source_and_final_zip_size_limits_return_clear_errors(self):
        large_document = b'%PDF-1.7 too large'
        with _patch_records([_record(31, data=large_document)]) as (_meta, data), patch(
            'flask_app.routes.contabilidade.MAX_CONTABILIDADE_ZIP_BYTES', 4
        ):
            response = self._post([31])

        self.assertEqual(response.status_code, 413)
        self.assertIn('limite', response.json['error'])
        data.assert_not_called()

        with _patch_records([_record(31, data=large_document)]), patch(
            'flask_app.routes.contabilidade.MAX_CONTABILIDADE_ZIP_BYTES',
            len(large_document),
        ):
            response = self._post([31])

        self.assertEqual(response.status_code, 413)
        self.assertIn('limite', response.json['error'])

    def test_database_and_zip_creation_failures_return_json_without_archive(self):
        with patch(
            'db.contabilidade.get_cont_invoice_zip_metadata',
            side_effect=RuntimeError('database unavailable'),
        ):
            response = self._post([31])
        self.assertEqual(response.status_code, 500)
        self.assertIn('verificar', response.json['error'])

        with _patch_records([_record(31)]), patch(
            'flask_app.routes.contabilidade.zipfile.ZipFile',
            side_effect=OSError('disk or compression failure'),
        ):
            response = self._post([31])
        self.assertEqual(response.status_code, 500)
        self.assertIn('criar o ZIP', response.json['error'])

    def test_accounting_permission_is_required(self):
        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 8,
                'username': 'compras-tester',
                'acesso_compras': True,
                'acesso_contabilidade': False,
            }
        with patch('db.contabilidade.get_cont_invoice_zip_metadata') as get_records:
            response = self._post([31])

        self.assertEqual(response.status_code, 302)
        get_records.assert_not_called()

    def test_zip_action_uses_current_selection_without_changing_bulk_status_action(self):
        with open('flask_app/templates/contabilidade/index.html', encoding='utf-8') as template:
            source = template.read()

        self.assertIn('id="bulk-download-zip-btn"', source)
        self.assertIn('data-bs-target="#contab-zip-modal"', source)
        self.assertIn('id="contab-zip-form" onsubmit="downloadSelectedZip(event)"', source)
        self.assertIn("function submitBulk()", source)
        self.assertIn("url_for(\"contabilidade.atualizar_estado\")", source)
        zip_function = source.split('function downloadSelectedZip(event) {', 1)[1].split(
            '\n}', 1
        )[0]
        self.assertIn('getSelectedIds()', zip_function)
        self.assertNotIn('clearSelection()', zip_function)


if __name__ == '__main__':
    unittest.main()