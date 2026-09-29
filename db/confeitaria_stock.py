"""Audited, product-ID-based stock balances for Confeitaria."""

from collections.abc import Mapping
from datetime import date, datetime
from uuid import UUID, uuid4

from db.connection import db_connection


MOVEMENT_LABELS = {
    'saldo_inicial': 'Saldo inicial',
    'correcao': 'Acerto',
    'producao': 'Produção real',
}

_MAX_QUANTITY = 2_147_483_647
_MIN_QUANTITY = -2_147_483_648
_MAX_REASON_LENGTH = 2000


class ConfeitariaStockError(ValueError):
    """A user-correctable Confeitaria stock movement validation error."""


def _validate_product_id(product_id):
    if (
        isinstance(product_id, bool)
        or not isinstance(product_id, int)
        or product_id <= 0
    ):
        raise ConfeitariaStockError('Produto de Confeitaria inválido.')
    return product_id


def _validate_date(movement_date):
    if (
        not isinstance(movement_date, date)
        or isinstance(movement_date, datetime)
    ):
        raise ConfeitariaStockError('A data do movimento é inválida.')
    return movement_date


def _validate_movement(tipo, quantity):
    tipo = str(tipo or '').strip()
    if tipo not in ('saldo_inicial', 'correcao'):
        raise ConfeitariaStockError('Tipo de movimento inválido.')
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ConfeitariaStockError(
            'A quantidade tem de ser um número inteiro.'
        )
    if quantity < _MIN_QUANTITY or quantity > _MAX_QUANTITY:
        raise ConfeitariaStockError('A quantidade excede o limite permitido.')
    if tipo == 'saldo_inicial' and quantity < 0:
        raise ConfeitariaStockError('O saldo inicial não pode ser negativo.')
    if tipo == 'correcao' and quantity == 0:
        raise ConfeitariaStockError('O acerto não pode ser zero.')
    return tipo, quantity


def _validate_actor(actor):
    if not isinstance(actor, str) or not actor.strip():
        raise ConfeitariaStockError(
            'Não foi possível identificar o responsável.'
        )
    actor = actor.strip()
    if len(actor) > 100:
        raise ConfeitariaStockError('O nome do responsável é demasiado longo.')
    return actor


def _validate_actor_and_reason(actor, reason):
    actor = _validate_actor(actor)
    if not isinstance(reason, str) or not reason.strip():
        raise ConfeitariaStockError('O motivo é obrigatório.')
    reason = reason.strip()
    if len(reason) > _MAX_REASON_LENGTH:
        raise ConfeitariaStockError('O motivo é demasiado longo.')
    return actor, reason


def _validate_idempotency_key(idempotency_key):
    try:
        return str(UUID(str(idempotency_key)))
    except (AttributeError, TypeError, ValueError):
        raise ConfeitariaStockError(
            'A chave de submissão é inválida.'
        ) from None


def _lock_movement(cursor, product_id, idempotency_key):
    # Lock request identity first so the same key cannot be accepted with a
    # different payload, then serialize balance changes for this product.
    for lock_key in (
        f'confeitaria-stock-request:{idempotency_key}',
        f'confeitaria-stock-product:{product_id}',
    ):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )


def _stock_state(cursor, product_id):
    cursor.execute("""
        SELECT COALESCE(SUM(quantidade), 0),
               COALESCE(BOOL_OR(tipo = 'saldo_inicial'), FALSE)
        FROM confeitaria_stock_movements
        WHERE produto_confeitaria_id = %s
    """, (product_id,))
    row = cursor.fetchone()
    return int(row[0] or 0), bool(row[1])


def _format_balance(product_id, product_name, active, balance, opening_set):
    return {
        'produto_confeitaria_id': product_id,
        'produto_id': product_id,
        'produto': product_name,
        'ativo': bool(active),
        'saldo': int(balance),
        'saldo_inicial_confirmado': bool(opening_set),
        'transferivel': bool(active and opening_set and balance > 0),
    }


def _movement_result(
    *, movement_id, product_id, product_name, active, tipo, quantity,
    movement_date, actor, reason, balance, opening_set, idempotency_key,
    replayed,
):
    return {
        **_format_balance(
            product_id, product_name, active, balance, opening_set
        ),
        'movement_id': movement_id,
        'tipo': tipo,
        'tipo_label': MOVEMENT_LABELS[tipo],
        'quantidade': quantity,
        'data': movement_date,
        'responsavel': actor,
        'motivo': reason,
        'idempotency_key': idempotency_key,
        'replayed': replayed,
    }


