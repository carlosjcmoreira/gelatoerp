"""Regression tests for common per-request database lookups."""
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from flask import Flask

from db.cache import clear_all


def _connection(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return nullcontext(conn)


class TestSessionUserLookup(unittest.TestCase):
    def setUp(self):
        clear_all()

    def test_session_user_is_one_query_and_is_refreshed_each_request(self):
        from db.auth import get_session_user

        row = (
            7, 'user', 'gestao', 'User', True, True, True, False, False,
            False, False, None, True, False, True, False, True, [2, 4],
        )
        cursor = MagicMock()
        cursor.fetchone.side_effect = [row, row]

        with patch('db.auth.db_connection', return_value=_connection(cursor)):
            first = get_session_user('token')
            second = get_session_user('token')

        self.assertEqual(cursor.execute.call_count, 2)
        self.assertEqual(first['vendas_store_ids'], [2, 4])
        self.assertEqual(second['vendas_store_ids'], [2, 4])
        sql = cursor.execute.call_args_list[0].args[0]
        self.assertIn('LEFT JOIN user_store_vendas', sql)
        self.assertIn('array_agg', sql)


class TestTileConfigurationCaching(unittest.TestCase):
    def setUp(self):
        clear_all()

    def test_module_tile_views_share_one_database_lookup(self):
        from db.tiles import get_tile_icons, get_tile_labels, get_tile_visibility

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ('overview', 'Resumo', True, '📊'),
            ('hidden', '', False, ''),
            ('_module_icon', '', True, '🍨'),
        ]

        with patch('db.tiles.db_connection', return_value=_connection(cursor)):
            self.assertEqual(
                get_tile_visibility('producao'),
                {'overview': True, 'hidden': False, '_module_icon': True},
            )
            self.assertEqual(get_tile_labels('producao'), {'overview': 'Resumo'})
            self.assertEqual(get_tile_icons('producao'), {'overview': '📊'})

        self.assertEqual(cursor.execute.call_count, 1)

    def test_admin_write_invalidates_cached_tile_configuration(self):
        from db.tiles import get_tile_labels, set_tile_label

        first_read = MagicMock()
        first_read.fetchall.return_value = [('overview', 'Antigo', True, '')]
        write = MagicMock()
        second_read = MagicMock()
        second_read.fetchall.return_value = [('overview', 'Novo', True, '')]

        connections = [
            _connection(first_read),
            _connection(write),
            _connection(second_read),
        ]
        with patch('db.tiles.db_connection', side_effect=connections):
            self.assertEqual(get_tile_labels('producao')['overview'], 'Antigo')
            set_tile_label('producao', 'overview', 'Novo')
            self.assertEqual(get_tile_labels('producao')['overview'], 'Novo')

        self.assertEqual(first_read.execute.call_count, 1)
        self.assertEqual(write.execute.call_count, 1)
        self.assertEqual(second_read.execute.call_count, 1)

    def test_generation_change_evicts_a_stale_worker_entry(self):
        from db.cache import _bump_generation, _prefix_scope
        from db.tiles import get_tile_labels

        first_read = MagicMock()
        first_read.fetchall.return_value = [('overview', 'Worker A', True, '')]
        second_read = MagicMock()
        second_read.fetchall.return_value = [('overview', 'Worker B', True, '')]

        with patch(
            'db.tiles.db_connection',
            side_effect=[_connection(first_read), _connection(second_read)],
        ):
            self.assertEqual(get_tile_labels('producao')['overview'], 'Worker A')
            # Simulate another worker publishing an admin write without touching
            # this worker's in-memory store. The shared generation alone must
            # make the warmed local entry stale.
            _bump_generation(_prefix_scope('tile_config:'))
            self.assertEqual(get_tile_labels('producao')['overview'], 'Worker B')

        self.assertEqual(first_read.execute.call_count, 1)
        self.assertEqual(second_read.execute.call_count, 1)


class TestInvoiceStatusCaching(unittest.TestCase):
    def setUp(self):
        clear_all()

    def test_status_template_globals_share_one_database_lookup(self):
        from db.faturas import (
            get_invoice_status_bulk_allowed,
            get_invoice_status_colors_map,
            get_invoice_status_labels_map,
        )

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            {
                'key': 'pending_review',
                'label': 'Por rever',
                'bg_class': 'bg-warning',
                'sort_order': 1,
                'active': True,
            },
            {
                'key': 'draft',
                'label': 'Rascunho',
                'bg_class': 'bg-secondary',
                'sort_order': 2,
                'active': True,
            },
        ]

        with patch('db.faturas.db_connection', return_value=_connection(cursor)):
            self.assertEqual(
                get_invoice_status_labels_map(),
                {'pending_review': 'Por rever', 'draft': 'Rascunho'},
            )
            self.assertEqual(
                get_invoice_status_colors_map(),
                {'pending_review': 'bg-warning', 'draft': 'bg-secondary'},
            )
            self.assertEqual(
                get_invoice_status_bulk_allowed(),
                ['pending_review'],
            )

        self.assertEqual(cursor.execute.call_count, 1)

    def test_status_write_invalidates_source_and_derived_caches(self):
        from db.faturas import (
            get_invoice_status_labels_map,
            upsert_invoice_status_config,
        )

        first_read = MagicMock()
        first_read.fetchall.return_value = [{
            'key': 'paid',
            'label': 'Paga',
            'bg_class': 'bg-success',
            'sort_order': 1,
            'active': True,
        }]
        write = MagicMock()
        second_read = MagicMock()
        second_read.fetchall.return_value = [{
            'key': 'paid',
            'label': 'Liquidada',
            'bg_class': 'bg-success',
            'sort_order': 1,
            'active': True,
        }]

        with patch(
            'db.faturas.db_connection',
            side_effect=[
                _connection(first_read),
                _connection(write),
                _connection(second_read),
            ],
        ):
            self.assertEqual(get_invoice_status_labels_map()['paid'], 'Paga')
            upsert_invoice_status_config('paid', 'Liquidada', 'bg-success', 1, True)
            self.assertEqual(get_invoice_status_labels_map()['paid'], 'Liquidada')

        self.assertEqual(first_read.execute.call_count, 1)
        self.assertEqual(write.execute.call_count, 1)
        self.assertEqual(second_read.execute.call_count, 1)


class TestNavigationRequestCache(unittest.TestCase):
    def setUp(self):
        clear_all()

    def test_navigation_is_computed_once_during_a_request(self):
        from flask_app.services.navigation import compute_nav_pages

        app = Flask(__name__)
        user = {'id': 3, 'acesso_producao': True, 'vendas_store_ids': []}

        with app.test_request_context('/'), \
             patch('flask_app.services.navigation.url_for',
                   side_effect=lambda endpoint, **kwargs: f'/{endpoint}'), \
             patch('db.tiles.get_module_labels', return_value={}) as labels, \
             patch('db.tiles.get_module_icons', return_value={}) as icons, \
             patch('flask_app.services.navigation.db.get_vendas_module_stores',
                   return_value=[]) as vendas:
            first = compute_nav_pages(user)
            second = compute_nav_pages(user)

        self.assertIs(first, second)
        labels.assert_called_once_with()
        icons.assert_called_once_with()
        vendas.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()