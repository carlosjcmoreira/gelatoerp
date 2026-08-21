"""
Regression tests for invoice list state across bulk edits and Compras filters.

Run with:
    python -m unittest tests.test_invoice_list_state -v
"""

import unittest
import os
import uuid
from unittest.mock import MagicMock, patch

from flask import Blueprint, Flask


def _user(**overrides):
    return {
        'id': 1,
        'username': 'testuser',
        'role': 'gestor',
        'acesso_gestor': True,
        'acesso_administrativo': False,
        'acesso_compras': True,
        **overrides,
    }


def _support_blueprints(app):
    auth_bp = Blueprint('auth', __name__)

    @auth_bp.route('/login')
    def login():
        return 'login', 200

    home_bp = Blueprint('home', __name__)

    @home_bp.route('/')
    def index():
        return 'home', 200

    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)


class TestBulkActionReturnUrl(unittest.TestCase):
    """Bulk edits must return to the exact list URL that initiated them."""

    @classmethod
    def setUpClass(cls):
        from flask_app.routes.faturas import faturas_bp

        cls.app = Flask(__name__)
        cls.app.secret_key = 'test-secret-key'
        cls.app.config['TESTING'] = True
        _support_blueprints(cls.app)
        cls.app.register_blueprint(faturas_bp, url_prefix='/financeiro/faturas')

    def test_assigning_cost_center_keeps_all_list_parameters(self):
        return_url = (
            '/financeiro/faturas/?sem_cc=1&centro_custo_id=8'
            '&categoria_custo_id=4&order_by=centro_custo_name'
            '&order_dir=asc&page=3&q=farinha'
        )
        with patch('flask_app.routes.faturas.update_invoice') as update:
            client = self.app.test_client()
            with client.session_transaction() as session:
                session['user'] = _user()

            response = client.post('/financeiro/faturas/bulk', data={
                'ids': ['101', '102'],
                'action': 'assign_field',
                'field': 'centro_custo_id',
                'value': '8',
                'return_url': return_url,
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], return_url)
        self.assertEqual(update.call_count, 2)

    def test_assigning_from_compras_returns_to_compras_list(self):
        return_url = (
            '/compras/faturas?sem_cc=1&categoria_custo_id=4'
            '&order_by=categoria_custo_name&order_dir=desc&page=2'
        )
        with patch('flask_app.routes.faturas.update_invoice'):
            client = self.app.test_client()
            with client.session_transaction() as session:
                session['user'] = _user()

            response = client.post('/financeiro/faturas/bulk', data={
                'ids': ['103'],
                'action': 'assign_field',
                'field': 'categoria_custo_id',
                'value': '4',
                'return_url': return_url,
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], return_url)

    def test_template_uses_relative_request_path_for_bulk_return_urls(self):
        with open('flask_app/templates/financeiro/faturas/index.html', encoding='utf-8') as template:
            source = template.read()

        self.assertNotIn('name="return_url" value="{{ request.url }}"', source)
        self.assertEqual(source.count('name="return_url" value="{{ request.full_path }}"'), 4)


class TestDocumentListStickyReferences(unittest.TestCase):
    """The document view keeps its controls and column labels usable in long lists."""

    @classmethod
    def setUpClass(cls):
        with open('flask_app/templates/financeiro/faturas/index.html', encoding='utf-8') as template:
            cls.source = template.read()

    def test_bulk_action_bar_is_sticky_and_accessible(self):
        self.assertIn('id="bulk-bar" class="document-list-actions d-none', self.source)
        self.assertIn('role="region" aria-label="Ações em documentos selecionados"', self.source)
        self.assertIn('id="bulk-count" role="status" aria-live="polite"', self.source)
        self.assertIn('.document-list-actions {\n    position: sticky;', self.source)
        self.assertIn('top: calc(56px + .5rem);', self.source)

    def test_document_table_uses_an_independent_scroll_area_with_sticky_headers(self):
        self.assertIn(
            'class="document-table-scroll table-responsive" role="region" aria-label="Lista de documentos" tabindex="0"',
            self.source,
        )
        self.assertIn('.document-table-scroll {\n    max-height:', self.source)
        self.assertIn('overflow: auto;', self.source)
        self.assertIn('.document-table-scroll #inv-table thead th {\n    position: sticky;', self.source)
        self.assertIn('top: 0;', self.source)

    def test_sticky_references_are_scoped_to_document_view(self):
        self.assertEqual(self.source.count('document-list-actions'), 3)
        self.assertEqual(self.source.count('class="document-table-scroll table-responsive"'), 1)
        self.assertIn('{% if invoices %}', self.source)


