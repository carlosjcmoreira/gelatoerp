"""Urgent store-purchase requests.

Urgent requests are exception documents, deliberately separate from weekly
planning and from stock-transfer movements.  They keep an immutable snapshot
of the requested article/origin and use a deterministic fingerprint so a
double-click or concurrent retry cannot create a second request.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from db.connection import db_connection
from db.encomendas_semanais import normalise_planning_sunday


LISBON = ZoneInfo("Europe/Lisbon")
URGENT_STATUSES = ("submetida", "em_preparacao", "concluida", "cancelada")
URGENT_REASONS = (
    "falta_planeamento",
    "procura_acima_previsto",
    "outro",
)
URGENT_REASON_LABELS = {
    "falta_planeamento": "Falta de planeamento",
    "procura_acima_previsto": "Procura acima do previsto",
    "outro": "Outro",
}
_ALLOWED_TRANSITIONS = {
    "submetida": {"em_preparacao", "cancelada"},
    "em_preparacao": {"concluida", "cancelada"},
    "concluida": set(),
    "cancelada": set(),
}


def lisboa_today() -> date:
    return datetime.now(LISBON).date()


def _as_date(value, message: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        raise ValueError(message)


def normalise_target_date(value, today: date | None = None) -> date:
    target = _as_date(value, "Indique uma data pretendida válida.")
    if target < (today or lisboa_today()):
        raise ValueError("A data pretendida não pode estar no passado.")
    return target


def normalise_reason(reason: str, reason_detail: str = "") -> tuple[str, str]:
    reason = str(reason or "").strip()
    detail = str(reason_detail or "").strip()[:1000]
    if reason not in URGENT_REASONS:
        raise ValueError("Escolha um motivo válido para o pedido urgente.")
    if reason == "outro" and not detail:
        raise ValueError("Explique o motivo do pedido urgente.")
    if reason != "outro":
        detail = ""
    return reason, detail


def _decimal_quantity(value) -> Decimal:
    try:
        quantity = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError):
        raise ValueError("A quantidade tem de ser um número válido.")
    if not quantity.is_finite() or quantity <= 0 or quantity > Decimal("1000000"):
        raise ValueError("A quantidade tem de estar entre 0 e 1.000.000.")
    return quantity.quantize(Decimal("0.001"))


def _row_to_order(row: dict, lines: list[dict] | None = None) -> dict:
    order = dict(row)
    for key in ("quantidade_total",):
        if key in order and order[key] is not None:
            order[key] = float(order[key])
    if lines is not None:
        order["linhas"] = lines
    return order


def _row_to_line(row) -> dict:
    line = dict(row)
    if line.get("quantidade") is not None:
        line["quantidade"] = float(line["quantidade"])
    return line


def _fetch_order(cursor, order_id: int, for_update: bool = False) -> dict | None:
    cursor.execute(
        """
        SELECT o.*, s.name AS loja_nome
        FROM compras_pedidos_urgentes o
        JOIN stores s ON s.id = o.store_id
        WHERE o.id = %s
        """ + (" FOR UPDATE" if for_update else ""),
        (order_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _fetch_lines(cursor, order_id: int) -> list[dict]:
    cursor.execute(
        """
        SELECT l.*, a.ativo AS artigo_ativo_atual,
               s.name AS supplier_nome_atual
        FROM compras_pedidos_urgentes_linhas l
        LEFT JOIN artigos_administrativos a ON a.id = l.artigo_id
        LEFT JOIN suppliers s ON s.id = l.origem_supplier_id_snapshot
        WHERE l.pedido_id = %s
        ORDER BY l.produto_snapshot, l.id
        """,
        (order_id,),
    )
    return [_row_to_line(row) for row in cursor.fetchall()]


def _snapshot(order: dict, lines: list[dict]) -> dict:
    return {
        "pedido_id": order["id"],
        "store_id": order["store_id"],
        "loja_nome": order.get("loja_nome"),
        "data_pretendida": order["data_pretendida"].isoformat(),
        "ciclo_domingo": (
            order["ciclo_domingo"].isoformat()
            if order.get("ciclo_domingo") else None
        ),
        "motivo": order["motivo"],
        "motivo_detalhe": order.get("motivo_detalhe") or "",
        "observacoes": order.get("observacoes") or "",
        "linhas": [
            {
                "artigo_id": line.get("artigo_id"),
                "produto": line["produto_snapshot"],
                "unidade": line["unidade_snapshot"],
                "quantidade": line["quantidade"],
                "origem_id": line.get("origem_id_snapshot"),
                "origem_tipo": line.get("origem_tipo_snapshot"),
                "origem_nome": line.get("origem_nome_snapshot"),
                "origem_store_id": line.get("origem_store_id_snapshot"),
                "supplier_id": line.get("origem_supplier_id_snapshot"),
                "supplier_nome": line.get("origem_supplier_nome_snapshot"),
                "observacoes": line.get("observacoes") or "",
            }
            for line in lines
        ],
    }


def _audit(
    cursor,
    order_id: int,
    event_type: str,
    actor: str | None,
    reason: str | None = None,
    payload: dict | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
):
    cursor.execute(
        """
        INSERT INTO compras_pedidos_urgentes_audit
            (pedido_id, event_type, actor, reason, payload,
             previous_status, new_status)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
        """,
        (
            order_id,
            event_type,
            actor or "sistema",
            reason,
            json.dumps(payload or {}, ensure_ascii=False, default=str),
            previous_status,
            new_status,
        ),
    )


def _article_catalog_rows(cursor, article_ids: list[int]) -> dict[int, dict]:
    if not article_ids:
        return {}
    cursor.execute(
        """
        SELECT a.id, a.produto, a.unidade, a.ativo,
               o.id AS catalog_origin_id, o.chave AS catalog_origin_key,
               o.tipo AS catalog_origin_type, o.nome AS catalog_origin_name,
               o.supplier_id, s.name AS supplier_nome, o.ativo AS origin_active,
               CASE WHEN o.chave = 'categoria:moedas' THEN hub.id ELSE o.id END
                   AS source_origin_id,
               CASE WHEN o.chave = 'categoria:moedas'
                    THEN 'centro_interno' ELSE o.tipo END AS source_origin_type,
               CASE WHEN o.chave = 'categoria:moedas'
                    THEN hub.nome ELSE o.nome END AS source_origin_name,
               CASE WHEN o.chave = 'categoria:moedas'
                    THEN hub.store_id ELSE o.store_id END AS source_origin_store_id,
               CASE WHEN o.chave = 'categoria:moedas'
                    THEN NULL ELSE o.supplier_id END AS source_supplier_id,
               CASE WHEN o.chave = 'categoria:moedas'
                    THEN NULL ELSE s.name END AS source_supplier_name
        FROM artigos_administrativos a
        JOIN compras_origens o ON o.id = a.origem_id
        LEFT JOIN suppliers s ON s.id = o.supplier_id
        LEFT JOIN compras_origens hub
          ON hub.chave = 'centro:matosinhos'
         AND hub.tipo = 'centro_interno'
         AND hub.ativo = TRUE
        WHERE a.id = ANY(%s)
          AND (
              (o.tipo = 'fornecedor_externo' AND o.supplier_id IS NOT NULL)
              OR (o.chave = 'categoria:moedas' AND hub.id IS NOT NULL)
          )
        """,
        (article_ids,),
    )
    return {int(row["id"]): dict(row) for row in cursor.fetchall()}


def get_available_urgent_articles() -> list[dict]:
    """Active external articles plus the typed Moedas → Matosinhos route."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT a.id, a.produto, a.unidade, a.ativo,
                   CASE WHEN o.chave = 'categoria:moedas' THEN hub.id ELSE o.id END
                       AS origem_id,
                   CASE WHEN o.chave = 'categoria:moedas'
                        THEN 'centro_interno' ELSE o.tipo END AS origem_tipo,
                   CASE WHEN o.chave = 'categoria:moedas'
                        THEN hub.nome ELSE o.nome END AS origem_nome,
                   CASE WHEN o.chave = 'categoria:moedas'
                        THEN hub.store_id ELSE o.store_id END AS origem_store_id,
                   CASE WHEN o.chave = 'categoria:moedas'
                        THEN NULL ELSE o.supplier_id END AS supplier_id,
                   CASE WHEN o.chave = 'categoria:moedas'
                        THEN NULL ELSE s.name END AS supplier_nome,
                   o.chave AS origem_catalogo_chave
            FROM artigos_administrativos a
            JOIN compras_origens o ON o.id = a.origem_id
            LEFT JOIN suppliers s ON s.id = o.supplier_id
            LEFT JOIN compras_origens hub
              ON hub.chave = 'centro:matosinhos'
             AND hub.tipo = 'centro_interno'
             AND hub.ativo = TRUE
            WHERE a.ativo = TRUE
              AND o.ativo = TRUE
              AND (
                  (o.tipo = 'fornecedor_externo' AND o.supplier_id IS NOT NULL)
                  OR (o.chave = 'categoria:moedas' AND hub.id IS NOT NULL)
              )
            ORDER BY CASE WHEN o.chave = 'categoria:moedas' THEN 0 ELSE 1 END,
                     origem_nome, a.produto, a.id
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def _normalise_lines(cursor, lines: list[dict]) -> tuple[dict[int, dict], dict[int, dict]]:
    requested: dict[int, dict] = {}
    for raw in lines or []:
        try:
            article_id = int(raw.get("artigo_id"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("O artigo selecionado é inválido.")
        if article_id in requested:
            raise ValueError("O mesmo artigo não pode aparecer duas vezes.")
        requested[article_id] = {
            "quantidade": _decimal_quantity(raw.get("quantidade")),
            "observacoes": str(raw.get("observacoes") or "").strip()[:1000],
        }
    if not requested:
        raise ValueError("Adicione pelo menos um artigo ao pedido urgente.")
    catalogue = _article_catalog_rows(cursor, list(requested))
    if len(catalogue) != len(requested):
        raise ValueError("Um dos artigos selecionados não está disponível.")
    for item in catalogue.values():
        if not item["ativo"] or not item["origin_active"]:
            raise ValueError(f'O artigo "{item["produto"]}" deixou de estar disponível.')
    return requested, catalogue


def _priority(target: date, today: date) -> int:
    return 1 if target <= today + timedelta(days=1) else 2


def create_urgent_order(
    store_id: int,
    target_date,
    lines: list[dict],
    actor: str,
    reason: str,
    observations: str,
    reason_detail: str = "",
    weekly_order_id: int | None = None,
    weekly_cycle=None,
    today: date | None = None,
) -> dict:
    """Create one submitted urgent request, safely deduplicated by fingerprint."""
    target = normalise_target_date(target_date, today=today)
    reason, reason_detail = normalise_reason(reason, reason_detail)
    observations = str(observations or "").strip()[:4000]
    if not observations:
        raise ValueError("Indique uma observação para justificar o pedido urgente.")
    current_day = today or lisboa_today()

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        requested, catalogue = _normalise_lines(cursor, lines)
        weekly_cycle_value = None
        if weekly_order_id is not None:
            try:
                weekly_order_id = int(weekly_order_id)
            except (TypeError, ValueError):
                raise ValueError("A referência da encomenda semanal é inválida.")
            cursor.execute(
                """
                SELECT id, store_id, ciclo_domingo
                FROM compras_encomendas_semanais
                WHERE id = %s
                """,
                (weekly_order_id,),
            )
            weekly = cursor.fetchone()
            if not weekly or int(weekly["store_id"]) != int(store_id):
                raise ValueError("A encomenda semanal relacionada não pertence a esta loja.")
            weekly_cycle_value = weekly["ciclo_domingo"]
        elif weekly_cycle not in (None, ""):
            weekly_cycle_value = normalise_planning_sunday(weekly_cycle)

        fingerprint_lines = [
            {
                "artigo_id": article_id,
                "quantidade": str(values["quantidade"]),
                "observacoes": values["observacoes"],
            }
            for article_id, values in sorted(requested.items())
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "store_id": int(store_id),
                    "target_date": target.isoformat(),
                    "weekly_order_id": weekly_order_id,
                    "weekly_cycle": (
                        weekly_cycle_value.isoformat()
                        if weekly_cycle_value else None
                    ),
                    "reason": reason,
                    "reason_detail": reason_detail,
                    "observations": observations,
                    "lines": fingerprint_lines,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cursor.execute(
            """
            INSERT INTO compras_pedidos_urgentes
                (store_id, data_pretendida, encomenda_semanal_id, ciclo_domingo,
                 motivo, motivo_detalhe, observacoes, prioridade, dedupe_key,
                 submitted_by, submitted_at, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP,
                    %s, CURRENT_TIMESTAMP)
            ON CONFLICT (store_id, dedupe_key) DO NOTHING
            RETURNING id
            """,
            (
                store_id,
                target,
                weekly_order_id,
                weekly_cycle_value,
                reason,
                reason_detail,
                observations,
                _priority(target, current_day),
                fingerprint,
                actor,
                actor,
            ),
        )
        inserted = cursor.fetchone()
        if inserted:
            order_id = int(inserted["id"])
            for article_id, values in requested.items():
                item = catalogue[article_id]
                cursor.execute(
                    """
                    INSERT INTO compras_pedidos_urgentes_linhas
                        (pedido_id, artigo_id, produto_snapshot, unidade_snapshot,
                         origem_id_snapshot, origem_tipo_snapshot,
                         origem_nome_snapshot, origem_store_id_snapshot,
                         origem_supplier_id_snapshot, origem_supplier_nome_snapshot,
                         quantidade, observacoes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        order_id,
                        article_id,
                        item["produto"],
                        item["unidade"] or "unidade",
                        item["source_origin_id"],
                        item["source_origin_type"],
                        item["source_origin_name"],
                        item["source_origin_store_id"],
                        item["source_supplier_id"],
                        item["source_supplier_name"],
                        values["quantidade"],
                        values["observacoes"],
                    ),
                )
            order = _fetch_order(cursor, order_id)
            lines_now = _fetch_lines(cursor, order_id)
            _audit(
                cursor,
                order_id,
                "submetida",
                actor,
                payload=_snapshot(order, lines_now),
                new_status="submetida",
            )
        else:
            cursor.execute(
                """
                SELECT id FROM compras_pedidos_urgentes
                WHERE store_id = %s AND dedupe_key = %s
                FOR UPDATE
                """,
                (store_id, fingerprint),
            )
            existing = cursor.fetchone()
            if not existing:
                raise ValueError("Não foi possível registar o pedido urgente.")
            order_id = int(existing["id"])
            order = _fetch_order(cursor, order_id)
            lines_now = _fetch_lines(cursor, order_id)
        conn.commit()
    return _row_to_order(order, lines_now)


def get_urgent_order(order_id: int, include_lines: bool = True) -> dict | None:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        order = _fetch_order(cursor, order_id)
        lines = _fetch_lines(cursor, order_id) if order and include_lines else None
    return _row_to_order(order, lines) if order else None


def list_urgent_orders(
    store_id: int | None = None,
    statuses: list[str] | None = None,
    target_date=None,
    reason: str | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if store_id is not None:
        clauses.append("o.store_id = %s")
        params.append(store_id)
    if statuses:
        invalid = set(statuses) - set(URGENT_STATUSES)
        if invalid:
            raise ValueError("Estado do pedido urgente inválido.")
        clauses.append("o.status = ANY(%s)")
        params.append(list(statuses))
    if target_date not in (None, ""):
        clauses.append("o.data_pretendida = %s")
        params.append(_as_date(target_date, "A data pretendida é inválida."))
    if reason:
        if reason not in URGENT_REASONS:
            raise ValueError("Motivo do pedido urgente inválido.")
        clauses.append("o.motivo = %s")
        params.append(reason)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            f"""
            SELECT o.*, s.name AS loja_nome,
                   COALESCE(SUM(l.quantidade), 0) AS quantidade_total,
                   STRING_AGG(DISTINCT l.produto_snapshot, ', '
                              ORDER BY l.produto_snapshot) AS artigos_resumo,
                   STRING_AGG(DISTINCT l.origem_nome_snapshot, ', '
                              ORDER BY l.origem_nome_snapshot) AS origens_resumo
            FROM compras_pedidos_urgentes o
            JOIN stores s ON s.id = o.store_id
            LEFT JOIN compras_pedidos_urgentes_linhas l ON l.pedido_id = o.id
            {where}
            GROUP BY o.id, s.name
            ORDER BY o.prioridade, o.data_pretendida, o.created_at, o.id
            """,
            params,
        )
        return [_row_to_order(dict(row)) for row in cursor.fetchall()]


def transition_urgent_order_status(
    order_id: int,
    new_status: str,
    actor: str,
    reason: str | None = None,
) -> dict:
    if new_status not in URGENT_STATUSES:
        raise ValueError("Estado do pedido urgente inválido.")
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        order = _fetch_order(cursor, order_id, for_update=True)
        if not order:
            raise ValueError("Pedido urgente não encontrado.")
        old_status = order["status"]
        if new_status == old_status:
            return _row_to_order(order, _fetch_lines(cursor, order_id))
        if new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
            raise ValueError(f"Não é possível passar de {old_status} para {new_status}.")
        if new_status == "cancelada" and not str(reason or "").strip():
            raise ValueError("Indique o motivo do cancelamento.")
        cursor.execute(
            """
            UPDATE compras_pedidos_urgentes
            SET status = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP,
                prepared_by = CASE WHEN %s = 'em_preparacao' THEN %s ELSE prepared_by END,
                prepared_at = CASE WHEN %s = 'em_preparacao' THEN CURRENT_TIMESTAMP ELSE prepared_at END,
                completed_by = CASE WHEN %s = 'concluida' THEN %s ELSE completed_by END,
                completed_at = CASE WHEN %s = 'concluida' THEN CURRENT_TIMESTAMP ELSE completed_at END,
                cancelled_by = CASE WHEN %s = 'cancelada' THEN %s ELSE cancelled_by END,
                cancelled_at = CASE WHEN %s = 'cancelada' THEN CURRENT_TIMESTAMP ELSE cancelled_at END,
                cancel_reason = CASE WHEN %s = 'cancelada' THEN %s ELSE cancel_reason END
            WHERE id = %s
            """,
            (
                new_status, actor,
                new_status, actor, new_status,
                new_status, actor, new_status,
                new_status, actor, new_status, new_status, reason,
                order_id,
            ),
        )
        _audit(
            cursor,
            order_id,
            "estado_alterado",
            actor,
            reason=reason,
            previous_status=old_status,
            new_status=new_status,
        )
        fresh = _fetch_order(cursor, order_id)
        lines = _fetch_lines(cursor, order_id)
        conn.commit()
    return _row_to_order(fresh, lines)


def get_urgent_order_audit(order_id: int) -> list[dict]:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, event_type, actor, reason, payload,
                   previous_status, new_status, created_at
            FROM compras_pedidos_urgentes_audit
            WHERE pedido_id = %s
            ORDER BY created_at, id
            """,
            (order_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_urgent_metrics() -> dict:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT motivo, COUNT(*) AS total
            FROM compras_pedidos_urgentes
            GROUP BY motivo ORDER BY total DESC, motivo
            """
        )
        by_reason = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT o.store_id, s.name AS loja_nome, COUNT(*) AS total
            FROM compras_pedidos_urgentes o
            JOIN stores s ON s.id = o.store_id
            GROUP BY o.store_id, s.name
            ORDER BY total DESC, s.name
            """
        )
        by_store = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT l.artigo_id, l.produto_snapshot, COUNT(*) AS total,
                   SUM(l.quantidade) AS quantidade_total
            FROM compras_pedidos_urgentes_linhas l
            GROUP BY l.artigo_id, l.produto_snapshot
            ORDER BY total DESC, l.produto_snapshot
            """
        )
        by_article = []
        for row in cursor.fetchall():
            item = dict(row)
            item["quantidade_total"] = float(item["quantidade_total"])
            by_article.append(item)
    return {
        "by_reason": by_reason,
        "by_store": by_store,
        "by_article": by_article,
    }