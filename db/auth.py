import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.connection import hash_password, verify_password
from db.cache import ttl_cache
import secrets
import json

def create_session(user_id: int, days: int = 30) -> str:
    import secrets
    token = secrets.token_hex(32)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE expires_at < NOW()")
        cursor.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, NOW() + %s * INTERVAL '1 day')",
            (token, user_id, days)
        )
        conn.commit()
    return token

def _fetch_vendas_store_ids(cursor, user_id: int) -> list:
    """Return list of store IDs from user_store_vendas for the given user."""
    cursor.execute(
        "SELECT store_id FROM user_store_vendas WHERE user_id = %s ORDER BY store_id",
        (user_id,)
    )
    return [r[0] for r in cursor.fetchall()]


def get_session_user(token: str):
    if not token:
        return None
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.role, u.nome, u.acesso_eurokg, u.acesso_producao, u.acesso_vendas, u.acesso_pastelaria, u.acesso_confeitaria, u.acesso_gestor, u.acesso_administrativo, u.loja_id, u.acesso_financeiro, u.acesso_eventos, u.acesso_tarefas
            FROM sessions s JOIN users u ON s.user_id = u.id
            WHERE s.token = %s AND s.expires_at > NOW() AND u.ativo = TRUE
        """, (token,))
        row = cursor.fetchone()
        if row:
            user_id = row[0]
            vendas_store_ids = _fetch_vendas_store_ids(cursor, user_id)
            return {
                'id': user_id, 'username': row[1], 'role': row[2], 'nome': row[3],
                'acesso_eurokg': row[4], 'acesso_producao': row[5], 'acesso_vendas': row[6],
                'acesso_pastelaria': row[7], 'acesso_confeitaria': row[8], 'acesso_gestor': row[9],
                'acesso_administrativo': row[10], 'loja_id': row[11],
                'acesso_financeiro': row[12], 'acesso_eventos': row[13],
                'acesso_tarefas': row[14],
                'vendas_store_ids': vendas_store_ids,
            }
        return None

def delete_session(token: str):
    if not token:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE token = %s", (token,))
        conn.commit()

def authenticate_user(username: str, password: str):
    import logging
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, username, password, role, nome, acesso_eurokg, acesso_producao, acesso_vendas, acesso_pastelaria, acesso_confeitaria, acesso_gestor, acesso_administrativo, loja_id, acesso_financeiro, acesso_eventos, acesso_tarefas FROM users WHERE LOWER(username) = LOWER(%s) AND ativo = TRUE",
                (username,)
            )
            user = cursor.fetchone()
            if user and verify_password(password, user[2]):
                user_id = user[0]
                vendas_store_ids = _fetch_vendas_store_ids(cursor, user_id)
                return {
                    'id': user_id, 'username': user[1], 'role': user[3], 'nome': user[4],
                    'acesso_eurokg': user[5], 'acesso_producao': user[6], 'acesso_vendas': user[7],
                    'acesso_pastelaria': user[8], 'acesso_confeitaria': user[9], 'acesso_gestor': user[10],
                    'acesso_administrativo': user[11], 'loja_id': user[12],
                    'acesso_financeiro': user[13], 'acesso_eventos': user[14],
                    'acesso_tarefas': user[15],
                    'vendas_store_ids': vendas_store_ids,
                }
            return None
    except Exception as e:
        logging.error(f"Authentication error: {e}")
        raise e

def get_all_users():
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, role, nome, ativo, acesso_eurokg, acesso_producao, acesso_vendas, acesso_pastelaria, acesso_confeitaria, acesso_gestor, acesso_administrativo, loja_id, acesso_financeiro, acesso_eventos, acesso_tarefas FROM users ORDER BY username")
        rows = cursor.fetchall()
        users = []
        for row in rows:
            user_id = row[0]
            vendas_store_ids = _fetch_vendas_store_ids(cursor, user_id)
            users.append({
                'id': user_id, 'username': row[1], 'role': row[2], 'nome': row[3], 'ativo': row[4],
                'acesso_eurokg': row[5], 'acesso_producao': row[6], 'acesso_vendas': row[7],
                'acesso_pastelaria': row[8], 'acesso_confeitaria': row[9], 'acesso_gestor': row[10],
                'acesso_administrativo': row[11], 'loja_id': row[12],
                'acesso_financeiro': row[13], 'acesso_eventos': row[14],
                'acesso_tarefas': row[15],
                'vendas_store_ids': vendas_store_ids,
            })
        return users

def add_user(username: str, password: str, permissoes: dict = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            hashed_pw = hash_password(password)
            perms = permissoes or {}
            role = 'gestao' if perms.get('acesso_gestor') else 'producao'
            cursor.execute(
                """INSERT INTO users (username, password, role, nome, acesso_eurokg, acesso_producao, acesso_vendas, acesso_pastelaria, acesso_confeitaria, acesso_gestor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (username, hashed_pw, role, username,
                 perms.get('acesso_eurokg', False), perms.get('acesso_producao', False),
                 perms.get('acesso_vendas', False), perms.get('acesso_pastelaria', False),
                 perms.get('acesso_confeitaria', False), perms.get('acesso_gestor', False))
            )
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_user_password(user_id: int, new_password: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        hashed_pw = hash_password(new_password)
        cursor.execute("UPDATE users SET password = %s WHERE id = %s", (hashed_pw, user_id))
        conn.commit()

def update_user_store_vendas(user_id: int, store_ids: list):
    """Replace the user's vendas store permissions with the given list of store IDs."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_store_vendas WHERE user_id = %s", (user_id,))
        for sid in store_ids:
            cursor.execute(
                "INSERT INTO user_store_vendas (user_id, store_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, sid)
            )
        has_vendas = len(store_ids) > 0
        cursor.execute(
            "UPDATE users SET acesso_vendas = %s WHERE id = %s",
            (has_vendas, user_id)
        )
        conn.commit()


def update_user_permissoes_batch(updates: list):
    with db_connection() as conn:
        cursor = conn.cursor()
        for u in updates:
            role = 'gestao' if u.get('acesso_gestor') else 'producao'
            store_ids = u.get('vendas_store_ids', [])
            has_vendas = len(store_ids) > 0
            cursor.execute(
                """UPDATE users SET acesso_eurokg = %s, acesso_producao = %s, acesso_vendas = %s,
                   acesso_pastelaria = %s, acesso_confeitaria = %s, acesso_gestor = %s, acesso_administrativo = %s,
                   acesso_financeiro = %s, acesso_eventos = %s, acesso_tarefas = %s,
                   role = %s, ativo = %s
                   WHERE id = %s""",
                (u['acesso_eurokg'], u['acesso_producao'], has_vendas,
                 u['acesso_pastelaria'], u['acesso_confeitaria'], u['acesso_gestor'], u.get('acesso_administrativo', False),
                 u.get('acesso_financeiro', False), u.get('acesso_eventos', True), u.get('acesso_tarefas', False),
                 role, u['ativo'], u['id'])
            )
            cursor.execute("DELETE FROM user_store_vendas WHERE user_id = %s", (u['id'],))
            for sid in store_ids:
                cursor.execute(
                    "INSERT INTO user_store_vendas (user_id, store_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (u['id'], sid)
                )
        conn.commit()


@ttl_cache('active_venda_stores', ttl=600)
def get_active_venda_stores():
    """Returns active retail stores (store_type='loja') for transfer dropdowns and stock validation."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM stores WHERE store_type = 'loja' AND is_active = TRUE ORDER BY name")
        return [{'id': r[0], 'name': r[1]} for r in cursor.fetchall()]


