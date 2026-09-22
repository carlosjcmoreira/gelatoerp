import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from db.connection import db_connection
from db.pastelaria import (
    add_stock_gelado,
    confirm_pesagem_draft,
    delete_stock_gelado,
    delete_stock_gelado_by_date,
    get_pesagem_audit_history,
    get_pesagem_batch_receipt,
    get_stock_gelado_df,
    restore_stock_gelado,
    save_pesagem_draft,
    update_stock_gelado,
)
from db.schema import (
    run_migrations_pesagem_audit,
    run_migrations_pesagem_draft_batches,
)


class PesagemAuditRecoveryIntegrationTests(unittest.TestCase):
    store_name = 'Audit Recovery Test Store'
    renamed_store_name = 'Audit Recovery Renamed Store'
    other_store_name = 'Audit Recovery Other Store'
    test_day = date(2026, 9, 18)

    @classmethod
    def setUpClass(cls):
        run_migrations_pesagem_draft_batches()
        run_migrations_pesagem_audit()

    def setUp(self):
        self._cleanup()
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO stores (
                    name, is_active, supports_vendas,
                    requires_eod_weighing
                ) VALUES (%s, TRUE, TRUE, TRUE)
                RETURNING id
            """, (self.store_name,))
            self.store_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO stores (
                    name, is_active, supports_vendas,
                    requires_eod_weighing
                ) VALUES (%s, TRUE, TRUE, TRUE)
                RETURNING id
            """, (self.other_store_name,))
            self.other_store_id = cursor.fetchone()[0]
            conn.commit()

    def tearDown(self):
        self._cleanup()

    def _client(self, gestor=False):
        from flask_app.app import create_app

        app = create_app()
        app.config['TESTING'] = True
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'id': 99 if gestor else 10,
                'username': 'manager-user' if gestor else 'store-user',
                'acesso_gestor': gestor,
                'acesso_producao': gestor,
                'vendas_store_ids': [] if gestor else [self.store_id],
                'loja_id': self.store_id,
            }
        return client

    @classmethod
    def _cleanup(cls):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM stock_gelado_carapinas
                WHERE stock_gelado_id IN (
                    SELECT id FROM stock_gelado
                    WHERE loja IN (%s, %s, %s)
                )
            """, (
                cls.store_name, cls.renamed_store_name,
                cls.other_store_name,
            ))
            cursor.execute("""
                DELETE FROM stock_gelado
                WHERE loja IN (%s, %s, %s)
            """, (
                cls.store_name, cls.renamed_store_name,
                cls.other_store_name,
            ))
            cursor.execute("""
                DELETE FROM pesagem_draft_batches
                WHERE loja IN (%s, %s, %s)
            """, (
                cls.store_name, cls.renamed_store_name,
                cls.other_store_name,
            ))
            cursor.execute("""
                DELETE FROM pesagem_day_justifications
                WHERE loja IN (%s, %s, %s)
            """, (
                cls.store_name, cls.renamed_store_name,
                cls.other_store_name,
            ))
            cursor.execute("""
                DELETE FROM stores WHERE name IN (%s, %s, %s)
            """, (
                cls.store_name, cls.renamed_store_name,
                cls.other_store_name,
            ))
            conn.commit()

    def test_create_edit_delete_restore_keeps_immutable_timeline(self):
        stock_id = add_stock_gelado(
            self.test_day,
            self.store_name,
            'Chocolate',
            3.2,
            'fim',
            actor_id=10,
            actor_username='store-user',
            origin='test',
        )
        update_stock_gelado(
            stock_id,
            2.8,
            loja=self.store_name,
            actor_id=11,
            actor_username='production-user',
            reason='Correção da leitura',
            origin='test',
        )
        delete_stock_gelado(
            stock_id,
            'Registo inserido por engano',
            actor_id=10,
            actor_username='store-user',
            origin='test',
        )

        self.assertEqual(
            get_stock_gelado_df(
                loja=self.store_name,
                data_inicio=self.test_day,
                data_fim=self.test_day,
            ),
            [],
        )
        restored = restore_stock_gelado(
            stock_id,
            'Reposição confirmada pelo gestor',
            actor_id=12,
            actor_username='manager-user',
        )
        self.assertTrue(restored['is_active'])

        events = get_pesagem_audit_history(
            self.store_id,
            data_inicio=self.test_day,
            data_fim=self.test_day,
        )
        self.assertEqual(
            {event['event_type'] for event in events},
            {'create', 'edit', 'delete', 'restore'},
        )
        edit = next(event for event in events if event['event_type'] == 'edit')
        self.assertEqual(edit['before_data']['quantidade_kg'], 3.2)
        self.assertEqual(edit['after_data']['quantidade_kg'], 2.8)

        with db_connection() as conn:
            cursor = conn.cursor()
            with self.assertRaises(Exception):
                cursor.execute("""
                    UPDATE pesagem_audit_events
                    SET reason = 'mutated'
                    WHERE id = %s
                """, (events[0]['id'],))
            conn.rollback()

    def test_full_day_delete_requires_reason_and_exact_count(self):
        for flavour in ('Chocolate', 'Baunilha'):
            add_stock_gelado(
                self.test_day,
                self.store_name,
                flavour,
                1.0,
                'fim',
                actor_username='store-user',
                origin='test',
            )

        with self.assertRaisesRegex(ValueError, 'motivo'):
            delete_stock_gelado_by_date(
                self.store_name, self.test_day, '', 2
            )
        with self.assertRaisesRegex(ValueError, 'agora 2 linha'):
            delete_stock_gelado_by_date(
                self.store_name,
                self.test_day,
                'Fecho registado no dia errado',
                1,
            )

        deleted = delete_stock_gelado_by_date(
            self.store_name,
            self.test_day,
            'Fecho registado no dia errado',
            2,
            actor_id=10,
            actor_username='store-user',
        )
        self.assertEqual(deleted, 2)
        events = get_pesagem_audit_history(
            self.store_id,
            data_inicio=self.test_day,
            data_fim=self.test_day,
        )
        day_event = next(
            event for event in events
            if event['event_type'] == 'delete_day'
        )
        self.assertEqual(day_event['affected_count'], 2)
        self.assertEqual(day_event['expected_count'], 2)

    def test_confirmed_receipt_survives_row_deactivation(self):
        batch_id = str(uuid.uuid4())
        draft = save_pesagem_draft(
            batch_id,
            None,
            self.store_name,
            self.store_id,
            10,
            'store-user',
            [{
                'data': self.test_day,
                'sabor': 'Chocolate',
                'quantidade_kg': 3.2,
                'suspeito': False,
            }],
        )
        receipt = confirm_pesagem_draft(
            batch_id,
            draft['revision'],
            self.store_name,
            'store-user',
            10,
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM stock_gelado
                WHERE source_batch_id = %s AND is_active = TRUE
            """, (batch_id,))
            stock_id = cursor.fetchone()[0]
        delete_stock_gelado(
            stock_id,
            'Correção posterior ao comprovativo',
            actor_id=10,
            actor_username='store-user',
        )

        loaded = get_pesagem_batch_receipt(batch_id, self.store_name)
        self.assertEqual(loaded['id'], receipt['id'])
        self.assertEqual(loaded['inserted_count'], 1)
        self.assertEqual(len(loaded['entries']), 1)

    def test_foreign_batch_failure_does_not_mutate_other_store(self):
        foreign_batch_id = str(uuid.uuid4())
        save_pesagem_draft(
            foreign_batch_id,
            None,
            self.other_store_name,
            self.other_store_id,
            20,
            'other-user',
            [{
                'data': self.test_day,
                'sabor': 'Baunilha',
                'quantidade_kg': 1.5,
                'suspeito': False,
            }],
        )

        with self.assertRaisesRegex(ValueError, 'já não existe'):
            confirm_pesagem_draft(
                foreign_batch_id,
                1,
                self.store_name,
                'store-user',
                10,
            )

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, error_message
                FROM pesagem_draft_batches
                WHERE id = %s
            """, (foreign_batch_id,))
            status, error_message = cursor.fetchone()
        self.assertEqual(status, 'draft')
        self.assertIsNone(error_message)
        events = get_pesagem_audit_history(self.store_id)
        failure = next(
            event for event in events
            if event['event_type'] == 'failure'
            and str(event['batch_id']) == foreign_batch_id
        )
        self.assertEqual(failure['outcome'], 'failed')
        self.assertEqual(failure['affected_count'], 0)

    def test_store_delete_and_manager_restore_are_available_in_history_ui(self):
        stock_id = add_stock_gelado(
            self.test_day,
            self.store_name,
            'Chocolate',
            3.2,
            'fim',
            actor_id=10,
            actor_username='store-user',
            origin='test',
        )
        store_client = self._client()
        response = store_client.post(
            f'/vendas/pesagem?loja_id={self.store_id}',
            data={
                'action': 'delete',
                'id': str(stock_id),
                'data': self.test_day.isoformat(),
                'reason': 'Leitura registada por engano',
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Hist', response.data)
        self.assertIn(b'desativada', response.data)

        manager_client = self._client(gestor=True)
        response = manager_client.post(
            (
                '/vendas/pesagem/auditoria/restaurar'
                f'?loja_id={self.store_id}'
            ),
            data={
                'stock_id': str(stock_id),
                'data': self.test_day.isoformat(),
                'reason': 'Reposição confirmada após investigação',
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'reposta', response.data)
        active = get_stock_gelado_df(
            loja=self.store_name,
            data_inicio=self.test_day,
            data_fim=self.test_day,
        )
        self.assertEqual([row['id'] for row in active], [stock_id])

    def test_simultaneous_restore_creates_one_active_row_and_one_event(self):
        stock_id = add_stock_gelado(
            self.test_day,
            self.store_name,
            'Chocolate',
            3.2,
            'fim',
            actor_username='store-user',
            origin='test',
        )
        delete_stock_gelado(
            stock_id,
            'Preparar teste de reposição concorrente',
            actor_username='store-user',
            origin='test',
        )

        def attempt(number):
            try:
                restore_stock_gelado(
                    stock_id,
                    f'Reposição concorrente autorizada {number}',
                    actor_id=100 + number,
                    actor_username=f'manager-{number}',
                )
                return 'restored'
            except ValueError:
                return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, (1, 2)))

        self.assertEqual(sorted(outcomes), ['rejected', 'restored'])
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM stock_gelado
                WHERE id = %s AND is_active = TRUE
            """, (stock_id,))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_store_rename_keeps_uniqueness_day_delete_and_restore_stable(self):
        old_stock_id = add_stock_gelado(
            self.test_day,
            self.store_name,
            'Chocolate',
            3.2,
            'fim',
            actor_username='store-user',
            origin='test',
        )
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE stores SET name = %s WHERE id = %s",
                (self.renamed_store_name, self.store_id),
            )
            conn.commit()

        with self.assertRaises(Exception):
            add_stock_gelado(
                self.test_day,
                self.renamed_store_name,
                'Chocolate',
                2.5,
                'fim',
                actor_username='store-user',
                origin='test',
            )
        add_stock_gelado(
            self.test_day,
            self.renamed_store_name,
            'Baunilha',
            1.5,
            'fim',
            actor_username='store-user',
            origin='test',
        )
        visible = get_stock_gelado_df(
            loja=self.renamed_store_name,
            data_inicio=self.test_day,
            data_fim=self.test_day,
        )
        self.assertEqual({row['sabor'] for row in visible}, {
            'Chocolate', 'Baunilha',
        })

        deleted = delete_stock_gelado_by_date(
            self.renamed_store_name,
            self.test_day,
            'Eliminar fecho após renomear a loja',
            2,
            actor_username='store-user',
        )
        self.assertEqual(deleted, 2)
        restored = restore_stock_gelado(
            old_stock_id,
            'Reposição após confirmar identidade da loja',
            actor_username='manager-user',
        )
        self.assertTrue(restored['is_active'])
        with self.assertRaises(Exception):
            add_stock_gelado(
                self.test_day,
                self.renamed_store_name,
                'Chocolate',
                2.5,
                'fim',
                actor_username='store-user',
                origin='test',
            )
            cursor.execute("""
                SELECT COUNT(*) FROM pesagem_audit_events
                WHERE stock_id = %s AND event_type = 'restore'
            """, (stock_id,))
            self.assertEqual(cursor.fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()