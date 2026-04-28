import time
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

# Per-connection last-checked timestamp (keyed by id(conn)).
# With _STALE_THRESHOLD = 0.0 every connection gets a SELECT 1 liveness ping
# before being handed to the caller, eliminating stale-connection 500 errors
# after deploys and DB-server restarts at the cost of ~1 ms per request.
_conn_last_checked: dict = {}
_check_lock = threading.Lock()
_STALE_THRESHOLD = 0.0  # always check liveness on acquisition


def get_pool():
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                _connection_pool = pg_pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=15,
                    dsn=DATABASE_URL,
                    connect_timeout=5,
                    keepalives=1,
                    keepalives_idle=10,
                    keepalives_interval=2,
                    keepalives_count=3,
                )
    return _connection_pool


def _should_check(conn) -> bool:
    """Return True if the connection should receive a liveness ping.

    With _STALE_THRESHOLD = 0.0 this always returns True so every connection
    gets a SELECT 1 check before use (eliminates stale-connection 500s).
    """
    now = time.monotonic()
    with _check_lock:
        last = _conn_last_checked.get(id(conn), 0)
    return (now - last) >= _STALE_THRESHOLD


def _mark_checked(conn) -> None:
    with _check_lock:
        _conn_last_checked[id(conn)] = time.monotonic()


def _clear_checked(conn) -> None:
    with _check_lock:
        _conn_last_checked.pop(id(conn), None)


def _is_conn_alive(conn) -> bool:
    if conn is None or conn.closed:
        return False
    cur = None
    try:
        cur = conn.cursor()
        # SET LOCAL scopes the timeout to this transaction only;
        # conn.rollback() below resets it, so no impact on real queries.
        cur.execute("SET LOCAL statement_timeout = 4000")
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
    _clear_checked(conn)
    try:
        get_pool().putconn(conn, close=True)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def get_connection():
    max_attempts = 5
    for attempt in range(max_attempts):
        conn = get_pool().getconn()
        if _should_check(conn):
            if not _is_conn_alive(conn):
                logger.warning("Stale DB connection detected, discarding (attempt %d/%d)", attempt + 1, max_attempts)
                _discard_conn(conn)
                time.sleep(0.1)
                continue
        # Guard: verify connection is actually open before returning
        if conn.closed:
            logger.warning("Closed connection returned from pool, discarding (attempt %d/%d)", attempt + 1, max_attempts)
            _discard_conn(conn)
            time.sleep(0.1)
            continue
        # Always record last-use time so _should_check() measures true idle time
        _mark_checked(conn)
        conn.autocommit = False
        return conn
    raise psycopg2.OperationalError("Failed to obtain a healthy database connection after %d attempts" % max_attempts)


def release_connection(conn):
    if conn is None:
        return
    try:
        if conn.closed:
            _clear_checked(conn)
            get_pool().putconn(conn, close=True)
            return
        try:
            conn.rollback()
        except Exception:
            _discard_conn(conn)
            return
        _mark_checked(conn)
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
