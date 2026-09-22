"""Focused tests for invoice-line to Compras catalogue associations."""

import unittest
from unittest.mock import MagicMock, patch


def _connection(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = conn
    return context, conn


class TestInvoiceProductSuggestions(unittest.TestCase):
    @patch('db.artigos.db_connection')
    def test_supplier_identity_conflict_disables_exact_suggestion(self, db_connection):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            10, 'Nome na fatura', 'PT123456789', 'Nome canónico', 'PT123456789'
        )
        cursor.fetchall.return_value = [
            (4, None, 'Produto X', None, None, None, None, None, None),
        ]
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import get_invoice_linha_artigo_suggestions
        result = get_invoice_linha_artigo_suggestions(7)

        self.assertEqual(result[4]['state'], 'supplier_unconfirmed')
        self.assertEqual(result[4]['suggestions'], [])

    @patch('db.artigos.db_connection')
    def test_two_exact_products_are_marked_ambiguous(self, db_connection):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            10, 'Fornecedor', 'PT123456789', 'Fornecedor', 'PT123456789'
        )
        cursor.fetchall.side_effect = [
            [(4, None, 'Produto X', None, None, None, None, None, None)],
            [
                (21, 'Fornecedor', 'Produto X', 'un', 301, 'Origem A'),
                (22, 'Fornecedor', 'Produto X', 'cx', 302, 'Origem B'),
            ],
        ]
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import get_invoice_linha_artigo_suggestions
        result = get_invoice_linha_artigo_suggestions(7)

        self.assertEqual(result[4]['state'], 'ambiguous')
        self.assertEqual([s['id'] for s in result[4]['suggestions']], [21, 22])


class TestInvoiceProductHistory(unittest.TestCase):
    @patch('db.artigos.db_connection')
    def test_history_returns_original_invoice_description_and_filters_confirmed_states(self, db_connection):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (7, None, 'FT-7', 'Fornecedor', 'Nome histórico', 3.0, 'kg',
             4.25, 'scheduled', 'fatura'),
        ]
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import get_artigo_comercial_history
        result = get_artigo_comercial_history(21)

        self.assertEqual(result['historico'][0]['descricao'], 'Nome histórico')
        self.assertEqual(result['ultimo_custo'], 4.25)
        query = cursor.execute.call_args.args[0]
        self.assertIn("i.status IN ('scheduled', 'paid')", query)
        self.assertIn("i.cfo_confirmed_date IS NOT NULL", query)


class TestInvoiceProductLinkValidation(unittest.TestCase):
    @patch('db.artigos.db_connection')
    def test_external_article_from_another_supplier_is_rejected(self, db_connection):
        cursor = MagicMock()
        cursor.fetchone.return_value = (None, 10, True, 'fornecedor_externo', 11)
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import link_invoice_linha_artigo
        with self.assertRaises(ValueError):
            link_invoice_linha_artigo(7, 4, 21, actor='ana')

        self.assertFalse(any(
            'UPDATE invoice_linhas SET artigo_id' in call.args[0]
            for call in cursor.execute.call_args_list
        ))


if __name__ == '__main__':
    unittest.main()