import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
import pandas as pd
from datetime import datetime, date, timedelta
import os
import bcrypt
import threading
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')

_db_initialized = False
_connection_pool = None
_pool_lock = threading.Lock()

def get_pool():
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                _connection_pool = pg_pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=10,
                    dsn=DATABASE_URL,
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=5
                )
    return _connection_pool

def _is_conn_alive(conn):
    if conn is None or conn.closed:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        cur = None
        conn.rollback()
        return True
    except Exception:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        return False

def _discard_conn(conn):
    try:
        get_pool().putconn(conn, close=True)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

def get_connection():
    max_attempts = 3
    for attempt in range(max_attempts):
        conn = get_pool().getconn()
        if _is_conn_alive(conn):
            conn.autocommit = False
            return conn
        logger.warning("Stale DB connection detected, discarding (attempt %d/%d)", attempt + 1, max_attempts)
        _discard_conn(conn)
    raise psycopg2.OperationalError("Failed to obtain a healthy database connection after %d attempts" % max_attempts)

def release_connection(conn):
    if conn is None:
        return
    try:
        if conn.closed:
            get_pool().putconn(conn, close=True)
            return
        try:
            conn.rollback()
        except Exception:
            get_pool().putconn(conn, close=True)
            return
        get_pool().putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

from contextlib import contextmanager

@contextmanager
def db_connection():
    conn = get_connection()
    try:
        yield conn
    finally:
        release_connection(conn)

def ensure_initialized():
    global _db_initialized
    if not _db_initialized:
        from db.schema import init_database
        init_database()
        _db_initialized = True
    return True

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception as e:
        import logging
        logging.error(f"Password verification error: {e}")
        return False


