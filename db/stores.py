import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache, invalidate
import json

def get_store_id_by_name(name: str) -> int | None:
    """Looks up a store's integer id from its display name (case-insensitive). Returns None if not found."""
    if not name:
        return None
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM stores WHERE LOWER(name) = LOWER(%s) LIMIT 1", (name,))
        row = cursor.fetchone()
        return row[0] if row else None


@ttl_cache('all_stores', ttl=600)
def get_all_stores():
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, address, latitude, longitude, store_type,
                   is_active, receives_transfers, requires_eod_weighing,
                   pos_store_code, opened_at, shows_on_landing, created_at
            FROM stores ORDER BY name
        """)
        return [{
            'id': r[0], 'name': r[1], 'address': r[2], 'latitude': r[3],
            'longitude': r[4], 'store_type': r[5], 'is_active': r[6],
            'receives_transfers': r[7], 'requires_eod_weighing': r[8],
            'pos_store_code': r[9], 'opened_at': r[10],
            'shows_on_landing': r[11], 'created_at': r[12]
        } for r in cursor.fetchall()]


def get_store(store_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, address, latitude, longitude, store_type,
                   is_active, receives_transfers, requires_eod_weighing,
                   pos_store_code, opened_at, shows_on_landing, created_at
            FROM stores WHERE id = %s
        """, (store_id,))
        r = cursor.fetchone()
        if not r:
            return None
        return {
            'id': r[0], 'name': r[1], 'address': r[2], 'latitude': r[3],
            'longitude': r[4], 'store_type': r[5], 'is_active': r[6],
            'receives_transfers': r[7], 'requires_eod_weighing': r[8],
            'pos_store_code': r[9], 'opened_at': r[10],
            'shows_on_landing': r[11], 'created_at': r[12]
        }


@ttl_cache('landing_stores', ttl=600)
def get_active_landing_stores():
    """Returns active stores that have shows_on_landing=True, for dynamic home page tiles."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM stores WHERE is_active=TRUE AND shows_on_landing=TRUE ORDER BY name")
        return [{'id': r[0], 'name': r[1]} for r in cursor.fetchall()]


def upsert_store(store_id, name, address, latitude, longitude, store_type,
                 is_active, receives_transfers, requires_eod_weighing,
                 pos_store_code, opened_at, shows_on_landing=False):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            if store_id:
                cursor.execute("""
                    UPDATE stores SET name=%s, address=%s, latitude=%s, longitude=%s,
                        store_type=%s, is_active=%s, receives_transfers=%s,
                        requires_eod_weighing=%s, pos_store_code=%s, opened_at=%s,
                        shows_on_landing=%s
                    WHERE id=%s
                """, (name, address, latitude, longitude, store_type, is_active,
                      receives_transfers, requires_eod_weighing, pos_store_code,
                      opened_at, shows_on_landing, store_id))
            else:
                cursor.execute("""
                    INSERT INTO stores (name, address, latitude, longitude, store_type,
                        is_active, receives_transfers, requires_eod_weighing,
                        pos_store_code, opened_at, shows_on_landing)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (name, address, latitude, longitude, store_type, is_active,
                      receives_transfers, requires_eod_weighing, pos_store_code,
                      opened_at, shows_on_landing))
            conn.commit()
            invalidate('active_venda_stores', 'vendas_module_stores', 'all_stores', 'landing_stores')
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False


def toggle_store_active(store_id: int, is_active: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE stores SET is_active=%s WHERE id=%s", (is_active, store_id))
        conn.commit()
    invalidate('active_venda_stores', 'vendas_module_stores', 'all_stores', 'landing_stores')


def delete_store(store_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stores WHERE id=%s", (store_id,))
        conn.commit()
    invalidate('active_venda_stores', 'vendas_module_stores', 'all_stores', 'landing_stores')


def get_store_aliases(store_id: int) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, store_id, alias_name, alias_code, created_at
            FROM store_aliases WHERE store_id = %s ORDER BY id
        """, (store_id,))
        return [
            {'id': r[0], 'store_id': r[1], 'alias_name': r[2],
             'alias_code': r[3], 'created_at': r[4]}
            for r in cursor.fetchall()
        ]


def add_store_alias(store_id: int, alias_name: str = None, alias_code: str = None) -> int:
    """Add an alias for a store. Returns the new alias id."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO store_aliases (store_id, alias_name, alias_code)
            VALUES (%s, %s, %s) RETURNING id
        """, (store_id, alias_name or None, alias_code or None))
        new_id = cursor.fetchone()[0]
        conn.commit()
    return new_id


def delete_store_alias(alias_id: int, store_id: int = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        if store_id is not None:
            cursor.execute("DELETE FROM store_aliases WHERE id = %s AND store_id = %s", (alias_id, store_id))
        else:
            cursor.execute("DELETE FROM store_aliases WHERE id = %s", (alias_id,))
        conn.commit()


def get_all_store_aliases() -> list:
    """Returns all aliases for all stores, used by _resolve_store at import time."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sa.id, sa.store_id, sa.alias_name, sa.alias_code,
                   s.name, s.is_active, s.pos_store_code
            FROM store_aliases sa
            JOIN stores s ON sa.store_id = s.id
            WHERE s.is_active = TRUE
            ORDER BY sa.store_id
        """)
        return [
            {
                'id': r[0], 'store_id': r[1], 'alias_name': r[2],
                'alias_code': r[3], 'store_name': r[4],
                'store_is_active': r[5], 'store_pos_code': r[6]
            }
            for r in cursor.fetchall()
        ]


