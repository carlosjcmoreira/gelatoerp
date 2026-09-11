import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from db import pastelaria


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
                'flask_app.routes.pastelaria.get_produtos_pastelaria',
                return_value=['Palito'],
            ),
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value=config,
            ),
            patch(
                'flask_app.routes.pastelaria.generate_pastelaria_priority_plan'
            ) as generate,
        ):
            response = client.post(
                '/pastelaria/stock-balcao',
                data={'action': 'gerar_plano', 'data_plano': '2026-09-06'},
            )
        self.assertEqual(response.status_code, 403)
        generate.assert_not_called()

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


if __name__ == '__main__':
    unittest.main()