"""Contextual ticket creation from the Contabilidade invoice list."""

import unittest
from datetime import date
from unittest.mock import patch

from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

from tests.test_contabilidade_centros_custo import _make_app


def _invoice(invoice_id, number, supplier):
    return {
        'id': invoice_id,
        'issue_date': date(2026, 5, 1),
        'supplier_legal_name': supplier,
        'supplier_display_name': supplier,
        'supplier_name': supplier,
        'supplier_nif': 'PT501234567',
        'centro_custo_name': 'Matosinhos',
        'invoice_number': number,
        'document_type': 'fatura',
        'amount_eur': 100.0,
        'vat_amount_eur': 19.0,
        'status': 'pending_review',
        'accounting_status': 'por_contabilizar',
        'accounting_notes': None,
        'has_pdf': False,
    }


class TestInvoiceRowTicketCreation(unittest.TestCase):
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

    def test_each_row_has_a_ticket_button_bound_to_its_invoice_and_return_context(self):
        invoices = [
            _invoice(31, 'FT-31', 'Fornecedor A'),
            _invoice(32, 'FT-32', 'Fornecedor B'),
        ]
        with patch('db.contabilidade.count_cont_invoices', return_value=120), \
                patch('db.contabilidade.get_cont_invoices', return_value=invoices), \
                patch('db.contabilidade.get_cont_summary', return_value={
                    'por_contabilizar_count': 0,
                    'por_contabilizar_eur': 0,
                    'contabilizado_mes': 0,
                    'tickets_abertos': 0,
                }), \
                patch('db.faturas.get_stores_list', return_value=[]), \
                patch('db.faturas.get_distinct_supplier_names', return_value=[]), \
                patch('db.centros_custo.get_cost_centers', return_value=[]):
            response = self.client.get(
                '/contabilidade/?q=farinha&centro_custo_id=17&page=2'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'data-bs-target="#contab-ticket-modal"'), 2)
        self.assertIn(b'data-invoice-id="31"', response.data)
        self.assertIn(b'data-invoice-label="FT-31', response.data)
        self.assertIn(b'data-invoice-id="32"', response.data)
        self.assertIn(b'id="contab-ticket-invoice-id"', response.data)
        self.assertIn(b'name="invoice_id_required" value="1"', response.data)
        self.assertIn(b'/contabilidade/?q=farinha&amp;centro_custo_id=17&amp;page=2', response.data)
        self.assertIn(b'event.stopPropagation()', response.data)
        self.assertIn(b'data-bs-target="#contab-ticket-modal"', response.data)
        ticket_button = response.data.split(
            b'data-bs-target="#contab-ticket-modal"', 1
        )[0].rsplit(b'<button', 1)[1].split(b'</button>', 1)[0]
        self.assertNotIn(b'onclick=', ticket_button)
        with open('flask_app/templates/contabilidade/index.html', encoding='utf-8') as template:
            source = template.read()
        self.assertIn(
            "event.target.closest('a,button,input,select,textarea,label,form,[data-no-invoice-panel]')",
            source,
        )
        self.assertIn('class="form-check-input cb-inv" value="{{ inv.id }}"', source)
        self.assertIn('onchange="updateBulkBar()"', source)

    def test_valid_submission_uses_the_selected_invoice_and_returns_to_filtered_page(self):
        return_url = '/contabilidade/?q=farinha&centro_custo_id=17&page=2'
        with patch('db.faturas.get_invoice', return_value={'id': 31}) as get_invoice, \
                patch('db.contabilidade.create_ticket', return_value=77) as create_ticket:
            response = self.client.post('/contabilidade/tickets/criar', data={
                'titulo': 'Confirmar documento',
                'descricao': 'Falta validar a fatura.',
                'prazo': '2026-06-30',
                'invoice_id': '31',
                'invoice_id_required': '1',
                '_return_url': return_url,
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, return_url)
        get_invoice.assert_called_once_with(31)
        self.assertEqual(create_ticket.call_args.kwargs, {
            'titulo': 'Confirmar documento',
            'descricao': 'Falta validar a fatura.',
            'criado_por': 'contabilidade-tester',
            'invoice_id': 31,
            'prazo': date(2026, 6, 30),
        })

    def test_missing_required_invoice_returns_to_list_without_creating_unassigned_ticket(self):
        return_url = '/contabilidade/?q=farinha&page=2'
        with patch('db.contabilidade.create_ticket') as create_ticket:
            response = self.client.post('/contabilidade/tickets/criar', data={
                'titulo': 'Confirmar documento',
                'invoice_id_required': '1',
                '_return_url': return_url,
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, return_url)
        create_ticket.assert_not_called()
        with self.client.session_transaction() as session:
            self.assertTrue(any(
                'Selecione uma fatura' in message
                for _category, message in session.get('_flashes', [])
            ))

    def test_invalid_or_nonexistent_invoice_never_creates_a_ticket(self):
        return_url = '/contabilidade/?page=3'
        with patch('db.faturas.get_invoice', return_value=None) as get_invoice, \
                patch('db.contabilidade.create_ticket') as create_ticket:
            response = self.client.post('/contabilidade/tickets/criar', data={
                'titulo': 'Confirmar documento',
                'invoice_id': '999',
                'invoice_id_required': '1',
                '_return_url': return_url,
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, return_url)
        get_invoice.assert_called_once_with(999)
        create_ticket.assert_not_called()

        with patch('db.faturas.get_invoice') as get_invoice, \
                patch('db.contabilidade.create_ticket') as create_ticket:
            response = self.client.post('/contabilidade/tickets/criar', data={
                'titulo': 'Confirmar documento',
                'invoice_id': '3x',
                'invoice_id_required': '1',
                '_return_url': return_url,
            })
        self.assertEqual(response.location, return_url)
        get_invoice.assert_not_called()
        create_ticket.assert_not_called()

    def test_invalid_title_keeps_return_context_and_prevents_creation(self):
        return_url = '/contabilidade/?q=farinha&page=2'
        with patch('db.contabilidade.create_ticket') as create_ticket:
            response = self.client.post('/contabilidade/tickets/criar', data={
                'titulo': '   ',
                'invoice_id': '31',
                'invoice_id_required': '1',
                '_return_url': return_url,
            })

        self.assertEqual(response.location, return_url)
        create_ticket.assert_not_called()
        with self.client.session_transaction() as session:
            self.assertTrue(any(
                'título do ticket é obrigatório' in message
                for _category, message in session.get('_flashes', [])
            ))

    def test_return_url_cannot_redirect_off_the_contabilidade_area(self):
        with patch('db.faturas.get_invoice', return_value={'id': 31}), \
                patch('db.contabilidade.create_ticket', return_value=77):
            response = self.client.post('/contabilidade/tickets/criar', data={
                'titulo': 'Confirmar documento',
                'invoice_id': '31',
                'invoice_id_required': '1',
                '_return_url': 'https://example.invalid/',
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/contabilidade/')

    def test_user_without_contabilidade_access_cannot_create_by_direct_post(self):
        with self.client.session_transaction() as session:
            session['user'] = {
                'id': 8,
                'username': 'compras-tester',
                'acesso_compras': True,
                'acesso_contabilidade': False,
            }
        with patch('db.faturas.get_invoice') as get_invoice, \
                patch('db.contabilidade.create_ticket') as create_ticket:
            response = self.client.post('/contabilidade/tickets/criar', data={
                'titulo': 'Confirmar documento',
                'invoice_id': '31',
                'invoice_id_required': '1',
            })

        self.assertEqual(response.status_code, 302)
        get_invoice.assert_not_called()
        create_ticket.assert_not_called()


if __name__ == '__main__':
    unittest.main()