"""Virtual Contabilidade folders for uploaded invoice documents."""

import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

from tests.test_contabilidade_centros_custo import _make_app


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


def _folder(center_id, code, name, invoice_id=31):
    return {
        'id': center_id,
        'code': code,
        'name': name,
        'label': f'{code} — {name}' if code else name,
        'count': 1,
        'documents': [{
            'id': invoice_id,
            'invoice_number': 'FT-31',
            'pdf_filename': 'fatura.pdf',
            'issue_date': date(2026, 9, 1),
            'document_type': 'fatura',
            'supplier_display_name': 'Fornecedor',
            'supplier_name': 'Fornecedor',
        }],
    }


class TestContabilidadeDocumentFolderQuery(unittest.TestCase):
    def test_groups_multi_center_and_unassigned_documents_with_unique_global_count(self):
        from db import contabilidade

        shared = (31, 'FT-31', 'fatura.pdf', date(2026, 9, 1), 'fatura',
                  'Fornecedor', 'Fornecedor')
        cursor = _Cursor(rows=[
            shared + (1, 'MAT', 'Matosinhos'),
            shared + (2, 'BOL', 'Bolhão'),
            shared + (1, 'MAT', 'Matosinhos'),  # defensive duplicate row
            (32, 'FT-32', 'sem-centro.pdf', date(2026, 8, 1), 'fatura',
             'Outro fornecedor', 'Outro fornecedor', None, None, None),
            (33, 'FT-33', 'legado.pdf', date(2026, 7, 1), 'fatura',
             'Fornecedor antigo', 'Fornecedor antigo', 3, 'LEG', 'Centro legado'),
        ])
        with patch.object(
            contabilidade, 'db_connection', return_value=_Connection(cursor)
        ):
            result = contabilidade.get_cont_invoice_document_folders()

        self.assertEqual(result['total_documents'], 3)
        self.assertEqual(
            [folder['label'] for folder in result['folders']],
            [
                'BOL — Bolhão',
                'LEG — Centro legado',
                'MAT — Matosinhos',
                'Sem centro de custo',
            ],
        )
        folders = {folder['id']: folder for folder in result['folders']}
        self.assertEqual(folders[1]['count'], 1)
        self.assertEqual(folders[2]['count'], 1)
        self.assertEqual(folders[1]['documents'][0]['id'], 31)
        self.assertEqual(folders[2]['documents'][0]['id'], 31)
        self.assertEqual(folders[None]['documents'][0]['id'], 32)
        self.assertEqual(folders[3]['documents'][0]['id'], 33)
        self.assertEqual(sum(folder['count'] for folder in result['folders']), 4)

        sql, params = cursor.calls[0]
        self.assertIsNone(params)
        self.assertIn("i.status NOT IN ('draft', 'cancelled')", sql)
        self.assertIn('i.pdf_data IS NOT NULL', sql)
        self.assertIn('octet_length(i.pdf_data) > 0', sql)
        self.assertIn(
            'COALESCE(icc.centro_custo_id, i.centro_custo_id)',
            sql,
        )

    def test_empty_result_has_no_empty_cost_center_folders(self):
        from db import contabilidade

        cursor = _Cursor()
        with patch.object(
            contabilidade, 'db_connection', return_value=_Connection(cursor)
        ):
            result = contabilidade.get_cont_invoice_document_folders()

        self.assertEqual(result, {'folders': [], 'total_documents': 0})


class TestContabilidadeDocumentFolderRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()
        cls.app.config['TESTING'] = True
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}'}),
            FileSystemLoader('flask_app/templates'),
        ])

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 7,
                'username': 'contabilidade-tester',
                'acesso_contabilidade': True,
            }

    def test_folder_page_shows_virtual_folders_and_same_document_links(self):
        result = {
            'folders': [
                _folder(1, 'MAT', 'Matosinhos'),
                _folder(2, 'BOL', 'Bolhão'),
            ],
            'total_documents': 1,
        }
        with patch(
            'db.contabilidade.get_cont_invoice_document_folders',
            return_value=result,
        ) as get_folders:
            response = self.client.get('/contabilidade/pastas-centro-custo')

        self.assertEqual(response.status_code, 200)
        get_folders.assert_called_once_with()
        self.assertIn(b'Lista de faturas', response.data)
        self.assertIn(b'Pastas por centro de custo', response.data)
        self.assertIn(b'MAT \xe2\x80\x94 Matosinhos', response.data)
        self.assertIn(b'BOL \xe2\x80\x94 Bolh', response.data)
        self.assertIn(b'1 documento carregado', response.data)
        self.assertIn(b'ficheiros n\xc3\xa3o s\xc3\xa3o movidos nem copiados', response.data)
        self.assertEqual(
            response.data.count(b'/contabilidade/fatura/31/detalhe'),
            2,
        )
        self.assertEqual(
            response.data.count(b'/contabilidade/fatura/31/pdf?dl=1'),
            2,
        )

    def test_existing_invoice_list_route_remains_and_shows_both_navigation_tiles(self):
        with patch('db.contabilidade.count_cont_invoices', return_value=0), \
                patch('db.contabilidade.get_cont_invoices', return_value=[]), \
                patch('db.contabilidade.get_cont_summary', return_value={
                    'por_contabilizar_count': 0,
                    'por_contabilizar_eur': 0,
                    'contabilizado_mes': 0,
                    'tickets_abertos': 0,
                }), \
                patch('db.faturas.get_stores_list', return_value=[]), \
                patch('db.faturas.get_distinct_supplier_names', return_value=[]), \
                patch('db.centros_custo.get_cost_centers', return_value=[]):
            response = self.client.get('/contabilidade/')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Lista de faturas', response.data)
        self.assertIn(b'Pastas por centro de custo', response.data)
        self.assertIn(b'href="/contabilidade/"', response.data)

    def test_folder_route_requires_contabilidade_permission(self):
        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 8,
                'username': 'compras-tester',
                'acesso_compras': True,
                'acesso_contabilidade': False,
            }

        with patch(
            'db.contabilidade.get_cont_invoice_document_folders'
        ) as get_folders:
            response = self.client.get('/contabilidade/pastas-centro-custo')

        self.assertEqual(response.status_code, 302)
        get_folders.assert_not_called()


if __name__ == '__main__':
    unittest.main()