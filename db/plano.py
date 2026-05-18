import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache, invalidate
from db.stores import get_store_id_by_name
import json

def get_plano_producao(data: date, sabor: str) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT pesagem_matosinhos, producao_estimada_bolhao, producao_estimada_matosinhos,
                   no_plano, COALESCE(producao_estimada_outros, 0),
                   COALESCE(producao_estimada_mouzinho, 0)
            FROM plano_producao WHERE data = %s AND sabor = %s
        """, (data, sabor))
        row = cursor.fetchone()
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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO plano_producao (data, sabor, no_plano)
            VALUES (%s, %s, TRUE)
            ON CONFLICT (data, sabor) DO UPDATE SET
                no_plano = TRUE,
                updated_at = NOW()
        """, (data, sabor))
        conn.commit()


def get_plano_do_dia(data: date) -> list:
    with db_connection() as conn:
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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT data, sabor, SUM(quantidade_kg)
            FROM producao
            WHERE data BETWEEN %s AND %s
            GROUP BY data, sabor
            ORDER BY data, sabor
        """, (data_inicio, data_fim))
        result = [{'data': r[0], 'sabor': r[1], 'real': float(r[2])} for r in cursor.fetchall()]
    return result


def get_producao_manual_daily(data_inicio: date, data_fim: date) -> list:
    """Returns daily totals of manually registered production (real_bolhao + real_matosinhos)
    from plano_producao, grouped by date."""
    with db_connection() as conn:
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
    return result


def get_producao_balanca_por_sabor(data_inicio: date, data_fim: date) -> list:
    """Returns scale/Calybrabox production from the producao table grouped by (data, sabor),
    ordered by date desc then sabor."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT data, COALESCE(sabor, '(sem sabor)'), SUM(quantidade_kg)
            FROM producao
            WHERE data BETWEEN %s AND %s
            GROUP BY data, sabor
            ORDER BY data DESC, sabor
        """, (data_inicio, data_fim))
        result = [{'data': r[0], 'sabor': r[1], 'kg': float(r[2])} for r in cursor.fetchall()]
    return result


def get_plano_producao_historico(data_inicio: date, data_fim: date) -> list:
    with db_connection() as conn:
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
    return result


def copiar_plano_dia_anterior(data_destino: date) -> int:
    """Copy the most recent previous plan's estimates to data_destino (without real values).
    Returns the number of rows copied."""
    with db_connection() as conn:
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
    return count


def update_producao_real(data: date, sabor: str, real_bolhao: float, real_matosinhos: float,
                         real_mouzinho: float = None, real_outros: float = None):
    with db_connection() as conn:
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

def upsert_plano_producao(data: date, sabor: str, pesagem_matosinhos: float,
                          producao_estimada_bolhao: float, producao_estimada_matosinhos: float,
                          producao_estimada_outros: float = 0.0,
                          producao_estimada_mouzinho: float = 0.0):
    with db_connection() as conn:
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


def get_plano_ajuste_dia(data: date) -> list:
    """Returns all plan entries for a given day for the ajuste (adjustment) page."""
    with db_connection() as conn:
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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT quantidade_kg, data FROM stock_gelado
            WHERE loja = 'Bolhão'
              AND sabor = %s
            ORDER BY data DESC, id DESC LIMIT 1
        """, (sabor,))
        row = cursor.fetchone()
    if row:
        return float(row[0]), row[1]
    return None, None


@ttl_cache('ordem_producao', ttl=60)
def get_ordem_producao() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, sabor, posicao FROM ordem_producao ORDER BY posicao")
        rows = cursor.fetchall()
    return [{'id': r[0], 'sabor': r[1], 'posicao': r[2]} for r in rows]


