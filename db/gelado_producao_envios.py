"""Atomic persistence for the production confirmation and its Bolhão dispatch."""

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid

from psycopg2.extras import Json

from db.cache import invalidate_prefix
from db.connection import db_connection
from db.plano import criar_ordem_transferencia_cursor


_FIELDS = (
    'pesagem_mat', 'prod_bolhao', 'prod_matosinhos',
    'prod_mouzinho', 'prod_b2b',
)


def _decimal_text(value):
    return format(value.normalize(), 'f') if value else '0'


def _normalise_rows(rows):
    clean_rows = []
    seen = set()
    for row in rows:
        sabor = str(row.get('sabor') or '').strip()
        if not sabor or len(sabor) > 255:
            raise ValueError('Indique um sabor válido.')
        if sabor in seen:
            raise ValueError(f'O sabor {sabor} aparece mais do que uma vez.')
        seen.add(sabor)
        explicit = bool(row.get('pesagem_mat_explicit'))
        clean = {'sabor': sabor, 'pesagem_mat_explicit': explicit}
        for field in _FIELDS:
            try:
                value = Decimal(str(row.get(field, 0)))
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError(f'Quantidade inválida para {sabor}.')
            if not value.is_finite() or value < 0 or value > Decimal('999999.9999'):
                raise ValueError(f'Quantidade inválida para {sabor}: use um valor entre 0 e 999999,9999 kg.')
            if max(0, -value.as_tuple().exponent) > 4:
                raise ValueError(f'A quantidade de {sabor} pode ter no máximo quatro casas decimais.')
            clean[field] = value
        clean_rows.append(clean)
    return sorted(clean_rows, key=lambda row: row['sabor'])


def submission_content_hash(data_prod: date, rows) -> str:
    """Hash only the submitted form content; the registration type comes from session."""
    if not isinstance(data_prod, date):
        raise ValueError('Data de produção inválida.')
    clean_rows = _normalise_rows(rows)
    canonical = {
        'data': data_prod.isoformat(),
        'rows': [
            {
                'sabor': row['sabor'],
                'pesagem_mat_explicit': row['pesagem_mat_explicit'],
                **{field: _decimal_text(row[field]) for field in _FIELDS},
            }
            for row in clean_rows
        ],
    }
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _store_ids(cursor, names, required=()):
    cursor.execute(
        "SELECT id, name FROM stores WHERE name = ANY(%s) AND is_active=TRUE",
        (list(names),),
    )
    found = {}
    for store_id, name in cursor.fetchall():
        found.setdefault(name, []).append(store_id)
    result = {}
    for name in names:
        ids = found.get(name, [])
        if not ids and name not in required:
            result[name] = None
        elif len(ids) != 1:
            raise ValueError(f'A identidade da loja {name} está ausente ou ambígua.')
        else:
            result[name] = ids[0]
    return result


def _upsert_plan(cursor, data_prod, sabor, values):
    cursor.execute("""
        INSERT INTO plano_producao (
            data, sabor, pesagem_matosinhos, producao_estimada_bolhao,
            producao_estimada_matosinhos, producao_estimada_outros,
            producao_estimada_mouzinho
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (data, sabor) DO UPDATE SET
            pesagem_matosinhos=EXCLUDED.pesagem_matosinhos,
            producao_estimada_bolhao=EXCLUDED.producao_estimada_bolhao,
            producao_estimada_matosinhos=EXCLUDED.producao_estimada_matosinhos,
            producao_estimada_outros=EXCLUDED.producao_estimada_outros,
            producao_estimada_mouzinho=EXCLUDED.producao_estimada_mouzinho,
            updated_at=NOW()
    """, (
        data_prod, sabor, values['pesagem_mat'], values['prod_bolhao'],
        values['prod_matosinhos'], values['prod_b2b'], values['prod_mouzinho'],
    ))


def _upsert_matosinhos_start_weighing(
    cursor, data_prod, sabor, quantity, store_id, actor,
):
    from sabor_utils import normalise_sabor
    from db.pastelaria import _pesagem_stock_snapshot, _write_pesagem_audit

    canonical_sabor = normalise_sabor(sabor)
    cursor.execute("""
        SELECT * FROM stock_gelado
        WHERE data=%s AND store_id=%s AND sabor=%s
          AND tipo='inicio' AND is_active=TRUE
        FOR UPDATE
    """, (data_prod, store_id, canonical_sabor))
    before = cursor.fetchone()
    before = _row_as_dict(cursor, before)
    cursor.execute("""
        INSERT INTO stock_gelado
            (data, loja, sabor, quantidade_kg, tipo, store_id)
        VALUES (%s, 'Matosinhos', %s, %s, 'inicio', %s)
        ON CONFLICT (data, store_id, sabor, tipo)
        WHERE is_active=TRUE AND store_id IS NOT NULL
        DO UPDATE SET quantidade_kg=EXCLUDED.quantidade_kg,
                      store_id=EXCLUDED.store_id
        RETURNING *
    """, (data_prod, canonical_sabor, quantity, store_id))
    after = cursor.fetchone()
    after = _row_as_dict(cursor, after)
    _write_pesagem_audit(
        cursor,
        'edit' if before else 'create',
        'Matosinhos',
        'production_start',
        actor_username=actor,
        store_id=store_id,
        event_date=data_prod,
        stock_id=after['id'],
        affected_count=1,
        before_data=_pesagem_stock_snapshot(before),
        after_data=_pesagem_stock_snapshot(after),
    )


