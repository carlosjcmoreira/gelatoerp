"""Auditable physical counts of the purchasing catalogue.

Counts are planning evidence only.  A submitted count is an immutable
snapshot and never writes to any of the specialised stock tables.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from db.connection import db_connection


LISBON = ZoneInfo("Europe/Lisbon")
COUNT_STATUSES = ("rascunho", "submetida")


def lisboa_today() -> date:
    return datetime.now(LISBON).date()


def _as_date(value, message="A data da contagem é inválida.") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        raise ValueError(message)


def normalise_count_date(value, today: date | None = None) -> date:
    selected = _as_date(value)
    if selected > (today or lisboa_today()):
        raise ValueError("A data da contagem não pode estar no futuro.")
    return selected


def _quantity(value) -> Decimal:
    try:
        quantity = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError):
        raise ValueError("A quantidade contada tem de ser um número válido.")
    if not quantity.is_finite() or quantity < 0 or quantity > Decimal("100000000"):
        raise ValueError("A quantidade contada tem de estar entre 0 e 100.000.000.")
    return quantity.quantize(Decimal("0.001"))


def _row_to_count(row, lines=None) -> dict:
    result = dict(row)
    if lines is not None:
        result["linhas"] = lines
    return result


def _row_to_line(row) -> dict:
    result = dict(row)
    if result.get("quantidade") is not None:
        result["quantidade"] = float(result["quantidade"])
    return result


def _fetch_count(cursor, count_id: int, for_update=False) -> dict | None:
    cursor.execute(
        """
        SELECT c.*, s.name AS loja_nome
        FROM compras_contagens_artigos c
        JOIN stores s ON s.id = c.store_id
        WHERE c.id = %s
        """ + (" FOR UPDATE" if for_update else ""),
        (count_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _fetch_lines(cursor, count_id: int) -> list[dict]:
    cursor.execute(
        """
        SELECT l.*, a.ativo AS artigo_ativo_atual
        FROM compras_contagens_artigos_linhas l
        LEFT JOIN artigos_administrativos a ON a.id = l.artigo_id
        WHERE l.contagem_id = %s
        ORDER BY l.produto_snapshot, l.id
        """,
        (count_id,),
    )
    return [_row_to_line(row) for row in cursor.fetchall()]


def _catalogue_rows(cursor, article_ids=None, active_only=True) -> dict[int, dict]:
    clauses = []
    params = []
    if article_ids is not None:
        clauses.append("a.id = ANY(%s)")
        params.append(article_ids)
    if active_only:
        clauses.extend(["a.ativo = TRUE", "o.ativo = TRUE"])
    cursor.execute(
        """
        SELECT a.id, a.produto, a.unidade, a.ativo,
               o.id AS catalog_origin_id, o.chave AS catalog_origin_key,
               o.tipo AS catalog_origin_type, o.nome AS catalog_origin_name,
               o.supplier_id, s.name AS supplier_nome,
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
        WHERE (
            (o.tipo = 'fornecedor_externo' AND o.supplier_id IS NOT NULL)
            OR (o.chave = 'categoria:moedas' AND hub.id IS NOT NULL)
        )
        """ + (" AND " + " AND ".join(clauses) if clauses else ""),
        params,
    )
    return {int(row["id"]): dict(row) for row in cursor.fetchall()}


def get_available_count_articles() -> list[dict]:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        rows = _catalogue_rows(cursor)
    return sorted(
        rows.values(),
        key=lambda row: (
            row["source_origin_name"] or "",
            row["produto"] or "",
            row["id"],
        ),
    )


def _normalise_lines(cursor, lines: list[dict]) -> tuple[dict[int, dict], dict[int, dict]]:
    requested = {}
    for raw in lines or []:
        try:
            article_id = int(raw.get("artigo_id"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("O artigo selecionado é inválido.")
        if article_id in requested:
            raise ValueError("O mesmo artigo não pode aparecer duas vezes.")
        requested[article_id] = {
            "quantidade": _quantity(raw.get("quantidade")),
            "observacoes": str(raw.get("observacoes") or "").strip()[:1000],
        }
    catalogue = _catalogue_rows(cursor, list(requested), active_only=True)
    if len(catalogue) != len(requested):
        raise ValueError("Um dos artigos deixou de estar disponível para contagem.")
    return requested, catalogue


def _snapshot(count: dict, lines: list[dict], version: int) -> dict:
    return {
        "contagem_id": count["id"],
        "versao": version,
        "store_id": count["store_id"],
        "loja_nome": count.get("loja_nome"),
        "data_contagem": count["data_contagem"].isoformat(),
        "created_by": count["created_by"],
        "submitted_by": count.get("submitted_by"),
        "linhas": [
            {
                "artigo_id": line.get("artigo_id"),
                "produto": line["produto_snapshot"],
                "unidade": line["unidade_snapshot"],
                "origem_id": line.get("origem_id_snapshot"),
                "origem_tipo": line.get("origem_tipo_snapshot"),
                "origem_nome": line.get("origem_nome_snapshot"),
                "origem_store_id": line.get("origem_store_id_snapshot"),
                "supplier_id": line.get("origem_supplier_id_snapshot"),
                "supplier_nome": line.get("origem_supplier_nome_snapshot"),
                "quantidade": line["quantidade"],
                "observacoes": line.get("observacoes") or "",
            }
            for line in lines
        ],
    }


def _audit(cursor, count_id, event_type, actor, payload=None):
    cursor.execute(
        """
        INSERT INTO compras_contagens_artigos_audit
            (contagem_id, event_type, actor, payload)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (
            count_id,
            event_type,
            actor or "sistema",
            json.dumps(payload or {}, ensure_ascii=False, default=str),
        ),
    )


