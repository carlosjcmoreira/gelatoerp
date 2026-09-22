"""Operational read models for the Compras abastecimento workspace.

This module only reads the three audited planning flows.  It deliberately
does not create transfer orders, receipts, stock movements, or purchase
orders.
"""

from __future__ import annotations

from datetime import date, datetime

from psycopg2.extras import RealDictCursor

from db.connection import db_connection
from db.encomendas_semanais import weekly_cycle_dates
from db.pedidos_urgentes import URGENT_STATUSES, URGENT_REASONS


WEEKLY_OPERATIONAL_STATUSES = ("submetida", "em_preparacao", "concluida")
ORIGIN_TYPES = (
    "fornecedor_externo",
    "centro_interno",
    "categoria_operacional",
    "por_resolver",
)


def _as_date(value, message):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        raise ValueError(message)


def _line_predicates(
    alias: str,
    product_query: str | None = None,
    origin_type: str | None = None,
    supplier_id: int | None = None,
):
    clauses = []
    params = []
    if product_query:
        clauses.append(
            f"({alias}.produto_snapshot ILIKE %s OR {alias}.artigo_id::text = %s)"
        )
        params.extend([f"%{product_query.strip()}%", product_query.strip()])
    if origin_type:
        clauses.append(f"{alias}.origem_tipo_snapshot = %s")
        params.append(origin_type)
    if supplier_id is not None:
        clauses.append(f"{alias}.origem_supplier_id_snapshot = %s")
        params.append(int(supplier_id))
    return clauses, params


def _validate_origin_type(origin_type):
    if origin_type and origin_type not in ORIGIN_TYPES:
        raise ValueError("Tipo de origem inválido.")


def _validate_supplier(supplier_id):
    if supplier_id in (None, ""):
        return None
    try:
        return int(supplier_id)
    except (TypeError, ValueError):
        raise ValueError("Fornecedor inválido.")


def list_weekly_orders(
    planning_sunday,
    store_id: int | None = None,
    statuses: list[str] | None = None,
    product_query: str | None = None,
    origin_type: str | None = None,
    supplier_id: int | None = None,
) -> list[dict]:
    cycle = weekly_cycle_dates(planning_sunday)
    statuses = list(statuses or WEEKLY_OPERATIONAL_STATUSES)
    invalid = set(statuses) - {
        "rascunho", "submetida", "em_preparacao", "concluida", "cancelada",
    }
    if invalid:
        raise ValueError("Estado da encomenda inválido.")
    _validate_origin_type(origin_type)
    supplier_id = _validate_supplier(supplier_id)
    line_clauses, line_params = _line_predicates(
        "lf", product_query, origin_type, supplier_id
    )
    line_join = "l.encomenda_id = o.id"
    if line_clauses:
        line_join += " AND " + " AND ".join(
            clause.replace("lf.", "l.") for clause in line_clauses
        )
    clauses = ["o.ciclo_domingo = %s", "o.status = ANY(%s)"]
    params = [cycle["ciclo_domingo"], statuses]
    if store_id is not None:
        clauses.append("o.store_id = %s")
        params.append(int(store_id))
    if line_clauses:
        clauses.append(
            "EXISTS (SELECT 1 FROM compras_encomendas_semanais_linhas lf "
            "WHERE lf.encomenda_id = o.id AND "
            + " AND ".join(line_clauses)
            + ")"
        )
        params.extend(line_params)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            f"""
            SELECT o.*, s.name AS loja_nome,
                   COALESCE(SUM(l.quantidade), 0) AS quantidade_total
            FROM compras_encomendas_semanais o
            JOIN stores s ON s.id = o.store_id
            LEFT JOIN compras_encomendas_semanais_linhas l ON {line_join}
            WHERE {' AND '.join(clauses)}
            GROUP BY o.id, s.name
            ORDER BY s.name, o.id
            """,
            line_params + params,
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["quantidade_total"] = float(item["quantidade_total"] or 0)
            rows.append(item)
        return rows


def get_weekly_consolidation(
    planning_sunday,
    store_id: int | None = None,
    statuses: list[str] | None = None,
    product_query: str | None = None,
    origin_type: str | None = None,
    supplier_id: int | None = None,
) -> list[dict]:
    cycle = weekly_cycle_dates(planning_sunday)
    statuses = list(statuses or WEEKLY_OPERATIONAL_STATUSES)
    invalid = set(statuses) - {
        "rascunho", "submetida", "em_preparacao", "concluida", "cancelada",
    }
    if invalid:
        raise ValueError("Estado da encomenda inválido.")
    _validate_origin_type(origin_type)
    supplier_id = _validate_supplier(supplier_id)
    line_clauses, line_params = _line_predicates(
        "l", product_query, origin_type, supplier_id
    )
    clauses = ["o.ciclo_domingo = %s", "o.status = ANY(%s)"]
    params = [cycle["ciclo_domingo"], statuses]
    if store_id is not None:
        clauses.append("o.store_id = %s")
        params.append(int(store_id))
    if line_clauses:
        clauses.extend(line_clauses)
        params.extend(line_params)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            f"""
            SELECT l.artigo_id, l.produto_snapshot, l.unidade_snapshot,
                   l.origem_id_snapshot, l.origem_tipo_snapshot,
                   l.origem_nome_snapshot, l.origem_supplier_id_snapshot,
                   l.origem_supplier_nome_snapshot,
                   SUM(l.quantidade) AS quantidade_total,
                   COUNT(DISTINCT o.store_id) AS lojas_count,
                   json_agg(json_build_object(
                       'store_id', o.store_id,
                       'loja_nome', s.name,
                       'quantidade', l.quantidade,
                       'unidade', l.unidade_snapshot,
                       'order_id', o.id
                   ) ORDER BY s.name) AS lojas
            FROM compras_encomendas_semanais_linhas l
            JOIN compras_encomendas_semanais o ON o.id = l.encomenda_id
            JOIN stores s ON s.id = o.store_id
            WHERE {' AND '.join(clauses)}
            GROUP BY l.artigo_id, l.produto_snapshot, l.unidade_snapshot,
                     l.origem_id_snapshot, l.origem_tipo_snapshot,
                     l.origem_nome_snapshot, l.origem_supplier_id_snapshot,
                     l.origem_supplier_nome_snapshot
            ORDER BY l.origem_nome_snapshot, l.produto_snapshot
            """,
            params,
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["quantidade_total"] = float(item["quantidade_total"] or 0)
            rows.append(item)
        return rows


