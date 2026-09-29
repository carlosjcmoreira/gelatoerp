"""Atomic, audited Confeitaria transfer batches and cutover reconciliation."""

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from uuid import uuid4

from db.connection import db_connection
from db import confeitaria_stock


ConfeitariaStockError = confeitaria_stock.ConfeitariaStockError
_MAX_QUANTITY = confeitaria_stock._MAX_QUANTITY


def _lock_product(cursor, product_id):
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f'confeitaria-stock-product:{product_id}',),
    )


def _validate_reconciliation_lines(lines):
    if not isinstance(lines, (list, tuple)):
        raise ConfeitariaStockError('Os saldos a confirmar são inválidos.')
    normalized = {}
    for line in lines:
        if not isinstance(line, Mapping):
            raise ConfeitariaStockError('Produto de Confeitaria inválido.')
        product_id = confeitaria_stock._validate_product_id(
            line.get('produto_confeitaria_id')
        )
        quantity = line.get('saldo_confirmado')
        expected = line.get('saldo_esperado')
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
            or quantity > _MAX_QUANTITY
        ):
            raise ConfeitariaStockError(
                'O saldo físico confirmado tem de ser um inteiro não negativo.'
            )
        if (
            isinstance(expected, bool)
            or not isinstance(expected, int)
            or expected < confeitaria_stock._MIN_QUANTITY
            or expected > _MAX_QUANTITY
        ):
            raise ConfeitariaStockError(
                'O saldo auditado da página expirou. Atualize antes de confirmar.'
            )
        if product_id in normalized:
            raise ConfeitariaStockError(
                'O mesmo produto foi indicado mais de uma vez.'
            )
        normalized[product_id] = {
            'saldo_confirmado': quantity,
            'saldo_esperado': expected,
        }
    if not normalized:
        raise ConfeitariaStockError(
            'Indique pelo menos um saldo físico atual para confirmar.'
        )
    return normalized


def reconciliar_confeitaria_stock_corte(
    *, lines, data, responsavel, confirmacao_explicita=False,
):
    """Record a one-time, operator-confirmed current count before transfers.

    The count is entered by the operator after considering legacy transfers
    made since the preparatory opening. It is never copied from legacy totals.
    The expected audited balance prevents applying a count made against a
    stale page while production or another audited stock write is in flight.
    """
    movement_date = confeitaria_stock._validate_date(data)
    actor = confeitaria_stock._validate_actor(responsavel)
    if confirmacao_explicita is not True:
        raise ConfeitariaStockError(
            'Confirme que a contagem atual considera as transferências '
            'legadas posteriores ao saldo inicial.'
        )
    requested = _validate_reconciliation_lines(lines)
    product_ids = sorted(requested)

    with db_connection() as conn:
        cursor = conn.cursor()
        for product_id in product_ids:
            _lock_product(cursor, product_id)

        cursor.execute("""
            SELECT id, nome, ativo
            FROM produtos_confeitaria
            WHERE id = ANY(%s)
            ORDER BY id
            FOR SHARE
        """, (product_ids,))
        products = {
            int(row[0]): {'produto': row[1], 'ativo': bool(row[2])}
            for row in cursor.fetchall()
        }
        missing = set(product_ids) - set(products)
        if missing:
            raise ConfeitariaStockError(
                'Um ou mais produtos de Confeitaria já não existem.'
            )

        results = []
        for product_id in product_ids:
            product = products[product_id]
            if not product['ativo']:
                raise ConfeitariaStockError(
                    f"O produto {product['produto']} está inativo."
                )

            balance, opening_set = confeitaria_stock._stock_state(
                cursor, product_id
            )
            if not opening_set:
                raise ConfeitariaStockError(
                    f"Confirme primeiro o saldo inicial de "
                    f"{product['produto']}."
                )

            if confeitaria_stock._cutover_confirmed(cursor, product_id):
                results.append({
                    'produto_confeitaria_id': product_id,
                    'produto': product['produto'],
                    'saldo': balance,
                    'replayed': True,
                })
                continue

            expected_balance = requested[product_id]['saldo_esperado']
            if balance != expected_balance:
                raise ConfeitariaStockError(
                    f"O saldo auditado de {product['produto']} mudou desde "
                    "a abertura da página. Atualize e volte a confirmar a "
                    "contagem."
                )

            confirmed_balance = requested[product_id]['saldo_confirmado']
            delta = confirmed_balance - balance
            reason = (
                'Reconciliação de corte: contagem física atual confirmada '
                'após considerar as transferências legadas posteriores ao '
                f'saldo inicial; saldo auditado anterior {balance}, '
                f'saldo físico confirmado {confirmed_balance}.'
            )
            cursor.execute("""
                INSERT INTO confeitaria_stock_movements (
                    produto_confeitaria_id, produto, tipo, quantidade, data,
                    responsavel, motivo, idempotency_key
                )
                VALUES (%s, %s, 'reconciliacao_corte', %s, %s, %s, %s, %s)
            """, (
                product_id, product['produto'], delta, movement_date, actor,
                reason, str(uuid4()),
            ))
            results.append({
                'produto_confeitaria_id': product_id,
                'produto': product['produto'],
                'saldo': confirmed_balance,
                'replayed': False,
            })

        conn.commit()
    return results