class _RecordingCursor:
    def __init__(self, fetchone_results, update_counts=()):
        self._fetchone_results = iter(fetchone_results)
        self._update_counts = iter(update_counts)
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.lstrip().startswith('UPDATE invoices'):
            self.rowcount = next(self._update_counts)

    def fetchone(self):
        return next(self._fetchone_results)


class _AuditRecordingCursor(_RecordingCursor):
    """Recording cursor that returns IDs from UPDATE ... RETURNING queries."""

    def __init__(self, fetchone_results, changed_invoice_ids, update_counts=()):
        super().__init__(fetchone_results, update_counts=update_counts)
        self._changed_invoice_ids = iter(changed_invoice_ids)

    def fetchall(self):
        return next(self._changed_invoice_ids)


class _RecordingConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


class TestSupplierClassificationPropagationData(unittest.TestCase):
    supplier_config = (17, 'Fornecedor de teste', 8, 8, 'Produção', True, 4, 4, 'Ingredientes', True)

    def test_preview_counts_only_empty_primary_classifications(self):
        cursor = _RecordingCursor([self.supplier_config, (3, 2)])
        connection = _RecordingConnection(cursor)
        with patch('db.faturas.db_connection', return_value=connection):
            from db.faturas import get_supplier_invoice_classification_preview
            preview = get_supplier_invoice_classification_preview(17)

        self.assertEqual(preview['fields']['centro_custo']['eligible_count'], 3)
        self.assertEqual(preview['fields']['categoria_custo']['eligible_count'], 2)
        count_sql = cursor.calls[1][0]
        self.assertIn("i.centro_custo_id IS NULL", count_sql)
        self.assertIn("FROM invoice_centros_custo icc", count_sql)
        self.assertIn("i.categoria_custo_id IS NULL", count_sql)
        self.assertIn("i.status != 'draft'", count_sql)

    def test_apply_never_overwrites_or_replaces_split_cost_centers(self):
        cursor = _RecordingCursor([self.supplier_config], update_counts=(3, 2))
        connection = _RecordingConnection(cursor)
        with patch('db.faturas.db_connection', return_value=connection):
            from db.faturas import apply_supplier_invoice_classifications
            result = apply_supplier_invoice_classifications(17)

        self.assertEqual(result['centro_custo']['updated_count'], 3)
        self.assertEqual(result['categoria_custo']['updated_count'], 2)
        self.assertTrue(connection.committed)
        centro_update, categoria_update = cursor.calls[1][0], cursor.calls[2][0]
        self.assertIn("i.centro_custo_id IS NULL", centro_update)
        self.assertIn("FROM invoice_centros_custo icc", centro_update)
        self.assertIn("i.categoria_custo_id IS NULL", categoria_update)
        self.assertIn("i.supplier_id = %s", centro_update)
        self.assertIn("i.status != 'draft'", categoria_update)

    def test_repeat_is_idempotent_when_no_empty_fields_remain(self):
        cursor = _RecordingCursor([self.supplier_config], update_counts=(0, 0))
        with patch('db.faturas.db_connection', return_value=_RecordingConnection(cursor)):
            from db.faturas import apply_supplier_invoice_classifications
            result = apply_supplier_invoice_classifications(17)

        self.assertEqual(result['centro_custo']['updated_count'], 0)
        self.assertEqual(result['categoria_custo']['updated_count'], 0)

    def test_apply_records_actor_supplier_and_field_for_each_changed_invoice(self):
        cursor = _AuditRecordingCursor(
            [self.supplier_config],
            changed_invoice_ids=[[(101,), (102,)], [(103,)]],
            update_counts=(2, 1),
        )
        with patch('db.faturas.db_connection', return_value=_RecordingConnection(cursor)):
            from db.faturas import apply_supplier_invoice_classifications
            apply_supplier_invoice_classifications(17, changed_by='gestora')

        audit_calls = [call for call in cursor.calls if call[0].lstrip().startswith('INSERT INTO invoice_audit_log')]
        self.assertEqual(len(audit_calls), 3)
        self.assertEqual(
            audit_calls[0][1],
            (101, 'centro_custo_id',
             'Produção — configuração do fornecedor: Fornecedor de teste', 'gestora'),
        )
        self.assertEqual(
            audit_calls[2][1],
            (103, 'categoria_custo_id',
             'Ingredientes — configuração do fornecedor: Fornecedor de teste', 'gestora'),
        )

    def test_apply_creates_no_audit_entries_when_no_invoice_is_changed(self):
        cursor = _AuditRecordingCursor(
            [self.supplier_config],
            changed_invoice_ids=[[], []],
            update_counts=(0, 0),
        )
        with patch('db.faturas.db_connection', return_value=_RecordingConnection(cursor)):
            from db.faturas import apply_supplier_invoice_classifications
            apply_supplier_invoice_classifications(17, changed_by='gestora')

        audit_calls = [call for call in cursor.calls if call[0].lstrip().startswith('INSERT INTO invoice_audit_log')]
        self.assertEqual(audit_calls, [])

    def test_supplier_without_configuration_changes_nothing(self):
        no_config = (17, 'Fornecedor de teste', None, None, None, None, None, None, None, None)
        cursor = _RecordingCursor([no_config])
        connection = _RecordingConnection(cursor)
        with patch('db.faturas.db_connection', return_value=connection):
            from db.faturas import apply_supplier_invoice_classifications
            result = apply_supplier_invoice_classifications(17)

        self.assertFalse(result['centro_custo']['configured'])
        self.assertFalse(result['categoria_custo']['configured'])
        self.assertTrue(connection.committed)
        self.assertEqual(len(cursor.calls), 1)

    def test_invalid_supplier_reference_is_rejected_before_updates(self):
        invalid_config = (17, 'Fornecedor de teste', 8, 8, 'Produção', False, 4, 4, 'Ingredientes', True)
        cursor = _RecordingCursor([invalid_config])
        with patch('db.faturas.db_connection', return_value=_RecordingConnection(cursor)):
            from db.faturas import apply_supplier_invoice_classifications
            with self.assertRaisesRegex(ValueError, 'inativo'):
                apply_supplier_invoice_classifications(17)

        self.assertEqual(len(cursor.calls), 1)


