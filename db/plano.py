import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
import hashlib
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache, invalidate
from db.stores import get_store_id_by_name
import json


LEGACY_TRANSFER_RECEIPT_MAX_ID = 1085
ADMIN_RECEIPT_AUDIT_PREFIX = 'Regularização administrativa:'

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
              AND is_active = TRUE
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
        remaining = float(quantidade_kg)
        updated = False
        for row_id, row_qty in rows:
            if remaining <= 0:
                break
            subtract = min(remaining, float(row_qty))
            cursor.execute("""
                UPDATE stock_producao
                SET quantidade_kg = GREATEST(quantidade_kg - %s, 0),
                    updated_at = NOW()
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
            WHERE loja = %s AND is_active = TRUE
            ORDER BY sabor, data DESC, id DESC
        """, (loja,))
        rows = cursor.fetchall()
    return {r[0]: {'kg': float(r[1]), 'data': r[2]} for r in rows}


def get_effective_stock_por_sabor(loja: str) -> dict:
    """Return effective available stock per sabor for a store.

    Computes: latest pesagem kg minus transfer exits written after that
    flavor's latest weighing.

    Orders predating the pesagem are already reflected in the pesagem reading
    and must NOT be subtracted again.

    Returns the same shape as get_latest_pesagem_por_sabor:
      {sabor: {'kg': float, 'data': date, 'transferido_kg': float}}
    where 'kg' is the effective available quantity (floored at 0)
    and 'transferido_kg' is the total already executed since the weighing
    since the pesagem date.
    """
    pesagens = get_latest_pesagem_por_sabor(loja)
    if not pesagens:
        return {}

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sabor, data, SUM(ABS(quantidade)) AS total_pendente
            FROM rececao_mercadoria
            WHERE loja = %s
              AND tipo_produto = 'gelado'
              AND lote = 'transferencia_saida'
            GROUP BY sabor, data
        """, (loja,))
        pending_rows = cursor.fetchall()

    pending_by_sabor: dict[str, float] = {}
    for sabor, order_date, qty in pending_rows:
        pesagem_info = pesagens.get(sabor)
        if pesagem_info is None:
            continue
        if order_date >= pesagem_info['data']:
            pending_by_sabor[sabor] = pending_by_sabor.get(sabor, 0.0) + float(qty)

    result = {}
    for sabor, info in pesagens.items():
        transferido_kg = pending_by_sabor.get(sabor, 0.0)
        effective_kg = max(0.0, info['kg'] - transferido_kg)
        result[sabor] = {
            'kg': effective_kg,
            'data': info['data'],
            'transferido_kg': transferido_kg,
        }
    return result


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


def _legacy_transfer_receipt_summary(rows):
    eligible = []
    eligible_by_store = {}
    excluded_by_reason = {}

    for row in rows:
        order_id, status, destination_type, receipt_state, store_name = row
        store_name = str(store_name or '').strip()
        if (
            status == 'confirmada'
            and destination_type == 'loja'
            and receipt_state == 'por_verificar'
            and store_name
        ):
            eligible.append((int(order_id), store_name))
            eligible_by_store[store_name] = eligible_by_store.get(store_name, 0) + 1
            continue

        if destination_type == 'b2b':
            reason = 'Destino B2B'
        elif status != 'confirmada':
            reason = 'Execução não confirmada'
        elif receipt_state == 'aceite':
            reason = 'Receção já aceite'
        elif receipt_state == 'problema':
            reason = 'Problema reportado'
        elif destination_type != 'loja':
            reason = 'Destino diferente de loja'
        elif not store_name:
            reason = 'Loja de destino em falta'
        else:
            reason = 'Outro estado de receção'
        excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1

    snapshot_payload = json.dumps(
        eligible, ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')
    snapshot = hashlib.sha256(snapshot_payload).hexdigest()

    return {
        'max_order_id': LEGACY_TRANSFER_RECEIPT_MAX_ID,
        'total_legacy': len(rows),
        'eligible_count': len(eligible),
        'eligible_by_store': [
            {'store': name, 'count': count}
            for name, count in sorted(eligible_by_store.items())
        ],
        'excluded_count': len(rows) - len(eligible),
        'excluded_by_reason': [
            {'reason': reason, 'count': count}
            for reason, count in sorted(excluded_by_reason.items())
        ],
        'snapshot': snapshot,
        'eligible_ids': [order_id for order_id, _ in eligible],
    }


def get_legacy_transfer_receipt_preview():
    """Summarize the one-time legacy receipt set without changing any rows."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, status, destino_tipo, rececao_estado, loja_destino
            FROM ordens_transferencia
            WHERE id <= %s
            ORDER BY id
        """, (LEGACY_TRANSFER_RECEIPT_MAX_ID,))
        rows = cursor.fetchall()
    return _legacy_transfer_receipt_summary(rows)


def regularize_legacy_transfer_receipts(expected_count, expected_snapshot, actor):
    """Atomically regularize only the previewed legacy store receipts.

    This records administrative acceptance, not physical receipt evidence, and
    intentionally does not create or modify any inventory movement.
    """
    try:
        expected_count = int(expected_count)
    except (TypeError, ValueError):
        raise ValueError('Contagem de confirmação inválida.')
    actor = str(actor or '').strip()
    expected_snapshot = str(expected_snapshot or '').strip()
    if expected_count < 0:
        raise ValueError('Contagem de confirmação inválida.')
    if not actor or len(actor) > 100:
        raise ValueError('Não foi possível identificar o Gestor responsável.')
    if len(expected_snapshot) != 64:
        raise ValueError('A prévia expirou. Atualize a página e tente novamente.')

    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            # Lock the entire bounded legacy set so rows cannot enter or leave
            # eligibility while the preview snapshot is checked and applied.
            cursor.execute("""
                SELECT id, status, destino_tipo, rececao_estado, loja_destino
                FROM ordens_transferencia
                WHERE id <= %s
                ORDER BY id
                FOR UPDATE
            """, (LEGACY_TRANSFER_RECEIPT_MAX_ID,))
            rows = cursor.fetchall()
            current = _legacy_transfer_receipt_summary(rows)
            if (
                current['eligible_count'] != expected_count
                or current['snapshot'] != expected_snapshot
            ):
                conn.rollback()
                return {
                    'stale': True,
                    'updated_count': 0,
                    'current_count': current['eligible_count'],
                }

            order_ids = current['eligible_ids']
            if not order_ids:
                conn.commit()
                return {'stale': False, 'updated_count': 0, 'replayed': True}

            cursor.execute("""
                UPDATE ordens_transferencia
                SET rececao_estado = 'aceite',
                    aceite_por = %s,
                    aceite_em = NOW()
                WHERE id = ANY(%s)
                  AND id <= %s
                  AND status = 'confirmada'
                  AND destino_tipo = 'loja'
                  AND rececao_estado = 'por_verificar'
                RETURNING id
            """, (actor, order_ids, LEGACY_TRANSFER_RECEIPT_MAX_ID))
            updated_ids = [row[0] for row in cursor.fetchall()]
            if set(updated_ids) != set(order_ids):
                raise RuntimeError(
                    'O conjunto de receções antigas mudou durante a regularização.'
                )

            reason = (
                f'{ADMIN_RECEIPT_AUDIT_PREFIX} realizada por {actor}. '
                'Não constitui confirmação física da receção pela loja.'
            )
            for order_id in sorted(updated_ids):
                _insert_evento(cursor, order_id, 'aceite', actor, reason)

            conn.commit()
            return {
                'stale': False,
                'updated_count': len(updated_ids),
                'replayed': False,
            }
        except Exception:
            conn.rollback()
            raise


def get_or_create_pending_batch(
    data: date,
    area_origem: str,
    loja_destino: str,
    destino_tipo: str = 'loja',
    destino_nome: str = None,
) -> str:
    """Return batch_id for an existing pendente batch with matching (data, area_origem, loja_destino),
    or generate a new UUID if none exists. Never touches the DB — safe to call before any INSERT."""
    import uuid as _uuid
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT batch_id FROM ordens_transferencia
            WHERE data = %s AND area_origem = %s AND loja_destino = %s
              AND destino_tipo = %s
              AND COALESCE(destino_nome, '') = COALESCE(%s, '')
              AND status = 'pendente' AND batch_id IS NOT NULL
            LIMIT 1
        """, (data, area_origem, loja_destino, destino_tipo, destino_nome))
        row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    return str(_uuid.uuid4())