def list_urgent_orders(
    store_id: int | None = None,
    statuses: list[str] | None = None,
    date_from=None,
    date_to=None,
    product_query: str | None = None,
    origin_type: str | None = None,
    supplier_id: int | None = None,
) -> list[dict]:
    statuses = list(statuses or URGENT_STATUSES)
    if set(statuses) - set(URGENT_STATUSES):
        raise ValueError("Estado do pedido urgente inválido.")
    _validate_origin_type(origin_type)
    supplier_id = _validate_supplier(supplier_id)
    line_clauses, line_params = _line_predicates(
        "lf", product_query, origin_type, supplier_id
    )
    clauses = ["o.status = ANY(%s)"]
    params = [statuses]
    if store_id is not None:
        clauses.append("o.store_id = %s")
        params.append(int(store_id))
    if date_from not in (None, ""):
        clauses.append("o.data_pretendida >= %s")
        params.append(_as_date(date_from, "A data inicial é inválida."))
    if date_to not in (None, ""):
        clauses.append("o.data_pretendida <= %s")
        params.append(_as_date(date_to, "A data final é inválida."))
    if line_clauses:
        clauses.append(
            "EXISTS (SELECT 1 FROM compras_pedidos_urgentes_linhas lf "
            "WHERE lf.pedido_id = o.id AND "
            + " AND ".join(line_clauses)
            + ")"
        )
        params.extend(line_params)
    join = "l.pedido_id = o.id"
    if line_clauses:
        join += " AND " + " AND ".join(
            clause.replace("lf.", "l.") for clause in line_clauses
        )
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            f"""
            SELECT o.*, s.name AS loja_nome,
                   COALESCE(SUM(l.quantidade), 0) AS quantidade_total,
                   json_agg(json_build_object(
                       'artigo_id', l.artigo_id,
                       'produto', l.produto_snapshot,
                       'unidade', l.unidade_snapshot,
                       'quantidade', l.quantidade,
                       'origem_id', l.origem_id_snapshot,
                       'origem_tipo', l.origem_tipo_snapshot,
                       'origem_nome', l.origem_nome_snapshot,
                       'supplier_id', l.origem_supplier_id_snapshot,
                       'supplier_nome', l.origem_supplier_nome_snapshot,
                       'observacoes', l.observacoes
                   ) ORDER BY l.produto_snapshot)
                   FILTER (WHERE l.id IS NOT NULL) AS linhas
            FROM compras_pedidos_urgentes o
            JOIN stores s ON s.id = o.store_id
            LEFT JOIN compras_pedidos_urgentes_linhas l ON {join}
            WHERE {' AND '.join(clauses)}
            GROUP BY o.id, s.name
            ORDER BY o.prioridade, o.data_pretendida, o.created_at, o.id
            """,
            line_params + params,
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["quantidade_total"] = float(item["quantidade_total"] or 0)
            item["linhas"] = item.get("linhas") or []
            rows.append(item)
        return rows


def get_urgent_metrics(
    store_id: int | None = None,
    statuses: list[str] | None = None,
    date_from=None,
    date_to=None,
    product_query: str | None = None,
    origin_type: str | None = None,
    supplier_id: int | None = None,
) -> dict:
    orders = list_urgent_orders(
        store_id=store_id,
        statuses=statuses,
        date_from=date_from,
        date_to=date_to,
        product_query=product_query,
        origin_type=origin_type,
        supplier_id=supplier_id,
    )
    by_reason = {}
    by_store = {}
    by_article = {}
    volume_total = 0.0
    for order in orders:
        by_reason[order["motivo"]] = by_reason.get(order["motivo"], 0) + 1
        by_store[order["loja_nome"]] = by_store.get(order["loja_nome"], 0) + 1
        for line in order.get("linhas") or []:
            volume_total += float(line.get("quantidade") or 0)
            key = (line.get("artigo_id"), line.get("produto"))
            entry = by_article.setdefault(
                key,
                {"artigo_id": key[0], "produto_snapshot": key[1], "total": 0, "quantidade_total": 0.0},
            )
            entry["total"] += 1
            entry["quantidade_total"] += float(line.get("quantidade") or 0)
    return {
        "volume_total": volume_total,
        "orders_total": len(orders),
        "by_reason": [
            {"motivo": key, "total": value}
            for key, value in sorted(by_reason.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_store": [
            {"loja_nome": key, "total": value}
            for key, value in sorted(by_store.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_article": sorted(
            by_article.values(),
            key=lambda item: (-item["total"], item["produto_snapshot"] or ""),
        ),
    }