class TestSupplierClassificationPropagationPostgres(unittest.TestCase):
    """Verify supplier classification updates and their audit rows in PostgreSQL."""

    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            raise unittest.SkipTest('DATABASE_URL not available')

        try:
            import psycopg2
            cls.connection = psycopg2.connect(database_url)
        except Exception as exc:
            raise unittest.SkipTest(f'PostgreSQL test database unavailable: {exc}')

        with cls.connection.cursor() as cursor:
            cursor.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
            """, ([
                'suppliers',
                'invoices',
                'cost_centers',
                'cost_categories',
                'invoice_audit_log',
                'invoice_centros_custo',
            ],))
            existing_tables = {row[0] for row in cursor.fetchall()}

        required_tables = {
            'suppliers',
            'invoices',
            'cost_centers',
            'cost_categories',
            'invoice_audit_log',
            'invoice_centros_custo',
        }
        missing_tables = required_tables - existing_tables
        if missing_tables:
            cls.connection.close()
            raise unittest.SkipTest(
                'PostgreSQL schema is missing: ' + ', '.join(sorted(missing_tables))
            )

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'connection', None) and not cls.connection.closed:
            cls.connection.close()

    def setUp(self):
        suffix = uuid.uuid4().hex[:6]
        self.suffix = suffix
        self.supplier_name = f'Fornecedor integração {suffix}'
        self.supplier_nif = f'T750{suffix}'
        self.actor = f'test-task-750-{suffix}'
        self.invoice_ids = []
        self.cost_center_ids = []
        self.category_ids = []

        with self.connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO cost_centers (code, name)
                VALUES (%s, %s)
                RETURNING id
            """, (f'T7{suffix}', f'Centro integração {suffix}'))
            target_centro_id = cursor.fetchone()[0]
            self.cost_center_ids.append(target_centro_id)

            cursor.execute("""
                INSERT INTO cost_centers (code, name)
                VALUES (%s, %s)
                RETURNING id
            """, (f'P7{suffix}', f'Centro prévio {suffix}'))
            existing_centro_id = cursor.fetchone()[0]
            self.cost_center_ids.append(existing_centro_id)

            cursor.execute("""
                INSERT INTO cost_categories (name)
                VALUES (%s)
                RETURNING id
            """, (f'Categoria integração {suffix}',))
            target_categoria_id = cursor.fetchone()[0]
            self.category_ids.append(target_categoria_id)

            cursor.execute("""
                INSERT INTO cost_categories (name)
                VALUES (%s)
                RETURNING id
            """, (f'Categoria prévia {suffix}',))
            existing_categoria_id = cursor.fetchone()[0]
            self.category_ids.append(existing_categoria_id)

            cursor.execute("""
                INSERT INTO suppliers (name, nif, centro_custo_id, categoria_custo_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (
                self.supplier_name,
                self.supplier_nif,
                target_centro_id,
                target_categoria_id,
            ))
            self.supplier_id = cursor.fetchone()[0]

            def add_invoice(number, centro_id=None, categoria_id=None):
                cursor.execute("""
                    INSERT INTO invoices (
                        supplier_id, supplier_name, supplier_nif, invoice_number,
                        status, centro_custo_id, categoria_custo_id
                    )
                    VALUES (%s, %s, %s, %s, 'pending_review', %s, %s)
                    RETURNING id
                """, (
                    self.supplier_id,
                    self.supplier_name,
                    self.supplier_nif,
                    f'{number}-{suffix}',
                    centro_id,
                    categoria_id,
                ))
                invoice_id = cursor.fetchone()[0]
                self.invoice_ids.append(invoice_id)
                return invoice_id

            # Both configured fields are empty, so both should be propagated.
            self.invoice_both = add_invoice('both')
            # Each of these proves that the fields are applied independently.
            self.invoice_centro_only = add_invoice(
                'centro-only',
                categoria_id=existing_categoria_id,
            )
            self.invoice_categoria_only = add_invoice(
                'categoria-only',
                centro_id=existing_centro_id,
            )
            # Existing classifications must not create audit entries.
            self.invoice_already_classified = add_invoice(
                'already-classified',
                centro_id=existing_centro_id,
                categoria_id=existing_categoria_id,
            )
            # A split-centre allocation must not be replaced by a primary centre.
            self.invoice_split = add_invoice('split-centre', categoria_id=existing_categoria_id)
            cursor.execute("""
                INSERT INTO invoice_centros_custo (invoice_id, centro_custo_id, percentagem)
                VALUES (%s, %s, 100.0)
            """, (self.invoice_split, existing_centro_id))

        self.connection.commit()

    def tearDown(self):
        self.connection.rollback()
        with self.connection.cursor() as cursor:
            for invoice_id in self.invoice_ids:
                cursor.execute('DELETE FROM invoices WHERE id = %s', (invoice_id,))
            cursor.execute('DELETE FROM suppliers WHERE id = %s', (self.supplier_id,))
            for category_id in self.category_ids:
                cursor.execute('DELETE FROM cost_categories WHERE id = %s', (category_id,))
            for centro_id in self.cost_center_ids:
                cursor.execute('DELETE FROM cost_centers WHERE id = %s', (centro_id,))
        self.connection.commit()

    def test_applies_both_classifications_and_audits_only_changed_invoices(self):
        from db.faturas import apply_supplier_invoice_classifications

        result = apply_supplier_invoice_classifications(
            self.supplier_id,
            changed_by=self.actor,
        )

        self.assertEqual(result['centro_custo']['updated_count'], 2)
        self.assertEqual(result['categoria_custo']['updated_count'], 2)

        with self.connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, centro_custo_id, categoria_custo_id
                FROM invoices
                WHERE id = ANY(%s)
            """, (self.invoice_ids,))
            classifications = {
                row[0]: (row[1], row[2])
                for row in cursor.fetchall()
            }

            cursor.execute("""
                SELECT invoice_id, campo_alterado, valor_novo, alterado_por
                FROM invoice_audit_log
                WHERE invoice_id = ANY(%s)
                ORDER BY invoice_id, campo_alterado
            """, (self.invoice_ids,))
            audit_rows = cursor.fetchall()

        target_centro_id = self.cost_center_ids[0]
        target_categoria_id = self.category_ids[0]
        existing_centro_id = self.cost_center_ids[1]
        existing_categoria_id = self.category_ids[1]

        self.assertEqual(
            classifications[self.invoice_both],
            (target_centro_id, target_categoria_id),
        )
        self.assertEqual(
            classifications[self.invoice_centro_only],
            (target_centro_id, existing_categoria_id),
        )
        self.assertEqual(
            classifications[self.invoice_categoria_only],
            (existing_centro_id, target_categoria_id),
        )
        self.assertEqual(
            classifications[self.invoice_already_classified],
            (existing_centro_id, existing_categoria_id),
        )
        self.assertEqual(
            classifications[self.invoice_split],
            (None, existing_categoria_id),
        )

        audit_by_invoice = {}
        for invoice_id, field_name, source, actor in audit_rows:
            audit_by_invoice.setdefault(invoice_id, []).append(
                (field_name, source, actor)
            )

        expected_source = {
            'centro_custo_id': (
                f'Centro integração {self.suffix} — configuração do fornecedor: '
                f'{self.supplier_name}'
            ),
            'categoria_custo_id': (
                f'Categoria integração {self.suffix} — configuração do fornecedor: '
                f'{self.supplier_name}'
            ),
        }
        self.assertEqual(
            {
                row[0]: (row[1], row[2])
                for row in audit_by_invoice[self.invoice_both]
            },
            {
                'centro_custo_id': (
                    expected_source['centro_custo_id'],
                    self.actor,
                ),
                'categoria_custo_id': (
                    expected_source['categoria_custo_id'],
                    self.actor,
                ),
            },
        )
        self.assertEqual(
            audit_by_invoice[self.invoice_centro_only],
            [('centro_custo_id', expected_source['centro_custo_id'], self.actor)],
        )
        self.assertEqual(
            audit_by_invoice[self.invoice_categoria_only],
            [('categoria_custo_id', expected_source['categoria_custo_id'], self.actor)],
        )
        self.assertNotIn(self.invoice_already_classified, audit_by_invoice)
        self.assertNotIn(self.invoice_split, audit_by_invoice)


class TestSupplierClassificationPropagationRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flask_app.routes.faturas import faturas_bp

        cls.app = Flask(__name__)
        cls.app.secret_key = 'test-secret-key'
        cls.app.config['TESTING'] = True
        _support_blueprints(cls.app)
        cls.app.register_blueprint(faturas_bp, url_prefix='/financeiro/faturas')

    def _client(self, **user_overrides):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user'] = _user(**user_overrides)
        return client

    def test_preview_is_available_to_manager_and_returns_counts(self):
        preview = {
            'supplier': {'id': 17, 'name': 'Fornecedor de teste'},
            'fields': {
                'centro_custo': {'configured': True, 'valid': True, 'eligible_count': 3},
                'categoria_custo': {'configured': True, 'valid': True, 'eligible_count': 2},
            },
        }
        with patch('flask_app.routes.faturas.get_supplier_invoice_classification_preview', return_value=preview):
            response = self._client().post('/financeiro/faturas/fornecedores/17/classificacoes/preview')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        self.assertEqual(response.get_json()['preview']['fields']['centro_custo']['eligible_count'], 3)

    def test_propagation_is_denied_without_manager_permission(self):
        with patch('flask_app.routes.faturas.apply_supplier_invoice_classifications') as apply:
            response = self._client(acesso_gestor=False, acesso_administrativo=False).post(
                '/financeiro/faturas/fornecedores/17/propagar-classificacoes'
            )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.get_json()['ok'])
        apply.assert_not_called()

    def test_confirmation_returns_per_field_result(self):
        result = {
            'supplier': {'id': 17, 'name': 'Fornecedor de teste'},
            'centro_custo': {'configured': True, 'name': 'Produção', 'updated_count': 3},
            'categoria_custo': {'configured': True, 'name': 'Ingredientes', 'updated_count': 2},
        }
        with patch('flask_app.routes.faturas.apply_supplier_invoice_classifications', return_value=result) as apply:
            response = self._client().post('/financeiro/faturas/fornecedores/17/propagar-classificacoes')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['result']['categoria_custo']['updated_count'], 2)
        apply.assert_called_once_with(17, changed_by='testuser')

    def test_template_explains_preview_and_confirmation_safety(self):
        with open('flask_app/templates/financeiro/faturas/fornecedores.html', encoding='utf-8') as template:
            source = template.read()

        self.assertIn('id="modalSupplierClassifications"', source)
        self.assertIn('Apenas são preenchidos campos vazios.', source)
        self.assertIn('Faturas já classificadas, repartições por vários centros', source)
        self.assertIn('Corrija a configuração inválida do fornecedor', source)


