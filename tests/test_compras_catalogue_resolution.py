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
            (18, 'CAFÉ ILLY', 'CAFÉ ILLY', 'por_resolver', None),
            (42, 'Fornecedor Legal, Lda.'),
            None,
            (91,),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import confirm_artigo_fornecedor
            result = confirm_artigo_fornecedor(18, 42, actor='ana')

        self.assertEqual(
            result,
            {'changed': True, 'supplier_name': 'Fornecedor Legal, Lda.'},
        )
        calls = cursor.execute.call_args_list
        self.assertIn('FOR UPDATE OF a', calls[0].args[0])
        origin_insert = next(
            call for call in calls
            if 'INSERT INTO compras_origens' in call.args[0]
        )
        self.assertEqual(
            origin_insert.args[1],
            ('fornecedor:42', 'Fornecedor Legal, Lda.',
             'Fornecedor Legal, Lda.', 42),
        )
        article_update = next(
            call for call in calls
            if 'UPDATE artigos_administrativos' in call.args[0]
        )
        self.assertEqual(article_update.args[1], (91, 18))
        audit_insert = next(
            call for call in calls
            if 'INSERT INTO artigos_administrativos_origem_audit' in call.args[0]
        )
        self.assertEqual(
            audit_insert.args[1],
            (18, 18, 91, 'CAFÉ ILLY', 'ana',
             'confirmação de fornecedor no catálogo'),
        )
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_unknown_supplier_is_rejected_without_writes(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (18, 'CAFÉ ILLY', 'CAFÉ ILLY', 'por_resolver', None),
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
            (72, 'CAFÉ ILLY', 'CAFÉ ILLY', 'fornecedor_externo', 42),
            (42, 'Fornecedor Legal, Lda.'),
            (72, True),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import confirm_artigo_fornecedor
            result = confirm_artigo_fornecedor(18, 42, actor='ana')

        self.assertEqual(
            result,
            {'changed': False, 'supplier_name': 'Fornecedor Legal, Lda.'},
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

    def test_inactive_supplier_origin_is_not_silently_reactivated(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (18, 'CAFÉ ILLY', 'CAFÉ ILLY', 'por_resolver', None),
            (42, 'Fornecedor Legal, Lda.'),
            (91, False),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import confirm_artigo_fornecedor
            with self.assertRaisesRegex(ValueError, 'inativa'):
                confirm_artigo_fornecedor(18, 42, actor='ana')

        self.assertFalse(any(
            'INSERT INTO' in sql or 'UPDATE artigos_administrativos' in sql
            for sql in _sql_calls(cursor)
        ))
        conn.commit.assert_not_called()
        invalidate.assert_not_called()

    def test_supplier_confirmation_cannot_replace_another_confirmed_origin(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            72, 'CAFÉ ILLY', 'CAFÉ ILLY', 'centro_interno', None,
        )
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn):
            from db.artigos import confirm_artigo_fornecedor
            with self.assertRaisesRegex(ValueError, 'outra origem confirmada'):
                confirm_artigo_fornecedor(18, 42, actor='ana')

        conn.commit.assert_not_called()
        self.assertEqual(cursor.execute.call_count, 1)


class TestArticleCatalogueNoopUpdates(unittest.TestCase):
    def test_unchanged_article_does_not_update_timestamps_or_audit(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ('CAFÉ ILLY', 'Café Clássico', None, None, 18,
             'CAFÉ ILLY', 'por_resolver'),
            (18, 'por_resolver'),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import update_artigo_administrativo
            result = update_artigo_administrativo(
                18, 'CAFÉ ILLY', 'Café Clássico',
                marca=None, unidade=None, origem_id=18, actor='ana',
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
             'CAFÉ ILLY', 'por_resolver'),
            (18, 'por_resolver'),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix') as invalidate:
            from db.artigos import update_artigo_administrativo
            result = update_artigo_administrativo(
                18, 'CAFÉ ILLY', 'Café Clássico Descafeinado',
                marca=None, unidade=None, origem_id=18, actor='ana',
            )

        self.assertEqual(
            result,
            {'found': True, 'changed': True, 'origin_type': 'por_resolver'},
        )
        self.assertTrue(any(
            'UPDATE artigos_administrativos' in sql
            for sql in _sql_calls(cursor)
        ))
        self.assertFalse(any(
            'INSERT INTO artigos_administrativos_origem_audit' in sql
            for sql in _sql_calls(cursor)
        ))
        conn.commit.assert_called_once()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_existing_origin_selector_still_changes_and_audits_origin(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ('CAFÉ ILLY', 'Café Clássico', None, None, 18,
             'CAFÉ ILLY', 'por_resolver'),
            (91, 'fornecedor_externo'),
        ]
        conn = _connection(cursor)

        with patch('db.artigos.db_connection', return_value=conn), \
                patch('db.artigos.invalidate_prefix'):
            from db.artigos import update_artigo_administrativo
            result = update_artigo_administrativo(
                18, 'CAFÉ ILLY', 'Café Clássico',
                marca=None, unidade=None, origem_id=91, actor='ana',
            )

        self.assertEqual(
            result,
            {'found': True, 'changed': True, 'origin_type': 'fornecedor_externo'},
        )
        audit_insert = next(
            call for call in cursor.execute.call_args_list
            if 'INSERT INTO artigos_administrativos_origem_audit' in call.args[0]
        )
        self.assertEqual(
            audit_insert.args[1],
            (18, 18, 91, 'CAFÉ ILLY', 'ana', 'edição do catálogo'),
        )
        conn.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()