@ttl_cache('vendas_module_stores', ttl=600)
def get_vendas_module_stores():
    """Returns active stores that participate in the vendas module (quebras + fecho de caixa).
    Uses supports_vendas flag, so includes stores of any type (e.g. Matosinhos as 'producao')."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, store_type FROM stores WHERE supports_vendas = TRUE AND is_active = TRUE ORDER BY name")
        return [{'id': r[0], 'name': r[1], 'store_type': r[2]} for r in cursor.fetchall()]


def get_store_by_id(store_id: int):
    """Returns store dict for a given id, or None."""
    if not store_id:
        return None
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, store_type, is_active, requires_eod_weighing, supports_vendas FROM stores WHERE id = %s",
            (store_id,)
        )
        row = cursor.fetchone()
        if row:
            return {
                'id': row[0], 'name': row[1], 'store_type': row[2],
                'is_active': row[3], 'requires_eod_weighing': row[4],
                'supports_vendas': row[5],
            }
        return None

def update_user(user_id: int, username: str = None, password: str = None, role: str = None, nome: str = None, ativo: bool = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        updates = []
        params = []
        if username:
            updates.append("username = %s")
            params.append(username)
        if password:
            updates.append("password = %s")
            params.append(hash_password(password))
        if role:
            updates.append("role = %s")
            params.append(role)
        if nome is not None:
            updates.append("nome = %s")
            params.append(nome)
        if ativo is not None:
            updates.append("ativo = %s")
            params.append(ativo)
        if updates:
            params.append(user_id)
            set_clause = ", ".join(updates)
            cursor.execute("UPDATE users SET " + set_clause + " WHERE id = %s", params)
            conn.commit()

def delete_user(user_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def delete_user_with_sessions(user_id: int):
    """Revoke all sessions and delete the user in a single atomic transaction.

    Preferred over calling revoke_user_sessions + delete_user separately so that
    a DELETE failure doesn't leave the user in a logged-out-but-still-existing state.
    Sessions cascade via ON DELETE CASCADE on the FK, but explicit revocation in
    the same transaction is belt-and-suspenders and makes the intent clear.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def revoke_user_sessions(user_id: int):
    """Delete all active sessions for a given user (e.g. after deactivation)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        conn.commit()


def count_admin_users() -> int:
    """Return the total number of users with role='admin' (regardless of ativo status)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
        return int(cursor.fetchone()[0])
