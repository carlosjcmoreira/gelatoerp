import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from threading import Barrier
from pathlib import Path
from unittest.mock import patch

import psycopg2
from flask import Flask
from db import area, pastelaria, pastelaria_stock, plano, schema


class _Cursor:
    def __init__(self, batches=()):
        self.batches = list(batches)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchall(self):
        return self.batches.pop(0) if self.batches else []

    def fetchone(self):
        rows = self.batches.pop(0) if self.batches else []
        return rows[0] if rows else None


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self, **_kwargs):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PastelariaStockPriorityTests(unittest.TestCase):
    TOKEN_81 = '81' * 16
    TOKEN_82 = '82' * 16

    def _complete_cursor(self):
        stores = [{'id': 1, 'name': 'Bolhão'}, {'id': 2, 'name': 'Matosinhos'}]
        products = [
            {'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''},
            {'id': 11, 'tipologia': 'Bolo', 'sabor': 'Chocolate', 'cobertura': ''},
            {'id': 12, 'tipologia': 'Nivotto', 'sabor': '', 'cobertura': ''},
        ]
        counts = [
            {'loja': 'Bolhão', 'produto': 'Palito', 'quantidade': 2,
             'produto_pastelaria_id': 10},
            {'loja': 'Matosinhos', 'produto': 'Palito', 'quantidade': 10,
             'produto_pastelaria_id': 10},
            {'loja': 'Bolhão', 'produto': 'Bolo, Chocolate', 'quantidade': 0,
             'produto_pastelaria_id': 11},
            {'loja': 'Matosinhos', 'produto': 'Bolo, Chocolate', 'quantidade': 4,
             'produto_pastelaria_id': 11},
            {'loja': 'Bolhão', 'produto': 'Nivotto', 'quantidade': 9,
             'produto_pastelaria_id': 12},
            {'loja': 'Matosinhos', 'produto': 'Nivotto', 'quantidade': 9,
             'produto_pastelaria_id': 12},
        ]
        minimums = [
            {'produto_id': 10, 'store_id': 1, 'quantidade_minima': 10},
            {'produto_id': 10, 'store_id': 2, 'quantidade_minima': 10},
            {'produto_id': 11, 'store_id': 1, 'quantidade_minima': 5},
            {'produto_id': 11, 'store_id': 2, 'quantidade_minima': 5},
            {'produto_id': 12, 'store_id': 1, 'quantidade_minima': 5},
            {'produto_id': 12, 'store_id': 2, 'quantidade_minima': 5},
        ]
        return _Cursor([stores, products, counts, minimums])

    def test_calculates_store_shortage_and_orders_by_percentage(self):
        cursor = self._complete_cursor()
        result = pastelaria._pastelaria_priority_inputs(cursor, date(2026, 9, 6))
        self.assertTrue(result['complete'])
        self.assertEqual(
            [row['produto'] for row in result['rows']],
            ['Bolo, Chocolate', 'Palito'],
        )
        self.assertEqual(result['rows'][0]['quantidade_total'], 6)
        self.assertEqual(result['rows'][0]['percentagem_falta'], 0.6)
        self.assertEqual(result['rows'][1]['quantidade_total'], 8)
        self.assertEqual(result['rows'][1]['percentagem_falta'], 0.4)
        self.assertEqual(result['rows'][0]['prioridade'], 1)

    def test_missing_product_count_blocks_generation_instead_of_assuming_zero(self):
        cursor = self._complete_cursor()
        cursor.batches[2] = cursor.batches[2][:-1]
        result = pastelaria._pastelaria_priority_inputs(cursor, date(2026, 9, 6))
        self.assertFalse(result['complete'])
        self.assertIn(
            {'loja': 'Matosinhos', 'produto': 'Nivotto'}, result['missing']
        )

    def test_stock_page_renders_partial_store_counts_as_unknown(self):
        from flask_app.routes import pastelaria as pastelaria_routes

        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.add_url_rule('/', endpoint='home.index', view_func=lambda: '')
        app.add_url_rule('/login', endpoint='auth.login', view_func=lambda: '')
        app.register_blueprint(
            pastelaria_routes.pastelaria_bp, url_prefix='/pastelaria'
        )
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'loja',
                'role': 'vendas',
                'acesso_pastelaria': True,
            }

        stores = [
            {'id': 1, 'name': 'Bolhão'},
            {'id': 2, 'name': 'Matosinhos'},
        ]
        partial_stock = [{
            'produto': 'Palito',
            'loja': 'Matosinhos',
            'quantidade': 7,
            'data': date(2026, 9, 13),
        }]
        count_status = {
            'stores': stores,
            'products': [{
                'id': 10,
                'nome': 'Palito',
                'counts': {1: None, 2: 0},
            }],
            'completed': 1,
            'total': 2,
            'completed_by_store': {1: 0, 2: 1},
            'total_by_store': {1: 1, 2: 1},
        }
        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value={'stores': stores, 'products': []},
            ),
            patch(
                'flask_app.routes.pastelaria.get_ultimo_stock_balcao',
                return_value=partial_stock,
            ),
            patch(
                'flask_app.routes.pastelaria.get_produtos_pastelaria',
                return_value=['Palito'],
            ),
            patch(
                'flask_app.routes.pastelaria.get_contagem_stock_df',
                return_value=pastelaria_routes.pd.DataFrame(),
            ),
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_sunday_count_grid',
                return_value=count_status,
            ),
            patch(
                'flask_app.routes.pastelaria._tabs_with_urls',
                return_value=[],
            ),
        ):
            response = client.get('/pastelaria/stock-balcao')

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('7', html)
        self.assertIn('13/09/2026', html)
        self.assertIn('—', html)
        self.assertRegex(html, r'1\s+contagem\s+em falta')
        self.assertIn('<strong>Bolhão:</strong>', html)
        self.assertIn('Palito', html)
        self.assertNotIn('<strong>Matosinhos:</strong>', html)

    def test_non_sunday_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'domingo'):
            pastelaria._pastelaria_priority_inputs(_Cursor(), date(2026, 9, 7))

    def test_minimums_reject_decimals_and_negative_values(self):
        for value in (-1, 1.5, True):
            with self.subTest(value=value), patch(
                'db.pastelaria.db_connection',
                return_value=_Connection(_Cursor()),
            ):
                with self.assertRaisesRegex(ValueError, 'inteiros não negativos'):
                    pastelaria.save_pastelaria_stock_minimums([(1, 2, value)])

    def test_sunday_grid_loads_latest_values_and_reports_progress(self):
        cursor = _Cursor([
            [{'id': 1, 'name': 'Bolhão'}, {'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
            [{'loja': 'Bolhão', 'produto': 'Palito', 'quantidade': 3,
              'produto_pastelaria_id': 10}],
            [{'snapshot_token': self.TOKEN_81}],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            grid = pastelaria.get_pastelaria_sunday_count_grid(
                date(2026, 9, 6)
            )
        self.assertEqual(grid['completed'], 1)
        self.assertEqual(grid['total'], 2)
        self.assertFalse(grid['complete'])
        self.assertEqual(grid['products'][0]['counts'], {1: 3, 2: None})
        self.assertEqual(grid['snapshot_token'], self.TOKEN_81)

    def test_count_identity_migration_only_backfills_unique_exact_labels(self):
        first_cursor = _Cursor([[(True,)], []])
        first_connection = _Connection(first_cursor)
        with patch(
            'db.schema.db_connection',
            return_value=first_connection,
        ):
            schema.run_migrations_pastelaria_count_product_id()

        statements = '\n'.join(query for query, _ in first_cursor.queries)
        self.assertTrue(first_connection.committed)
        self.assertIn('ADD COLUMN IF NOT EXISTS produto_pastelaria_id', statements)
        self.assertIn('HAVING COUNT(*) = 1', statements)
        self.assertIn('cs.produto = ul.label', statements)
        self.assertNotIn('LOWER(cs.produto)', statements)

        second_cursor = _Cursor([[(True,)], [(1,)]])
        second_connection = _Connection(second_cursor)
        with patch(
            'db.schema.db_connection',
            return_value=second_connection,
        ):
            schema.run_migrations_pastelaria_count_product_id()
        rerun_statements = '\n'.join(
            query for query, _ in second_cursor.queries
        )
        self.assertNotIn('UPDATE contagem_stock cs', rerun_statements)

    def test_store_grid_is_scoped_to_requested_store(self):
        cursor = _Cursor([
            [{'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
            [{'produto': 'Palito', 'quantidade': 4,
              'produto_pastelaria_id': 10}],
            [{'snapshot_token': self.TOKEN_81}],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            grid = pastelaria.get_pastelaria_store_count_grid(
                date(2026, 9, 6), 2
            )
        self.assertEqual(grid['store']['name'], 'Matosinhos')
        self.assertEqual(grid['products'][0]['count'], 4)
        self.assertEqual(grid['completed'], 1)
        self.assertEqual(grid['total'], 1)

    def test_weekday_store_grid_requires_explicit_daily_mode(self):
        count_date = date(2026, 9, 9)
        with self.assertRaisesRegex(ValueError, 'domingo'):
            pastelaria.get_pastelaria_store_count_grid(count_date, 2)

        cursor = _Cursor([
            [{'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
            [],
            [{'snapshot_token': self.TOKEN_81}],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            grid = pastelaria.get_pastelaria_store_count_grid(
                count_date, 2, allow_non_sunday=True,
            )

        self.assertEqual(grid['date'], count_date)
        self.assertEqual(grid['products'][0]['count'], None)
        self.assertFalse(grid['complete'])
        self.assertTrue(any(
            "origem='contagem'" in query for query, _ in cursor.queries
        ))
        self.assertIn(
            (f'pastelaria-count:{count_date.isoformat()}',),
            [params for query, params in cursor.queries
             if 'pg_advisory_xact_lock' in query],
        )

    def test_unlinked_legacy_label_is_not_invented_as_catalogue_identity(self):
        cursor = _Cursor([
            [{'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
            [{'produto': 'Palito', 'quantidade': 4,
              'produto_pastelaria_id': None}],
            [{'snapshot_token': self.TOKEN_81}],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            grid = pastelaria.get_pastelaria_store_count_grid(
                date(2026, 9, 6), 2
            )
        self.assertIsNone(grid['products'][0]['count'])
        self.assertEqual(grid['completed'], 0)

    @patch('db.pastelaria.execute_values')
    def test_store_count_save_keeps_other_stores_and_uses_date_lock(self, bulk):
        cursor = _Cursor([
            [{'id': 2, 'name': 'Matosinhos'}],
            [{'snapshot_token': self.TOKEN_81}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
        ])
        connection = _Connection(cursor)
        with patch(
            'db.pastelaria.db_connection',
            return_value=connection,
        ):
            saved = pastelaria.save_pastelaria_store_counts(
                date(2026, 9, 6), 2, [(10, 7)], self.TOKEN_81
            )
        self.assertEqual(saved, 1)
        self.assertTrue(connection.committed)
        bulk.assert_called_once()
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('pg_advisory_xact_lock', statements)
        self.assertIn(
            ('pastelaria-count:2026-09-06',),
            [params for query, params in cursor.queries if 'pg_advisory_xact_lock' in query],
        )
        self.assertNotIn('DELETE FROM contagem_stock', statements)

    @patch('db.pastelaria.execute_values')
    def test_daily_store_count_saves_physical_rows_on_weekdays(self, bulk):
        count_date = date(2026, 9, 9)
        cursor = _Cursor([
            [{'id': 2, 'name': 'Matosinhos'}],
            [{'snapshot_token': self.TOKEN_81}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
        ])
        connection = _Connection(cursor)
        with patch(
            'db.pastelaria.db_connection',
            return_value=connection,
        ):
            saved = pastelaria.save_pastelaria_store_counts(
                count_date, 2, [(10, 7)], self.TOKEN_81,
                allow_non_sunday=True,
            )

        self.assertEqual(saved, 1)
        self.assertTrue(connection.committed)
        self.assertEqual(
            bulk.call_args.args[2],
            [(count_date, 'Matosinhos', 'Palito', 7, 'pastelaria',
              'contagem', 10)],
        )
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn("origem='contagem'", statements)
        self.assertIn('pg_advisory_xact_lock', statements)

    @patch('db.pastelaria.execute_values')
    def test_complete_sunday_grid_is_saved_once_and_keeps_history(self, bulk):
        cursor = _Cursor([
            [{'snapshot_token': self.TOKEN_81}],
            [{'id': 1, 'name': 'Bolhão'}, {'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
        ])
        connection = _Connection(cursor)
        with patch('db.pastelaria.db_connection', return_value=connection):
            pastelaria.save_pastelaria_sunday_counts(
                date(2026, 9, 6), [(10, 1, 3), (10, 2, 0)],
                self.TOKEN_81,
            )
        self.assertTrue(connection.committed)
        bulk.assert_called_once()
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('pg_advisory_xact_lock', statements)
        self.assertNotIn('DELETE FROM contagem_stock', statements)

    def test_partial_sunday_grid_is_rejected_without_writes(self):
        cursor = _Cursor([
            [{'snapshot_token': self.TOKEN_81}],
            [{'id': 1, 'name': 'Bolhão'}, {'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
        ])
        connection = _Connection(cursor)
        with patch('db.pastelaria.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'todas as contagens'):
                pastelaria.save_pastelaria_sunday_counts(
                    date(2026, 9, 6), [(10, 1, 3)], self.TOKEN_81
                )
        self.assertFalse(connection.committed)

    def test_stale_sunday_grid_is_rejected_before_insert(self):
        cursor = _Cursor([[{'snapshot_token': self.TOKEN_82}]])
        connection = _Connection(cursor)
        with (
            patch('db.pastelaria.db_connection', return_value=connection),
            patch('db.pastelaria.execute_values') as bulk,
        ):
            with self.assertRaisesRegex(ValueError, 'outro utilizador'):
                pastelaria.save_pastelaria_sunday_counts(
                    date(2026, 9, 6), [(10, 1, 3)], self.TOKEN_81
                )
        bulk.assert_not_called()
        self.assertFalse(connection.committed)

    def test_sunday_grid_parser_requires_every_cell(self):
        from flask_app.routes.pastelaria import _parse_sunday_count_matrix
        config = {
            'products': [{'id': 10}],
            'stores': [{'id': 1}, {'id': 2}],
        }
        with self.assertRaisesRegex(ValueError, 'todas as contagens'):
            _parse_sunday_count_matrix(config, {'count_10_1': '3'})
        self.assertEqual(
            _parse_sunday_count_matrix(
                config, {'count_10_1': '3', 'count_10_2': '0'}
            ),
            [(10, 1, 3), (10, 2, 0)],
        )

    def test_generation_creates_a_new_locked_snapshot_version(self):
        cursor = self._complete_cursor()
        cursor.batches.extend([[{'next_version': 3}], [{'id': 44}]])
        connection = _Connection(cursor)
        with patch('db.pastelaria.db_connection', return_value=connection):
            plan_id = pastelaria.generate_pastelaria_priority_plan(
                date(2026, 9, 6), actor='produtor',
            )
        self.assertEqual(plan_id, 44)
        self.assertTrue(connection.committed)
        statements = '\n'.join(query for query, _ in cursor.queries)
        self.assertIn('pg_advisory_xact_lock', statements)
        self.assertIn('MAX(versao)', statements)
        self.assertNotIn('DELETE FROM pastelaria_plano_prioridade_linhas', statements)
        self.assertEqual(
            statements.count('INSERT INTO pastelaria_plano_prioridade_linhas'), 2
        )

    def test_missing_minimum_configuration_blocks_generation(self):
        cursor = self._complete_cursor()
        cursor.batches[3] = cursor.batches[3][:-1]
        result = pastelaria._pastelaria_priority_inputs(cursor, date(2026, 9, 6))
        self.assertFalse(result['complete'])
        self.assertIn(
            {'loja': 'Matosinhos', 'produto': 'Nivotto'},
            result['missing_minimums'],
        )

    def test_minimum_matrix_requires_every_product_store_value(self):
        from flask_app.routes.pastelaria import (
            _parse_stock_minimum_matrix,
        )
        config = {
            'products': [{'id': 10}],
            'stores': [{'id': 1}, {'id': 2}],
        }
        with self.assertRaisesRegex(ValueError, 'todos os stocks mínimos'):
            _parse_stock_minimum_matrix(config, {'min_10_1': '5'})
        self.assertEqual(
            _parse_stock_minimum_matrix(
                config, {'min_10_1': '5', 'min_10_2': '0'}
            ),
            [(10, 1, 5), (10, 2, 0)],
        )

    def test_product_state_change_preserves_identity_and_minimum_rows(self):
        cursor = _Cursor([[{'id': 10, 'ativo': True}, {'id': 11, 'ativo': False}]])
        connection = _Connection(cursor)
        token = pastelaria.pastelaria_product_state_token([
            {'id': 10, 'ativo': True}, {'id': 11, 'ativo': False},
        ])
        with patch('db.pastelaria.db_connection', return_value=connection):
            changed = pastelaria.save_produtos_pastelaria_active([
                (10, False), (11, False),
            ], token, actor_id=7, actor_username='gestor')
        self.assertEqual(changed, 1)
        self.assertTrue(connection.committed)
        statements = '\n'.join(query for query, _params in cursor.queries)
        self.assertIn('UPDATE produtos_pastelaria SET ativo=%s', statements)
        audit_inserts = [
            params for query, params in cursor.queries
            if 'INSERT INTO pastelaria_produto_estado_audit' in query
        ]
        self.assertEqual(
            audit_inserts,
            [(10, 7, 'gestor', True, False)],
        )
        self.assertNotIn('DELETE', statements)
        self.assertNotIn('pastelaria_stock_minimos', statements)

    def test_pastelaria_user_can_change_product_states(self):
        from flask_app.routes.pastelaria import pastelaria_bp
        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        products = [{
            'id': 10, 'tipologia': 'Palito', 'sabor': '',
            'cobertura': '', 'ativo': True,
        }]
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'loja', 'role': 'vendas',
                'acesso_pastelaria': True,
            }
        with (
            patch(
                'flask_app.routes.pastelaria.db.get_all_produtos_pastelaria',
                return_value=products,
            ),
            patch(
                'flask_app.routes.pastelaria.db.save_produtos_pastelaria_active',
                return_value=1,
            ) as save,
        ):
            response = client.post(
                '/pastelaria/produtos',
                data={
                    'action': 'save_product_states',
                    'state_10': 'inactive', 'state_token': 'token-a',
                },
            )
        self.assertEqual(response.status_code, 302)
        save.assert_called_once_with(
            [(10, False)], 'token-a'
        )

    def test_stale_product_state_form_is_rejected(self):
        cursor = _Cursor([[{'id': 10, 'ativo': False}]])
        connection = _Connection(cursor)
        with patch('db.pastelaria.db_connection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'outro utilizador'):
                pastelaria.save_produtos_pastelaria_active(
                    [(10, True)], 'stale-token'
                )
        self.assertFalse(connection.committed)
        self.assertFalse(any(
            'UPDATE produtos_pastelaria' in query
            for query, _params in cursor.queries
        ))
        self.assertFalse(any(
            'INSERT INTO pastelaria_produto_estado_audit' in query
            for query, _params in cursor.queries
        ))

    def test_product_state_history_returns_recent_actor_and_transition(self):
        history = [{
            'id': 3,
            'produto_id': 10,
            'actor_id': 7,
            'actor_username': 'gestor',
            'previous_active': True,
            'new_active': False,
            'changed_at': date(2026, 9, 14),
            'tipologia': 'Palitos',
            'sabor': '',
            'cobertura': '',
        }]
        cursor = _Cursor([history])
        connection = _Connection(cursor)
        with patch('db.pastelaria.db_connection', return_value=connection):
            result = pastelaria.get_pastelaria_product_state_history(10)
        self.assertEqual(result[0]['produto'], 'Palitos')
        self.assertEqual(result[0]['actor_username'], 'gestor')
        self.assertTrue(result[0]['previous_active'])
        self.assertFalse(result[0]['new_active'])
        self.assertIn('ORDER BY a.changed_at DESC, a.id DESC', cursor.queries[0][0])

    def test_pastelaria_user_can_add_product_but_permanent_delete_stays_blocked(self):
        from flask_app.routes.pastelaria import pastelaria_bp
        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'loja', 'role': 'vendas',
                'acesso_pastelaria': True,
            }
        with (
            patch('flask_app.routes.pastelaria.db.add_produto_pastelaria') as add,
            patch(
                'flask_app.routes.pastelaria.db.delete_produtos_pastelaria_bulk'
            ) as delete,
        ):
            add_response = client.post('/pastelaria/produtos', data={
                'action': 'add_produto_past', 'novo_tipologia': 'Palito',
            })
            delete_response = client.post('/pastelaria/produtos', data={
                'action': 'delete_bulk_produtos_past', 'produto_ids': '10',
            })
        self.assertEqual(add_response.status_code, 302)
        self.assertEqual(delete_response.status_code, 302)
        add.assert_called_once_with('Palito', '', '')
        delete.assert_not_called()

    def test_breakage_rejects_inactive_or_forged_product(self):
        from flask_app.routes.pastelaria import pastelaria_bp
        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'operador', 'role': 'producao',
                'acesso_pastelaria': True,
            }
        with (
            patch(
                'flask_app.routes.pastelaria.get_produtos_pastelaria',
                return_value=['Palito'],
            ),
            patch('flask_app.routes.pastelaria.add_quebra_area') as add,
        ):
            response = client.post('/pastelaria/registar-quebra', data={
                'data': '2026-09-12', 'quantidade': '2',
                'produto': 'Nivotto inativo', 'motivo': 'teste',
            })
        self.assertEqual(response.status_code, 302)
        add.assert_not_called()

    def test_schema_blocks_catalog_product_deletion_when_minimums_exist(self):
        schema = Path('db/schema.py').read_text()
        self.assertIn(
            'REFERENCES produtos_pastelaria(id) ON DELETE RESTRICT', schema
        )
        self.assertIn(
            'REFERENCES produtos_pastelaria(id) ON DELETE SET NULL', schema
        )

    def test_product_workflows_only_expose_reversible_inactivation(self):
        product_template = Path(
            'flask_app/templates/pastelaria/lista_produtos.html'
        ).read_text()
        legacy_template = Path(
            'flask_app/templates/gestor/_produtos_pastelaria.html'
        ).read_text()
        for template in (product_template, legacy_template):
            self.assertNotIn('delete_produto_past', template)
            self.assertNotIn('delete_bulk_produtos_past', template)
            self.assertNotIn('produto_past_id', template)
        self.assertIn('value="inactive"', product_template)
        self.assertIn('eliminação permanente', legacy_template)

    def test_retired_product_delete_helpers_are_hard_delete_guards(self):
        with self.assertRaisesRegex(ValueError, 'eliminação permanente'):
            pastelaria.delete_produto_pastelaria(10)
        with self.assertRaisesRegex(ValueError, 'eliminação permanente'):
            pastelaria.delete_produtos_pastelaria_bulk([10])

    def test_routes_reject_partial_configuration_and_allow_module_generation(self):
        from flask_app.routes.pastelaria import pastelaria_bp

        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        config = {
            'products': [{'id': 10}],
            'stores': [{'id': 1, 'name': 'Bolhão'}, {'id': 2, 'name': 'Matosinhos'}],
        }
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'gestor', 'role': 'gestao',
                'acesso_pastelaria': True, 'acesso_gestor': True,
            }
        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value=config,
            ),
            patch(
                'flask_app.routes.pastelaria.save_pastelaria_stock_minimums'
            ) as save,
        ):
            response = client.post(
                '/pastelaria/produtos',
                data={'action': 'save_minimos_stock', 'min_10_1': '5'},
            )
        self.assertEqual(response.status_code, 302)
        save.assert_not_called()

        with client.session_transaction() as session:
            session['user'] = {
                'username': 'vendas', 'role': 'vendasmat',
                'acesso_pastelaria': True, 'acesso_gestor': False,
            }
        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_priority_status',
                return_value={
                    'complete': True, 'rows': [], 'missing': [],
                    'missing_minimums': [],
                },
            ),
            patch(
                'flask_app.routes.pastelaria.list_pastelaria_priority_plans',
                return_value=[],
            ),
            patch(
                'flask_app.routes.pastelaria.generate_pastelaria_priority_plan'
            ) as generate,
        ):
            response = client.post(
                '/pastelaria/planear',
                data={
                    'action': 'gerar_plano',
                    'data_contagem': '2026-09-06',
                },
            )
        self.assertEqual(response.status_code, 302)
        generate.assert_called_once()

    def test_store_scoped_grid_only_requires_the_selected_store(self):
        cursor = _Cursor([
            [{'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
            [{'loja': 'Matosinhos', 'produto': 'Palito', 'quantidade': 4,
              'produto_pastelaria_id': 10}],
            [{'snapshot_token': self.TOKEN_81}],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            grid = pastelaria.get_pastelaria_sunday_count_grid(
                date(2026, 9, 6), store_id=2,
            )
        self.assertEqual([store['name'] for store in grid['stores']], ['Matosinhos'])
        self.assertEqual(grid['total'], 1)
        self.assertTrue(grid['complete'])
        self.assertTrue(any('id=%s' in query for query, _ in cursor.queries))

    def test_unscoped_save_does_not_inherit_last_cell_store(self):
        cursor = _Cursor([
            [{'snapshot_token': self.TOKEN_81}],
            [{'id': 1, 'name': 'Bolhão'}, {'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
        ])
        with (
            patch('db.pastelaria.db_connection', return_value=_Connection(cursor)),
            patch('db.pastelaria.execute_values'),
        ):
            pastelaria.save_pastelaria_sunday_counts(
                date(2026, 9, 6),
                [(10, 1, 3), (10, 2, 4)],
                self.TOKEN_81,
            )
        token_params = cursor.queries[1][1]
        stores_params = cursor.queries[2][1]
        self.assertEqual(token_params, (date(2026, 9, 6),))
        self.assertIsNone(stores_params)

    def test_invalid_plan_post_never_generates_fallback_sunday(self):
        from flask_app.routes.pastelaria import pastelaria_bp
        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'gestor', 'role': 'gestao',
                'acesso_pastelaria': True, 'acesso_gestor': True,
            }
        fallback = {
            'complete': False, 'rows': [], 'missing': [],
            'missing_minimums': [],
        }
        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_priority_status',
                side_effect=[
                    ValueError('O plano só pode ser gerado a partir de domingo.'),
                    fallback,
                ],
            ),
            patch(
                'flask_app.routes.pastelaria.list_pastelaria_priority_plans',
                return_value=[],
            ),
            patch(
                'flask_app.routes.pastelaria.generate_pastelaria_priority_plan'
            ) as generate,
        ):
            response = client.post(
                '/pastelaria/planear', data={'domingo': '2026-09-07'}
            )
        self.assertEqual(response.status_code, 200)
        generate.assert_not_called()

    def test_store_user_cannot_switch_pastelaria_count_to_another_store(self):
        from flask_app.routes.vendas import vendas_bp
        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(vendas_bp, url_prefix='/vendas')
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'bolhao', 'role': 'vendas',
                'vendas_store_ids': [1], 'acesso_pastelaria': True,
            }
        store_rows = {
            1: {'id': 1, 'name': 'Bolhão', 'store_type': 'loja',
                'requires_eod_weighing': True},
            2: {'id': 2, 'name': 'Matosinhos', 'store_type': 'loja',
                'requires_eod_weighing': True},
        }
        grid = {
            'products': [], 'stores': [store_rows[1]], 'completed': 0,
            'total': 0, 'complete': False, 'snapshot_token': self.TOKEN_81,
        }
        with (
            patch(
                'flask_app.routes.vendas.get_store_by_id',
                side_effect=lambda store_id: store_rows.get(store_id),
            ),
            patch(
                'flask_app.routes.vendas.get_pastelaria_sunday_count_grid',
                return_value=grid,
            ) as get_grid,
            patch(
                'flask_app.routes.vendas._build_tabs', return_value=[]
            ),
            patch(
                'flask_app.routes.vendas.render_template',
                side_effect=lambda _name, **context: context['loja_nome'],
            ),
        ):
            response = client.get(
                '/vendas/contagem-pastelaria?loja_id=2&data=2026-09-06'
            )
        self.assertEqual(response.get_data(as_text=True), 'Bolhão')
        get_grid.assert_called_once_with(date(2026, 9, 6), 1)

    def test_count_conflict_keeps_store_values_and_stock_view_is_read_only(self):
        from flask_app.routes.vendas import vendas_bp
        from flask_app.routes.pastelaria import pastelaria_bp
        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(vendas_bp, url_prefix='/vendas')
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'bolhao', 'role': 'vendas',
                'vendas_store_ids': [1], 'acesso_pastelaria': True,
            }
        store = {
            'id': 1, 'name': 'Bolhão', 'store_type': 'loja',
            'requires_eod_weighing': True,
        }
        def fresh_grid():
            return {
                'products': [{
                    'id': 10, 'nome': 'Palito', 'counts': {1: 2},
                }],
                'stores': [store], 'completed': 1, 'total': 1,
                'complete': True, 'snapshot_token': self.TOKEN_82,
            }
        rendered = {}
        def capture(_name, **context):
            rendered.update(context)
            return str(context['grid']['products'][0]['counts'][1])
        with (
            patch('flask_app.routes.vendas.get_store_by_id', return_value=store),
            patch(
                'flask_app.routes.vendas.get_pastelaria_sunday_count_grid',
                side_effect=[fresh_grid(), fresh_grid()],
            ),
            patch(
                'flask_app.routes.vendas.save_pastelaria_sunday_counts',
                side_effect=ValueError('alterada por outro utilizador'),
            ),
            patch('flask_app.routes.vendas._build_tabs', return_value=[]),
            patch('flask_app.routes.vendas.render_template', side_effect=capture),
        ):
            response = client.post('/vendas/contagem-pastelaria', data={
                '_loja_id': '2', 'data': '2026-09-06',
                'snapshot_token': self.TOKEN_81, 'count_10': '7',
            })
        self.assertEqual(response.get_data(as_text=True), '7')
        self.assertIn('alterada por outro utilizador', rendered['error'])
        self.assertEqual(client.post('/pastelaria/stock-balcao').status_code, 405)

    def test_pastelaria_count_page_allows_authorized_cross_store_weekday_count(self):
        from flask_app.routes.pastelaria import pastelaria_bp

        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.add_url_rule('/home', endpoint='home.index', view_func=lambda: '')
        app.add_url_rule('/login', endpoint='auth.login', view_func=lambda: '')
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        stores = [
            {'id': 1, 'name': 'Bolhão'},
            {'id': 2, 'name': 'Matosinhos'},
        ]
        grid = {
            'store': stores[1],
            'products': [{
                'id': 10, 'nome': 'Palito', 'count': None,
            }],
            'completed': 0, 'total': 1, 'complete': False,
            'snapshot_token': self.TOKEN_81,
        }
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'pastelaria',
                'role': 'vendas',
                'vendas_store_ids': [1],
                'acesso_pastelaria': True,
            }

        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value={'stores': stores, 'products': []},
            ),
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_store_count_grid',
                return_value=grid,
            ) as get_grid,
            patch('flask_app.routes.pastelaria._tabs_with_urls', return_value=[]),
        ):
            response = client.get(
                '/pastelaria/contagem-stock?loja_id=2&data_contagem=2026-09-09'
            )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Contagem física de Pastelaria', html)
        self.assertIn('Matosinhos', html)
        self.assertIn('name="count_10"', html)
        get_grid.assert_called_once_with(
            date(2026, 9, 9), 2, allow_non_sunday=True,
        )

        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value={'stores': stores, 'products': []},
            ),
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_store_count_grid',
                return_value=grid,
            ) as get_grid,
            patch(
                'flask_app.routes.pastelaria.save_pastelaria_store_counts',
                return_value=1,
            ) as save_counts,
        ):
            response = client.post('/pastelaria/contagem-stock', data={
                'loja_id': '2',
                'data_contagem': '2026-09-09',
                'snapshot_token': self.TOKEN_81,
                'count_10': '4',
            })

        self.assertEqual(response.status_code, 302)
        self.assertIn('loja_id=2', response.location)
        self.assertIn('data_contagem=2026-09-09', response.location)
        get_grid.assert_called_once_with(
            date(2026, 9, 9), 2, allow_non_sunday=True,
        )
        save_counts.assert_called_once_with(
            date(2026, 9, 9), 2, [(10, 4)], self.TOKEN_81,
            allow_non_sunday=True,
        )

    def test_pastelaria_count_rejects_forged_store_and_missing_permission(self):
        from flask_app.routes.pastelaria import pastelaria_bp

        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.add_url_rule('/home', endpoint='home.index', view_func=lambda: '')
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        stores = [{'id': 1, 'name': 'Bolhão'}]
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'pastelaria',
                'role': 'vendas',
                'acesso_pastelaria': True,
            }

        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value={'stores': stores, 'products': []},
            ),
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_store_count_grid'
            ) as get_grid,
            patch(
                'flask_app.routes.pastelaria.save_pastelaria_store_counts'
            ) as save_counts,
            patch(
                'flask_app.routes.pastelaria._tabs_with_urls',
                return_value=[],
            ),
        ):
            response = client.post('/pastelaria/contagem-stock', data={
                'loja_id': '2',
                'data_contagem': '2026-09-09',
                'count_10': '4',
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('Loja inválida', response.get_data(as_text=True))
        get_grid.assert_not_called()
        save_counts.assert_not_called()

        with client.session_transaction() as session:
            session['user'] = {
                'username': 'vendas',
                'role': 'vendas',
                'vendas_store_ids': [1],
            }
        response = client.get('/pastelaria/contagem-stock')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/home')

    def test_pastelaria_stale_daily_count_keeps_entered_values(self):
        from flask_app.routes.pastelaria import pastelaria_bp

        app = Flask(__name__, template_folder='../flask_app/templates')
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        store = {'id': 2, 'name': 'Matosinhos'}

        def fresh_grid():
            return {
                'store': store,
                'products': [{
                    'id': 10, 'nome': 'Palito', 'count': None,
                }],
                'completed': 0, 'total': 1, 'complete': False,
                'snapshot_token': self.TOKEN_82,
            }

        rendered = {}
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'pastelaria',
                'acesso_pastelaria': True,
            }

        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value={'stores': [store], 'products': []},
            ),
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_store_count_grid',
                side_effect=[fresh_grid(), fresh_grid()],
            ),
            patch(
                'flask_app.routes.pastelaria.save_pastelaria_store_counts',
                side_effect=ValueError('A grelha foi alterada por outro utilizador.'),
            ),
            patch('flask_app.routes.pastelaria._tabs_with_urls', return_value=[]),
            patch(
                'flask_app.routes.pastelaria.render_template',
                side_effect=lambda _name, **context: (
                    rendered.update(context) or 'rendered'
                ),
            ),
        ):
            response = client.post('/pastelaria/contagem-stock', data={
                'loja_id': '2',
                'data_contagem': '2026-09-09',
                'snapshot_token': self.TOKEN_81,
                'count_10': '8',
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('outro utilizador', rendered['error'])
        self.assertEqual(rendered['grid']['products'][0]['count'], 8)

    def test_intelligence_calculates_rotation_and_keeps_sales_at_typology_level(self):
        cursor = _Cursor([
            [{
                'id': 10, 'tipologia': 'Palito', 'sabor': 'Pistacchio',
                'cobertura': '', 'ativo': True,
            }],
            [{'loja': 'Bolhão'}],
            [
                {
                    'data': date(2026, 1, 4), 'loja': 'Bolhão',
                    'produto': 'Palito, Pistacchio', 'quantidade': 10,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
                {
                    'data': date(2026, 1, 11), 'loja': 'Bolhão',
                    'produto': 'Palito, Pistacchio', 'quantidade': 4,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
            ],
            [{
                'data': date(2026, 1, 8), 'loja': 'Bolhão',
                'produto': 'Palito, Pistacchio', 'quantidade': 2,
                'produto_pastelaria_id': 10,
            }],
            [{
                'data': date(2026, 1, 9), 'loja': 'Bolhão',
                'produto': 'Palito, Pistacchio', 'quantidade': 1,
                'produto_pastelaria_id': 10,
            }],
            [{
                'data': date(2026, 1, 8), 'loja': 'Bolhão',
                'tipologia': 'Palitos', 'unidades': 20, 'valor': 100,
            }],
            [{
                'data': date(2025, 1, 8), 'loja': 'Bolhão',
                'tipologia': 'Palitos', 'unidades': 10, 'valor': 80,
            }],
            [
                {'data': date(2026, 1, day), 'loja': 'Bolhão', 'valor': 500}
                for day in range(1, 15)
            ],
            [
                {'data': date(2025, 1, day), 'loja': 'Bolhão', 'valor': 400}
                for day in range(1, 15)
            ],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            result = pastelaria.get_pastelaria_intelligence(
                date(2026, 1, 1), date(2026, 1, 14), loja='Bolhão'
            )
        interval = result['intervals'][0]
        self.assertEqual(interval['entradas_conhecidas'], 3)
        self.assertEqual(interval['consumo_estimado'], 9)
        self.assertEqual(interval['confianca'], 'baixa')
        self.assertTrue(interval['comparavel'])
        self.assertEqual(result['rotation_ranking'][0]['produto'],
                         'Palito, Pistacchio')
        self.assertEqual(result['sales_ranking'], [{
            'tipologia': 'Palitos', 'unidades': 20, 'valor': 100.0,
        }])
        self.assertEqual(result['summary']['pastry_share'], 1.4)
        self.assertEqual(result['sales_comparison'][0]['variacao'], 25.0)
        self.assertEqual(result['summary']['previous_sales_days'], 14)
        self.assertEqual(result['coverage_by_store'][0]['loja'], 'Bolhão')
        self.assertNotIn('Pistacchio', result['sales_ranking'][0]['tipologia'])
        statements = [query for query, _params in cursor.queries]
        count_query = next(
            query for query in statements if 'FROM contagem_stock' in query
            and 'DISTINCT ON' in query
        )
        transfer_query = next(
            query for query in statements if 'FROM ordens_transferencia' in query
        )
        self.assertIn("origem='contagem'", count_query)
        self.assertIn('confirmado_em::date', transfer_query)

    def test_intelligence_route_allows_pastelaria_user(self):
        from flask_app.routes.pastelaria import pastelaria_bp
        app = Flask(__name__)
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'loja', 'role': 'vendas',
                'acesso_pastelaria': True,
            }
        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_intelligence',
                return_value={'intervals': [], 'sales_series': []},
            ),
            patch(
                'flask_app.routes.pastelaria.render_template',
                return_value='ok',
            ),
        ):
            response = client.get('/pastelaria/inteligencia')
        self.assertEqual(response.status_code, 200)

    def test_intelligence_route_still_blocks_user_without_pastelaria_access(self):
        from flask_app.routes.pastelaria import pastelaria_bp
        app = Flask(__name__)
        app.secret_key = 'test'
        app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        app.add_url_rule('/', endpoint='home.index', view_func=lambda: 'home')
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = {
                'username': 'compras', 'role': 'compras',
                'acesso_pastelaria': False,
            }
        response = client.get('/pastelaria/inteligencia')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/')

    def test_intelligence_excludes_inconsistent_rotation_and_uses_store_days(self):
        cursor = _Cursor([
            [{
                'id': 10, 'tipologia': 'Palito', 'sabor': '',
                'cobertura': '', 'ativo': True,
            }],
            [{'loja': 'Bolhão'}, {'loja': 'Matosinhos'}],
            [
                {
                    'data': date(2026, 1, 1), 'loja': 'Bolhão',
                    'produto': 'Palito', 'quantidade': 1, 'created_at': None,
                },
                {
                    'data': date(2026, 1, 2), 'loja': 'Bolhão',
                    'produto': 'Palito', 'quantidade': 5, 'created_at': None,
                },
            ],
            [],
            [],
            [],
            [],
            [
                {'data': date(2026, 1, 1), 'loja': 'Bolhão', 'valor': 10},
                {'data': date(2026, 1, 2), 'loja': 'Matosinhos', 'valor': 10},
            ],
            [],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            result = pastelaria.get_pastelaria_intelligence(
                date(2026, 1, 1), date(2026, 1, 2)
            )
        self.assertTrue(result['intervals'][0]['inconsistencia'])
        self.assertEqual(result['rotation_ranking'], [])
        self.assertEqual(result['summary']['rotation_intervals'], 0)
        self.assertEqual(result['summary']['inconsistent_intervals'], 1)
        self.assertEqual(result['summary']['sales_coverage'], 50.0)
        self.assertFalse(result['summary']['comparison_comparable'])

    def test_print_template_has_calculated_and_handwritten_columns(self):
        template = Path(
            'flask_app/templates/pastelaria/plano_prioridade.html'
        ).read_text()
        self.assertIn('Quantidade total', template)
        self.assertIn('Distribuição por loja', template)
        self.assertIn('Data de produção', template)
        self.assertIn('Produtor', template)
        self.assertIn('Comentários', template)
        self.assertIn('@page { size: A4 landscape;', template)

    def test_active_planning_template_has_no_manual_weekly_or_cake_forms(self):
        template = Path('flask_app/templates/pastelaria/planear.html').read_text()
        self.assertNotIn('save_plano', template)
        self.assertNotIn('save_bolo', template)
        self.assertNotIn('Plano semanal', template)
        self.assertNotIn('Adicionar Bolo', template)

    def test_intelligence_includes_transfer_created_before_product_rename(self):
        cursor = _Cursor([
            [{
                'id': 10, 'tipologia': 'Palito Renomeado', 'sabor': '',
                'cobertura': '', 'ativo': True,
            }],
            [{'loja': 'Bolhão', 'active_sales_store': True}],
            [
                {
                    'data': date(2026, 9, 1), 'loja': 'Bolhão',
                    'produto': 'Palito Renomeado', 'quantidade': 5,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
                {
                    'data': date(2026, 9, 8), 'loja': 'Bolhão',
                    'produto': 'Palito Renomeado', 'quantidade': 2,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
            ],
            [],
            [{
                'data': date(2026, 9, 4), 'loja': 'Bolhão',
                'produto': 'Palito Renomeado', 'quantidade': 3,
                'produto_pastelaria_id': 10,
            }],
            [],
            [],
            [],
            [],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            result = pastelaria.get_pastelaria_intelligence(
                date(2026, 9, 1), date(2026, 9, 8), loja='Bolhão'
            )
        self.assertEqual(result['intervals'][0]['entradas_conhecidas'], 3)
        self.assertEqual(result['intervals'][0]['consumo_estimado'], 6)

    def test_intelligence_includes_production_recorded_before_product_rename(self):
        cursor = _Cursor([
            [{
                'id': 10, 'tipologia': 'Palito Renomeado', 'sabor': '',
                'cobertura': '', 'ativo': True,
            }],
            [{'loja': 'Bolhão', 'active_sales_store': True}],
            [
                {
                    'data': date(2026, 9, 1), 'loja': 'Bolhão',
                    'produto': 'Palito Renomeado', 'quantidade': 5,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
                {
                    'data': date(2026, 9, 8), 'loja': 'Bolhão',
                    'produto': 'Palito Renomeado', 'quantidade': 2,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
            ],
            [{
                'data': date(2026, 9, 4), 'loja': 'Bolhão',
                'produto': 'Palito Renomeado', 'quantidade': 3,
                'produto_pastelaria_id': 10,
            }],
            [],
            [],
            [],
            [],
            [],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            result = pastelaria.get_pastelaria_intelligence(
                date(2026, 9, 1), date(2026, 9, 8), loja='Bolhão'
            )
        self.assertEqual(result['intervals'][0]['entradas_conhecidas'], 3)
        self.assertEqual(result['intervals'][0]['consumo_estimado'], 6)

    def test_intelligence_keeps_reused_legacy_label_as_separate_identity(self):
        cursor = _Cursor([
            [{
                'id': 10, 'tipologia': 'Palito', 'sabor': '',
                'cobertura': '', 'ativo': True,
            }],
            [{'loja': 'Bolhão', 'active_sales_store': True}],
            [
                {
                    'data': date(2026, 9, 1), 'loja': 'Bolhão',
                    'produto': 'Palito', 'quantidade': 10,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
                {
                    'data': date(2026, 9, 8), 'loja': 'Bolhão',
                    'produto': 'Palito', 'quantidade': 7,
                    'created_at': None, 'produto_pastelaria_id': 10,
                },
                {
                    'data': date(2026, 9, 1), 'loja': 'Bolhão',
                    'produto': 'Palito', 'quantidade': 6,
                    'created_at': None, 'produto_pastelaria_id': None,
                },
                {
                    'data': date(2026, 9, 8), 'loja': 'Bolhão',
                    'produto': 'Palito', 'quantidade': 5,
                    'created_at': None, 'produto_pastelaria_id': None,
                },
            ],
            [],
            [],
            [],
            [],
            [],
            [],
        ])
        with patch(
            'db.pastelaria.db_connection',
            return_value=_Connection(cursor),
        ):
            result = pastelaria.get_pastelaria_intelligence(
                date(2026, 9, 1), date(2026, 9, 8), loja='Bolhão'
            )
        ranking = {
            row['identity_key']: row for row in result['rotation_ranking']
        }
        self.assertEqual(set(ranking), {'id:10', 'text:Palito'})
        self.assertEqual(ranking['id:10']['consumo_estimado'], 3)
        self.assertEqual(ranking['id:10']['stock_final'], 7)
        self.assertEqual(ranking['text:Palito']['consumo_estimado'], 1)
        self.assertEqual(ranking['text:Palito']['stock_final'], 5)
        self.assertEqual(
            ranking['text:Palito']['produto'],
            'Palito (histórico sem associação)',
        )


class PastelariaPlanMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            raise unittest.SkipTest(
                'DATABASE_URL is required for PostgreSQL integration tests'
            )

        try:
            cls.admin_connection = psycopg2.connect(database_url)
        except Exception as exc:
            raise unittest.SkipTest(
                f'PostgreSQL test database unavailable: {exc}'
            )

        cls.admin_connection.autocommit = True
        cls.schema_name = f'test_pastelaria_plan_migration_{uuid.uuid4().hex}'
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{cls.schema_name}"')
            cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            cursor.execute("""
                CREATE TABLE plano_producao_pastelaria (
                    id SERIAL PRIMARY KEY
                );
                CREATE TABLE stores (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE
                );
                CREATE TABLE produtos_pastelaria (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR(255) NOT NULL
                );
                CREATE TABLE pastelaria_stock_minimos (
                    produto_id INTEGER NOT NULL
                        REFERENCES produtos_pastelaria(id),
                    store_id INTEGER NOT NULL REFERENCES stores(id),
                    quantidade_minima INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (produto_id, store_id)
                );
                CREATE TABLE pastelaria_planos_prioridade (
                    id SERIAL PRIMARY KEY,
                    data_contagem DATE NOT NULL UNIQUE,
                    generated_by VARCHAR(255),
                    generated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE pastelaria_plano_prioridade_linhas (
                    id SERIAL PRIMARY KEY,
                    plano_id INTEGER NOT NULL
                        REFERENCES pastelaria_planos_prioridade(id)
                        ON DELETE CASCADE,
                    produto_id INTEGER NOT NULL
                        REFERENCES produtos_pastelaria(id),
                    produto VARCHAR(500) NOT NULL,
                    stock_minimo_total INTEGER NOT NULL,
                    stock_contado_total INTEGER NOT NULL,
                    quantidade_total INTEGER NOT NULL,
                    percentagem_falta NUMERIC(8,5) NOT NULL,
                    prioridade INTEGER NOT NULL,
                    distribuicao_lojas JSONB NOT NULL DEFAULT '[]'::jsonb,
                    UNIQUE (plano_id, produto_id)
                );

                INSERT INTO stores (name) VALUES ('Bolhão');
                INSERT INTO produtos_pastelaria (nome) VALUES ('Palito');
                INSERT INTO pastelaria_stock_minimos
                    (produto_id, store_id, quantidade_minima)
                VALUES (1, 1, 12);
                INSERT INTO pastelaria_planos_prioridade
                    (data_contagem, generated_by)
                VALUES ('2026-09-06', 'historico');
                INSERT INTO pastelaria_plano_prioridade_linhas (
                    plano_id, produto_id, produto, stock_minimo_total,
                    stock_contado_total, quantidade_total, percentagem_falta,
                    prioridade, distribuicao_lojas
                ) VALUES (
                    1, 1, 'Palito', 12, 4, 8, 0.66667, 1,
                    '[{"loja": "Bolhão", "quantidade": 8}]'
                );
            """)

        @contextmanager
        def isolated_connection():
            connection = psycopg2.connect(database_url)
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            try:
                yield connection
            finally:
                connection.close()

        cls.isolated_connection = staticmethod(isolated_connection)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'admin_connection', None):
            with cls.admin_connection.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{cls.schema_name}" CASCADE'
                )
            cls.admin_connection.close()

    def test_upgrade_preserves_history_and_delete_rules(self):
        with patch('db.schema.db_connection', self.isolated_connection):
            schema.run_migrations_pastelaria_plano()
            schema.run_migrations_pastelaria_plano()

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, data_contagem, versao, generated_by
                    FROM pastelaria_planos_prioridade
                    ORDER BY id
                """)
                self.assertEqual(
                    cursor.fetchall(),
                    [(1, date(2026, 9, 6), 1, 'historico')],
                )
                cursor.execute("""
                    SELECT plano_id, produto_id, produto, quantidade_total
                    FROM pastelaria_plano_prioridade_linhas
                """)
                self.assertEqual(cursor.fetchall(), [(1, 1, 'Palito', 8)])

                cursor.execute("""
                    INSERT INTO pastelaria_planos_prioridade
                        (data_contagem, versao, generated_by)
                    VALUES ('2026-09-06', 2, 'novo')
                    RETURNING versao
                """)
                self.assertEqual(cursor.fetchone()[0], 2)
                cursor.execute('SAVEPOINT duplicate_version')
                with self.assertRaises(psycopg2.errors.UniqueViolation):
                    cursor.execute("""
                        INSERT INTO pastelaria_planos_prioridade
                            (data_contagem, versao)
                        VALUES ('2026-09-06', 2)
                    """)
                cursor.execute('ROLLBACK TO SAVEPOINT duplicate_version')

                cursor.execute('SAVEPOINT product_delete')
                with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
                    cursor.execute('DELETE FROM produtos_pastelaria WHERE id = 1')
                cursor.execute('ROLLBACK TO SAVEPOINT product_delete')
                cursor.execute('SELECT COUNT(*) FROM pastelaria_stock_minimos')
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("""
                    SELECT produto_id, produto
                    FROM pastelaria_plano_prioridade_linhas
                    WHERE plano_id = 1
                """)
                self.assertEqual(cursor.fetchone(), (1, 'Palito'))
                cursor.execute("""
                    SELECT COUNT(*) FROM pastelaria_planos_prioridade
                    WHERE id = 1
                """)
                self.assertEqual(cursor.fetchone()[0], 1)


class PastelariaSundayConcurrencyPostgresTests(unittest.TestCase):
    """Exercise Sunday count writes with independent PostgreSQL sessions."""

    COUNT_DATE = date(2026, 9, 6)

    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            raise unittest.SkipTest(
                'DATABASE_URL is required for PostgreSQL integration tests'
            )

        try:
            cls.admin_connection = psycopg2.connect(database_url)
        except Exception as exc:
            raise unittest.SkipTest(
                f'PostgreSQL test database unavailable: {exc}'
            )

        cls.admin_connection.autocommit = True
        cls.schema_name = f'test_pastelaria_sunday_concurrency_{uuid.uuid4().hex}'
        with cls.admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{cls.schema_name}"')
            cursor.execute(f'SET search_path TO "{cls.schema_name}"')
            cursor.execute("""
                CREATE TABLE stores (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    supports_vendas BOOLEAN NOT NULL DEFAULT TRUE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                );
                CREATE TABLE produtos_pastelaria (
                    id SERIAL PRIMARY KEY,
                    tipologia VARCHAR(255) NOT NULL,
                    sabor VARCHAR(255) NOT NULL DEFAULT '',
                    cobertura VARCHAR(255) NOT NULL DEFAULT '',
                    ativo BOOLEAN NOT NULL DEFAULT TRUE
                );
                CREATE TABLE contagem_stock (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    loja VARCHAR(50) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    quantidade INTEGER NOT NULL,
                    tipo VARCHAR(50) NOT NULL,
                    origem VARCHAR(30) NOT NULL DEFAULT 'contagem',
                    produto_pastelaria_id INTEGER
                        REFERENCES produtos_pastelaria(id) ON DELETE SET NULL,
                    ordem_transferencia_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE ordens_transferencia (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    area_origem VARCHAR(100) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    sabor VARCHAR(255),
                    quantidade REAL NOT NULL,
                    unidade VARCHAR(50) NOT NULL DEFAULT 'kg',
                    loja_destino VARCHAR(100) NOT NULL,
                    loja_origem VARCHAR(100),
                    status VARCHAR(50) NOT NULL DEFAULT 'pendente',
                    criado_por VARCHAR(100),
                    confirmado_por VARCHAR(100),
                    confirmado_em TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    data_prevista DATE,
                    batch_id VARCHAR(100),
                    destino_tipo VARCHAR(20) NOT NULL DEFAULT 'loja',
                    destino_nome VARCHAR(255),
                    produto_pastelaria_id INTEGER
                        REFERENCES produtos_pastelaria(id) ON DELETE SET NULL,
                    rececao_estado VARCHAR(30) NOT NULL DEFAULT 'por_verificar',
                    aceite_por VARCHAR(100),
                    aceite_em TIMESTAMPTZ,
                    problema_por VARCHAR(100),
                    problema_em TIMESTAMPTZ,
                    motivo_problema TEXT
                );
                CREATE UNIQUE INDEX uq_test_contagem_transfer_order
                    ON contagem_stock(ordem_transferencia_id)
                    WHERE ordem_transferencia_id IS NOT NULL;
                CREATE TABLE transferencias_eventos (
                    id SERIAL PRIMARY KEY,
                    ordem_id INTEGER NOT NULL
                        REFERENCES ordens_transferencia(id) ON DELETE CASCADE,
                    event_type VARCHAR(50) NOT NULL,
                    utilizador VARCHAR(100),
                    motivo TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE plano_producao_pastelaria (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    producao_estimada INTEGER DEFAULT 0,
                    producao_real INTEGER,
                    no_plano BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR(20) DEFAULT 'pendente',
                    nota TEXT DEFAULT '',
                    producao_estimada_bolhao INTEGER DEFAULT 0,
                    producao_estimada_matosinhos INTEGER DEFAULT 0,
                    item_tipo VARCHAR(20) DEFAULT 'standard',
                    bolo_tamanho VARCHAR(50),
                    bolo_sabor_1 VARCHAR(255),
                    bolo_sabor_2 VARCHAR(255),
                    bolo_sabor_3 VARCHAR(255),
                    bolo_cobertura VARCHAR(255),
                    produto_pastelaria_id INTEGER
                        REFERENCES produtos_pastelaria(id) ON DELETE SET NULL
                );
                CREATE TABLE stock_producao_pastelaria (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    quantidade INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    produto_pastelaria_id INTEGER
                        REFERENCES produtos_pastelaria(id) ON DELETE SET NULL
                );
                CREATE TABLE producao_pastelaria (
                    id SERIAL PRIMARY KEY,
                    data DATE NOT NULL,
                    loja VARCHAR(100) NOT NULL,
                    produto VARCHAR(255) NOT NULL,
                    quantidade INTEGER NOT NULL,
                    lote VARCHAR(50),
                    store_id INTEGER,
                    produto_pastelaria_id INTEGER
                        REFERENCES produtos_pastelaria(id) ON DELETE SET NULL
                );
                CREATE UNIQUE INDEX plan_linked_identity
                    ON plano_producao_pastelaria (
                        data, produto_pastelaria_id
                    ) WHERE produto_pastelaria_id IS NOT NULL;
                CREATE UNIQUE INDEX plan_unlinked_identity
                    ON plano_producao_pastelaria (data, produto)
                    WHERE produto_pastelaria_id IS NULL;
                CREATE UNIQUE INDEX stock_linked_identity
                    ON stock_producao_pastelaria (
                        data, produto_pastelaria_id
                    ) WHERE produto_pastelaria_id IS NOT NULL;
                CREATE UNIQUE INDEX stock_unlinked_identity
                    ON stock_producao_pastelaria (data, produto)
                    WHERE produto_pastelaria_id IS NULL;

                INSERT INTO stores (name) VALUES ('Bolhão'), ('Matosinhos');
                INSERT INTO produtos_pastelaria (tipologia)
                VALUES ('Palito');
            """)

        @contextmanager
        def isolated_connection():
            connection = psycopg2.connect(database_url)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f'SET search_path TO "{cls.schema_name}"')
                yield connection
            finally:
                connection.close()

        cls.isolated_connection = staticmethod(isolated_connection)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'admin_connection', None):
            with cls.admin_connection.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{cls.schema_name}" CASCADE'
                )
            cls.admin_connection.close()

    def _latest_rows(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT loja, produto, quantidade
                    FROM contagem_stock
                    WHERE data=%s AND tipo='pastelaria'
                    ORDER BY loja, id
                """, (self.COUNT_DATE,))
                return cursor.fetchall()

    def test_catalogue_rename_keeps_saved_count_on_same_product(self):
        count_date = date(2026, 9, 20)
        with patch(
            'db.pastelaria.db_connection',
            self.isolated_connection,
        ):
            grid = pastelaria.get_pastelaria_store_count_grid(count_date, 1)
            pastelaria.save_pastelaria_store_counts(
                count_date,
                1,
                [(grid['products'][0]['id'], 6)],
                grid['snapshot_token'],
            )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE produtos_pastelaria SET tipologia='Palito Renomeado' "
                    "WHERE id=1"
                )
                connection.commit()

        try:
            with patch(
                'db.pastelaria.db_connection',
                self.isolated_connection,
            ):
                renamed_grid = pastelaria.get_pastelaria_store_count_grid(
                    count_date, 1
                )
                history = pastelaria.get_contagem_stock_df(
                    'pastelaria', count_date, count_date
                )

            self.assertEqual(renamed_grid['products'][0]['nome'],
                             'Palito Renomeado')
            self.assertEqual(renamed_grid['products'][0]['count'], 6)
            self.assertEqual(history.iloc[0]['produto'], 'Palito Renomeado')
            self.assertEqual(int(history.iloc[0]['produto_pastelaria_id']), 1)
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE produtos_pastelaria SET tipologia='Palito' "
                        "WHERE id=1"
                    )
                    connection.commit()

    def test_completed_transfer_keeps_product_identity_across_rename(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO plano_producao_pastelaria (
                        data, produto, producao_real, produto_pastelaria_id
                    ) VALUES ('2026-09-14', 'Palito', 12, 1)
                """)
                cursor.execute("""
                    INSERT INTO stock_producao_pastelaria (
                        data, produto, quantidade, produto_pastelaria_id
                    ) VALUES ('2026-09-14', 'Palito', 7, 1)
                """)
                connection.commit()
        with patch('db.plano.db_connection', self.isolated_connection):
            order_id = plano.criar_ordem_transferencia(
                date(2026, 9, 14),
                'Pastelaria',
                'Palito',
                5,
                unidade='und',
                loja_destino='Bolhão',
                criado_por='producao',
            )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE produtos_pastelaria SET tipologia='Palito Renomeado' "
                    "WHERE id=1"
                )
                connection.commit()

        try:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT cs.produto, cs.produto_pastelaria_id,
                               p.tipologia
                        FROM contagem_stock cs
                        JOIN produtos_pastelaria p
                          ON p.id=cs.produto_pastelaria_id
                        WHERE cs.origem='transferencia'
                          AND cs.loja='Bolhão'
                        ORDER BY cs.id DESC
                        LIMIT 1
                    """)
                    receipt = cursor.fetchone()
            self.assertEqual(receipt, ('Palito', 1, 'Palito Renomeado'))

            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO contagem_stock (
                            data, loja, produto, quantidade, tipo, origem,
                            produto_pastelaria_id
                        ) VALUES (
                            '2099-09-15', 'Bolhão', 'Palito Renomeado', 5,
                            'pastelaria', 'contagem', 1
                        )
                    """)
                    connection.commit()

            with patch('db.area.db_connection', self.isolated_connection):
                reconciliation = area.get_reconciliacao_pastelaria(
                    date(2026, 9, 1), date(2099, 9, 30)
                )
            linked_rows = [
                row for row in reconciliation
                if row['produto'] == 'Palito Renomeado'
            ]
            self.assertEqual(len(linked_rows), 1)
            self.assertEqual(linked_rows[0]['total_produzido'], 12)
            self.assertEqual(linked_rows[0]['total_transferido'], 5)
            self.assertEqual(linked_rows[0]['stock_producao'], 7)
            self.assertEqual(linked_rows[0]['balcao_bolhao'], 5)
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM plano_producao_pastelaria")
                    cursor.execute("DELETE FROM stock_producao_pastelaria")
                    cursor.execute(
                        "UPDATE produtos_pastelaria SET tipologia='Palito' "
                        "WHERE id=1"
                    )
                    connection.commit()

    def test_pre_rename_plan_records_first_stock_under_same_product_id(self):
        with patch('db.area.db_connection', self.isolated_connection):
            area.upsert_plano_area(
                'pastelaria', date(2026, 10, 1), 'Palito', 4
            )
            area.marcar_produto_no_plano(
                'pastelaria', date(2026, 10, 1), 'Palito'
            )

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE produtos_pastelaria SET tipologia='Palito Novo' "
                    "WHERE id=1"
                )
                connection.commit()

        try:
            with patch('db.area.db_connection', self.isolated_connection):
                plan_rows = area.get_plano_do_dia_area(
                    'pastelaria', date(2026, 10, 1)
                )
                self.assertEqual(
                    [row['produto'] for row in plan_rows], ['Palito Novo']
                )
                area.update_producao_real_area(
                    'pastelaria', date(2026, 10, 1), 'Palito Novo', 4
                )
                area.upsert_stock_producao_area(
                    'pastelaria', date(2026, 10, 1), 'Palito Novo', 4
                )
                stock_rows = area.get_stock_producao_area_all('pastelaria')

            self.assertEqual(stock_rows, [{
                'produto': 'Palito Novo', 'quantidade': 4,
            }])
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT produto_pastelaria_id, COUNT(*), SUM(quantidade)
                        FROM stock_producao_pastelaria
                        WHERE data='2026-10-01'
                        GROUP BY produto_pastelaria_id
                    """)
                    self.assertEqual(cursor.fetchone(), (1, 1, 4))
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM plano_producao_pastelaria "
                        "WHERE data='2026-10-01'"
                    )
                    cursor.execute(
                        "DELETE FROM stock_producao_pastelaria "
                        "WHERE data='2026-10-01'"
                    )
                    cursor.execute(
                        "UPDATE produtos_pastelaria SET tipologia='Palito' "
                        "WHERE id=1"
                    )
                    connection.commit()

    def test_label_reuse_never_claims_unresolved_plan_or_stock(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO plano_producao_pastelaria (
                        data, produto, producao_estimada,
                        produto_pastelaria_id
                    ) VALUES ('2026-11-01', 'Palito', 9, NULL)
                """)
                cursor.execute("""
                    INSERT INTO stock_producao_pastelaria (
                        data, produto, quantidade, produto_pastelaria_id
                    ) VALUES ('2026-11-01', 'Palito', 7, NULL)
                """)
                connection.commit()

        try:
            with patch('db.area.db_connection', self.isolated_connection):
                area.upsert_plano_area(
                    'pastelaria', date(2026, 11, 1), 'Palito', 4
                )
                area.upsert_stock_producao_area(
                    'pastelaria', date(2026, 11, 1), 'Palito', 3
                )

            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT produto_pastelaria_id, producao_estimada
                        FROM plano_producao_pastelaria
                        WHERE data='2026-11-01'
                        ORDER BY produto_pastelaria_id NULLS FIRST
                    """)
                    self.assertEqual(cursor.fetchall(), [(None, 9), (1, 4)])
                    cursor.execute("""
                        SELECT produto_pastelaria_id, quantidade
                        FROM stock_producao_pastelaria
                        WHERE data='2026-11-01'
                        ORDER BY produto_pastelaria_id NULLS FIRST
                    """)
                    self.assertEqual(cursor.fetchall(), [(None, 7), (1, 3)])
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM plano_producao_pastelaria "
                        "WHERE data='2026-11-01'"
                    )
                    cursor.execute(
                        "DELETE FROM stock_producao_pastelaria "
                        "WHERE data='2026-11-01'"
                    )
                    connection.commit()

    def test_ambiguous_catalogue_label_is_rejected_by_all_stock_writers(self):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO produtos_pastelaria (tipologia) "
                    "VALUES ('Palito')"
                )
                connection.commit()
        try:
            with patch('db.area.db_connection', self.isolated_connection):
                with self.assertRaisesRegex(ValueError, 'identidade única'):
                    area.upsert_plano_area(
                        'pastelaria', date(2026, 12, 1), 'Palito', 2
                    )
            with (
                patch('db.pastelaria.db_connection', self.isolated_connection),
                patch('db.pastelaria.get_store_id_by_name', return_value=1),
            ):
                with self.assertRaisesRegex(ValueError, 'identidade única'):
                    pastelaria.add_producao_pastelaria(
                        date(2026, 12, 1), 'Bolhão', 'Palito', 2
                    )
            with patch('db.plano.db_connection', self.isolated_connection):
                with self.assertRaisesRegex(ValueError, 'identidade única'):
                    plano.criar_ordem_transferencia(
                        date(2026, 12, 1), 'Pastelaria', 'Palito', 2,
                        unidade='und', loja_destino='Bolhão',
                    )
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM produtos_pastelaria WHERE id <> 1"
                    )
                    connection.commit()

    def test_concurrent_first_plan_and_stock_writes_are_atomic(self):
        plan_barrier = Barrier(2)

        def save_plan(value):
            plan_barrier.wait(timeout=5)
            with patch('db.area.db_connection', self.isolated_connection):
                area.upsert_plano_area(
                    'pastelaria', date(2026, 12, 2), 'Palito', value
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(save_plan, (2, 3)))

        stock_barrier = Barrier(2)

        def save_stock(value):
            stock_barrier.wait(timeout=5)
            with patch('db.area.db_connection', self.isolated_connection):
                area.upsert_stock_producao_area(
                    'pastelaria', date(2026, 12, 2), 'Palito', value
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(save_stock, (2, 3)))

        try:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT COUNT(*)
                        FROM plano_producao_pastelaria
                        WHERE data='2026-12-02'
                          AND produto_pastelaria_id=1
                    """)
                    self.assertEqual(cursor.fetchone()[0], 1)
                    cursor.execute("""
                        SELECT COUNT(*), SUM(quantidade)
                        FROM stock_producao_pastelaria
                        WHERE data='2026-12-02'
                          AND produto_pastelaria_id=1
                    """)
                    self.assertEqual(cursor.fetchone(), (1, 5))
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM plano_producao_pastelaria "
                        "WHERE data='2026-12-02'"
                    )
                    cursor.execute(
                        "DELETE FROM stock_producao_pastelaria "
                        "WHERE data='2026-12-02'"
                    )
                    connection.commit()

    def test_two_stores_save_same_sunday_without_losing_values(self):
        with patch(
            'db.pastelaria.db_connection',
            self.isolated_connection,
        ):
            full_grid = pastelaria.get_pastelaria_sunday_count_grid(
                self.COUNT_DATE
            )
            bolhao_grid = pastelaria.get_pastelaria_store_count_grid(
                self.COUNT_DATE, 1
            )
            matosinhos_grid = pastelaria.get_pastelaria_store_count_grid(
                self.COUNT_DATE, 2
            )

        self.assertEqual(full_grid['snapshot_token'], bolhao_grid['snapshot_token'])
        self.assertEqual(full_grid['snapshot_token'], matosinhos_grid['snapshot_token'])
        self.assertEqual(full_grid['completed'], 0)

        # Both requests enter their transactions before either can acquire the
        # date-scoped advisory lock. The second request must then re-check its
        # own store version after the first request commits.
        ready = Barrier(2)

        @contextmanager
        def synchronized_connection():
            with self.isolated_connection() as connection:
                ready.wait(timeout=10)
                yield connection

        with patch(
            'db.pastelaria.db_connection',
            synchronized_connection,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        pastelaria.save_pastelaria_store_counts,
                        self.COUNT_DATE,
                        1,
                        [(1, 3)],
                        bolhao_grid['snapshot_token'],
                    ),
                    executor.submit(
                        pastelaria.save_pastelaria_store_counts,
                        self.COUNT_DATE,
                        2,
                        [(1, 7)],
                        matosinhos_grid['snapshot_token'],
                    ),
                ]
                self.assertEqual(
                    [future.result(timeout=15) for future in futures],
                    [1, 1],
                )

        self.assertEqual(
            self._latest_rows(),
            [
                ('Bolhão', 'Palito', 3),
                ('Matosinhos', 'Palito', 7),
            ],
        )
        with patch(
            'db.pastelaria.db_connection',
            self.isolated_connection,
        ):
            saved_grid = pastelaria.get_pastelaria_sunday_count_grid(
                self.COUNT_DATE
            )
        self.assertEqual(saved_grid['completed'], 2)
        self.assertEqual(saved_grid['products'][0]['counts'], {1: 3, 2: 7})
        self.assertNotEqual(
            saved_grid['snapshot_token'],
            full_grid['snapshot_token'],
        )

        # A full-grid request that was opened before either store saved must
        # lose the common lock's version check and must not append an overwrite.
        with patch(
            'db.pastelaria.db_connection',
            self.isolated_connection,
        ):
            with self.assertRaisesRegex(ValueError, 'outro utilizador'):
                pastelaria.save_pastelaria_sunday_counts(
                    self.COUNT_DATE,
                    [(1, 1, 99), (1, 2, 99)],
                    full_grid['snapshot_token'],
                )

        self.assertEqual(
            self._latest_rows(),
            [
                ('Bolhão', 'Palito', 3),
                ('Matosinhos', 'Palito', 7),
            ],
        )

    def test_weekday_count_is_saved_and_transfer_receipt_is_not_a_count(self):
        weekday_date = date(2099, 12, 30)
        sunday_date = date(2099, 12, 27)
        try:
            with patch(
                'db.pastelaria.db_connection',
                self.isolated_connection,
            ):
                grid = pastelaria.get_pastelaria_store_count_grid(
                    weekday_date, 1, allow_non_sunday=True,
                )
                self.assertEqual(grid['products'][0]['count'], None)
                saved = pastelaria.save_pastelaria_store_counts(
                    weekday_date, 1, [(1, 7)], grid['snapshot_token'],
                    allow_non_sunday=True,
                )
            self.assertEqual(saved, 1)

            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO contagem_stock (
                            data, loja, produto, quantidade, tipo, origem,
                            produto_pastelaria_id
                        ) VALUES (
                            %s, 'Bolhão', 'Palito', 5, 'pastelaria',
                            'transferencia', 1
                        )
                    """, (sunday_date,))
                    connection.commit()

            with patch(
                'db.pastelaria.db_connection',
                self.isolated_connection,
            ):
                sunday_grid = pastelaria.get_pastelaria_store_count_grid(
                    sunday_date, 1,
                )
                sunday_planning_grid = (
                    pastelaria.get_pastelaria_sunday_count_grid(sunday_date)
                )
                history = pastelaria.get_contagem_stock_df(
                    'pastelaria', sunday_date, weekday_date,
                )
            self.assertEqual(sunday_grid['completed'], 0)
            self.assertIsNone(sunday_grid['products'][0]['count'])
            self.assertEqual(sunday_planning_grid['completed'], 0)
            self.assertEqual(len(history), 1)
            self.assertEqual(
                str(history.iloc[0]['data'])[:10], weekday_date.isoformat(),
            )
            self.assertEqual(int(history.iloc[0]['quantidade']), 7)

            with patch('db.area.db_connection', self.isolated_connection):
                latest = area.get_ultimo_stock_balcao('pastelaria')
            self.assertEqual(
                [row for row in latest if row['loja'] == 'Bolhão'],
                [{
                    'produto': 'Palito', 'loja': 'Bolhão',
                    'quantidade': 7, 'data': weekday_date,
                }],
            )
        finally:
            with self.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM contagem_stock WHERE data IN (%s, %s)",
                        (weekday_date, sunday_date),
                    )
                    connection.commit()

    def _ensure_pastelaria_stock_schema(self):
        with patch('db.schema.db_connection', self.isolated_connection):
            schema.run_migrations_pastelaria_production_stock()

    def _new_stock_product(self):
        label = f"Produto stock {uuid.uuid4().hex[:10]}"
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO produtos_pastelaria (
                        tipologia, sabor, cobertura, ativo
                    )
                    VALUES (%s, '', '', TRUE)
                    RETURNING id
                """, (label,))
                product_id = cursor.fetchone()[0]
            connection.commit()
        self.addCleanup(self._deactivate_stock_test_product, product_id)
        return product_id, label

    def _deactivate_stock_test_product(self, product_id):
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE produtos_pastelaria
                    SET ativo = FALSE
                    WHERE id = %s
                """, (product_id,))
            connection.commit()

    def _register_stock_movement(
        self, product_id, tipo, quantity, reason, movement_date=None,
    ):
        with patch(
            'db.pastelaria_stock.db_connection',
            self.isolated_connection,
        ):
            return pastelaria_stock.register_pastelaria_stock_movement(
                identity_key=f'catalog:{product_id}',
                tipo=tipo,
                quantidade=quantity,
                data=movement_date or date(2026, 9, 24),
                responsavel='gestor-teste',
                motivo=reason,
            )

    def _create_stock_transfer(
        self, product_id, quantity, request_key=None,
        destination='Bolhão', transfer_date=None,
    ):
        request_key = request_key or str(uuid.uuid4())
        transfer_date = transfer_date or date(2026, 9, 24)
        with patch('db.plano.db_connection', self.isolated_connection):
            return plano.criar_ordens_transferencia_pastelaria(
                data=transfer_date,
                loja_destino=destination,
                lines=[{
                    'identity_key': f'catalog:{product_id}',
                    'quantidade': quantity,
                }],
                criado_por='operador-teste',
                data_prevista=transfer_date,
                request_key=request_key,
            )

    def _stock_balance(self, product_id):
        with patch(
            'db.pastelaria_stock.db_connection',
            self.isolated_connection,
        ):
            options = pastelaria_stock.get_pastelaria_stock_options()
        return next(
            item for item in options
            if item['identity_key'] == f'catalog:{product_id}'
        )

    def test_opening_production_and_correction_keep_an_audit_trail(self):
        self._ensure_pastelaria_stock_schema()
        product_id, label = self._new_stock_product()

        self._register_stock_movement(
            product_id, 'saldo_inicial', 10, 'Contagem inicial confirmada'
        )
        self._register_stock_movement(
            product_id, 'producao', 4, 'Produção do turno da manhã'
        )
        self._register_stock_movement(
            product_id, 'correcao', -2, 'Acerto após conferência'
        )
        with self.assertRaises(pastelaria_stock.PastelariaStockError):
            self._register_stock_movement(
                product_id, 'saldo_inicial', 10, 'Não pode duplicar'
            )

        state = self._stock_balance(product_id)
        self.assertTrue(state['saldo_inicial_confirmado'])
        self.assertEqual(state['saldo'], 12)
        with patch(
            'db.pastelaria_stock.db_connection',
            self.isolated_connection,
        ):
            history = pastelaria_stock.get_pastelaria_stock_movements()
        product_history = [
            row for row in history if row['produto'] == label
        ]
        self.assertEqual(
            [row['tipo'] for row in reversed(product_history)],
            ['saldo_inicial', 'producao', 'correcao'],
        )
        self.assertTrue(all(row['responsavel'] == 'gestor-teste' for row in product_history))
        self.assertEqual(
            {row['motivo'] for row in product_history},
            {
                'Contagem inicial confirmada',
                'Produção do turno da manhã',
                'Acerto após conferência',
            },
        )

    def test_insufficient_line_rolls_back_the_entire_transfer_batch(self):
        self._ensure_pastelaria_stock_schema()
        first_id, _ = self._new_stock_product()
        second_id, _ = self._new_stock_product()
        self._register_stock_movement(
            first_id, 'saldo_inicial', 10, 'Saldo confirmado'
        )
        self._register_stock_movement(
            second_id, 'saldo_inicial', 2, 'Saldo confirmado'
        )
        batch_key = str(uuid.uuid4())
        with patch('db.plano.db_connection', self.isolated_connection):
            with self.assertRaisesRegex(
                pastelaria_stock.PastelariaStockError,
                'Saldo insuficiente',
            ):
                plano.criar_ordens_transferencia_pastelaria(
                    data=date(2026, 9, 24),
                    loja_destino='Bolhão',
                    lines=[
                        {'identity_key': f'catalog:{first_id}', 'quantidade': 4},
                        {'identity_key': f'catalog:{second_id}', 'quantidade': 3},
                    ],
                    criado_por='operador-teste',
                    data_prevista=date(2026, 9, 24),
                    request_key=batch_key,
                )

        self.assertEqual(self._stock_balance(first_id)['saldo'], 10)
        self.assertEqual(self._stock_balance(second_id)['saldo'], 2)
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) FROM ordens_transferencia
                    WHERE batch_id = %s
                """, (batch_key,))
                self.assertEqual(cursor.fetchone()[0], 0)
                cursor.execute("""
                    SELECT COUNT(*) FROM pastelaria_stock_transfer_requests
                    WHERE request_key = %s
                """, (batch_key,))
                self.assertEqual(cursor.fetchone()[0], 0)

    def test_replaying_same_transfer_request_does_not_debit_twice(self):
        self._ensure_pastelaria_stock_schema()
        product_id, _ = self._new_stock_product()
        self._register_stock_movement(
            product_id, 'saldo_inicial', 10, 'Saldo confirmado'
        )
        request_key = str(uuid.uuid4())

        first = self._create_stock_transfer(product_id, 3, request_key)
        replay = self._create_stock_transfer(product_id, 3, request_key)

        self.assertFalse(first['replayed'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(first['order_ids'], replay['order_ids'])
        self.assertEqual(self._stock_balance(product_id)['saldo'], 7)
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) FROM pastelaria_stock_movements
                    WHERE ordem_transferencia_id = %s
                      AND tipo = 'transferencia_saida'
                """, (first['order_ids'][0],))
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("""
                    SELECT COUNT(*) FROM contagem_stock
                    WHERE ordem_transferencia_id = %s
                """, (first['order_ids'][0],))
                self.assertEqual(cursor.fetchone()[0], 1)

    def test_transfer_requires_an_explicit_opening_balance(self):
        self._ensure_pastelaria_stock_schema()
        product_id, _ = self._new_stock_product()
        with patch('db.plano.db_connection', self.isolated_connection):
            with self.assertRaisesRegex(
                pastelaria_stock.PastelariaStockError,
                'Saldo inicial por confirmar',
            ):
                plano.criar_ordens_transferencia_pastelaria(
                    data=date(2026, 9, 24),
                    loja_destino='Matosinhos',
                    lines=[{
                        'identity_key': f'catalog:{product_id}',
                        'quantidade': 1,
                    }],
                    criado_por='operador-teste',
                    data_prevista=date(2026, 9, 24),
                    request_key=str(uuid.uuid4()),
                )
        self.assertEqual(self._stock_balance(product_id)['saldo'], 0)
        self.assertFalse(
            self._stock_balance(product_id)['saldo_inicial_confirmado']
        )

    def test_store_acceptance_does_not_debit_or_receive_twice(self):
        self._ensure_pastelaria_stock_schema()
        product_id, _ = self._new_stock_product()
        self._register_stock_movement(
            product_id, 'saldo_inicial', 6, 'Saldo confirmado'
        )
        created = self._create_stock_transfer(product_id, 3)
        order_id = created['order_ids'][0]
        self.assertEqual(self._stock_balance(product_id)['saldo'], 3)

        with patch('db.plano.db_connection', self.isolated_connection):
            self.assertTrue(
                plano.confirmar_ordem_transferencia(order_id, 'loja-teste')
            )
            self.assertFalse(
                plano.confirmar_ordem_transferencia(order_id, 'loja-teste')
            )

        self.assertEqual(self._stock_balance(product_id)['saldo'], 3)
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) FROM pastelaria_stock_movements
                    WHERE ordem_transferencia_id = %s
                      AND tipo = 'transferencia_saida'
                """, (order_id,))
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("""
                    SELECT COUNT(*) FROM contagem_stock
                    WHERE ordem_transferencia_id = %s
                """, (order_id,))
                self.assertEqual(cursor.fetchone()[0], 1)

    def test_concurrent_transfers_for_same_product_cannot_overdraw(self):
        self._ensure_pastelaria_stock_schema()
        product_id, _ = self._new_stock_product()
        self._register_stock_movement(
            product_id, 'saldo_inicial', 5, 'Saldo confirmado'
        )
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=10)
            try:
                return plano.criar_ordens_transferencia_pastelaria(
                    data=date(2026, 9, 24),
                    loja_destino='Bolhão',
                    lines=[{
                        'identity_key': f'catalog:{product_id}',
                        'quantidade': 4,
                    }],
                    criado_por='operador-teste',
                    data_prevista=date(2026, 9, 24),
                    request_key=str(uuid.uuid4()),
                )
            except pastelaria_stock.PastelariaStockError as exc:
                return exc

        with patch('db.plano.db_connection', self.isolated_connection):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: submit(), range(2)))

        successes = [result for result in results if isinstance(result, dict)]
        failures = [
            result for result in results
            if isinstance(result, pastelaria_stock.PastelariaStockError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self._stock_balance(product_id)['saldo'], 1)

    def test_cancelling_transfer_restores_stock_only_once(self):
        self._ensure_pastelaria_stock_schema()
        product_id, _ = self._new_stock_product()
        self._register_stock_movement(
            product_id, 'saldo_inicial', 6, 'Saldo confirmado'
        )
        created = self._create_stock_transfer(product_id, 3)
        order_id = created['order_ids'][0]

        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET status = 'rejeitada',
                        motivo_rejeicao = 'Ordem anulada pelo gestor',
                        confirmado_por = 'gestor-teste'
                    WHERE id = %s
                """, (order_id,))
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET status = 'confirmada'
                    WHERE id = %s
                """, (order_id,))
                cursor.execute("""
                    UPDATE ordens_transferencia
                    SET status = 'rejeitada'
                    WHERE id = %s
                """, (order_id,))
            connection.commit()

        self.assertEqual(self._stock_balance(product_id)['saldo'], 6)
        with self.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) FROM pastelaria_stock_movements
                    WHERE ordem_transferencia_id = %s
                      AND tipo = 'transferencia_anulacao'
                """, (order_id,))
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("""
                    SELECT COUNT(*) FROM transferencias_eventos
                    WHERE ordem_id = %s AND event_type = 'anulado'
                """, (order_id,))
                self.assertEqual(cursor.fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