def criar_ordem_transferencia(
    data: date,
    area_origem: str,
    produto: str,
    quantidade: float,
    unidade: str = 'kg',
    loja_destino: str = 'Bolhão',
    sabor: str = None,
    criado_por: str = None,
    data_prevista: date = None,
    batch_id: str = None,
    destino_tipo: str = 'loja',
    destino_nome: str = None,
    loja_origem: str = None,
):
    if destino_tipo not in ('loja', 'b2b'):
        raise ValueError("Tipo de destino inválido")
    destino_nome = (destino_nome or '').strip() or None
    if destino_tipo == 'b2b':
        if not destino_nome:
            raise ValueError("A entidade destinatária é obrigatória para destinos B2B")
        loja_destino = 'B2B'
    else:
        destino_nome = None
    with db_connection() as conn:
        cursor = conn.cursor()
        produto_pastelaria_id = None
        if area_origem == 'Pastelaria':
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
            produto_pastelaria_id = (
                identity_row[0] if identity_row else None
            )
            if (
                produto_pastelaria_id is None
                and not produto.startswith('Bolo — ')
            ):
                raise ValueError(
                    f'Produto de Pastelaria sem identidade única: {produto}'
                )
        cursor.execute("""
            INSERT INTO ordens_transferencia (
                data, area_origem, produto, sabor, quantidade, unidade,
                loja_destino, criado_por, data_prevista, batch_id,
                destino_tipo, destino_nome, produto_pastelaria_id,
                status, confirmado_por, confirmado_em, rececao_estado,
                loja_origem
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, 'confirmada', %s, NOW(),
                CASE WHEN %s = 'b2b' THEN 'nao_aplicavel' ELSE 'por_verificar' END,
                %s
            )
            RETURNING id
        """, (
            data, area_origem, produto, sabor, quantidade, unidade,
            loja_destino, criado_por, data_prevista or data, batch_id,
            destino_tipo, destino_nome, produto_pastelaria_id, criado_por,
            destino_tipo, loja_origem,
        ))
        order_id = cursor.fetchone()[0]
        _insert_evento(cursor, order_id, 'criado', criado_por)
        _insert_evento(cursor, order_id, 'executado', criado_por)
        if destino_tipo == 'loja':
            _insert_transfer_receipt(
                cursor, order_id, data, area_origem, produto, sabor,
                quantidade, loja_destino, produto_pastelaria_id,
            )
        conn.commit()
    return order_id


def get_ordem_transferencia_by_id(ordem_id: int):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM ordens_transferencia WHERE id = %s", (ordem_id,))
        row = cursor.fetchone()
    return dict(row) if row else None