def get_or_create_count_draft(store_id: int, count_date, actor: str) -> dict:
    selected = normalise_count_date(count_date)
    actor = str(actor or "sistema")[:100]
    draft_key = hashlib.sha256(
        f"{int(store_id)}|{selected.isoformat()}|{actor}".encode("utf-8")
    ).hexdigest()
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO compras_contagens_artigos
                (store_id, data_contagem, status, draft_key, created_by,
                 updated_by, updated_at)
            VALUES (%s, %s, 'rascunho', %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (store_id, selected, draft_key, actor, actor),
        )
        inserted = cursor.fetchone()
        if inserted:
            count_id = int(inserted["id"])
        else:
            cursor.execute(
                """
                SELECT id FROM compras_contagens_artigos
                WHERE draft_key = %s AND status = 'rascunho'
                FOR UPDATE
                """,
                (draft_key,),
            )
            existing = cursor.fetchone()
            if not existing:
                raise ValueError("Não foi possível abrir a contagem.")
            count_id = int(existing["id"])
        count = _fetch_count(cursor, count_id)
        lines = _fetch_lines(cursor, count_id)
        conn.commit()
    return _row_to_count(count, lines)


def save_count_draft(count_id: int, lines: list[dict], actor: str) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        count = _fetch_count(cursor, int(count_id), for_update=True)
        if not count:
            raise ValueError("Contagem não encontrada.")
        if count["status"] != "rascunho":
            raise ValueError("Esta contagem já foi submetida e não aceita alterações.")
        requested, catalogue = _normalise_lines(cursor, lines)
        cursor.execute(
            "DELETE FROM compras_contagens_artigos_linhas WHERE contagem_id = %s",
            (count_id,),
        )
        for article_id, values in requested.items():
            item = catalogue[article_id]
            cursor.execute(
                """
                INSERT INTO compras_contagens_artigos_linhas
                    (contagem_id, artigo_id, produto_snapshot, unidade_snapshot,
                     origem_id_snapshot, origem_tipo_snapshot, origem_nome_snapshot,
                     origem_store_id_snapshot, origem_supplier_id_snapshot,
                     origem_supplier_nome_snapshot, quantidade, observacoes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    count_id, article_id, item["produto"], item["unidade"] or "unidade",
                    item["source_origin_id"], item["source_origin_type"],
                    item["source_origin_name"], item["source_origin_store_id"],
                    item["source_supplier_id"], item["source_supplier_name"],
                    values["quantidade"], values["observacoes"],
                ),
            )
        cursor.execute(
            """
            UPDATE compras_contagens_artigos
            SET updated_by = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (actor, count_id),
        )
        fresh = _fetch_count(cursor, count_id)
        fresh_lines = _fetch_lines(cursor, count_id)
        conn.commit()
    return _row_to_count(fresh, fresh_lines)


