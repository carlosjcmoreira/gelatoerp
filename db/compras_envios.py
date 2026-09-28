"""Audited dispatch and receipt records for store purchasing requests.

This ledger is operational evidence only. It deliberately does not create
transfer orders, stock movements, supplier orders, or accounting records.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from psycopg2.extras import RealDictCursor

from db.connection import db_connection


_MAX_QUANTITY = Decimal("999999999.999")
_QUANTUM = Decimal("0.001")


def _decimal_quantity(value, allow_zero=False) -> Decimal:
    try:
        raw_quantity = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        raise ValueError("Indique uma quantidade válida.")
    if not raw_quantity.is_finite() or raw_quantity > _MAX_QUANTITY:
        raise ValueError("A quantidade indicada é inválida.")
    try:
        quantity = raw_quantity.quantize(_QUANTUM)
    except InvalidOperation:
        raise ValueError("A quantidade indicada é inválida.")
    if raw_quantity != quantity:
        raise ValueError("Use no máximo três casas decimais.")
    if not quantity.is_finite() or quantity > _MAX_QUANTITY:
        raise ValueError("A quantidade indicada é inválida.")
    if quantity < 0 or (quantity == 0 and not allow_zero):
        raise ValueError(
            "A quantidade tem de ser maior que zero."
            if not allow_zero else "A quantidade não pode ser negativa."
        )
    return quantity


def _uuid_value(value, message):
    try:
        return UUID(str(value or "").strip())
    except (TypeError, ValueError, AttributeError):
        raise ValueError(message)


def _payload_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _order_tables(order_type):
    if order_type == "semanal":
        return (
            "compras_encomendas_semanais",
            "compras_encomendas_semanais_linhas",
            "encomenda_id",
            "versao_submetida",
            "submetida, em_preparacao",
        )
    if order_type == "urgente":
        return (
            "compras_pedidos_urgentes",
            "compras_pedidos_urgentes_linhas",
            "pedido_id",
            None,
            "submetida, em_preparacao",
        )
    raise ValueError("Tipo de pedido inválido.")


def _get_order_and_lines(cursor, order_type, order_id, for_update=False):
    order_table, line_table, foreign_key, version_column, _ = _order_tables(order_type)
    version_sql = (
        f", {version_column} AS versao_pedido"
        if version_column else ", NULL::integer AS versao_pedido"
    )
    cursor.execute(
        f"""
        SELECT id, store_id, status{version_sql}
        FROM {order_table}
        WHERE id = %s
        """ + (" FOR UPDATE" if for_update else ""),
        (order_id,),
    )
    order = cursor.fetchone()
    if not order:
        return None, []

    cursor.execute(
        f"""
        SELECT id AS pedido_linha_id, artigo_id, produto_snapshot,
               unidade_snapshot, quantidade,
               origem_id_snapshot, origem_tipo_snapshot, origem_nome_snapshot,
               origem_supplier_id_snapshot, origem_supplier_nome_snapshot,
               fornecedor_oficial_id_snapshot,
               fornecedor_oficial_nome_snapshot
        FROM {line_table}
        WHERE {foreign_key} = %s
        ORDER BY id
        """ + (" FOR UPDATE" if for_update else ""),
        (order_id,),
    )
    return dict(order), [dict(row) for row in cursor.fetchall()]


def _already_processed(cursor, request_key, payload_hash, table):
    cursor.execute(
        f"""
        SELECT id, payload_hash FROM {table}
        WHERE request_key = %s
        """,
        (str(request_key),),
    )
    existing = cursor.fetchone()
    if not existing:
        return None
    if existing["payload_hash"].strip() != payload_hash:
        raise ValueError(
            "Esta submissão já foi usada com outros dados. Atualize a página."
        )
    return int(existing["id"])


def _prior_sent(cursor, order_type, order_id, article_id, order_line_id):
    cursor.execute(
        """
        SELECT COALESCE(SUM(l.quantidade_enviada), 0) AS total
        FROM compras_pedidos_envios e
        JOIN compras_pedidos_envios_linhas l ON l.envio_id = e.id
        WHERE e.tipo_pedido = %s AND e.pedido_id = %s
          AND (
              (l.artigo_id IS NOT NULL AND l.artigo_id = %s)
              OR (l.artigo_id IS NULL AND l.pedido_linha_id = %s)
          )
        """,
        (order_type, order_id, article_id, order_line_id),
    )
    return Decimal(str(cursor.fetchone()["total"] or "0"))


def create_dispatch(
    order_type: str,
    order_id: int,
    lines: list[dict],
    actor: str,
    request_key: str,
    observations: str = "",
) -> dict:
    """Record one manual dispatch, allowing partial dispatches per order line."""
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise ValueError("Pedido inválido.")
    if order_id <= 0:
        raise ValueError("Pedido inválido.")
    request_uuid = _uuid_value(request_key, "Chave do envio inválida.")
    observations = str(observations or "").strip()[:4000]

    requested = {}
    for raw in lines or []:
        try:
            line_id = int(raw.get("pedido_linha_id"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Uma linha do pedido é inválida.")
        if line_id <= 0 or line_id in requested:
            raise ValueError("A mesma linha não pode aparecer duas vezes.")
        requested[line_id] = _decimal_quantity(raw.get("quantidade"))
    if not requested:
        raise ValueError("Indique pelo menos uma quantidade a enviar.")

    payload_hash = _payload_hash({
        "tipo_pedido": order_type,
        "pedido_id": order_id,
        "observacoes": observations,
        "linhas": [
            {"pedido_linha_id": line_id, "quantidade": str(quantity)}
            for line_id, quantity in sorted(requested.items())
        ],
    })

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
            (str(request_uuid),),
        )
        existing_id = _already_processed(
            cursor, request_uuid, payload_hash, "compras_pedidos_envios"
        )
        if existing_id is not None:
            conn.commit()
            return {"id": existing_id, "duplicate": True}

        order, order_lines = _get_order_and_lines(
            cursor, order_type, order_id, for_update=True
        )
        if not order:
            raise ValueError("Pedido não encontrado.")
        if order["status"] not in ("submetida", "em_preparacao"):
            raise ValueError("Só é possível enviar um pedido submetido ou em preparação.")
        by_id = {int(line["pedido_linha_id"]): line for line in order_lines}
        if any(line_id not in by_id for line_id in requested):
            raise ValueError("Uma das linhas já não pertence à versão atual do pedido.")

        for line_id, quantity in requested.items():
            line = by_id[line_id]
            already_sent = _prior_sent(
                cursor, order_type, order_id, line.get("artigo_id"), line_id
            )
            remaining = Decimal(str(line["quantidade"])) - already_sent
            if quantity > remaining:
                raise ValueError(
                    f'A quantidade a enviar de "{line["produto_snapshot"]}" '
                    f'excede o pendente ({remaining:.3f}).'
                )

        cursor.execute(
            """
            INSERT INTO compras_pedidos_envios
                (tipo_pedido, pedido_id, store_id, versao_pedido, request_key,
                 payload_hash, observacoes, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                order_type, order_id, order["store_id"], order["versao_pedido"],
                str(request_uuid), payload_hash, observations,
                str(actor or "sistema")[:100],
            ),
        )
        shipment_id = int(cursor.fetchone()["id"])
        for line_id, quantity in requested.items():
            line = by_id[line_id]
            cursor.execute(
                """
                INSERT INTO compras_pedidos_envios_linhas
                    (envio_id, pedido_linha_id, artigo_id, produto_snapshot,
                     unidade_snapshot, origem_id_snapshot, origem_tipo_snapshot,
                     origem_nome_snapshot, origem_supplier_id_snapshot,
                     origem_supplier_nome_snapshot, fornecedor_oficial_id_snapshot,
                     fornecedor_oficial_nome_snapshot, quantidade_pedida_snapshot,
                     quantidade_enviada)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    shipment_id, line_id, line.get("artigo_id"),
                    line["produto_snapshot"], line["unidade_snapshot"],
                    line.get("origem_id_snapshot"),
                    line.get("origem_tipo_snapshot"),
                    line.get("origem_nome_snapshot"),
                    line.get("origem_supplier_id_snapshot"),
                    line.get("origem_supplier_nome_snapshot"),
                    line.get("fornecedor_oficial_id_snapshot"),
                    line.get("fornecedor_oficial_nome_snapshot"),
                    line["quantidade"], quantity,
                ),
            )
        conn.commit()
    return {"id": shipment_id, "duplicate": False}


def _fetch_shipment_lines(cursor, shipment_id):
    cursor.execute(
        """
        SELECT l.id, l.pedido_linha_id, l.artigo_id, l.produto_snapshot,
               l.unidade_snapshot, l.origem_id_snapshot,
               l.origem_tipo_snapshot, l.origem_nome_snapshot,
               l.origem_supplier_id_snapshot, l.origem_supplier_nome_snapshot,
               l.fornecedor_oficial_id_snapshot,
               l.fornecedor_oficial_nome_snapshot, l.quantidade_enviada,
               l.quantidade_pedida_snapshot,
               COALESCE(SUM(r.quantidade_recebida), 0) AS quantidade_recebida
        FROM compras_pedidos_envios_linhas l
        LEFT JOIN compras_pedidos_rececoes_linhas r ON r.envio_linha_id = l.id
        WHERE l.envio_id = %s
        GROUP BY l.id
        ORDER BY l.produto_snapshot, l.id
        """,
        (shipment_id,),
    )
    result = []
    for row in cursor.fetchall():
        line = dict(row)
        sent = Decimal(str(line["quantidade_enviada"]))
        received = Decimal(str(line["quantidade_recebida"] or "0"))
        line["quantidade_enviada"] = float(sent)
        line["quantidade_recebida"] = float(received)
        if line["quantidade_pedida_snapshot"] is not None:
            line["quantidade_pedida_snapshot"] = float(
                line["quantidade_pedida_snapshot"]
            )
        line["quantidade_pendente"] = float(max(Decimal("0"), sent - received))
        result.append(line)
    return result


def _fetch_receipts(cursor, shipment_id):
    cursor.execute(
        """
        SELECT id, status, observacoes, received_by, received_at
        FROM compras_pedidos_rececoes
        WHERE envio_id = %s
        ORDER BY received_at DESC, id DESC
        """,
        (shipment_id,),
    )
    receipts = [dict(row) for row in cursor.fetchall()]
    for receipt in receipts:
        cursor.execute(
            """
            SELECT rl.envio_linha_id, sl.produto_snapshot,
                   rl.quantidade_recebida, rl.discrepancia
            FROM compras_pedidos_rececoes_linhas rl
            JOIN compras_pedidos_envios_linhas sl ON sl.id = rl.envio_linha_id
            WHERE rl.rececao_id = %s
            ORDER BY sl.produto_snapshot, rl.id
            """,
            (receipt["id"],),
        )
        receipt["linhas"] = [
            dict(row) for row in cursor.fetchall()
        ]
        for line in receipt["linhas"]:
            line["quantidade_recebida"] = float(line["quantidade_recebida"])
    return receipts


def _fetch_shipment(cursor, shipment_id):
    cursor.execute(
        """
        SELECT e.*, s.name AS loja_nome
        FROM compras_pedidos_envios e
        JOIN stores s ON s.id = e.store_id
        WHERE e.id = %s
        """,
        (shipment_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    shipment = dict(row)
    shipment["linhas"] = _fetch_shipment_lines(cursor, shipment_id)
    shipment["rececoes"] = _fetch_receipts(cursor, shipment_id)
    shipment["quantidade_enviada_total"] = sum(
        line["quantidade_enviada"] for line in shipment["linhas"]
    )
    shipment["quantidade_recebida_total"] = sum(
        line["quantidade_recebida"] for line in shipment["linhas"]
    )
    shipment["quantidade_pendente_total"] = sum(
        line["quantidade_pendente"] for line in shipment["linhas"]
    )
    return shipment


def get_shipment(shipment_id: int, store_id: int | None = None) -> dict | None:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if store_id is not None:
            cursor.execute(
                "SELECT id FROM compras_pedidos_envios WHERE id = %s AND store_id = %s",
                (shipment_id, store_id),
            )
            if not cursor.fetchone():
                return None
        shipment = _fetch_shipment(cursor, shipment_id)
    return shipment


def _list_shipments(where_sql, params):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            f"""
            SELECT e.id
            FROM compras_pedidos_envios e
            WHERE {where_sql}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT 200
            """,
            params,
        )
        ids = [int(row["id"]) for row in cursor.fetchall()]
        return [_fetch_shipment(cursor, shipment_id) for shipment_id in ids]


def list_shipments_for_store(store_id: int) -> list[dict]:
    return _list_shipments("e.store_id = %s", (int(store_id),))


def list_shipments_for_order(order_type: str, order_id: int) -> list[dict]:
    if order_type not in ("semanal", "urgente"):
        raise ValueError("Tipo de pedido inválido.")
    return _list_shipments(
        "e.tipo_pedido = %s AND e.pedido_id = %s",
        (order_type, int(order_id)),
    )


def create_receipt(
    shipment_id: int,
    store_id: int,
    lines: list[dict],
    actor: str,
    request_key: str,
    observations: str = "",
) -> dict:
    """Append one store receipt confirmation, including partials and issues."""
    try:
        shipment_id = int(shipment_id)
        store_id = int(store_id)
    except (TypeError, ValueError):
        raise ValueError("Envio ou loja inválidos.")
    request_uuid = _uuid_value(request_key, "Chave da receção inválida.")
    observations = str(observations or "").strip()[:4000]

    requested = {}
    for raw in lines or []:
        try:
            line_id = int(raw.get("envio_linha_id"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Uma linha do envio é inválida.")
        if line_id <= 0 or line_id in requested:
            raise ValueError("A mesma linha não pode aparecer duas vezes.")
        requested[line_id] = {
            "quantidade": _decimal_quantity(
                raw.get("quantidade_recebida"), allow_zero=True
            ),
            "discrepancia": str(raw.get("discrepancia") or "").strip()[:1000],
        }
    if not requested:
        raise ValueError("Indique as quantidades recebidas.")
    if not observations and all(
        item["quantidade"] == 0 and not item["discrepancia"]
        for item in requested.values()
    ):
        raise ValueError("Indique uma quantidade recebida ou descreva a diferença.")

    payload_hash = _payload_hash({
        "envio_id": shipment_id,
        "store_id": store_id,
        "observacoes": observations,
        "linhas": [
            {
                "envio_linha_id": line_id,
                "quantidade_recebida": str(values["quantidade"]),
                "discrepancia": values["discrepancia"],
            }
            for line_id, values in sorted(requested.items())
        ],
    })

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
            (str(request_uuid),),
        )
        existing_id = _already_processed(
            cursor, request_uuid, payload_hash, "compras_pedidos_rececoes"
        )
        if existing_id is not None:
            conn.commit()
            return {"id": existing_id, "duplicate": True}

        cursor.execute(
            """
            SELECT id, store_id
            FROM compras_pedidos_envios
            WHERE id = %s
            FOR UPDATE
            """,
            (shipment_id,),
        )
        shipment = cursor.fetchone()
        if not shipment or int(shipment["store_id"]) != store_id:
            raise ValueError("O envio não pertence a esta loja.")

        cursor.execute(
            """
            SELECT id, quantidade_enviada
            FROM compras_pedidos_envios_linhas
            WHERE envio_id = %s
            FOR UPDATE
            """,
            (shipment_id,),
        )
        shipment_lines = {
            int(row["id"]): dict(row) for row in cursor.fetchall()
        }
        if any(line_id not in shipment_lines for line_id in requested):
            raise ValueError("Uma das linhas não pertence a este envio.")

        cursor.execute(
            """
            SELECT envio_linha_id, COALESCE(SUM(quantidade_recebida), 0) AS total
            FROM compras_pedidos_rececoes_linhas
            WHERE envio_linha_id = ANY(%s)
            GROUP BY envio_linha_id
            """,
            (list(shipment_lines),),
        )
        received_so_far = {
            int(row["envio_linha_id"]): Decimal(str(row["total"] or "0"))
            for row in cursor.fetchall()
        }

        for line_id, values in requested.items():
            sent = Decimal(str(shipment_lines[line_id]["quantidade_enviada"]))
            already_received = received_so_far.get(line_id, Decimal("0"))
            if already_received + values["quantidade"] > sent:
                raise ValueError(
                    "A quantidade recebida não pode ultrapassar a enviada."
                )

        remaining_after = Decimal("0")
        for line_id, line in shipment_lines.items():
            sent = Decimal(str(line["quantidade_enviada"]))
            previous = received_so_far.get(line_id, Decimal("0"))
            added = requested.get(line_id, {}).get("quantidade", Decimal("0"))
            remaining_after += max(Decimal("0"), sent - previous - added)
        has_issue = any(
            values["discrepancia"] for values in requested.values()
        )
        status = (
            "problema" if has_issue
            else "parcial" if remaining_after > 0
            else "confirmada"
        )
        cursor.execute(
            """
            INSERT INTO compras_pedidos_rececoes
                (envio_id, store_id, request_key, payload_hash, status,
                 observacoes, received_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                shipment_id, store_id, str(request_uuid), payload_hash, status,
                observations, str(actor or "sistema")[:100],
            ),
        )
        receipt_id = int(cursor.fetchone()["id"])
        for line_id, values in requested.items():
            cursor.execute(
                """
                INSERT INTO compras_pedidos_rececoes_linhas
                    (rececao_id, envio_linha_id, quantidade_recebida, discrepancia)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    receipt_id, line_id, values["quantidade"],
                    values["discrepancia"],
                ),
            )
        conn.commit()
    return {"id": receipt_id, "status": status, "duplicate": False}