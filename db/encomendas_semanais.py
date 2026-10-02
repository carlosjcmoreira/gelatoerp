"""Weekly store-purchase orders.

This module deliberately does not use ``ordens_transferencia``.  A weekly
purchase is a request/planning document, not a stock movement.  Its live
lines are convenient for editing, while submitted versions and audit events
are immutable evidence of what was sent to Compras.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from db.artigos import ensure_artigo_encomendavel
from db.connection import db_connection


LISBON = ZoneInfo("Europe/Lisbon")
WEEKLY_STATUSES = ("rascunho", "submetida", "em_preparacao", "concluida", "cancelada")
_ALLOWED_TRANSITIONS = {
    "rascunho": {"submetida", "cancelada"},
    "submetida": {"em_preparacao", "cancelada"},
    "em_preparacao": {"concluida", "cancelada"},
    "concluida": set(),
    "cancelada": set(),
}


def lisboa_today() -> date:
    return datetime.now(LISBON).date()


def normalise_planning_sunday(value) -> date:
    """Validate and return the Sunday that identifies a weekly cycle."""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        try:
            value = date.fromisoformat(str(value or ""))
        except (TypeError, ValueError):
            raise ValueError("Indique um domingo de planeamento válido.")
    if value.weekday() != 6:
        raise ValueError("A encomenda semanal tem de ser planeada num domingo.")
    return value


def weekly_cycle_dates(planning_sunday) -> dict:
    sunday = normalise_planning_sunday(planning_sunday)
    return {
        "ciclo_domingo": sunday,
        "entrega_prevista": sunday + timedelta(days=1),
    }


def next_planning_sunday(reference: date | None = None) -> date:
    """Return the current Sunday or the next Sunday in the Lisbon calendar."""
    current = reference or lisboa_today()
    if isinstance(current, datetime):
        current = current.date()
    return current + timedelta(days=(6 - current.weekday()) % 7)


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
        FROM compras_encomendas_semanais o
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
        SELECT l.*, s.name AS supplier_nome_atual,
               a.ativo AS artigo_ativo_atual
        FROM compras_encomendas_semanais_linhas l
        LEFT JOIN suppliers s ON s.id = l.origem_supplier_id_snapshot
        LEFT JOIN artigos_administrativos a ON a.id = l.artigo_id
        WHERE l.encomenda_id = %s
        ORDER BY l.produto_snapshot, l.id
        """,
        (order_id,),
    )
    return [_row_to_line(row) for row in cursor.fetchall()]


