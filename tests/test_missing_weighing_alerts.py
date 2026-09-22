import os
import unittest
import uuid
from datetime import date, timedelta
from unittest.mock import patch

from db.connection import db_connection
from db.pastelaria import (
    add_stock_gelado,
    add_stock_gelado_bulk,
    get_open_pesagem_draft,
    save_pesagem_draft,
    update_stock_gelado,
)
from db.plano import get_pesagens_loja_3dias
from db.schema import (
    run_migrations_pesagem_day_justifications,
    run_migrations_pesagem_draft_batches,
)
from db.weighing_status import (
    get_daily_weighing_statuses,
    justify_missing_weighing,
    portugal_today,
)


@unittest.skipUnless(
    os.environ.get('DATABASE_URL'),
    'PostgreSQL is required for weighing-status integration tests',
)
class MissingWeighingStatusIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations_pesagem_draft_batches()
        run_migrations_pesagem_day_justifications()

    def setUp(self):
        self.store_name = f'__test_eod_{uuid.uuid4().hex}'
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO stores (
                    name, is_active, requires_eod_weighing
                ) VALUES (%s, TRUE, TRUE)
                RETURNING id
            """, (self.store_name,))
            self.store_id = cursor.fetchone()[0]
            conn.commit()

    def tearDown(self):
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM stock_gelado WHERE loja = %s OR store_id = %s",
                (self.store_name, self.store_id),
            )
            cursor.execute(
                "DELETE FROM pesagem_draft_batches WHERE loja = %s OR store_id = %s",
                (self.store_name, self.store_id),
            )
            cursor.execute(
                "DELETE FROM pesagem_day_justifications WHERE store_id = %s",
                (self.store_id,),
            )
            cursor.execute(
                "DELETE FROM stores WHERE id = %s",
                (self.store_id,),
            )
            conn.commit()

    def _draft(self, day):
        return save_pesagem_draft(
            str(uuid.uuid4()),
            None,
            self.store_name,
            self.store_id,
            123,
            'test-user',
            [
                {
                    'data': day,
                    'sabor': 'Chocolate',
                    'quantidade_kg': 3.2,
                    'suspeito': False,
                },
                {
                    'data': day,
                    'sabor': 'Pistacchio',
                    'quantidade_kg': 2.1,
                    'suspeito': False,
                },
            ],
        )

    def _store_client(self):
        from flask_app.app import create_app

        app = create_app()
        app.config['TESTING'] = True
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'id': 654,
                'username': 'store-user',
                'acesso_gestor': False,
                'acesso_producao': False,
                'vendas_store_ids': [self.store_id],
                'loja_id': self.store_id,
            }
        return client

    def test_three_day_view_uses_consecutive_calendar_dates_across_month(self):
        result = get_pesagens_loja_3dias(
            self.store_name,
            end_date=date(2026, 3, 1),
        )

        self.assertEqual(
            result['dates'],
            [date(2026, 2, 27), date(2026, 2, 28), date(2026, 3, 1)],
        )
        self.assertEqual(result['rows'], [])
        self.assertEqual(
            [status['state'] for status in result['date_statuses']],
            ['missing', 'missing', 'missing'],
        )

    def test_draft_state_includes_count_and_last_update(self):
        day = date(2026, 9, 21)
        self._draft(day)

        status = get_daily_weighing_statuses(
            [day], [self.store_name]
        )[(self.store_name, day)]

        self.assertEqual(status['state'], 'draft')
        self.assertEqual(status['entry_count'], 2)
        self.assertIsNotNone(status['updated_at_local'])
        self.assertEqual(
            [entry['sabor'] for entry in status['entries']],
            ['Chocolate', 'Pistacchio'],
        )
        self.assertIsNotNone(status['batch_id'])
        self.assertEqual(status['revision'], 1)

    def test_confirmed_stock_takes_precedence_over_open_draft(self):
        day = date(2026, 9, 21)
        self._draft(day)
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO stock_gelado (
                    data, loja, sabor, quantidade_kg, tipo, store_id
                ) VALUES (%s, %s, 'Chocolate', 3.2, 'fim', %s)
            """, (day, self.store_name, self.store_id))
            conn.commit()

        status = get_daily_weighing_statuses(
            [day], [self.store_name]
        )[(self.store_name, day)]

        self.assertEqual(status['state'], 'confirmed')
        self.assertEqual(status['entry_count'], 1)

    def test_justification_is_audited_without_creating_stock(self):
        day = date(2026, 9, 20)
        justify_missing_weighing(
            self.store_id,
            day,
            'Loja encerrada para manutenção',
            321,
            'production-user',
        )

        status = get_daily_weighing_statuses(
            [day], [self.store_name]
        )[(self.store_name, day)]

        self.assertEqual(status['state'], 'justified')
        self.assertEqual(
            status['reason'],
            'Loja encerrada para manutenção',
        )
        self.assertEqual(status['created_by'], 'production-user')
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM stock_gelado WHERE loja = %s",
                (self.store_name,),
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_justified_day_rejects_single_and_bulk_stock_writes(self):
        day = date(2026, 9, 20)
        justify_missing_weighing(
            self.store_id,
            day,
            'Loja encerrada para manutenção',
            321,
            'production-user',
        )

        with self.assertRaisesRegex(ValueError, 'justificado'):
            add_stock_gelado(
                day, self.store_name, 'Chocolate', 3.2, 'fim'
            )
        with self.assertRaisesRegex(ValueError, 'justificado'):
            add_stock_gelado_bulk([{
                'data': day,
                'sabor': 'Chocolate',
                'quantidade_kg': 3.2,
                'tipo': 'fim',
            }], self.store_name)

    def test_existing_stock_cannot_be_moved_into_justified_day(self):
        source_day = date(2026, 9, 19)
        justified_day = date(2026, 9, 20)
        stock_id = add_stock_gelado(
            source_day, self.store_name, 'Chocolate', 3.2, 'fim'
        )
        justify_missing_weighing(
            self.store_id,
            justified_day,
            'Loja encerrada para manutenção',
            321,
            'production-user',
        )

        with self.assertRaisesRegex(ValueError, 'justificado'):
            update_stock_gelado(
                stock_id,
                4.1,
                self.store_name,
                nova_data=justified_day,
            )

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT data, quantidade_kg FROM stock_gelado WHERE id = %s",
                (stock_id,),
            )
            saved_day, saved_kg = cursor.fetchone()
        self.assertEqual(saved_day, source_day)
        self.assertEqual(float(saved_kg), 3.2)

    def test_legacy_stock_without_store_id_cannot_bypass_justification(self):
        source_day = date(2026, 9, 19)
        justified_day = date(2026, 9, 20)
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO stock_gelado (
                    data, loja, sabor, quantidade_kg, tipo, store_id
                ) VALUES (%s, %s, 'Chocolate', 3.2, 'fim', NULL)
                RETURNING id
            """, (source_day, self.store_name))
            stock_id = cursor.fetchone()[0]
            conn.commit()
        justify_missing_weighing(
            self.store_id,
            justified_day,
            'Loja encerrada para manutenção',
            321,
            'production-user',
        )

        with self.assertRaisesRegex(ValueError, 'justificado'):
            update_stock_gelado(
                stock_id,
                4.1,
                self.store_name,
                nova_data=justified_day,
            )

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT data, quantidade_kg FROM stock_gelado WHERE id = %s",
                (stock_id,),
            )
            saved_day, saved_kg = cursor.fetchone()
        self.assertEqual(saved_day, source_day)
        self.assertEqual(float(saved_kg), 3.2)

    def test_day_with_draft_cannot_be_justified(self):
        day = date(2026, 9, 21)
        self._draft(day)

        with self.assertRaisesRegex(ValueError, 'rascunho'):
            justify_missing_weighing(
                self.store_id,
                day,
                'Loja encerrada para manutenção',
                321,
                'production-user',
            )

    def test_current_day_cannot_be_justified(self):
        with self.assertRaisesRegex(ValueError, 'encerrado'):
            justify_missing_weighing(
                self.store_id,
                portugal_today(),
                'Loja encerrada para manutenção',
                321,
                'production-user',
            )

    def test_status_follows_store_id_after_store_is_renamed(self):
        day = date(2026, 9, 21)
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO stock_gelado (
                    data, loja, sabor, quantidade_kg, tipo, store_id
                ) VALUES (%s, %s, 'Chocolate', 3.2, 'fim', %s)
            """, (day, self.store_name, self.store_id))
            renamed = f'{self.store_name}_renamed'
            cursor.execute(
                "UPDATE stores SET name = %s WHERE id = %s",
                (renamed, self.store_id),
            )
            conn.commit()
        self.store_name = renamed

        status = get_daily_weighing_statuses(
            [day], [renamed]
        )[(renamed, day)]

        self.assertEqual(status['state'], 'confirmed')
        self.assertEqual(status['entry_count'], 1)
        with self.assertRaisesRegex(ValueError, 'já tem'):
            justify_missing_weighing(
                self.store_id,
                day,
                'Loja encerrada para manutenção',
                321,
                'production-user',
            )

    def test_reused_store_name_cannot_open_previous_store_draft(self):
        original_name = self.store_name
        day = date(2026, 9, 21)
        self._draft(day)
        renamed = f'{original_name}_renamed'
        second_store_id = None
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE stores SET name = %s WHERE id = %s",
                    (renamed, self.store_id),
                )
                cursor.execute("""
                    INSERT INTO stores (
                        name, is_active, requires_eod_weighing
                    ) VALUES (%s, TRUE, TRUE)
                    RETURNING id
                """, (original_name,))
                second_store_id = cursor.fetchone()[0]
                conn.commit()
            self.store_name = renamed

            self.assertIsNone(get_open_pesagem_draft(original_name))
            self.assertIsNotNone(get_open_pesagem_draft(renamed))
        finally:
            if second_store_id:
                with db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "DELETE FROM stores WHERE id = %s",
                        (second_store_id,),
                    )
                    conn.commit()

    def test_production_only_user_can_confirm_reviewed_draft(self):
        day = date(2026, 9, 21)
        draft = self._draft(day)
        from flask_app.app import create_app

        app = create_app()
        app.config['TESTING'] = True
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'id': 321,
                'username': 'production-only',
                'acesso_producao': True,
                'acesso_gestor': False,
                'vendas_store_ids': [],
            }

        response = client.post(
            '/producao/pesagens-loja?days=3',
            data={
                'action': 'confirm_draft',
                'loja': self.store_name,
                'batch_id': draft['id'],
                'revision': draft['revision'],
                'loja_redirect': self.store_name,
            },
        )

        self.assertEqual(response.status_code, 302)
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM pesagem_draft_batches WHERE id = %s",
                (draft['id'],),
            )
            self.assertEqual(cursor.fetchone()[0], 'confirmed')
            cursor.execute(
                "SELECT COUNT(*) FROM stock_gelado WHERE store_id = %s",
                (self.store_id,),
            )
            self.assertEqual(cursor.fetchone()[0], 2)

    def test_store_landing_alerts_missing_previous_day_without_navigation(self):
        client = self._store_client()
        previous_day = portugal_today() - timedelta(days=1)

        response = client.get('/vendas/')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Ontem por resolver', response.data)
        self.assertIn(
            previous_day.isoformat().encode(),
            response.data,
        )

    def test_store_dashboard_alerts_previous_day_draft(self):
        previous_day = portugal_today() - timedelta(days=1)
        self._draft(previous_day)
        client = self._store_client()
        dashboard_data = {
            'mode': 'fecho_caixa',
            'rows': [],
            'total_ontem': '0.000',
            'total_recebido': '0.000',
            'total_fim': None,
            'fecho': None,
        }

        with patch(
            'flask_app.routes.vendas.vendas_svc.build_dashboard_rows',
            return_value=dashboard_data,
        ):
            response = client.get('/vendas/dashboard')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Pesagem de ontem por resolver', response.data)
        self.assertIn(b'2 entrada(s) est', response.data)
        self.assertIn(previous_day.isoformat().encode(), response.data)

    def test_store_add_to_justified_day_returns_message_not_server_error(self):
        justified_day = portugal_today() - timedelta(days=1)
        justify_missing_weighing(
            self.store_id,
            justified_day,
            'Loja encerrada para manutenção',
            321,
            'production-user',
        )
        client = self._store_client()

        response = client.post(
            f'/vendas/pesagem?loja_id={self.store_id}',
            data={
                'action': 'add',
                'sabor': 'Chocolate',
                'data': justified_day.isoformat(),
                'quantidade[]': ['3,200'],
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'foi justificado como sem pesagem', response.data)

    def test_three_day_route_ignores_historical_end_date_override(self):
        from flask_app.app import create_app

        app = create_app()
        app.config['TESTING'] = True
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'id': 321,
                'username': 'production-only',
                'acesso_producao': True,
                'acesso_gestor': False,
                'vendas_store_ids': [],
            }
        fake = {
            'dates': [],
            'date_labels': [],
            'dates_iso': [],
            'date_statuses': [],
            'rows': [],
        }
        with patch(
            'flask_app.routes.producao.get_pesagens_loja_3dias',
            return_value=fake,
        ) as get_three_days:
            response = client.get(
                '/producao/pesagens-loja'
                f'?days=3&loja={self.store_name}&end_date=2020-01-01'
            )

        self.assertEqual(response.status_code, 200)
        get_three_days.assert_called_once_with(
            self.store_name,
            portugal_today() - timedelta(days=1),
        )


if __name__ == '__main__':
    unittest.main()