def get_ordens_transferencia(status: str = None, loja_destino: str = None, area_origem: str = None, data: date = None, data_prevista: date = None, loja_origem: str = None, limit: int = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, data, area_origem, produto, sabor, quantidade, unidade,
                   loja_destino, status, criado_por, confirmado_por,
                   confirmado_em, created_at, data_prevista, motivo_rejeicao,
                   batch_id, loja_origem, destino_tipo, destino_nome,
                   rececao_estado, aceite_por, aceite_em, problema_por,
                   problema_em, motivo_problema,
                   EXISTS (
                       SELECT 1
                       FROM transferencias_eventos audit_event
                       WHERE audit_event.ordem_id = ordens_transferencia.id
                         AND audit_event.event_type = 'aceite'
                         AND audit_event.motivo LIKE %s
                   )
            FROM ordens_transferencia WHERE 1=1
        """
        params = [f'{ADMIN_RECEIPT_AUDIT_PREFIX}%']
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
        if loja_origem:
            query += " AND loja_origem = %s"
            params.append(loja_origem)
        query += " ORDER BY created_at DESC"
        if limit:
            query += " LIMIT %s"
            params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
    return [{
        'id': r[0], 'data': r[1], 'area_origem': r[2], 'produto': r[3], 'sabor': r[4],
        'quantidade': float(r[5]), 'unidade': r[6], 'loja_destino': r[7], 'status': r[8],
        'criado_por': r[9], 'confirmado_por': r[10], 'confirmado_em': r[11], 'created_at': r[12],
        'data_prevista': r[13], 'motivo_rejeicao': r[14], 'batch_id': r[15], 'loja_origem': r[16],
        'destino_tipo': r[17] or 'loja', 'destino_nome': r[18],
        'rececao_estado': r[19] or 'por_verificar', 'aceite_por': r[20],
        'aceite_em': r[21], 'problema_por': r[22], 'problema_em': r[23],
        'motivo_problema': r[24],
        'rececao_regularizada_admin': bool(r[25]) if len(r) > 25 else False,
    } for r in rows]


def get_ordens_transferencia_with_events(
    status: str = None,
    loja_destino: str = None,
    area_origem: str = None,
    data_inicio: date = None,
    data_fim: date = None,
    page: int = 1,
    per_page: int = 50,
    rececao_estado: str = None,
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
        # The paginated history reader is only for final execution states.
        # Keep this in SQL so totals and page boundaries exclude pending rows.
        where = " WHERE o.status IN ('confirmada', 'rejeitada')"
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
        if rececao_estado == 'por_verificar':
            where += (
                " AND COALESCE(o.destino_tipo, 'loja') <> 'b2b'"
                " AND COALESCE(o.rececao_estado, 'por_verificar')"
                " = 'por_verificar'"
            )
        elif rececao_estado == 'aceite':
            where += (
                " AND COALESCE(o.destino_tipo, 'loja') <> 'b2b'"
                " AND COALESCE(o.rececao_estado, 'por_verificar') = 'aceite'"
                " AND NOT EXISTS ("
                " SELECT 1 FROM transferencias_eventos receipt_event"
                " WHERE receipt_event.ordem_id = o.id"
                " AND receipt_event.event_type = 'aceite'"
                " AND receipt_event.motivo LIKE %s)"
            )
            params.append(f'{ADMIN_RECEIPT_AUDIT_PREFIX}%')
        elif rececao_estado == 'problema':
            where += (
                " AND COALESCE(o.destino_tipo, 'loja') <> 'b2b'"
                " AND COALESCE(o.rececao_estado, 'por_verificar') = 'problema'"
            )
        elif rececao_estado == 'regularizada_admin':
            where += (
                " AND COALESCE(o.destino_tipo, 'loja') <> 'b2b'"
                " AND COALESCE(o.rececao_estado, 'por_verificar') = 'aceite'"
                " AND EXISTS ("
                " SELECT 1 FROM transferencias_eventos receipt_event"
                " WHERE receipt_event.ordem_id = o.id"
                " AND receipt_event.event_type = 'aceite'"
                " AND receipt_event.motivo LIKE %s)"
            )
            params.append(f'{ADMIN_RECEIPT_AUDIT_PREFIX}%')
        elif rececao_estado == 'nao_aplicavel':
            where += (
                " AND (COALESCE(o.destino_tipo, 'loja') = 'b2b'"
                " OR o.rececao_estado = 'nao_aplicavel')"
            )
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
            " o.created_at, o.data_prevista, o.motivo_rejeicao,"
            " o.destino_tipo, o.destino_nome, o.rececao_estado,"
            " o.aceite_por, o.aceite_em, o.problema_por, o.problema_em,"
            " o.motivo_problema"
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
            'destino_tipo': r[15] or 'loja', 'destino_nome': r[16],
            'rececao_estado': r[17] or 'por_verificar',
            'aceite_por': r[18], 'aceite_em': r[19],
            'problema_por': r[20], 'problema_em': r[21],
            'motivo_problema': r[22],
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
                    'is_admin_regularization': (
                        ev_row[1] == 'aceite'
                        and str(ev_row[3] or '').startswith(
                            ADMIN_RECEIPT_AUDIT_PREFIX
                        )
                    ),
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
                        'is_admin_regularization': False,
                    }]
                o['rececao_regularizada_admin'] = any(
                    ev.get('is_admin_regularization')
                    for ev in o['eventos']
                )

        return {
            'ordens': ordens,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
        }


def _insert_transfer_receipt(
    cursor, ordem_id: int, movement_date: date, area_origem: str,
    produto: str, sabor: str, quantidade: float, loja_destino: str,
    produto_pastelaria_id=None,
):
    """Materialize an internal destination movement exactly once per order."""
    if area_origem == 'Gelado':
        cursor.execute("""
            INSERT INTO rececao_mercadoria (
                data, loja, tipo_produto, produto, sabor, lote, quantidade,
                unidade, ordem_transferencia_id
            )
            VALUES (%s, %s, 'gelado', %s, %s, '', %s, 'kg', %s)
            ON CONFLICT (ordem_transferencia_id)
                WHERE ordem_transferencia_id IS NOT NULL DO NOTHING
        """, (
            movement_date, loja_destino, produto, sabor or produto,
            float(quantidade), ordem_id,
        ))
    elif area_origem in ('Pastelaria', 'Confeitaria'):
        if area_origem == 'Pastelaria' and movement_date.weekday() == 6:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f'pastelaria-count:{movement_date.isoformat()}',),
            )
        cursor.execute("""
            INSERT INTO contagem_stock (
                data, loja, produto, quantidade, tipo, origem,
                produto_pastelaria_id, ordem_transferencia_id
            )
            VALUES (
                %s, %s, %s, %s, %s, 'transferencia',
                CASE WHEN %s = 'Pastelaria' THEN %s::INTEGER
                     ELSE NULL::INTEGER END, %s
            )
            ON CONFLICT (ordem_transferencia_id)
                WHERE ordem_transferencia_id IS NOT NULL DO NOTHING
        """, (
            movement_date, loja_destino, produto, int(quantidade),
            area_origem.lower(), area_origem, produto_pastelaria_id, ordem_id,
        ))


def criar_ordens_transferencia_pastelaria(
    *, data: date, loja_destino: str, lines: list, criado_por: str,
    data_prevista: date, request_key: str,
) -> dict:
    """Create a Pastelaria transfer batch and debit its stock in one transaction.

    The request key makes browser retries idempotent. Per-product advisory
    locks serialize different requests competing for the same balance.
    """
    import hashlib
    import json as _json
    import uuid as _uuid
    from db.pastelaria_stock import (
        PastelariaStockError,
        _insert_stock_movement_cursor,
        _lock_stock_identity,
        _resolve_identity_cursor,
        _stock_state_cursor,
    )

    try:
        request_uuid = _uuid.UUID(str(request_key))
    except (TypeError, ValueError, AttributeError):
        raise PastelariaStockError("Formulário de transferência inválido.")
    request_uuid_text = str(request_uuid)

    actor = str(criado_por or '').strip()
    destination = str(loja_destino or '').strip()
    if not actor:
        raise PastelariaStockError("Não foi possível identificar o responsável.")
    if not destination:
        raise PastelariaStockError("A loja de destino é obrigatória.")
    if not data_prevista:
        raise PastelariaStockError("A data prevista de entrega é obrigatória.")

    requested_by_identity = {}
    for line in lines or []:
        identity_key = str(line.get('identity_key') or '').strip()
        try:
            quantity = int(line.get('quantidade'))
        except (TypeError, ValueError):
            raise PastelariaStockError(
                "As quantidades têm de ser números inteiros."
            )
        if not identity_key or quantity <= 0:
            raise PastelariaStockError(
                "Cada artigo tem de ter uma quantidade superior a zero."
            )
        requested_by_identity[identity_key] = (
            requested_by_identity.get(identity_key, 0) + quantity
        )
    if not requested_by_identity:
        raise PastelariaStockError("Indique pelo menos um artigo.")

    canonical_lines = [
        {"identity_key": key, "quantidade": quantity}
        for key, quantity in sorted(requested_by_identity.items())
    ]
    payload = {
        "data": data.isoformat(),
        "data_prevista": data_prevista.isoformat(),
        "loja_destino": destination,
        "lines": canonical_lines,
    }
    payload_hash = hashlib.sha256(
        _json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()
    batch_id = request_uuid_text

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'pastelaria-transfer-request:{request_uuid_text}',),
        )
        cursor.execute("""
            SELECT payload_hash, batch_id
            FROM pastelaria_stock_transfer_requests
            WHERE request_key = %s
        """, (request_uuid_text,))
        existing_request = cursor.fetchone()
        if existing_request:
            if existing_request[0].strip() != payload_hash:
                raise PastelariaStockError(
                    "Este formulário já foi utilizado com outros dados."
                )
            cursor.execute("""
                SELECT id FROM ordens_transferencia
                WHERE batch_id = %s AND area_origem = 'Pastelaria'
                ORDER BY id
            """, (existing_request[1],))
            return {
                "order_ids": [row[0] for row in cursor.fetchall()],
                "replayed": True,
            }

        identities = {}
        for identity_key in sorted(requested_by_identity):
            identities[identity_key] = _resolve_identity_cursor(
                cursor,
                identity_key,
                cake_plan_date=data_prevista,
                require_active=True,
            )

        for identity_key in sorted(identities):
            _lock_stock_identity(cursor, identity_key)

        for identity_key in sorted(identities):
            identity = identities[identity_key]
            available, opening_confirmed = _stock_state_cursor(
                cursor, identity_key
            )
            requested_quantity = requested_by_identity[identity_key]
            if not opening_confirmed:
                raise PastelariaStockError(
                    f"Saldo inicial por confirmar para {identity['produto']}. "
                    "Registe-o em Stock de Produção antes de transferir."
                )
            if available < requested_quantity:
                raise PastelariaStockError(
                    f"Saldo insuficiente de {identity['produto']}: "
                    f"disponível {available}, pedido {requested_quantity}."
                )

        cursor.execute("""
            INSERT INTO pastelaria_stock_transfer_requests (
                request_key, payload_hash, batch_id, criado_por
            )
            VALUES (%s, %s, %s, %s)
        """, (request_uuid_text, payload_hash, batch_id, actor))

        order_ids = []
        for identity_key in sorted(identities):
            identity = identities[identity_key]
            quantity = requested_by_identity[identity_key]
            cursor.execute("""
                INSERT INTO ordens_transferencia (
                    data, area_origem, produto, sabor, quantidade, unidade,
                    loja_destino, criado_por, data_prevista, batch_id,
                    destino_tipo, destino_nome, produto_pastelaria_id,
                    status, confirmado_por, confirmado_em, rececao_estado,
                    loja_origem
                )
                VALUES (
                    %s, 'Pastelaria', %s, NULL, %s, 'und',
                    %s, %s, %s, %s,
                    'loja', NULL, %s,
                    'confirmada', %s, NOW(), 'por_verificar',
                    NULL
                )
                RETURNING id
            """, (
                data, identity['produto'], quantity, destination, actor,
                data_prevista, batch_id, identity['produto_pastelaria_id'],
                actor,
            ))
            order_id = cursor.fetchone()[0]
            order_ids.append(order_id)
            _insert_evento(cursor, order_id, 'criado', actor)
            _insert_evento(cursor, order_id, 'executado', actor)
            _insert_stock_movement_cursor(
                cursor,
                identity=identity,
                tipo='transferencia_saida',
                quantidade=-quantity,
                data=data,
                responsavel=actor,
                motivo=f'Transferência para {destination} (ordem {order_id})',
                ordem_transferencia_id=order_id,
            )
            _insert_transfer_receipt(
                cursor,
                order_id,
                data,
                'Pastelaria',
                identity['produto'],
                None,
                quantity,
                destination,
                identity['produto_pastelaria_id'],
            )

        conn.commit()
    return {"order_ids": order_ids, "replayed": False}


def _normalizar_ordens_rececao_batch(ordem_ids):
    if not isinstance(ordem_ids, (list, tuple)) or not ordem_ids:
        raise ValueError("Indique pelo menos uma ordem de transferência.")
    if len(ordem_ids) > 1000:
        raise ValueError("O lote contém demasiadas ordens.")

    normalized = []
    seen = set()
    for order_id in ordem_ids:
        if (
            isinstance(order_id, bool)
            or not isinstance(order_id, int)
            or order_id <= 0
        ):
            raise ValueError("O lote contém um identificador de ordem inválido.")
        if order_id in seen:
            raise ValueError("O mesmo identificador de ordem foi repetido.")
        seen.add(order_id)
        normalized.append(order_id)
    return sorted(normalized)


def _lock_and_validate_rececao_batch(cursor, order_ids, loja_destino):
    cursor.execute("""
        SELECT id, batch_id, loja_destino, status, destino_tipo,
               COALESCE(rececao_estado, 'por_verificar'), motivo_problema
        FROM ordens_transferencia
        WHERE id = ANY(%s)
        ORDER BY id
    """, (order_ids,))
    initial_rows = cursor.fetchall()
    if len(initial_rows) != len(order_ids):
        raise ValueError(
            "Uma ou mais ordens do lote não existem."
        )

    batch_ids = {row[1] or None for row in initial_rows}
    if len(batch_ids) != 1:
        raise ValueError(
            "O lote contém ordens de transferências diferentes."
        )
    batch_id = next(iter(batch_ids))

    if batch_id is None:
        cursor.execute("""
            SELECT id, batch_id, loja_destino, status, destino_tipo,
                   COALESCE(rececao_estado, 'por_verificar'),
                   motivo_problema
            FROM ordens_transferencia
            WHERE id = ANY(%s)
            ORDER BY id
            FOR UPDATE
        """, (order_ids,))
    else:
        cursor.execute("""
            SELECT id, batch_id, loja_destino, status, destino_tipo,
                   COALESCE(rececao_estado, 'por_verificar'),
                   motivo_problema
            FROM ordens_transferencia
            WHERE id = ANY(%s) OR batch_id = %s
            ORDER BY id
            FOR UPDATE
        """, (order_ids, batch_id))
    locked_rows = cursor.fetchall()
    selected_set = set(order_ids)
    selected_rows = [
        row for row in locked_rows if row[0] in selected_set
    ]
    if len(selected_rows) != len(order_ids):
        raise ValueError("Uma ou mais ordens do lote já não existem.")

    if any(row[2] != loja_destino for row in selected_rows):
        raise ValueError("O lote contém ordens destinadas a outra loja.")
    if any(
        row[3] != 'confirmada' or row[4] != 'loja'
        for row in selected_rows
    ):
        raise ValueError(
            "O lote contém ordens que não podem ser recebidas por uma loja."
        )
    if {row[1] or None for row in selected_rows} != {batch_id}:
        raise ValueError(
            "O lote contém ordens de transferências diferentes."
        )

    if batch_id is None:
        if len(order_ids) != 1:
            raise ValueError(
                "Ordens sem identificador de lote têm de ser processadas "
                "individualmente."
            )
        batch_rows = [
            (row[0], row[5], row[6]) for row in selected_rows
        ]
    else:
        batch_rows = [
            (row[0], row[5], row[6])
            for row in locked_rows
            if row[1] == batch_id
            and row[2] == loja_destino
            and row[3] == 'confirmada'
            and row[4] == 'loja'
        ]

    selected_ids = set(order_ids)
    all_ids = {row[0] for row in batch_rows}
    pending_ids = {
        row[0] for row in batch_rows
        if row[1] == 'por_verificar'
    }
    if selected_ids != pending_ids and selected_ids != all_ids:
        raise ValueError(
            "Os identificadores não correspondem ao lote completo pendente."
        )
    return selected_rows


def confirmar_ordens_transferencia_batch(
    ordem_ids, loja_destino: str, confirmado_por: str,
):
    """Accept a whole destination-store batch once, without moving stock."""
    order_ids = _normalizar_ordens_rececao_batch(ordem_ids)
    destination = str(loja_destino or '').strip()
    actor = str(confirmado_por or '').strip()
    if not destination or len(destination) > 100:
        raise ValueError("Loja de destino inválida.")
    if not actor or len(actor) > 100:
        raise ValueError("Não foi possível identificar o responsável.")

    with db_connection() as conn:
        cursor = conn.cursor()
        selected_rows = _lock_and_validate_rececao_batch(
            cursor, order_ids, destination
        )
        if any(
            row[5] not in ('por_verificar', 'aceite')
            for row in selected_rows
        ):
            raise ValueError(
                "Uma ou mais ordens já têm outro estado de receção."
            )

        pending_ids = [
            row[0] for row in selected_rows
            if row[5] == 'por_verificar'
        ]
        if pending_ids:
            cursor.execute("""
                UPDATE ordens_transferencia
                SET rececao_estado = 'aceite', aceite_por = %s,
                    aceite_em = NOW(), problema_por = NULL,
                    problema_em = NULL, motivo_problema = NULL
                WHERE id = ANY(%s)
                  AND loja_destino = %s
                  AND status = 'confirmada'
                  AND destino_tipo = 'loja'
                  AND rececao_estado = 'por_verificar'
                RETURNING id
            """, (actor, pending_ids, destination))
            updated_ids = [row[0] for row in cursor.fetchall()]
            if set(updated_ids) != set(pending_ids):
                raise ValueError(
                    "O estado do lote mudou. Atualize a página e tente de novo."
                )
            for order_id in sorted(updated_ids):
                _insert_evento(cursor, order_id, 'aceite', actor)

        conn.commit()
    return {
        'updated_count': len(pending_ids),
        'replayed': not pending_ids,
    }


def reportar_problema_ordens_transferencia_batch(
    ordem_ids, loja_destino: str, reportado_por: str, motivo: str,
):
    """Record one problem report for a complete store batch, once only."""
    order_ids = _normalizar_ordens_rececao_batch(ordem_ids)
    destination = str(loja_destino or '').strip()
    actor = str(reportado_por or '').strip()
    reason = str(motivo or '').strip()
    if not destination or len(destination) > 100:
        raise ValueError("Loja de destino inválida.")
    if not actor or len(actor) > 100:
        raise ValueError("Não foi possível identificar o responsável.")
    if not reason:
        raise ValueError("O motivo do problema é obrigatório.")
    if len(reason) > 500:
        raise ValueError("O motivo do problema não pode exceder 500 caracteres.")

    with db_connection() as conn:
        cursor = conn.cursor()
        selected_rows = _lock_and_validate_rececao_batch(
            cursor, order_ids, destination
        )
        for row in selected_rows:
            state = row[5]
            if state not in ('por_verificar', 'problema'):
                raise ValueError(
                    "Uma ou mais ordens já têm outro estado de receção."
                )
            if state == 'problema' and row[6] != reason:
                raise ValueError(
                    "Já existe um problema registado com outro motivo."
                )

        pending_ids = [
            row[0] for row in selected_rows
            if row[5] == 'por_verificar'
        ]
        if pending_ids:
            cursor.execute("""
                UPDATE ordens_transferencia
                SET rececao_estado = 'problema', problema_por = %s,
                    problema_em = NOW(), motivo_problema = %s
                WHERE id = ANY(%s)
                  AND loja_destino = %s
                  AND status = 'confirmada'
                  AND destino_tipo = 'loja'
                  AND rececao_estado = 'por_verificar'
                RETURNING id
            """, (actor, reason, pending_ids, destination))
            updated_ids = [row[0] for row in cursor.fetchall()]
            if set(updated_ids) != set(pending_ids):
                raise ValueError(
                    "O estado do lote mudou. Atualize a página e tente de novo."
                )
            for order_id in sorted(updated_ids):
                _insert_evento(
                    cursor,
                    order_id,
                    'problema_reportado',
                    actor,
                    reason,
                )

        conn.commit()
    return {
        'updated_count': len(pending_ids),
        'replayed': not pending_ids,
    }


def confirmar_ordem_transferencia(
    ordem_id: int, confirmado_por: str, loja_destino: str = None,
):
    """Record the destination store's optional acceptance without moving stock."""
    with db_connection() as conn:
        cursor = conn.cursor()
        query = """
            UPDATE ordens_transferencia
            SET rececao_estado = 'aceite', aceite_por = %s, aceite_em = NOW(),
                problema_por = NULL, problema_em = NULL, motivo_problema = NULL
            WHERE id = %s AND status = 'confirmada'
              AND destino_tipo = 'loja' AND rececao_estado = 'por_verificar'
        """
        params = [confirmado_por, ordem_id]
        if loja_destino is not None:
            query += " AND loja_destino = %s"
            params.append(loja_destino)
        cursor.execute(query, params)
        updated = cursor.rowcount > 0
        if updated:
            _insert_evento(cursor, ordem_id, 'aceite', confirmado_por)
        conn.commit()
    return updated


def reportar_problema_ordem_transferencia(
    ordem_id: int, reportado_por: str, motivo: str,
    loja_destino: str = None,
):
    """Record a receipt discrepancy without changing the completed movement."""
    motivo = str(motivo or '').strip()
    if not motivo:
        raise ValueError("O motivo do problema é obrigatório")
    if len(motivo) > 500:
        raise ValueError("O motivo do problema não pode exceder 500 caracteres")
    with db_connection() as conn:
        cursor = conn.cursor()
        query = """
            UPDATE ordens_transferencia
            SET rececao_estado = 'problema', problema_por = %s,
                problema_em = NOW(), motivo_problema = %s
            WHERE id = %s AND status = 'confirmada'
              AND destino_tipo = 'loja' AND rececao_estado = 'por_verificar'
        """
        params = [reportado_por, motivo, ordem_id]
        if loja_destino is not None:
            query += " AND loja_destino = %s"
            params.append(loja_destino)
        cursor.execute(query, params)
        updated = cursor.rowcount > 0
        if updated:
            _insert_evento(
                cursor, ordem_id, 'problema_reportado', reportado_por, motivo
            )
        conn.commit()
    return updated


def criar_transferencia_entre_lojas(
    data: date, sabor: str, loja_origem: str, loja_destino: str,
    quantidade_kg: float, criado_por: str, data_prevista: date = None,
    destino_tipo: str = 'loja', destino_nome: str = None,
) -> int:
    """Create a store-to-store gelado transfer order.

    Atomically:
    1. Inserts a negative rececao_mercadoria entry (lote='transferencia_saida')
       on the source store so the stock-movement ledger deducts the exit.
    2. Inserts a completed ordens_transferencia row with loja_origem set.
    3. Creates the internal destination receipt (except for B2B).
    4. Records creation and execution audit events.

    Returns the new order ID.  Raises on any DB error (caller should handle).
    """
    if destino_tipo not in ('loja', 'b2b'):
        raise ValueError("Tipo de destino inválido")
    destino_nome = (destino_nome or '').strip() or None
    if destino_tipo == 'b2b':
        if not destino_nome:
            raise ValueError("A entidade destinatária é obrigatória para destinos B2B")
        loja_destino = 'B2B'
    else:
        destino_nome = None

    with db_connection() as conn:
        cursor = conn.cursor()
        qty = abs(float(quantidade_kg))
        cursor.execute("""
            INSERT INTO rececao_mercadoria
                (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade)
            VALUES (%s, %s, 'gelado', %s, %s, 'transferencia_saida', %s, 'kg')
        """, (data, loja_origem, sabor, sabor, -qty))
        cursor.execute("""
            INSERT INTO ordens_transferencia
                (data, area_origem, produto, sabor, quantidade, unidade,
                 loja_destino, loja_origem, status, criado_por, data_prevista,
                 destino_tipo, destino_nome, confirmado_por, confirmado_em,
                 rececao_estado)
            VALUES (%s, 'Gelado', %s, %s, %s, 'kg', %s, %s, 'confirmada',
                    %s, %s, %s, %s, %s, NOW(),
                    CASE WHEN %s = 'b2b' THEN 'nao_aplicavel' ELSE 'por_verificar' END)
            RETURNING id
        """, (
            data, sabor, sabor, qty, loja_destino, loja_origem, criado_por,
            data_prevista or data, destino_tipo, destino_nome, criado_por,
            destino_tipo,
        ))
        order_id = cursor.fetchone()[0]
        _insert_evento(cursor, order_id, 'criado', criado_por)
        _insert_evento(cursor, order_id, 'executado', criado_por)
        if destino_tipo == 'loja':
            _insert_transfer_receipt(
                cursor, order_id, data, 'Gelado', sabor, sabor, qty,
                loja_destino,
            )
        conn.commit()
    return order_id


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
    from db.pastelaria import upsert_stock_gelado_matosinhos
    upsert_stock_gelado_matosinhos(data, sabor, quantidade_kg)


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
              AND is_active = TRUE
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
              AND is_active = TRUE
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


def get_pesagens_loja_3dias(
    loja_nome: str,
    end_date: date | None = None,
) -> dict:
    """Return three consecutive closed calendar days for a store.

    Returns:
      {
        'dates': [date_n2, date_n1, date_n],   # sorted ascending (oldest first)
        'rows': [
          {
            'sabor': str,
            'dias': [
              {'id': int, 'kg': float, 'data_iso': str, 'data_fmt': str} or None,
              ...  # one per date in dates (same order)
            ]
          },
          ...
        ]
      }
    """
    from db.weighing_status import (
        get_daily_weighing_statuses,
        portugal_today,
    )

    last_day = end_date or (portugal_today() - timedelta(days=1))
    dates = [last_day - timedelta(days=2), last_day - timedelta(days=1), last_day]

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT ON (sabor, data) id, sabor, quantidade_kg, data
            FROM stock_gelado
            WHERE loja = %s AND data = ANY(%s)
              AND is_active = TRUE
            ORDER BY sabor, data, id DESC
        """, (loja_nome, dates))
        entries = cursor.fetchall()

    from collections import defaultdict
    sabor_date_map = defaultdict(dict)
    all_sabores = set()
    for stock_id, sabor, kg, dt in entries:
        all_sabores.add(sabor)
        sabor_date_map[sabor][dt] = {'id': stock_id, 'kg': float(kg), 'data': dt}

    result_rows = []
    for sabor in sorted(all_sabores):
        dia_entries = []
        for d in dates:
            entry = sabor_date_map[sabor].get(d)
            if entry:
                try:
                    data_fmt = d.strftime('%d/%m') if d else '-'
                    data_iso = d.strftime('%Y-%m-%d') if d else ''
                except Exception:
                    data_fmt = str(d)
                    data_iso = str(d)
                dia_entries.append({
                    'id': entry['id'],
                    'kg': entry['kg'],
                    'data_fmt': data_fmt,
                    'data_iso': data_iso,
                })
            else:
                dia_entries.append(None)
        result_rows.append({'sabor': sabor, 'dias': dia_entries})

    date_labels = []
    dates_iso = []
    for d in dates:
        try:
            date_labels.append(d.strftime('%d/%m'))
            dates_iso.append(d.strftime('%Y-%m-%d'))
        except Exception:
            date_labels.append(str(d))
            dates_iso.append(str(d))

    statuses = get_daily_weighing_statuses(dates, [loja_nome])
    date_statuses = [
        statuses.get((loja_nome, day), {
            'loja': loja_nome,
            'data': day,
            'data_iso': day.isoformat(),
            'data_fmt': day.strftime('%d/%m/%Y'),
            'state': 'missing',
            'label': 'Pesagem em falta',
            'entry_count': 0,
        })
        for day in dates
    ]
    return {
        'dates': dates,
        'date_labels': date_labels,
        'dates_iso': dates_iso,
        'date_statuses': date_statuses,
        'rows': result_rows,
    }


def get_movimentos_stock_gelado(loja: str, data_inicio: date, data_fim: date, sabor_filtro: str = None) -> dict:
    """Return a per-sabor stock-movement ledger for *loja* in [data_inicio, data_fim].

    Baseline
    --------
    Most-recent pesagem (stock_gelado) **within** [data_inicio, data_fim].
    If none exists in the window, falls back to the most-recent pesagem before
    data_inicio (flagged with 'out_of_period': True so the UI can show a notice).

    Movement sources (no double-counting)
    --------------------------------------
    1. ``ordens_transferencia`` WHERE loja_destino=loja AND area_origem='Gelado'
       AND status='confirmada' → confirmed inbound transfers (with order ID reference).
    2. ``rececao_mercadoria`` WHERE tipo_produto='gelado' AND lote != ''
       → direct physical receptions (OCR / manual; lote='' entries are always written
       by confirmar_ordem_transferencia and are captured via source 1 above).
    3. ``rececao_mercadoria`` WHERE tipo_produto='gelado' AND quantidade < 0
       → outgoing exits written by store-initiated transfers (Task #417).
    4. ``ordens_transferencia`` WHERE loja_origem=loja (column added by Task #417)
       → explicit outbound transfers when the column is present; safe no-op otherwise.

    Returns
    -------
    {
      sabor: {
        'baseline':          {'kg': float, 'data': date, 'out_of_period': bool},
        'movimentos':        [{'data', 'tipo', 'quantidade_kg', 'descricao', 'referencia'}, …],
        'saldo_calculado':   float,
        'pesagem_posterior': {'kg': float, 'data': date} | None,
      }
    }
    """
    with db_connection() as conn:
        cursor = conn.cursor()

        sabor_cond = "AND sabor = %s" if sabor_filtro else ""

        # ── 1. Find baseline pesagem within [data_inicio, data_fim] ──────────
        params_in = [loja, data_inicio, data_fim]
        if sabor_filtro:
            params_in.append(sabor_filtro)
        cursor.execute(f"""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg::float, data
            FROM stock_gelado
            WHERE loja = %s AND data BETWEEN %s AND %s
              AND is_active = TRUE {sabor_cond}
            ORDER BY sabor, data DESC, id DESC
        """, params_in)
        in_period = {r[0]: (float(r[1]), r[2], False) for r in cursor.fetchall()}

        # Sabores without an in-period baseline → look before data_inicio
        if sabor_filtro:
            missing_sabores = [] if sabor_filtro in in_period else [sabor_filtro]
        else:
            missing_sabores = None  # resolved below after all-sabores query

        if not in_period:
            # No baselines at all in the period; look pre-period for all sabores
            params_pre = [loja, data_inicio]
            if sabor_filtro:
                params_pre.append(sabor_filtro)
            cursor.execute(f"""
                SELECT DISTINCT ON (sabor) sabor, quantidade_kg::float, data
                FROM stock_gelado
                WHERE loja = %s AND data < %s
                  AND is_active = TRUE {sabor_cond}
                ORDER BY sabor, data DESC, id DESC
            """, params_pre)
            pre_period = {r[0]: (float(r[1]), r[2], True) for r in cursor.fetchall()}
        else:
            if sabor_filtro:
                # Specific sabor was requested and found in-period — no pre-period
                # lookup needed and we must NOT query other sabores.
                pre_period = {}
            else:
                # No sabor filter: supplement with pre-period baselines for any
                # sabores that have pesagens before data_inicio but not within it.
                in_sabores = list(in_period.keys())
                cursor.execute("""
                    SELECT DISTINCT ON (sabor) sabor, quantidade_kg::float, data
                    FROM stock_gelado
                    WHERE loja = %s AND data < %s
                      AND is_active = TRUE AND sabor != ALL(%s)
                    ORDER BY sabor, data DESC, id DESC
                """, [loja, data_inicio, in_sabores])
                pre_period = {r[0]: (float(r[1]), r[2], True) for r in cursor.fetchall()}

        baselines = {**pre_period, **in_period}  # in_period wins on conflict
        if not baselines:
            return {}

        result = {}
        sabores = list(baselines.keys())
        for sabor, (kg_b, dt_b, oop) in baselines.items():
            result[sabor] = {
                'baseline': {'kg': float(kg_b), 'data': dt_b, 'out_of_period': oop},
                'movimentos': [],
                'saldo_calculado': float(kg_b),
                'pesagem_posterior': None,
            }

        # ── 2. Confirmed inbound transfers from ordens_transferencia ─────────
        min_baseline_date = min(v['baseline']['data'] for v in result.values())
        cursor.execute("""
            SELECT sabor, confirmado_em::date, quantidade::float, id
            FROM ordens_transferencia
            WHERE loja_destino = %s AND area_origem = 'Gelado'
              AND status = 'confirmada' AND sabor = ANY(%s)
              AND confirmado_em::date > %s AND confirmado_em::date <= %s
            ORDER BY confirmado_em, id
        """, (loja, sabores, min_baseline_date, data_fim))
        for sabor_r, data_r, qty, ordem_id in cursor.fetchall():
            if sabor_r not in result:
                continue
            if data_r <= result[sabor_r]['baseline']['data']:
                continue
            result[sabor_r]['movimentos'].append({
                'data': data_r,
                'tipo': 'entrada',
                'quantidade_kg': float(qty),
                'descricao': 'Transferência recebida',
                'referencia': f'#{ordem_id}',
            })
            result[sabor_r]['saldo_calculado'] += float(qty)

        # ── 3. Direct positive receptions: lote!='' AND qty>0 ───────────────
        # lote='' entries are always written by confirmar_ordem_transferencia;
        # they are already captured via source 2 (ordens_transferencia) above.
        # lote='transferencia_saida' entries are outgoing exits handled in source 4.
        # Only positive quantities here to avoid any overlap with outbound logic.
        cursor.execute("""
            SELECT sabor, data, quantidade::float, lote
            FROM rececao_mercadoria
            WHERE loja = %s AND tipo_produto = 'gelado'
              AND sabor = ANY(%s)
              AND data > %s AND data <= %s
              AND lote IS NOT NULL AND lote != ''
              AND quantidade > 0
            ORDER BY data, id
        """, (loja, sabores, min_baseline_date, data_fim))
        for sabor_r, data_r, qty, lote in cursor.fetchall():
            if sabor_r not in result:
                continue
            if data_r <= result[sabor_r]['baseline']['data']:
                continue
            result[sabor_r]['movimentos'].append({
                'data': data_r,
                'tipo': 'entrada',
                'quantidade_kg': float(qty),
                'descricao': 'Receção',
                'referencia': lote,
            })
            result[sabor_r]['saldo_calculado'] += float(qty)

        # ── 4. Outbound: two-source merge with explicit deduplication ────────
        # Source A: ordens_transferencia WHERE loja_origem=loja (non-NULL).
        #   Available only when the column exists (Task #417). Provides order ID
        #   and destination for the description.
        # Source B: rececao_mercadoria WHERE lote='transferencia_saida' AND qty<0.
        #   Fallback for rows where loja_origem is NULL (mixed/transition data) or
        #   where the column does not yet exist.
        # Deduplication: if an order from Source A and a rececao row from Source B
        #   share the same (sabor, date, |qty| within 0.01 kg), only the order entry
        #   is kept. This prevents double-counting when Task #417 writes both records
        #   for the same confirmed outbound movement.

        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'ordens_transferencia' AND column_name = 'loja_origem'
        """)
        has_loja_origem = cursor.fetchone() is not None

        # Keys already counted via orders — (sabor, date, rounded_qty)
        outbound_order_keys: set = set()

        if has_loja_origem:
            cursor.execute("""
                SELECT sabor, data, quantidade::float, id, loja_destino
                FROM ordens_transferencia
                WHERE loja_origem = %s AND loja_origem IS NOT NULL
                  AND area_origem = 'Gelado'
                  AND status IN ('pendente', 'confirmada') AND sabor = ANY(%s)
                  AND data > %s AND data <= %s
                ORDER BY data, id
            """, (loja, sabores, min_baseline_date, data_fim))
            for sabor_r, data_r, qty, ordem_id, loja_dest in cursor.fetchall():
                if sabor_r not in result:
                    continue
                if data_r <= result[sabor_r]['baseline']['data']:
                    continue
                qty_abs = round(abs(float(qty)), 2)
                outbound_order_keys.add((sabor_r, data_r, qty_abs))
                result[sabor_r]['movimentos'].append({
                    'data': data_r,
                    'tipo': 'saida',
                    'quantidade_kg': -qty_abs,
                    'descricao': f'Enviado para {loja_dest}',
                    'referencia': f'#{ordem_id}',
                })
                result[sabor_r]['saldo_calculado'] -= qty_abs

        # Source B: rececao_mercadoria transfer-exit marker — covers NULL loja_origem
        # rows (transition data) and the pre-Task-#417 state (column absent).
        # Only rows explicitly marked lote='transferencia_saida' are considered;
        # other negative rows (corrections, adjustments) are ignored per spec.
        cursor.execute("""
            SELECT sabor, data, quantidade::float, lote
            FROM rececao_mercadoria
            WHERE loja = %s AND tipo_produto = 'gelado'
              AND sabor = ANY(%s)
              AND data > %s AND data <= %s
              AND lote = 'transferencia_saida' AND quantidade < 0
            ORDER BY data, id
        """, (loja, sabores, min_baseline_date, data_fim))
        for sabor_r, data_r, qty, lote in cursor.fetchall():
            if sabor_r not in result:
                continue
            if data_r <= result[sabor_r]['baseline']['data']:
                continue
            qty_abs = round(abs(float(qty)), 2)
            if (sabor_r, data_r, qty_abs) in outbound_order_keys:
                continue  # already captured by Source A — skip to avoid double-count
            result[sabor_r]['movimentos'].append({
                'data': data_r,
                'tipo': 'saida',
                'quantidade_kg': -qty_abs,
                'descricao': 'Transferência enviada',
                'referencia': lote,
            })
            result[sabor_r]['saldo_calculado'] -= qty_abs

        # ── 5. Find pesagem_posterior for each sabor (comparison) ────────────
        for sabor in sabores:
            baseline_dt = result[sabor]['baseline']['data']
            cursor.execute("""
                SELECT quantidade_kg::float, data
                FROM stock_gelado
                WHERE loja = %s AND sabor = %s AND data > %s AND data <= %s
                  AND is_active = TRUE
                ORDER BY data DESC, id DESC
                LIMIT 1
            """, (loja, sabor, baseline_dt, data_fim))
            post_row = cursor.fetchone()
            if post_row:
                result[sabor]['pesagem_posterior'] = {
                    'kg': float(post_row[0]),
                    'data': post_row[1],
                }

        for sabor in result:
            result[sabor]['movimentos'].sort(key=lambda m: m['data'])
            result[sabor]['saldo_calculado'] = round(result[sabor]['saldo_calculado'], 4)

        return result


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
