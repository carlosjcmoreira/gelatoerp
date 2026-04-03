import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache, invalidate
from db.stores import get_store_id_by_name
import json

def get_plano_producao(data: date, sabor: str) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT pesagem_matosinhos, producao_estimada_bolhao, producao_estimada_matosinhos,
               no_plano, COALESCE(producao_estimada_outros, 0),
               COALESCE(producao_estimada_mouzinho, 0)
        FROM plano_producao WHERE data = %s AND sabor = %s
    """, (data, sabor))
    row = cursor.fetchone()
    release_connection(conn)
    if row:
        return {
            'pesagem_matosinhos': float(row[0] or 0),
            'producao_estimada_bolhao': float(row[1] or 0),
            'producao_estimada_matosinhos': float(row[2] or 0),
            'no_plano': bool(row[3]),
            'producao_estimada_outros': float(row[4] or 0),
            'producao_estimada_mouzinho': float(row[5] or 0),
        }
    return {
        'pesagem_matosinhos': 0.0, 'producao_estimada_bolhao': 0.0,
        'producao_estimada_matosinhos': 0.0, 'no_plano': False,
        'producao_estimada_outros': 0.0, 'producao_estimada_mouzinho': 0.0,
    }


def marcar_sabor_no_plano(data: date, sabor: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO plano_producao (data, sabor, no_plano)
        VALUES (%s, %s, TRUE)
        ON CONFLICT (data, sabor) DO UPDATE SET
            no_plano = TRUE,
            updated_at = NOW()
    """, (data, sabor))
    conn.commit()
    release_connection(conn)


def get_plano_do_dia(data: date) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sabor, pesagem_matosinhos,
               producao_estimada_bolhao, producao_estimada_matosinhos,
               producao_real_bolhao, producao_real_matosinhos,
               COALESCE(producao_estimada_outros, 0),
               COALESCE(producao_estimada_mouzinho, 0),
               producao_real_mouzinho,
               producao_real_outros
        FROM plano_producao
        WHERE data = %s AND (
            no_plano = TRUE
            OR pesagem_matosinhos > 0
            OR producao_estimada_bolhao > 0
            OR producao_estimada_matosinhos > 0
            OR producao_estimada_outros > 0
            OR producao_estimada_mouzinho > 0
            OR producao_real_bolhao IS NOT NULL
            OR producao_real_matosinhos IS NOT NULL
            OR producao_real_mouzinho IS NOT NULL
            OR producao_real_outros IS NOT NULL
        )
        ORDER BY sabor
    """, (data,))
    rows = cursor.fetchall()
    release_connection(conn)
    return [
        {
            'sabor': r[0],
            'pesagem_matosinhos': float(r[1] or 0),
            'estimado_bolhao': float(r[2] or 0),
            'estimado_matosinhos': float(r[3] or 0),
            'real_bolhao': float(r[4]) if r[4] is not None else None,
            'real_matosinhos': float(r[5]) if r[5] is not None else None,
            'estimado_outros': float(r[6] or 0),
            'estimado_mouzinho': float(r[7] or 0),
            'real_mouzinho': float(r[8]) if r[8] is not None else None,
            'real_outros': float(r[9]) if r[9] is not None else None,
        }
        for r in rows
    ]


def get_producao_por_sabor_7dias(data_inicio: date, data_fim: date) -> list:
    """Returns actual production kg grouped by (data, sabor) from the producao table."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT data, sabor, SUM(quantidade_kg)
        FROM producao
        WHERE data BETWEEN %s AND %s
        GROUP BY data, sabor
        ORDER BY data, sabor
    """, (data_inicio, data_fim))
    result = [{'data': r[0], 'sabor': r[1], 'real': float(r[2])} for r in cursor.fetchall()]
    release_connection(conn)
    return result


def get_producao_manual_daily(data_inicio: date, data_fim: date) -> list:
    """Returns daily totals of manually registered production (real_bolhao + real_matosinhos)
    from plano_producao, grouped by date."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT data,
               SUM(COALESCE(producao_real_bolhao, 0) + COALESCE(producao_real_matosinhos, 0)
                   + COALESCE(producao_real_mouzinho, 0)) AS total_kg
        FROM plano_producao
        WHERE data BETWEEN %s AND %s
          AND (producao_real_bolhao IS NOT NULL OR producao_real_matosinhos IS NOT NULL
               OR producao_real_mouzinho IS NOT NULL)
        GROUP BY data
        ORDER BY data
    """, (data_inicio, data_fim))
    result = [{'data': r[0], 'total_kg': float(r[1])} for r in cursor.fetchall()]
    release_connection(conn)
    return result


def get_producao_balanca_por_sabor(data_inicio: date, data_fim: date) -> list:
    """Returns scale/Calybrabox production from the producao table grouped by (data, sabor),
    ordered by date desc then sabor."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT data, COALESCE(sabor, '(sem sabor)'), SUM(quantidade_kg)
        FROM producao
        WHERE data BETWEEN %s AND %s
        GROUP BY data, sabor
        ORDER BY data DESC, sabor
    """, (data_inicio, data_fim))
    result = [{'data': r[0], 'sabor': r[1], 'kg': float(r[2])} for r in cursor.fetchall()]
    release_connection(conn)
    return result


def get_plano_producao_historico(data_inicio: date, data_fim: date) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT data, sabor,
               COALESCE(producao_estimada_bolhao, 0),
               COALESCE(producao_estimada_matosinhos, 0),
               producao_real_bolhao,
               producao_real_matosinhos,
               COALESCE(producao_estimada_mouzinho, 0),
               producao_real_mouzinho
        FROM plano_producao
        WHERE data BETWEEN %s AND %s
          AND (no_plano = TRUE
               OR producao_estimada_bolhao > 0
               OR producao_estimada_matosinhos > 0
               OR producao_estimada_mouzinho > 0
               OR producao_real_bolhao IS NOT NULL
               OR producao_real_matosinhos IS NOT NULL
               OR producao_real_mouzinho IS NOT NULL)
        ORDER BY data, sabor
    """, (data_inicio, data_fim))
    result = []
    for r in cursor.fetchall():
        real_b = float(r[4]) if r[4] is not None else None
        real_m = float(r[5]) if r[5] is not None else None
        real_mou = float(r[7]) if r[7] is not None else None
        est_total = float(r[2]) + float(r[3]) + float(r[6])
        real_parts = [x for x in [real_b, real_m, real_mou] if x is not None]
        result.append({
            'data': r[0],
            'sabor': r[1],
            'est': est_total,
            'real': sum(real_parts) if real_parts else None,
        })
    release_connection(conn)
    return result


