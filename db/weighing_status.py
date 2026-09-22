from datetime import date, datetime
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from db.connection import db_connection


LISBON_TZ = ZoneInfo('Europe/Lisbon')


def portugal_today() -> date:
    return datetime.now(LISBON_TZ).date()


def get_eod_weighing_stores() -> list[dict]:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, name
            FROM stores
            WHERE is_active = TRUE
              AND requires_eod_weighing = TRUE
            ORDER BY name
        """)
        return [dict(row) for row in cursor.fetchall()]


def _empty_status(store: dict, day: date) -> dict:
    return {
        'store_id': store['id'],
        'loja': store['name'],
        'data': day,
        'data_iso': day.isoformat(),
        'data_fmt': day.strftime('%d/%m/%Y'),
        'state': 'missing',
        'label': 'Pesagem em falta',
        'entry_count': 0,
        'updated_at_local': None,
        'reason': None,
        'created_by': None,
        'created_at_local': None,
    }


def get_daily_weighing_statuses(
    days: list[date],
    store_names: list[str] | None = None,
) -> dict[tuple[str, date], dict]:
    """Return one shared operational state per configured store and date."""
    unique_days = sorted(set(days))
    if not unique_days:
        return {}

    stores = get_eod_weighing_stores()
    if store_names is not None:
        allowed = set(store_names)
        stores = [store for store in stores if store['name'] in allowed]
    if not stores:
        return {}

    store_ids = [store['id'] for store in stores]
    statuses = {
        (store['name'], day): _empty_status(store, day)
        for store in stores
        for day in unique_days
    }

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT s.name AS loja, sg.data, COUNT(*) AS entry_count
            FROM stores s
            JOIN stock_gelado sg
              ON sg.store_id = s.id
              OR (
                  sg.store_id IS NULL
                  AND lower(sg.loja) = lower(s.name)
              )
            WHERE s.id = ANY(%s)
              AND sg.tipo = 'fim'
              AND sg.is_active = TRUE
              AND (
                  sg.source_batch_id IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM pesagem_draft_batches confirmed_batch
                      WHERE confirmed_batch.id = sg.source_batch_id
                        AND confirmed_batch.status = 'confirmed'
                  )
              )
              AND sg.data = ANY(%s)
            GROUP BY s.id, s.name, sg.data
        """, (store_ids, unique_days))
        confirmed = cursor.fetchall()

        cursor.execute("""
            SELECT s.name AS loja,
                   COALESCE(sg.data, e.data) AS data,
                   b.id AS batch_id, b.revision, b.status,
                   COUNT(*) AS entry_count,
                   to_char(
                       MAX(b.updated_at) AT TIME ZONE 'Europe/Lisbon',
                       'DD/MM/YYYY HH24:MI'
                   ) AS updated_at_local,
                   json_agg(
                       json_build_object(
                            'sabor', COALESCE(sg.sabor, e.sabor),
                            'quantidade_kg',
                                COALESCE(sg.quantidade_kg, e.quantidade_kg)
                       )
                       ORDER BY e.position
                   ) AS entries
            FROM stores s
            JOIN pesagem_draft_batches b
              ON b.store_id = s.id
              OR (
                  b.store_id IS NULL
                  AND lower(b.loja) = lower(s.name)
              )
            JOIN pesagem_draft_entries e ON e.batch_id = b.id
            LEFT JOIN stock_gelado sg
              ON sg.id = e.stock_id
             AND sg.source_batch_id = b.id
             AND sg.is_active = TRUE
            WHERE s.id = ANY(%s)
              AND b.status IN ('draft', 'registered', 'failed')
              AND COALESCE(sg.data, e.data) = ANY(%s)
            GROUP BY s.id, s.name, COALESCE(sg.data, e.data),
                     b.id, b.revision, b.status
        """, (store_ids, unique_days))
        drafts = cursor.fetchall()

        cursor.execute("""
            SELECT j.store_id, s.name AS loja, j.data, j.reason, j.created_by,
                   to_char(
                       j.created_at AT TIME ZONE 'Europe/Lisbon',
                       'DD/MM/YYYY HH24:MI'
                   ) AS created_at_local
            FROM pesagem_day_justifications j
            JOIN stores s ON s.id = j.store_id
            WHERE j.store_id = ANY(%s)
              AND j.data = ANY(%s)
        """, (store_ids, unique_days))
        justifications = cursor.fetchall()

    for row in drafts:
        key = (row['loja'], row['data'])
        if key in statuses:
            is_registered = row['status'] == 'registered'
            statuses[key].update({
                'state': 'registered' if is_registered else 'draft',
                'label': (
                    'Registada — por confirmar'
                    if is_registered else 'Por confirmar'
                ),
                'entry_count': int(row['entry_count']),
                'updated_at_local': row['updated_at_local'],
                'batch_id': str(row['batch_id']),
                'revision': int(row['revision']),
                'entries': [
                    {
                        'sabor': entry['sabor'],
                        'quantidade_kg': float(entry['quantidade_kg']),
                    }
                    for entry in row['entries']
                ],
            })

    for row in justifications:
        key = (row['loja'], row['data'])
        if key in statuses:
            statuses[key].update({
                'state': 'justified',
                'label': 'Justificada',
                'reason': row['reason'],
                'created_by': row['created_by'],
                'created_at_local': row['created_at_local'],
            })

    for row in confirmed:
        key = (row['loja'], row['data'])
        if key in statuses:
            statuses[key].update({
                'state': 'confirmed',
                'label': 'Confirmada',
                'entry_count': int(row['entry_count']),
            })

    return statuses


