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
                (21, 'Fornecedor', 'Produto X', 'un', 10),
                (22, 'Fornecedor', 'Produto X', 'cx', 10),
            ],
        ]
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import get_invoice_linha_artigo_suggestions
        result = get_invoice_linha_artigo_suggestions(7)

        self.assertEqual(result[4]['state'], 'ambiguous')
        self.assertEqual([s['id'] for s in result[4]['suggestions']], [21, 22])
        candidate_query = cursor.execute.call_args_list[-1].args[0]
        self.assertIn('a.fornecedor_oficial_id = %s', candidate_query)
        self.assertNotIn('JOIN compras_origens', candidate_query)
        self.assertEqual(result[4]['suggestions'][0]['supplier_id'], 10)


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
    def test_unconfirmed_invoice_supplier_is_rejected(self, db_connection):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import link_invoice_linha_artigo
        with self.assertRaisesRegex(ValueError, 'identidade do fornecedor'):
            link_invoice_linha_artigo(7, 4, 21, actor='ana')

        self.assertFalse(any(
            'UPDATE invoice_linhas SET artigo_id' in call.args[0]
            for call in cursor.execute.call_args_list
        ))

    @patch('db.artigos.db_connection')
    def test_unconfirmed_article_supplier_is_rejected(self, db_connection):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (10, 'Fornecedor', 'PT123456789', 'Fornecedor', 'PT123456789'),
            (None, True, None),
        ]
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import link_invoice_linha_artigo
        with self.assertRaisesRegex(ValueError, 'ainda não tem'):
            link_invoice_linha_artigo(7, 4, 21, actor='ana')

        self.assertFalse(any(
            'UPDATE invoice_linhas SET artigo_id' in call.args[0]
            for call in cursor.execute.call_args_list
        ))

    @patch('db.artigos.db_connection')
    def test_direct_supplier_mismatch_is_rejected(self, db_connection):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (10, 'Fornecedor', 'PT123456789', 'Fornecedor', 'PT123456789'),
            (None, True, 11),
        ]
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import link_invoice_linha_artigo
        with self.assertRaisesRegex(ValueError, 'outro fornecedor'):
            link_invoice_linha_artigo(7, 4, 21, actor='ana')

        self.assertFalse(any(
            'UPDATE invoice_linhas SET artigo_id' in call.args[0]
            for call in cursor.execute.call_args_list
        ))

    @patch('db.artigos.db_connection')
    def test_matching_direct_supplier_links_without_rewriting_line_snapshot(self, db_connection):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (10, 'Fornecedor', 'PT123456789', 'Fornecedor', 'PT123456789'),
            (None, True, 10),
        ]
        db_connection.return_value = _connection(cursor)[0]

        from db.artigos import link_invoice_linha_artigo
        self.assertTrue(link_invoice_linha_artigo(7, 4, 21, actor='ana'))

        update = next(
            call for call in cursor.execute.call_args_list
            if 'UPDATE invoice_linhas SET artigo_id' in call.args[0]
        )
        self.assertEqual(update.args[1], (21, 4, 7))
        self.assertNotIn('descricao =', update.args[0])


if __name__ == '__main__':
    unittest.main()