def mover_sabor_ordem(sabor_id: int, direcao: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT posicao FROM ordem_producao WHERE id = %s", (sabor_id,))
        row = cursor.fetchone()
        if not row:
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
    invalidate('ordem_producao')


def adicionar_sabor_ordem(sabor: str, posicao_depois_de: int = None):
    with db_connection() as conn:
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
    invalidate('ordem_producao')


def remover_sabor_ordem(sabor_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT posicao FROM ordem_producao WHERE id = %s", (sabor_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute("DELETE FROM ordem_producao WHERE id = %s", (sabor_id,))
            cursor.execute("UPDATE ordem_producao SET posicao = posicao - 1 WHERE posicao > %s", (row[0],))
        conn.commit()
    invalidate('ordem_producao')


def reorder_ordem_producao(id_list: list):
    with db_connection() as conn:
        cursor = conn.cursor()
        for pos, sid in enumerate(id_list):
            cursor.execute("UPDATE ordem_producao SET posicao = %s WHERE id = %s", (pos, int(sid)))
        conn.commit()
    invalidate('ordem_producao')


def upsert_stock_producao(data: date, sabor: str, loja: str, quantidade_kg: float):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO stock_producao (data, sabor, loja, quantidade_kg)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (data, sabor, loja) DO UPDATE SET
                quantidade_kg = EXCLUDED.quantidade_kg,
                updated_at = NOW()
        """, (data, sabor, loja, quantidade_kg))
        conn.commit()


def add_stock_producao(data: date, sabor: str, loja: str, quantidade_kg: float):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO stock_producao (data, sabor, loja, quantidade_kg, store_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (data, sabor, loja) DO UPDATE SET
                quantidade_kg = stock_producao.quantidade_kg + EXCLUDED.quantidade_kg,
                updated_at = NOW()
        """, (data, sabor, loja, quantidade_kg, store_id))
        conn.commit()


def get_stock_producao_all(data: date = None) -> list:
    with db_connection() as conn:
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
    return [{'sabor': r[0], 'loja': r[1], 'quantidade_kg': float(r[2])} for r in rows]


def get_stock_producao(data: date, sabor: str, loja: str) -> float:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(quantidade_kg), 0) FROM stock_producao
            WHERE sabor = %s AND loja = %s AND quantidade_kg > 0
        """, (sabor, loja))
        row = cursor.fetchone()
    return float(row[0]) if row else 0.0


def reduzir_stock_producao(data: date, sabor: str, loja: str, quantidade_kg: float) -> bool:
    with db_connection() as conn:
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
    return updated


def add_transferencia(data: date, sabor: str, loja_destino: str, quantidade_kg: float):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO transferencias (data, sabor, loja_destino, quantidade_kg)
            VALUES (%s, %s, %s, %s)
        """, (data, sabor, loja_destino, quantidade_kg))
        conn.commit()


def get_latest_pesagem_por_sabor(loja: str) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg, data
            FROM stock_gelado
            WHERE loja = %s
            ORDER BY sabor, data DESC, id DESC
        """, (loja,))
        rows = cursor.fetchall()
    return {r[0]: {'kg': float(r[1]), 'data': r[2]} for r in rows}


def _insert_evento(cursor, ordem_id: int, event_type: str, utilizador: str | None, motivo: str | None = None):
    """Insert an audit event into transferencias_eventos within the caller's transaction.

    Any DB error is logged and re-raised so the enclosing transaction is rolled back.
    This ensures that a transfer state change never commits without a corresponding
    audit event — the audit trail is atomic with the state transition.
    """
    try:
        cursor.execute("""
            INSERT INTO transferencias_eventos (ordem_id, event_type, utilizador, motivo)
            VALUES (%s, %s, %s, %s)
        """, (ordem_id, event_type, utilizador, motivo))
    except Exception as exc:
        logger.error(
            "_insert_evento failed: ordem_id=%s event_type=%s utilizador=%s — %s",
            ordem_id, event_type, utilizador, exc,
        )
        raise


def get_or_create_pending_batch(data: date, area_origem: str, loja_destino: str) -> str:
    """Return batch_id for an existing pendente batch with matching (data, area_origem, loja_destino),
    or generate a new UUID if none exists. Never touches the DB — safe to call before any INSERT."""
    import uuid as _uuid
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT batch_id FROM ordens_transferencia
            WHERE data = %s AND area_origem = %s AND loja_destino = %s
              AND status = 'pendente' AND batch_id IS NOT NULL
            LIMIT 1
        """, (data, area_origem, loja_destino))
        row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    return str(_uuid.uuid4())


