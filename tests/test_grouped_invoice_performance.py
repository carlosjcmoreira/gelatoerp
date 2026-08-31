"""Regression tests for bounded SQL-backed grouped invoice views."""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch


def _connection(cursor):
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False
    return connection


def _detail_row(invoice_id, group_key, amount=10, status='scheduled'):
    return (
        invoice_id, 101, 'Fornecedor legal', '500000001', f'FT {invoice_id}',
        amount, None, date(2026, 8, 1), date(2026, 8, 31), None,
        None, None, None, status, None, 'user', None, None, None, None,
        None, 'fatura', 7, 9, False, 'Fornecedor legal', 'Fornecedor comum',
        group_key,
    )


class TestGroupedInvoiceQueries(unittest.TestCase):
    def test_supplier_summary_only_mode_skips_detail_query(self):
        from db.faturas import get_contas_por_fornecedor

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                'supplier:101', 101, None, '500000001',
                'Fornecedor legal', 'Fornecedor comum',
                800, 120000.0, 2500.0, 1,
            ),
        ]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            groups = get_contas_por_fornecedor(
                status_filters=['pending_review', 'scheduled'],
                detail_limit=0,
            )

        self.assertEqual(cursor.execute.call_count, 1)
        self.assertEqual(groups[0]['invoices'], [])
        self.assertTrue(groups[0]['detail_has_more'])

    def test_cost_group_summary_is_aggregated_in_sql(self):
        from db.faturas import get_invoice_group_summaries

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ('7', 'LOJA — Bolhão', 1234, 98765.43, 2),
            ('sem_centro', 'Sem centro de custo', 5, 120.0, 2),
        ]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            groups = get_invoice_group_summaries(
                'centro_custo',
                status_filters=['pending_review', 'scheduled'],
                exclude_gov=True,
                date_from=date(2026, 1, 1),
                date_to=date(2026, 8, 31),
                date_field='due_date',
            )

        sql, params = cursor.execute.call_args.args
        self.assertIn('COUNT(*) AS invoice_count', sql)
        self.assertIn('SUM(i.amount_eur)', sql)
        self.assertNotIn('SELECT i.id', sql)
        self.assertEqual(groups[0]['count'], 1234)
        self.assertEqual(groups[0]['total'], 98765.43)
        self.assertEqual(groups[0]['invoices'], [])
        self.assertIn(['pending_review', 'scheduled'], params)
        self.assertIn(date(2026, 1, 1), params)
        self.assertIn(date(2026, 8, 31), params)

    def test_group_details_use_a_bounded_window(self):
        from db.faturas import get_invoice_group_details

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            _detail_row(1, '7'),
            _detail_row(2, '7'),
            _detail_row(3, 'sem_centro'),
        ]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)), \
             patch('db.faturas.get_invoice_status_labels_map', return_value={}):
            grouped = get_invoice_group_details(
                'centro_custo',
                status_filter='overdue',
                exclude_gov=True,
                detail_limit=2,
            )

        sql, params = cursor.execute.call_args_list[0].args
        self.assertIn('ROW_NUMBER() OVER', sql)
        self.assertIn('WHERE detail_rank <= %s', sql)
        self.assertEqual(params[-1], 2)
        self.assertEqual([invoice['id'] for invoice in grouped['7']], [1, 2])
        self.assertEqual([invoice['id'] for invoice in grouped['sem_centro']], [3])

    def test_supplier_totals_do_not_depend_on_preview_size(self):
        from db.faturas import get_contas_por_fornecedor

        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [
                (
                    'supplier:101', 101, None, '500000001',
                    'Fornecedor legal', 'Fornecedor comum',
                    800, 120000.0, 2500.0, 1,
                ),
            ],
            [
                _detail_row(1, 'supplier:101', amount=20),
                _detail_row(2, 'supplier:101', amount=30),
            ],
        ]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)), \
             patch('db.faturas.get_invoice_status_labels_map', return_value={}):
            groups = get_contas_por_fornecedor(
                status_filters=['pending_review', 'scheduled'],
                exclude_gov=True,
                detail_limit=2,
            )

        group = groups[0]
        self.assertEqual(group['n_docs'], 800)
        self.assertEqual(group['total_faturas'], 120000.0)
        self.assertEqual(group['total_nc'], 2500.0)
        self.assertEqual(group['saldo_liquido'], -117500.0)
        self.assertEqual(len(group['invoices']), 2)
        self.assertTrue(group['detail_has_more'])
        detail_sql, detail_params = cursor.execute.call_args_list[1].args
        self.assertIn('ROW_NUMBER() OVER', detail_sql)
        self.assertEqual(detail_params[-1], 2)

    def test_primary_cost_centre_drilldown_matches_grouping_semantics(self):
        from db.faturas import _build_invoice_where

        where, params = _build_invoice_where(
            primary_centro_custo_id=7,
            no_status_filter=True,
            exclude_drafts=True,
        )

        self.assertIn("i.status != 'draft'", where)
        self.assertIn('i.centro_custo_id = %s', where)
        self.assertNotIn('invoice_centros_custo', where)
        self.assertEqual(params, [7])

    def test_overdue_drilldown_uses_virtual_status_predicate(self):
        from db.faturas import _build_invoice_where

        where, params = _build_invoice_where(statuses=['overdue'])

        self.assertIn("i.status IN ('pending_review', 'scheduled')", where)
        self.assertIn('i.due_date < CURRENT_DATE', where)
        self.assertNotIn('i.status = ANY', where)
        self.assertEqual(params, [])


if __name__ == '__main__':
    unittest.main()