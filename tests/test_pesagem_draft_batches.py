import os
import unittest
import uuid
from datetime import date

from db.connection import db_connection
from db.pastelaria import (
    confirm_pesagem_draft,
    get_open_pesagem_draft,
    register_pesagem_draft,
    save_pesagem_draft,
)
from db.schema import run_migrations_pesagem_draft_batches


@unittest.skipUnless(
    os.environ.get('DATABASE_URL'),
    'PostgreSQL is required for weighing-draft integration tests',
)
class PesagemDraftBatchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations_pesagem_draft_batches()

    def setUp(self):
        self.loja = f'__test_pesagem_draft_{uuid.uuid4().hex}'
        self.batch_id = str(uuid.uuid4())
        self.entries = [
            {
                'data': date(2098, 1, 2),
                'sabor': 'Chocolate',
                'quantidade_kg': 3.125,
                'suspeito': False,
            },
            {
                'data': date(2098, 1, 2),
                'sabor': 'Pistacchio',
                'quantidade_kg': 2.75,
                'suspeito': False,
            },
        ]

    def tearDown(self):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM stock_gelado WHERE loja = %s",
                (self.loja,),
            )
            cursor.execute(
                "DELETE FROM pesagem_draft_batches WHERE loja = %s",
                (self.loja,),
            )
            conn.commit()

    def _stock_rows(self):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT sabor, quantidade_kg
                FROM stock_gelado
                WHERE loja = %s
                ORDER BY sabor
            """, (self.loja,))
            return cursor.fetchall()

    def _save(self, entries=None, batch_id=None, revision=None):
        return save_pesagem_draft(
            batch_id or self.batch_id,
            revision,
            self.loja,
            None,
            123,
            'test-user',
            self.entries if entries is None else entries,
        )

    def test_draft_is_recoverable_and_does_not_create_stock(self):
        saved = self._save()
        recovered = get_open_pesagem_draft(self.loja)

        self.assertEqual(saved['id'], self.batch_id)
        self.assertEqual(recovered['id'], self.batch_id)
        self.assertEqual(recovered['status'], 'draft')
        self.assertEqual(len(recovered['entries']), 2)
        self.assertEqual(self._stock_rows(), [])

    def test_registration_is_atomic_and_confirmation_is_idempotent(self):
        draft = self._save()

        registered = register_pesagem_draft(
            self.batch_id, draft['revision'], self.loja, 'test-user'
        )
        self.assertEqual(registered['status'], 'registered')
        self.assertEqual(len(self._stock_rows()), 2)

        first_receipt = confirm_pesagem_draft(
            self.batch_id, registered['revision'], self.loja, 'test-user'
        )
        second_receipt = confirm_pesagem_draft(
            self.batch_id, registered['revision'], self.loja, 'test-user'
        )

        self.assertEqual(first_receipt['status'], 'confirmed')
        self.assertEqual(first_receipt['inserted_count'], 2)
        self.assertEqual(second_receipt['id'], first_receipt['id'])
        self.assertEqual(second_receipt['confirmed_at'], first_receipt['confirmed_at'])
        self.assertIsNone(get_open_pesagem_draft(self.loja))

    def test_conflict_rolls_back_every_new_stock_row_and_keeps_draft(self):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO stock_gelado (
                    data, loja, sabor, quantidade_kg, tipo
                ) VALUES (%s, %s, %s, %s, 'fim')
            """, (
                self.entries[0]['data'],
                self.loja,
                self.entries[0]['sabor'],
                1.0,
            ))
            conn.commit()
        self._save()

        with self.assertRaisesRegex(ValueError, 'Nenhuma foi registada'):
            register_pesagem_draft(
                self.batch_id, 1, self.loja, 'test-user'
            )

        stock_rows = self._stock_rows()
        recovered = get_open_pesagem_draft(self.loja)
        self.assertEqual(len(stock_rows), 1)
        self.assertEqual(stock_rows[0][0], 'Chocolate')
        self.assertEqual(recovered['status'], 'failed')
        self.assertEqual(len(recovered['entries']), 2)
        self.assertIn('Nenhuma foi registada', recovered['error_message'])

    def test_second_device_updates_the_same_open_batch(self):
        first = self._save()
        replacement = [dict(self.entries[0], quantidade_kg=4.5)]

        second = self._save(
            entries=replacement,
            batch_id=first['id'],
            revision=first['revision'],
        )

        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['expected_count'], 1)
        self.assertEqual(second['entries'][0]['quantidade_kg'], 4.5)
        self.assertEqual(self._stock_rows(), [])

    def test_stale_device_cannot_overwrite_a_newer_draft(self):
        first = self._save()
        newer = self._save(
            entries=[dict(self.entries[0], quantidade_kg=4.5)],
            batch_id=first['id'],
            revision=first['revision'],
        )

        with self.assertRaisesRegex(ValueError, 'outro dispositivo'):
            self._save(
                entries=[dict(self.entries[0], quantidade_kg=9.0)],
                batch_id=first['id'],
                revision=first['revision'],
            )

        recovered = get_open_pesagem_draft(self.loja)
        self.assertEqual(recovered['revision'], newer['revision'])
        self.assertEqual(recovered['entries'][0]['quantidade_kg'], 4.5)

    def test_stale_device_cannot_delete_a_newer_draft(self):
        first = self._save()
        newer = self._save(
            entries=[dict(self.entries[0], quantidade_kg=4.5)],
            batch_id=first['id'],
            revision=first['revision'],
        )

        with self.assertRaisesRegex(ValueError, 'outro dispositivo'):
            self._save(
                entries=[],
                batch_id=first['id'],
                revision=first['revision'],
            )

        recovered = get_open_pesagem_draft(self.loja)
        self.assertEqual(recovered['revision'], newer['revision'])
        self.assertEqual(recovered['entries'][0]['quantidade_kg'], 4.5)

    def test_confirmation_cannot_register_an_unreviewed_newer_revision(self):
        first = self._save()
        newer = self._save(
            entries=[dict(self.entries[0], quantidade_kg=4.5)],
            batch_id=first['id'],
            revision=first['revision'],
        )

        with self.assertRaisesRegex(ValueError, 'depois da revisão'):
            register_pesagem_draft(
                first['id'],
                first['revision'],
                self.loja,
                'test-user',
            )

        recovered = get_open_pesagem_draft(self.loja)
        self.assertEqual(self._stock_rows(), [])
        self.assertEqual(recovered['revision'], newer['revision'])
        self.assertEqual(recovered['entries'][0]['quantidade_kg'], 4.5)


class ManualPesagemEntryValidationTests(unittest.TestCase):
    def test_duplicate_flavour_for_same_day_is_rejected(self):
        from flask_app.routes.vendas import _parse_manual_pesagem_entries

        with self.assertRaisesRegex(ValueError, 'mais do que uma vez'):
            _parse_manual_pesagem_entries([
                {
                    'data': '2026-09-22',
                    'sabor': 'Pistacchio',
                    'quantidade_kg': 1.0,
                },
                {
                    'data': '2026-09-22',
                    'sabor': 'pistachio',
                    'quantidade_kg': 2.0,
                },
            ])

    def test_non_finite_quantity_is_rejected(self):
        from flask_app.routes.vendas import _parse_manual_pesagem_entries

        with self.assertRaisesRegex(ValueError, 'número válido'):
            _parse_manual_pesagem_entries([{
                'data': '2026-09-22',
                'sabor': 'Pistacchio',
                'quantidade_kg': float('nan'),
            }])


if __name__ == '__main__':
    unittest.main()