def submit_count(count_id: int, actor: str) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        count = _fetch_count(cursor, int(count_id), for_update=True)
        if not count:
            raise ValueError("Contagem não encontrada.")
        if count["status"] == "submetida":
            cursor.execute(
                """
                SELECT snapshot FROM compras_contagens_artigos_versoes
                WHERE contagem_id = %s AND versao = %s
                """,
                (count_id, count["versao_submetida"]),
            )
            row = cursor.fetchone()
            conn.commit()
            return dict(row["snapshot"]) if row else _row_to_count(count, [])
        lines = _fetch_lines(cursor, count_id)
        if not lines:
            raise ValueError("Registe pelo menos um artigo antes de submeter.")
        version = 1
        snapshot = _snapshot(count, lines, version)
        cursor.execute(
            """
            INSERT INTO compras_contagens_artigos_versoes
                (contagem_id, versao, snapshot, actor)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (contagem_id, versao) DO NOTHING
            """,
            (
                count_id, version,
                json.dumps(snapshot, ensure_ascii=False, default=str),
                actor,
            ),
        )
        cursor.execute(
            """
            UPDATE compras_contagens_artigos
            SET status = 'submetida', draft_key = NULL,
                versao_submetida = %s, submitted_by = %s,
                submitted_at = CURRENT_TIMESTAMP,
                updated_by = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (version, actor, actor, count_id),
        )
        count = _fetch_count(cursor, count_id)
        _audit(cursor, count_id, "submetida", actor, snapshot)
        conn.commit()
    return snapshot


def get_count_draft(count_id: int) -> dict | None:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        count = _fetch_count(cursor, int(count_id))
        lines = _fetch_lines(cursor, int(count_id)) if count else []
    return _row_to_count(count, lines) if count else None


def _snapshot_from_row(row) -> dict:
    snapshot = row["snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    result = dict(snapshot)
    result["submitted_at"] = row.get("submitted_at")
    result["id"] = row["id"]
    result["status"] = row["status"]
    return result


def list_submitted_counts(
    store_id: int | None = None,
    date_from=None,
    date_to=None,
    article_query: str | None = None,
    origin_id: int | None = None,
    limit: int = 200,
) -> list[dict]:
    clauses = ["c.status = 'submetida'"]
    params = []
    if store_id is not None:
        clauses.append("c.store_id = %s")
        params.append(int(store_id))
    if date_from not in (None, ""):
        clauses.append("c.data_contagem >= %s")
        params.append(_as_date(date_from, "A data inicial é inválida."))
    if date_to not in (None, ""):
        clauses.append("c.data_contagem <= %s")
        params.append(_as_date(date_to, "A data final é inválida."))
    if article_query:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM jsonb_array_elements(v.snapshot->'linhas') AS line
                WHERE LOWER(line->>'produto') LIKE LOWER(%s)
                   OR line->>'artigo_id' = %s
            )
            """
        )
        params.extend([f"%{article_query.strip()}%", article_query.strip()])
    if origin_id is not None:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM jsonb_array_elements(v.snapshot->'linhas') AS line
                WHERE line->>'origem_id' = %s
            )
            """
        )
        params.append(str(int(origin_id)))
    params.append(max(1, min(int(limit), 1000)))
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            f"""
            SELECT c.id, c.store_id, c.data_contagem, c.status,
                   c.created_by, c.submitted_by, c.submitted_at,
                   s.name AS loja_nome, v.snapshot
            FROM compras_contagens_artigos c
            JOIN stores s ON s.id = c.store_id
            JOIN compras_contagens_artigos_versoes v
              ON v.contagem_id = c.id AND v.versao = c.versao_submetida
            WHERE {' AND '.join(clauses)}
            ORDER BY c.data_contagem DESC, c.submitted_at DESC, c.id DESC
            LIMIT %s
            """,
            params,
        )
        return [_snapshot_from_row(row) for row in cursor.fetchall()]


def get_latest_count(store_id: int) -> dict | None:
    rows = list_submitted_counts(store_id=store_id, limit=1)
    return rows[0] if rows else None


def get_count_history(store_id: int, limit: int = 50) -> list[dict]:
    return list_submitted_counts(store_id=store_id, limit=limit)


def get_submitted_count(count_id: int) -> dict | None:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT c.id, c.store_id, c.data_contagem, c.status,
                   c.created_by, c.submitted_by, c.submitted_at,
                   s.name AS loja_nome, v.snapshot
            FROM compras_contagens_artigos c
            JOIN stores s ON s.id = c.store_id
            JOIN compras_contagens_artigos_versoes v
              ON v.contagem_id = c.id AND v.versao = c.versao_submetida
            WHERE c.id = %s AND c.status = 'submetida'
            """,
            (count_id,),
        )
        row = cursor.fetchone()
    return _snapshot_from_row(row) if row else None


def get_count_origins() -> list[dict]:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT DISTINCT
                   (line->>'origem_id')::INTEGER AS id,
                   line->>'origem_nome' AS nome,
                   line->>'origem_tipo' AS tipo
            FROM compras_contagens_artigos_versoes v
            CROSS JOIN LATERAL jsonb_array_elements(v.snapshot->'linhas') AS line
            ORDER BY nome
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def get_count_audit(count_id: int) -> list[dict]:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, event_type, actor, payload, created_at
            FROM compras_contagens_artigos_audit
            WHERE contagem_id = %s
            ORDER BY created_at, id
            """,
            (count_id,),
        )
        return [dict(row) for row in cursor.fetchall()]