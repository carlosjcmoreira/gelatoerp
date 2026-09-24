"""Audited production-stock balances for Pastelaria transfers."""

from db.connection import db_connection


CATALOGUE_PREFIX = "catalog:"
CAKE_PREFIX = "cake:"

MOVEMENT_LABELS = {
    "saldo_inicial": "Saldo inicial",
    "producao": "Produção",
    "correcao": "Correção",
    "transferencia_saida": "Transferência",
    "transferencia_anulacao": "Anulação de transferência",
}


class PastelariaStockError(ValueError):
    """A user-correctable production-stock validation error."""


def _lock_stock_identity(cursor, identity_key):
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"pastelaria-production-stock:{identity_key}",),
    )


def _resolve_identity_cursor(
    cursor, identity_key, *, cake_plan_date=None, require_active=False
):
    identity_key = str(identity_key or "").strip()
    if identity_key.startswith(CATALOGUE_PREFIX):
        try:
            product_id = int(identity_key[len(CATALOGUE_PREFIX):])
        except (TypeError, ValueError):
            raise PastelariaStockError("Produto inválido.")
        cursor.execute("""
            SELECT id, CONCAT_WS(', ',
                NULLIF(BTRIM(tipologia), ''),
                NULLIF(BTRIM(sabor), ''),
                NULLIF(BTRIM(cobertura), '')
            ), ativo
            FROM produtos_pastelaria
            WHERE id = %s
        """, (product_id,))
        row = cursor.fetchone()
        if not row:
            raise PastelariaStockError(
                "O produto de Pastelaria já não existe no catálogo."
            )
        if require_active and not row[2]:
            raise PastelariaStockError(
                "O produto foi desativado e já não pode ser transferido."
            )
        return {
            "identity_key": f"{CATALOGUE_PREFIX}{row[0]}",
            "produto_pastelaria_id": row[0],
            "produto": row[1],
            "kind": "catalogue",
        }

    if identity_key.startswith(CAKE_PREFIX):
        label = identity_key[len(CAKE_PREFIX):]
        if not label.startswith("Bolo — ") or len(label) > 255:
            raise PastelariaStockError("Configuração de bolo inválida.")
        query = """
            SELECT 1
            FROM plano_producao_pastelaria
            WHERE produto = %s AND produto LIKE 'Bolo — %%'
        """
        params = [label]
        if cake_plan_date is not None:
            query += " AND data = %s"
            params.append(cake_plan_date)
        query += " LIMIT 1"
        cursor.execute(query, params)
        if not cursor.fetchone():
            raise PastelariaStockError(
                "O bolo configurado já não está disponível para esta data."
            )
        return {
            "identity_key": f"{CAKE_PREFIX}{label}",
            "produto_pastelaria_id": None,
            "produto": label,
            "kind": "cake",
        }

    raise PastelariaStockError("Produto inválido.")


def identity_key_for_product_label(label, *, cake_plan_date=None):
    label = str(label or "").strip()
    with db_connection() as conn:
        cursor = conn.cursor()
        if label.startswith("Bolo — "):
            identity_key = f"{CAKE_PREFIX}{label}"
            _resolve_identity_cursor(
                cursor,
                identity_key,
                cake_plan_date=cake_plan_date,
            )
            return identity_key

        cursor.execute("""
            SELECT id
            FROM produtos_pastelaria
            WHERE CONCAT_WS(', ',
                NULLIF(BTRIM(tipologia), ''),
                NULLIF(BTRIM(sabor), ''),
                NULLIF(BTRIM(cobertura), '')
            ) = %s AND ativo = TRUE
            ORDER BY id
        """, (label,))
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise PastelariaStockError(
            f"Produto de Pastelaria sem identidade única: {label}"
        )
    return f"{CATALOGUE_PREFIX}{rows[0][0]}"


def _stock_state_cursor(cursor, identity_key):
    cursor.execute("""
        SELECT COALESCE(SUM(quantidade), 0),
               COALESCE(BOOL_OR(tipo = 'saldo_inicial'), FALSE)
        FROM pastelaria_stock_movements
        WHERE identity_key = %s
    """, (identity_key,))
    row = cursor.fetchone()
    return int(row[0] or 0), bool(row[1])