def register_confeitaria_stock_movement(
    *,
    produto_confeitaria_id,
    tipo,
    quantidade,
    data,
    responsavel,
    motivo,
    idempotency_key,
):
    """Append one immutable opening or correction to the Confeitaria ledger.

    The idempotency key must be reused when retrying one submission. It returns
    the existing movement for an identical payload and rejects key reuse with
    a different payload.
    """
    product_id = _validate_product_id(produto_confeitaria_id)
    movement_date = _validate_date(data)
    tipo, quantity = _validate_movement(tipo, quantidade)
    actor, reason = _validate_actor_and_reason(responsavel, motivo)
    request_key = _validate_idempotency_key(idempotency_key)

    with db_connection() as conn:
        cursor = conn.cursor()
        _lock_movement(cursor, product_id, request_key)

        cursor.execute("""
            SELECT id, produto_confeitaria_id, tipo, quantidade, data,
                   responsavel, motivo, produto
            FROM confeitaria_stock_movements
            WHERE idempotency_key = %s
        """, (request_key,))
        existing = cursor.fetchone()
        if existing:
            existing_payload = (
                int(existing[1]), existing[2], int(existing[3]), existing[4],
                existing[5], existing[6],
            )
            requested_payload = (
                product_id, tipo, quantity, movement_date, actor, reason,
            )
            if existing_payload != requested_payload:
                raise ConfeitariaStockError(
                    'A chave de submissão já foi usada para outro movimento.'
                )
            cursor.execute("""
                SELECT ativo
                FROM produtos_confeitaria
                WHERE id = %s
            """, (product_id,))
            current_product = cursor.fetchone()
            if not current_product:
                raise ConfeitariaStockError(
                    'O produto de Confeitaria já não existe no catálogo.'
                )
            balance, opening_set = _stock_state(cursor, product_id)
            conn.commit()
            return _movement_result(
                movement_id=existing[0],
                product_id=product_id,
                product_name=existing[7],
                active=current_product[0],
                tipo=tipo,
                quantity=quantity,
                movement_date=movement_date,
                actor=actor,
                reason=reason,
                balance=balance,
                opening_set=opening_set,
                idempotency_key=request_key,
                replayed=True,
            )

        cursor.execute("""
            SELECT id, nome, ativo
            FROM produtos_confeitaria
            WHERE id = %s
            FOR SHARE
        """, (product_id,))
        product = cursor.fetchone()
        if not product:
            raise ConfeitariaStockError(
                'O produto de Confeitaria já não existe no catálogo.'
            )
        if not product[2]:
            raise ConfeitariaStockError(
                'O produto está inativo e não aceita novos movimentos.'
            )

        balance, opening_set = _stock_state(cursor, product_id)
        if tipo == 'saldo_inicial':
            if opening_set:
                raise ConfeitariaStockError(
                    f'O saldo inicial de {product[1]} já foi confirmado.'
                )
        else:
            if not opening_set:
                raise ConfeitariaStockError(
                    f'Confirme primeiro o saldo inicial de {product[1]}.'
                )
            if balance + quantity < 0:
                raise ConfeitariaStockError(
                    f'O acerto deixaria o saldo de {product[1]} negativo.'
                )

        cursor.execute("""
            INSERT INTO confeitaria_stock_movements (
                produto_confeitaria_id, produto, tipo, quantidade, data,
                responsavel, motivo, idempotency_key
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            product_id, product[1], tipo, quantity, movement_date,
            actor, reason, request_key,
        ))
        movement_id = cursor.fetchone()[0]
        conn.commit()

    new_balance = balance + quantity
    return _movement_result(
        movement_id=movement_id,
        product_id=product_id,
        product_name=product[1],
        active=product[2],
        tipo=tipo,
        quantity=quantity,
        movement_date=movement_date,
        actor=actor,
        reason=reason,
        balance=new_balance,
        opening_set=True,
        idempotency_key=request_key,
        replayed=False,
    )


def record_confeitaria_production_batch(
    *, data, production_values, responsavel,
):
    """Save real production, its legacy stock delta, and audit movements atomically.

    The locked production-plan value is the source for the effective delta.
    Reposting the same values therefore creates no duplicate stock movement.
    """
    movement_date = _validate_date(data)
    actor = _validate_actor(responsavel)
    if not isinstance(production_values, Mapping):
        raise ConfeitariaStockError('Os valores de produção são inválidos.')

    normalized_values = {}
    for raw_product_name, quantity in production_values.items():
        if not isinstance(raw_product_name, str):
            raise ConfeitariaStockError('Produto de produção inválido.')
        product_name = raw_product_name.strip()
        if not product_name or len(product_name) > 255:
            raise ConfeitariaStockError('Produto de produção inválido.')
        if product_name in normalized_values:
            raise ConfeitariaStockError(
                f'O produto {product_name} foi indicado mais de uma vez.'
            )
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ConfeitariaStockError(
                'A produção real tem de ser um número inteiro.'
            )
        if quantity < 0:
            raise ConfeitariaStockError(
                'A produção real não pode ser negativa.'
            )
        if quantity > _MAX_QUANTITY:
            raise ConfeitariaStockError(
                'A produção real excede o limite permitido.'
            )
        normalized_values[product_name] = quantity

    if not normalized_values:
        return []

    product_names = sorted(normalized_values)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT produto, producao_real
            FROM plano_producao_confeitaria
            WHERE data = %s AND produto = ANY(%s)
            ORDER BY produto
            FOR UPDATE
        """, (movement_date, product_names))
        plan_rows = {
            row[0]: row[1]
            for row in cursor.fetchall()
        }

        for product_name in product_names:
            if product_name not in plan_rows:
                raise ConfeitariaStockError(
                    f'O produto {product_name} já não está no plano desta data.'
                )

        changes = []
        for product_name in product_names:
            previous_real = plan_rows[product_name]
            target_real = normalized_values[product_name]
            previous_quantity = int(previous_real or 0)
            delta = target_real - previous_quantity
            changes.append({
                'produto': product_name,
                'previous_real': previous_real,
                'target_real': target_real,
                'delta': delta,
                'changed': previous_real != target_real,
            })

        changed_names = [
            change['produto'] for change in changes if change['changed']
        ]
        products_by_name = {}
        if changed_names:
            cursor.execute("""
                SELECT id, nome, ativo
                FROM produtos_confeitaria
                WHERE nome = ANY(%s)
                ORDER BY id
                FOR SHARE
            """, (changed_names,))
            products_by_name = {
                row[1]: (int(row[0]), bool(row[2]))
                for row in cursor.fetchall()
            }
            for product_name in changed_names:
                product = products_by_name.get(product_name)
                if not product:
                    raise ConfeitariaStockError(
                        f'O produto {product_name} já não existe no catálogo.'
                    )
                if not product[1]:
                    raise ConfeitariaStockError(
                        f'O produto {product_name} está inativo e não aceita '
                        'nova produção.'
                    )

        movement_changes = [
            change for change in changes if change['delta'] != 0
        ]
        for product_id in sorted(
            products_by_name[change['produto']][0]
            for change in movement_changes
        ):
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f'confeitaria-stock-product:{product_id}',),
            )

        balance_by_name = {}
        for change in movement_changes:
            product_id = products_by_name[change['produto']][0]
            balance, opening_set = _stock_state(cursor, product_id)
            if not opening_set:
                raise ConfeitariaStockError(
                    f"Confirme primeiro o saldo inicial de "
                    f"{change['produto']} antes de registar produção."
                )
            if balance + change['delta'] < 0:
                raise ConfeitariaStockError(
                    f"A correção da produção deixaria o saldo de "
                    f"{change['produto']} negativo."
                )
            balance_by_name[change['produto']] = balance

        results = []
        for change in changes:
            product_name = change['produto']
            target_real = change['target_real']
            delta = change['delta']
            movement_id = None

            if change['changed']:
                cursor.execute("""
                    UPDATE plano_producao_confeitaria
                    SET producao_real = %s, updated_at = NOW()
                    WHERE data = %s AND produto = %s
                """, (target_real, movement_date, product_name))

            if delta:
                product_id = products_by_name[product_name][0]
                previous_quantity = int(change['previous_real'] or 0)
                operation = (
                    'Produção real corrigida'
                    if change['previous_real'] is not None
                    else 'Produção real registada'
                )
                reason = (
                    f'{operation} de {previous_quantity} para '
                    f'{target_real}; diferença {delta:+d}.'
                )
                request_key = str(uuid4())

                # Keep legacy transfers working until their separate cutover,
                # without ever using legacy quantity in the audited balance.
                cursor.execute("""
                    INSERT INTO stock_producao_confeitaria (
                        data, produto, quantidade
                    )
                    VALUES (%s, %s, GREATEST(%s, 0))
                    ON CONFLICT (data, produto) DO UPDATE
                    SET quantidade = GREATEST(
                            stock_producao_confeitaria.quantidade + %s, 0
                        ),
                        updated_at = NOW()
                """, (
                    movement_date, product_name, delta, delta,
                ))

                cursor.execute("""
                    INSERT INTO confeitaria_stock_movements (
                        produto_confeitaria_id, produto, tipo, quantidade,
                        data, responsavel, motivo, idempotency_key
                    )
                    VALUES (%s, %s, 'producao', %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    product_id, product_name, delta, movement_date,
                    actor, reason, request_key,
                ))
                movement_id = cursor.fetchone()[0]

            results.append({
                'produto': product_name,
                'producao_real': target_real,
                'delta': delta,
                'changed': change['changed'],
                'movement_id': movement_id,
                'saldo_auditado': (
                    balance_by_name[product_name] + delta
                    if delta else None
                ),
            })

        conn.commit()
    return results


def get_confeitaria_stock_balance(produto_confeitaria_id):
    """Read the confirmed balance; legacy production totals are not consulted."""
    product_id = _validate_product_id(produto_confeitaria_id)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.nome, p.ativo,
                   COALESCE(SUM(m.quantidade), 0),
                   COALESCE(BOOL_OR(m.tipo = 'saldo_inicial'), FALSE)
            FROM produtos_confeitaria p
            LEFT JOIN confeitaria_stock_movements m
              ON m.produto_confeitaria_id = p.id
            WHERE p.id = %s
            GROUP BY p.id, p.nome, p.ativo
        """, (product_id,))
        row = cursor.fetchone()
    if not row:
        raise ConfeitariaStockError(
            'O produto de Confeitaria já não existe no catálogo.'
        )
    return _format_balance(row[0], row[1], row[2], row[3], row[4])