def copiar_plano_dia_anterior(data_destino: date) -> int:
    """Copy the most recent previous plan's estimates to data_destino (without real values).
    Returns the number of rows copied."""
    conn = get_connection()
    cursor = conn.cursor()
    data_origem = data_destino - timedelta(days=1)
    cursor.execute("""
        SELECT sabor, pesagem_matosinhos, producao_estimada_bolhao, producao_estimada_matosinhos,
               COALESCE(producao_estimada_mouzinho, 0)
        FROM plano_producao
        WHERE data = %s AND no_plano = TRUE
    """, (data_origem,))
    rows = cursor.fetchall()
    if not rows:
        cursor.execute("""
            SELECT sabor, pesagem_matosinhos, producao_estimada_bolhao, producao_estimada_matosinhos,
                   COALESCE(producao_estimada_mouzinho, 0)
            FROM plano_producao
            WHERE data < %s AND no_plano = TRUE
            ORDER BY data DESC
            LIMIT 50
        """, (data_destino,))
        rows = cursor.fetchall()
    count = 0
    for r in rows:
        sabor = r[0]
        pesagem_mat = float(r[1] or 0)
        est_bol = float(r[2] or 0)
        est_mat = float(r[3] or 0)
        est_mou = float(r[4] or 0)
        cursor.execute("""
            INSERT INTO plano_producao (data, sabor, pesagem_matosinhos, producao_estimada_bolhao,
                                       producao_estimada_matosinhos, producao_estimada_mouzinho, no_plano)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (data, sabor) DO NOTHING
        """, (data_destino, sabor, pesagem_mat, est_bol, est_mat, est_mou))
        count += cursor.rowcount
    conn.commit()
    release_connection(conn)
    return count


def update_producao_real(data: date, sabor: str, real_bolhao: float, real_matosinhos: float,
                         real_mouzinho: float = None, real_outros: float = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE plano_producao
        SET producao_real_bolhao = %s,
            producao_real_matosinhos = %s,
            producao_real_mouzinho = %s,
            producao_real_outros = %s,
            updated_at = NOW()
        WHERE data = %s AND sabor = %s
    """, (real_bolhao, real_matosinhos, real_mouzinho, real_outros, data, sabor))
    conn.commit()
    release_connection(conn)

def upsert_plano_producao(data: date, sabor: str, pesagem_matosinhos: float,
                          producao_estimada_bolhao: float, producao_estimada_matosinhos: float,
                          producao_estimada_outros: float = 0.0,
                          producao_estimada_mouzinho: float = 0.0):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO plano_producao (data, sabor, pesagem_matosinhos, producao_estimada_bolhao,
                                   producao_estimada_matosinhos, producao_estimada_outros,
                                   producao_estimada_mouzinho)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (data, sabor) DO UPDATE SET
            pesagem_matosinhos = EXCLUDED.pesagem_matosinhos,
            producao_estimada_bolhao = EXCLUDED.producao_estimada_bolhao,
            producao_estimada_matosinhos = EXCLUDED.producao_estimada_matosinhos,
            producao_estimada_outros = EXCLUDED.producao_estimada_outros,
            producao_estimada_mouzinho = EXCLUDED.producao_estimada_mouzinho,
            updated_at = NOW()
    """, (data, sabor, pesagem_matosinhos, producao_estimada_bolhao,
          producao_estimada_matosinhos, producao_estimada_outros, producao_estimada_mouzinho))
    conn.commit()
    release_connection(conn)


def get_plano_ajuste_dia(data: date) -> list:
    """Returns all plan entries for a given day for the ajuste (adjustment) page."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sabor, pesagem_matosinhos,
               COALESCE(producao_estimada_bolhao, 0),
               COALESCE(producao_estimada_matosinhos, 0),
               COALESCE(producao_estimada_outros, 0),
               no_plano,
               COALESCE(producao_estimada_mouzinho, 0)
        FROM plano_producao
        WHERE data = %s
        ORDER BY sabor
    """, (data,))
    rows = cursor.fetchall()
    release_connection(conn)
    return [
        {
            'sabor': r[0],
            'pesagem_matosinhos': float(r[1] or 0),
            'estimado_bolhao': float(r[2] or 0),
            'estimado_matosinhos': float(r[3] or 0),
            'estimado_outros': float(r[4] or 0),
            'no_plano': bool(r[5]),
            'estimado_mouzinho': float(r[6] or 0),
        }
        for r in rows
    ]