def _row_as_dict(cursor, row):
    if row is None:
        return None
    return {
        column[0]: value
        for column, value in zip(cursor.description, row)
    }


def _insert_production(cursor, data_prod, store, store_id, quantity, kind, sabor):
    cursor.execute("""
        INSERT INTO producao
            (data, loja, quantidade_kg, tipo, sabor, store_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (data_prod, store, quantity, kind, sabor, store_id))
    return cursor.fetchone()[0]


def _add_production_stock(cursor, data_prod, sabor, store, store_id, quantity):
    cursor.execute("""
        INSERT INTO stock_producao
            (data, sabor, loja, quantidade_kg, store_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (data, sabor, loja) DO UPDATE SET
            quantidade_kg=stock_producao.quantidade_kg + EXCLUDED.quantidade_kg,
            updated_at=NOW(),
            store_id=COALESCE(stock_producao.store_id, EXCLUDED.store_id)
    """, (data_prod, sabor, store, quantity, store_id))


def guardar_submissao_producao(
    submission_id,
    data_prod: date,
    rows,
    tipo_registo: str,
    actor: str,
):
    """Save the complete form once, including its automatic Bolhão movements."""
    try:
        token = str(uuid.UUID(str(submission_id)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Identidade de submissão inválida. Reabra o formulário de confirmação.')
    if tipo_registo not in ('manual', 'ocr'):
        raise ValueError('Tipo de registo de produção inválido.')
    clean_rows = _normalise_rows(rows)
    content_hash = submission_content_hash(data_prod, clean_rows)
    actor = (actor or 'system')[:100]

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (token,),
        )
        cursor.execute("""
            SELECT content_hash, resultado
            FROM producao_submissoes
            WHERE submission_id=%s
            FOR UPDATE
        """, (token,))
        previous = cursor.fetchone()
        if previous:
            if previous[0].strip() != content_hash:
                raise ValueError(
                    'Este formulário já foi guardado com outros valores. '
                    'Reabra a confirmação para registar uma alteração como novo lote.'
                )
            if previous[1] is None:
                raise RuntimeError('A submissão anterior não tem um resultado persistido.')
            conn.commit()
            return {**previous[1], 'replayed': True}

        cursor.execute("""
            INSERT INTO producao_submissoes
                (submission_id, content_hash, autor, data)
            VALUES (%s, %s, %s, %s)
        """, (token, content_hash, actor, data_prod))

        stores_needed = set()
        stores_required = set()
        for row in clean_rows:
            values = {field: row[field] for field in _FIELDS}
            if values['prod_bolhao'] > 0:
                stores_needed.update(('Bolhão', 'Matosinhos'))
                stores_required.update(('Bolhão', 'Matosinhos'))
            if values['prod_matosinhos'] > 0 or row['pesagem_mat_explicit']:
                stores_needed.add('Matosinhos')
                stores_required.add('Matosinhos')
            if values['prod_mouzinho'] > 0:
                stores_needed.add('Mouzinho')
            if values['prod_b2b'] > 0:
                stores_needed.add('B2B')
        stores = _store_ids(cursor, stores_needed, stores_required)
        saved = 0
        automatic_orders = []
        total_bolhao = Decimal('0')
        for row in clean_rows:
            sabor = row['sabor']
            values = {field: row[field] for field in _FIELDS}
            has_changes = any(values[field] != 0 for field in _FIELDS)
            if not has_changes and not row['pesagem_mat_explicit']:
                continue

            _upsert_plan(cursor, data_prod, sabor, values)
            if row['pesagem_mat_explicit']:
                _upsert_matosinhos_start_weighing(
                    cursor, data_prod, sabor, values['pesagem_mat'],
                    stores['Matosinhos'], actor,
                )

            for store, field in (
                ('Bolhão', 'prod_bolhao'),
                ('Matosinhos', 'prod_matosinhos'),
                ('Mouzinho', 'prod_mouzinho'),
                ('B2B', 'prod_b2b'),
            ):
                quantity = values[field]
                if quantity <= 0:
                    continue
                production_id = _insert_production(
                    cursor, data_prod, store, stores[store],
                    quantity, tipo_registo, sabor,
                )
                if store == 'Bolhão':
                    order_id = criar_ordem_transferencia_cursor(
                        cursor,
                        data_prod,
                        'Gelado',
                        sabor,
                        quantity,
                        'kg',
                        'Bolhão',
                        sabor=sabor,
                        criado_por=actor,
                        data_prevista=data_prod,
                        batch_id=token,
                        loja_origem='Matosinhos',
                        origem_registo='producao_dia',
                        producao_origem_id=production_id,
                    )
                    automatic_orders.append({
                        'order_id': order_id,
                        'production_id': production_id,
                        'sabor': sabor,
                        'quantidade_kg': _decimal_text(quantity),
                    })
                    total_bolhao += quantity
                else:
                    _add_production_stock(
                        cursor, data_prod, sabor, store, stores[store], quantity,
                    )
            saved += 1

        result = {
            'submission_id': token,
            'data': data_prod.isoformat(),
            'saved': saved,
            'automatic_orders': automatic_orders,
            'automatic_kg': _decimal_text(total_bolhao),
        }
        cursor.execute("""
            UPDATE producao_submissoes
            SET resultado=%s
            WHERE submission_id=%s
        """, (Json(result), token))
        conn.commit()

    invalidate_prefix('kpi_annual')
    invalidate_prefix('kpi_monthly')
    invalidate_prefix('kpi_by_day')
    return {**result, 'replayed': False}