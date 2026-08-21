"""Regression tests for supplier duplicate identification and alias-safe backfill."""

import unittest
from unittest.mock import MagicMock, patch


def _connection(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = lambda instance: instance
    conn.__exit__ = MagicMock(return_value=False)
    return conn


class TestDuplicateSupplierSuggestions(unittest.TestCase):
    def test_short_trade_name_is_suggested_for_human_confirmation(self):
        """A short OCR name must be visible, but never silently merged."""
        suppliers = [
            {'id': 60, 'name': 'Grasumos', 'nif': None},
            {
                'id': 120,
                'name': 'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.',
                'nif': 'PT506454223',
            },
        ]
        cursor = MagicMock()
        cursor.fetchall.return_value = []

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_duplicate_supplier_suggestions
            suggestions = get_duplicate_supplier_suggestions(suppliers)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual({suggestions[0]['a']['id'], suggestions[0]['b']['id']}, {60, 120})
        self.assertIn('confirmar', suggestions[0]['reason'].lower())

    def test_unrelated_short_names_are_not_suggested(self):
        suppliers = [
            {'id': 1, 'name': 'Norte', 'nif': None},
            {'id': 2, 'name': 'Norte Verde Serviços', 'nif': None},
        ]
        cursor = MagicMock()
        cursor.fetchall.return_value = []

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_duplicate_supplier_suggestions
            suggestions = get_duplicate_supplier_suggestions(suppliers)

        self.assertEqual(suggestions, [])


class TestSupplierAliasBackfill(unittest.TestCase):
    def test_backfill_uses_confirmed_alias_before_creating_supplier(self):
        """An unlinked alias must be attached to the canonical supplier."""
        cursor = MagicMock()
        cursor.fetchall.return_value = [('Grasumos', '')]
        cursor.fetchone.side_effect = [
            (True,),                    # advisory lock
            ('supplier_aliases',),      # to_regclass
            None,                       # no exact canonical name
            (120, 'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.'),  # alias match
        ]
        cursor.rowcount = 4

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import backfill_supplier_ids
            result = backfill_supplier_ids()

        self.assertEqual(result, {'suppliers_created': 0, 'invoices_linked': 4})
        executed = [call.args[0] for call in cursor.execute.call_args_list]
        alias_query = next(sql for sql in executed if 'FROM supplier_aliases' in sql)
        self.assertIn('LOWER(a.alias_name) = LOWER(%s)', alias_query)
        update_call = cursor.execute.call_args_list[-1]
        self.assertEqual(update_call.args[1][0], 120)
        self.assertEqual(
            update_call.args[1][1],
            'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.',
        )

    def test_backfill_requires_nif_agreement_for_an_alias_with_nif(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [('Grasumos', 'PT506454223')]
        cursor.fetchone.side_effect = [
            (True,),
            ('supplier_aliases',),
            None,
            (120, 'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.'),
        ]
        cursor.rowcount = 1

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import backfill_supplier_ids
            backfill_supplier_ids()

        executed = [call.args[0] for call in cursor.execute.call_args_list]
        alias_query = next(sql for sql in executed if 'FROM supplier_aliases' in sql)
        self.assertIn("regexp_replace(upper(COALESCE(a.alias_nif, '')), '^PT', '') = %s", alias_query)
        update_query = executed[-1]
        self.assertIn("AND COALESCE(supplier_nif, '') = %s", update_query)

    def test_backfill_groups_same_name_by_nif(self):
        """Different NIFs under the same text name must not be linked together."""
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.side_effect = [(True,), ('supplier_aliases',)]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import backfill_supplier_ids
            backfill_supplier_ids()

        unlinked_query = cursor.execute.call_args_list[1].args[0]
        self.assertIn("GROUP BY supplier_name, COALESCE(supplier_nif, '')", unlinked_query)


class TestNifSafeSupplierResolution(unittest.TestCase):
    def test_alias_with_conflicting_nif_is_rejected(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (120, 'Fornecedor Canónico', 'PT506454223', None, None, None, None, None, None),
        ]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_supplier_by_alias
            supplier = get_supplier_by_alias('Fornecedor OCR', 'PT509999999')

        self.assertIsNone(supplier)

    def test_nif_upsert_keeps_existing_canonical_name(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(120,), ('Fornecedor Canónico',)]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)), \
                patch('db.faturas.link_invoices_to_supplier_by_name'):
            from db.faturas import upsert_supplier
            supplier_id = upsert_supplier('Nome OCR Curto', 'PT506454223')

        self.assertEqual(supplier_id, 120)
        lookup_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn("regexp_replace(upper(COALESCE(nif, '')), '^PT', '')", lookup_sql)
        update_sql = cursor.execute.call_args_list[2].args[0]
        self.assertNotIn('name =', update_sql.lower())

    def test_digits_only_import_matches_pt_prefixed_supplier(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(120,), ('Fornecedor Canónico',)]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)), \
                patch('db.faturas.link_invoices_to_supplier_by_name'):
            from db.faturas import upsert_supplier
            supplier_id = upsert_supplier('Nome OCR Curto', '506454223')

        self.assertEqual(supplier_id, 120)
        lock_call = cursor.execute.call_args_list[0]
        self.assertIn('pg_advisory_xact_lock', lock_call.args[0])
        self.assertEqual(lock_call.args[1], ('supplier_nif:506454223',))
        lookup_call = cursor.execute.call_args_list[1]
        self.assertEqual(lookup_call.args[1], ('506454223',))


