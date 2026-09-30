"""Focused checks for the versioned Compras catalogue seed and identity rules."""

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from db.artigos import (
    catalog_key_for,
    classify_compras_origin_label,
    infer_artigo_unidade,
    update_artigos_administrativos_bulk,
)
from db.compras_catalog_seed import CATALOG_ROWS, CATALOG_SOURCE, CATALOG_VERSION
from db.compras_article_categories import (
    ARTICLE_CATEGORIES,
    UNCATEGORIZED,
    category_for_product,
)


class TestComprasCatalogSeed(unittest.TestCase):
    def test_seed_contains_the_validated_unique_rows(self):
        self.assertEqual(len(CATALOG_ROWS), 113)
        keys = [catalog_key_for(origin, product) for _, origin, product, _ in CATALOG_ROWS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(origin.strip() and product.strip()
                            for _, origin, product, _ in CATALOG_ROWS))
        self.assertEqual(CATALOG_SOURCE, 'bolhao_lista_compras')
        self.assertEqual(CATALOG_VERSION, 'bolhao-initial-v1')

    def test_catalog_identity_normalises_spacing_and_case(self):
        self.assertEqual(
            catalog_key_for('  Porto Higiene ', 'Produto  X'),
            catalog_key_for('porto higiene', 'produto x'),
        )

    def test_unit_inference_is_conservative(self):
        self.assertEqual(infer_artigo_unidade('Manteiga (kg)'), 'kg')
        self.assertEqual(infer_artigo_unidade('Leite inteiro (l)'), 'l')
        self.assertEqual(infer_artigo_unidade('Fita caixas (rolo)'), 'rolo')
        self.assertEqual(infer_artigo_unidade('Produto sem embalagem explícita'), None)

    def test_seed_labels_do_not_invent_supplier_links(self):
        for _, origin, _, _ in CATALOG_ROWS:
            classification = classify_compras_origin_label(origin)
            if classification['tipo'] == 'por_resolver':
                self.assertNotIn('supplier_id', classification)

    def test_store_categories_are_explicit_and_cover_the_seed(self):
        self.assertTrue(
            all(category_for_product(product) in ARTICLE_CATEGORIES
                for _, _, product, _ in CATALOG_ROWS)
        )
        self.assertEqual(
            category_for_product('Bolacha Maria (pacote com 800g)'),
            'Ingredientes e alimentos',
        )
        self.assertEqual(
            category_for_product('Caneta permanente preta (und)'),
            'Escritório e identificação',
        )
        self.assertEqual(category_for_product('Artigo novo sem classificação'), UNCATEGORIZED)


class TestComprasCatalogBulkEdit(unittest.TestCase):
    @staticmethod
    def _connection_context(connection):
        @contextmanager
        def context():
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
        return context

    @staticmethod
    def _change(article_id):
        return {
            'artigo_id': article_id,
            'fornecedor': f'Etiqueta {article_id}',
            'produto': f'Produto {article_id}',
            'marca': None,
            'unidade': 'un',
            'categoria_artigo': 'Higiene e limpeza',
            'origem_id': None,
        }

    def test_bulk_edit_saves_only_changed_rows_in_one_commit(self):
        connection = MagicMock()
        changes = [self._change(12), self._change(4)]
        with patch(
            'db.artigos.db_connection',
            side_effect=self._connection_context(connection),
        ), patch(
            'db.artigos._update_artigo_administrativo_in_transaction',
            side_effect=[
                {'found': True, 'changed': True, 'origin_type': 'centro_interno'},
                {'found': True, 'changed': False, 'origin_type': 'por_resolver'},
            ],
        ) as update_row, patch(
            'db.artigos.invalidate_prefix',
        ) as invalidate:
            result = update_artigos_administrativos_bulk(changes, actor='gestor')

        self.assertEqual(result, {
            'updated': 1, 'unchanged': 1, 'unresolved': 1,
        })
        self.assertEqual(
            [call.args[1] for call in update_row.call_args_list],
            [4, 12],
        )
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
        invalidate.assert_called_once_with('artigos_administrativos')

    def test_bulk_edit_rolls_back_every_row_if_one_row_fails(self):
        connection = MagicMock()
        changes = [self._change(4), self._change(12)]
        with patch(
            'db.artigos.db_connection',
            side_effect=self._connection_context(connection),
        ), patch(
            'db.artigos._update_artigo_administrativo_in_transaction',
            side_effect=[
                {'found': True, 'changed': True, 'origin_type': 'centro_interno'},
                ValueError('A origem selecionada não existe ou está inativa.'),
            ],
        ), patch(
            'db.artigos.invalidate_prefix',
        ) as invalidate:
            with self.assertRaisesRegex(ValueError, r'Artigo #12'):
                update_artigos_administrativos_bulk(changes, actor='gestor')

        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()
        invalidate.assert_not_called()


if __name__ == '__main__':
    unittest.main()