import unittest
from datetime import date
from decimal import Decimal

from db.gelado_producao_envios import (
    _normalise_rows,
    submission_content_hash,
)


class GeladoProductionSubmissionUnitTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            'sabor': 'Baunilha',
            'pesagem_mat': Decimal('0'),
            'pesagem_mat_explicit': False,
            'prod_bolhao': Decimal('20.000'),
            'prod_matosinhos': Decimal('0'),
            'prod_mouzinho': Decimal('0'),
            'prod_b2b': Decimal('0'),
        }
        row.update(overrides)
        return row

    def test_hash_is_stable_for_decimal_format_and_row_order(self):
        first = [
            self._row(),
            self._row(sabor='Chocolate', prod_bolhao=Decimal('1.5')),
        ]
        second = [
            self._row(sabor='Chocolate', prod_bolhao=Decimal('1.5000')),
            self._row(prod_bolhao=Decimal('20')),
        ]
        self.assertEqual(
            submission_content_hash(date(2026, 10, 2), first),
            submission_content_hash(date(2026, 10, 2), second),
        )

    def test_explicit_zero_weighing_is_content_different_from_blank(self):
        blank = self._row(prod_bolhao=Decimal('0'))
        explicit_zero = self._row(
            prod_bolhao=Decimal('0'),
            pesagem_mat_explicit=True,
        )
        self.assertNotEqual(
            submission_content_hash(date(2026, 10, 2), [blank]),
            submission_content_hash(date(2026, 10, 2), [explicit_zero]),
        )

    def test_normalization_rejects_negative_non_finite_and_excess_precision(self):
        for value in (Decimal('-0.001'), Decimal('NaN'), Decimal('Infinity')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _normalise_rows([self._row(prod_bolhao=value)])
        with self.assertRaisesRegex(ValueError, 'quatro casas'):
            _normalise_rows([self._row(prod_bolhao=Decimal('1.00001'))])

    def test_normalization_rejects_duplicate_or_blank_flavour(self):
        with self.assertRaisesRegex(ValueError, 'mais do que uma vez'):
            _normalise_rows([self._row(), self._row()])
        with self.assertRaisesRegex(ValueError, 'sabor válido'):
            _normalise_rows([self._row(sabor='  ')])


if __name__ == '__main__':
    unittest.main()