def _snapshot(order: dict, lines: list[dict]) -> dict:
    return {
        "encomenda_id": order["id"],
        "store_id": order["store_id"],
        "loja_nome": order.get("loja_nome"),
        "ciclo_domingo": order["ciclo_domingo"].isoformat(),
        "entrega_prevista": order["entrega_prevista"].isoformat(),
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
                "supplier_id": line.get("origem_supplier_id_snapshot"),
                "supplier_nome": line.get("supplier_nome_snapshot"),
                "fornecedor_oficial_id": line.get(
                    "fornecedor_oficial_id_snapshot"
                ),
                "fornecedor_oficial_nome": line.get(
                    "fornecedor_oficial_nome_snapshot"
                ),
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
        INSERT INTO compras_encomendas_semanais_audit
            (encomenda_id, event_type, actor, reason, payload,
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
               a.encomendavel, a.fornecedor_oficial_id,
               official_s.name AS fornecedor_oficial_nome
        FROM artigos_administrativos a
        LEFT JOIN suppliers official_s ON official_s.id = a.fornecedor_oficial_id
        WHERE a.id = ANY(%s)
        ORDER BY a.id
        FOR UPDATE OF a
        """,
        (article_ids,),
    )
    return {int(row["id"]): dict(row) for row in cursor.fetchall()}


def get_available_weekly_articles() -> list[dict]:
    """Return every active article; unresolved origins remain requestable."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT a.id, a.produto, a.unidade, a.fornecedor,
                   a.categoria_artigo,
                   a.encomendavel, a.fornecedor_oficial_id,
                   official_s.name AS fornecedor_oficial_nome
            FROM artigos_administrativos a
            LEFT JOIN suppliers official_s ON official_s.id = a.fornecedor_oficial_id
            WHERE a.ativo = TRUE AND a.encomendavel = TRUE
             ORDER BY a.categoria_artigo, a.produto, a.id
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def get_or_create_weekly_order(store_id: int, planning_sunday, actor: str | None = None) -> dict:
    cycle = weekly_cycle_dates(planning_sunday)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO compras_encomendas_semanais
                (store_id, ciclo_domingo, entrega_prevista, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (store_id, ciclo_domingo) DO NOTHING
            """,
            (store_id, cycle["ciclo_domingo"], cycle["entrega_prevista"], actor, actor),
        )
        cursor.execute(
            """
            SELECT o.*, s.name AS loja_nome
            FROM compras_encomendas_semanais o
            JOIN stores s ON s.id = o.store_id
            WHERE o.store_id = %s AND o.ciclo_domingo = %s
            FOR UPDATE
            """,
            (store_id, cycle["ciclo_domingo"]),
        )
        row = cursor.fetchone()
        lines = _fetch_lines(cursor, row["id"]) if row else []
        conn.commit()
    if not row:
        raise ValueError("Não foi possível criar a encomenda semanal.")
    return _row_to_order(dict(row), lines)


def get_weekly_order(order_id: int, include_lines: bool = True) -> dict | None:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        order = _fetch_order(cursor, order_id)
        lines = _fetch_lines(cursor, order_id) if order and include_lines else None
    return _row_to_order(order, lines) if order else None


def get_weekly_order_for_store(store_id: int, planning_sunday, include_lines: bool = True) -> dict | None:
    cycle = weekly_cycle_dates(planning_sunday)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT o.*, s.name AS loja_nome
            FROM compras_encomendas_semanais o
            JOIN stores s ON s.id = o.store_id
            WHERE o.store_id = %s AND o.ciclo_domingo = %s
            """,
            (store_id, cycle["ciclo_domingo"]),
        )
        order = cursor.fetchone()
        lines = _fetch_lines(cursor, order["id"]) if order and include_lines else None
    return _row_to_order(dict(order), lines) if order else None


def save_weekly_draft(
    order_id: int,
    lines: list[dict],
    actor: str,
    observacoes: str = "",
) -> dict:
    """Replace draft lines atomically, with one canonical row per article."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        order = _fetch_order(cursor, order_id, for_update=True)
        if not order:
            raise ValueError("Encomenda semanal não encontrada.")
        if order["status"] != "rascunho":
            raise ValueError("Só é possível editar uma encomenda em rascunho.")

        requested: dict[int, dict] = {}
        for raw in lines or []:
            try:
                article_id = int(raw.get("artigo_id"))
            except (TypeError, ValueError, AttributeError):
                raise ValueError("O artigo selecionado é inválido.")
            quantity = _decimal_quantity(raw.get("quantidade"))
            if article_id in requested:
                raise ValueError("O mesmo artigo não pode aparecer duas vezes.")
            requested[article_id] = {
                "quantidade": quantity,
                "observacoes": str(raw.get("observacoes") or "").strip()[:1000],
            }

        catalogue = _article_catalog_rows(cursor, list(requested))
        if len(catalogue) != len(requested):
            raise ValueError("Um dos artigos selecionados já não existe.")
        for article_id, article in catalogue.items():
            ensure_artigo_encomendavel(article)

        cursor.execute(
            "DELETE FROM compras_encomendas_semanais_linhas WHERE encomenda_id = %s",
            (order_id,),
        )
        for article_id, requested_line in requested.items():
            article = catalogue[article_id]
            cursor.execute(
                """
                INSERT INTO compras_encomendas_semanais_linhas
                    (encomenda_id, artigo_id, produto_snapshot, unidade_snapshot,
                     origem_id_snapshot, origem_tipo_snapshot, origem_nome_snapshot,
                     origem_supplier_id_snapshot, origem_supplier_nome_snapshot,
                     fornecedor_oficial_id_snapshot,
                     fornecedor_oficial_nome_snapshot, quantidade, observacoes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    article_id,
                    article["produto"],
                    article["unidade"] or "unidade",
                    None,
                    None,
                    None,
                    None,
                    None,
                    article["fornecedor_oficial_id"],
                    article["fornecedor_oficial_nome"],
                    requested_line["quantidade"],
                    requested_line["observacoes"],
                ),
            )
        observacoes = str(observacoes or "").strip()[:4000]
        cursor.execute(
            """
            UPDATE compras_encomendas_semanais
            SET observacoes = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP,
                versao_actual = versao_actual + 1
            WHERE id = %s
            """,
            (observacoes, actor, order_id),
        )
        fresh = _fetch_order(cursor, order_id)
        fresh_lines = _fetch_lines(cursor, order_id)
        _audit(cursor, order_id, "rascunho_guardado", actor, payload=_snapshot(fresh, fresh_lines))
        conn.commit()
    return _row_to_order(fresh, fresh_lines)


def submit_weekly_order(
    order_id: int,
    actor: str,
    today: date | None = None,
) -> dict:
    """Freeze version one. Repeated calls return the already submitted order."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        order = _fetch_order(cursor, order_id, for_update=True)
        if not order:
            raise ValueError("Encomenda semanal não encontrada.")
        if order["status"] != "rascunho":
            lines = _fetch_lines(cursor, order_id)
            conn.commit()
            return _row_to_order(order, lines)
        current_day = today or lisboa_today()
        if current_day != order["ciclo_domingo"]:
            raise ValueError("A submissão da encomenda semanal só pode ser feita no domingo.")
        lines = _fetch_lines(cursor, order_id)
        if not lines:
            raise ValueError("Adicione pelo menos um artigo antes de submeter.")
        article_ids = [
            int(line["artigo_id"])
            for line in lines
            if line.get("artigo_id") is not None
        ]
        catalogue = _article_catalog_rows(cursor, article_ids)
        if len(catalogue) != len(lines):
            raise ValueError(
                "Um artigo do rascunho deixou de estar disponível para encomenda."
            )
        for item in catalogue.values():
            ensure_artigo_encomendavel(item)
        payload = _snapshot(order, lines)
        cursor.execute(
            """
            INSERT INTO compras_encomendas_semanais_versoes
                (encomenda_id, versao, tipo, snapshot, actor)
            VALUES (%s, 1, 'submetida', %s::jsonb, %s)
            ON CONFLICT (encomenda_id, versao) DO NOTHING
            """,
            (order_id, json.dumps(payload, ensure_ascii=False, default=str), actor),
        )
        cursor.execute(
            """
            UPDATE compras_encomendas_semanais
            SET status = 'submetida', submitted_by = %s, submitted_at = CURRENT_TIMESTAMP,
                updated_by = %s, updated_at = CURRENT_TIMESTAMP,
                versao_submetida = 1
            WHERE id = %s AND status = 'rascunho'
            """,
            (actor, actor, order_id),
        )
        _audit(
            cursor, order_id, "submetida", actor, payload=payload,
            previous_status="rascunho", new_status="submetida",
        )
        fresh = _fetch_order(cursor, order_id)
        conn.commit()
    return _row_to_order(fresh, lines)


def transition_weekly_order_status(
    order_id: int,
    new_status: str,
    actor: str,
    reason: str | None = None,
) -> dict:
    if new_status not in WEEKLY_STATUSES:
        raise ValueError("Estado da encomenda inválido.")
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        order = _fetch_order(cursor, order_id, for_update=True)
        if not order:
            raise ValueError("Encomenda semanal não encontrada.")
        old_status = order["status"]
        if new_status == old_status:
            return _row_to_order(order, _fetch_lines(cursor, order_id))
        if new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
            raise ValueError(f"Não é possível passar de {old_status} para {new_status}.")
        if new_status == "cancelada" and not str(reason or "").strip():
            raise ValueError("Indique o motivo do cancelamento.")
        cursor.execute(
            """
            UPDATE compras_encomendas_semanais
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
            cursor, order_id, "estado_alterado", actor, reason=reason,
            previous_status=old_status, new_status=new_status,
        )
        fresh = _fetch_order(cursor, order_id)
        lines = _fetch_lines(cursor, order_id)
        conn.commit()
    return _row_to_order(fresh, lines)