def get_confeitaria_stock_options():
    """Return catalogue products with balances derived only from movements."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.nome, p.ativo,
                   COALESCE(SUM(m.quantidade), 0),
                   COALESCE(BOOL_OR(m.tipo = 'saldo_inicial'), FALSE)
            FROM produtos_confeitaria p
            LEFT JOIN confeitaria_stock_movements m
              ON m.produto_confeitaria_id = p.id
            GROUP BY p.id, p.nome, p.ativo
            ORDER BY p.nome, p.id
        """)
        rows = cursor.fetchall()
    return [
        _format_balance(
            row[0], row[1], row[2], row[3], row[4]
        )
        for row in rows
    ]


def get_confeitaria_stock_movements(
    *, produto_confeitaria_id=None, limit=100
):
    """Return immutable movement history with original label snapshots."""
    if produto_confeitaria_id is not None:
        produto_confeitaria_id = _validate_product_id(
            produto_confeitaria_id
        )
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100

    query = """
        SELECT id, produto_confeitaria_id, produto, tipo, quantidade, data,
               responsavel, motivo, idempotency_key, created_at
        FROM confeitaria_stock_movements
    """
    params = []
    if produto_confeitaria_id is not None:
        query += " WHERE produto_confeitaria_id = %s"
        params.append(produto_confeitaria_id)
    query += " ORDER BY data DESC, created_at DESC, id DESC LIMIT %s"
    params.append(limit)

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
    return [{
        'id': row[0],
        'produto_confeitaria_id': row[1],
        'produto': row[2],
        'tipo': row[3],
        'tipo_label': MOVEMENT_LABELS[row[3]],
        'quantidade': int(row[4]),
        'data': row[5],
        'responsavel': row[6],
        'motivo': row[7],
        'idempotency_key': str(row[8]),
        'created_at': row[9],
    } for row in rows]