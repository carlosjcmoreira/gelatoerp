"""Cash Flow configuration helpers and schema migrations.

Provides the cashflow_config table used by salários, débitos diretos,
and configurações routes.  The 13-week projection engine was removed;
this module now only exposes the config read/write helpers and the
idempotent migration that creates the backing table.
"""
from datetime import date, timedelta
import logging
from psycopg2.extras import RealDictCursor
from db.connection import db_connection

logger = logging.getLogger(__name__)


def run_migrations_cashflow():
    """Idempotent migrations for cashflow_config table.
    Uses an advisory lock to serialise concurrent worker executions."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202604)")
            acquired = cursor.fetchone()[0]
            if not acquired:
                return

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cashflow_config (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            defaults = [
                ('alert_threshold_eur', '2000'),
                ('salarios_impostos_dia', '15'),
                ('salarios_liquido_dia', '28'),
                ('salarios_impostos_eur', '0'),
                ('salarios_liquido_eur', '0'),
                ('debitos_directos', '[]'),
            ]
            for k, v in defaults:
                cursor.execute("""
                    INSERT INTO cashflow_config (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                """, (k, v))

            conn.commit()
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202604)")
                conn.commit()
            except Exception:
                pass


def get_cashflow_config() -> dict:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT key, value FROM cashflow_config")
        rows = cursor.fetchall()
    return {r['key']: r['value'] for r in rows}


def set_cashflow_config(key: str, value: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO cashflow_config (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (key, value))
        conn.commit()


def get_saldo_inicial_tesouraria() -> float:
    """Return the current bank balance anchor for Tesouraria Previsional. Returns 0.0 if not set."""
    cfg = get_cashflow_config()
    try:
        return float(cfg.get('saldo_inicial_tesouraria', 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def set_saldo_inicial_tesouraria(valor: float):
    """Persist the current bank balance anchor for Tesouraria Previsional."""
    set_cashflow_config('saldo_inicial_tesouraria', str(round(valor, 2)))