class TestSupplierCommonNames(unittest.TestCase):
    def test_common_name_is_trimmed_and_can_be_cleared(self):
        from db.faturas import normalize_supplier_common_name

        self.assertEqual(normalize_supplier_common_name('  Grasumos   Lisboa  '), 'Grasumos Lisboa')
        self.assertIsNone(normalize_supplier_common_name('   '))

    def test_common_name_rejects_values_over_the_presentation_limit(self):
        from db.faturas import MAX_SUPPLIER_COMMON_NAME_LENGTH, normalize_supplier_common_name

        with self.assertRaises(ValueError):
            normalize_supplier_common_name('a' * (MAX_SUPPLIER_COMMON_NAME_LENGTH + 1))

    def test_editing_common_name_never_rewrites_invoice_history(self):
        cursor = MagicMock()
        cursor.rowcount = 1

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import update_supplier
            update_supplier(20, 'Nome Legal', nif='506454223', common_name='Nome comum')

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(len(statements), 1)
        self.assertIn('common_name = %s', statements[0])
        self.assertNotIn('UPDATE invoices', statements[0])

    def test_linked_invoice_keeps_legal_name_and_exposes_common_name(self):
        row = [
            10, 20, 'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.', '506454223',
            'FT 1', 12.5, None, None, None, None, None, None, None,
            'scheduled', None, 'user', None, None, None, None, None,
            'fatura', None, None, None, None, 0, 0, False, None, None,
            'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.', 'Grasumos',
        ]
        cursor = MagicMock()
        cursor.fetchall.return_value = [tuple(row)]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_invoices
            invoice = get_invoices()[0]

        self.assertEqual(invoice['supplier_name'], 'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.')
        self.assertEqual(invoice['supplier_legal_name'], 'GRASUMOS, COMÉRCIO DE BEBIDAS, LDA.')
        self.assertEqual(invoice['supplier_display_name'], 'Grasumos')

    def test_unlinked_invoice_falls_back_to_its_stored_supplier_name(self):
        row = [
            11, None, 'Fornecedor OCR', None, 'FT 2', 5, None, None, None,
            None, None, None, None, 'scheduled', None, 'user', None, None,
            None, None, None, 'fatura', None, None, None, None, 0, 0, False,
            None, None, None, 'Fornecedor OCR',
        ]
        cursor = MagicMock()
        cursor.fetchall.return_value = [tuple(row)]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_invoices
            invoice = get_invoices()[0]

        self.assertEqual(invoice['supplier_display_name'], 'Fornecedor OCR')
        self.assertEqual(invoice['supplier_name'], 'Fornecedor OCR')

    def test_payment_list_exposes_common_name_without_replacing_legal_name(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{
            'id': 12,
            'supplier_name': 'FORNECEDOR LEGAL, LDA.',
            'supplier_legal_name': 'FORNECEDOR LEGAL, LDA.',
            'supplier_display_name': 'Fornecedor habitual',
        }]

        with patch('db.pagamentos.db_connection', return_value=_connection(cursor)):
            from db.pagamentos import get_invoices_with_payments
            invoice = get_invoices_with_payments(search='Fornecedor habitual')[0]

        self.assertEqual(invoice['supplier_name'], 'FORNECEDOR LEGAL, LDA.')
        self.assertEqual(invoice['supplier_display_name'], 'Fornecedor habitual')
        sql = cursor.execute.call_args.args[0]
        self.assertIn('LEFT JOIN suppliers s ON s.id = i.supplier_id', sql)
        self.assertIn('s.common_name', sql)

    def test_pending_installments_expose_common_name_without_replacing_legal_name(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{
            'invoice_id': 12,
            'supplier_name': 'FORNECEDOR LEGAL, LDA.',
            'supplier_legal_name': 'FORNECEDOR LEGAL, LDA.',
            'supplier_display_name': 'Fornecedor habitual',
        }]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_pending_installments
            installment = get_pending_installments()[0]

        self.assertEqual(installment['supplier_name'], 'FORNECEDOR LEGAL, LDA.')
        self.assertEqual(installment['supplier_display_name'], 'Fornecedor habitual')
        sql = cursor.execute.call_args.args[0]
        self.assertIn('LEFT JOIN suppliers s ON s.id = i.supplier_id', sql)
        self.assertIn('s.common_name', sql)

    def test_confirming_dashboard_exposes_common_name_in_open_and_history_lists(self):
        row = {
            'invoice_id': 12,
            'supplier_name': 'FORNECEDOR LEGAL, LDA.',
            'supplier_legal_name': 'FORNECEDOR LEGAL, LDA.',
            'supplier_display_name': 'Fornecedor habitual',
        }
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[], [row], [row]]

        with patch('db.credito.db_connection', return_value=_connection(cursor)), \
                patch('db.credito.get_confirming_utilizacao_por_contrato', return_value={}):
            from db.credito import get_confirming_dashboard
            dashboard = get_confirming_dashboard()

        self.assertEqual(
            dashboard['parcelas_abertas'][0]['supplier_display_name'],
            'Fornecedor habitual',
        )
        self.assertEqual(
            dashboard['todas_parcelas'][0]['supplier_name'],
            'FORNECEDOR LEGAL, LDA.',
        )
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        for query in queries[1:]:
            self.assertIn('LEFT JOIN suppliers s ON s.id = i.supplier_id', query)
            self.assertIn('supplier_display_name', query)

    def test_confirming_parcel_query_and_templates_use_display_name(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{
            'supplier_name': 'FORNECEDOR LEGAL, LDA.',
            'supplier_display_name': 'Fornecedor habitual',
        }]

        with patch('db.credito.db_connection', return_value=_connection(cursor)):
            from db.credito import get_confirming_parcelas
            parcela = get_confirming_parcelas()[0]

        self.assertEqual(parcela['supplier_display_name'], 'Fornecedor habitual')
        sql = cursor.execute.call_args.args[0]
        self.assertIn('LEFT JOIN suppliers s ON s.id = i.supplier_id', sql)
        with open('flask_app/templates/credito/confirming.html', encoding='utf-8') as template:
            self.assertIn('p.supplier_display_name', template.read())

    def test_payment_and_scheduled_document_templates_use_the_display_name(self):
        for path in (
            'flask_app/templates/pagamentos/index.html',
            'flask_app/templates/pagamentos/faturas.html',
            'flask_app/templates/financeiro/faturas/dashboard.html',
        ):
            with open(path, encoding='utf-8') as template:
                self.assertIn('supplier_display_name', template.read(), path)

        for path in (
            'flask_app/templates/financeiro/faturas/index.html',
            'flask_app/templates/financeiro/faturas/dashboard.html',
        ):
            with open(path, encoding='utf-8') as template:
                self.assertIn('inst.supplier_display_name', template.read(), path)

    def test_common_name_collision_keeps_linked_supplier_groups_separate(self):
        def invoice_row(invoice_id, supplier_id, legal_name, nif, amount):
            return (
                invoice_id, supplier_id, legal_name, nif, f'FT {invoice_id}', amount,
                None, None, None, None, None, None, None, 'scheduled', None,
                'user', None, None, None, None, None, 'fatura', None, None,
                False, legal_name, 'Fornecedor comum',
            )

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            invoice_row(1, 101, 'Fornecedor Legal A, LDA.', '500000001', 10),
            invoice_row(2, 202, 'Fornecedor Legal B, LDA.', '500000002', 25),
        ]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_contas_por_fornecedor
            groups = get_contas_por_fornecedor()

        self.assertEqual(len(groups), 2)
        by_supplier = {group['supplier_id']: group for group in groups}
        self.assertEqual(by_supplier[101]['supplier_name'], 'Fornecedor comum')
        self.assertEqual(by_supplier[101]['supplier_legal_name'], 'Fornecedor Legal A, LDA.')
        self.assertEqual(by_supplier[101]['supplier_nif'], '500000001')
        self.assertEqual(by_supplier[101]['total_faturas'], 10.0)
        self.assertEqual(by_supplier[202]['supplier_legal_name'], 'Fornecedor Legal B, LDA.')
        self.assertEqual(by_supplier[202]['supplier_nif'], '500000002')
        self.assertEqual(by_supplier[202]['total_faturas'], 25.0)

    def test_paid_counts_use_supplier_identity_not_a_common_name(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [('supplier:101', 2), ('supplier:202', 3)]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            from db.faturas import get_paid_counts_by_supplier
            counts = get_paid_counts_by_supplier([101, 202])

        self.assertEqual(counts, {'supplier:101': 2, 'supplier:202': 3})
        sql, params = cursor.execute.call_args.args
        self.assertIn("i.supplier_id = ANY(%s)", sql)
        self.assertEqual(params, [[101, 202]])


if __name__ == '__main__':
    unittest.main()