def get_latest_bolhao_pesagem(sabor: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT quantidade_kg, data FROM stock_gelado
        WHERE loja = 'Bolhão'
          AND sabor = %s
        ORDER BY data DESC, id DESC LIMIT 1
    """, (sabor,))
    row = cursor.fetchone()
    release_connection(conn)
    if row:
        return float(row[0]), row[1]
    return None, None


@ttl_cache('ordem_producao', ttl=60)
def get_ordem_producao() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, sabor, posicao FROM ordem_producao ORDER BY posicao")
    rows = cursor.fetchall()
    release_connection(conn)
    return [{'id': r[0], 'sabor': r[1], 'posicao': r[2]} for r in rows]


def mover_sabor_ordem(sabor_id: int, direcao: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT posicao FROM ordem_producao WHERE id = %s", (sabor_id,))
    row = cursor.fetchone()
    if not row:
        release_connection(conn)
        return
    pos_atual = row[0]
    if direcao == 'up':
        cursor.execute("SELECT id, posicao FROM ordem_producao WHERE posicao < %s ORDER BY posicao DESC LIMIT 1", (pos_atual,))
    else:
        cursor.execute("SELECT id, posicao FROM ordem_producao WHERE posicao > %s ORDER BY posicao ASC LIMIT 1", (pos_atual,))
    vizinho = cursor.fetchone()
    if vizinho:
        cursor.execute("UPDATE ordem_producao SET posicao = %s WHERE id = %s", (vizinho[1], sabor_id))
        cursor.execute("UPDATE ordem_producao SET posicao = %s WHERE id = %s", (pos_atual, vizinho[0]))
    conn.commit()
    release_connection(conn)
    invalidate('ordem_producao')


def adicionar_sabor_ordem(sabor: str, posicao_depois_de: int = None):
    conn = get_connection()
    cursor = conn.cursor()
    if posicao_depois_de is not None:
        cursor.execute("SELECT posicao FROM ordem_producao WHERE id = %s", (posicao_depois_de,))
        row = cursor.fetchone()
        if row:
            pos_ref = row[0]
            cursor.execute("UPDATE ordem_producao SET posicao = posicao + 1 WHERE posicao > %s", (pos_ref,))
            nova_pos = pos_ref + 1
        else:
            cursor.execute("SELECT COALESCE(MAX(posicao), -1) + 1 FROM ordem_producao")
            nova_pos = cursor.fetchone()[0]
    else:
        cursor.execute("SELECT COALESCE(MAX(posicao), -1) + 1 FROM ordem_producao")
        nova_pos = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO ordem_producao (sabor, posicao) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (sabor, nova_pos)
    )
    conn.commit()
    release_connection(conn)
    invalidate('ordem_producao')


def remover_sabor_ordem(sabor_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT posicao FROM ordem_producao WHERE id = %s", (sabor_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM ordem_producao WHERE id = %s", (sabor_id,))
        cursor.execute("UPDATE ordem_producao SET posicao = posicao - 1 WHERE posicao > %s", (row[0],))
    conn.commit()
    release_connection(conn)
    invalidate('ordem_producao')


def reorder_ordem_producao(id_list: list):
    conn = get_connection()
    cursor = conn.cursor()
    for pos, sid in enumerate(id_list):
        cursor.execute("UPDATE ordem_producao SET posicao = %s WHERE id = %s", (pos, int(sid)))
    conn.commit()
    release_connection(conn)
    invalidate('ordem_producao')


def upsert_stock_producao(data: date, sabor: str, loja: str, quantidade_kg: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO stock_producao (data, sabor, loja, quantidade_kg)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (data, sabor, loja) DO UPDATE SET
            quantidade_kg = EXCLUDED.quantidade_kg,
            updated_at = NOW()
    """, (data, sabor, loja, quantidade_kg))
    conn.commit()
    release_connection(conn)


def add_stock_producao(data: date, sabor: str, loja: str, quantidade_kg: float):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO stock_producao (data, sabor, loja, quantidade_kg, store_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (data, sabor, loja) DO UPDATE SET
            quantidade_kg = stock_producao.quantidade_kg + EXCLUDED.quantidade_kg,
            updated_at = NOW()
    """, (data, sabor, loja, quantidade_kg, store_id))
    conn.commit()
    release_connection(conn)


def get_stock_producao_all(data: date = None) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sabor, loja, SUM(quantidade_kg) as total
        FROM stock_producao
        WHERE quantidade_kg > 0
        GROUP BY sabor, loja
        HAVING SUM(quantidade_kg) > 0
        ORDER BY sabor, loja
    """)
    rows = cursor.fetchall()
    release_connection(conn)
    return [{'sabor': r[0], 'loja': r[1], 'quantidade_kg': float(r[2])} for r in rows]


def get_stock_producao(data: date, sabor: str, loja: str) -> float:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(SUM(quantidade_kg), 0) FROM stock_producao
        WHERE sabor = %s AND loja = %s AND quantidade_kg > 0
    """, (sabor, loja))
    row = cursor.fetchone()
    release_connection(conn)
    return float(row[0]) if row else 0.0


def reduzir_stock_producao(data: date, sabor: str, loja: str, quantidade_kg: float) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, quantidade_kg FROM stock_producao
        WHERE sabor = %s AND loja = %s AND quantidade_kg > 0
        ORDER BY data ASC
    """, (sabor, loja))
    rows = cursor.fetchall()
    remaining = quantidade_kg
    updated = False
    for row_id, row_qty in rows:
        if remaining <= 0:
            break
        subtract = min(remaining, float(row_qty))
        cursor.execute("""
            UPDATE stock_producao
            SET quantidade_kg = GREATEST(quantidade_kg - %s, 0), updated_at = NOW()
            WHERE id = %s
        """, (subtract, row_id))
        remaining -= subtract
        updated = True
    conn.commit()
    release_connection(conn)
    return updated


