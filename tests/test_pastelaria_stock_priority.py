import os
import unittest
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch

import psycopg2
from flask import Flask
from db import pastelaria, schema


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
            {'loja': 'Bolhão', 'produto': 'Palito', 'quantidade': 2},
            {'loja': 'Matosinhos', 'produto': 'Palito', 'quantidade': 10},
            {'loja': 'Bolhão', 'produto': 'Bolo, Chocolate', 'quantidade': 0},
            {'loja': 'Matosinhos', 'produto': 'Bolo, Chocolate', 'quantidade': 4},
            {'loja': 'Bolhão', 'produto': 'Nivotto', 'quantidade': 9},
            {'loja': 'Matosinhos', 'produto': 'Nivotto', 'quantidade': 9},
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
            [{'loja': 'Bolhão', 'produto': 'Palito', 'quantidade': 3}],
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

    def test_permissions_separate_sales_from_management_and_production(self):
        from flask_app.routes.pastelaria import (
            _can_configure_stock_minimums,
            _can_generate_priority_plan,
            _parse_stock_minimum_matrix,
        )
        sales = {'role': 'vendasmat', 'acesso_pastelaria': True}
        self.assertFalse(_can_configure_stock_minimums(sales))
        self.assertFalse(_can_generate_priority_plan(sales))
        self.assertTrue(_can_configure_stock_minimums({'role': 'gestao'}))
        self.assertTrue(_can_generate_priority_plan({'role': 'producao'}))
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

    def test_schema_keeps_history_when_catalog_products_are_deleted(self):
        schema = Path('db/schema.py').read_text()
        self.assertIn(
            'REFERENCES produtos_pastelaria(id) ON DELETE CASCADE', schema
        )
        self.assertIn(
            'REFERENCES produtos_pastelaria(id) ON DELETE SET NULL', schema
        )

    def test_routes_reject_partial_configuration_and_sales_generation(self):
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
                data={'domingo': '2026-09-06'},
            )
        self.assertEqual(response.status_code, 403)
        generate.assert_not_called()

    def test_store_scoped_grid_only_requires_the_selected_store(self):
        cursor = _Cursor([
            [{'id': 2, 'name': 'Matosinhos'}],
            [{'id': 10, 'tipologia': 'Palito', 'sabor': '', 'cobertura': ''}],
            [{'loja': 'Matosinhos', 'produto': 'Palito', 'quantidade': 4}],
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
                'vendas_store_ids': [1],
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
                'vendas_store_ids': [1],
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

                cursor.execute('DELETE FROM produtos_pastelaria WHERE id = 1')
                cursor.execute('SELECT COUNT(*) FROM pastelaria_stock_minimos')
                self.assertEqual(cursor.fetchone()[0], 0)
                cursor.execute("""
                    SELECT produto_id, produto
                    FROM pastelaria_plano_prioridade_linhas
                    WHERE plano_id = 1
                """)
                self.assertEqual(cursor.fetchone(), (None, 'Palito'))
                cursor.execute("""
                    SELECT COUNT(*) FROM pastelaria_planos_prioridade
                    WHERE id = 1
                """)
                self.assertEqual(cursor.fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()