def amend_weekly_order(
    order_id: int,
    lines: list[dict],
    actor: str,
    reason: str,
    observacoes: str | None = None,
) -> dict:
    """Apply a post-submission amendment while retaining every prior snapshot."""
    if not str(reason or "").strip():
        raise ValueError("Indique o motivo da alteração.")
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        order = _fetch_order(cursor, order_id, for_update=True)
        if not order or order["status"] not in ("submetida", "em_preparacao"):
            raise ValueError("Só é possível alterar uma encomenda submetida ou em preparação.")
        old_lines = _fetch_lines(cursor, order_id)
        old_payload = _snapshot(order, old_lines)
        old_by_article = {
            int(line["artigo_id"]): line
            for line in old_lines
            if line.get("artigo_id") is not None
        }
        next_version = int(order.get("versao_actual") or 1) + 1
        cursor.execute(
            """
            INSERT INTO compras_encomendas_semanais_versoes
                (encomenda_id, versao, tipo, snapshot, actor, reason)
            VALUES (%s, %s, 'alteracao', %s::jsonb, %s, %s)
            """,
            (
                order_id, next_version,
                json.dumps(old_payload, ensure_ascii=False, default=str),
                actor, reason,
            ),
        )
        # Reuse the same strict catalogue validation as a draft, but keep the
        # transaction and the immutable pre-change version together.
        requested: dict[int, dict] = {}
        for raw in lines or []:
            article_id = int(raw.get("artigo_id"))
            if article_id in requested:
                raise ValueError("O mesmo artigo não pode aparecer duas vezes.")
            requested[article_id] = {
                "quantidade": _decimal_quantity(raw.get("quantidade")),
                "observacoes": str(raw.get("observacoes") or "").strip()[:1000],
            }
        catalogue = _article_catalog_rows(cursor, list(requested))
        if len(catalogue) != len(requested):
            raise ValueError("Um dos artigos selecionados já não existe.")
        for article_id, item in catalogue.items():
            # A previously submitted line may remain editable even if the
            # catalogue entry was later deactivated.  Its original snapshot
            # is reused below; new additions still require an active article.
            if article_id not in old_by_article:
                ensure_artigo_encomendavel(item)
        for old_line in old_lines:
            article_id = old_line.get("artigo_id")
            if article_id is None:
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(sl.quantidade_enviada), 0)
                           AS quantidade_enviada
                    FROM compras_pedidos_envios e
                    JOIN compras_pedidos_envios_linhas sl ON sl.envio_id = e.id
                    WHERE e.tipo_pedido = 'semanal' AND e.pedido_id = %s
                      AND sl.artigo_id IS NULL AND sl.pedido_linha_id = %s
                    """,
                    (order_id, old_line["id"]),
                )
                already_sent = Decimal(
                    str(cursor.fetchone()["quantidade_enviada"] or "0")
                )
                if already_sent > 0:
                    raise ValueError(
                        f'A alteração de "{old_line["produto_snapshot"]}" '
                        'não é permitida porque o artigo já foi removido do catálogo '
                        'e tem quantidades enviadas.'
                    )
                continue
            article_id = int(article_id)
            cursor.execute(
                """
                SELECT COALESCE(SUM(sl.quantidade_enviada), 0)
                       AS quantidade_enviada
                FROM compras_pedidos_envios e
                JOIN compras_pedidos_envios_linhas sl ON sl.envio_id = e.id
                WHERE e.tipo_pedido = 'semanal' AND e.pedido_id = %s
                  AND sl.artigo_id = %s
                """,
                (order_id, article_id),
            )
            already_sent = Decimal(
                str(cursor.fetchone()["quantidade_enviada"] or "0")
            )
            next_quantity = requested.get(article_id, {}).get("quantidade")
            if already_sent > 0 and (
                next_quantity is None or next_quantity < already_sent
            ):
                raise ValueError(
                    f'A alteração não pode reduzir/remover '
                    f'"{old_line["produto_snapshot"]}" abaixo da quantidade já enviada '
                    f'({already_sent:.3f}).'
                )
        cursor.execute("DELETE FROM compras_encomendas_semanais_linhas WHERE encomenda_id = %s", (order_id,))
        for article_id, requested_line in requested.items():
            current_item = catalogue[article_id]
            item = (
                old_by_article.get(article_id)
                if article_id in old_by_article
                and (
                    not current_item["ativo"]
                    or not current_item["encomendavel"]
                )
                else current_item
            )
            cursor.execute(
                """
                INSERT INTO compras_encomendas_semanais_linhas
                    (encomenda_id, artigo_id, produto_snapshot, unidade_snapshot,
                     origem_id_snapshot, origem_tipo_snapshot, origem_nome_snapshot,
                     origem_supplier_id_snapshot, origem_supplier_nome_snapshot,
                     fornecedor_oficial_id_snapshot,
                     fornecedor_oficial_nome_snapshot, quantidade, observacoes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    article_id,
                    item.get("produto_snapshot", item.get("produto")),
                    item.get("unidade_snapshot", item.get("unidade")) or "unidade",
                    item.get("origem_id_snapshot"),
                    item.get("origem_tipo_snapshot"),
                    item.get("origem_nome_snapshot"),
                    item.get("origem_supplier_id_snapshot"),
                    item.get("origem_supplier_nome_snapshot"),
                    item.get(
                        "fornecedor_oficial_id_snapshot",
                        item.get("fornecedor_oficial_id"),
                    ),
                    item.get(
                        "fornecedor_oficial_nome_snapshot",
                        item.get("fornecedor_oficial_nome"),
                    ),
                    requested_line["quantidade"], requested_line["observacoes"],
                ),
            )
        cursor.execute(
            """
            UPDATE compras_encomendas_semanais
            SET observacoes = COALESCE(%s, observacoes), updated_by = %s,
                updated_at = CURRENT_TIMESTAMP, versao_actual = %s
            WHERE id = %s
            """,
            (observacoes, actor, next_version, order_id),
        )
        fresh = _fetch_order(cursor, order_id)
        new_lines = _fetch_lines(cursor, order_id)
        _audit(
            cursor, order_id, "alteracao_pos_submissao", actor, reason=reason,
            payload={"antes": old_payload, "depois": _snapshot(fresh, new_lines)},
        )
        conn.commit()
    return _row_to_order(fresh, new_lines)


