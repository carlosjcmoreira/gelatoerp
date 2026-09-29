import psycopg2
import hashlib
from uuid import uuid4
from psycopg2.extras import Json, RealDictCursor, DictCursor, execute_values
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger, db_retry
from db.dose_associations import (
    backfill_weight_sales,
    close_current_association,
)
from db.cache import ttl_cache, invalidate, invalidate_prefix
from db.stores import get_store_id_by_name
from db.producao import get_sabores_mapping
import pandas as pd
import json
import os
import math
from collections import defaultdict
from bisect import bisect_right

def generate_lote_pastelaria(data: date) -> str:
    return f"P{data.strftime('%d%m%y')}"

def generate_lote_confeitaria(data: date) -> str:
    return f"C{data.strftime('%d%m%y')}"

def add_producao_pastelaria(data: date, loja: str, produto: str, quantidade: int):
    store_id = get_store_id_by_name(loja)
    lote = generate_lote_pastelaria(data)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MIN(id)
            FROM produtos_pastelaria
            WHERE CONCAT_WS(', ',
                NULLIF(BTRIM(tipologia), ''),
                NULLIF(BTRIM(sabor), ''),
                NULLIF(BTRIM(cobertura), '')
            ) = %s
            HAVING COUNT(*) = 1
        """, (produto,))
        identity_row = cursor.fetchone()
        produto_pastelaria_id = identity_row[0] if identity_row else None
        if (
            produto_pastelaria_id is None
            and not produto.startswith('Bolo — ')
        ):
            raise ValueError(
                f'Produto de Pastelaria sem identidade única: {produto}'
            )
        cursor.execute('''
            INSERT INTO producao_pastelaria (
                data, loja, produto, quantidade, lote, store_id,
                produto_pastelaria_id
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s
            )
        ''', (
            data, loja, produto, quantidade, lote, store_id,
            produto_pastelaria_id,
        ))
        conn.commit()
    return lote

def get_pastelaria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    query = "SELECT * FROM producao_pastelaria WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def get_pastelaria_stock() -> pd.DataFrame:
    query = """
        SELECT loja, produto, SUM(quantidade) as stock
        FROM producao_pastelaria
        GROUP BY loja, produto
        ORDER BY loja, produto
    """
    with db_connection() as conn:
        return pd.read_sql_query(query, conn)

def add_producao_confeitaria(data: date, loja: str, produto: str, quantidade: int):
    store_id = get_store_id_by_name(loja)
    lote = generate_lote_confeitaria(data)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO producao_confeitaria (data, loja, produto, quantidade, lote, store_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (data, loja, produto, quantidade, lote, store_id))
        conn.commit()
    return lote

def get_confeitaria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    query = "SELECT * FROM producao_confeitaria WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def get_confeitaria_stock() -> pd.DataFrame:
    query = """
        SELECT loja, produto, SUM(quantidade) as stock
        FROM producao_confeitaria
        GROUP BY loja, produto
        ORDER BY loja, produto
    """
    with db_connection() as conn:
        return pd.read_sql_query(query, conn)

def add_contagem_stock(data: date, loja: str, produto: str, quantidade: int, tipo: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        if tipo == 'pastelaria' and data.weekday() == 6:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f'pastelaria-count:{data.isoformat()}',),
            )
        produto_pastelaria_id = None
        if tipo == 'pastelaria':
            cursor.execute("""
                SELECT id
                FROM produtos_pastelaria
                WHERE CONCAT_WS(', ',
                    NULLIF(BTRIM(tipologia), ''),
                    NULLIF(BTRIM(sabor), ''),
                    NULLIF(BTRIM(cobertura), '')
                ) = %s
            """, (produto,))
            matches = cursor.fetchall()
            if len(matches) != 1:
                raise ValueError('Produto de Pastelaria inválido ou ambíguo.')
            produto_pastelaria_id = matches[0][0]
        cursor.execute("""
            INSERT INTO contagem_stock
                (data, loja, produto, quantidade, tipo, produto_pastelaria_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (data, loja, produto, quantidade, tipo, produto_pastelaria_id))
        conn.commit()


def get_pastelaria_sunday_count_grid(count_date, store_id=None):
    """Return the active store/product matrix and latest count for a Sunday."""
    if count_date.weekday() != 6:
        raise ValueError('Escolha um domingo para preencher a grelha de contagem.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-count:{count_date.isoformat()}',),
        )
        store_filter = " AND id=%s" if store_id is not None else ""
        cursor.execute(f"""
            SELECT id, name FROM stores
            WHERE supports_vendas=TRUE AND is_active=TRUE{store_filter}
            ORDER BY name
        """, (store_id,) if store_id is not None else None)
        stores = cursor.fetchall()
        cursor.execute("""
            SELECT id, tipologia, sabor, cobertura
            FROM produtos_pastelaria WHERE ativo=TRUE
            ORDER BY tipologia, sabor, cobertura
        """)
        products = cursor.fetchall()
        cursor.execute("""
            SELECT DISTINCT ON (
                       COALESCE(cs.store_id, legacy_store.id),
                       COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                   )
                   COALESCE(cs.store_id, legacy_store.id) AS store_id,
                   cs.produto, cs.quantidade, cs.produto_pastelaria_id
            FROM contagem_stock cs
            LEFT JOIN LATERAL (
                SELECT MIN(id) AS id
                FROM stores
                WHERE name=cs.loja
                HAVING COUNT(*)=1
            ) AS legacy_store ON cs.store_id IS NULL
            WHERE tipo='pastelaria' AND origem='contagem' AND data=%s
            ORDER BY COALESCE(cs.store_id, legacy_store.id),
                     COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                     cs.id DESC
        """, (count_date,))
        counts = {
            (row['store_id'],
             row.get('produto_pastelaria_id') or row['produto']): int(row['quantidade'])
            for row in cursor.fetchall()
        }
        token_filter = """
            AND (
                cs.store_id=%s
                OR (cs.store_id IS NULL AND legacy_store.id=%s)
            )
        """ if store_id is not None else ""
        cursor.execute(f"""
            SELECT MD5(COALESCE(STRING_AGG(id::text, ',' ORDER BY loja, product_key), ''))
                   AS snapshot_token
            FROM (
                SELECT DISTINCT ON (
                           COALESCE(cs.store_id, legacy_store.id),
                           COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                       )
                       cs.id, cs.loja,
                       COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                           AS product_key
                FROM contagem_stock cs
                LEFT JOIN LATERAL (
                    SELECT MIN(id) AS id
                    FROM stores
                    WHERE name=cs.loja
                    HAVING COUNT(*)=1
                ) AS legacy_store ON cs.store_id IS NULL
                WHERE cs.tipo='pastelaria' AND cs.origem='contagem' AND cs.data=%s
                {token_filter}
                ORDER BY COALESCE(cs.store_id, legacy_store.id),
                         COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                         cs.id DESC
            ) latest
        """, (count_date, store_id, store_id)
        if store_id is not None else (count_date,))
        snapshot_token = cursor.fetchone()['snapshot_token']
    rows = []
    completed = 0
    completed_by_store = {store['id']: 0 for store in stores}
    for product in products:
        label = _pastelaria_product_label(product)
        values = {}
        for store in stores:
            quantity = counts.get((store['id'], product['id']))
            values[store['id']] = quantity
            completed += quantity is not None
            completed_by_store[store['id']] += quantity is not None
        rows.append({**product, 'nome': label, 'counts': values})
    total = len(stores) * len(products)
    return {
        'date': count_date,
        'stores': stores,
        'products': rows,
        'completed': completed,
        'total': total,
        'complete': total > 0 and completed == total,
        'completed_by_store': completed_by_store,
        'total_by_store': {store['id']: len(products) for store in stores},
        'snapshot_token': snapshot_token,
    }


def get_pastelaria_store_count_grid(
    count_date, store_id, allow_non_sunday=False,
):
    """Return a physical count grid for one active sales store."""
    if count_date.weekday() != 6 and not allow_non_sunday:
        raise ValueError('Escolha um domingo para preencher a grelha de contagem.')
    try:
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise ValueError('Loja inválida.') from None

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-count:{count_date.isoformat()}',),
        )
        cursor.execute("""
            SELECT id, name
            FROM stores
            WHERE id=%s AND supports_vendas=TRUE AND is_active=TRUE
        """, (store_id,))
        store = cursor.fetchone()
        if not store:
            raise ValueError('Loja inválida ou sem acesso ao módulo Vendas.')
        cursor.execute("""
            SELECT id, tipologia, sabor, cobertura
            FROM produtos_pastelaria WHERE ativo=TRUE
            ORDER BY tipologia, sabor, cobertura
        """)
        products = cursor.fetchall()
        cursor.execute("""
            SELECT DISTINCT ON (COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto))
                   cs.produto, cs.quantidade, cs.produto_pastelaria_id
            FROM contagem_stock cs
            LEFT JOIN LATERAL (
                SELECT MIN(id) AS id
                FROM stores
                WHERE name=cs.loja
                HAVING COUNT(*)=1
            ) AS legacy_store ON cs.store_id IS NULL
            WHERE cs.tipo='pastelaria' AND cs.origem='contagem'
              AND cs.data=%s
              AND (
                  cs.store_id=%s
                  OR (cs.store_id IS NULL AND legacy_store.id=%s)
              )
            ORDER BY COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto), cs.id DESC
        """, (count_date, store_id, store_id))
        counts = {
            row.get('produto_pastelaria_id') or row['produto']: int(row['quantidade'])
            for row in cursor.fetchall()
        }
        cursor.execute("""
            SELECT MD5(COALESCE(STRING_AGG(id::text, ',' ORDER BY product_key), ''))
                   AS snapshot_token
            FROM (
                SELECT DISTINCT ON (
                           COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                       )
                       cs.id,
                       COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                           AS product_key
                FROM contagem_stock cs
                LEFT JOIN LATERAL (
                    SELECT MIN(id) AS id
                    FROM stores
                    WHERE name=cs.loja
                    HAVING COUNT(*)=1
                ) AS legacy_store ON cs.store_id IS NULL
                WHERE cs.tipo='pastelaria' AND cs.origem='contagem'
                  AND cs.data=%s
                  AND (
                      cs.store_id=%s
                      OR (cs.store_id IS NULL AND legacy_store.id=%s)
                  )
                ORDER BY COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                         cs.id DESC
            ) latest
        """, (count_date, store_id, store_id))
        snapshot_token = cursor.fetchone()['snapshot_token']

    rows = []
    completed = 0
    for product in products:
        label = _pastelaria_product_label(product)
        quantity = counts.get(product['id'])
        completed += quantity is not None
        rows.append({**product, 'nome': label, 'count': quantity})
    total = len(products)
    return {
        'date': count_date,
        'store': store,
        'stores': [store],
        'products': rows,
        'completed': completed,
        'total': total,
        'complete': total > 0 and completed == total,
        'snapshot_token': snapshot_token,
    }


def save_pastelaria_store_counts(
    count_date, store_id, values, snapshot_token, allow_non_sunday=False,
    submitted_by=None,
):
    """Append one store's complete physical count snapshot."""
    if count_date.weekday() != 6 and not allow_non_sunday:
        raise ValueError('A data da contagem tem de ser um domingo.')
    try:
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise ValueError('Loja inválida.') from None
    submitted_by = _validated_count_submitter(submitted_by)
    normalized = {}
    snapshot_token = str(snapshot_token or '')
    if len(snapshot_token) != 32 or any(
        character not in '0123456789abcdef' for character in snapshot_token
    ):
        raise ValueError('A versão da grelha é inválida.') from None
    for product_id, quantity in values:
        if (
            isinstance(quantity, bool) or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise ValueError('As contagens devem ser números inteiros não negativos.')
        if product_id in normalized:
            raise ValueError('A grelha contém produtos repetidos.')
        normalized[product_id] = quantity

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-count:{count_date.isoformat()}',),
        )
        cursor.execute("""
            SELECT id, name
            FROM stores
            WHERE id=%s AND supports_vendas=TRUE AND is_active=TRUE
        """, (store_id,))
        store = cursor.fetchone()
        if not store:
            raise ValueError('Loja inválida ou sem acesso ao módulo Vendas.')
        cursor.execute("""
            SELECT MD5(COALESCE(STRING_AGG(id::text, ',' ORDER BY product_key), ''))
                   AS snapshot_token
            FROM (
                SELECT DISTINCT ON (
                           COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                       )
                       cs.id,
                       COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                           AS product_key
                 FROM contagem_stock cs
                 LEFT JOIN LATERAL (
                     SELECT MIN(id) AS id
                     FROM stores
                     WHERE name=cs.loja
                     HAVING COUNT(*)=1
                 ) AS legacy_store ON cs.store_id IS NULL
                 WHERE cs.tipo='pastelaria' AND cs.origem='contagem'
                   AND cs.data=%s
                   AND (
                       cs.store_id=%s
                       OR (cs.store_id IS NULL AND legacy_store.id=%s)
                   )
                ORDER BY COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                          cs.id DESC
            ) latest
        """, (count_date, store_id, store_id))
        current_token = cursor.fetchone()['snapshot_token']
        if current_token != snapshot_token:
            raise ValueError(
                'Esta grelha foi alterada por outro utilizador. '
                'Atualize a página antes de guardar.'
            )
        cursor.execute("""
            SELECT id, tipologia, sabor, cobertura
            FROM produtos_pastelaria WHERE ativo=TRUE
            ORDER BY tipologia, sabor, cobertura
        """)
        products = cursor.fetchall()
        expected = {product['id'] for product in products}
        if not expected or set(normalized) != expected:
            raise ValueError(
                'Preencha todas as contagens da grelha antes de guardar.'
            )
        product_names = {
            product['id']: _pastelaria_product_label(product)
            for product in products
        }
        submission_id = uuid4()
        rows = [
            (
                count_date, store['name'], product_names[product_id],
                quantity, 'pastelaria', 'contagem', product_id, store_id,
                str(submission_id), submitted_by,
            )
            for product_id, quantity in normalized.items()
        ]
        execute_values(cursor, """
            INSERT INTO contagem_stock
                (data, loja, produto, quantidade, tipo, origem,
                 produto_pastelaria_id, store_id, submission_id, submitted_by)
            VALUES %s
        """, rows)
        conn.commit()
    return len(rows)


def save_pastelaria_sunday_counts(
    count_date, values, snapshot_token, store_id=None, submitted_by=None
):
    """Append one complete Sunday count snapshot in a single transaction."""
    if count_date.weekday() != 6:
        raise ValueError('A data da contagem tem de ser um domingo.')
    normalized = {}
    snapshot_token = str(snapshot_token or '')
    if len(snapshot_token) != 32 or any(
        character not in '0123456789abcdef' for character in snapshot_token
    ):
        raise ValueError('A versão da grelha é inválida.') from None
    submitted_by = _validated_count_submitter(submitted_by)
    for product_id, value_store_id, quantity in values:
        if (
            isinstance(quantity, bool) or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise ValueError('As contagens devem ser números inteiros não negativos.')
        key = (product_id, value_store_id)
        if key in normalized:
            raise ValueError('A grelha contém células repetidas.')
        normalized[key] = quantity

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-count:{count_date.isoformat()}',),
        )
        token_filter = """
            AND (
                cs.store_id=%s
                OR (cs.store_id IS NULL AND legacy_store.id=%s)
            )
        """ if store_id is not None else ""
        cursor.execute(f"""
            SELECT MD5(COALESCE(STRING_AGG(id::text, ',' ORDER BY loja, product_key), ''))
                   AS snapshot_token
            FROM (
                SELECT DISTINCT ON (
                           COALESCE(cs.store_id, legacy_store.id),
                           COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                       )
                       cs.id, cs.loja,
                       COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                           AS product_key
                 FROM contagem_stock cs
                 LEFT JOIN LATERAL (
                     SELECT MIN(id) AS id
                     FROM stores
                     WHERE name=cs.loja
                     HAVING COUNT(*)=1
                 ) AS legacy_store ON cs.store_id IS NULL
                 WHERE cs.tipo='pastelaria' AND cs.origem='contagem' AND cs.data=%s
                {token_filter}
                 ORDER BY COALESCE(cs.store_id, legacy_store.id),
                         COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                         cs.id DESC
            ) latest
        """, (count_date, store_id, store_id)
        if store_id is not None else (count_date,))
        current_token = cursor.fetchone()['snapshot_token']
        if current_token != snapshot_token:
            raise ValueError(
                'Esta grelha foi alterada por outro utilizador. '
                'Atualize a página antes de guardar.'
            )
        store_filter = " AND id=%s" if store_id is not None else ""
        cursor.execute(f"""
            SELECT id, name FROM stores
            WHERE supports_vendas=TRUE AND is_active=TRUE{store_filter}
            ORDER BY name
        """, (store_id,) if store_id is not None else None)
        stores = cursor.fetchall()
        cursor.execute("""
            SELECT id, tipologia, sabor, cobertura
            FROM produtos_pastelaria WHERE ativo=TRUE
            ORDER BY tipologia, sabor, cobertura
        """)
        products = cursor.fetchall()
        expected = {
            (product['id'], store['id'])
            for product in products for store in stores
        }
        if not expected or set(normalized) != expected:
            raise ValueError(
                'Preencha todas as contagens da grelha antes de guardar.'
            )
        store_names = {store['id']: store['name'] for store in stores}
        product_names = {
            product['id']: _pastelaria_product_label(product)
            for product in products
        }
        submission_id = uuid4()
        rows = [
            (
                count_date, store_names[store_id], product_names[product_id],
                quantity, 'pastelaria', 'contagem', product_id, store_id,
                str(submission_id), submitted_by,
            )
            for (product_id, store_id), quantity in normalized.items()
        ]
        execute_values(cursor, """
            INSERT INTO contagem_stock
                (data, loja, produto, quantidade, tipo, origem,
                 produto_pastelaria_id, store_id, submission_id, submitted_by)
            VALUES %s
        """, rows)
        conn.commit()


def _validated_count_submitter(submitted_by):
    if not isinstance(submitted_by, str) or not submitted_by.strip():
        raise ValueError('Não foi possível identificar quem submeteu a contagem.')
    submitted_by = submitted_by.strip()
    if len(submitted_by) > 100:
        raise ValueError('O nome do utilizador é demasiado longo.')
    return submitted_by


def get_pastelaria_count_submission_history(count_date, store_id):
    """Return submission-level metadata for one store and count date."""
    try:
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise ValueError('Loja inválida.') from None
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT submission_id, data, store_id, loja AS store_name,
                   submitted_by, MIN(submitted_at) AS submitted_at,
                   COUNT(*) AS product_count
            FROM contagem_stock
            WHERE tipo='pastelaria' AND origem='contagem'
              AND submission_id IS NOT NULL
              AND store_id=%s AND data=%s
            GROUP BY submission_id, data, store_id, loja, submitted_by
            ORDER BY MIN(submitted_at) DESC, submission_id DESC
        """, (store_id, count_date))
        return cursor.fetchall()


def get_contagem_stock_df(tipo: str, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    with db_connection() as conn:
        cursor = conn.cursor()
        if tipo == 'pastelaria':
            cursor.execute("""
                SELECT pg_advisory_xact_lock(
                    hashtextextended('pastelaria-count:' || data::text, 0)
                )
                FROM (
                    SELECT DISTINCT data
                    FROM contagem_stock
                    WHERE tipo='pastelaria'
                      AND (%s IS NULL OR data >= %s)
                      AND (%s IS NULL OR data <= %s)
                      AND EXTRACT(DOW FROM data)=0
                ) sundays
            """, (data_inicio, data_inicio, data_fim, data_fim))
        query = """
            SELECT cs.id, cs.data, cs.loja,
                   CASE WHEN cs.tipo='pastelaria' AND p.id IS NOT NULL
                        THEN CONCAT_WS(', ',
                            NULLIF(BTRIM(p.tipologia), ''),
                            NULLIF(BTRIM(p.sabor), ''),
                            NULLIF(BTRIM(p.cobertura), '')
                        )
                        ELSE cs.produto END AS produto,
                   cs.quantidade, cs.tipo, cs.origem, cs.created_at,
                   cs.produto_pastelaria_id
            FROM contagem_stock cs
            LEFT JOIN produtos_pastelaria p
              ON p.id=cs.produto_pastelaria_id
            WHERE cs.tipo = %s
        """
        params = [tipo]
        if tipo == 'pastelaria':
            query += " AND cs.origem='contagem'"
        if data_inicio:
            query += " AND cs.data >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND cs.data <= %s"
            params.append(data_fim)
        query += " ORDER BY cs.data DESC, produto"
        return pd.read_sql_query(query, conn, params=params)

def add_ajuste_producao(ano: int, mes: int, quantidade_kg: float, descricao: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ajustes_producao (ano, mes, quantidade_kg, descricao) VALUES (%s, %s, %s, %s)",
            (ano, mes, quantidade_kg, descricao)
        )
        conn.commit()

def get_ajustes_producao(ano: int = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        if ano:
            cursor.execute("SELECT id, ano, mes, quantidade_kg, descricao, created_at FROM ajustes_producao WHERE ano = %s ORDER BY mes", (ano,))
        else:
            cursor.execute("SELECT id, ano, mes, quantidade_kg, descricao, created_at FROM ajustes_producao ORDER BY ano, mes")
        rows = cursor.fetchall()
    return [{'id': r[0], 'ano': r[1], 'mes': r[2], 'quantidade_kg': r[3], 'descricao': r[4], 'created_at': r[5]} for r in rows]

def delete_ajuste_producao(ajuste_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ajustes_producao WHERE id = %s", (ajuste_id,))
        conn.commit()

def delete_contagem_stock(contagem_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT data, tipo FROM contagem_stock WHERE id = %s",
            (contagem_id,),
        )
        row = cursor.fetchone()
        if row and row[1] == 'pastelaria' and row[0].weekday() == 6:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f'pastelaria-count:{row[0].isoformat()}',),
            )
            cursor.execute(
                "SELECT id FROM contagem_stock WHERE id = %s FOR UPDATE",
                (contagem_id,),
            )
        cursor.execute("DELETE FROM contagem_stock WHERE id = %s", (contagem_id,))
        conn.commit()

def get_produtos_confeitaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM produtos_confeitaria WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def add_produto_confeitaria(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO produtos_confeitaria (nome) VALUES (%s)", (nome,))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_produto_confeitaria(id: int, nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos_confeitaria SET nome = %s WHERE id = %s", (nome, id))
        conn.commit()

def set_produto_confeitaria_ativo(id: int, ativo: bool) -> bool:
    if not isinstance(id, int) or isinstance(id, bool) or id <= 0:
        return False
    if not isinstance(ativo, bool):
        return False
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE produtos_confeitaria SET ativo = %s WHERE id = %s",
            (ativo, id),
        )
        updated = cursor.rowcount == 1
        conn.commit()
    return updated

def delete_produto_confeitaria(id: int):
    """Legacy API: preserve catalog identity by deactivating instead of deleting."""
    return set_produto_confeitaria_ativo(id, False)

def get_all_produtos_confeitaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, COALESCE(ativo, FALSE) AS ativo
            FROM produtos_confeitaria
            ORDER BY (ativo IS TRUE) DESC, nome
        """)
        return [
            {'id': row[0], 'nome': row[1], 'ativo': bool(row[2])}
            for row in cursor.fetchall()
        ]

@ttl_cache('motivos_quebra', ttl=300)
def get_motivos_quebra() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM motivos_quebra WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_motivos_quebra() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, ativo FROM motivos_quebra ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]

def add_motivo_quebra(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO motivos_quebra (nome) VALUES (%s)", (nome,))
            conn.commit()
            success = True
        except psycopg2.IntegrityError:
            conn.rollback()
            success = False
    invalidate('motivos_quebra')
    return success

def update_motivo_quebra(id: int, nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE motivos_quebra SET nome = %s WHERE id = %s", (nome, id))
        conn.commit()
    invalidate('motivos_quebra')

def delete_motivo_quebra(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM motivos_quebra WHERE id = %s", (id,))
        conn.commit()
    invalidate('motivos_quebra')

def get_produtos_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tipologia, sabor, cobertura FROM produtos_pastelaria WHERE ativo = TRUE ORDER BY tipologia, sabor")
        produtos = []
        for row in cursor.fetchall():
            tipologia = row[0] or ''
            sabor = row[1] or ''
            cobertura = row[2] or ''
            parts = [tipologia]
            if sabor:
                parts.append(sabor)
            if cobertura:
                parts.append(cobertura)
            produtos.append(', '.join(parts))
    return produtos


def get_bolo_tamanhos() -> list:
    """Return active cake sizes in their configured display order."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT nome
            FROM tamanhos_bolo_pastelaria
            WHERE ativo = TRUE
            ORDER BY ordem, nome
        """)
        return [row[0] for row in cursor.fetchall()]


def build_bolo_product_label(tamanho: str, sabores: list, cobertura: str = '') -> str:
    """Build the stable display/stock key for a configured cake."""
    sabores_text = ' + '.join(sabores)
    label = f"Bolo — {tamanho} — {sabores_text}"
    if cobertura:
        label += f" — Cobertura: {cobertura}"
    return label

def get_all_produtos_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, tipologia, sabor, cobertura, ativo FROM produtos_pastelaria ORDER BY tipologia, sabor")
        return [{'id': row[0], 'tipologia': row[1] or '', 'sabor': row[2] or '', 'cobertura': row[3] or '', 'ativo': row[4]} for row in cursor.fetchall()]


def pastelaria_product_state_token(products):
    payload = '|'.join(
        f"{int(product['id'])}:{1 if product['ativo'] else 0}"
        for product in sorted(products, key=lambda product: int(product['id']))
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def get_pastelaria_product_state_history(limit=100) -> list:
    """Return recent catalogue state changes, newest first."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT a.id, a.produto_id, a.actor_id, a.actor_username,
                   a.previous_active, a.new_active, a.changed_at,
                   p.tipologia, p.sabor, p.cobertura
            FROM pastelaria_produto_estado_audit a
            JOIN produtos_pastelaria p ON p.id = a.produto_id
            ORDER BY a.changed_at DESC, a.id DESC
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
    return [
        {
            **row,
            'produto': _pastelaria_product_label(row),
        }
        for row in rows
    ]


def _pastelaria_product_label(row):
    return ', '.join(
        str(row.get(key) or '').strip()
        for key in ('tipologia', 'sabor', 'cobertura')
        if str(row.get(key) or '').strip()
    )


def get_pastelaria_stock_minimums():
    """Active catalogue products with the configured minimum for each active sales store."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, tipologia, sabor, cobertura
            FROM produtos_pastelaria WHERE ativo=TRUE
            ORDER BY tipologia, sabor, cobertura
        """)
        products = cursor.fetchall()
        cursor.execute("""
            SELECT id, name FROM stores
            WHERE supports_vendas=TRUE AND is_active=TRUE ORDER BY name
        """)
        stores = cursor.fetchall()
        cursor.execute("""
            SELECT produto_id, store_id, quantidade_minima
            FROM pastelaria_stock_minimos
        """)
        values = {
            (row['produto_id'], row['store_id']): row['quantidade_minima']
            for row in cursor.fetchall()
        }
    return {
        'stores': stores,
        'products': [{
            **product,
            'nome': _pastelaria_product_label(product),
            'minimums': {
                store['id']: values.get((product['id'], store['id']))
                for store in stores
            },
        } for product in products],
    }


def get_pastelaria_intelligence(
    data_inicio: date,
    data_fim: date,
    loja: str = '',
    produto: str = '',
    tipologia: str = '',
    tipologia_venda: str = '',
    estado: str = 'todos',
):
    """Build stock-rotation and uploaded-sales intelligence without inventing flavour sales."""
    if data_inicio > data_fim:
        raise ValueError('A data inicial não pode ser posterior à data final.')
    if (data_fim - data_inicio).days > 1095:
        raise ValueError('Escolha um período máximo de três anos.')
    if estado not in {'todos', 'ativos', 'inativos'}:
        estado = 'todos'
    def previous_year(value):
        try:
            return value.replace(year=value.year - 1)
        except ValueError:
            return value.replace(year=value.year - 1, day=28)
    previous_start = previous_year(data_inicio)
    previous_end = previous_year(data_fim)

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, tipologia, sabor, cobertura, ativo
            FROM produtos_pastelaria
            ORDER BY tipologia, sabor, cobertura
        """)
        catalogue = [dict(row) for row in cursor.fetchall()]
        for row in catalogue:
            row['nome'] = _pastelaria_product_label(row)
        catalogue_by_name = {row['nome']: row for row in catalogue}

        cursor.execute("""
            SELECT name AS loja, TRUE AS active_sales_store
            FROM stores
            WHERE supports_vendas=TRUE AND is_active=TRUE
            ORDER BY name
        """)
        store_rows = [dict(row) for row in cursor.fetchall()]
        stores = [row['loja'] for row in store_rows]
        active_sales_stores = {
            row['loja'] for row in store_rows
            if row.get('active_sales_store', True)
        }

        cursor.execute("""
            SELECT DISTINCT ON (
                       cs.data, cs.loja,
                       COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto)
                   )
                   cs.data, cs.loja,
                   COALESCE(
                       NULLIF(CONCAT_WS(', ',
                           NULLIF(BTRIM(p.tipologia), ''),
                           NULLIF(BTRIM(p.sabor), ''),
                           NULLIF(BTRIM(p.cobertura), '')
                       ), ''),
                       cs.produto
                   ) AS produto,
                   cs.quantidade, cs.created_at, cs.produto_pastelaria_id
            FROM contagem_stock cs
            LEFT JOIN produtos_pastelaria p
              ON p.id=cs.produto_pastelaria_id
            WHERE cs.tipo='pastelaria' AND cs.origem='contagem'
              AND cs.data BETWEEN %s AND %s
            ORDER BY cs.data, cs.loja,
                     COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                     cs.id DESC
        """, (data_inicio, data_fim))
        counts = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT pp.data, pp.loja,
                   COALESCE(
                       NULLIF(CONCAT_WS(', ',
                           NULLIF(BTRIM(p.tipologia), ''),
                           NULLIF(BTRIM(p.sabor), ''),
                           NULLIF(BTRIM(p.cobertura), '')
                       ), ''),
                       pp.produto
                   ) AS produto,
                   SUM(pp.quantidade)::integer AS quantidade,
                   pp.produto_pastelaria_id
            FROM producao_pastelaria pp
            LEFT JOIN produtos_pastelaria p
              ON p.id=pp.produto_pastelaria_id
            WHERE pp.data BETWEEN %s AND %s
            GROUP BY pp.data, pp.loja, pp.produto_pastelaria_id,
                     p.tipologia, p.sabor, p.cobertura, pp.produto
        """, (data_inicio, data_fim))
        production = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT COALESCE(confirmado_em::date, data_prevista, data) AS data,
                   ot.loja_destino AS loja,
                   COALESCE(
                       NULLIF(CONCAT_WS(', ',
                           NULLIF(BTRIM(p.tipologia), ''),
                           NULLIF(BTRIM(p.sabor), ''),
                           NULLIF(BTRIM(p.cobertura), '')
                       ), ''),
                       ot.produto
                   ) AS produto,
                   SUM(ot.quantidade)::numeric AS quantidade,
                   ot.produto_pastelaria_id
            FROM ordens_transferencia ot
            LEFT JOIN produtos_pastelaria p
              ON p.id=ot.produto_pastelaria_id
            WHERE LOWER(ot.area_origem)='pastelaria'
              AND ot.status='confirmada'
              AND ot.unidade IN ('und', 'un', 'unidade', 'unidades')
              AND ((ot.confirmado_em >= %s AND ot.confirmado_em < %s)
                   OR (ot.confirmado_em IS NULL
                       AND COALESCE(ot.data_prevista, ot.data) BETWEEN %s AND %s))
              AND COALESCE(ot.destino_tipo, 'loja')='loja'
            GROUP BY COALESCE(
                         ot.confirmado_em::date, ot.data_prevista, ot.data
                     ),
                     ot.loja_destino, ot.produto_pastelaria_id,
                     p.tipologia, p.sabor, p.cobertura, ot.produto
        """, (data_inicio, data_fim + timedelta(days=1),
              data_inicio, data_fim))
        transfers = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT vd.data, vd.loja, vd.produto AS tipologia,
                   SUM(vd.quantidade)::integer AS unidades,
                   COALESCE(SUM(vd.valor_euros), 0)::numeric AS valor
            FROM vendas_detalhe vd
            INNER JOIN produtos_vendas_config pvc ON pvc.produto=vd.produto
            WHERE pvc.pastelaria=TRUE AND vd.data BETWEEN %s AND %s
            GROUP BY vd.data, vd.loja, vd.produto
            ORDER BY vd.data, vd.loja, vd.produto
        """, (data_inicio, data_fim))
        pastry_sales = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT vd.data, vd.loja, vd.produto AS tipologia,
                   SUM(vd.quantidade)::integer AS unidades,
                   COALESCE(SUM(vd.valor_euros), 0)::numeric AS valor
            FROM vendas_detalhe vd
            INNER JOIN produtos_vendas_config pvc ON pvc.produto=vd.produto
            WHERE pvc.pastelaria=TRUE AND vd.data BETWEEN %s AND %s
            GROUP BY vd.data, vd.loja, vd.produto
            ORDER BY vd.data, vd.loja, vd.produto
        """, (previous_start, previous_end))
        previous_pastry_sales = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT vd.data, vd.loja,
                   COALESCE(SUM(vd.valor_euros), 0)::numeric AS valor
            FROM vendas_detalhe vd
            WHERE vd.data BETWEEN %s AND %s
            GROUP BY vd.data, vd.loja
            ORDER BY vd.data, vd.loja
        """, (data_inicio, data_fim))
        total_sales = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT vd.data, vd.loja,
                   COALESCE(SUM(vd.valor_euros), 0)::numeric AS valor
            FROM vendas_detalhe vd
            WHERE vd.data BETWEEN %s AND %s
            GROUP BY vd.data, vd.loja
            ORDER BY vd.data, vd.loja
        """, (previous_start, previous_end))
        previous_total_sales = [dict(row) for row in cursor.fetchall()]

    catalogue_by_id = {row['id']: row for row in catalogue}

    def product_allowed(name, product_id=None, allow_name_match=True):
        row = catalogue_by_id.get(product_id) if product_id else None
        if product_id is None and allow_name_match and name in catalogue_by_name:
            row = catalogue_by_name[name]
        if produto and name != produto:
            return False
        if tipologia and (not row or row['tipologia'] != tipologia):
            return False
        if estado == 'ativos' and (not row or not row['ativo']):
            return False
        if estado == 'inativos' and (not row or row['ativo']):
            return False
        return True

    analysis_stores = {loja} if loja else active_sales_stores
    counts = [
        row for row in counts
        if row['loja'] in analysis_stores
        and product_allowed(
            row['produto'], row.get('produto_pastelaria_id'),
            allow_name_match=False,
        )
    ]
    for row in counts:
        catalogue_row = catalogue_by_id.get(row.get('produto_pastelaria_id'))
        row['identity_key'] = (
            f"id:{row['produto_pastelaria_id']}"
            if row.get('produto_pastelaria_id') is not None
            else f"text:{row['produto']}"
        )
        row['estado'] = (
            'Ativo' if catalogue_row and catalogue_row['ativo']
            else 'Inativo' if catalogue_row else 'Histórico'
        )
    production = [
        row for row in production
        if row['loja'] in analysis_stores
        and product_allowed(
            row['produto'], row.get('produto_pastelaria_id'),
            allow_name_match=row.get('produto_pastelaria_id') is None,
        )
    ]
    transfers = [
        row for row in transfers
        if row['loja'] in analysis_stores
        and product_allowed(
            row['produto'], row.get('produto_pastelaria_id'),
            allow_name_match=row.get('produto_pastelaria_id') is None,
        )
    ]
    available_sales_typologies = sorted({
        row['tipologia'] for row in pastry_sales + previous_pastry_sales
        if row['loja'] in analysis_stores
    })
    pastry_sales = [
        row for row in pastry_sales
        if row['loja'] in analysis_stores
        and (not tipologia_venda or row['tipologia'] == tipologia_venda)
    ]
    previous_pastry_sales = [
        row for row in previous_pastry_sales
        if row['loja'] in analysis_stores
        and (not tipologia_venda or row['tipologia'] == tipologia_venda)
    ]
    total_sales = [
        row for row in total_sales if row['loja'] in analysis_stores
    ]
    previous_total_sales = [
        row for row in previous_total_sales if row['loja'] in analysis_stores
    ]

    entries = defaultdict(lambda: defaultdict(int))
    for row in production:
        identity_key = (
            f"id:{row['produto_pastelaria_id']}"
            if row.get('produto_pastelaria_id') is not None
            else f"text:{row['produto']}"
        )
        entries[(row['loja'], identity_key)][row['data']] += int(row['quantidade'])
    for row in transfers:
        identity_key = (
            f"id:{row['produto_pastelaria_id']}"
            if row.get('produto_pastelaria_id') is not None
            else f"text:{row['produto']}"
        )
        entries[(row['loja'], identity_key)][row['data']] += int(row['quantidade'])
    entry_index = {}
    for key, day_values in entries.items():
        dates = sorted(day_values)
        cumulative = [0]
        for movement_date in dates:
            cumulative.append(cumulative[-1] + day_values[movement_date])
        entry_index[key] = (dates, cumulative)

    grouped_counts = defaultdict(list)
    for row in counts:
        grouped_counts[
            (row['loja'], row['identity_key'], row['produto'])
        ].append(row)
    intervals = []
    for (store_name, identity_key, product_name), rows in grouped_counts.items():
        rows.sort(key=lambda row: row['data'])
        for opening, closing in zip(rows, rows[1:]):
            dates, cumulative = entry_index.get(
                (store_name, identity_key), ([], [0])
            )
            start_index = bisect_right(dates, opening['data'])
            end_index = bisect_right(dates, closing['data'])
            interval_entries = (
                cumulative[end_index] - cumulative[start_index]
            )
            available = int(opening['quantidade']) + interval_entries
            raw_consumption = available - int(closing['quantidade'])
            days = (closing['data'] - opening['data']).days
            intervals.append({
                'loja': store_name,
                'identity_key': identity_key,
                'produto': product_name,
                'inicio': opening['data'],
                'fim': closing['data'],
                'dias': days,
                'stock_inicial': int(opening['quantidade']),
                'entradas_conhecidas': interval_entries,
                'stock_final': int(closing['quantidade']),
                'consumo_estimado': max(0, raw_consumption),
                'taxa_rotacao': round(
                    max(0, raw_consumption) * 100 / available, 1
                ) if available > 0 else None,
                'confianca': 'baixa',
                'comparavel': raw_consumption >= 0 and 5 <= days <= 14,
                'inconsistencia': raw_consumption < 0,
            })

    usable_intervals = [
        row for row in intervals if not row['inconsistencia']
    ]
    rotation_by_product = defaultdict(lambda: {
        'consumo_estimado': 0, 'intervalos': 0, 'intervalos_baixa': 0,
        'stock_final_por_loja': {},
    })
    for row in usable_intervals:
        item = rotation_by_product[row['identity_key']]
        item['produto'] = row['produto']
        item['consumo_estimado'] += row['consumo_estimado']
        item['intervalos'] += 1
        item['intervalos_baixa'] += row['confianca'] == 'baixa'
        previous = item['stock_final_por_loja'].get(row['loja'])
        if previous is None or row['fim'] > previous['fim']:
            item['stock_final_por_loja'][row['loja']] = {
                'fim': row['fim'], 'stock_final': row['stock_final'],
            }
    labels_per_identity = defaultdict(set)
    for identity_key, values in rotation_by_product.items():
        labels_per_identity[values['produto']].add(identity_key)
    rotation_ranking = []
    for identity_key, values in rotation_by_product.items():
        product_name = values['produto']
        if (
            identity_key.startswith('text:')
            and len(labels_per_identity[product_name]) > 1
        ):
            product_name = f'{product_name} (histórico sem associação)'
        rotation_ranking.append({
            'identity_key': identity_key,
            'produto': product_name,
            'consumo_estimado': values['consumo_estimado'],
            'intervalos': values['intervalos'],
            'intervalos_baixa': values['intervalos_baixa'],
            'stock_final': sum(
                item['stock_final']
                for item in values['stock_final_por_loja'].values()
            ),
        })
    rotation_ranking.sort(
        key=lambda row: (-row['consumo_estimado'], row['produto'])
    )

    sales_by_type = defaultdict(lambda: {'unidades': 0, 'valor': 0.0})
    sales_daily = defaultdict(lambda: {'unidades': 0, 'valor': 0.0})
    for row in pastry_sales:
        sales_by_type[row['tipologia']]['unidades'] += int(row['unidades'])
        sales_by_type[row['tipologia']]['valor'] += float(row['valor'])
        sales_daily[row['data']]['unidades'] += int(row['unidades'])
        sales_daily[row['data']]['valor'] += float(row['valor'])
    sales_ranking = [
        {'tipologia': name, 'unidades': values['unidades'],
         'valor': round(values['valor'], 2)}
        for name, values in sales_by_type.items()
    ]
    sales_ranking.sort(key=lambda row: (-row['valor'], row['tipologia']))
    previous_by_type = defaultdict(lambda: {'unidades': 0, 'valor': 0.0})
    for row in previous_pastry_sales:
        previous_by_type[row['tipologia']]['unidades'] += int(row['unidades'])
        previous_by_type[row['tipologia']]['valor'] += float(row['valor'])
    sales_comparison = []
    for name in sorted(set(sales_by_type) | set(previous_by_type)):
        current = sales_by_type[name]
        previous = previous_by_type[name]
        current_value = round(current['valor'], 2)
        previous_value = round(previous['valor'], 2)
        sales_comparison.append({
            'tipologia': name,
            'valor_atual': current_value,
            'valor_anterior': previous_value,
            'variacao': round(
                (current_value - previous_value) * 100 / previous_value, 1
            ) if previous_value else None,
            'unidades_atuais': current['unidades'],
            'unidades_anteriores': previous['unidades'],
        })
    sales_comparison.sort(
        key=lambda row: (-row['valor_atual'], row['tipologia'])
    )
    sales_series = [
        {'data': day, 'unidades': values['unidades'],
         'valor': round(values['valor'], 2)}
        for day, values in sorted(sales_daily.items())
    ]

    pastry_value = round(sum(float(row['valor']) for row in pastry_sales), 2)
    total_value = round(sum(float(row['valor']) for row in total_sales), 2)
    sales_days = {row['data'] for row in total_sales}
    sales_store_days = {(row['data'], row['loja']) for row in total_sales}
    previous_sales_days = {row['data'] for row in previous_total_sales}
    previous_sales_store_days = {
        (row['data'], row['loja']) for row in previous_total_sales
    }
    calendar_days = (data_fim - data_inicio).days + 1
    previous_calendar_days = (previous_end - previous_start).days + 1
    coverage_by_store = []
    coverage_store_names = sorted(analysis_stores)
    for store_name in coverage_store_names:
        store_days = {
            row['data'] for row in total_sales if row['loja'] == store_name
        }
        coverage_by_store.append({
            'loja': store_name,
            'dias': len(store_days),
            'cobertura': round(
                len(store_days) * 100 / calendar_days, 1
            ) if calendar_days else 0,
        })
    store_count = len(coverage_store_names)
    possible_store_days = calendar_days * store_count
    previous_possible_store_days = previous_calendar_days * store_count
    coverage = round(
        len(sales_store_days) * 100 / possible_store_days, 1
    ) if possible_store_days else 0
    previous_coverage = round(
        len(previous_sales_store_days) * 100 / previous_possible_store_days, 1
    ) if previous_possible_store_days else 0
    comparison_comparable = (
        coverage >= 70
        and previous_coverage >= 70
        and abs(coverage - previous_coverage) <= 10
    )
    if not comparison_comparable:
        for row in sales_comparison:
            row['variacao'] = None

    product_options = sorted({
        row['nome'] for row in catalogue
    } | {row['produto'] for row in counts})
    typology_options = sorted({
        row['tipologia'] for row in catalogue if row['tipologia']
    })
    return {
        'stores': stores,
        'products': product_options,
        'typologies': typology_options,
        'sales_typologies': available_sales_typologies,
        'catalogue': catalogue,
        'counts': counts,
        'intervals': sorted(intervals, key=lambda row: row['fim']),
        'rotation_ranking': rotation_ranking,
        'sales_ranking': sales_ranking,
        'sales_comparison': sales_comparison,
        'sales_series': sales_series,
        'coverage_by_store': coverage_by_store,
        'previous_period': {
            'start': previous_start, 'end': previous_end,
        },
        'summary': {
            'count_snapshots': len({(r['data'], r['loja']) for r in counts}),
            'count_cells': len(counts),
            'rotation_intervals': len(usable_intervals),
            'inconsistent_intervals': len(intervals) - len(usable_intervals),
            'estimated_consumption': sum(
                row['consumo_estimado'] for row in usable_intervals
            ),
            'pastry_units': sum(int(row['unidades']) for row in pastry_sales),
            'pastry_value': pastry_value,
            'total_sales_value': total_value,
            'pastry_share': round(pastry_value * 100 / total_value, 1)
            if total_value else None,
            'sales_days': len(sales_days),
            'calendar_days': calendar_days,
            'sales_store_days': len(sales_store_days),
            'possible_store_days': possible_store_days,
            'sales_coverage': coverage,
            'previous_sales_days': len(previous_sales_days),
            'previous_sales_coverage': previous_coverage,
            'comparison_comparable': comparison_comparable,
        },
    }


def save_pastelaria_stock_minimums(values):
    """Persist validated integer minimums in one transaction."""
    with db_connection() as conn:
        cursor = conn.cursor()
        for product_id, store_id, quantity in values:
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
                raise ValueError('Os stocks mínimos devem ser números inteiros não negativos.')
            cursor.execute("""
                INSERT INTO pastelaria_stock_minimos
                    (produto_id, store_id, quantidade_minima, updated_at)
                SELECT p.id, s.id, %s, NOW()
                FROM produtos_pastelaria p, stores s
                WHERE p.id=%s AND p.ativo=TRUE
                  AND s.id=%s AND s.is_active=TRUE AND s.supports_vendas=TRUE
                ON CONFLICT (produto_id, store_id) DO UPDATE SET
                    quantidade_minima=EXCLUDED.quantidade_minima,
                    updated_at=NOW()
            """, (quantity, product_id, store_id))
        conn.commit()


def _pastelaria_priority_inputs(cursor, count_date):
    if count_date.weekday() != 6:
        raise ValueError('O plano só pode ser gerado a partir de uma contagem de domingo.')
    cursor.execute("""
        SELECT id, name FROM stores
        WHERE supports_vendas=TRUE AND is_active=TRUE ORDER BY name
    """)
    stores = cursor.fetchall()
    cursor.execute("""
        SELECT id, tipologia, sabor, cobertura
        FROM produtos_pastelaria WHERE ativo=TRUE
        ORDER BY tipologia, sabor, cobertura
    """)
    products = cursor.fetchall()
    cursor.execute("""
        SELECT DISTINCT ON (cs.loja, COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto))
               cs.loja, cs.produto, cs.quantidade, cs.produto_pastelaria_id
        FROM contagem_stock cs
        WHERE tipo='pastelaria' AND data=%s
        ORDER BY cs.loja, COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto), cs.id DESC
    """, (count_date,))
    counts = {
        (row['loja'], row.get('produto_pastelaria_id') or row['produto']):
            int(row['quantidade'])
        for row in cursor.fetchall()
    }
    cursor.execute("""
        SELECT produto_id, store_id, quantidade_minima
        FROM pastelaria_stock_minimos
    """)
    minimums = {
        (row['produto_id'], row['store_id']): int(row['quantidade_minima'])
        for row in cursor.fetchall()
    }
    missing = []
    missing_minimums = []
    rows = []
    for product in products:
        label = _pastelaria_product_label(product)
        distribution = []
        total_minimum = total_count = total_need = 0
        for store in stores:
            key = (store['name'], product['id'])
            minimum_key = (product['id'], store['id'])
            if minimum_key not in minimums:
                missing_minimums.append({
                    'loja': store['name'], 'produto': label,
                })
            if key not in counts:
                missing.append({'loja': store['name'], 'produto': label})
                continue
            minimum = minimums.get(minimum_key, 0)
            counted = counts[key]
            need = max(minimum - counted, 0)
            total_minimum += minimum
            total_count += counted
            total_need += need
            distribution.append({
                'loja': store['name'], 'minimo': minimum,
                'contado': counted, 'necessidade': need,
            })
        if total_need > 0 and len(distribution) == len(stores):
            rows.append({
                'produto_id': product['id'], 'produto': label,
                'stock_minimo_total': total_minimum,
                'stock_contado_total': total_count,
                'quantidade_total': total_need,
                'percentagem_falta': total_need / total_minimum if total_minimum else 0,
                'distribuicao_lojas': distribution,
            })
    rows.sort(key=lambda row: (
        -row['percentagem_falta'], -row['quantidade_total'],
        row['produto'].casefold(),
    ))
    for index, row in enumerate(rows, 1):
        row['prioridade'] = index
    return {
        'data_contagem': count_date, 'stores': stores, 'missing': missing,
        'missing_minimums': missing_minimums,
        'complete': (
            not missing and not missing_minimums
            and bool(stores) and bool(products)
        ),
        'rows': rows,
    }


def get_pastelaria_priority_status(count_date):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-count:{count_date.isoformat()}',),
        )
        return _pastelaria_priority_inputs(cursor, count_date)


def generate_pastelaria_priority_plan(count_date, actor=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-plan:{count_date.isoformat()}',),
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-count:{count_date.isoformat()}',),
        )
        data = _pastelaria_priority_inputs(cursor, count_date)
        if not data['complete']:
            raise ValueError(
                'Faltam contagens ou stocks mínimos para este domingo.'
            )
        cursor.execute("""
            SELECT COALESCE(MAX(versao), 0) + 1 AS next_version
            FROM pastelaria_planos_prioridade
            WHERE data_contagem=%s
        """, (count_date,))
        version = cursor.fetchone()['next_version']
        cursor.execute("""
            INSERT INTO pastelaria_planos_prioridade
                (data_contagem, versao, generated_by, generated_at)
            VALUES (%s,%s,%s,NOW())
            RETURNING id
        """, (count_date, version, actor))
        plan_id = cursor.fetchone()['id']
        for row in data['rows']:
            cursor.execute("""
                INSERT INTO pastelaria_plano_prioridade_linhas
                    (plano_id,produto_id,produto,stock_minimo_total,
                     stock_contado_total,quantidade_total,percentagem_falta,
                     prioridade,distribuicao_lojas)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """, (
                plan_id, row['produto_id'], row['produto'],
                row['stock_minimo_total'], row['stock_contado_total'],
                row['quantidade_total'], row['percentagem_falta'],
                row['prioridade'], json.dumps(row['distribuicao_lojas']),
            ))
        conn.commit()
        return plan_id


def get_pastelaria_priority_plan(plan_id=None, count_date=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if plan_id is not None:
            cursor.execute(
                "SELECT * FROM pastelaria_planos_prioridade WHERE id=%s", (plan_id,)
            )
        else:
            cursor.execute("""
                SELECT * FROM pastelaria_planos_prioridade
                WHERE data_contagem=%s ORDER BY versao DESC LIMIT 1
            """, (count_date,))
        plan = cursor.fetchone()
        if not plan:
            return None
        cursor.execute("""
            SELECT * FROM pastelaria_plano_prioridade_linhas
            WHERE plano_id=%s ORDER BY prioridade
        """, (plan['id'],))
        plan['rows'] = cursor.fetchall()
        return plan


def list_pastelaria_priority_plans(limit=100):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT p.id, p.data_contagem, p.versao, p.generated_by,
                   p.generated_at, COUNT(l.id) AS line_count
            FROM pastelaria_planos_prioridade p
            LEFT JOIN pastelaria_plano_prioridade_linhas l
              ON l.plano_id=p.id
            GROUP BY p.id
            ORDER BY p.data_contagem DESC, p.versao DESC
            LIMIT %s
        """, (limit,))
        return cursor.fetchall()

def add_produto_pastelaria(tipologia: str, sabor: str = '', cobertura: str = ''):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO produtos_pastelaria (tipologia, sabor, cobertura) VALUES (%s, %s, %s)", (tipologia, sabor, cobertura))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_produto_pastelaria(id: int, tipologia: str, sabor: str = '', cobertura: str = ''):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos_pastelaria SET tipologia = %s, sabor = %s, cobertura = %s WHERE id = %s", (tipologia, sabor, cobertura, id))
        conn.commit()


def save_produtos_pastelaria_active(
    states, expected_token, actor_id=None, actor_username=None
):
    """Persist active flags and audit each successful state transition.

    The product rows are locked before checking ``expected_token``.  This
    means a stale or concurrent submission exits before either product updates
    or audit inserts are made.
    """
    normalized = {}
    for product_id, active in states:
        if not isinstance(product_id, int) or not isinstance(active, bool):
            raise ValueError('Estado de produto inválido.')
        normalized[product_id] = active
    if not normalized:
        raise ValueError('Nenhum estado de produto recebido.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT id, ativo FROM produtos_pastelaria ORDER BY id FOR UPDATE"
        )
        current_rows = cursor.fetchall()
        current = {row['id']: row['ativo'] for row in current_rows}
        if not expected_token or pastelaria_product_state_token(
            current_rows
        ) != expected_token:
            raise ValueError(
                'Os estados foram alterados por outro utilizador. '
                'Atualize a página antes de guardar.'
            )
        if set(current) != set(normalized):
            raise ValueError('Um ou mais produtos já não existem.')
        changed = 0
        for product_id, active in normalized.items():
            if current[product_id] != active:
                previous_active = current[product_id]
                cursor.execute(
                    "UPDATE produtos_pastelaria SET ativo=%s WHERE id=%s",
                    (active, product_id),
                )
                cursor.execute("""
                    INSERT INTO pastelaria_produto_estado_audit
                        (produto_id, actor_id, actor_username,
                         previous_active, new_active)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    product_id,
                    actor_id,
                    str(actor_username or 'sistema')[:100],
                    previous_active,
                    active,
                ))
                changed += 1
        conn.commit()
    return changed


def delete_produto_pastelaria(id: int):
    """Reject the retired hard-delete API for catalogue products.

    Product rows are historical identities. Operational removal must use the
    ``ativo`` flag so saved minimums, counts, plans and movements remain
    traceable. This guard remains for stale callers and is intentionally not an
    administrative cleanup mechanism.
    """
    raise ValueError(
        'A eliminação permanente de produtos de Pastelaria está bloqueada; '
        'marque o produto como Inativo.'
    )


def delete_produtos_pastelaria_bulk(ids: list):
    """Reject the retired bulk hard-delete API; see ``delete_produto_pastelaria``."""
    raise ValueError(
        'A eliminação permanente de produtos de Pastelaria está bloqueada; '
        'marque os produtos como Inativos.'
    )

def get_gelado_por_tipologia() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, tipologia, quantidade_gelado_g FROM gelado_por_tipologia ORDER BY tipologia")
        return [{'id': row[0], 'tipologia': row[1], 'quantidade_gelado_g': row[2] or 0} for row in cursor.fetchall()]

def add_gelado_por_tipologia(tipologia: str, quantidade_gelado_g: float = 0):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO gelado_por_tipologia (tipologia, quantidade_gelado_g) VALUES (%s, %s)", (tipologia, quantidade_gelado_g))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_gelado_por_tipologia(id: int, tipologia: str, quantidade_gelado_g: float = 0):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE gelado_por_tipologia SET tipologia = %s, quantidade_gelado_g = %s WHERE id = %s", (tipologia, quantidade_gelado_g, id))
        conn.commit()

def delete_gelado_por_tipologia(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM gelado_por_tipologia WHERE id = %s", (id,))
        conn.commit()

def get_gelado_peso_by_tipologia(tipologia_nome: str) -> float:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(
                (
                    SELECT hist.gramas
                    FROM gramas_gelado_historico hist
                    WHERE LOWER(BTRIM(hist.artigo))=LOWER(BTRIM(%s))
                      AND CURRENT_DATE BETWEEN hist.valid_from
                          AND COALESCE(hist.valid_to, 'infinity'::date)
                      AND hist.tipo_dose='fixa'
                    ORDER BY hist.valid_from DESC LIMIT 1
                ),
                (
                    SELECT quantidade_gelado_g
                    FROM gelado_por_tipologia
                    WHERE tipologia=%s
                ),
                0
            )
        """, (tipologia_nome, tipologia_nome))
        row = cursor.fetchone()
    return row[0] if row else 0

def update_gelado_peso_by_tipologia(tipologia_nome: str, quantidade_gelado_g: float = 0):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO gelado_por_tipologia (tipologia, quantidade_gelado_g) VALUES (%s, %s) ON CONFLICT (tipologia) DO UPDATE SET quantidade_gelado_g = %s", (tipologia_nome, quantidade_gelado_g, quantidade_gelado_g))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()

def get_coberturas() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM coberturas WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_coberturas() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, ativo FROM coberturas ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]

def add_cobertura(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO coberturas (nome) VALUES (%s)", (nome,))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_cobertura(id: int, nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE coberturas SET nome = %s WHERE id = %s", (nome, id))
        conn.commit()

def delete_cobertura(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM coberturas WHERE id = %s", (id,))
        conn.commit()

def get_produtos_rececao() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome, tipo FROM produtos_rececao WHERE ativo = TRUE ORDER BY nome")
        return [{'nome': row[0], 'tipo': row[1]} for row in cursor.fetchall()]

def get_all_produtos_rececao() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, tipo, ativo FROM produtos_rececao ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'tipo': row[2], 'ativo': row[3]} for row in cursor.fetchall()]

def add_produto_rececao(nome: str, tipo: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO produtos_rececao (nome, tipo) VALUES (%s, %s)", (nome, tipo))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_produto_rececao(id: int, nome: str, tipo: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos_rececao SET nome = %s, tipo = %s WHERE id = %s", (nome, tipo, id))
        conn.commit()

def delete_produto_rececao(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM produtos_rececao WHERE id = %s", (id,))
        conn.commit()

@ttl_cache('receitas_gelado', ttl=600)
def get_receitas_gelado() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM receitas_gelado WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_receitas_gelado() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, nome_corrente, ativo, COALESCE(conta_eurokg, TRUE) FROM receitas_gelado ORDER BY COALESCE(nome_corrente, nome)")
        return [{'id': row[0], 'nome': row[1], 'nome_corrente': row[2] or '', 'ativo': row[3], 'conta_eurokg': row[4]} for row in cursor.fetchall()]

def add_receita_gelado(nome: str, nome_corrente: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO receitas_gelado (nome, nome_corrente) VALUES (%s, %s)", (nome, nome_corrente))
            conn.commit()
            success = True
        except psycopg2.IntegrityError:
            conn.rollback()
            success = False
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')
    return success

def update_receita_gelado(id: int, nome: str, nome_corrente: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE receitas_gelado SET nome = %s, nome_corrente = %s WHERE id = %s", (nome, nome_corrente, id))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def update_receita_gelado_ativo(id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE receitas_gelado SET ativo = %s WHERE id = %s", (ativo, id))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def update_receita_gelado_conta_eurokg(id: int, conta_eurokg: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE receitas_gelado SET conta_eurokg = %s WHERE id = %s", (conta_eurokg, id))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def delete_receita_gelado(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM receitas_gelado WHERE id = %s", (id,))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def add_rececao_mercadoria(data: date, loja: str, tipo_produto: str, quantidade: float, unidade: str, produto: str = None, sabor: str = None, lote: str = None):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO rececao_mercadoria (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade, store_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade, store_id))
        conn.commit()

def get_rececao_mercadoria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    query = "SELECT * FROM rececao_mercadoria WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC, id DESC"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def delete_rececao_mercadoria(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rececao_mercadoria WHERE id = %s", (id,))
        conn.commit()

def add_venda_detalhe(data: date, loja: str, produto: str, quantidade: float, categoria: str = None,
                       valor_euros: float = None, valor_sem_iva_euros: float = None,
                       peso_vendido_kg: float = None):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO produtos_vendas_config (produto, dose_config_pendente)
            SELECT %s, EXISTS (
                SELECT 1 FROM regras_negocio
                WHERE area='gelado_kpi'
                  AND LOWER(%s) LIKE '%%' || LOWER(palavra_chave) || '%%'
            )
            ON CONFLICT (produto) DO UPDATE SET produto=EXCLUDED.produto
            RETURNING id
        """, (produto, produto))
        produto_config_id = cursor.fetchone()[0]
        cursor.execute('''
            INSERT INTO vendas_detalhe (
                data, loja, produto, categoria, quantidade, valor_euros,
                store_id, valor_sem_iva_euros, peso_vendido_kg,
                produto_vendas_config_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (data, loja, produto, categoria, quantidade, valor_euros,
              store_id, valor_sem_iva_euros, peso_vendido_kg, produto_config_id))
        conn.commit()


def add_venda_detalhe_batch(records: list, pre_delete_pairs: list = None) -> int:
    """Insert multiple vendas_detalhe records in a single transaction.

    Each record is a dict with keys: data, loja, produto, quantidade,
    categoria (optional), valor_euros (optional), peso_vendido_kg (optional),
    valor_sem_iva_euros (optional —
    the real net-of-VAT amount, when the source file provides it; NULL means the
    row's IVA is estimated rather than real, see compute_vat_period).

    If ``pre_delete_pairs`` is provided (list of ``(date, loja)`` tuples),
    those rows are deleted atomically before the insert so that replace
    semantics are safe: if the insert fails the delete is also rolled back.

    Returns the number of rows inserted.
    """
    if not records and not pre_delete_pairs:
        return 0
    store_id_cache = {}
    rows = []
    for rec in records:
        loja = rec['loja']
        if loja not in store_id_cache:
            store_id_cache[loja] = get_store_id_by_name(loja)
        rows.append((
            rec['data'],
            loja,
            rec['produto'],
            rec.get('categoria'),
            rec['quantidade'],
            rec.get('valor_euros'),
            store_id_cache[loja],
            rec.get('valor_sem_iva_euros'),
            rec.get('peso_vendido_kg'),
        ))
    if pre_delete_pairs and not rows:
        raise ValueError(
            f"pre_delete_pairs contains {len(pre_delete_pairs)} pair(s) but the "
            "parsed batch is empty — refusing to delete existing data without replacement. "
            "Check that the file contains valid sales rows for the expected lojas."
        )
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            product_ids = {}
            for product in sorted({row[2] for row in rows}):
                cursor.execute("""
                    INSERT INTO produtos_vendas_config
                        (produto, dose_config_pendente)
                    SELECT %s, EXISTS (
                        SELECT 1 FROM regras_negocio
                        WHERE area='gelado_kpi'
                          AND LOWER(%s) LIKE
                              '%%' || LOWER(palavra_chave) || '%%'
                    )
                    ON CONFLICT (produto) DO UPDATE SET produto=EXCLUDED.produto
                    RETURNING id
                """, (product, product))
                product_ids[product] = cursor.fetchone()[0]
            if pre_delete_pairs:
                for d, loja in pre_delete_pairs:
                    cursor.execute(
                        "DELETE FROM vendas_detalhe WHERE data = %s AND loja = %s",
                        (d, loja)
                    )
            if rows:
                execute_values(
                    cursor,
                    '''INSERT INTO vendas_detalhe
                       (data, loja, produto, categoria, quantidade, valor_euros,
                         store_id, valor_sem_iva_euros, peso_vendido_kg,
                         produto_vendas_config_id)
                       VALUES %s''',
                    [row + (product_ids[row[2]],) for row in rows],
                    page_size=500,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return len(rows)


def _apply_vendas_weight_rules(records: list, history: list) -> list:
    enriched = []
    for record in records:
        item = dict(record)
        if item.get('peso_vendido_kg') is None:
            sale_date = item.get('data')
            rule = next((
                row for row in history
                if row.get('produto') == item.get('produto')
                and (
                    row.get('valid_from') is None
                    or row['valid_from'] <= sale_date
                )
                and (
                    row.get('valid_to') is None
                    or row['valid_to'] >= sale_date
                )
            ), None)
            if rule and rule.get('tipo_dose') == 'peso':
                quantity = float(item.get('quantidade'))
                if not math.isfinite(quantity) or quantity == 0:
                    raise ValueError(
                        'A quantidade vendida ao peso tem de ser um número '
                        'finito e diferente de zero.'
                    )
                item['peso_vendido_kg'] = quantity
        enriched.append(item)
    return enriched


def apply_vendas_weight_rules(records: list) -> list:
    """Copy actual kg from Quantidade for rows historically configured by weight."""
    if not records:
        return records
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT pvc.produto, hist.tipo_dose,
                   prd.valid_from, prd.valid_to
            FROM produto_regra_dose_historico prd
            JOIN produtos_vendas_config pvc
              ON pvc.id=prd.produto_vendas_config_id
            JOIN gramas_gelado_historico hist
              ON hist.id=prd.regra_dose_id
        """)
        history = [dict(row) for row in cursor.fetchall()]
    return _apply_vendas_weight_rules(records, history)


def get_vendas_detalhe_df(loja: str = None, data_inicio: date = None, data_fim: date = None, categoria: str = None, limit: int = 500) -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = "SELECT * FROM vendas_detalhe WHERE 1=1"
        params = []
        if loja:
            query += " AND loja = %s"
            params.append(loja)
        if data_inicio:
            query += " AND data >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND data <= %s"
            params.append(data_fim)
        if categoria:
            query += " AND categoria = %s"
            params.append(categoria)
        query += " ORDER BY data DESC, id DESC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]

def _lock_pesagem_days(cursor, store_id, days):
    if not store_id:
        return
    for day in sorted(set(days)):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f'pesagem-day:{store_id}:{day.isoformat()}',),
        )


def _ensure_pesagem_days_not_justified(cursor, store_id, days):
    if not store_id:
        return
    unique_days = sorted(set(days))
    if not unique_days:
        return
    cursor.execute("""
        SELECT data
        FROM pesagem_day_justifications
        WHERE store_id = %s AND data = ANY(%s)
        LIMIT 1
    """, (store_id, unique_days))
    row = cursor.fetchone()
    if row:
        raise ValueError(
            'Este dia foi justificado como sem pesagem e não aceita '
            'novos registos.'
        )


def _pesagem_stock_snapshot(row):
    if not row:
        return None
    return {
        'id': row.get('id'),
        'data': (
            row.get('data').isoformat()
            if row.get('data') else None
        ),
        'sabor': row.get('sabor'),
        'quantidade_kg': (
            float(row.get('quantidade_kg'))
            if row.get('quantidade_kg') is not None else None
        ),
        'tipo': row.get('tipo'),
        'local': row.get('local'),
        'is_active': bool(row.get('is_active', True)),
    }


def _write_pesagem_audit(
    cursor,
    event_type,
    loja,
    origin,
    actor_id=None,
    actor_username='sistema',
    store_id=None,
    event_date=None,
    stock_id=None,
    batch_id=None,
    reason=None,
    affected_count=0,
    expected_count=None,
    before_data=None,
    after_data=None,
    outcome='success',
    safe_cause=None,
):
    cursor.execute("""
        INSERT INTO pesagem_audit_events (
            event_type, outcome, stock_id, batch_id, store_id,
            loja, event_date, actor_id, actor_username, origin,
            reason, affected_count, expected_count,
            before_data, after_data, safe_cause
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
    """, (
        event_type, outcome, stock_id, batch_id, store_id,
        loja, event_date, actor_id,
        (actor_username or 'sistema')[:100], origin,
        reason, affected_count, expected_count,
        Json(before_data) if before_data is not None else None,
        Json(after_data) if after_data is not None else None,
        (safe_cause or '')[:500] or None,
    ))


def add_stock_gelado(
    data: date,
    loja: str,
    sabor: str,
    quantidade_kg: float,
    tipo: str,
    local: str = None,
    actor_id=None,
    actor_username: str = 'sistema',
    origin: str = 'system',
    batch_id=None,
) -> int:
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if tipo == 'fim':
            _lock_pesagem_days(cursor, store_id, [data])
            _ensure_pesagem_days_not_justified(
                cursor, store_id, [data]
            )
        cursor.execute('''
            INSERT INTO stock_gelado (
                data, loja, sabor, quantidade_kg, tipo, local,
                store_id, source_batch_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        ''', (
            data, loja, sabor, quantidade_kg, tipo, local,
            store_id, batch_id,
        ))
        row = cursor.fetchone()
        new_id = row['id']
        _write_pesagem_audit(
            cursor, 'create', loja, origin,
            actor_id=actor_id, actor_username=actor_username,
            store_id=store_id, event_date=data, stock_id=new_id,
            batch_id=batch_id, affected_count=1,
            after_data=_pesagem_stock_snapshot(row),
        )
        conn.commit()
    return new_id


def add_stock_gelado_carapinas(stock_gelado_id: int, carapinas: list) -> None:
    """Save individual carapina weights for a stock_gelado record.

    Only stores data when there are 2 or more carapinas — a single carapina
    value adds no information beyond the total already stored in stock_gelado.
    """
    if not carapinas or len(carapinas) < 2:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        execute_values(
            cursor,
            "INSERT INTO stock_gelado_carapinas (stock_gelado_id, carapina_numero, quantidade_kg) VALUES %s",
            [(stock_gelado_id, i + 1, float(kg)) for i, kg in enumerate(carapinas)],
        )
        conn.commit()


def get_carapinas_for_stock_ids(stock_ids: list) -> dict:
    """Return carapina breakdown for a list of stock_gelado IDs.

    Returns {stock_id: [kg1, kg2, ...]} ordered by carapina_numero.
    Only IDs that have carapina records are included in the result.
    """
    if not stock_ids:
        return {}
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT stock_gelado_id, quantidade_kg
            FROM stock_gelado_carapinas
            WHERE stock_gelado_id = ANY(%s)
            ORDER BY stock_gelado_id, carapina_numero
        """, (list(stock_ids),))
        rows = cursor.fetchall()
    result: dict = {}
    for stock_id, kg in rows:
        if stock_id not in result:
            result[stock_id] = []
        result[stock_id].append(float(kg))
    return result


def add_stock_gelado_bulk(
    entries: list,
    loja: str,
    actor_id=None,
    actor_username: str = 'sistema',
    origin: str = 'bulk',
) -> int:
    """
    Insert multiple stock_gelado records atomically in a single transaction.
    Each entry is a dict with keys: data (date), sabor (str), quantidade_kg (float), tipo (str).
    Returns the number of rows inserted.
    Raises on any DB error — no partial writes.

    Note: this function inserts directly via execute_values rather than looping
    add_stock_gelado, because each call to add_stock_gelado opens its own connection
    and commits immediately, making atomicity impossible. If add_stock_gelado ever
    gains pre-insert business logic (e.g. duplicate checks, quota validation), that
    logic should be replicated or extracted into a shared helper and applied here too.
    """
    if not entries:
        return 0
    store_id = get_store_id_by_name(loja)
    rows = [
        (e['data'], loja, e['sabor'], e['quantidade_kg'], e['tipo'], None, store_id)
        for e in entries
    ]
    with db_connection() as conn:
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            _lock_pesagem_days(
                cursor,
                store_id,
                [entry['data'] for entry in entries if entry['tipo'] == 'fim'],
            )
            _ensure_pesagem_days_not_justified(
                cursor,
                store_id,
                [entry['data'] for entry in entries if entry['tipo'] == 'fim'],
            )
            inserted_rows = execute_values(
                cursor,
                '''INSERT INTO stock_gelado (
                       data, loja, sabor, quantidade_kg, tipo, local, store_id
                   )
                   VALUES %s
                   ON CONFLICT DO NOTHING
                   RETURNING *''',
                rows,
                fetch=True,
            )
            for row in inserted_rows:
                _write_pesagem_audit(
                    cursor, 'create', loja, origin,
                    actor_id=actor_id, actor_username=actor_username,
                    store_id=store_id, event_date=row['data'],
                    stock_id=row['id'], affected_count=1,
                    after_data=_pesagem_stock_snapshot(row),
                )
            inserted = len(inserted_rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return inserted


def _pesagem_batch_dict(row, entries):
    if not row:
        return None
    result = dict(row)
    for key in ('id',):
        if result.get(key) is not None:
            result[key] = str(result[key])
    for key in ('created_at', 'updated_at', 'confirmed_at'):
        if result.get(key) is not None:
            result[key] = result[key].isoformat()
    result['entries'] = []
    for entry in entries:
        entry_date = entry['data']
        if hasattr(entry_date, 'isoformat'):
            data_iso = entry_date.isoformat()
            data_label = entry_date.strftime('%d/%m/%Y')
        else:
            data_iso = str(entry_date)
            try:
                data_label = date.fromisoformat(data_iso).strftime('%d/%m/%Y')
            except (TypeError, ValueError):
                data_label = data_iso
        item = {
            'data': data_iso,
            'sabor': entry['sabor'],
            'quantidade_kg': float(entry['quantidade_kg']),
            'suspeito': bool(entry.get('suspeito', False)),
        }
        if entry.get('stock_id') is not None:
            item['stock_id'] = int(entry['stock_id'])
        result['entries'].append(item)
    result['dates'] = sorted({
        (
            entry['data'].strftime('%d/%m/%Y')
            if hasattr(entry['data'], 'strftime')
            else date.fromisoformat(str(entry['data'])).strftime('%d/%m/%Y')
        )
        for entry in entries
    })
    return result


def _load_pesagem_batch(cursor, loja: str, batch_id=None, open_only=False):
    cursor.execute(
        "SELECT id FROM stores WHERE lower(name) = lower(%s) LIMIT 1",
        (loja,),
    )
    store = cursor.fetchone()
    requested_store_id = store['id'] if store else None
    params = [requested_store_id, requested_store_id, loja]
    query = """
         SELECT b.id, b.loja, b.store_id, b.status,
               b.expected_count, b.revision, b.inserted_count,
               b.created_by_id, b.created_by, b.updated_by, b.error_message,
                b.created_at, b.updated_at, b.confirmed_at,
                b.registered_at, b.registered_by_id, b.registered_by,
                b.receipt_snapshot,
               to_char(
                   b.confirmed_at AT TIME ZONE 'Europe/Lisbon',
                   'DD/MM/YYYY HH24:MI'
               ) AS confirmed_at_local
        FROM pesagem_draft_batches b
        WHERE (
            (%s IS NOT NULL AND b.store_id = %s)
            OR (b.store_id IS NULL AND b.loja = %s)
        )
    """
    if batch_id is not None:
        query += " AND b.id = %s"
        params.append(str(batch_id))
    if open_only:
        query += (
            " AND b.status IN "
            "('draft', 'registering', 'registered', 'confirming', 'failed')"
        )
    query += " ORDER BY b.updated_at DESC LIMIT 1"
    cursor.execute(query, params)
    batch = cursor.fetchone()
    if not batch:
        return None
    if batch['status'] == 'confirmed' and batch.get('receipt_snapshot'):
        entries = batch['receipt_snapshot']
    elif batch['status'] == 'registered':
        cursor.execute("""
            SELECT e.data, e.sabor, e.quantidade_kg, e.suspeito,
                   e.stock_id,
                   COALESCE(sg.data, e.data) AS current_data,
                   COALESCE(sg.sabor, e.sabor) AS current_sabor,
                   COALESCE(sg.quantidade_kg, e.quantidade_kg)
                       AS current_quantidade_kg
            FROM pesagem_draft_entries e
            LEFT JOIN stock_gelado sg
              ON sg.id = e.stock_id
             AND sg.source_batch_id = e.batch_id
             AND sg.is_active = TRUE
            WHERE e.batch_id = %s
            ORDER BY e.position
        """, (batch['id'],))
        entries = []
        for entry in cursor.fetchall():
            entry = dict(entry)
            entry['data'] = entry['current_data']
            entry['sabor'] = entry['current_sabor']
            entry['quantidade_kg'] = entry['current_quantidade_kg']
            entries.append(entry)
    else:
        cursor.execute("""
            SELECT data, sabor, quantidade_kg, suspeito, stock_id
            FROM pesagem_draft_entries
            WHERE batch_id = %s
            ORDER BY position
        """, (batch['id'],))
        entries = cursor.fetchall()
    return _pesagem_batch_dict(batch, entries)


def get_open_pesagem_draft(loja: str):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        return _load_pesagem_batch(cursor, loja, open_only=True)


def save_pesagem_draft(
    batch_id,
    expected_revision,
    loja: str,
    store_id,
    actor_id,
    actor_username: str,
    entries: list,
):
    """Replace a store's open manual weighing draft atomically."""
    import uuid

    requested_id = str(
        uuid.UUID(str(batch_id)) if batch_id else uuid.uuid4()
    )
    username = (actor_username or 'sistema')[:100]
    resolved_store_id = store_id or get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (
                    f'pesagem-draft-store:{resolved_store_id}'
                    if resolved_store_id
                    else f'pesagem-draft-legacy:{loja.lower()}',
                ),
            )
            cursor.execute("""
                SELECT status, revision
                FROM pesagem_draft_batches
                WHERE id = %s
                  AND (
                      (%s IS NOT NULL AND store_id = %s)
                      OR (store_id IS NULL AND loja = %s)
                  )
            """, (
                requested_id,
                resolved_store_id, resolved_store_id, loja,
            ))
            requested_batch = cursor.fetchone()
            if requested_batch and requested_batch['status'] == 'confirmed':
                conn.commit()
                receipt = _load_pesagem_batch(
                    cursor, loja, requested_id
                )
                try:
                    client_revision = int(expected_revision)
                except (ValueError, TypeError):
                    client_revision = None
                submitted = [
                    (
                        entry['data'].isoformat(),
                        entry['sabor'],
                        float(entry['quantidade_kg']),
                        bool(entry.get('suspeito')),
                    )
                    for entry in entries
                ]
                confirmed = [
                    (
                        entry['data'],
                        entry['sabor'],
                        float(entry['quantidade_kg']),
                        bool(entry.get('suspeito')),
                    )
                    for entry in receipt['entries']
                ]
                if (
                    client_revision == receipt['revision']
                    and submitted == confirmed
                ):
                    return receipt
                raise ValueError(
                    'Este lote já foi confirmado com outros dados. '
                    'As alterações locais foram mantidas para revisão.'
                )
            if requested_batch and requested_batch['status'] in (
                'registering', 'registered'
            ):
                raise ValueError(
                    'Este lote já foi registado no stock. '
                    'Reveja e edite as pesagens persistidas antes de confirmar.'
                )
            cursor.execute("""
                SELECT id, status, revision, store_id
                FROM pesagem_draft_batches
                WHERE (
                    (%s IS NOT NULL AND store_id = %s)
                    OR (store_id IS NULL AND loja = %s)
                )
                  AND status IN (
                      'draft', 'registering', 'registered',
                      'confirming', 'failed'
                  )
                ORDER BY updated_at DESC
                LIMIT 1
                FOR UPDATE
            """, (resolved_store_id, resolved_store_id, loja))
            existing = cursor.fetchone()
            effective_store_id = (
                (existing or {}).get('store_id')
                or resolved_store_id
            )

            if not entries:
                if not existing:
                    conn.commit()
                    return None
                if existing['status'] in (
                    'confirming', 'registering', 'registered'
                ):
                    raise ValueError(
                        'Este lote já está a ser processado ou foi registado. '
                        'Recarregue a página antes de continuar.'
                    )
                if str(existing['id']) != requested_id:
                    raise ValueError(
                        'Existe um rascunho mais recente nesta loja. '
                        'Recarregue-o antes de apagar alterações.'
                    )
                cursor.execute("""
                    SELECT DISTINCT data
                    FROM pesagem_draft_entries
                    WHERE batch_id = %s
                """, (existing['id'],))
                _lock_pesagem_days(
                    cursor,
                    effective_store_id,
                    [row['data'] for row in cursor.fetchall()],
                )
                try:
                    client_revision = int(expected_revision)
                except (ValueError, TypeError):
                    raise ValueError(
                        'A versão do rascunho está em falta. '
                        'Recarregue a página antes de continuar.'
                    )
                cursor.execute("""
                    DELETE FROM pesagem_draft_batches
                    WHERE id = %s AND revision = %s
                      AND status IN ('draft', 'failed')
                """, (requested_id, client_revision))
                if cursor.rowcount != 1:
                    raise ValueError(
                        'Este rascunho foi alterado noutro dispositivo. '
                        'As suas alterações locais foram mantidas; '
                        'recarregue para escolher qual versão usar.'
                    )
                conn.commit()
                return None

            draft_days = sorted({entry['data'] for entry in entries})
            _lock_pesagem_days(cursor, effective_store_id, draft_days)
            _ensure_pesagem_days_not_justified(
                cursor, effective_store_id, draft_days
            )

            if existing:
                if existing['status'] in (
                    'confirming', 'registering', 'registered'
                ):
                    raise ValueError(
                        'Este lote já está a ser processado ou foi registado. '
                        'Recarregue a página antes de continuar.'
                    )
                if str(existing['id']) != requested_id:
                    raise ValueError(
                        'Existe um rascunho mais recente nesta loja. '
                        'Recarregue-o antes de guardar alterações.'
                    )
                try:
                    client_revision = int(expected_revision)
                except (ValueError, TypeError):
                    raise ValueError(
                        'A versão do rascunho está em falta. '
                        'Recarregue a página antes de continuar.'
                    )
                effective_id = existing['id']
                cursor.execute("""
                    UPDATE pesagem_draft_batches
                    SET status = 'draft',
                        loja = %s,
                        expected_count = %s,
                        revision = revision + 1,
                        inserted_count = 0,
                        registered_at = NULL,
                        registered_by_id = NULL,
                        registered_by = NULL,
                        receipt_snapshot = NULL,
                        store_id = %s,
                        updated_by = %s,
                        error_message = NULL,
                        updated_at = NOW()
                    WHERE id = %s AND revision = %s
                """, (
                    loja, len(entries), effective_store_id, username, effective_id,
                    client_revision,
                ))
                if cursor.rowcount != 1:
                    raise ValueError(
                        'Este rascunho foi alterado noutro dispositivo. '
                        'As suas alterações locais foram mantidas; '
                        'recarregue para escolher qual versão usar.'
                    )
                cursor.execute(
                    "DELETE FROM pesagem_draft_entries WHERE batch_id = %s",
                    (effective_id,),
                )
            else:
                effective_id = requested_id
                cursor.execute("""
                    INSERT INTO pesagem_draft_batches (
                        id, loja, store_id, status, expected_count,
                        created_by_id, created_by, updated_by
                    ) VALUES (%s, %s, %s, 'draft', %s, %s, %s, %s)
                """, (
                    effective_id, loja, effective_store_id, len(entries),
                    actor_id, username, username,
                ))

            execute_values(
                cursor,
                """
                INSERT INTO pesagem_draft_entries (
                    batch_id, position, data, sabor,
                    quantidade_kg, suspeito
                ) VALUES %s
                """,
                [
                    (
                        effective_id,
                        position,
                        entry['data'],
                        entry['sabor'],
                        entry['quantidade_kg'],
                        bool(entry.get('suspeito')),
                    )
                    for position, entry in enumerate(entries)
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        return _load_pesagem_batch(cursor, loja, effective_id)


def register_pesagem_draft(
    batch_id,
    expected_revision,
    loja: str,
    actor_username: str,
    actor_id=None,
):
    """Register a draft exactly once and create all stock rows atomically."""
    username = (actor_username or 'sistema')[:100]
    conflict_message = None
    validated_batch = None
    current_store_id = None
    try:
        with db_connection() as conn:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                "SELECT id FROM stores WHERE lower(name) = lower(%s) LIMIT 1",
                (loja,),
            )
            current_store = cursor.fetchone()
            current_store_id = current_store['id'] if current_store else None
            cursor.execute("""
                SELECT b.id, b.status, b.expected_count,
                       b.revision, b.store_id
                FROM pesagem_draft_batches b
                WHERE b.id = %s
                  AND (
                      (%s IS NOT NULL AND b.store_id = %s)
                      OR (b.store_id IS NULL AND b.loja = %s)
                  )
                FOR UPDATE
            """, (
                str(batch_id),
                current_store_id, current_store_id, loja,
            ))
            batch = cursor.fetchone()
            if not batch:
                raise ValueError('O lote de pesagens já não existe.')
            validated_batch = dict(batch)
            try:
                client_revision = int(expected_revision)
            except (ValueError, TypeError):
                raise ValueError(
                    'A versão revista do lote está em falta. '
                    'Recarregue o rascunho antes de registar.'
                )
            if client_revision != int(batch['revision']):
                raise ValueError(
                    'Este rascunho foi alterado noutro dispositivo depois '
                    'da revisão. Nenhuma pesagem foi registada.'
                )
            if batch['status'] == 'confirmed':
                return _load_pesagem_batch(cursor, loja, batch_id)
            if batch['status'] == 'registered':
                return _load_pesagem_batch(cursor, loja, batch_id)
            if batch['status'] not in ('draft', 'failed'):
                raise ValueError(
                    'Este lote já está a ser registado. '
                    'Recarregue a página e tente novamente.'
                )

            cursor.execute("""
                SELECT id, data, sabor, quantidade_kg
                FROM pesagem_draft_entries
                WHERE batch_id = %s
                ORDER BY position
            """, (batch['id'],))
            entries = cursor.fetchall()
            expected_count = int(batch['expected_count'])
            if not entries or len(entries) != expected_count:
                raise ValueError(
                    'O lote está incompleto e não foi registado. '
                    'Reveja o rascunho e tente novamente.'
                )
            effective_store_id = batch['store_id']
            if not effective_store_id:
                cursor.execute(
                    "SELECT id FROM stores WHERE lower(name) = lower(%s)",
                    (loja,),
                )
                store_row = cursor.fetchone()
                effective_store_id = store_row['id'] if store_row else None
            _lock_pesagem_days(
                cursor,
                effective_store_id,
                [entry['data'] for entry in entries],
            )
            _ensure_pesagem_days_not_justified(
                cursor,
                effective_store_id,
                [entry['data'] for entry in entries],
            )

            cursor.execute("""
                UPDATE pesagem_draft_batches
                SET status = 'registering', loja = %s, store_id = %s,
                    updated_by = %s,
                    error_message = NULL, updated_at = NOW()
                WHERE id = %s
            """, (loja, effective_store_id, username, batch['id']))

            inserted_ids = []
            for entry in entries:
                cursor.execute("""
                    INSERT INTO stock_gelado (
                        data, loja, sabor, quantidade_kg,
                        tipo, local, store_id, source_batch_id
                    )
                    SELECT %s, b.loja, %s, %s, 'fim', NULL, b.store_id, b.id
                    FROM pesagem_draft_batches b
                    WHERE b.id = %s
                    ON CONFLICT DO NOTHING
                    RETURNING *
                """, (
                    entry['data'], entry['sabor'],
                    entry['quantidade_kg'], batch['id'],
                ))
                inserted = cursor.fetchone()
                if inserted:
                    inserted_ids.append(inserted['id'])
                    cursor.execute("""
                        UPDATE pesagem_draft_entries
                        SET stock_id = %s
                        WHERE id = %s AND batch_id = %s
                    """, (inserted['id'], entry['id'], batch['id']))
                    _write_pesagem_audit(
                        cursor, 'create', loja, 'batch_registration',
                        actor_id=actor_id, actor_username=username,
                        store_id=effective_store_id,
                        event_date=inserted['data'],
                        stock_id=inserted['id'],
                        batch_id=batch['id'],
                        affected_count=1,
                        after_data=_pesagem_stock_snapshot(inserted),
                    )

            if len(inserted_ids) != expected_count:
                conflict_message = (
                    f'O servidor recebeu {expected_count} entradas, mas apenas '
                    f'{len(inserted_ids)} poderiam ser gravadas. Nenhuma foi '
                    'registada; verifique pesagens já existentes.'
                )
                raise ValueError(conflict_message)

            cursor.execute("""
                UPDATE pesagem_draft_batches
                SET status = 'registered',
                    inserted_count = %s,
                    updated_by = %s,
                    error_message = NULL,
                    updated_at = NOW(),
                    registered_at = NOW(),
                    registered_by_id = %s,
                    registered_by = %s
                WHERE id = %s
            """, (
                expected_count, username, actor_id, username, batch['id']
            ))
            _write_pesagem_audit(
                cursor, 'batch_register', loja, 'manual_draft',
                actor_id=actor_id, actor_username=username,
                store_id=effective_store_id,
                batch_id=batch['id'],
                affected_count=expected_count,
                expected_count=expected_count,
                after_data={
                    'revision': int(batch['revision']),
                    'stock_ids': inserted_ids,
                },
            )
            conn.commit()
            return _load_pesagem_batch(cursor, loja, batch_id)
    except Exception as exc:
        message = (
            conflict_message
            or (
                str(exc)
                if isinstance(exc, ValueError)
                else 'Falha técnica interna durante a confirmação.'
            )
        )
        import uuid
        try:
            audited_batch_id = str(uuid.UUID(str(batch_id)))
        except (ValueError, TypeError, AttributeError):
            audited_batch_id = None
        if not validated_batch:
            with db_connection() as conn:
                cursor = conn.cursor()
                _write_pesagem_audit(
                    cursor, 'failure', loja, 'batch_registration',
                    actor_id=actor_id, actor_username=username,
                    store_id=current_store_id,
                    batch_id=audited_batch_id,
                    affected_count=0,
                    outcome='failed',
                    safe_cause=message,
                )
                conn.commit()
            raise
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE pesagem_draft_batches
                SET status = 'failed', error_message = %s,
                    updated_by = %s, updated_at = NOW()
                WHERE id = %s
                  AND status <> 'confirmed'
                  AND (
                      (%s IS NOT NULL AND store_id = %s)
                      OR (store_id IS NULL AND lower(loja) = lower(%s))
                  )
            """, (
                message[:1000], username, str(batch_id),
                current_store_id, current_store_id, loja,
            ))
            if cursor.rowcount:
                _write_pesagem_audit(
                    cursor, 'failure', loja, 'batch_registration',
                    actor_id=actor_id, actor_username=username,
                    store_id=(
                        validated_batch.get('store_id')
                        or current_store_id
                    ),
                    batch_id=audited_batch_id,
                    affected_count=0,
                    expected_count=validated_batch.get('expected_count'),
                    outcome='failed',
                    safe_cause=message,
                )
            conn.commit()
        raise


def confirm_pesagem_draft(
    batch_id,
    expected_revision,
    loja: str,
    actor_username: str,
    actor_id=None,
):
    """Certify the active rows already registered after operator review."""
    username = (actor_username or 'sistema')[:100]
    validated_batch = None
    current_store_id = None
    try:
        with db_connection() as conn:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                "SELECT id FROM stores WHERE lower(name) = lower(%s) LIMIT 1",
                (loja,),
            )
            current_store = cursor.fetchone()
            current_store_id = current_store['id'] if current_store else None
            cursor.execute("""
                SELECT b.id, b.loja, b.store_id, b.status,
                       b.expected_count, b.revision, b.inserted_count
                FROM pesagem_draft_batches b
                WHERE b.id = %s
                  AND (
                      (%s IS NOT NULL AND b.store_id = %s)
                      OR (b.store_id IS NULL AND b.loja = %s)
                  )
                FOR UPDATE
            """, (
                str(batch_id),
                current_store_id, current_store_id, loja,
            ))
            batch = cursor.fetchone()
            if not batch:
                raise ValueError('O lote de pesagens já não existe.')
            validated_batch = dict(batch)
            try:
                client_revision = int(expected_revision)
            except (ValueError, TypeError):
                raise ValueError(
                    'A versão revista do lote está em falta. '
                    'Recarregue o lote antes de confirmar.'
                )
            if client_revision != int(batch['revision']):
                raise ValueError(
                    'Este lote foi alterado noutro dispositivo depois '
                    'da revisão. Nenhuma confirmação foi feita.'
                )
            if batch['status'] == 'confirmed':
                return _load_pesagem_batch(cursor, loja, batch_id)
            if batch['status'] != 'registered':
                raise ValueError(
                    'Registe primeiro as pesagens antes de confirmar a revisão.'
                )

            cursor.execute("""
                SELECT id, position, stock_id, suspeito
                FROM pesagem_draft_entries
                WHERE batch_id = %s
                ORDER BY position
                FOR UPDATE
            """, (batch['id'],))
            draft_entries = cursor.fetchall()
            expected_count = int(batch['expected_count'])
            if len(draft_entries) != expected_count:
                raise ValueError(
                    'O conjunto revisto já não corresponde ao lote registado.'
                )

            cursor.execute("""
                SELECT *
                FROM stock_gelado
                WHERE source_batch_id = %s
                  AND tipo = 'fim'
                  AND is_active = TRUE
                ORDER BY id
                FOR UPDATE
            """, (batch['id'],))
            stock_rows = cursor.fetchall()
            stock_by_id = {row['id']: row for row in stock_rows}
            stock_ids = [
                entry['stock_id'] for entry in draft_entries
            ]
            if (
                len(stock_rows) != expected_count
                or len(stock_ids) != len(set(stock_ids))
                or any(stock_id not in stock_by_id for stock_id in stock_ids)
            ):
                raise ValueError(
                    'O conjunto registado foi alterado. '
                    'Reponha as linhas em falta ou recarregue antes de confirmar.'
                )

            rows_by_position = [
                stock_by_id[entry['stock_id']]
                for entry in draft_entries
            ]
            effective_store_id = batch['store_id'] or current_store_id
            _lock_pesagem_days(
                cursor,
                effective_store_id,
                [row['data'] for row in rows_by_position],
            )
            _ensure_pesagem_days_not_justified(
                cursor,
                effective_store_id,
                [row['data'] for row in rows_by_position],
            )
            cursor.execute("""
                UPDATE pesagem_draft_batches
                SET status = 'confirming',
                    updated_by = %s,
                    error_message = NULL,
                    updated_at = NOW()
                WHERE id = %s
            """, (username, batch['id']))

            snapshot = [
                {
                    'data': row['data'].isoformat(),
                    'sabor': row['sabor'],
                    'quantidade_kg': float(row['quantidade_kg']),
                    'suspeito': bool(entry['suspeito']),
                    'stock_id': int(row['id']),
                }
                for entry, row in zip(draft_entries, rows_by_position)
            ]
            cursor.execute("""
                UPDATE pesagem_draft_batches
                SET status = 'confirmed',
                    updated_by = %s,
                    error_message = NULL,
                    updated_at = NOW(),
                    confirmed_at = NOW(),
                    receipt_snapshot = %s
                WHERE id = %s
            """, (username, Json(snapshot), batch['id']))
            _write_pesagem_audit(
                cursor, 'batch_confirm', loja, 'manual_draft',
                actor_id=actor_id, actor_username=username,
                store_id=effective_store_id,
                batch_id=batch['id'],
                affected_count=expected_count,
                expected_count=expected_count,
                after_data={
                    'revision': int(batch['revision']),
                    'stock_ids': [row['id'] for row in rows_by_position],
                },
            )
            conn.commit()
            return _load_pesagem_batch(cursor, loja, batch_id)
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else 'Falha técnica interna durante a confirmação da revisão.'
        )
        import uuid
        try:
            audited_batch_id = str(uuid.UUID(str(batch_id)))
        except (ValueError, TypeError, AttributeError):
            audited_batch_id = None
        with db_connection() as conn:
            cursor = conn.cursor()
            _write_pesagem_audit(
                cursor, 'failure', loja, 'batch_confirmation',
                actor_id=actor_id, actor_username=username,
                store_id=(
                    (validated_batch or {}).get('store_id')
                    or current_store_id
                ),
                batch_id=audited_batch_id,
                affected_count=0,
                expected_count=(validated_batch or {}).get('expected_count'),
                outcome='failed',
                safe_cause=message,
            )
            conn.commit()
        raise


def get_pesagem_batch_receipt(batch_id, loja: str):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        return _load_pesagem_batch(cursor, loja, batch_id)


def upsert_stock_gelado_matosinhos(data: date, sabor: str, quantidade_kg: float) -> None:
    """
    Upsert a stock_gelado record for Matosinhos/inicio.  If a row already
    exists for (data, Matosinhos, sabor, inicio), it is updated in-place;
    otherwise a new row is inserted.  Sabor name is normalised to its
    canonical form before the upsert so case variants are collapsed.

    Uses ON CONFLICT on the uq_stock_gelado_data_loja_sabor_tipo unique
    constraint — no race conditions possible.
    """
    from sabor_utils import normalise_sabor
    sabor = normalise_sabor(sabor)
    store_id = get_store_id_by_name('Matosinhos')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT * FROM stock_gelado
            WHERE data = %s AND store_id = %s
              AND sabor = %s AND tipo = 'inicio'
              AND is_active = TRUE
            FOR UPDATE
        """, (data, store_id, sabor))
        before = cursor.fetchone()
        cursor.execute('''
            INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, store_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (data, store_id, sabor, tipo)
            WHERE is_active = TRUE AND store_id IS NOT NULL
            DO UPDATE SET quantidade_kg = EXCLUDED.quantidade_kg,
                          store_id     = EXCLUDED.store_id
            RETURNING *
        ''', (data, 'Matosinhos', sabor, quantidade_kg, 'inicio', store_id))
        after = cursor.fetchone()
        _write_pesagem_audit(
            cursor,
            'edit' if before else 'create',
            'Matosinhos',
            'production_start',
            actor_username='sistema',
            store_id=store_id,
            event_date=data,
            stock_id=after['id'],
            affected_count=1,
            before_data=_pesagem_stock_snapshot(before),
            after_data=_pesagem_stock_snapshot(after),
        )
        conn.commit()


def get_pesagem_comparison(loja: str, tipo: str, data_atual: date = None, local: str = None):
    if data_atual is None:
        data_atual = date.today()

    local_filter = " AND local = %s" if local else ""

    def p(*dates):
        params = [loja, tipo] + list(dates)
        if local:
            params.append(local)
        return params

    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT DISTINCT data FROM stock_gelado
            WHERE loja = %s AND tipo = %s AND data < %s
              AND is_active = TRUE
              AND sabor IS NOT NULL
              {local_filter}
            ORDER BY data DESC LIMIT 1
        """, p(data_atual))
        row = cursor.fetchone()
        last_date = row[0] if row else None

        cursor.execute(f"""
            SELECT sabor, SUM(quantidade_kg) FROM stock_gelado
            WHERE loja = %s AND tipo = %s AND data = %s
              AND is_active = TRUE
              AND sabor IS NOT NULL
              {local_filter}
            GROUP BY sabor
        """, p(data_atual))
        hoje = {r[0]: float(r[1]) for r in cursor.fetchall()}

        anterior = {}
        if last_date:
            cursor.execute(f"""
                SELECT sabor, SUM(quantidade_kg) FROM stock_gelado
                WHERE loja = %s AND tipo = %s AND data = %s
                  AND is_active = TRUE
                  AND sabor IS NOT NULL
                  {local_filter}
                GROUP BY sabor
            """, p(last_date))
            anterior = {r[0]: float(r[1]) for r in cursor.fetchall()}

    return last_date, anterior, hoje

@db_retry
def get_stock_gelado_df(loja: str = None, tipo: str = None, data_inicio: date = None, data_fim: date = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = "SELECT * FROM stock_gelado WHERE is_active = TRUE"
        params = []
        if loja:
            store_id = get_store_id_by_name(loja)
            if store_id:
                query += """
                    AND (
                        store_id = %s
                        OR (store_id IS NULL AND lower(loja) = lower(%s))
                    )
                """
                params.extend([store_id, loja])
            else:
                query += " AND lower(loja) = lower(%s)"
                params.append(loja)
        if tipo:
            query += " AND tipo = %s"
            params.append(tipo)
        if data_inicio:
            query += " AND data >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND data <= %s"
            params.append(data_fim)
        query += " ORDER BY data DESC, id DESC"
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]

def get_stock_gelado_by_id(stock_id: int, include_inactive: bool = False):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=DictCursor)
        query = "SELECT * FROM stock_gelado WHERE id = %s"
        if not include_inactive:
            query += " AND is_active = TRUE"
        cursor.execute(query, (stock_id,))
        row = cursor.fetchone()
    return dict(row) if row else None

def delete_stock_gelado(
    stock_id: int,
    reason: str,
    actor_id=None,
    actor_username: str = 'sistema',
    origin: str = 'vendas',
):
    clean_reason = (reason or '').strip()
    if len(clean_reason) < 5:
        raise ValueError('Indique o motivo da eliminação (mínimo 5 caracteres).')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT * FROM stock_gelado
            WHERE id = %s AND is_active = TRUE
            FOR UPDATE
        """, (stock_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError('Esta pesagem já não está ativa.')
        if row['tipo'] == 'fim':
            _lock_pesagem_days(cursor, row['store_id'], [row['data']])
        cursor.execute("""
            UPDATE stock_gelado
            SET is_active = FALSE,
                deactivated_at = NOW(),
                deactivated_by_id = %s,
                deactivated_by = %s,
                deactivation_reason = %s
            WHERE id = %s AND is_active = TRUE
        """, (
            actor_id, (actor_username or 'sistema')[:100],
            clean_reason, stock_id,
        ))
        _write_pesagem_audit(
            cursor, 'delete', row['loja'], origin,
            actor_id=actor_id, actor_username=actor_username,
            store_id=row['store_id'], event_date=row['data'],
            stock_id=row['id'], batch_id=row['source_batch_id'],
            reason=clean_reason, affected_count=1,
            before_data=_pesagem_stock_snapshot(row),
            after_data={**_pesagem_stock_snapshot(row), 'is_active': False},
        )
        conn.commit()

def delete_stock_gelado_by_date(
    loja: str,
    data_date,
    reason: str,
    expected_count: int,
    actor_id=None,
    actor_username: str = 'sistema',
) -> int:
    """Delete all fim-de-dia stock_gelado rows for a given loja + date.

    Returns the number of rows deleted.
    """
    clean_reason = (reason or '').strip()
    if len(clean_reason) < 5:
        raise ValueError('Indique o motivo da eliminação (mínimo 5 caracteres).')
    try:
        expected_count = int(expected_count)
    except (TypeError, ValueError):
        raise ValueError('Confirme o número de linhas a eliminar.') from None
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        _lock_pesagem_days(cursor, store_id, [data_date])
        cursor.execute("""
            SELECT * FROM stock_gelado
            WHERE (
                    store_id = %s
                    OR (store_id IS NULL AND lower(loja) = lower(%s))
                  )
              AND data = %s AND tipo = 'fim'
              AND is_active = TRUE
            ORDER BY id
            FOR UPDATE
        """, (store_id, loja, data_date))
        rows = cursor.fetchall()
        if len(rows) != expected_count:
            raise ValueError(
                f'A data tem agora {len(rows)} linha(s) ativa(s). '
                'Recarregue antes de confirmar.'
            )
        if not rows:
            raise ValueError('Não existem pesagens ativas para eliminar.')
        cursor.execute("""
            UPDATE stock_gelado
            SET is_active = FALSE,
                deactivated_at = NOW(),
                deactivated_by_id = %s,
                deactivated_by = %s,
                deactivation_reason = %s
            WHERE (
                    store_id = %s
                    OR (store_id IS NULL AND lower(loja) = lower(%s))
                  )
              AND data = %s AND tipo = 'fim'
              AND is_active = TRUE
        """, (
            actor_id, (actor_username or 'sistema')[:100],
            clean_reason, store_id, loja, data_date,
        ))
        deleted = cursor.rowcount
        for row in rows:
            _write_pesagem_audit(
                cursor, 'delete', loja, 'vendas_delete_day',
                actor_id=actor_id, actor_username=actor_username,
                store_id=row['store_id'], event_date=row['data'],
                stock_id=row['id'], batch_id=row['source_batch_id'],
                reason=clean_reason, affected_count=1,
                before_data=_pesagem_stock_snapshot(row),
                after_data={
                    **_pesagem_stock_snapshot(row),
                    'is_active': False,
                },
            )
        _write_pesagem_audit(
            cursor, 'delete_day', loja, 'vendas_delete_day',
            actor_id=actor_id, actor_username=actor_username,
            store_id=store_id, event_date=data_date,
            reason=clean_reason, affected_count=deleted,
            expected_count=expected_count,
        )
        conn.commit()
    return deleted


def update_stock_gelado(
    stock_id: int,
    quantidade_kg: float,
    loja: str = None,
    nova_data: date = None,
    actor_id=None,
    actor_username: str = 'sistema',
    reason: str = None,
    origin: str = 'vendas',
    expected_store_id=None,
):
    clean_reason = (reason or '').strip()
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute("""
                SELECT sg.*,
                       s.name AS current_store_name
                FROM stock_gelado sg
                LEFT JOIN stores s ON s.id = sg.store_id
                WHERE sg.id = %s AND sg.is_active = TRUE
                FOR UPDATE OF sg
            """, (stock_id,))
            current = cursor.fetchone()
            if not current:
                raise ValueError('A pesagem já não está ativa.')
            effective_current_store_id = current['store_id']
            if effective_current_store_id is None:
                cursor.execute("""
                    SELECT id FROM stores
                    WHERE lower(name) = lower(%s)
                    LIMIT 1
                """, (current['loja'],))
                legacy_store = cursor.fetchone()
                effective_current_store_id = (
                    legacy_store['id'] if legacy_store else None
                )
            if (
                expected_store_id is not None
                and effective_current_store_id != int(expected_store_id)
            ):
                raise ValueError('A pesagem não pertence à loja selecionada.')
            if loja and loja not in (
                current['loja'],
                current['current_store_name'],
            ):
                conn.commit()
                return
            destination = nova_data or current['data']
            if (
                current['tipo'] == 'fim'
                and destination != current['data']
            ):
                effective_store_id = current['store_id']
                if not effective_store_id:
                    cursor.execute("""
                        SELECT id
                        FROM stores
                        WHERE lower(name) = lower(%s)
                        LIMIT 1
                    """, (current['loja'],))
                    legacy_store = cursor.fetchone()
                    effective_store_id = (
                        legacy_store['id'] if legacy_store else None
                    )
                if not effective_store_id:
                    raise ValueError(
                        'Não foi possível identificar a loja desta pesagem '
                        'antiga. A data não foi alterada.'
                    )
                _lock_pesagem_days(
                    cursor,
                    effective_store_id,
                    [current['data'], destination],
                )
                _ensure_pesagem_days_not_justified(
                    cursor,
                    effective_store_id,
                    [destination],
                )
            if len(clean_reason) < 5:
                raise ValueError(
                    'Indique o motivo da edição (mínimo 5 caracteres).'
                )
            cursor.execute("""
                UPDATE stock_gelado
                SET quantidade_kg = %s, data = %s
                WHERE id = %s
                RETURNING *
            """, (quantidade_kg, destination, stock_id))
            updated = cursor.fetchone()
            _write_pesagem_audit(
                cursor, 'edit', current['loja'], origin,
                actor_id=actor_id, actor_username=actor_username,
                store_id=current['store_id'], event_date=destination,
                stock_id=stock_id,
                reason=clean_reason,
                affected_count=1,
                before_data=_pesagem_stock_snapshot(current),
                after_data=_pesagem_stock_snapshot(updated),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def restore_stock_gelado(
    stock_id: int,
    reason: str,
    actor_id=None,
    actor_username: str = 'sistema',
):
    clean_reason = (reason or '').strip()
    if len(clean_reason) < 5:
        raise ValueError('Indique o motivo da reposição (mínimo 5 caracteres).')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute("""
                SELECT * FROM stock_gelado
                WHERE id = %s AND is_active = FALSE
                FOR UPDATE
            """, (stock_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError('Esta pesagem não está disponível para reposição.')
            if row['tipo'] == 'fim':
                _lock_pesagem_days(cursor, row['store_id'], [row['data']])
                _ensure_pesagem_days_not_justified(
                    cursor, row['store_id'], [row['data']]
                )
            cursor.execute("""
                UPDATE stock_gelado
                SET is_active = TRUE,
                    restored_at = NOW(),
                    restored_by_id = %s,
                    restored_by = %s
                WHERE id = %s AND is_active = FALSE
                RETURNING *
            """, (
                actor_id, (actor_username or 'sistema')[:100], stock_id,
            ))
            restored = cursor.fetchone()
            _write_pesagem_audit(
                cursor, 'restore', row['loja'], 'manager_history',
                actor_id=actor_id, actor_username=actor_username,
                store_id=row['store_id'], event_date=row['data'],
                stock_id=row['id'], batch_id=row['source_batch_id'],
                reason=clean_reason, affected_count=1,
                before_data=_pesagem_stock_snapshot(row),
                after_data=_pesagem_stock_snapshot(restored),
            )
            conn.commit()
            return dict(restored)
        except psycopg2.IntegrityError:
            conn.rollback()
            raise ValueError(
                'Já existe uma pesagem ativa com a mesma data e sabor.'
            ) from None
        except Exception:
            conn.rollback()
            raise


def get_pesagem_audit_history(
    store_id: int,
    data_inicio: date = None,
    data_fim: date = None,
    batch_id=None,
    limit: int = 250,
):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = """
            SELECT id, event_uuid, occurred_at, event_type, outcome,
                   stock_id, batch_id, store_id, loja, event_date,
                   actor_id, actor_username, origin, reason,
                   affected_count, expected_count, before_data,
                   after_data, safe_cause
            FROM pesagem_audit_events
            WHERE store_id = %s
        """
        params = [store_id]
        if data_inicio:
            query += " AND event_date >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND event_date <= %s"
            params.append(data_fim)
        if batch_id:
            query += " AND batch_id = %s"
            params.append(str(batch_id))
        query += " ORDER BY occurred_at DESC, id DESC LIMIT %s"
        params.append(max(1, min(int(limit), 500)))
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def get_pesagens_recentes(n: int = 3) -> dict:
    """Return the last n pesagem dates and per-sabor kg.
    Matosinhos: reads pesagem_matosinhos from plano_producao (morning weigh-in during planning).
    Bolhão: reads from stock_gelado tipo=fim (end-of-day stock check)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        result = {}

        cursor.execute("""
            SELECT DISTINCT data FROM plano_producao
            WHERE pesagem_matosinhos > 0 AND sabor IS NOT NULL
            ORDER BY data DESC LIMIT %s
        """, (n,))
        datas_mat = [r[0] for r in cursor.fetchall()]
        pivot_mat = {}
        if datas_mat:
            cursor.execute("""
                SELECT data, sabor, pesagem_matosinhos
                FROM plano_producao
                WHERE pesagem_matosinhos > 0 AND sabor IS NOT NULL AND data = ANY(%s)
                ORDER BY sabor, data
            """, (datas_mat,))
            for r in cursor.fetchall():
                d, s, kg = r[0], r[1], float(r[2])
                if s not in pivot_mat:
                    pivot_mat[s] = {}
                pivot_mat[s][d] = kg
        result['matosinhos'] = {'loja': 'Matosinhos', 'tipo': 'inicio', 'datas': datas_mat, 'pivot': pivot_mat}

        cursor.execute("""
            SELECT DISTINCT data FROM stock_gelado
            WHERE loja = 'Bolhão' AND tipo = 'fim' AND sabor IS NOT NULL
              AND is_active = TRUE
            ORDER BY data DESC LIMIT %s
        """, (n,))
        datas_bol = [r[0] for r in cursor.fetchall()]
        pivot_bol = {}
        if datas_bol:
            cursor.execute("""
                SELECT data, sabor, SUM(quantidade_kg)
                FROM stock_gelado
                WHERE loja = 'Bolhão' AND tipo = 'fim' AND sabor IS NOT NULL AND data = ANY(%s)
                  AND is_active = TRUE
                GROUP BY data, sabor
                ORDER BY sabor, data
            """, (datas_bol,))
            for r in cursor.fetchall():
                d, s, kg = r[0], r[1], float(r[2])
                if s not in pivot_bol:
                    pivot_bol[s] = {}
                pivot_bol[s][d] = kg
        result['bolhao'] = {'loja': 'Bolhão', 'tipo': 'fim', 'datas': datas_bol, 'pivot': pivot_bol}

    return result


def get_latest_stock_by_sabor(loja: str, tipo: str) -> pd.DataFrame:
    today = date.today()

    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    sabores_correntes = sorted(set(mapping.values()))

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sg.id, sg.sabor, sg.quantidade_kg, sg.data
            FROM stock_gelado sg
            WHERE sg.loja = %s AND sg.tipo = %s
              AND sg.is_active = TRUE
            ORDER BY sg.id DESC
        """, (loja, tipo))
        rows = cursor.fetchall()

    before_today = {}
    today_data = {}

    for row_id, sabor_raw, qtd, data in rows:
        sabor = reverse_mapping.get(sabor_raw, sabor_raw)
        if data < today:
            if sabor not in before_today:
                before_today[sabor] = {'quantidade_kg': qtd, 'data': data}
        elif data == today:
            if sabor not in today_data:
                today_data[sabor] = qtd

    result = []
    for sabor in sabores_correntes:
        bt = before_today.get(sabor, {})
        result.append({
            'sabor': sabor,
            'quantidade_kg': bt.get('quantidade_kg', 0),
            'data': bt.get('data', None),
            'pesagem_hoje': today_data.get(sabor, None)
        })

    return pd.DataFrame(result)

def add_stock_gelado_batch(data: date, loja: str, stocks: list, tipo: str):
    entries = [
        {
            'data': data,
            'sabor': stock['sabor'],
            'quantidade_kg': stock['quantidade'],
            'tipo': tipo,
        }
        for stock in stocks
        if stock['quantidade'] is not None and stock['quantidade'] >= 0
    ]
    return add_stock_gelado_bulk(
        entries, loja, actor_username='sistema', origin='legacy_batch'
    )

def get_vendas_bolhao_dashboard_data(loja: str = 'Bolhão') -> list:
    today = date.today()
    yesterday = today - timedelta(days=1)

    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg
            FROM stock_gelado
            WHERE loja = %s AND tipo IN ('fim', 'inicio') AND data = %s
              AND is_active = TRUE
            ORDER BY sabor, CASE WHEN tipo = 'fim' THEN 0 ELSE 1 END, id DESC
        """, (loja, yesterday))
        pesagem_ontem_raw = {}
        for sabor_raw, qtd in cursor.fetchall():
            sabor = reverse_mapping.get(sabor_raw, sabor_raw)
            pesagem_ontem_raw[sabor] = qtd

        cursor.execute("""
            SELECT sabor, SUM(quantidade) as total
            FROM rececao_mercadoria
            WHERE loja = %s AND data = %s AND sabor IS NOT NULL
            AND unidade = 'kg'
            GROUP BY sabor
        """, (loja, today))
        rececao_hoje_raw = {}
        for sabor_raw, total in cursor.fetchall():
            sabor = reverse_mapping.get(sabor_raw, sabor_raw)
            rececao_hoje_raw[sabor] = rececao_hoje_raw.get(sabor, 0) + float(total)

        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg
            FROM stock_gelado
            WHERE loja = %s AND tipo IN ('fim', 'inicio') AND data = %s
              AND is_active = TRUE
            ORDER BY sabor, CASE WHEN tipo = 'fim' THEN 0 ELSE 1 END, id DESC
        """, (loja, today))
        pesagem_hoje_raw = {}
        for sabor_raw, qtd in cursor.fetchall():
            sabor = reverse_mapping.get(sabor_raw, sabor_raw)
            pesagem_hoje_raw[sabor] = qtd

    results = []
    all_sabores = sorted(set(pesagem_ontem_raw.keys()) | set(rececao_hoje_raw.keys()))
    for sabor in all_sabores:
        results.append({
            'Sabor': sabor,
            'Pesagem Ontem (kg)': round(pesagem_ontem_raw.get(sabor, 0), 3),
            'Recebido Hoje (kg)': round(rececao_hoje_raw.get(sabor, 0), 3),
            'Pesagem Fim Dia (kg)': round(pesagem_hoje_raw[sabor], 3) if sabor in pesagem_hoje_raw else None
        })

    return results


def get_regras_negocio(area: str = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if area:
            cursor.execute("SELECT * FROM regras_negocio WHERE area = %s ORDER BY palavra_chave", (area,))
        else:
            cursor.execute("SELECT * FROM regras_negocio ORDER BY area, palavra_chave")
        rows = cursor.fetchall()
    return [dict(r) for r in rows]

def add_regra_negocio(area: str, palavra_chave: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO regras_negocio (area, palavra_chave) VALUES (%s, %s)", (area, palavra_chave))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def update_regra_negocio(regra_id: int, palavra_chave: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE regras_negocio SET palavra_chave = %s WHERE id = %s", (palavra_chave, regra_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def delete_regra_negocio(regra_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM regras_negocio WHERE id = %s", (regra_id,))
        conn.commit()

def get_vendas_diarias_diagnostico(data_inicio: date, data_fim: date) -> list:
    """Return daily gelado_kpi sales totals per loja for the given date range.

    Each row is a dict with:
      data, loja, total_euros, n_produtos
    Only products with gelado_kpi=TRUE are included.
    Days/lojas with no rows are NOT included (caller must detect gaps).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT vd.data, vd.loja,
                   COALESCE(SUM(vd.valor_euros), 0) AS total_euros,
                   COUNT(DISTINCT vd.produto) AS n_produtos
            FROM vendas_detalhe vd
            INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
            WHERE pvc.gelado_kpi = TRUE
              AND vd.data >= %s AND vd.data <= %s
            GROUP BY vd.data, vd.loja
            ORDER BY vd.data, vd.loja
        """, (data_inicio, data_fim))
        rows = cursor.fetchall()
    return [{'data': r[0], 'loja': r[1], 'total_euros': float(r[2]), 'n_produtos': int(r[3])} for r in rows]


def delete_vendas_detalhe_by_date_loja_pairs(pairs: list) -> int:
    """Delete all vendas_detalhe rows matching any (date, loja) in ``pairs``.

    ``pairs`` is a list of (date_obj, loja_name) tuples.
    Returns the total number of deleted rows.
    Used to clear existing data before a targeted reimport (replace semantics).
    """
    if not pairs:
        return 0
    with db_connection() as conn:
        cursor = conn.cursor()
        total_deleted = 0
        for d, loja in pairs:
            cursor.execute(
                "DELETE FROM vendas_detalhe WHERE data = %s AND loja = %s",
                (d, loja)
            )
            total_deleted += cursor.rowcount
        conn.commit()
    return total_deleted


def check_vendas_dates_have_data(datas: list, loja: str = None) -> list:
    """Return which dates from ``datas`` already have rows in vendas_detalhe.

    Used to warn the user before re-importing a file that may duplicate data.
    If ``loja`` is given, filter to that loja; otherwise any loja counts.
    Returns a list of date objects that already have data.
    """
    if not datas:
        return []
    with db_connection() as conn:
        cursor = conn.cursor()
        if loja:
            cursor.execute("""
                SELECT DISTINCT data FROM vendas_detalhe
                WHERE data = ANY(%s) AND loja = %s
            """, (datas, loja))
        else:
            cursor.execute("""
                SELECT DISTINCT data FROM vendas_detalhe
                WHERE data = ANY(%s)
            """, (datas,))
        rows = cursor.fetchall()
    return [r[0] for r in rows]


def sync_produtos_vendas_config():
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT produto FROM vendas_detalhe ORDER BY produto")
        produtos = [r[0] for r in cursor.fetchall()]

        cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'gelado_kpi'")
        palavras_gelado = [r[0].lower() for r in cursor.fetchall()]
        cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'pastelaria'")
        palavras_pastelaria = [r[0].lower() for r in cursor.fetchall()]
        cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'confeitaria'")
        palavras_confeitaria = [r[0].lower() for r in cursor.fetchall()]

        if produtos:
            rows = []
            for produto in produtos:
                p_lower = produto.lower()
                is_gelado = any(kw in p_lower for kw in palavras_gelado) if palavras_gelado else False
                is_pastelaria = any(kw in p_lower for kw in palavras_pastelaria) if palavras_pastelaria else False
                is_confeitaria = any(kw in p_lower for kw in palavras_confeitaria) if palavras_confeitaria else False
                rows.append((
                    produto, False, is_pastelaria, is_confeitaria, is_gelado
                ))

            execute_values(
                cursor,
                """INSERT INTO produtos_vendas_config
                       (produto, gelado_kpi, pastelaria, confeitaria,
                        dose_config_pendente)
                   VALUES %s ON CONFLICT (produto) DO UPDATE
                   SET dose_config_pendente = (
                       produtos_vendas_config.dose_config_pendente
                       OR (
                           EXCLUDED.dose_config_pendente
                           AND NOT produtos_vendas_config.gelado_kpi
                           AND NOT EXISTS (
                               SELECT 1 FROM produto_regra_dose_historico prd
                               WHERE prd.produto_vendas_config_id =
                                     produtos_vendas_config.id
                           )
                       )
                   )""",
                rows,
                page_size=200,
            )

        conn.commit()

def get_produtos_vendas_config() -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT pvc.*, alias.nome_atual AS alias_target
            FROM produtos_vendas_config pvc
            LEFT JOIN produtos_vendas_aliases alias
              ON alias.nome_antigo=pvc.produto
             AND EXISTS (
                SELECT 1 FROM produtos_vendas_config canonical
                WHERE canonical.produto=alias.nome_atual
             )
            ORDER BY pvc.produto
        """)
        return [dict(r) for r in cursor.fetchall()]

def update_produto_vendas_config(produto_id: int, gelado_kpi: bool, pastelaria: bool, confeitaria: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        if not gelado_kpi:
            close_current_association(cursor, produto_id, date.today())
        cursor.execute("""
            UPDATE produtos_vendas_config
            SET gelado_kpi = %(gelado)s,
                dose_config_pendente = CASE
                    WHEN NOT %(gelado)s THEN FALSE
                    WHEN EXISTS (
                        SELECT 1 FROM produto_regra_dose_historico prd
                        WHERE prd.produto_vendas_config_id =
                              produtos_vendas_config.id
                          AND CURRENT_DATE BETWEEN prd.valid_from
                              AND COALESCE(prd.valid_to, 'infinity'::date)
                    ) THEN FALSE
                    ELSE TRUE
                END,
                pastelaria = %(pastelaria)s,
                confeitaria = %(confeitaria)s
            WHERE id = %(id)s
        """, {
            'gelado': gelado_kpi, 'pastelaria': pastelaria,
            'confeitaria': confeitaria, 'id': produto_id,
        })
        conn.commit()

def update_produtos_vendas_config_batch(updates: list):
    with db_connection() as conn:
        cursor = conn.cursor()
        queued = 0
        for u in updates:
            if not u['gelado_kpi']:
                close_current_association(cursor, u['id'], date.today())
            cursor.execute("""
                UPDATE produtos_vendas_config
                SET gelado_kpi = %(gelado)s,
                    dose_config_pendente = CASE
                        WHEN NOT %(gelado)s THEN FALSE
                        WHEN EXISTS (
                            SELECT 1
                            FROM produto_regra_dose_historico prd
                            WHERE prd.produto_vendas_config_id =
                                  produtos_vendas_config.id
                              AND CURRENT_DATE BETWEEN prd.valid_from
                                  AND COALESCE(
                                      prd.valid_to, 'infinity'::date
                                  )
                        ) THEN FALSE
                        ELSE TRUE
                    END,
                    pastelaria = %(pastelaria)s,
                    confeitaria = %(confeitaria)s
                WHERE id = %(id)s
                RETURNING dose_config_pendente
            """, {
                'gelado': u['gelado_kpi'],
                'pastelaria': u['pastelaria'],
                'confeitaria': u['confeitaria'],
                'id': u['id'],
            })
            result = cursor.fetchone()
            queued += int(bool(result and result[0]))
        conn.commit()
        return queued


def update_conta_vendas_diarias_batch(updates: list):
    """Set conta_vendas_diarias flag for a batch of produtos_vendas_config rows.

    Each entry in *updates* must have keys: id (int), conta (bool).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        for u in updates:
            cursor.execute("""
                UPDATE produtos_vendas_config
                SET conta_vendas_diarias = %s
                WHERE id = %s
            """, (u['conta'], u['id']))
        conn.commit()


def update_b2b_vendas_diarias_batch(updates: list):
    """Set b2b flag for a batch of produtos_vendas_config rows.

    Each entry in *updates* must have keys: id (int), b2b (bool).

    Marking a product as b2b is retroactive: the Dashboard de Vendas
    aggregation joins on this column at query time, so all existing
    vendas_detalhe history for the product (2025 and 2026) moves into the
    B2B channel immediately, not just future sales.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        for u in updates:
            cursor.execute("""
                UPDATE produtos_vendas_config
                SET b2b = %s
                WHERE id = %s
            """, (u['b2b'], u['id']))
        conn.commit()

def get_produtos_by_area(area: str) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'confeitaria':
            cursor.execute("SELECT produto FROM produtos_vendas_config WHERE confeitaria = TRUE ORDER BY produto")
        elif area == 'pastelaria':
            cursor.execute("SELECT produto FROM produtos_vendas_config WHERE pastelaria = TRUE ORDER BY produto")
        else:
            cursor.execute("""
                SELECT produto FROM produtos_vendas_config pvc
                WHERE gelado_kpi=TRUE
                  AND EXISTS (
                      SELECT 1 FROM produto_regra_dose_historico prd
                      WHERE prd.produto_vendas_config_id=pvc.id
                        AND CURRENT_DATE BETWEEN prd.valid_from
                            AND COALESCE(prd.valid_to, 'infinity'::date)
                  )
                ORDER BY produto
            """)
        return [r[0] for r in cursor.fetchall()]

def delete_vendas_detalhe_by_dates(data_inicio: date, data_fim: date, loja: str = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        if loja:
            cursor.execute("DELETE FROM vendas_detalhe WHERE data >= %s AND data <= %s AND loja = %s", (data_inicio, data_fim, loja))
        else:
            cursor.execute("DELETE FROM vendas_detalhe WHERE data >= %s AND data <= %s", (data_inicio, data_fim))
        deleted = cursor.rowcount
        conn.commit()
    return deleted

def get_gramas_gelado() -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT atual.*, COALESCE(hist.tipo_dose, 'fixa') AS tipo_dose
            FROM gramas_gelado atual
            LEFT JOIN gramas_gelado_historico hist
              ON LOWER(BTRIM(hist.artigo)) = LOWER(BTRIM(atual.artigo))
             AND hist.valid_to IS NULL
            ORDER BY atual.artigo
        """)
        return [dict(r) for r in cursor.fetchall()]

def update_gramas_gelado(artigo_id: int, artigo: str, gramas: float,
                         tipo_dose: str = 'fixa') -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT artigo FROM gramas_gelado WHERE id = %s FOR UPDATE",
                (artigo_id,),
            )
            old = cursor.fetchone()
            if not old:
                return False
            today = date.today()
            cursor.execute("""
                SELECT DISTINCT prd.produto_vendas_config_id
                FROM produto_regra_dose_historico prd
                JOIN gramas_gelado_historico hist
                  ON hist.id=prd.regra_dose_id
                WHERE LOWER(BTRIM(hist.artigo))=LOWER(BTRIM(%s))
                  AND CURRENT_DATE BETWEEN prd.valid_from
                      AND COALESCE(prd.valid_to, 'infinity'::date)
            """, (old[0],))
            affected_product_ids = [row[0] for row in cursor.fetchall()]
            cursor.execute("""
                UPDATE gramas_gelado_historico
                SET valid_to = %s
                WHERE LOWER(BTRIM(artigo)) = LOWER(BTRIM(%s))
                  AND valid_to IS NULL
                  AND valid_from < %s
            """, (today - timedelta(days=1), old[0], today))
            cursor.execute("""
                UPDATE produto_regra_dose_historico prd
                SET valid_to=%s
                FROM gramas_gelado_historico hist
                WHERE hist.id=prd.regra_dose_id
                  AND LOWER(BTRIM(hist.artigo))=LOWER(BTRIM(%s))
                  AND prd.valid_to IS NULL AND prd.valid_from < %s
            """, (today - timedelta(days=1), old[0], today))
            cursor.execute("""
                DELETE FROM produto_regra_dose_historico prd
                USING gramas_gelado_historico hist
                WHERE hist.id=prd.regra_dose_id
                  AND LOWER(BTRIM(hist.artigo))=LOWER(BTRIM(%s))
                  AND hist.valid_to IS NULL AND hist.valid_from=%s
            """, (old[0], today))
            cursor.execute("""
                DELETE FROM gramas_gelado_historico
                WHERE LOWER(BTRIM(artigo)) = LOWER(BTRIM(%s))
                  AND valid_to IS NULL AND valid_from = %s
            """, (old[0], today))
            cursor.execute("UPDATE gramas_gelado SET artigo = %s, gramas = %s WHERE id = %s", (artigo, gramas, artigo_id))
            cursor.execute("""
                INSERT INTO gramas_gelado_historico
                    (artigo, gramas, tipo_dose, valid_from)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (artigo, gramas if tipo_dose == 'fixa' else None,
                  tipo_dose, today))
            new_rule_id = cursor.fetchone()[0]
            if affected_product_ids:
                cursor.execute("""
                    INSERT INTO produto_regra_dose_historico (
                        produto_vendas_config_id, regra_dose_id,
                        valid_from, created_by
                    )
                    SELECT product_id, %s, %s, 'sistema'
                    FROM UNNEST(%s::integer[]) AS product_id
                """, (new_rule_id, today, affected_product_ids))
                backfill_weight_sales(
                    cursor, product_ids=affected_product_ids,
                    rule_ids=[new_rule_id],
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def add_gramas_gelado(artigo: str, gramas: float,
                      tipo_dose: str = 'fixa') -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO gramas_gelado (artigo, gramas) VALUES (%s, %s)",
                (artigo, gramas if tipo_dose == 'fixa' else 1),
            )
            cursor.execute("""
                INSERT INTO gramas_gelado_historico
                    (artigo, gramas, tipo_dose, valid_from)
                VALUES (%s, %s, %s, %s)
            """, (artigo, gramas if tipo_dose == 'fixa' else None,
                  tipo_dose, date.today()))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def delete_gramas_gelado(artigo_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT artigo FROM gramas_gelado WHERE id = %s FOR UPDATE",
            (artigo_id,),
        )
        row = cursor.fetchone()
        if not row:
            return
        today = date.today()
        cursor.execute("""
            SELECT DISTINCT prd.produto_vendas_config_id
            FROM produto_regra_dose_historico prd
            JOIN gramas_gelado_historico hist
              ON hist.id=prd.regra_dose_id
            WHERE LOWER(BTRIM(hist.artigo))=LOWER(BTRIM(%s))
              AND CURRENT_DATE BETWEEN prd.valid_from
                  AND COALESCE(prd.valid_to, 'infinity'::date)
        """, (row[0],))
        affected_product_ids = [value[0] for value in cursor.fetchall()]
        cursor.execute("""
            UPDATE gramas_gelado_historico SET valid_to = %s
            WHERE LOWER(BTRIM(artigo)) = LOWER(BTRIM(%s))
              AND valid_to IS NULL AND valid_from < %s
        """, (today - timedelta(days=1), row[0], today))
        cursor.execute("""
            UPDATE produto_regra_dose_historico prd
            SET valid_to=%s
            FROM gramas_gelado_historico hist
            WHERE hist.id=prd.regra_dose_id
              AND LOWER(BTRIM(hist.artigo))=LOWER(BTRIM(%s))
              AND prd.valid_to IS NULL AND prd.valid_from < %s
        """, (today - timedelta(days=1), row[0], today))
        cursor.execute("""
            DELETE FROM produto_regra_dose_historico prd
            USING gramas_gelado_historico hist
            WHERE hist.id=prd.regra_dose_id
              AND LOWER(BTRIM(hist.artigo))=LOWER(BTRIM(%s))
              AND hist.valid_to IS NULL AND hist.valid_from=%s
        """, (row[0], today))
        cursor.execute("""
            DELETE FROM gramas_gelado_historico
            WHERE LOWER(BTRIM(artigo)) = LOWER(BTRIM(%s))
              AND valid_to IS NULL AND valid_from = %s
        """, (row[0], today))
        cursor.execute("DELETE FROM gramas_gelado WHERE id = %s", (artigo_id,))
        if affected_product_ids:
            cursor.execute("""
                UPDATE produtos_vendas_config pvc
                SET gelado_kpi=FALSE, dose_config_pendente=TRUE
                WHERE pvc.id=ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM produto_regra_dose_historico prd
                      WHERE prd.produto_vendas_config_id=pvc.id
                        AND CURRENT_DATE BETWEEN prd.valid_from
                            AND COALESCE(prd.valid_to, 'infinity'::date)
                  )
            """, (affected_product_ids,))
        conn.commit()

def get_consumo_gelado_mensal(
    loja: str = None,
    data_inicio: date = None,
    data_fim: date = None,
) -> pd.DataFrame:
    from db.doseamento import load_dose_sales_with_rules
    if data_inicio is not None and data_fim is not None and data_inicio > data_fim:
        raise ValueError('A data inicial não pode ser posterior à data final.')
    sales, _history = load_dose_sales_with_rules(
        data_inicio=data_inicio, data_fim=data_fim, loja=loja
    )
    vendas_df = pd.DataFrame([{
        'produto': row.get('canonical_produto') or row['produto'],
        'data': row['data'],
        'mes': row['data'].strftime('%Y-%m'),
        'quantidade_vendida': row['quantidade'],
        'peso_vendido_kg': row.get('peso_vendido_kg'),
        'gramas_por_unidade': (
            row.get('dose_rule', {}).get('gramas')
            if row.get('dose_rule') else None
        ),
        'tipo_dose': (
            row.get('dose_rule', {}).get('tipo_dose')
            if row.get('dose_rule') else None
        ),
    } for row in sales])

    if vendas_df.empty:
        return vendas_df

    def row_consumption(row):
        if pd.isna(row['tipo_dose']):
            return None
        if row['tipo_dose'] == 'peso':
            weight = row['peso_vendido_kg']
            return None if pd.isna(weight) else float(weight)
        return (
            float(row['quantidade_vendida'])
            * float(row['gramas_por_unidade']) / 1000
        )
    vendas_df['consumo_kg'] = vendas_df.apply(row_consumption, axis=1)
    vendas_df['consumo_incompleto'] = vendas_df['consumo_kg'].isna()
    return vendas_df.groupby(
        ['produto', 'mes', 'gramas_por_unidade', 'tipo_dose'],
        dropna=False, as_index=False,
    ).agg(
        quantidade_vendida=('quantidade_vendida', 'sum'),
        consumo_kg=(
            'consumo_kg',
            lambda values: (
                values.sum() if not values.isna().any() else float('nan')
            ),
        ),
        consumo_incompleto=('consumo_incompleto', 'any'),
    )


def get_product_dashboard_summary(area: str) -> pd.DataFrame:
    if area == 'pastelaria':
        prod_table = 'producao_pastelaria'
        config_field = 'pastelaria'
    else:
        prod_table = 'producao_confeitaria'
        config_field = 'confeitaria'

    today = date.today()
    week_ago = today - timedelta(days=7)

    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(f"SELECT produto FROM produtos_vendas_config WHERE {config_field} = TRUE ORDER BY produto")
        produtos = [row[0] for row in cursor.fetchall()]

        if not produtos:
            if area == 'pastelaria':
                cursor.execute("SELECT nome FROM produtos_pastelaria WHERE ativo = TRUE ORDER BY nome")
            else:
                cursor.execute("SELECT nome FROM produtos_confeitaria WHERE ativo = TRUE ORDER BY nome")
            produtos = [row[0] for row in cursor.fetchall()]

        if not produtos:
            return pd.DataFrame()

        cursor.execute(f"""
            SELECT DISTINCT ON (produto) produto, data, quantidade
            FROM {prod_table}
            ORDER BY produto, data DESC, id DESC
        """)
        all_prod_map = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        last_prod_map = {}
        for p in produtos:
            if p in all_prod_map:
                last_prod_map[p] = all_prod_map[p]
            else:
                for prod_name, val in all_prod_map.items():
                    if p.lower() in prod_name.lower() or prod_name.lower() in p.lower():
                        last_prod_map[p] = val
                        break

        cursor.execute("""
            SELECT produto, loja, SUM(quantidade) as total
            FROM vendas_detalhe
            WHERE LOWER(produto) IN (SELECT LOWER(produto) FROM produtos_vendas_config WHERE """ + config_field + """ = TRUE)
            AND data >= %s AND data <= %s
            GROUP BY produto, loja
        """, (week_ago, today))
        vendas_semana = {}
        for row in cursor.fetchall():
            produto_venda = row[0]
            loja = row[1]
            total = int(row[2]) if row[2] else 0
            if produto_venda not in vendas_semana:
                vendas_semana[produto_venda] = {}
            vendas_semana[produto_venda][loja] = total

        if area == 'pastelaria':
            cursor.execute("""
                WITH latest_identity AS (
                    SELECT DISTINCT ON (
                               COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                               cs.loja
                           )
                           COALESCE(
                               NULLIF(CONCAT_WS(', ',
                                   NULLIF(BTRIM(p.tipologia), ''),
                                   NULLIF(BTRIM(p.sabor), ''),
                                   NULLIF(BTRIM(p.cobertura), '')
                               ), ''),
                               cs.produto
                           ) AS produto,
                           cs.loja, cs.quantidade, cs.data, cs.id,
                           cs.produto_pastelaria_id
                    FROM contagem_stock cs
                    LEFT JOIN produtos_pastelaria p
                      ON p.id=cs.produto_pastelaria_id
                    WHERE cs.tipo = %s
                    ORDER BY COALESCE(
                                 cs.produto_pastelaria_id::text,
                                 'text:' || cs.produto
                             ),
                             cs.loja, cs.data DESC, cs.id DESC
                )
                SELECT DISTINCT ON (produto, loja)
                       produto, loja, quantidade
                FROM latest_identity
                ORDER BY produto, loja,
                         (produto_pastelaria_id IS NOT NULL) DESC,
                         data DESC, id DESC
            """, (area,))
        else:
            cursor.execute("""
                SELECT DISTINCT ON (produto, loja) produto, loja, quantidade
                FROM contagem_stock
                WHERE tipo = %s
                ORDER BY produto, loja, data DESC, id DESC
            """, (area,))
        stock_map = {}
        for row in cursor.fetchall():
            produto_stock = row[0]
            loja = row[1]
            qtd = int(row[2]) if row[2] else 0
            if produto_stock not in stock_map:
                stock_map[produto_stock] = {}
            stock_map[produto_stock][loja] = qtd

    results = []
    for produto in produtos:
        lp = last_prod_map.get(produto)
        ultima_prod = f"{lp[1]} un ({lp[0].strftime('%d/%m')})" if lp else "-"

        vendas_mat = 0
        vendas_bol = 0
        for vp, lojas in vendas_semana.items():
            if produto.lower() in vp.lower() or vp.lower() in produto.lower():
                vendas_mat += lojas.get('Matosinhos', 0)
                vendas_bol += lojas.get('Bolhão', 0)

        stock_mat = stock_map.get(produto, {}).get('Matosinhos', 0)
        stock_bol = stock_map.get(produto, {}).get('Bolhão', 0)

        results.append({
            'Produto': produto,
            'Última Produção': ultima_prod,
            'Vendas Semana Mat.': int(vendas_mat),
            'Vendas Semana Bol.': int(vendas_bol),
            'Stock Mat.': int(stock_mat),
            'Stock Bol.': int(stock_bol),
        })

    return pd.DataFrame(results)

def get_tipologias_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM tipologias_pastelaria WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_tipologias_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, ativo FROM tipologias_pastelaria ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]

def add_tipologia_pastelaria(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO tipologias_pastelaria (nome) VALUES (%s)", (nome,))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def delete_tipologia_pastelaria(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tipologias_pastelaria WHERE id = %s", (id,))
        conn.commit()


def get_precos_caixa_kg_historico() -> list:
    """Return all config_preco_caixa_kg rows ordered by data_inicio DESC."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, data_inicio, preco_kg, created_at
            FROM config_preco_caixa_kg
            ORDER BY data_inicio DESC
        """)
        return [dict(r) for r in cursor.fetchall()]


def add_preco_caixa_kg(data_inicio: date, preco_kg: float) -> None:
    """Insert or update a price period in config_preco_caixa_kg."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO config_preco_caixa_kg (data_inicio, preco_kg)
            VALUES (%s, %s)
            ON CONFLICT (data_inicio) DO UPDATE SET preco_kg = EXCLUDED.preco_kg
        """, (data_inicio, preco_kg))
        conn.commit()


def delete_preco_caixa_kg(id: int) -> bool:
    """Delete a price period by id. Returns False if it is the base record (first ever)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(id) FROM config_preco_caixa_kg")
        min_id = cursor.fetchone()[0]
        if min_id is None or int(id) == int(min_id):
            return False
        cursor.execute("DELETE FROM config_preco_caixa_kg WHERE id = %s", (id,))
        conn.commit()
    return True


def get_volume_por_produto(data_inicio: date = None, data_fim: date = None, loja: str = None) -> list:
    """Return per-product sales volume for the eurokg section.

    Returns a list of dicts with:
        produto       str   — product name
        gelado_kpi    bool  — included in €/kg KPI
        caixa_loja    bool  — in-store box sold by weight (excluded from KPI)
        canal         str   — Uber | Bolt | Glovo | Loja | Directo
        unidades      int   — number of sale lines in the period
        valor_euros   float — total value sold
        kg_estimado   float | None — estimated kg for caixa_loja (value/preco_kg_per_date);
                      NULL when no price is configured in config_preco_caixa_kg for the
                      sale date, or for non-caixa_loja products.

    Only products with gelado_kpi=TRUE or caixa_loja=TRUE are returned so the page
    focuses on the gelado product mix. kg_estimado uses effective-dated pricing from
    config_preco_caixa_kg (most recent data_inicio <= vd.data). Rows without a
    matching price record return NULL for kg_estimado rather than a silent default.
    """
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = """
            SELECT
                vd.produto,
                pvc.gelado_kpi,
                pvc.caixa_loja,
                CASE
                    WHEN vd.produto ILIKE '%%Uber%%'  THEN 'Uber'
                    WHEN vd.produto ILIKE '%%Bolt%%'  THEN 'Bolt'
                    WHEN vd.produto ILIKE '%%Glovo%%' THEN 'Glovo'
                    WHEN pvc.caixa_loja = TRUE        THEN 'Loja'
                    ELSE 'Directo'
                END AS canal,
                COUNT(*) AS unidades,
                COALESCE(SUM(vd.valor_euros), 0) AS valor_euros,
                CASE
                    WHEN pvc.caixa_loja = TRUE THEN
                        SUM(
                            vd.valor_euros /
                            (SELECT ck.preco_kg
                             FROM config_preco_caixa_kg ck
                             WHERE ck.data_inicio <= vd.data
                             ORDER BY ck.data_inicio DESC
                             LIMIT 1)
                        )
                    ELSE NULL
                END AS kg_estimado
            FROM vendas_detalhe vd
            INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
            WHERE (pvc.gelado_kpi = TRUE OR pvc.caixa_loja = TRUE)
        """
        params = []
        if loja:
            query += " AND vd.loja = %s"
            params.append(loja)
        if data_inicio:
            query += " AND vd.data >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND vd.data <= %s"
            params.append(data_fim)
        query += """
            GROUP BY vd.produto, pvc.gelado_kpi, pvc.caixa_loja,
                     CASE
                         WHEN vd.produto ILIKE '%%Uber%%'  THEN 'Uber'
                         WHEN vd.produto ILIKE '%%Bolt%%'  THEN 'Bolt'
                         WHEN vd.produto ILIKE '%%Glovo%%' THEN 'Glovo'
                         WHEN pvc.caixa_loja = TRUE        THEN 'Loja'
                         ELSE 'Directo'
                     END
            ORDER BY valor_euros DESC
        """
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]
