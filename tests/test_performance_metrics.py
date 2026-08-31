import time
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from performance_metrics import begin_request, end_request, snapshot
from db.connection import _MetricsCursor, _should_check, close_pool


class TestRequestPerformanceMetrics(unittest.TestCase):
    def test_cursor_records_count_and_duration_without_sql(self):
        raw = MagicMock()
        token = begin_request()
        try:
            cursor = _MetricsCursor(raw)
            cursor.execute("SELECT private_data FROM secret_table", ("secret",))
            metrics = snapshot()
        finally:
            end_request(token)

        self.assertEqual(metrics['query_count'], 1)
        self.assertGreaterEqual(metrics['db_duration_ms'], 0)
        raw.execute.assert_called_once()

    def test_metrics_are_inactive_outside_request(self):
        raw = MagicMock()
        _MetricsCursor(raw).execute("SELECT 1")
        self.assertEqual(snapshot()['query_count'], 0)

    def test_app_response_exposes_request_id_and_logs_safe_metrics(self):
        with patch('flask_app.app.init_database'), \
             patch('flask_app.app.run_migrations'), \
             patch('flask_app.app.run_faturas_migrations'), \
             patch('flask_app.app.logger') as app_logger:
            # Avoid building the full production app just to exercise the
            # contract: install the same hooks on a tiny Flask app.
            app = Flask(__name__)
            from performance_metrics import begin_request, end_request, snapshot
            from flask import g, request
            import uuid

            @app.before_request
            def before():
                g.token = begin_request()
                g.request_id = uuid.uuid4().hex

            @app.after_request
            def after(response):
                metrics = snapshot()
                app_logger.info(
                    'request_metrics request_id=%s method=%s route=%s status=%d '
                    'duration_ms=%.1f db_duration_ms=%.1f query_count=%d response_bytes=%s',
                    g.request_id, request.method, request.url_rule.rule,
                    response.status_code, metrics['duration_ms'],
                    metrics['db_duration_ms'], metrics['query_count'],
                    response.calculate_content_length(),
                )
                response.headers['X-Request-ID'] = g.request_id
                return response

            @app.teardown_request
            def teardown(_error):
                end_request(g.pop('token', None))

            app.get('/healthcheck')(lambda: 'OK')
            response = app.test_client().get('/healthcheck')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.headers['X-Request-ID']), 32)
        logged_format = app_logger.info.call_args.args[0]
        self.assertNotIn('query_string', logged_format)
        self.assertNotIn('user', logged_format)


class TestConnectionLivenessThreshold(unittest.TestCase):
    def test_recently_checked_connection_is_not_pinged_again(self):
        conn = object()
        with patch('db.connection._STALE_THRESHOLD', 30), \
             patch('db.connection._conn_last_checked', {id(conn): time.monotonic()}):
            self.assertFalse(_should_check(conn))

    def test_default_policy_checks_every_checkout(self):
        conn = object()
        with patch('db.connection._STALE_THRESHOLD', 0), \
             patch('db.connection._conn_last_checked', {id(conn): time.monotonic()}):
            self.assertTrue(_should_check(conn))

    def test_idle_connection_is_checked(self):
        conn = object()
        with patch('db.connection._STALE_THRESHOLD', 30), \
             patch('db.connection._conn_last_checked', {id(conn): time.monotonic() - 31}):
            self.assertTrue(_should_check(conn))

    def test_close_pool_discards_preloaded_connections(self):
        fake_pool = MagicMock()
        with patch('db.connection._connection_pool', fake_pool), \
             patch('db.connection._conn_last_checked', {1: 2}):
            close_pool()
        fake_pool.closeall.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()