def list_weekly_orders(
    store_id: int | None = None,
    planning_sunday=None,
    statuses: list[str] | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if store_id is not None:
        clauses.append("o.store_id = %s")
        params.append(store_id)
    if planning_sunday is not None:
        cycle = weekly_cycle_dates(planning_sunday)
        clauses.append("o.ciclo_domingo = %s")
        params.append(cycle["ciclo_domingo"])
    if statuses:
        invalid = set(statuses) - set(WEEKLY_STATUSES)
        if invalid:
            raise ValueError("Estado da encomenda inválido.")
        clauses.append("o.status = ANY(%s)")
        params.append(list(statuses))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            f"""
            SELECT o.*, s.name AS loja_nome,
                   COALESCE(SUM(l.quantidade), 0) AS quantidade_total
            FROM compras_encomendas_semanais o
            JOIN stores s ON s.id = o.store_id
            LEFT JOIN compras_encomendas_semanais_linhas l ON l.encomenda_id = o.id
            {where}
            GROUP BY o.id, s.name
            ORDER BY o.ciclo_domingo DESC, s.name, o.id
            """,
            params,
        )
        return [_row_to_order(dict(row)) for row in cursor.fetchall()]


def get_weekly_consolidation(planning_sunday) -> list[dict]:
    cycle = weekly_cycle_dates(planning_sunday)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT l.artigo_id, l.produto_snapshot, l.unidade_snapshot,
                   COALESCE(
                       l.fornecedor_oficial_id_snapshot,
                       CASE WHEN l.origem_tipo_snapshot = 'fornecedor_externo'
                            THEN l.origem_supplier_id_snapshot END
                   ) AS fornecedor_oficial_id_snapshot,
                   COALESCE(
                       l.fornecedor_oficial_nome_snapshot,
                       CASE WHEN l.origem_tipo_snapshot = 'fornecedor_externo'
                            THEN l.origem_supplier_nome_snapshot END
                   ) AS fornecedor_oficial_nome_snapshot,
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
            WHERE o.ciclo_domingo = %s
              AND o.status IN ('submetida', 'em_preparacao', 'concluida')
            GROUP BY l.artigo_id, l.produto_snapshot, l.unidade_snapshot,
                     COALESCE(
                         l.fornecedor_oficial_id_snapshot,
                         CASE WHEN l.origem_tipo_snapshot = 'fornecedor_externo'
                              THEN l.origem_supplier_id_snapshot END
                     ),
                     COALESCE(
                         l.fornecedor_oficial_nome_snapshot,
                         CASE WHEN l.origem_tipo_snapshot = 'fornecedor_externo'
                              THEN l.origem_supplier_nome_snapshot END
                     )
             ORDER BY COALESCE(
                          l.fornecedor_oficial_nome_snapshot,
                          CASE WHEN l.origem_tipo_snapshot = 'fornecedor_externo'
                               THEN l.origem_supplier_nome_snapshot END
                      ) NULLS LAST,
                      l.produto_snapshot
            """,
            (cycle["ciclo_domingo"],),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["quantidade_total"] = float(item["quantidade_total"])
            rows.append(item)
        return rows


def get_weekly_order_audit(order_id: int) -> list[dict]:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, event_type, actor, reason, payload,
                   previous_status, new_status, created_at
            FROM compras_encomendas_semanais_audit
            WHERE encomenda_id = %s
            ORDER BY created_at, id
            """,
            (order_id,),
        )
        return [dict(row) for row in cursor.fetchall()]