def justify_missing_weighing(
    store_id: int,
    day: date,
    reason: str,
    actor_id,
    actor_username: str,
) -> dict:
    clean_reason = (reason or '').strip()
    if len(clean_reason) < 5:
        raise ValueError('Indique uma justificação com pelo menos 5 caracteres.')
    if day >= portugal_today():
        raise ValueError('Só é possível justificar um dia já encerrado.')

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute("""
                SELECT id, name
                FROM stores
                WHERE id = %s
                  AND is_active = TRUE
                  AND requires_eod_weighing = TRUE
                FOR UPDATE
            """, (store_id,))
            store = cursor.fetchone()
            if not store:
                raise ValueError(
                    'A loja não está configurada para pesagem diária.'
                )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f'pesagem-day:{store_id}:{day.isoformat()}',),
            )
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1
                    FROM stock_gelado
                    WHERE (
                        store_id = %s
                        OR (
                            store_id IS NULL
                            AND lower(loja) = lower(%s)
                        )
                    )
                      AND data = %s AND tipo = 'fim'
                      AND is_active = TRUE
                ) OR EXISTS (
                    SELECT 1
                    FROM pesagem_draft_batches b
                    JOIN pesagem_draft_entries e ON e.batch_id = b.id
                    WHERE (
                        b.store_id = %s
                        OR (
                            b.store_id IS NULL
                            AND lower(b.loja) = lower(%s)
                        )
                    )
                      AND e.data = %s
                      AND b.status IN (
                          'draft', 'registering', 'registered',
                          'confirming', 'failed'
                      )
                ) AS has_work
            """, (
                store_id, store['name'], day,
                store_id, store['name'], day,
            ))
            if cursor.fetchone()['has_work']:
                raise ValueError(
                    'Este dia já tem pesagens ou um rascunho por confirmar.'
                )
            cursor.execute("""
                INSERT INTO pesagem_day_justifications (
                    store_id, loja, data, reason,
                    created_by_id, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (store_id, data) DO NOTHING
                RETURNING id, store_id, loja, data, reason,
                          created_by_id, created_by, created_at
            """, (
                store_id,
                store['name'],
                day,
                clean_reason,
                actor_id,
                (actor_username or 'sistema')[:100],
            ))
            row = cursor.fetchone()
            if not row:
                raise ValueError('Este dia já tem uma justificação registada.')
            result = dict(row)
            from db.pastelaria import _write_pesagem_audit
            _write_pesagem_audit(
                cursor,
                'justify',
                store['name'],
                'production_justification',
                actor_id=actor_id,
                actor_username=actor_username,
                store_id=store_id,
                event_date=day,
                reason=clean_reason,
                affected_count=1,
                after_data={'state': 'justified'},
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise