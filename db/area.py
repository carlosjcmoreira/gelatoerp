import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger

def get_ultimo_stock_balcao(area: str) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT ON (produto, loja) produto, loja, quantidade, data
            FROM contagem_stock
            WHERE tipo = %s
            ORDER BY produto, loja, data DESC, id DESC
        """, (area,))
        rows = cursor.fetchall()
    return [{'produto': r[0], 'loja': r[1], 'quantidade': int(r[2]), 'data': r[3]} for r in rows]


def _plano_table(area: str) -> str:
    if area == 'pastelaria':
        return 'plano_producao_pastelaria'
    return 'plano_producao_confeitaria'


def _stock_prod_table(area: str) -> str:
    if area == 'pastelaria':
        return 'stock_producao_pastelaria'
    return 'stock_producao_confeitaria'


def upsert_plano_area(area: str, data: date, produto: str, producao_estimada: int,
                      estimada_bolhao: int = 0, estimada_matosinhos: int = 0):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            cursor.execute(f"""
                INSERT INTO {tbl} (data, produto, producao_estimada, producao_estimada_bolhao, producao_estimada_matosinhos)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (data, produto) DO UPDATE SET
                    producao_estimada = EXCLUDED.producao_estimada,
                    producao_estimada_bolhao = EXCLUDED.producao_estimada_bolhao,
                    producao_estimada_matosinhos = EXCLUDED.producao_estimada_matosinhos,
                    updated_at = NOW()
            """, (data, produto, producao_estimada, estimada_bolhao, estimada_matosinhos))
        else:
            cursor.execute(f"""
                INSERT INTO {tbl} (data, produto, producao_estimada)
                VALUES (%s, %s, %s)
                ON CONFLICT (data, produto) DO UPDATE SET
                    producao_estimada = EXCLUDED.producao_estimada,
                    updated_at = NOW()
            """, (data, produto, producao_estimada))
        conn.commit()


def marcar_produto_no_plano(area: str, data: date, produto: str):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE {tbl} SET no_plano = TRUE, updated_at = NOW()
            WHERE data = %s AND produto = %s
        """, (data, produto))
        conn.commit()


def remover_produto_do_plano(area: str, data: date, produto: str):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE {tbl} SET no_plano = FALSE, updated_at = NOW()
            WHERE data = %s AND produto = %s
        """, (data, produto))
        conn.commit()


def get_plano_do_dia_area(area: str, data: date) -> list:
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            cursor.execute(f"""
                SELECT produto, producao_estimada, producao_real,
                       COALESCE(status, 'pendente'), COALESCE(nota, ''),
                       COALESCE(producao_estimada_bolhao, 0), COALESCE(producao_estimada_matosinhos, 0)
                FROM {tbl}
                WHERE data = %s AND no_plano = TRUE
                ORDER BY produto
            """, (data,))
            rows = cursor.fetchall()
            return [
                {
                    'produto': r[0],
                    'estimado': int(r[1] or 0),
                    'real': int(r[2]) if r[2] is not None else None,
                    'status': r[3],
                    'nota': r[4],
                    'estimado_bolhao': int(r[5]),
                    'estimado_matosinhos': int(r[6]),
                }
                for r in rows
            ]
        else:
            cursor.execute(f"""
                SELECT produto, producao_estimada, producao_real
                FROM {tbl}
                WHERE data = %s AND no_plano = TRUE
                ORDER BY produto
            """, (data,))
            rows = cursor.fetchall()
            return [
                {
                    'produto': r[0],
                    'estimado': int(r[1] or 0),
                    'real': int(r[2]) if r[2] is not None else None,
                }
                for r in rows
            ]


def get_plano_produto(area: str, data: date, produto: str) -> dict:
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT producao_estimada, producao_real, no_plano
            FROM {tbl}
            WHERE data = %s AND produto = %s
        """, (data, produto))
        row = cursor.fetchone()
    if row:
        return {'estimado': int(row[0] or 0), 'real': int(row[1]) if row[1] is not None else None, 'no_plano': row[2]}
    return None


def update_producao_real_area(area: str, data: date, produto: str, producao_real: int):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria' and producao_real > 0:
            cursor.execute(f"""
                UPDATE {tbl}
                SET producao_real = %s, status = 'concluido', updated_at = NOW()
                WHERE data = %s AND produto = %s
            """, (producao_real, data, produto))
        else:
            cursor.execute(f"""
                UPDATE {tbl}
                SET producao_real = %s, updated_at = NOW()
                WHERE data = %s AND produto = %s
            """, (producao_real, data, produto))
        conn.commit()


def update_plano_status_area(area: str, data: date, produto: str, status: str, nota: str = ''):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE {tbl}
            SET status = %s, nota = %s, updated_at = NOW()
            WHERE data = %s AND produto = %s
        """, (status, nota, data, produto))
        conn.commit()


def upsert_stock_producao_area(area: str, data: date, produto: str, quantidade: int):
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT INTO {tbl} (data, produto, quantidade)
            VALUES (%s, %s, GREATEST(%s, 0))
            ON CONFLICT (data, produto) DO UPDATE SET
                quantidade = GREATEST({tbl}.quantidade + EXCLUDED.quantidade, 0),
                updated_at = NOW()
        """, (data, produto, quantidade))
        conn.commit()


def get_stock_producao_area_all(area: str, data: date = None) -> list:
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT produto, SUM(quantidade) as total
            FROM {tbl}
            WHERE quantidade > 0
            GROUP BY produto
            HAVING SUM(quantidade) > 0
            ORDER BY produto
        """)
        rows = cursor.fetchall()
    return [{'produto': r[0], 'quantidade': int(r[1])} for r in rows]


def get_stock_producao_area(area: str, data: date, produto: str) -> int:
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT COALESCE(SUM(quantidade), 0) FROM {tbl}
            WHERE produto = %s AND quantidade > 0
        """, (produto,))
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def reduzir_stock_producao_area(area: str, data: date, produto: str, quantidade: int) -> bool:
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, quantidade FROM {tbl}
            WHERE produto = %s AND quantidade > 0
            ORDER BY data ASC
        """, (produto,))
        rows = cursor.fetchall()
        remaining = quantidade
        updated = False
        for row_id, row_qty in rows:
            if remaining <= 0:
                break
            subtract = min(remaining, int(row_qty))
            cursor.execute(f"""
                UPDATE {tbl}
                SET quantidade = GREATEST(quantidade - %s, 0), updated_at = NOW()
                WHERE id = %s
            """, (subtract, row_id))
            remaining -= subtract
            updated = True
        conn.commit()
    return updated


# ── Crédito ──────────────────────────────────────────────────────────────────