class TestComprasFilterState(unittest.TestCase):
    """Compras must forward and remember the same cost filters as Financeiro."""

    @classmethod
    def setUpClass(cls):
        from flask_app.routes.compras import compras_bp

        cls.app = Flask(__name__)
        cls.app.secret_key = 'test-secret-key'
        cls.app.config['TESTING'] = True
        _support_blueprints(cls.app)
        cls.app.register_blueprint(compras_bp, url_prefix='/compras')

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = _user()

        self.patches = [
            patch('flask_app.routes.compras.count_invoices', return_value=0),
            patch('flask_app.routes.compras.get_invoices', return_value=[]),
            patch('flask_app.routes.compras.get_payment_methods_config', return_value=[]),
            patch('flask_app.routes.compras.get_distinct_supplier_names', return_value=[]),
            patch('flask_app.routes.compras.get_stores_list', return_value=[]),
            patch('flask_app.routes.compras.get_cost_centers', return_value=[]),
            patch('flask_app.routes.compras.get_cost_categories_tree', return_value=[]),
            patch('db.centros_custo.get_cost_categories', return_value=[]),
            patch('flask_app.routes.compras.render_template', return_value='ok'),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_cost_filters_and_sort_are_forwarded_and_saved(self):
        from flask_app.routes import compras as compras_route

        response = self.client.get(
            '/compras/faturas?sem_cc=1&centro_custo_id=8&categoria_custo_id=4'
            '&order_by=centro_custo_name&order_dir=asc'
        )

        self.assertEqual(response.status_code, 200)
        invoice_kwargs = compras_route.get_invoices.call_args.kwargs
        self.assertTrue(invoice_kwargs['sem_cc'])
        self.assertEqual(invoice_kwargs['centro_custo_id'], 8)
        self.assertEqual(invoice_kwargs['categoria_custo_id'], 4)
        self.assertEqual(invoice_kwargs['order_by'], 'centro_custo_name')
        self.assertEqual(invoice_kwargs['order_dir'], 'asc')

        template_kwargs = compras_route.render_template.call_args.kwargs
        self.assertTrue(template_kwargs['sem_cc_filter'])
        self.assertEqual(template_kwargs['centro_custo_filter'], 8)
        self.assertEqual(template_kwargs['categoria_custo_filter'], 4)

        with self.client.session_transaction() as session:
            self.assertEqual(session['faturas_filters']['sem_cc'], '1')
            self.assertEqual(session['faturas_filters']['centro_custo_id'], 8)
            self.assertEqual(session['faturas_filters']['categoria_custo_id'], 4)
            self.assertEqual(session['faturas_filters']['order_by'], 'centro_custo_name')
            self.assertEqual(session['faturas_filters']['order_dir'], 'asc')

    def test_clearing_a_cost_filter_does_not_restore_it_from_session(self):
        with self.client.session_transaction() as session:
            session['faturas_filters'] = {
                'sem_cc': '1',
                'centro_custo_id': 8,
                'categoria_custo_id': 4,
            }

        response = self.client.get(
            '/compras/faturas?sem_cc=0&clear_filter=centro_custo_id'
            '&clear_filter=categoria_custo_id'
        )

        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session['faturas_filters'], {})


if __name__ == '__main__':
    unittest.main()