def _normalize_transfer_lines(lines):
    if not isinstance(lines, (list, tuple)):
        raise ConfeitariaStockError('As linhas de transferência são inválidas.')
    requested = {}
    for line in lines:
        if not isinstance(line, Mapping):
            raise ConfeitariaStockError('Produto de Confeitaria inválido.')
        product_id = confeitaria_stock._validate_product_id(
            line.get('produto_confeitaria_id')
        )
        quantity = line.get('quantidade')
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ConfeitariaStockError(
                'As quantidades têm de ser números inteiros.'
            )
        if quantity <= 0:
            raise ConfeitariaStockError(
                'Cada linha transferida tem de ter uma quantidade superior a zero.'
            )
        if quantity > _MAX_QUANTITY:
            raise ConfeitariaStockError(
                'A quantidade excede o limite permitido.'
            )
        if product_id in requested:
            raise ConfeitariaStockError(
                'O mesmo produto foi indicado mais de uma vez.'
            )
        requested[product_id] = quantity
    if not requested:
        raise ConfeitariaStockError('Indique pelo menos um produto.')
    return requested


def criar_ordens_transferencia_confeitaria(
    *, data, loja_destino, lines, criado_por, data_prevista, request_key,
):
    """Create a complete Confeitaria batch with audited debits in one commit."""
    movement_date = confeitaria_stock._validate_date(data)
    planned_date = confeitaria_stock._validate_date(data_prevista)
    actor = confeitaria_stock._validate_actor(criado_por)
    destination = str(loja_destino or '').strip()
    if not destination or len(destination) > 100:
        raise ConfeitariaStockError('A loja de destino é inválida.')
    request_uuid = confeitaria_stock._validate_idempotency_key(request_key)
    requested = _normalize_transfer_lines(lines)

    canonical_lines = [
        {'produto_confeitaria_id': product_id, 'quantidade': quantity}
        for product_id, quantity in sorted(requested.items())
    ]
    payload = {
        'data_prevista': planned_date.isoformat(),
        'loja_destino': destination,
        'lines': canonical_lines,
    }
    payload_hash = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()
    batch_id = request_uuid
    product_ids = sorted(requested)

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'confeitaria-transfer-request:{request_uuid}',),
        )
        cursor.execute("""
            SELECT payload_hash, batch_id
            FROM confeitaria_stock_transfer_requests
            WHERE request_key = %s
        """, (request_uuid,))
        existing = cursor.fetchone()
        if existing:
            if existing[0].strip() != payload_hash:
                raise ConfeitariaStockError(
                    'Este formulário já foi utilizado com outros dados.'
                )
            cursor.execute("""
                SELECT id
                FROM ordens_transferencia
                WHERE batch_id = %s AND area_origem = 'Confeitaria'
                ORDER BY id
            """, (existing[1],))
            order_ids = [row[0] for row in cursor.fetchall()]
            conn.commit()
            return {'order_ids': order_ids, 'replayed': True}

        cursor.execute("""
            SELECT id
            FROM stores
            WHERE name = %s AND store_type = 'loja' AND is_active = TRUE
            FOR SHARE
        """, (destination,))
        if not cursor.fetchone():
            raise ConfeitariaStockError('Loja de destino inválida ou inativa.')

        cursor.execute("""
            SELECT id, nome, ativo
            FROM produtos_confeitaria
            WHERE id = ANY(%s)
            ORDER BY id
            FOR SHARE
        """, (product_ids,))
        products = {
            int(row[0]): {'produto': row[1], 'ativo': bool(row[2])}
            for row in cursor.fetchall()
        }
        missing = set(product_ids) - set(products)
        if missing:
            raise ConfeitariaStockError(
                'Um ou mais produtos de Confeitaria são inválidos.'
            )

        for product_id in product_ids:
            _lock_product(cursor, product_id)

        for product_id in product_ids:
            product = products[product_id]
            if not product['ativo']:
                raise ConfeitariaStockError(
                    f"O produto {product['produto']} está inativo."
                )
            balance, opening_set = confeitaria_stock._stock_state(
                cursor, product_id
            )
            if not opening_set:
                raise ConfeitariaStockError(
                    f"Confirme primeiro o saldo inicial de "
                    f"{product['produto']}."
                )
            if not confeitaria_stock._cutover_confirmed(cursor, product_id):
                raise ConfeitariaStockError(
                    f"Confirme a contagem atual de {product['produto']} "
                    "depois de rever as transferências legadas."
                )
            requested_quantity = requested[product_id]
            if balance < requested_quantity:
                raise ConfeitariaStockError(
                    f"Saldo insuficiente de {product['produto']}: "
                    f"disponível {balance}, pedido {requested_quantity}. "
                    "Nenhuma ordem foi criada."
                )

        cursor.execute("""
            INSERT INTO confeitaria_stock_transfer_requests (
                request_key, payload_hash, batch_id, criado_por
            )
            VALUES (%s, %s, %s, %s)
        """, (request_uuid, payload_hash, batch_id, actor))

        from db.plano import _insert_evento, _insert_transfer_receipt

        order_ids = []
        for product_id in product_ids:
            product = products[product_id]
            quantity = requested[product_id]
            cursor.execute("""
                INSERT INTO ordens_transferencia (
                    data, area_origem, produto, sabor, quantidade, unidade,
                    loja_destino, criado_por, data_prevista, batch_id,
                    destino_tipo, destino_nome, produto_confeitaria_id,
                    status, confirmado_por, confirmado_em, rececao_estado,
                    loja_origem
                )
                VALUES (
                    %s, 'Confeitaria', %s, NULL, %s, 'und',
                    %s, %s, %s, %s,
                    'loja', NULL, %s,
                    'confirmada', %s, NOW(), 'por_verificar',
                    NULL
                )
                RETURNING id
            """, (
                movement_date, product['produto'], quantity, destination,
                actor, planned_date, batch_id, product_id, actor,
            ))
            order_id = cursor.fetchone()[0]
            order_ids.append(order_id)
            _insert_evento(cursor, order_id, 'criado', actor)
            _insert_evento(cursor, order_id, 'executado', actor)
            cursor.execute("""
                INSERT INTO confeitaria_stock_movements (
                    produto_confeitaria_id, produto, tipo, quantidade, data,
                    responsavel, motivo, idempotency_key,
                    ordem_transferencia_id
                )
                VALUES (
                    %s, %s, 'transferencia_saida', %s, %s, %s, %s, %s, %s
                )
            """, (
                product_id, product['produto'], -quantity, movement_date,
                actor,
                f'Transferência para {destination} (ordem {order_id})',
                str(uuid4()), order_id,
            ))
            _insert_transfer_receipt(
                cursor, order_id, movement_date, 'Confeitaria',
                product['produto'], None, quantity, destination,
            )

        conn.commit()
    return {'order_ids': order_ids, 'replayed': False}