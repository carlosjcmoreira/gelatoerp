import unittest
from unittest.mock import MagicMock, patch


def _connection(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = lambda instance: instance
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _sql_calls(cursor):
    return [call.args[0] for call in cursor.execute.call_args_list]


class TestConfirmArticleSupplier(unittest.TestCase):
    def test_confirmation_uses_supplier_id_and_preserves_original_label(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (None, None),
            ('Fornecedor Legal, Lda.',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import confirm_artigo_fornecedor
            result = confirm_artigo_fornecedor(18, 42, actor='ana')

        self.assertEqual(
            result,
            {
                'found': True, 'changed': True,
                'supplier_name': 'Fornecedor Legal, Lda.',
            },
        )
        calls = cursor.execute.call_args_list
        self.assertIn('FOR UPDATE OF a', calls[0].args[0])
        article_update = next(
            call for call in calls
            if 'UPDATE artigos_administrativos' in call.args[0]
        )
        self.assertEqual(article_update.args[1], (42, 18))
        audit_insert = next(
            call for call in calls
            if 'INSERT INTO artigos_administrativos_fornecedor_audit' in call.args[0]
        )
        self.assertEqual(
            audit_insert.args[1],
            (18, None, None, 42, 'Fornecedor Legal, Lda.', 'ana',
             'confirmação do fornecedor registado no catálogo'),
        )
        self.assertFalse(any('compras_origens' in sql for sql in _sql_calls(cursor)))
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_unknown_supplier_is_rejected_without_writes(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (None, None),
            None,
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import confirm_artigo_fornecedor
            with self.assertRaisesRegex(ValueError, 'não existe'):
                confirm_artigo_fornecedor(18, 999, actor='ana')

        self.assertFalse(any(
            'INSERT INTO' in sql or 'UPDATE artigos_administrativos' in sql
            for sql in _sql_calls(cursor)
        ))
        conn.commit.assert_not_called()
        invalidate.assert_not_called()

    def test_same_supplier_confirmation_is_idempotent(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (42, 'Fornecedor Legal, Lda.'),
            ('Fornecedor Legal, Lda.',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import confirm_artigo_fornecedor
            result = confirm_artigo_fornecedor(18, 42, actor='ana')

        self.assertEqual(
            result,
            {
                'found': True, 'changed': False,
                'supplier_name': 'Fornecedor Legal, Lda.',
            },
        )
        sql_calls = _sql_calls(cursor)
        self.assertFalse(any(
            'INSERT INTO compras_origens' in sql
            or 'INSERT INTO artigos_administrativos_origem_audit' in sql
            or 'UPDATE artigos_administrativos' in sql
            for sql in sql_calls
        ))
        conn.commit.assert_not_called()
        invalidate.assert_not_called()

    def test_supplier_link_does_not_reactivate_or_create_legacy_origin(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (None, None),
            ('Fornecedor Legal, Lda.',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import confirm_artigo_fornecedor
            result = confirm_artigo_fornecedor(18, 42, actor='ana')

        self.assertTrue(result['changed'])
        self.assertFalse(any('compras_origens' in sql for sql in _sql_calls(cursor)))
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_supplier_link_can_differ_from_historical_origin(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (None, None),
            ('Fornecedor Legal, Lda.',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix'):
            from db.artigos import confirm_artigo_fornecedor
            result = confirm_artigo_fornecedor(18, 42, actor='ana')

        self.assertTrue(result['changed'])
        self.assertFalse(any('compras_origens' in sql for sql in _sql_calls(cursor)))
        conn.commit.assert_called_once()


class TestOfficialArticleSupplier(unittest.TestCase):
    def test_assigning_supplier_preserves_non_supplier_origin_and_audits(self):
        for origin_type in ('centro_interno', 'categoria_operacional'):
            with self.subTest(origin_type=origin_type):
                cursor = MagicMock()
                cursor.fetchone.side_effect = [
                    (None, None, origin_type, None),
                    ('Fornecedor Legal, Lda.',),
                ]
                conn = _connection(cursor)

                with patch('db.artigos.db_connection', return_value=conn), \
                        patch('db.artigos.invalidate_prefix') as invalidate:
                    from db.artigos import set_artigo_fornecedor_oficial
                    result = set_artigo_fornecedor_oficial(18, 42, actor='ana')

                self.assertEqual(
                    result,
                    {
                        'found': True,
                        'changed': True,
                        'supplier_name': 'Fornecedor Legal, Lda.',
                    },
                )
                article_update = next(
                    call for call in cursor.execute.call_args_list
                    if 'UPDATE artigos_administrativos' in call.args[0]
                )
                self.assertEqual(article_update.args[1], (42, 18))
                audit_insert = next(
                    call for call in cursor.execute.call_args_list
                    if 'INSERT INTO artigos_administrativos_fornecedor_audit'
                    in call.args[0]
                )
                self.assertEqual(
                    audit_insert.args[1],
                    (18, None, None, 42, 'Fornecedor Legal, Lda.', 'ana',
                     'edição do fornecedor oficial no catálogo'),
                )
                conn.commit.assert_called_once()
                invalidate.assert_called_once_with('artigos_administrativos')

    def test_repeating_same_supplier_is_a_noop_without_duplicate_audit(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (42, 'Fornecedor Legal, Lda.', 'centro_interno', None),
            ('Fornecedor Legal, Lda.',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import set_artigo_fornecedor_oficial
            result = set_artigo_fornecedor_oficial(18, 42, actor='ana')

        self.assertEqual(
            result,
            {
                'found': True,
                'changed': False,
                'supplier_name': 'Fornecedor Legal, Lda.',
            },
        )
        self.assertFalse(any(
            'UPDATE artigos_administrativos' in sql
            or 'INSERT INTO artigos_administrativos_fornecedor_audit' in sql
            for sql in _sql_calls(cursor)
        ))
        conn.commit.assert_not_called()
        invalidate.assert_not_called()

    def test_removing_supplier_records_an_audited_change(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            42, 'Fornecedor Legal, Lda.', 'categoria_operacional', None,
        )
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import set_artigo_fornecedor_oficial
            result = set_artigo_fornecedor_oficial(18, None, actor='ana')

        self.assertEqual(
            result,
            {'found': True, 'changed': True, 'supplier_name': None},
        )
        article_update = next(
            call for call in cursor.execute.call_args_list
            if 'UPDATE artigos_administrativos' in call.args[0]
        )
        self.assertEqual(article_update.args[1], (None, 18))
        audit_insert = next(
            call for call in cursor.execute.call_args_list
            if 'INSERT INTO artigos_administrativos_fornecedor_audit'
            in call.args[0]
        )
        self.assertEqual(
            audit_insert.args[1],
            (18, 42, 'Fornecedor Legal, Lda.', None, None, 'ana',
             'edição do fornecedor oficial no catálogo'),
        )
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_direct_supplier_can_differ_from_external_legacy_origin(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (42, 'Fornecedor histórico'),
            ('Fornecedor canónico novo',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import set_artigo_fornecedor_oficial
            result = set_artigo_fornecedor_oficial(18, 73, actor='ana')

        self.assertTrue(result['changed'])
        self.assertEqual(result['supplier_name'], 'Fornecedor canónico novo')
        update = next(
            call for call in cursor.execute.call_args_list
            if 'UPDATE artigos_administrativos' in call.args[0]
        )
        self.assertEqual(update.args[1], (73, 18))
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')


class TestArticleCatalogueNoopUpdates(unittest.TestCase):
    def test_unchanged_article_does_not_update_timestamps_or_audit(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ('CAFÉ ILLY', 'Café Clássico', None, None, 18,
             'CAFÉ ILLY', 'por_resolver', None, None, None),
            (18, 'por_resolver', None),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import update_artigo_administrativo
            result = update_artigo_administrativo(
                18, 'CAFÉ ILLY', 'Café Clássico',
                marca=None, unidade=None, origem_id=None, actor='ana',
            )

        self.assertEqual(
            result,
            {'found': True, 'changed': False, 'origin_type': 'por_resolver'},
        )
        self.assertFalse(any(
            'UPDATE artigos_administrativos' in sql
            or 'INSERT INTO artigos_administrativos_origem_audit' in sql
            for sql in _sql_calls(cursor)
        ))
        conn.commit.assert_not_called()
        invalidate.assert_not_called()

    def test_editing_product_keeps_unresolved_status_in_result(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ('CAFÉ ILLY', 'Café Clássico', None, None, 18,
             'CAFÉ ILLY', 'por_resolver', None, None, None),
            (18, 'por_resolver', None),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import update_artigo_administrativo
            result = update_artigo_administrativo(
                18, 'CAFÉ ILLY', 'Café Clássico Descafeinado',
                marca=None, unidade=None, origem_id=None, actor='ana',
            )

        self.assertEqual(
            result,
            {'found': True, 'changed': True, 'origin_type': 'por_resolver'},
        )
        self.assertTrue(any(
            'UPDATE artigos_administrativos' in sql
            for sql in _sql_calls(cursor)
        ))
        article_update = next(
            call for call in cursor.execute.call_args_list
            if 'UPDATE artigos_administrativos' in call.args[0]
        )
        self.assertEqual(
            article_update.args[1],
            ('CAFÉ ILLY', 'Café Clássico Descafeinado', None, None,
             'Por classificar', True, 'CAFÉ ILLY', 'Café Clássico Descafeinado',
             None, None, 18),
        )
        self.assertFalse(any(
            'INSERT INTO artigos_administrativos_origem_audit' in sql
            for sql in _sql_calls(cursor)
        ))
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_catalogue_update_rejects_legacy_origin_changes(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ('CAFÉ ILLY', 'Café Clássico', None, None, 18,
             'CAFÉ ILLY', 'por_resolver', None, None, None),
            (91, 'fornecedor_externo', 42),
            ('Fornecedor Legal, Lda.',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import update_artigo_administrativo
            with self.assertRaisesRegex(ValueError, 'origem histórica'):
                update_artigo_administrativo(
                    18, 'CAFÉ ILLY', 'Café Clássico',
                    marca=None, unidade=None, origem_id=91, actor='ana',
                )

        self.assertFalse(any(
            'UPDATE artigos_administrativos' in sql or 'INSERT INTO' in sql
            for sql in _sql_calls(cursor)
        ))
        conn.commit.assert_not_called()
        invalidate.assert_not_called()


class TestLegacyOriginReview(unittest.TestCase):
    def test_confirming_supplier_preserves_origin_and_audits_both_decisions(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (
                'por_rever', 71, 'MATOSINHOS', 'Matosinhos',
                55, 'Fornecedor anterior', 'centro_interno',
            ),
            ('Fornecedor confirmado, Lda.',),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import review_artigo_origem
            result = review_artigo_origem(18, 42, actor='ana')

        self.assertEqual(result, {
            'found': True, 'changed': True, 'estado': 'revisto',
            'supplier_id': 42, 'supplier_name': 'Fornecedor confirmado, Lda.',
        })
        calls = cursor.execute.call_args_list
        self.assertIn('FOR UPDATE OF a', calls[0].args[0])
        article_update = next(
            call for call in calls if 'UPDATE artigos_administrativos' in call.args[0]
        )
        self.assertEqual(article_update.args[1], ('revisto', 42, 18))
        self.assertNotIn('origem_id =', article_update.args[0])
        self.assertNotIn('origem_original =', article_update.args[0])
        supplier_audit = next(
            call for call in calls
            if 'INSERT INTO artigos_administrativos_fornecedor_audit'
            in call.args[0]
        )
        self.assertEqual(supplier_audit.args[1][1:5], (
            55, 'Fornecedor anterior', 42, 'Fornecedor confirmado, Lda.',
        ))
        review_audit = next(
            call for call in calls
            if 'INSERT INTO artigos_administrativos_origem_revisao_audit'
            in call.args[0]
        )
        self.assertEqual(review_audit.args[1][1:6], (
            'por_rever', 'revisto', 71, 'centro_interno', 'MATOSINHOS',
        ))
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_keep_pending_preserves_existing_supplier_and_records_decision(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            'revisto', 81, 'MOEDAS', 'Moedas',
            55, 'Fornecedor já ligado', 'categoria_operacional',
        )
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import review_artigo_origem
            result = review_artigo_origem(19, None, actor='ana')

        self.assertEqual(result, {
            'found': True, 'changed': True, 'estado': 'por_rever',
            'supplier_id': 55, 'supplier_name': 'Fornecedor já ligado',
        })
        article_update = next(
            call for call in cursor.execute.call_args_list
            if 'UPDATE artigos_administrativos' in call.args[0]
        )
        self.assertEqual(article_update.args[1], ('por_rever', 55, 19))
        self.assertFalse(any(
            'INSERT INTO artigos_administrativos_fornecedor_audit' in call.args[0]
            for call in cursor.execute.call_args_list
        ))
        review_audit = next(
            call for call in cursor.execute.call_args_list
            if 'INSERT INTO artigos_administrativos_origem_revisao_audit'
            in call.args[0]
        )
        self.assertEqual(review_audit.args[1][6:10], (
            55, 'Fornecedor já ligado', 55, 'Fornecedor já ligado',
        ))
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_review_rejects_non_legacy_origin_without_writes(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            'por_rever', 91, 'Fornecedor', 'Fornecedor',
            42, 'Fornecedor Legal', 'fornecedor_externo',
        )
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import review_artigo_origem
            with self.assertRaisesRegex(ValueError, 'origem herdada'):
                review_artigo_origem(20, 42, actor='ana')

        self.assertEqual(cursor.execute.call_count, 1)
        conn.commit.assert_not_called()
        invalidate.assert_not_called()


if __name__ == '__main__':
    unittest.main()