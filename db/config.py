import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache_args, invalidate_prefix

from db.schema import run_faturas_migrations  # noqa: F401
from db.stores import *  # noqa: F401,F403
from db.artigos import *  # noqa: F401,F403


@ttl_cache_args('system_config', ttl=300)
def get_system_config(key: str, default=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_config WHERE key = %s", (key,))
        row = cursor.fetchone()
    return row[0] if row else default


def set_system_config(key: str, value):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO system_config (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        ''', (key, value))
        conn.commit()
        invalidate_prefix('system_config')
