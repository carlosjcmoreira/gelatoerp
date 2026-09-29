"""Invoice detail panel access and accounting-activity auditing."""

import unittest
from datetime import date, datetime
from unittest.mock import patch

from jinja2 import FileSystemLoader

from tests.test_contabilidade_centros_custo import _make_app


class _Cursor:
    def __init__(self, one=None, rows=()):
        self.one = one
        self.rows = list(rows)
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def _invoice(**overrides):
    invoice = {
        'id': 31,
        'supplier_id': 4,
        'supplier_name': 'Fornecedor Exemplo',
        'supplier_display_name': 'Fornecedor Exemplo',
        'supplier_nif': 'PT501234567',
        'invoice_number': 'FT-31',
        'document_type': 'fatura',
        'status': 'pending_review',
        'status_label': 'A rever',
        'amount_eur': 123.45,
        'vat_amount_eur': 23.08,
        'issue_date': date(2026, 5, 1),
        'due_date': date(2026, 6, 1),
        'paid_date': None,
        'category': 'Matéria-prima',
        'notes': 'Nota geral',
        'pdf_filename': 'fatura.pdf',
        'has_pdf': True,
        'accounting_status': 'contabilizado',
        'accounting_notes': 'Conferida',
        'accounting_updated_by': 'contabilidade',
        'accounting_updated_at': datetime(2026, 5, 2, 10, 30),
    }
    invoice.update(overrides)
    return invoice


class TestAccountingActivityWrites(unittest.TestCase):
    def test_single_update_audits_only_changed_status_and_note(self):
        from db import contabilidade

        cursor = _Cursor(one=('por_contabilizar', 'Rascunho'))
        connection = _Connection(cursor)
        with patch.object(contabilidade, 'db_connection', return_value=connection):
            result = contabilidade.update_accounting_status(
                31, 'contabilizado', 'ana', notes='Conferida'
            )

        self.assertTrue(result)
        audit = [
            params for sql, params in cursor.calls
            if 'INSERT INTO invoice_audit_log' in sql
        ]
        self.assertEqual(
            audit,
            [
                (31, 'accounting_status', 'por_contabilizar', 'contabilizado', 'ana'),
                (31, 'accounting_notes', 'Rascunho', 'Conferida', 'ana'),
            ],
        )
        self.assertEqual(connection.commits, 1)

    def test_unchanged_status_without_notes_does_not_create_audit_event(self):
        from db import contabilidade

        cursor = _Cursor(one=('contabilizado', 'Conferida'))
        connection = _Connection(cursor)
        with patch.object(contabilidade, 'db_connection', return_value=connection):
            result = contabilidade.update_accounting_status(
                31, 'contabilizado', 'ana'
            )

        self.assertTrue(result)
        self.assertFalse(any('UPDATE invoices' in sql for sql, _ in cursor.calls))
        self.assertFalse(any('INSERT INTO invoice_audit_log' in sql for sql, _ in cursor.calls))

    def test_bulk_update_audits_only_invoices_whose_status_changed(self):
        from db import contabilidade

        cursor = _Cursor(rows=[
            (31, 'por_contabilizar'),
            (32, 'contabilizado'),
        ])
        connection = _Connection(cursor)
        with patch.object(contabilidade, 'db_connection', return_value=connection):
            updated = contabilidade.bulk_update_accounting_status(
                [31, 32], 'contabilizado', 'ana'
            )

        self.assertEqual(updated, 1)
        audit = [
            params for sql, params in cursor.calls
            if 'INSERT INTO invoice_audit_log' in sql
        ]
        self.assertEqual(
            audit,
            [(31, 'accounting_status', 'por_contabilizar', 'contabilizado', 'ana')],
        )
        self.assertEqual(connection.commits, 1)


class TestContabilidadeInvoiceDetailPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()
        cls.app.config['TESTING'] = True
        cls.app.jinja_loader = FileSystemLoader('flask_app/templates')

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 7,
                'username': 'contabilidade-tester',
                'acesso_contabilidade': True,
            }

    def test_panel_shows_data_document_and_recorded_activity_without_finance_actions(self):
        from db import faturas

        audit_log = [{
            'id': 1,
            'campo_alterado': 'accounting_status',
            'valor_anterior': 'por_contabilizar',
            'valor_novo': 'contabilizado',
            'alterado_por': 'ana',
            'alterado_em': datetime(2026, 5, 2, 10, 30),
        }]
        with patch.object(faturas, 'get_invoice', return_value=_invoice()), \
                patch.object(faturas, 'get_invoice_audit_log', return_value=audit_log), \
                patch.object(faturas, 'get_invoice_status_labels_map', return_value={
                    'pending_review': 'A rever',
                    'scheduled': 'Agendado',
                    'paid': 'Pago',
                }):
            response = self.client.get('/contabilidade/fatura/31/detalhe')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Fornecedor Exemplo'.encode(), response.data)
        self.assertIn('FT-31'.encode(), response.data)
        self.assertIn('iframe'.encode(), response.data)
        self.assertIn('contabilidade/fatura/31/pdf'.encode(), response.data)
        self.assertIn('Atividade registada'.encode(), response.data)
        self.assertIn('Por Contabilizar'.encode(), response.data)
        self.assertIn('Contabilizado'.encode(), response.data)
        self.assertIn('ana'.encode(), response.data)
        for forbidden_action in (
            'Marcar como paga',
            'Guardar alterações',
            'Registar stock',
            'Criar ticket',
            'Editar fatura',
        ):
            self.assertNotIn(forbidden_action.encode(), response.data)

    def test_legacy_accounting_metadata_is_not_fabricated_as_an_audit_event(self):
        from db import faturas

        with patch.object(faturas, 'get_invoice', return_value=_invoice()), \
                patch.object(faturas, 'get_invoice_audit_log', return_value=[]), \
                patch.object(faturas, 'get_invoice_status_labels_map', return_value={}):
            response = self.client.get('/contabilidade/fatura/31/detalhe')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Última atualização do registo contabilístico'.encode(), response.data)
        self.assertIn('Ainda não existe histórico de atividade registado'.encode(), response.data)
        self.assertNotIn('Estado contabilístico:'.encode(), response.data)

    def test_nonexistent_invoice_returns_404_without_reading_activity(self):
        from db import faturas

        with patch.object(faturas, 'get_invoice', return_value=None), \
                patch.object(faturas, 'get_invoice_audit_log') as get_audit:
            response = self.client.get('/contabilidade/fatura/99999/detalhe')

        self.assertEqual(response.status_code, 404)
        get_audit.assert_not_called()

    def test_other_module_permission_does_not_grant_panel_access(self):
        from db import faturas

        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 8,
                'username': 'compras-tester',
                'acesso_compras': True,
                'acesso_contabilidade': False,
            }
        with patch.object(faturas, 'get_invoice') as get_invoice:
            response = self.client.get('/contabilidade/fatura/31/detalhe')

        self.assertEqual(response.status_code, 302)
        get_invoice.assert_not_called()

    def test_row_opening_ignores_interactive_controls_and_keeps_the_list_in_place(self):
        with open('flask_app/templates/contabilidade/index.html', encoding='utf-8') as template:
            source = template.read()

        self.assertIn('onclick="handleContInvoiceRowClick(event, this)"', source)
        self.assertIn('data-invoice-detail-url=', source)
        self.assertIn(
            "event.target.closest('a,button,input,select,textarea,label,form,[data-no-invoice-panel]')",
            source,
        )
        panel_function = source.split('function openContInvoicePanel(row) {', 1)[1].split(
            '\n}', 1
        )[0]
        self.assertIn('fetch(row.dataset.invoiceDetailUrl', panel_function)
        self.assertNotIn('window.location', panel_function)
        self.assertIn("event.stopPropagation(); openContInvoicePanel(this.closest('tr'))", source)


if __name__ == '__main__':
    unittest.main()