def _insert_stock_movement_cursor(
    cursor, *, identity, tipo, quantidade, data, responsavel, motivo,
    ordem_transferencia_id=None,
):
    cursor.execute("""
        INSERT INTO pastelaria_stock_movements (
            identity_key, produto_pastelaria_id, produto, tipo, quantidade,
            data, responsavel, motivo, ordem_transferencia_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        identity["identity_key"],
        identity["produto_pastelaria_id"],
        identity["produto"],
        tipo,
        int(quantidade),
        data,
        responsavel,
        motivo.strip(),
        ordem_transferencia_id,
    ))
    return cursor.fetchone()[0]


def get_pastelaria_stock_options():
    """Return catalogue/cake identities and their separately confirmed balances."""
    with db_connection() as conn:
        cursor = conn.cursor()
        identities = {}

        cursor.execute("""
            SELECT id, CONCAT_WS(', ',
                NULLIF(BTRIM(tipologia), ''),
                NULLIF(BTRIM(sabor), ''),
                NULLIF(BTRIM(cobertura), '')
            )
            FROM produtos_pastelaria
            WHERE ativo = TRUE
            ORDER BY tipologia, sabor, cobertura, id
        """)
        for product_id, label in cursor.fetchall():
            key = f"{CATALOGUE_PREFIX}{product_id}"
            identities[key] = {
                "identity_key": key,
                "produto_pastelaria_id": product_id,
                "produto": label,
                "kind": "catalogue",
                "saldo": 0,
                "saldo_inicial_confirmado": False,
                "ativo": True,
            }

        cursor.execute("""
            SELECT DISTINCT produto
            FROM plano_producao_pastelaria
            WHERE produto LIKE 'Bolo — %%'
            ORDER BY produto
        """)
        for (label,) in cursor.fetchall():
            key = f"{CAKE_PREFIX}{label}"
            identities.setdefault(key, {
                "identity_key": key,
                "produto_pastelaria_id": None,
                "produto": label,
                "kind": "cake",
                "saldo": 0,
                "saldo_inicial_confirmado": False,
                "ativo": True,
            })

        cursor.execute("""
            SELECT identity_key, MAX(produto_pastelaria_id), MAX(produto),
                   SUM(quantidade),
                   COALESCE(BOOL_OR(tipo = 'saldo_inicial'), FALSE)
            FROM pastelaria_stock_movements
            GROUP BY identity_key
        """)
        for key, product_id, label, balance, opening_confirmed in cursor.fetchall():
            if key not in identities:
                if key.startswith(CATALOGUE_PREFIX):
                    kind = "catalogue"
                elif key.startswith(CAKE_PREFIX):
                    kind = "cake"
                    label = key[len(CAKE_PREFIX):]
                else:
                    kind = "historico"
                identities[key] = {
                    "identity_key": key,
                    "produto_pastelaria_id": product_id,
                    "produto": label,
                    "kind": kind,
                    "saldo": 0,
                    "saldo_inicial_confirmado": False,
                    "ativo": False,
                }
            identities[key]["saldo"] = int(balance or 0)
            identities[key]["saldo_inicial_confirmado"] = bool(
                opening_confirmed
            )

    return sorted(
        identities.values(),
        key=lambda item: (str(item["produto"]).casefold(), item["identity_key"]),
    )


def get_pastelaria_stock_movements(limit=100):
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, data, produto, tipo, quantidade, responsavel, motivo,
                   ordem_transferencia_id, created_at
            FROM pastelaria_stock_movements
            ORDER BY data DESC, created_at DESC, id DESC
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
    return [{
        "id": row[0],
        "data": row[1],
        "produto": row[2],
        "tipo": row[3],
        "tipo_label": MOVEMENT_LABELS.get(row[3], row[3]),
        "quantidade": int(row[4]),
        "responsavel": row[5],
        "motivo": row[6],
        "ordem_transferencia_id": row[7],
        "created_at": row[8],
    } for row in rows]


def register_pastelaria_stock_movement(
    *, identity_key, tipo, quantidade, data, responsavel, motivo,
):
    tipo = str(tipo or "").strip()
    if tipo not in ("saldo_inicial", "producao", "correcao"):
        raise PastelariaStockError("Tipo de movimento inválido.")
    try:
        quantidade = int(quantidade)
    except (TypeError, ValueError):
        raise PastelariaStockError("A quantidade tem de ser um número inteiro.")
    if tipo == "saldo_inicial" and quantidade < 0:
        raise PastelariaStockError("O saldo inicial não pode ser negativo.")
    if tipo == "producao" and quantidade <= 0:
        raise PastelariaStockError("A produção tem de ser superior a zero.")
    if tipo == "correcao" and quantidade == 0:
        raise PastelariaStockError("A correção não pode ser zero.")

    responsavel = str(responsavel or "").strip()
    motivo = str(motivo or "").strip()
    if not responsavel:
        raise PastelariaStockError("Não foi possível identificar o responsável.")
    if not motivo:
        raise PastelariaStockError("O motivo é obrigatório.")

    with db_connection() as conn:
        cursor = conn.cursor()
        identity = _resolve_identity_cursor(cursor, identity_key)
        _lock_stock_identity(cursor, identity["identity_key"])
        balance, opening_confirmed = _stock_state_cursor(
            cursor, identity["identity_key"]
        )

        if tipo == "saldo_inicial":
            if opening_confirmed:
                raise PastelariaStockError(
                    f"O saldo inicial de {identity['produto']} já foi confirmado."
                )
        else:
            if not opening_confirmed:
                raise PastelariaStockError(
                    f"Confirme primeiro o saldo inicial de {identity['produto']}."
                )
            if balance + quantidade < 0:
                raise PastelariaStockError(
                    f"A correção deixaria o saldo de {identity['produto']} "
                    "negativo."
                )

        _insert_stock_movement_cursor(
            cursor,
            identity=identity,
            tipo=tipo,
            quantidade=quantidade,
            data=data,
            responsavel=responsavel,
            motivo=motivo,
        )
        conn.commit()
        return {
            "identity_key": identity["identity_key"],
            "produto": identity["produto"],
            "saldo": balance + quantidade,
            "saldo_inicial_confirmado": True,
        }