def add_transferencia(data: date, sabor: str, loja_destino: str, quantidade_kg: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO transferencias (data, sabor, loja_destino, quantidade_kg)
        VALUES (%s, %s, %s, %s)
    """, (data, sabor, loja_destino, quantidade_kg))
    conn.commit()
    release_connection(conn)


def get_latest_pesagem_por_sabor(loja: str) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT ON (sabor) sabor, quantidade_kg, data
        FROM stock_gelado
        WHERE loja = %s
        ORDER BY sabor, data DESC, id DESC
    """, (loja,))
    rows = cursor.fetchall()
    release_connection(conn)
    return {r[0]: {'kg': float(r[1]), 'data': r[2]} for r in rows}


def criar_ordem_transferencia(data: date, area_origem: str, produto: str, quantidade: float, unidade: str = 'kg', loja_destino: str = 'Bolhão', sabor: str = None, criado_por: str = None, data_prevista: date = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ordens_transferencia (data, area_origem, produto, sabor, quantidade, unidade, loja_destino, criado_por, data_prevista)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (data, area_origem, produto, sabor, quantidade, unidade, loja_destino, criado_por, data_prevista or data))
    order_id = cursor.fetchone()[0]
    conn.commit()
    release_connection(conn)
    return order_id


def get_ordem_transferencia_by_id(ordem_id: int):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    cursor.execute("SELECT * FROM ordens_transferencia WHERE id = %s", (ordem_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None


def get_ordens_transferencia(status: str = None, loja_destino: str = None, area_origem: str = None, data: date = None, data_prevista: date = None) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT id, data, area_origem, produto, sabor, quantidade, unidade, loja_destino, status, criado_por, confirmado_por, confirmado_em, created_at, data_prevista
        FROM ordens_transferencia WHERE 1=1
    """
    params = []
    if status:
        query += " AND status = %s"
        params.append(status)
    if loja_destino:
        query += " AND loja_destino = %s"
        params.append(loja_destino)
    if area_origem:
        query += " AND area_origem = %s"
        params.append(area_origem)
    if data:
        query += " AND data = %s"
        params.append(data)
    if data_prevista:
        query += " AND data_prevista = %s"
        params.append(data_prevista)
    query += " ORDER BY created_at DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [{
        'id': r[0], 'data': r[1], 'area_origem': r[2], 'produto': r[3], 'sabor': r[4],
        'quantidade': float(r[5]), 'unidade': r[6], 'loja_destino': r[7], 'status': r[8],
        'criado_por': r[9], 'confirmado_por': r[10], 'confirmado_em': r[11], 'created_at': r[12],
        'data_prevista': r[13]
    } for r in rows]


def confirmar_ordem_transferencia(ordem_id: int, confirmado_por: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT area_origem, produto, sabor, quantidade, unidade, loja_destino
        FROM ordens_transferencia
        WHERE id = %s AND status = 'pendente'
    """, (ordem_id,))
    ordem = cursor.fetchone()
    if not ordem:
        release_connection(conn)
        return False
    area_origem, produto, sabor, quantidade, unidade, loja_destino = ordem
    cursor.execute("""
        UPDATE ordens_transferencia
        SET status = 'confirmada', confirmado_por = %s, confirmado_em = NOW()
        WHERE id = %s AND status = 'pendente'
    """, (confirmado_por, ordem_id))
    updated = cursor.rowcount > 0
    if updated:
        today = date.today()
        if area_origem == 'Gelado':
            cursor.execute("""
                INSERT INTO rececao_mercadoria (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (today, loja_destino, 'gelado', produto, sabor or produto, '', float(quantidade), 'kg'))
        elif area_origem in ('Pastelaria', 'Confeitaria'):
            cursor.execute("""
                INSERT INTO contagem_stock (data, loja, produto, quantidade, tipo)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (today, loja_destino, produto, int(quantidade), area_origem.lower()))
    conn.commit()
    release_connection(conn)
    return updated


def rejeitar_ordem_transferencia(ordem_id: int, confirmado_por: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE ordens_transferencia
        SET status = 'rejeitada', confirmado_por = %s, confirmado_em = NOW()
        WHERE id = %s AND status = 'pendente'
    """, (confirmado_por, ordem_id))
    updated = cursor.rowcount > 0
    conn.commit()
    release_connection(conn)
    return updated