def criar_ordem_transferencia(data: date, area_origem: str, produto: str, quantidade: float, unidade: str = 'kg', loja_destino: str = 'Bolhão', sabor: str = None, criado_por: str = None, data_prevista: date = None, batch_id: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ordens_transferencia (data, area_origem, produto, sabor, quantidade, unidade, loja_destino, criado_por, data_prevista, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (data, area_origem, produto, sabor, quantidade, unidade, loja_destino, criado_por, data_prevista or data, batch_id))
        order_id = cursor.fetchone()[0]
        _insert_evento(cursor, order_id, 'criado', criado_por)
        conn.commit()
    return order_id


def get_ordem_transferencia_by_id(ordem_id: int):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM ordens_transferencia WHERE id = %s", (ordem_id,))
        row = cursor.fetchone()
    return dict(row) if row else None


def get_ordens_transferencia(status: str = None, loja_destino: str = None, area_origem: str = None, data: date = None, data_prevista: date = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, data, area_origem, produto, sabor, quantidade, unidade, loja_destino, status, criado_por, confirmado_por, confirmado_em, created_at, data_prevista, motivo_rejeicao, batch_id
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
    return [{
        'id': r[0], 'data': r[1], 'area_origem': r[2], 'produto': r[3], 'sabor': r[4],
        'quantidade': float(r[5]), 'unidade': r[6], 'loja_destino': r[7], 'status': r[8],
        'criado_por': r[9], 'confirmado_por': r[10], 'confirmado_em': r[11], 'created_at': r[12],
        'data_prevista': r[13], 'motivo_rejeicao': r[14], 'batch_id': r[15]
    } for r in rows]


def get_ordens_transferencia_with_events(
    status: str = None,
    loja_destino: str = None,
    area_origem: str = None,
    data_inicio: date = None,
    data_fim: date = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Return paginated orders with their audit events embedded, newest first.

    Returns:
        {
            'ordens': [...],   # list of order dicts with 'eventos' key
            'total': int,      # total matching rows (for pagination)
            'page': int,
            'per_page': int,
            'total_pages': int,
        }
    Each order dict has 'eventos': [{'event_type', 'utilizador', 'motivo', 'created_at'}, ...]
    ordered oldest-first.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        where = " WHERE 1=1"
        params = []
        if status:
            where += " AND o.status = %s"
            params.append(status)
        if loja_destino:
            where += " AND o.loja_destino = %s"
            params.append(loja_destino)
        if area_origem:
            where += " AND o.area_origem = %s"
            params.append(area_origem)
        if data_inicio:
            where += " AND o.data >= %s"
            params.append(data_inicio)
        if data_fim:
            where += " AND o.data <= %s"
            params.append(data_fim)

        # Total count
        cursor.execute(
            f"SELECT COUNT(*) FROM ordens_transferencia o{where}", params
        )
        total = cursor.fetchone()[0]

        page = max(1, page)
        per_page = max(1, min(per_page, 500))
        total_pages = max(1, -(-total // per_page))
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        query = (
            "SELECT o.id, o.data, o.area_origem, o.produto, o.sabor,"
            " o.quantidade, o.unidade, o.loja_destino, o.status,"
            " o.criado_por, o.confirmado_por, o.confirmado_em,"
            " o.created_at, o.data_prevista, o.motivo_rejeicao"
            f" FROM ordens_transferencia o{where}"
            " ORDER BY o.created_at DESC LIMIT %s OFFSET %s"
        )
        cursor.execute(query, params + [per_page, offset])
        rows = cursor.fetchall()
        ordens = [{
            'id': r[0], 'data': r[1], 'area_origem': r[2], 'produto': r[3], 'sabor': r[4],
            'quantidade': float(r[5]), 'unidade': r[6], 'loja_destino': r[7], 'status': r[8],
            'criado_por': r[9], 'confirmado_por': r[10], 'confirmado_em': r[11],
            'created_at': r[12], 'data_prevista': r[13], 'motivo_rejeicao': r[14],
            'eventos': [],
        } for r in rows]

        if ordens:
            # Fetch events in one batch for all orders on this page
            ordem_ids = [o['id'] for o in ordens]
            cursor.execute("""
                SELECT ordem_id, event_type, utilizador, motivo, created_at
                FROM transferencias_eventos
                WHERE ordem_id = ANY(%s)
                ORDER BY created_at ASC
            """, (ordem_ids,))
            idx = {o['id']: o for o in ordens}
            for ev_row in cursor.fetchall():
                ev = {
                    'event_type': ev_row[1],
                    'utilizador': ev_row[2],
                    'motivo': ev_row[3],
                    'created_at': ev_row[4],
                }
                if ev_row[0] in idx:
                    idx[ev_row[0]]['eventos'].append(ev)

            # Synthetic fallback: orders with no events get a 'criado' event
            # derived from their own columns (robustness if backfill was skipped)
            for o in ordens:
                if not o['eventos']:
                    o['eventos'] = [{
                        'event_type': 'criado',
                        'utilizador': o.get('criado_por'),
                        'motivo': None,
                        'created_at': o.get('created_at'),
                    }]

        return {
            'ordens': ordens,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
        }


def confirmar_ordem_transferencia(ordem_id: int, confirmado_por: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT area_origem, produto, sabor, quantidade, unidade, loja_destino
            FROM ordens_transferencia
            WHERE id = %s AND status = 'pendente'
        """, (ordem_id,))
        ordem = cursor.fetchone()
        if not ordem:
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
        if updated:
            _insert_evento(cursor, ordem_id, 'confirmado', confirmado_por)
        conn.commit()
    return updated


def rejeitar_ordem_transferencia(ordem_id: int, confirmado_por: str, motivo: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ordens_transferencia
            SET status = 'rejeitada', confirmado_por = %s, confirmado_em = NOW(),
                motivo_rejeicao = %s
            WHERE id = %s AND status = 'pendente'
        """, (confirmado_por, motivo or None, ordem_id))
        updated = cursor.rowcount > 0
        if updated:
            _insert_evento(cursor, ordem_id, 'rejeitado', confirmado_por, motivo)
        conn.commit()
    return updated


def get_stock_producao_by_loja(loja: str) -> list:
    """Return all active production stock rows for a specific loja.

    Returns a list of {'sabor': str, 'quantidade_kg': float} dicts.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sabor, SUM(quantidade_kg) as total
            FROM stock_producao
            WHERE loja = %s AND quantidade_kg > 0
            GROUP BY sabor
            HAVING SUM(quantidade_kg) > 0
            ORDER BY sabor
        """, (loja,))
        rows = cursor.fetchall()
    return [{'sabor': r[0], 'quantidade_kg': float(r[1])} for r in rows]


def upsert_pesagem_matosinhos_inicio(data: date, sabor: str, quantidade_kg: float) -> None:
    """Replace same-day Matosinhos/inicio stock_gelado entry for a given sabor.

    Deletes any existing row(s) for (data, Matosinhos, sabor, inicio) then
    inserts the new measurement.  This is the correct replacement semantics for
    the daily production start weighing.
    """
    from sabor_utils import normalise_sabor
    sabor = normalise_sabor(sabor)
    store_id = get_store_id_by_name('Matosinhos')
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM stock_gelado
            WHERE data = %s AND loja = 'Matosinhos' AND sabor = %s AND tipo = 'inicio'
        """, (data, sabor))
        cursor.execute("""
            INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, store_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (data, 'Matosinhos', sabor, quantidade_kg, 'inicio', store_id))
        conn.commit()


def get_latest_pesagem_por_sabor_all_lojas() -> dict:
    """Return the latest weighing per sabor for each loja (from stock_gelado).

    Only weighings from today or yesterday are considered current stock.  Any
    sabor whose most-recent entry is older than that is silently omitted so
    stale data never pollutes the Pesagens de Loja tile.

    Matosinhos 'inicio' rows (from the daily OCR weighing) are now included
    alongside Bolhão and Mouzinho fim-de-dia rows.

    Returns:
        {
          'Bolhão':      {sabor: {'kg': float, 'data': date}, ...},
          'Matosinhos':  {sabor: {'kg': float, 'data': date}, ...},
          'Mouzinho':    {sabor: {'kg': float, 'data': date}, ...},
          ...
        }
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT ON (loja, sabor) id, loja, sabor, quantidade_kg, data
            FROM stock_gelado
            WHERE data >= CURRENT_DATE - INTERVAL '1 day'
            ORDER BY loja, sabor, data DESC, id DESC
        """)
        rows = cursor.fetchall()
    result = {}
    for stock_id, loja, sabor, kg, dt in rows:
        if loja not in result:
            result[loja] = {}
        result[loja][sabor] = {'id': stock_id, 'kg': float(kg), 'data': dt}
    if result:
        all_ids = [entry['id'] for loja_data in result.values() for entry in loja_data.values()]
        from db.pastelaria import get_carapinas_for_stock_ids
        carapinas_map = get_carapinas_for_stock_ids(all_ids)
        for loja_data in result.values():
            for entry in loja_data.values():
                entry['carapinas'] = carapinas_map.get(entry['id'], [])
    return result


def get_pesagens_loja_range(days: int = 7) -> list:
    """Return all pesagens from stock_gelado within the last N days.

    Unlike get_latest_pesagem_por_sabor_all_lojas(), this returns every row
    (not just the most-recent per loja/sabor) so users can browse and edit
    historical records.

    Returns a list of dicts: {id, loja, sabor, kg, data}
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, loja, sabor, quantidade_kg, data
            FROM stock_gelado
            WHERE data >= CURRENT_DATE - (%s * INTERVAL '1 day')
            ORDER BY data DESC, loja, sabor
        """, (days,))
        rows = cursor.fetchall()
    base = [
        {'id': row[0], 'loja': row[1], 'sabor': row[2], 'kg': float(row[3]), 'data': row[4]}
        for row in rows
    ]
    if base:
        from db.pastelaria import get_carapinas_for_stock_ids
        carapinas_map = get_carapinas_for_stock_ids([r['id'] for r in base])
        for r in base:
            r['carapinas'] = carapinas_map.get(r['id'], [])
    return base


def set_stock_producao(sabor: str, loja: str, quantidade_kg: float):
    """Overwrite the total production stock for a sabor+loja combination.

    This collapses all existing rows into one (FIFO order is lost) and sets the
    new total.  Used by the manual stock-correction form on the Stock Gelado page.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        today = __import__('datetime').date.today()
        cursor.execute("""
            UPDATE stock_producao
            SET quantidade_kg = 0, updated_at = NOW()
            WHERE sabor = %s AND loja = %s AND quantidade_kg > 0
        """, (sabor, loja))
        cursor.execute("""
            INSERT INTO stock_producao (data, sabor, loja, quantidade_kg)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (data, sabor, loja) DO UPDATE SET
                quantidade_kg = EXCLUDED.quantidade_kg,
                updated_at = NOW()
        """, (today, sabor, loja, max(quantidade_kg, 0)))
        conn.commit()
