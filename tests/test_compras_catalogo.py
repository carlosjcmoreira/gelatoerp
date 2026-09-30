"""Focused checks for the versioned Compras catalogue seed and identity rules."""

import unittest

from db.artigos import (
    catalog_key_for,
    classify_compras_origin_label,
    infer_artigo_unidade,
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


if __name__ == '__main__':
    unittest.main()