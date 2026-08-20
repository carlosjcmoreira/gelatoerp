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


if __name__ == '__main__':
    unittest.main()