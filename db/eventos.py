import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
import json
import hashlib
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


EVENT_STATUS_ORDER = (
    'novos',
    'orcamentado',
    'enviado',
    'adjudicado',
    'rejeitado',
    'sinalizado',
    'realizado',
    'faturado',
    'recebido',
    'cancelado',
)

_LEGACY_EVENT_STATUS_MAP = {
    'lead': 'novos',
    'contacted': 'orcamentado',
    'negotiating': 'orcamentado',
    'proposal_sent': 'enviado',
    'won': 'adjudicado',
    'lost': 'rejeitado',
    'cancelled': 'cancelado',
}

VALID_EVENT_TRANSITIONS = {
    'novos': ('orcamentado', 'rejeitado', 'cancelado'),
    'orcamentado': ('enviado', 'rejeitado', 'cancelado'),
    'enviado': ('orcamentado', 'adjudicado', 'rejeitado', 'cancelado'),
    'adjudicado': ('sinalizado', 'cancelado'),
    'sinalizado': ('realizado', 'cancelado'),
    'realizado': ('faturado', 'cancelado'),
    'faturado': ('recebido', 'cancelado'),
    'rejeitado': (),
    'recebido': (),
    'cancelado': (),
}

EVENT_FINANCIAL_STATUSES = (
    'adjudicado',
    'sinalizado',
    'realizado',
    'faturado',
    'recebido',
)

_SHEET_PROTECTED_STATUSES = frozenset((
    'adjudicado', 'sinalizado', 'realizado', 'faturado', 'recebido', 'rejeitado', 'cancelado',
))

EVENT_SERVICE_MODES = (
    'pending',
    'niva_serves',
    'client_serves',
    'delivery_only',
    'catering',
)

_PORTAL_BRAND_DEFAULTS = {
    'brand_name': 'Scoopy',
    'logo_filename': None,
    'primary_color': '#167C70',
    'accent_color': '#35A394',
    'background_color': '#FFF8F2',
    'text_color': '#173B38',
    'button_color': '#167C70',
    'button_text_color': '#FFFFFF',
    'form_title': 'Peça o seu evento',
    'form_intro': (
        'Conte-nos o que imagina. A equipa confirma disponibilidade, logística e orçamento.'
    ),
    'confirmation_message': (
        'Recebemos o seu pedido. A equipa irá confirmar disponibilidade e logística.'
    ),
    'contact_text': 'Deixe-nos os seus contactos para podermos responder ao pedido.',
    'field_labels': {},
    'visible_fields': {
        'event_name': True, 'duration': True, 'service_mode': True,
        'resource_preferences': True, 'referral_source': True,
        'marketing_consent': True,
    },
    'is_default': False,
}


def normalize_event_status(status: str | None) -> str:
    """Map legacy CRM statuses to the agreed v2 pipeline."""
    value = (status or 'novos').strip().lower()
    return _LEGACY_EVENT_STATUS_MAP.get(value, value)


def _money(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError):
        raise ValueError('Valor monetário inválido.')


def _rate(value) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Taxa de IVA inválida.')
    if parsed < 0 or parsed > 1:
        raise ValueError('A taxa de IVA deve estar entre 0 e 1.')
    return parsed


def calculate_quote_line(quantity, unit_price_gross, taxa_iva):
    """Return immutable gross/net/VAT snapshots for one customer quote line."""
    try:
        qty = Decimal(str(quantity))
    except (InvalidOperation, ValueError):
        raise ValueError('Quantidade inválida.')
    if qty < 0:
        raise ValueError('A quantidade não pode ser negativa.')
    unit_gross = _money(unit_price_gross)
    rate = _rate(taxa_iva)
    gross = (qty * unit_gross).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if rate is None:
        return {
            'unit_price_gross': unit_gross,
            'total_net': None,
            'total_vat': None,
            'total_gross': gross,
        }
    net = (gross / (Decimal('1') + rate)).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    return {
        'unit_price_gross': unit_gross,
        'total_net': net,
        'total_vat': gross - net,
        'total_gross': gross,
    }


def get_default_quote_taxa_iva(artigo_codigo=None):
    """Read the current editable IVA setting for a new quote line."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(
                (SELECT taxa_iva FROM artigos_evento WHERE codigo = %s AND ativo = TRUE),
                (SELECT taxa_iva FROM event_pricing_settings
                 WHERE key = CASE WHEN COALESCE(%s, '') LIKE 'gelado%%'
                                  THEN 'gelado_kg' ELSE 'servico_fixo' END
                   AND active = TRUE)
            )
            """,
            (artigo_codigo, artigo_codigo),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def _insert_event_history(cursor, event_id, event_type, *, old_status=None,
                          new_status=None, actor=None, reason=None, details=None):
    """Write the only supported event-history mutation: an append-only insert."""
    cursor.execute(
        """
        INSERT INTO event_history
            (event_id, event_type, old_status, new_status, actor, reason, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            event_id, event_type, old_status, new_status, actor, reason,
            json.dumps(details or {}, ensure_ascii=False, default=str),
        ),
    )

def get_artigos_evento(apenas_ativos=True):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        q = "SELECT * FROM artigos_evento"
        if apenas_ativos:
            q += " WHERE ativo = TRUE"
        q += " ORDER BY id"
        cursor.execute(q)
        return cursor.fetchall()

def get_artigo_evento(artigo_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM artigos_evento WHERE id = %s", (artigo_id,))
        return cursor.fetchone()

def upsert_artigo_evento(codigo, nome, unidade, preco_base, ativo=True, artigo_id=None, cost_tier='medium'):
    with db_connection() as conn:
        cursor = conn.cursor()
        if artigo_id:
            cursor.execute("""
                UPDATE artigos_evento SET codigo=%s, nome=%s, unidade=%s, preco_base=%s, ativo=%s, cost_tier=%s
                WHERE id=%s
            """, (codigo, nome, unidade, preco_base, ativo, cost_tier, artigo_id))
        else:
            cursor.execute("""
                INSERT INTO artigos_evento (codigo, nome, unidade, preco_base, ativo, cost_tier)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (codigo) DO UPDATE SET nome=EXCLUDED.nome, unidade=EXCLUDED.unidade,
                    preco_base=EXCLUDED.preco_base, ativo=EXCLUDED.ativo, cost_tier=EXCLUDED.cost_tier
            """, (codigo, nome, unidade, preco_base, ativo, cost_tier))
        conn.commit()

def toggle_artigo_evento(artigo_id, ativo):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE artigos_evento SET ativo=%s WHERE id=%s", (ativo, artigo_id))
        conn.commit()


# ── V2 foundation: pricing, occurrences, resources and audit ───────────────────

def get_event_pricing_settings(active_only=False):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where = " WHERE active = TRUE" if active_only else ""
        cursor.execute(
            f"SELECT * FROM event_pricing_settings{where} ORDER BY key"
        )
        return cursor.fetchall()


def upsert_event_pricing_setting(key, label, setting_type, value_gross,
                                 taxa_iva=None, actor=None,
                                 requires_tax_review=True, active=True):
    """Store editable pricing inputs; customer quote lines snapshot their result."""
    if setting_type not in ('money', 'percentage'):
        raise ValueError('Tipo de configuração inválido.')
    gross = _money(value_gross)
    rate = _rate(taxa_iva)
    if setting_type == 'percentage' and gross > 100:
        raise ValueError('Uma percentagem não pode exceder 100%.')
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO event_pricing_settings
                (key, label, setting_type, value_gross, taxa_iva,
                 requires_tax_review, active, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
                label = EXCLUDED.label,
                setting_type = EXCLUDED.setting_type,
                value_gross = EXCLUDED.value_gross,
                taxa_iva = EXCLUDED.taxa_iva,
                requires_tax_review = EXCLUDED.requires_tax_review,
                active = EXCLUDED.active,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            """,
            (key, label, setting_type, gross, rate, requires_tax_review, active, actor),
        )
        conn.commit()


def get_event_quote_totals(event_id):
    """Return customer-facing base, IVA and gross totals without inventing IVA."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT
                COALESCE(SUM(COALESCE(total_gross, total)), 0) AS total_gross,
                SUM(total_net) AS total_net,
                SUM(total_vat) AS total_vat,
                COUNT(*) FILTER (WHERE taxa_iva IS NULL OR total_net IS NULL) AS unknown_tax_lines
            FROM quote_items
            WHERE event_id = %s
            """,
            (event_id,),
        )
        row = cursor.fetchone() or {}
        return {
            'total_gross': _money(row.get('total_gross')),
            'total_net': _money(row['total_net']) if row.get('total_net') is not None else None,
            'total_vat': _money(row['total_vat']) if row.get('total_vat') is not None else None,
            'unknown_tax_lines': int(row.get('unknown_tax_lines') or 0),
        }


def get_event_occurrences(event_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT eo.*,
                   COALESCE(
                       json_agg(
                           json_build_object(
                               'reservation_id', err.id,
                               'resource_id', er.id,
                               'resource_code', er.code,
                               'resource_name', er.name,
                               'status', err.status,
                               'risk_acknowledged', err.risk_acknowledged
                           ) ORDER BY err.id
                       ) FILTER (WHERE err.id IS NOT NULL),
                       '[]'::json
                   ) AS reservations
            FROM event_occurrences eo
            LEFT JOIN event_resource_reservations err ON err.occurrence_id = eo.id
            LEFT JOIN event_resources er ON er.id = err.resource_id
            WHERE eo.event_id = %s
            GROUP BY eo.id
            ORDER BY eo.occurrence_number, eo.event_date, eo.id
            """,
            (event_id,),
        )
        return cursor.fetchall()


def add_event_occurrence(event_id, data, actor=None):
    """Add one dated/location occurrence and record the change in event history."""
    service_mode = data.get('service_mode') or 'pending'
    if service_mode not in EVENT_SERVICE_MODES:
        raise ValueError('Modo de serviço inválido.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT COALESCE(MAX(occurrence_number), 0) + 1 AS next_number "
            "FROM event_occurrences WHERE event_id = %s",
            (event_id,),
        )
        next_number = cursor.fetchone()['next_number']
        cursor.execute(
            """
            INSERT INTO event_occurrences (
                event_id, occurrence_number, event_date, venue, venue_address,
                latitude, longitude, estimated_km, service_start_time,
                service_end_time, expected_duration_minutes, logistics_notes, service_mode
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                COALESCE(%s, 'pending')
            ) RETURNING id
            """,
            (
                event_id, next_number, data.get('event_date'), data.get('venue'),
                data.get('venue_address'), data.get('latitude'), data.get('longitude'),
                data.get('estimated_km'), data.get('service_start_time'),
                data.get('service_end_time'), data.get('expected_duration_minutes'),
                data.get('logistics_notes'), service_mode,
            ),
        )
        occurrence_id = cursor.fetchone()['id']
        _sync_event_schedule_from_occurrences(cursor, event_id)
        _insert_event_history(
            cursor, event_id, 'occurrence_added', actor=actor,
            details={'occurrence_id': occurrence_id, 'occurrence_number': next_number},
        )
        conn.commit()
        return occurrence_id


def update_event_occurrence(occurrence_id, data, actor=None, risk_acknowledged=False,
                            expected_event_id=None):
    if data.get('service_mode') and data['service_mode'] not in EVENT_SERVICE_MODES:
        raise ValueError('Modo de serviço inválido.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT * FROM event_occurrences WHERE id = %s AND (%s IS NULL OR event_id = %s)",
            (occurrence_id, expected_event_id, expected_event_id),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError('Ocorrência não encontrada.')
        def merged(field):
            return data[field] if field in data else row.get(field)
        event_date = merged('event_date')
        service_start_time = merged('service_start_time')
        service_end_time = merged('service_end_time')
        try:
            conflicts = _validate_occurrence_reservation_change(
                cursor, occurrence_id, event_date, service_start_time, service_end_time,
                risk_acknowledged=risk_acknowledged,
            )
        except Exception:
            conn.rollback()
            raise
        cursor.execute(
            """
            UPDATE event_occurrences SET
                event_date = %s, venue = %s, venue_address = %s,
                latitude = %s, longitude = %s, estimated_km = %s,
                service_start_time = %s, service_end_time = %s,
                expected_duration_minutes = %s, logistics_notes = %s,
                service_mode = COALESCE(%s, service_mode), updated_at = NOW()
            WHERE id = %s
            """,
            (
                event_date, merged('venue'), merged('venue_address'),
                merged('latitude'), merged('longitude'), merged('estimated_km'),
                service_start_time, service_end_time,
                merged('expected_duration_minutes'), merged('logistics_notes'),
                merged('service_mode'), occurrence_id,
            ),
        )
        _sync_event_schedule_from_occurrences(cursor, row['event_id'])
        _insert_event_history(
            cursor, row['event_id'], 'occurrence_updated', actor=actor,
            details={
                'occurrence_id': occurrence_id,
                'risk_acknowledged': risk_acknowledged,
                'conflict_count': len(conflicts),
            },
        )
        conn.commit()


def delete_event_occurrence(occurrence_id, actor=None, expected_event_id=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT event_id, occurrence_number FROM event_occurrences "
            "WHERE id=%s AND (%s IS NULL OR event_id=%s) FOR UPDATE",
            (occurrence_id, expected_event_id, expected_event_id),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError('Ocorrência não encontrada.')
        cursor.execute("SELECT COUNT(*) AS count FROM event_occurrences WHERE event_id=%s", (row['event_id'],))
        if cursor.fetchone()['count'] <= 1:
            raise ValueError('Um evento deve manter pelo menos uma ocorrência.')
        cursor.execute("DELETE FROM event_occurrences WHERE id=%s", (occurrence_id,))
        _sync_event_schedule_from_occurrences(cursor, row['event_id'])
        _insert_event_history(
            cursor, row['event_id'], 'occurrence_deleted', actor=actor,
            details={'occurrence_id': occurrence_id, 'occurrence_number': row['occurrence_number']},
        )
        conn.commit()


def _sync_event_schedule_from_occurrences(cursor, event_id):
    """Keep legacy event columns aligned with the next operational occurrence."""
    cursor.execute("""
        SELECT event_date, venue, venue_address, service_start_time, service_end_time
        FROM event_occurrences
        WHERE event_id=%s
        ORDER BY event_date NULLS LAST, service_start_time NULLS LAST, occurrence_number, id
        LIMIT 1
    """, (event_id,))
    primary = cursor.fetchone()
    if not primary:
        return
    def value(name):
        return primary[name] if hasattr(primary, 'get') else None
    start = value('service_start_time')
    end = value('service_end_time')
    cursor.execute("""
        UPDATE events SET event_date=%s, event_time=%s, event_end_time=%s,
            venue=%s, venue_address=%s, updated_at=NOW()
        WHERE id=%s
    """, (
        value('event_date'),
        start.strftime('%H:%M') if start else '',
        end.strftime('%H:%M') if end else '',
        value('venue'), value('venue_address'), event_id,
    ))


def get_event_resources(active_only=True):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where = " WHERE active = TRUE" if active_only else ""
        cursor.execute(f"SELECT * FROM event_resources{where} ORDER BY name")
        return cursor.fetchall()


def upsert_event_resource(code, name, resource_type='equipment',
                          capacity_carapinas=None, capacity_flavors=None,
                          active=True, notes=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO event_resources
                (code, name, resource_type, capacity_carapinas, capacity_flavors, active, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                resource_type = EXCLUDED.resource_type,
                capacity_carapinas = EXCLUDED.capacity_carapinas,
                capacity_flavors = EXCLUDED.capacity_flavors,
                active = EXCLUDED.active,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            """,
            (code, name, resource_type, capacity_carapinas, capacity_flavors, active, notes),
        )
        conn.commit()


def _get_resource_conflicts(cursor, resource_id, event_date, service_start_time=None,
                            service_end_time=None, exclude_occurrence_id=None):
    """Run an overlap check using the caller's transaction."""
    if not event_date:
        return []
    start = service_start_time or '00:00:00'
    end = service_end_time or '23:59:59'
    cursor.execute(
        """
        SELECT err.id AS reservation_id, err.status AS reservation_status,
               eo.id AS occurrence_id, eo.event_id, eo.event_date,
               eo.service_start_time, eo.service_end_time,
               e.event_name, e.status AS event_status
        FROM event_resource_reservations err
        JOIN event_occurrences eo ON eo.id = err.occurrence_id
        JOIN events e ON e.id = eo.event_id
        WHERE err.resource_id = %s
          AND err.status IN ('requested', 'reserved')
          AND e.status NOT IN ('rejeitado', 'cancelado')
          AND eo.event_date = %s
          AND (%s IS NULL OR eo.id != %s)
          AND (
              eo.service_start_time IS NULL OR eo.service_end_time IS NULL
              OR (eo.service_start_time < %s::time AND eo.service_end_time > %s::time)
          )
        ORDER BY eo.event_date, eo.service_start_time NULLS FIRST, err.id
        """,
        (resource_id, event_date, exclude_occurrence_id, exclude_occurrence_id, end, start),
    )
    return cursor.fetchall()


def _row_value(row, key, index=0):
    return row.get(key) if hasattr(row, 'get') else row[index]


def _validate_occurrence_reservation_change(cursor, occurrence_id, event_date,
                                            service_start_time=None,
                                            service_end_time=None,
                                            *, risk_acknowledged=False):
    """Lock and validate active resources before moving a reserved occurrence."""
    cursor.execute(
        """
        SELECT resource_id
        FROM event_resource_reservations
        WHERE occurrence_id = %s AND status IN ('requested', 'reserved')
        ORDER BY resource_id
        """,
        (occurrence_id,),
    )
    reservations = cursor.fetchall()
    resource_ids = [_row_value(row, 'resource_id') for row in reservations]
    if resource_ids and not event_date:
        raise ValueError('Não é possível remover a data de uma ocorrência com recursos reservados.')

    conflicts = []
    for resource_id in resource_ids:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f'event-resource:{resource_id}:{event_date}',),
        )
        conflicts.extend(_get_resource_conflicts(
            cursor, resource_id, event_date, service_start_time,
            service_end_time, occurrence_id,
        ))
    if conflicts and not risk_acknowledged:
        raise ValueError(
            'A nova data/horário entra em conflito com outro recurso reservado. '
            'Confirme explicitamente o risco para guardar.'
        )
    return conflicts


def get_resource_conflicts(resource_id, event_date, service_start_time=None,
                           service_end_time=None, exclude_occurrence_id=None):
    """Return overlapping active reservations; missing times conservatively conflict."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        return _get_resource_conflicts(
            cursor, resource_id, event_date, service_start_time,
            service_end_time, exclude_occurrence_id,
        )


def reserve_event_resource(occurrence_id, resource_id, *, actor=None,
                            risk_acknowledged=False, notes=None, expected_event_id=None):
    """Record a requested reservation, refusing unacknowledged resource conflicts."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT event_id, event_date, service_start_time, service_end_time "
            "FROM event_occurrences WHERE id = %s AND (%s IS NULL OR event_id = %s)",
            (occurrence_id, expected_event_id, expected_event_id),
        )
        occurrence = cursor.fetchone()
        if not occurrence:
            raise ValueError('Ocorrência não encontrada.')
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f'event-resource:{resource_id}:{occurrence["event_date"]}',),
        )
        cursor.execute(
            """
            SELECT err.id AS reservation_id, eo.event_id, eo.event_date
            FROM event_resource_reservations err
            JOIN event_occurrences eo ON eo.id = err.occurrence_id
            WHERE err.occurrence_id = %s AND err.resource_id = %s
            """,
            (occurrence_id, resource_id),
        )
        existing = cursor.fetchone()
        conflicts = _get_resource_conflicts(
            cursor, resource_id, occurrence['event_date'],
            occurrence['service_start_time'], occurrence['service_end_time'],
            occurrence_id,
        )
        if conflicts and not risk_acknowledged:
            conn.rollback()
            return False, conflicts
        cursor.execute(
            """
            INSERT INTO event_resource_reservations
                (occurrence_id, resource_id, status, risk_acknowledged, notes, reserved_by)
            VALUES (%s, %s, 'requested', %s, %s, %s)
            ON CONFLICT (occurrence_id, resource_id) DO UPDATE SET
                risk_acknowledged = EXCLUDED.risk_acknowledged,
                notes = EXCLUDED.notes,
                reserved_by = EXCLUDED.reserved_by,
                updated_at = NOW()
            """,
            (occurrence_id, resource_id, risk_acknowledged, notes, actor),
        )
        _insert_event_history(
            cursor, occurrence['event_id'], 'resource_requested', actor=actor,
            details={
                'occurrence_id': occurrence_id,
                'resource_id': resource_id,
                'risk_acknowledged': risk_acknowledged,
                'conflict_count': len(conflicts),
                'replaced_reservation_id': existing['reservation_id'] if existing else None,
            },
        )
        conn.commit()
        return True, conflicts


def release_event_resource(occurrence_id, resource_id, *, actor=None, expected_event_id=None):
    """Release a resource without erasing the original reservation audit."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT event_id FROM event_occurrences WHERE id = %s AND (%s IS NULL OR event_id = %s)",
            (occurrence_id, expected_event_id, expected_event_id),
        )
        occurrence = cursor.fetchone()
        if not occurrence:
            raise ValueError('Ocorrência não encontrada.')
        cursor.execute(
            """
            UPDATE event_resource_reservations
            SET status = 'released', updated_at = NOW()
            WHERE occurrence_id = %s AND resource_id = %s
              AND status IN ('requested', 'reserved')
            """,
            (occurrence_id, resource_id),
        )
        if not cursor.rowcount:
            raise ValueError('Reserva ativa não encontrada.')
        _insert_event_history(
            cursor, occurrence['event_id'], 'resource_released', actor=actor,
            details={'occurrence_id': occurrence_id, 'resource_id': resource_id},
        )
        conn.commit()


def get_event_history(event_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT * FROM event_history
            WHERE event_id = %s
            ORDER BY created_at DESC, id DESC
            """,
            (event_id,),
        )
        return cursor.fetchall()


def validate_event_deposit(event_id, amount, *, proof_reference, actor=None, received_at=None):
    """Validate a non-refundable deposit and atomically reserve the event."""
    amount = _money(amount)
    if amount <= 0:
        raise ValueError('O valor validado do sinal deve ser superior a zero.')
    proof_reference = (proof_reference or '').strip()
    if not proof_reference:
        raise ValueError('Indique a referência do comprovativo validado.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT status, deposit_amount_eur, invoice_amount_eur
            FROM events WHERE id = %s
            """,
            (event_id,),
        )
        event = cursor.fetchone()
        if not event:
            raise ValueError('Evento não encontrado.')
        if normalize_event_status(event['status']) != 'adjudicado':
            raise ValueError('O sinal só pode ser validado após a adjudicação.')
        expected = _money(event.get('deposit_amount_eur'))
        invoice_total = _money(event.get('invoice_amount_eur'))
        if invoice_total and amount > invoice_total:
            raise ValueError('O sinal não pode exceder o valor total do evento.')
        if expected and amount < expected:
            raise ValueError(
                f'O sinal validado é inferior ao sinal obrigatório ({expected:.2f} €).'
            )
        cursor.execute(
            """
            UPDATE events SET
                deposit_amount_eur = %s,
                deposit_received_at = COALESCE(%s, NOW()),
                deposit_validated_at = NOW(),
                deposit_verified_by = %s,
                deposit_proof_reference = %s,
                deposit_non_refundable = TRUE,
                payment_amount_eur = %s,
                payment_status = CASE WHEN %s >= invoice_amount_eur THEN 'received' ELSE 'partial' END,
                status = 'sinalizado',
                status_changed_at = NOW(),
                reserved_at = COALESCE(reserved_at, NOW()),
                updated_at = NOW()
            WHERE id = %s
            """,
            (amount, received_at, actor, proof_reference, amount, amount, event_id),
        )
        _insert_event_history(
            cursor, event_id, 'deposit_validated', actor=actor,
            details={
                'deposit_amount_eur': amount,
                'received_total_eur': amount,
                'proof_reference': proof_reference,
                'non_refundable': True,
            },
        )
        cursor.execute(
            """
            UPDATE event_resource_reservations err
            SET status='reserved', reserved_by=COALESCE(%s, err.reserved_by), updated_at=NOW()
            FROM event_occurrences eo
            WHERE err.occurrence_id=eo.id AND eo.event_id=%s AND err.status='requested'
            """,
            (actor, event_id),
        )
        _sync_event_production_requirements(cursor, event_id)
        _insert_event_history(
            cursor, event_id, 'status_changed', old_status='adjudicado',
            new_status='sinalizado', actor=actor,
            details={'reason': 'deposit_validated'},
        )
        conn.commit()


def reject_event_deposit(event_id, reason, *, actor=None):
    """Keep an auditable rejection without losing the customer's uploaded proof."""
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('Indique o motivo da rejeição do comprovativo.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT status FROM events WHERE id=%s FOR UPDATE", (event_id,))
        event = cursor.fetchone()
        if not event or normalize_event_status(event['status']) != 'adjudicado':
            raise ValueError('Só pode rejeitar comprovativos de eventos adjudicados.')
        _insert_event_history(
            cursor, event_id, 'deposit_rejected', actor=actor, reason=reason,
            details={'non_refundable_rule': True},
        )
        conn.commit()


def _sync_event_production_requirements(cursor, event_id):
    """Persist portal flavour demand per occurrence; safe to call repeatedly."""
    cursor.execute("""
        SELECT e.estimated_guests, p.servings_per_guest, p.flavours
        FROM events e LEFT JOIN event_portal_requests p ON p.event_id=e.id
        WHERE e.id=%s
    """, (event_id,))
    request_data = cursor.fetchone()
    if not request_data or not request_data.get('flavours') or not request_data.get('estimated_guests'):
        return
    plan = calculate_portal_flavours(
        request_data['estimated_guests'], request_data.get('servings_per_guest') or 1,
        request_data['flavours'],
    )
    cursor.execute(
        "SELECT id FROM event_occurrences WHERE event_id=%s ORDER BY occurrence_number, id",
        (event_id,),
    )
    occurrences = cursor.fetchall()
    for occurrence in occurrences:
        occurrence_id = occurrence['id']
        for flavour in plan['flavours']:
            cursor.execute("""
                INSERT INTO event_production_requirements
                    (event_id, occurrence_id, sabor, required_kg)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (occurrence_id, sabor) DO UPDATE SET
                    required_kg=EXCLUDED.required_kg, updated_at=NOW()
            """, (event_id, occurrence_id, flavour['name'], flavour['kg']))


def get_event_production_requirements(event_date=None):
    """Expose confirmed per-occurrence gelato needs to the production plan."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        clauses, params = ["e.status IN ('sinalizado','realizado','faturado','recebido')"], []
        if event_date:
            clauses.append("eo.event_date=%s")
            params.append(event_date)
        cursor.execute(f"""
            SELECT r.*, eo.event_date, eo.venue, e.event_name, e.client_name
            FROM event_production_requirements r
            JOIN event_occurrences eo ON eo.id=r.occurrence_id
            JOIN events e ON e.id=r.event_id
            WHERE {' AND '.join(clauses)}
            ORDER BY eo.event_date, e.event_name, r.sabor
        """, params)
        return cursor.fetchall()


def mark_event_invoiced(event_id, invoice_reference, *, actor=None):
    """Link the commercial invoice and move a completed event to invoiced."""
    invoice_reference = (invoice_reference or '').strip()
    if not invoice_reference:
        raise ValueError('Indique a referência ou número da fatura.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT status FROM events WHERE id=%s FOR UPDATE", (event_id,))
        event = cursor.fetchone()
        if not event or normalize_event_status(event['status']) != 'realizado':
            raise ValueError('A fatura só pode ser enviada depois de o evento estar realizado.')
        cursor.execute("""
            UPDATE events SET status='faturado', invoice_reference=%s, invoice_sent_at=NOW(),
                status_changed_at=NOW(), updated_at=NOW() WHERE id=%s
        """, (invoice_reference, event_id))
        _insert_event_history(
            cursor, event_id, 'invoice_sent', old_status='realizado', new_status='faturado',
            actor=actor, details={'invoice_reference': invoice_reference},
        )
        conn.commit()


def record_event_receipt(event_id, amount, received_at, *, payment_method, payment_reference=None, actor=None):
    """Record a confirmed customer receipt; only a fully settled event is received."""
    amount = _money(amount)
    if amount <= 0:
        raise ValueError('O valor recebido deve ser superior a zero.')
    if not payment_method:
        raise ValueError('Indique o método de pagamento.')
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT status, invoice_amount_eur, payment_amount_eur FROM events WHERE id=%s FOR UPDATE",
            (event_id,),
        )
        event = cursor.fetchone()
        if not event or normalize_event_status(event['status']) != 'faturado':
            raise ValueError('O recebimento só pode ser confirmado após o envio da fatura.')
        total_received = _money(event.get('payment_amount_eur')) + amount
        invoice_total = _money(event.get('invoice_amount_eur'))
        remaining = invoice_total - _money(event.get('payment_amount_eur'))
        if remaining <= 0:
            raise ValueError('O evento já não tem saldo em aberto.')
        if amount > remaining:
            raise ValueError(f'O recebimento não pode exceder o saldo em aberto ({remaining:.2f} €).')
        paid_in_full = total_received >= invoice_total
        cursor.execute("""
            UPDATE events SET payment_amount_eur=%s, payment_received_at=%s,
                payment_method=%s, payment_reference=%s, payment_received_by=%s,
                payment_status=%s, status=CASE WHEN %s THEN 'recebido' ELSE status END,
                status_changed_at=CASE WHEN %s THEN NOW() ELSE status_changed_at END,
                updated_at=NOW()
            WHERE id=%s
        """, (total_received, received_at, payment_method, payment_reference or None, actor,
              'received' if paid_in_full else 'partial', paid_in_full, paid_in_full, event_id))
        _insert_event_history(
            cursor, event_id, 'payment_received', old_status=event['status'],
            new_status='recebido' if paid_in_full else event['status'], actor=actor,
            details={'amount_eur': amount, 'total_received_eur': total_received,
                     'payment_method': payment_method, 'reference': payment_reference,
                     'paid_in_full': paid_in_full},
        )
        conn.commit()
        return paid_in_full


# ── lead_requests ──────────────────────────────────────────────────────────────

def get_leads(status=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if status:
            cursor.execute("SELECT * FROM lead_requests WHERE status=%s ORDER BY submitted_at DESC NULLS LAST, created_at DESC", (status,))
        else:
            cursor.execute("SELECT * FROM lead_requests ORDER BY submitted_at DESC NULLS LAST, created_at DESC")
        return cursor.fetchall()

def get_lead(lead_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM lead_requests WHERE id=%s", (lead_id,))
        return cursor.fetchone()

def create_lead(data: dict):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO lead_requests (
                submitted_at, source, google_sheet_row_id, event_type, event_date, event_time,
                event_end_time, estimated_guests_raw, estimated_guests, venue, venue_address,
                client_name, client_email, client_phone, marketing_consent,
                referral_source, notes, internal_notes, status
            ) VALUES (
                %(submitted_at)s, %(source)s, %(google_sheet_row_id)s, %(event_type)s,
                %(event_date)s, %(event_time)s, %(event_end_time)s, %(estimated_guests_raw)s, %(estimated_guests)s,
                %(venue)s, %(venue_address)s, %(client_name)s, %(client_email)s, %(client_phone)s,
                %(marketing_consent)s, %(referral_source)s, %(notes)s, %(internal_notes)s, %(status)s
            ) RETURNING id
        """, data)
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None

def update_lead(lead_id, data: dict):
    data['updated_at'] = datetime.now()
    data['id'] = lead_id
    data.setdefault('loss_reason', None)
    data.setdefault('event_end_time', None)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE lead_requests SET
                event_type=%(event_type)s, event_date=%(event_date)s, event_time=%(event_time)s,
                event_end_time=%(event_end_time)s,
                estimated_guests_raw=%(estimated_guests_raw)s, estimated_guests=%(estimated_guests)s,
                venue=%(venue)s, venue_address=%(venue_address)s,
                client_name=%(client_name)s, client_email=%(client_email)s, client_phone=%(client_phone)s,
                marketing_consent=%(marketing_consent)s, referral_source=%(referral_source)s,
                notes=%(notes)s, internal_notes=%(internal_notes)s, status=%(status)s,
                loss_reason=%(loss_reason)s, updated_at=%(updated_at)s
            WHERE id=%(id)s
        """, data)
        conn.commit()

def upsert_lead_from_sheet(row_id, data: dict):
    """Insert or update a lead from Google Sheets using google_sheet_row_id as key."""
    data.setdefault('event_end_time', None)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM lead_requests WHERE google_sheet_row_id=%s", (row_id,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("""
                UPDATE lead_requests SET
                    submitted_at=%(submitted_at)s, event_type=%(event_type)s,
                    event_date=%(event_date)s, event_time=%(event_time)s,
                    event_end_time=%(event_end_time)s,
                    estimated_guests_raw=%(estimated_guests_raw)s, estimated_guests=%(estimated_guests)s,
                    venue=%(venue)s, venue_address=%(venue_address)s,
                    client_name=%(client_name)s, client_email=%(client_email)s, client_phone=%(client_phone)s,
                    marketing_consent=%(marketing_consent)s, referral_source=%(referral_source)s,
                    notes=%(notes)s, updated_at=NOW()
                WHERE google_sheet_row_id=%(google_sheet_row_id)s
            """, data)
            conn.commit()
            return existing[0], False
        else:
            cursor.execute("""
                INSERT INTO lead_requests (
                    submitted_at, source, google_sheet_row_id, event_type, event_date, event_time,
                    event_end_time, estimated_guests_raw, estimated_guests, venue, venue_address,
                    client_name, client_email, client_phone, marketing_consent,
                    referral_source, notes, status
                ) VALUES (
                    %(submitted_at)s, 'google_sheet', %(google_sheet_row_id)s, %(event_type)s,
                    %(event_date)s, %(event_time)s, %(event_end_time)s, %(estimated_guests_raw)s, %(estimated_guests)s,
                    %(venue)s, %(venue_address)s, %(client_name)s, %(client_email)s, %(client_phone)s,
                    %(marketing_consent)s, %(referral_source)s, %(notes)s, 'lead'
                ) RETURNING id
            """, data)
            row = cursor.fetchone()
            conn.commit()
            return row[0] if row else None, True

def upsert_event_from_sheet(row_id, data, status):
    """Insert or update an event in the pipeline from a Google Sheets row.
    data must contain: event_name, event_type, event_date, event_time, event_end_time,
    estimated_guests, venue, venue_address, client_name, client_email, client_phone.
    Returns (event_id, is_new).
    """
    orcamento = data.pop('orcamento', None)
    source_status = normalize_event_status(status)
    # A sync cannot claim that a payment proof was validated.  Preserve the
    # source value in the audit row, but stop at adjudicated until the team
    # validates the deposit inside Scoopy.
    status = (
        'adjudicado'
        if source_status in ('sinalizado', 'realizado', 'faturado', 'recebido')
        else source_status
    )
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, status FROM events WHERE google_sheet_row_id=%s",
            (row_id,),
        )
        existing = cursor.fetchone()
        if existing:
            event_id = existing[0]
            old_status = normalize_event_status(existing[1])
            apply_sheet_update = old_status not in _SHEET_PROTECTED_STATUSES
            if apply_sheet_update:
                cursor.execute("""
                    UPDATE events SET
                        event_name=%(event_name)s, event_type=%(event_type)s,
                        event_date=%(event_date)s, event_time=%(event_time)s,
                        event_end_time=%(event_end_time)s,
                        estimated_guests=%(estimated_guests)s,
                        venue=%(venue)s, venue_address=%(venue_address)s,
                        client_name=%(client_name)s, client_email=%(client_email)s,
                        client_phone=%(client_phone)s,
                        status=%(status)s, updated_at=NOW()
                    WHERE google_sheet_row_id=%(google_sheet_row_id)s
                """, {**data, 'status': status, 'google_sheet_row_id': row_id})
            else:
                # A completed/local operational record is authoritative.  The
                # Sheet remains a historical source, but repeated imports may
                # not reverse proof validation, resource commitments or edits.
                status = old_status
            is_new = False
        else:
            cursor.execute("""
                INSERT INTO events (
                    google_sheet_row_id, source, event_name, event_type,
                    event_date, event_time, event_end_time,
                    estimated_guests, venue, venue_address,
                    client_name, client_email, client_phone, status
                ) VALUES (
                    %(google_sheet_row_id)s, 'google_sheet', %(event_name)s, %(event_type)s,
                    %(event_date)s, %(event_time)s, %(event_end_time)s,
                    %(estimated_guests)s, %(venue)s, %(venue_address)s,
                    %(client_name)s, %(client_email)s, %(client_phone)s, %(status)s
                ) RETURNING id
            """, {**data, 'status': status, 'google_sheet_row_id': row_id})
            row = cursor.fetchone()
            event_id = row[0] if row else None
            old_status = None
            is_new = True
            apply_sheet_update = True
        if event_id and apply_sheet_update:
            _upsert_primary_occurrence(cursor, event_id, data)
        if event_id and apply_sheet_update and orcamento and float(orcamento) > 0:
            cursor.execute(
                "DELETE FROM quote_items WHERE event_id=%s AND descricao='Orçamento (Sheets)'",
                (event_id,)
            )
            sheet_total = _money(orcamento)
            cursor.execute("""
                INSERT INTO quote_items (
                    event_id, artigo_codigo, descricao, quantidade, preco_unitario,
                    unit_price_gross, total_gross
                )
                VALUES (%s, 'outro', 'Orçamento (Sheets)', 1, %s, %s, %s)
            """, (event_id, sheet_total, sheet_total, sheet_total))
        if event_id and apply_sheet_update and status == 'adjudicado':
            cursor.execute(
                "SELECT COALESCE(SUM(COALESCE(total_gross, total)), 0) "
                "FROM quote_items WHERE event_id = %s",
                (event_id,),
            )
            imported_total = cursor.fetchone()[0] or 0
            cursor.execute(
                "SELECT value_gross FROM event_pricing_settings "
                "WHERE key = 'sinal_percentagem' AND active = TRUE"
            )
            deposit_setting = cursor.fetchone()
            deposit_pct = deposit_setting[0] if deposit_setting else 0
            cursor.execute(
                """
                UPDATE events SET
                    invoice_amount_eur = %s,
                    deposit_amount_eur = %s,
                    expected_payment_date = CASE
                        WHEN event_date IS NULL THEN NULL
                        ELSE event_date + INTERVAL '30 day'
                    END
                WHERE id = %s
                """,
                (
                    imported_total,
                    _money(imported_total * deposit_pct / 100),
                    event_id,
                ),
            )
        if event_id and apply_sheet_update:
            _insert_event_history(
                cursor, event_id,
                'sheet_event_created' if is_new else 'sheet_event_updated',
                old_status=old_status, new_status=status,
                details={
                    'google_sheet_row_id': row_id,
                    'quote_imported': bool(orcamento),
                    'source_status': source_status,
                    'status_downgraded_for_deposit_validation': source_status != status,
                },
            )
        conn.commit()
        return event_id, is_new


def get_pipeline_dashboard():
    """Return aggregated pipeline stats for the dashboard."""
    from datetime import date as date_type
    today = date_type.today()
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT
                e.status,
                COUNT(*) as count,
                COALESCE(SUM(
                    COALESCE((SELECT SUM(qi.total) FROM quote_items qi WHERE qi.event_id=e.id), 0)
                ), 0) as total_value
            FROM events e
            GROUP BY e.status
        """)
        by_status = {r['status']: dict(r) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT
                COALESCE(SUM(
                    COALESCE((SELECT SUM(qi.total) FROM quote_items qi WHERE qi.event_id=e.id), 0)
                ), 0) as won_future_value,
                COUNT(*) as won_future_count
            FROM events e
            WHERE e.status IN ('adjudicado', 'sinalizado', 'realizado', 'faturado', 'recebido')
              AND e.event_date >= %s
        """, (today,))
        won_future = cursor.fetchone()
        cursor.execute("""
            SELECT status, COUNT(*) AS count,
                   COALESCE(SUM(invoice_amount_eur), 0) AS invoiced_value,
                   COALESCE(SUM(payment_amount_eur), 0) AS received_value,
                   COALESCE(SUM(GREATEST(invoice_amount_eur - COALESCE(payment_amount_eur, 0), 0)), 0)
                       AS outstanding_value
            FROM events
            WHERE status IN ('adjudicado','sinalizado','realizado','faturado','recebido')
            GROUP BY status
        """)
        financial_by_status = {row['status']: dict(row) for row in cursor.fetchall()}
        cursor.execute("""
            SELECT date_trunc('month', event_date)::date AS month,
                   COALESCE(SUM(invoice_amount_eur), 0) AS forecast_value,
                   COUNT(*) AS count
            FROM events
            WHERE event_date >= %s
              AND status IN ('adjudicado','sinalizado','realizado','faturado')
            GROUP BY 1 ORDER BY 1 LIMIT 6
        """, (today.replace(day=1),))
        monthly_forecast = cursor.fetchall()
        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='adjudicado') AS deposits_pending,
                COUNT(*) FILTER (WHERE status='cancelado') AS cancelled_count,
                COUNT(*) FILTER (WHERE status='sinalizado' AND event_date < %s) AS operational_risk_count
            FROM events
        """, (today,))
        finance_alerts = cursor.fetchone()

        return {
            'by_status': by_status,
            'won_future_value': float(won_future['won_future_value'] or 0),
            'won_future_count': int(won_future['won_future_count'] or 0),
            'financial_by_status': financial_by_status,
            'monthly_forecast': monthly_forecast,
            'finance_alerts': dict(finance_alerts or {}),
        }


# ── events ─────────────────────────────────────────────────────────────────────

def get_events(status=None, search=None, client=None, event_type=None,
               date_from=None, date_to=None, resource_id=None):
    status = normalize_event_status(status) if status else None
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        clauses, params = [], []
        if status:
            clauses.append("e.status=%s")
            params.append(status)
        if search:
            clauses.append("(e.event_name ILIKE %s OR e.client_name ILIKE %s OR e.client_email ILIKE %s)")
            params.extend([f"%{search.strip()}%"] * 3)
        if client:
            clauses.append("e.client_name ILIKE %s")
            params.append(f"%{client.strip()}%")
        if event_type:
            clauses.append("e.event_type=%s")
            params.append(event_type)
        if date_from or date_to:
            occurrence_dates = ["eo.event_id=e.id"]
            if date_from:
                occurrence_dates.append("eo.event_date >= %s")
                params.append(date_from)
            if date_to:
                occurrence_dates.append("eo.event_date <= %s")
                params.append(date_to)
            clauses.append(
                "EXISTS (SELECT 1 FROM event_occurrences eo WHERE "
                + " AND ".join(occurrence_dates) + ")"
            )
        if resource_id:
            clauses.append("""EXISTS (
                SELECT 1 FROM event_occurrences eo
                JOIN event_resource_reservations err ON err.occurrence_id=eo.id
                WHERE eo.event_id=e.id AND err.resource_id=%s
                  AND err.status IN ('requested','reserved')
            )""")
            params.append(resource_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        cursor.execute(f"""
            SELECT e.*, COALESCE((
                SELECT SUM(COALESCE(total_gross,total)) FROM quote_items WHERE event_id=e.id
            ), 0) AS quote_total
            FROM events e {where}
            ORDER BY e.event_date ASC NULLS LAST, e.created_at DESC
        """, params)
        return cursor.fetchall()


def get_event_calendar_occurrences(date_from, date_to):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT eo.*, e.event_name, e.client_name, e.status,
                   COALESCE(json_agg(er.code ORDER BY er.code)
                     FILTER (WHERE er.id IS NOT NULL), '[]'::json) AS resources
            FROM event_occurrences eo
            JOIN events e ON e.id=eo.event_id
            LEFT JOIN event_resource_reservations err
              ON err.occurrence_id=eo.id AND err.status IN ('requested','reserved')
            LEFT JOIN event_resources er ON er.id=err.resource_id
            WHERE eo.event_date BETWEEN %s AND %s
              AND e.status NOT IN ('rejeitado','cancelado')
            GROUP BY eo.id, e.id
            ORDER BY eo.event_date, eo.service_start_time NULLS FIRST, eo.id
        """, (date_from, date_to))
        return cursor.fetchall()


def get_event_notifications(today=None):
    today = today or date.today()
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT * FROM (
                SELECT e.id AS event_id, 'new_request' AS kind, 'Novo pedido' AS title,
                       e.event_name, e.created_at AS occurred_at
                FROM events e WHERE e.status='novos'
                UNION ALL
                SELECT e.id, 'accepted_quote', 'Orçamento aceite', e.event_name, l.created_at
                FROM event_portal_access_log l JOIN events e ON e.id=l.event_id
                WHERE l.action='quote_accepted'
                UNION ALL
                SELECT e.id, 'proof_uploaded', 'Comprovativo carregado', e.event_name, l.created_at
                FROM event_portal_access_log l JOIN events e ON e.id=l.event_id
                WHERE l.action='deposit_proof_uploaded'
                UNION ALL
                SELECT e.id, 'payment_received', 'Pagamento recebido', e.event_name, e.updated_at
                FROM events e WHERE e.payment_status='received'
                UNION ALL
                SELECT e.id, 'resource_conflict', 'Conflito de recurso assumido', e.event_name, err.updated_at
                FROM event_resource_reservations err
                JOIN event_occurrences eo ON eo.id=err.occurrence_id
                JOIN events e ON e.id=eo.event_id
                WHERE err.risk_acknowledged=TRUE AND err.status IN ('requested','reserved')
                UNION ALL
                SELECT e.id, 'event_soon', 'Evento nos próximos 7 dias', e.event_name,
                       e.event_date::timestamp
                FROM events e WHERE e.event_date BETWEEN %s AND %s
                  AND e.status IN ('adjudicado','sinalizado')
            ) alerts ORDER BY occurred_at DESC NULLS LAST LIMIT 30
        """, (today, today + timedelta(days=7)))
        return cursor.fetchall()

def get_event(event_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT e.*,
                COALESCE((SELECT SUM(total) FROM quote_items WHERE event_id=e.id), 0) as quote_total,
                COALESCE((
                    SELECT SUM(qi.total)
                    FROM quote_items qi
                    JOIN artigos_evento ae ON ae.codigo = qi.artigo_codigo
                    WHERE qi.event_id=e.id AND ae.cost_tier='high'
                ), 0) as quote_high_cost,
                COALESCE((
                    SELECT SUM(qi.total)
                    FROM quote_items qi
                    JOIN artigos_evento ae ON ae.codigo = qi.artigo_codigo
                    WHERE qi.event_id=e.id AND ae.cost_tier='medium'
                ), 0) as quote_medium_cost,
                COALESCE((
                    SELECT SUM(qi.total)
                    FROM quote_items qi
                    JOIN artigos_evento ae ON ae.codigo = qi.artigo_codigo
                    WHERE qi.event_id=e.id AND ae.cost_tier='low'
                ), 0) as quote_low_cost
            FROM events e WHERE e.id=%s
        """, (event_id,))
        return cursor.fetchone()

def create_event(data: dict, actor=None):
    data.setdefault('event_end_time', None)
    data['status'] = normalize_event_status(data.get('status'))
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO events (
                lead_id, event_name, event_type, event_date, event_time, event_end_time,
                estimated_guests, venue, venue_address,
                client_name, client_email, client_phone,
                status, internal_notes
            ) VALUES (
                %(lead_id)s, %(event_name)s, %(event_type)s, %(event_date)s, %(event_time)s, %(event_end_time)s,
                %(estimated_guests)s, %(venue)s, %(venue_address)s,
                %(client_name)s, %(client_email)s, %(client_phone)s,
                %(status)s, %(internal_notes)s
            ) RETURNING id
        """, data)
        row = cursor.fetchone()
        event_id = row[0] if row else None
        if event_id:
            _upsert_primary_occurrence(cursor, event_id, data)
            _insert_event_history(
                cursor, event_id, 'event_created',
                new_status=data['status'], actor=actor,
                details={'source': data.get('source', 'manual')},
            )
        conn.commit()
        return event_id

def update_event(event_id, data: dict, actor=None, risk_acknowledged=False):
    data['updated_at'] = datetime.now()
    data['id'] = event_id
    data.setdefault('event_end_time', None)
    data['status'] = normalize_event_status(data.get('status'))
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            conflicts = _upsert_primary_occurrence(
                cursor, event_id, data, risk_acknowledged=risk_acknowledged,
            )
        except Exception:
            conn.rollback()
            raise
        cursor.execute("""
            UPDATE events SET
                event_name=%(event_name)s, event_type=%(event_type)s,
                event_date=%(event_date)s, event_time=%(event_time)s, event_end_time=%(event_end_time)s,
                estimated_guests=%(estimated_guests)s,
                venue=%(venue)s, venue_address=%(venue_address)s,
                client_name=%(client_name)s, client_email=%(client_email)s, client_phone=%(client_phone)s,
                status=%(status)s, loss_reason=%(loss_reason)s,
                internal_notes=%(internal_notes)s, updated_at=%(updated_at)s
            WHERE id=%(id)s
        """, data)
        _insert_event_history(
            cursor, event_id, 'event_updated', actor=actor,
            details={
                'fields': ['event_name', 'event_type', 'event_date', 'venue', 'client'],
                'risk_acknowledged': risk_acknowledged,
                'resource_conflict_count': len(conflicts),
            },
        )
        conn.commit()

def _upsert_primary_occurrence(cursor, event_id, data, risk_acknowledged=False):
    """Keep existing single-date forms compatible with the v2 occurrences model."""
    cursor.execute(
        "SELECT id FROM event_occurrences WHERE event_id = %s AND occurrence_number = 1",
        (event_id,),
    )
    existing = cursor.fetchone()
    conflicts = []
    if existing:
        occurrence_id = _row_value(existing, 'id')
        conflicts = _validate_occurrence_reservation_change(
            cursor, occurrence_id, data.get('event_date'),
            data.get('event_time') or None, data.get('event_end_time') or None,
            risk_acknowledged=risk_acknowledged,
        )
    cursor.execute(
        """
        INSERT INTO event_occurrences (
            event_id, occurrence_number, event_date, venue, venue_address,
            service_start_time, service_end_time, logistics_notes
        ) VALUES (
            %s, 1, %s, %s, %s, NULLIF(%s, '')::time, NULLIF(%s, '')::time, %s
        )
        ON CONFLICT (event_id, occurrence_number) DO UPDATE SET
            event_date = EXCLUDED.event_date,
            venue = EXCLUDED.venue,
            venue_address = EXCLUDED.venue_address,
            service_start_time = EXCLUDED.service_start_time,
            service_end_time = EXCLUDED.service_end_time,
            logistics_notes = EXCLUDED.logistics_notes,
            updated_at = NOW()
        """,
        (
            event_id, data.get('event_date'), data.get('venue'),
            data.get('venue_address'), data.get('event_time') or '',
            data.get('event_end_time') or '', data.get('internal_notes'),
        ),
    )
    return conflicts


def transition_event_status(event_id, new_status, loss_reason=None, actor=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM events WHERE id=%s", (event_id,))
        ev = cursor.fetchone()
        if not ev:
            return False, "Evento não encontrado"

        old_status = normalize_event_status(ev['status'])
        new_status = normalize_event_status(new_status)
        if new_status not in VALID_EVENT_TRANSITIONS.get(old_status, ()):
            return False, f"Transição inválida de '{old_status}' para '{new_status}'"

        updates = {'status': new_status, 'loss_reason': None}
        if new_status == 'orcamentado':
            cursor.execute("SELECT COUNT(*) AS count FROM quote_items WHERE event_id=%s", (event_id,))
            if cursor.fetchone()['count'] == 0:
                return False, "Crie pelo menos uma linha de orçamento antes de o preparar."
        if new_status == 'enviado':
            cursor.execute(
                "SELECT COALESCE(SUM(COALESCE(total_gross,total)), 0) AS total, COUNT(*) AS count "
                "FROM quote_items WHERE event_id=%s",
                (event_id,),
            )
            quote = cursor.fetchone()
            if not quote['count'] or quote['total'] <= 0:
                return False, "O orçamento atual deve ter pelo menos uma linha e total positivo."
            revision = _quote_revision(cursor, event_id, lock_rows=True)
            cursor.execute(
                "SELECT id FROM event_quote_versions WHERE event_id=%s AND quote_revision=%s "
                "AND total_gross > 0 ORDER BY version_number DESC LIMIT 1",
                (event_id, revision),
            )
            sent_version = cursor.fetchone()
            if not sent_version:
                return False, "Guarde a versão atual do orçamento antes de registar o envio ao cliente."

        if new_status in ('rejeitado', 'cancelado'):
            if not loss_reason or not loss_reason.strip():
                return False, "É obrigatório indicar o motivo."
            updates['loss_reason'] = loss_reason.strip()

        if new_status == 'adjudicado':
            cursor.execute(
                "SELECT COALESCE(SUM(COALESCE(total_gross, total)), 0) "
                "FROM quote_items WHERE event_id=%s",
                (event_id,),
            )
            total_row = cursor.fetchone()
            total = next(iter(total_row.values())) if total_row else 0
            updates['invoice_amount_eur'] = float(total)
            if ev['event_date']:
                updates['expected_payment_date'] = ev['event_date'] + timedelta(days=30)
            else:
                updates['expected_payment_date'] = None
            cursor.execute(
                "SELECT value_gross FROM event_pricing_settings "
                "WHERE key = 'sinal_percentagem' AND active = TRUE"
            )
            deposit = cursor.fetchone()
            deposit_pct = next(iter(deposit.values())) if deposit else 0
            updates['deposit_amount_eur'] = round(float(total) * float(deposit_pct) / 100, 2)
        else:
            updates['invoice_amount_eur'] = ev['invoice_amount_eur']
            updates['expected_payment_date'] = ev['expected_payment_date']
            updates['deposit_amount_eur'] = ev.get('deposit_amount_eur')
        if new_status == 'sinalizado' and (
            not ev.get('deposit_received_at') or not ev.get('deposit_verified_by')
        ):
            return False, "Valide o comprovativo do sinal antes de sinalizar o evento."

        updates['id'] = event_id
        cursor.execute("""
            UPDATE events SET status=%(status)s, loss_reason=%(loss_reason)s,
                invoice_amount_eur=%(invoice_amount_eur)s,
                expected_payment_date=%(expected_payment_date)s,
                deposit_amount_eur=%(deposit_amount_eur)s,
                status_changed_at=NOW(), updated_at=NOW(),
                reserved_at=CASE WHEN %(status)s = 'sinalizado'
                    THEN COALESCE(reserved_at, NOW()) ELSE reserved_at END
            WHERE id=%(id)s
        """, updates)
        if new_status == 'enviado':
            cursor.execute(
                "UPDATE event_portal_requests SET sent_quote_version_id=%s, updated_at=NOW() "
                "WHERE event_id=%s",
                (sent_version['id'], event_id),
            )
        if new_status == 'sinalizado':
            cursor.execute(
                """
                UPDATE event_resource_reservations err
                SET status = 'reserved', reserved_by = COALESCE(%s, err.reserved_by),
                    updated_at = NOW()
                FROM event_occurrences eo
                WHERE err.occurrence_id = eo.id
                  AND eo.event_id = %s
                  AND err.status = 'requested'
                """,
                (actor, event_id),
            )
        _insert_event_history(
            cursor, event_id, 'status_changed', old_status=old_status,
            new_status=new_status, actor=actor, reason=updates['loss_reason'],
            details={
                'invoice_amount_eur': updates['invoice_amount_eur'],
                'deposit_amount_eur': updates['deposit_amount_eur'],
            },
        )
        conn.commit()
    return True, "OK"

def delete_event(event_id, actor=None):
    """Archive an event instead of destroying its immutable operational history."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT status FROM events WHERE id=%s", (event_id,))
        event = cursor.fetchone()
        if not event:
            return False
        old_status = normalize_event_status(event['status'])
        cursor.execute(
            """
            UPDATE events SET
                status = 'cancelado',
                loss_reason = COALESCE(loss_reason, 'Arquivado pela equipa'),
                archived_at = NOW(),
                archived_by = %s,
                status_changed_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            """,
            (actor, event_id),
        )
        _insert_event_history(
            cursor, event_id, 'event_archived', old_status=old_status,
            new_status='cancelado', actor=actor,
            reason='Arquivado pela equipa',
        )
        conn.commit()
        return True

# ── quote_items ────────────────────────────────────────────────────────────────

def _lock_quote_event(cursor, event_id):
    """Serialize quote editing with portal acceptance for the same event."""
    cursor.execute("SELECT id FROM events WHERE id = %s FOR UPDATE", (event_id,))

def get_quote_items(event_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM quote_items WHERE event_id=%s ORDER BY id", (event_id,))
        return cursor.fetchall()

def add_quote_item(event_id, artigo_codigo, descricao, quantidade, preco_unitario,
                   taxa_iva=None, actor=None):
    snapshot = calculate_quote_line(quantidade, preco_unitario, taxa_iva)
    with db_connection() as conn:
        cursor = conn.cursor()
        _lock_quote_event(cursor, event_id)
        cursor.execute("""
            INSERT INTO quote_items (
                event_id, artigo_codigo, descricao, quantidade, preco_unitario, taxa_iva,
                unit_price_gross, total_net, total_vat, total_gross
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            event_id, artigo_codigo, descricao, quantidade, preco_unitario, taxa_iva,
            snapshot['unit_price_gross'], snapshot['total_net'],
            snapshot['total_vat'], snapshot['total_gross'],
        ))
        _insert_event_history(
            cursor, event_id, 'quote_item_added', actor=actor,
            details={'artigo_codigo': artigo_codigo, 'total_gross': snapshot['total_gross']},
        )
        conn.commit()

def update_quote_item(item_id, event_id, descricao, quantidade, preco_unitario,
                      taxa_iva=None, actor=None):
    snapshot = calculate_quote_line(quantidade, preco_unitario, taxa_iva)
    with db_connection() as conn:
        cursor = conn.cursor()
        _lock_quote_event(cursor, event_id)
        cursor.execute("""
            UPDATE quote_items SET
                descricao=%s, quantidade=%s, preco_unitario=%s, taxa_iva=%s,
                unit_price_gross=%s, total_net=%s, total_vat=%s, total_gross=%s
            WHERE id=%s AND event_id=%s
        """, (
            descricao, quantidade, preco_unitario, taxa_iva,
            snapshot['unit_price_gross'], snapshot['total_net'],
            snapshot['total_vat'], snapshot['total_gross'], item_id, event_id,
        ))
        _insert_event_history(
            cursor, event_id, 'quote_item_updated', actor=actor,
            details={'item_id': item_id, 'total_gross': snapshot['total_gross']},
        )
        conn.commit()

def delete_quote_item(item_id, event_id, actor=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        _lock_quote_event(cursor, event_id)
        cursor.execute("DELETE FROM quote_items WHERE id=%s AND event_id=%s", (item_id, event_id))
        _insert_event_history(
            cursor, event_id, 'quote_item_deleted', actor=actor,
            details={'item_id': item_id},
        )
        conn.commit()


def create_quote_version(event_id, reason=None, actor=None):
    """Freeze the current quote before a negotiation or send; later edits stay separate."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        _lock_quote_event(cursor, event_id)
        cursor.execute("SELECT * FROM quote_items WHERE event_id=%s ORDER BY id", (event_id,))
        items = cursor.fetchall()
        total_gross = sum(
            Decimal(str(item.get('total_gross') if item.get('total_gross') is not None else item.get('total') or 0))
            for item in items
        )
        if not items or total_gross <= 0:
            raise ValueError('Não é possível guardar uma versão sem linhas e total positivo.')
        total_net = sum((Decimal(str(item.get('total_net') or 0)) for item in items), Decimal('0'))
        total_vat = sum((Decimal(str(item.get('total_vat') or 0)) for item in items), Decimal('0'))
        revision = _quote_revision(cursor, event_id, lock_rows=True)
        cursor.execute(
            "SELECT COALESCE(MAX(version_number), 0)+1 AS next_version "
            "FROM event_quote_versions WHERE event_id=%s",
            (event_id,),
        )
        version = cursor.fetchone()['next_version']
        cursor.execute("""
            INSERT INTO event_quote_versions
              (event_id, version_number, reason, created_by, snapshot, total_net, total_vat, total_gross, quote_revision)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
        """, (
            event_id, version, reason or None, actor,
            json.dumps(items, ensure_ascii=False, default=str),
            total_net, total_vat, total_gross, revision,
        ))
        _insert_event_history(
            cursor, event_id, 'quote_version_created', actor=actor, reason=reason,
            details={'version': version, 'item_count': len(items)},
        )
        conn.commit()
        return version


def get_quote_versions(event_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT * FROM event_quote_versions WHERE event_id=%s ORDER BY version_number DESC",
            (event_id,),
        )
        return cursor.fetchall()


def get_won_events_missing_taxa_iva():
    """List committed events that still have quote_items with taxa_iva IS NULL, for
    review and backfill against real invoicing records. Ordered oldest first so
    older, more time-sensitive VAT periods surface first."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT
                e.id, e.event_name, e.event_date, e.client_name,
                COALESCE(SUM(qi.total), 0) AS quote_total,
                COUNT(qi.id) AS total_items,
                COUNT(qi.id) FILTER (WHERE qi.taxa_iva IS NULL) AS missing_items
            FROM events e
            JOIN quote_items qi ON qi.event_id = e.id
            WHERE e.status IN ('adjudicado', 'sinalizado', 'realizado', 'faturado', 'recebido')
            GROUP BY e.id, e.event_name, e.event_date, e.client_name
            HAVING COUNT(qi.id) FILTER (WHERE qi.taxa_iva IS NULL) > 0
            ORDER BY e.event_date ASC NULLS LAST, e.id ASC
        """)
        return cursor.fetchall()


def bulk_set_taxa_iva(event_id, taxa_iva):
    """Set taxa_iva on all quote_items of an event that still have it NULL.
    Never overwrites a rate that was already explicitly recorded per-item."""
    with db_connection() as conn:
        cursor = conn.cursor()
        rate = _rate(taxa_iva)
        cursor.execute("""
            UPDATE quote_items SET
                taxa_iva=%s,
                unit_price_gross=COALESCE(unit_price_gross, preco_unitario),
                total_gross=COALESCE(total_gross, total),
                total_net=ROUND(total / (1 + %s), 2),
                total_vat=total - ROUND(total / (1 + %s), 2)
            WHERE event_id=%s AND taxa_iva IS NULL
        """, (rate, rate, rate, event_id))
        updated = cursor.rowcount
        if updated:
            _insert_event_history(
                cursor, event_id, 'quote_iva_backfilled',
                details={'taxa_iva': rate, 'updated_lines': updated},
            )
        conn.commit()
        return updated

def recalc_event_invoice(event_id):
    """Recalculate the gross event amount once a quote has been adjudicated."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, event_date FROM events WHERE id=%s", (event_id,))
        row = cursor.fetchone()
        if not row or normalize_event_status(row[0]) not in EVENT_FINANCIAL_STATUSES:
            return
        cursor.execute(
            "SELECT COALESCE(SUM(COALESCE(total_gross, total)),0) "
            "FROM quote_items WHERE event_id=%s",
            (event_id,),
        )
        total = cursor.fetchone()[0] or 0
        exp_pay = None
        if row[1]:
            exp_pay = row[1] + timedelta(days=30)
        cursor.execute("""
            UPDATE events SET invoice_amount_eur=%s, expected_payment_date=%s, updated_at=NOW()
            WHERE id=%s
        """, (float(total), exp_pay, event_id))
        conn.commit()

# ── leads → events conversion ──────────────────────────────────────────────────

def convert_lead_to_event(lead_id):
    """Create an event from a lead and link them."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM lead_requests WHERE id=%s", (lead_id,))
        lead = cursor.fetchone()
        if not lead:
            return None
        cursor.execute("""
            INSERT INTO events (
                lead_id, event_name, event_type, event_date, event_time, event_end_time,
                estimated_guests, venue, venue_address,
                client_name, client_email, client_phone, status, internal_notes
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'novos',%s)
            RETURNING id
        """, (
            lead['id'],
            f"Evento {lead['client_name'] or ''}".strip(),
            lead['event_type'], lead['event_date'], lead['event_time'],
            lead.get('event_end_time'),
            lead['estimated_guests'], lead['venue'], lead['venue_address'],
            lead['client_name'], lead['client_email'], lead['client_phone'],
            lead['internal_notes'] or '',
        ))
        row = cursor.fetchone()
        event_id = row['id'] if row else None
        if event_id:
            _upsert_primary_occurrence(cursor, event_id, {
                'event_date': lead['event_date'],
                'event_time': lead['event_time'],
                'event_end_time': lead.get('event_end_time'),
                'venue': lead['venue'],
                'venue_address': lead['venue_address'],
                'internal_notes': lead['internal_notes'] or '',
            })
            _insert_event_history(
                cursor, event_id, 'event_created', new_status='novos',
                details={'source': 'lead_conversion', 'lead_id': lead_id},
            )
            cursor.execute("UPDATE lead_requests SET status='contacted', updated_at=NOW() WHERE id=%s", (lead_id,))
        conn.commit()
    return event_id

# ── Customer event portal ───────────────────────────────────────────────────────

def normalize_portal_email(value):
    """Canonical email identity used exclusively by the public portal."""
    email = (value or '').strip().casefold()
    if not email or len(email) > 255 or email.count('@') != 1:
        raise ValueError('Indique um email válido.')
    local, domain = email.rsplit('@', 1)
    if not local or '.' not in domain or ' ' in email:
        raise ValueError('Indique um email válido.')
    return email


def calculate_portal_flavours(guests, scoops, flavours):
    """Calculate a conservative customer estimate with the agreed serving rules."""
    try:
        guests = int(guests)
        scoops = int(scoops)
    except (TypeError, ValueError):
        raise ValueError('Indique o número de participantes e de bolas.')
    cleaned = [str(flavour).strip() for flavour in (flavours or []) if str(flavour).strip()]
    cleaned = list(dict.fromkeys(cleaned))
    if guests < 1 or guests > 5000:
        raise ValueError('O número de participantes deve estar entre 1 e 5000.')
    if scoops not in (1, 2):
        raise ValueError('Escolha uma ou duas bolas por pessoa.')
    if not cleaned or len(cleaned) > 6:
        raise ValueError('Escolha entre um e seis sabores.')
    grams_per_guest = 70 if scoops == 1 else 120
    required_kg = Decimal(guests * grams_per_guest) / Decimal(1000)
    per_flavour = max(
        Decimal('2.00'),
        (required_kg / len(cleaned)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
    )
    quantities = [
        {'name': flavour, 'kg': str(per_flavour)}
        for flavour in cleaned
    ]
    total_kg = (per_flavour * len(cleaned)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {
        'guests': guests,
        'scoops': scoops,
        'grams_per_guest': grams_per_guest,
        'flavours': quantities,
        'total_kg': total_kg,
    }


def get_portal_unavailable_dates():
    """Return dates that deserve a warning on the customer-facing calendar."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT DISTINCT eo.event_date, 'confirmed' AS risk
            FROM event_occurrences eo
            JOIN events e ON e.id = eo.event_id
            WHERE eo.event_date IS NOT NULL
              AND e.status IN ('adjudicado', 'sinalizado', 'realizado', 'faturado', 'recebido')
            UNION
            SELECT DISTINCT eo.event_date, 'resource_risk' AS risk
            FROM event_occurrences eo
            JOIN event_resource_reservations err ON err.occurrence_id = eo.id
            JOIN events e ON e.id = eo.event_id
            WHERE eo.event_date IS NOT NULL
              AND err.status IN ('requested', 'reserved')
              AND err.risk_acknowledged = TRUE
              AND e.status NOT IN ('rejeitado', 'cancelado')
            ORDER BY event_date
        """)
        return cursor.fetchall()


def _portal_brand_from_row(row):
    brand = dict(_PORTAL_BRAND_DEFAULTS)
    brand['field_labels'] = dict(_PORTAL_BRAND_DEFAULTS['field_labels'])
    brand['visible_fields'] = dict(_PORTAL_BRAND_DEFAULTS['visible_fields'])
    if row:
        brand.update(dict(row))
        brand['field_labels'] = {
            **_PORTAL_BRAND_DEFAULTS['field_labels'],
            **(row.get('field_labels') or {}),
        }
        brand['visible_fields'] = {
            **_PORTAL_BRAND_DEFAULTS['visible_fields'],
            **(row.get('visible_fields') or {}),
        }
    return brand


def get_portal_brand_config(store_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, store_id, brand_name, logo_filename, primary_color, accent_color,
                   background_color, text_color, button_color, button_text_color,
                   form_title, form_intro, confirmation_message, contact_text,
                   field_labels, visible_fields, is_default
            FROM event_portal_brand_configs
            WHERE store_id = %s
        """, (store_id,))
        return _portal_brand_from_row(cursor.fetchone())


def get_portal_brand_configs():
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, store_id, brand_name, logo_filename, primary_color, accent_color,
                   background_color, text_color, button_color, button_text_color,
                   form_title, form_intro, confirmation_message, contact_text,
                   field_labels, visible_fields, is_default
            FROM event_portal_brand_configs
            ORDER BY store_id
        """)
        return [_portal_brand_from_row(row) for row in cursor.fetchall()]


def get_default_portal_brand():
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT c.id, c.store_id, c.brand_name, c.logo_filename, c.primary_color,
                   c.accent_color, c.background_color, c.text_color, c.button_color,
                   c.button_text_color, c.form_title, c.form_intro,
                   c.confirmation_message, c.contact_text, c.field_labels,
                   c.visible_fields, c.is_default
            FROM event_portal_brand_configs c
            JOIN stores s ON s.id = c.store_id
            WHERE s.is_active = TRUE
            ORDER BY c.is_default DESC, c.updated_at DESC
            LIMIT 1
        """)
        return _portal_brand_from_row(cursor.fetchone())


def save_portal_brand_config(store_id, values, logo_filename=None, is_default=False):
    """Upsert one store identity; selecting default atomically clears the old one."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT is_active FROM stores WHERE id=%s FOR UPDATE", (store_id,)
            )
            store = cursor.fetchone()
            if not store:
                raise ValueError('A loja selecionada já não existe.')
            if is_default and not store[0]:
                raise ValueError('Só uma loja ativa pode ser a marca pública predefinida.')
            if is_default:
                cursor.execute(
                    "UPDATE event_portal_brand_configs SET is_default=FALSE, updated_at=NOW() "
                    "WHERE is_default=TRUE"
                )
            cursor.execute("""
                INSERT INTO event_portal_brand_configs (
                    store_id, brand_name, logo_filename, primary_color, accent_color,
                    background_color, text_color, button_color, button_text_color,
                    form_title, form_intro, confirmation_message, contact_text,
                    field_labels, visible_fields, is_default
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (store_id) DO UPDATE SET
                    brand_name=EXCLUDED.brand_name, logo_filename=EXCLUDED.logo_filename,
                    primary_color=EXCLUDED.primary_color, accent_color=EXCLUDED.accent_color,
                    background_color=EXCLUDED.background_color, text_color=EXCLUDED.text_color,
                    button_color=EXCLUDED.button_color, button_text_color=EXCLUDED.button_text_color,
                    form_title=EXCLUDED.form_title, form_intro=EXCLUDED.form_intro,
                    confirmation_message=EXCLUDED.confirmation_message,
                    contact_text=EXCLUDED.contact_text, field_labels=EXCLUDED.field_labels,
                    visible_fields=EXCLUDED.visible_fields, is_default=EXCLUDED.is_default,
                    updated_at=NOW()
            """, (
                store_id, values['brand_name'], logo_filename,
                values['primary_color'], values['accent_color'], values['background_color'],
                values['text_color'], values['button_color'], values['button_text_color'],
                values['form_title'], values['form_intro'], values['confirmation_message'],
                values['contact_text'], json.dumps(values['field_labels'], ensure_ascii=False),
                json.dumps(values['visible_fields'], ensure_ascii=False), is_default,
            ))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _quote_revision(cursor, event_id, lock_rows=False):
    cursor.execute("""
        SELECT id, descricao, quantidade, preco_unitario, taxa_iva,
               unit_price_gross, total_net, total_vat, total_gross, total
        FROM quote_items WHERE event_id = %s ORDER BY id
    """ + (" FOR UPDATE" if lock_rows else ""), (event_id,))
    rows = cursor.fetchall()
    fields = (
        'id', 'descricao', 'quantidade', 'preco_unitario', 'taxa_iva',
        'unit_price_gross', 'total_net', 'total_vat', 'total_gross', 'total',
    )
    normalized = [
        [
            str(row.get(field)) if row.get(field) is not None else None
            for field in fields
        ] if hasattr(row, 'get') else [
            str(value) if value is not None else None for value in row
        ]
        for row in rows
    ]
    encoded = json.dumps(normalized, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest(), rows


def create_portal_event_request(data):
    """Create a public request, its occurrences and its initial estimate atomically."""
    email = normalize_portal_email(data.get('client_email'))
    occurrences = data.get('occurrences') or []
    if not occurrences:
        raise ValueError('Adicione pelo menos uma data para o evento.')
    if not data.get('privacy_accepted'):
        raise ValueError('É necessário aceitar a informação de privacidade.')
    flavour_plan = calculate_portal_flavours(
        data.get('estimated_guests'), data.get('servings_per_guest'), data.get('flavours'),
    )
    catering = bool(data.get('catering_requested'))
    estimate_eligible = (
        flavour_plan['guests'] <= 200 and len(occurrences) == 1 and not catering
    )

    with db_connection() as conn:
        cursor = conn.cursor()
        access_code = secrets.token_urlsafe(9)
        access_code_hash = hashlib.sha256(access_code.encode('utf-8')).hexdigest()
        first = occurrences[0]
        cursor.execute("""
            INSERT INTO events (
                source, event_name, event_type, event_date, event_time, event_end_time,
                estimated_guests, venue, venue_address, client_name, client_email,
                client_phone, status, internal_notes
            ) VALUES (
                'customer_portal', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, 'novos', %s
            ) RETURNING id
        """, (
            data.get('event_name') or f"Pedido de {data.get('client_name', '').strip()}",
            data.get('event_type'), first.get('event_date'), first.get('service_start_time'),
            first.get('service_end_time'), flavour_plan['guests'], first.get('venue'),
            first.get('venue_address'), data.get('client_name'), email,
            data.get('client_phone'), 'Pedido submetido pelo portal de clientes.',
        ))
        event_row = cursor.fetchone()
        event_id = event_row[0]
        for number, occurrence in enumerate(occurrences, start=1):
            cursor.execute("""
                INSERT INTO event_occurrences (
                    event_id, occurrence_number, event_date, venue, venue_address,
                    latitude, longitude, estimated_km, service_start_time,
                    service_end_time, expected_duration_minutes, logistics_notes, service_mode
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                event_id, number, occurrence.get('event_date'), occurrence.get('venue'),
                occurrence.get('venue_address'), occurrence.get('latitude'),
                occurrence.get('longitude'), occurrence.get('estimated_km'),
                occurrence.get('service_start_time'), occurrence.get('service_end_time'),
                occurrence.get('expected_duration_minutes'),
                occurrence.get('logistics_notes'), occurrence.get('service_mode', 'pending'),
            ))

        estimate_base = estimate_vat = estimate_total = None
        if estimate_eligible:
            cursor.execute("""
                SELECT value_gross, taxa_iva FROM event_pricing_settings
                WHERE key = 'gelado_kg' AND active = TRUE
            """)
            price = cursor.fetchone() or (Decimal('0'), None)
            snapshot = calculate_quote_line(flavour_plan['total_kg'], price[0], price[1])
            estimate_base = snapshot['total_net']
            estimate_vat = snapshot['total_vat']
            estimate_total = snapshot['total_gross']
            cursor.execute("""
                INSERT INTO quote_items (
                    event_id, artigo_codigo, descricao, quantidade, preco_unitario, taxa_iva,
                    unit_price_gross, total_net, total_vat, total_gross
                ) VALUES (%s, 'gelado_kg', 'Estimativa de gelado (portal)', %s, %s, %s, %s, %s, %s, %s)
            """, (
                event_id, flavour_plan['total_kg'], price[0], price[1],
                snapshot['unit_price_gross'], snapshot['total_net'],
                snapshot['total_vat'], snapshot['total_gross'],
            ))

        cursor.execute("""
            INSERT INTO event_portal_requests (
                event_id, email_normalized, access_code_hash, marketing_consent, referral_source,
                servings_per_guest, flavours, resource_preferences, catering_requested,
                estimate_eligible, estimated_base_eur, estimated_vat_eur, estimated_total_eur,
                public_message, brand_store_id
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            event_id, email, access_code_hash, bool(data.get('marketing_consent')), data.get('referral_source'),
            flavour_plan['scoops'], json.dumps(flavour_plan['flavours'], ensure_ascii=False),
            json.dumps(data.get('resource_preferences') or [], ensure_ascii=False),
            catering, estimate_eligible, estimate_base, estimate_vat, estimate_total,
            data.get('confirmation_message') or (
                'Recebemos o seu pedido. A equipa irá confirmar disponibilidade e logística.'
                if estimate_eligible else
                'Recebemos o seu pedido. Como envolve mais de 200 participantes, várias datas '
                'ou catering, a equipa irá contactar para preparar uma proposta.'
            ),
            data.get('brand_store_id'),
        ))
        _insert_event_history(
            cursor, event_id, 'portal_request_submitted', actor=f'portal:{email}',
            new_status='novos',
            details={
                'occurrence_count': len(occurrences),
                'estimate_eligible': estimate_eligible,
                'resource_preferences': data.get('resource_preferences') or [],
            },
        )
        conn.commit()
        return {
            'event_id': event_id,
            'estimate_eligible': estimate_eligible,
            'estimated_base_eur': estimate_base,
            'estimated_vat_eur': estimate_vat,
            'estimated_total_eur': estimate_total,
            'flavour_plan': flavour_plan,
            'access_code': access_code,
        }


def record_portal_access(email, action, event_id=None, ip_fingerprint=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO event_portal_access_log
                (email_normalized, event_id, action, ip_fingerprint)
            VALUES (%s, %s, %s, %s)
        """, (normalize_portal_email(email), event_id, action, ip_fingerprint))
        conn.commit()


def verify_portal_request_access(email, access_code):
    """Return only the individual request protected by the supplied code."""
    normalized = normalize_portal_email(email)
    candidate = hashlib.sha256((access_code or '').strip().encode('utf-8')).hexdigest()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT event_id FROM event_portal_requests
            WHERE email_normalized = %s AND access_code_hash = %s
            """,
            (normalized, candidate),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def get_portal_events_for_email(email):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT e.id, e.event_name, e.event_type, e.status, e.created_at,
                   p.brand_store_id,
                   p.estimate_eligible, p.estimated_total_eur, p.public_message,
                   (SELECT MIN(eo.event_date) FROM event_occurrences eo WHERE eo.event_id = e.id) AS next_date,
                   (SELECT COUNT(*) FROM event_occurrences eo WHERE eo.event_id = e.id) AS occurrence_count
            FROM event_portal_requests p
            JOIN events e ON e.id = p.event_id
            WHERE p.email_normalized = %s
            ORDER BY e.created_at DESC
        """, (normalize_portal_email(email),))
        return cursor.fetchall()


def get_portal_event_for_email(event_id, email):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT e.*, p.marketing_consent, p.referral_source, p.servings_per_guest,
                   p.flavours, p.resource_preferences, p.catering_requested,
                   p.estimate_eligible, p.estimated_base_eur, p.estimated_vat_eur,
                    p.estimated_total_eur, p.public_message, p.logistics_message,
                    p.brand_store_id,
                    p.sent_quote_version_id, p.accepted_quote_revision, p.quote_accepted_at,
                   (SELECT MIN(eo.event_date) FROM event_occurrences eo
                    WHERE eo.event_id = e.id) AS next_date,
                   (SELECT COUNT(*) FROM event_occurrences eo
                    WHERE eo.event_id = e.id) AS occurrence_count
            FROM event_portal_requests p
            JOIN events e ON e.id = p.event_id
            WHERE e.id = %s AND p.email_normalized = %s
        """, (event_id, normalize_portal_email(email)))
        event = cursor.fetchone()
        if not event:
            return None
        event['portal_brand'] = (
            get_portal_brand_config(event['brand_store_id'])
            if event.get('brand_store_id')
            else get_default_portal_brand()
        )
        if event['sent_quote_version_id']:
            cursor.execute("""
                SELECT snapshot, quote_revision, total_net, total_vat, total_gross
                FROM event_quote_versions WHERE id=%s AND event_id=%s
            """, (event['sent_quote_version_id'], event_id))
            sent_quote = cursor.fetchone()
        else:
            sent_quote = None
        if sent_quote:
            quote_items = sent_quote['snapshot'] or []
            for item in quote_items:
                for field in ('total_net', 'total_vat', 'total_gross'):
                    if item.get(field) is not None:
                        item[field] = Decimal(str(item[field]))
            event['quote_items_public'] = quote_items
            event['quote_revision'] = sent_quote['quote_revision']
            event['quote_totals'] = {
                'total_net': sent_quote['total_net'],
                'total_vat': sent_quote['total_vat'],
                'total_gross': sent_quote['total_gross'],
            }
        else:
            event['quote_items_public'] = []
            event['quote_revision'] = None
            event['quote_totals'] = {'total_net': None, 'total_vat': None, 'total_gross': Decimal('0')}
        event['occurrences_public'] = get_event_occurrences(event_id)
        cursor.execute("""
            SELECT original_filename, purpose, created_at
            FROM event_portal_files
            WHERE event_id = %s AND deleted_at IS NULL
            ORDER BY created_at DESC
        """, (event_id,))
        event['proofs'] = cursor.fetchall()
        return event


def accept_portal_quote(event_id, email, presented_revision):
    """Accept only a sent, unchanged quote and leave deposit validation to staff."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT e.status, e.event_date, p.accepted_quote_revision, p.sent_quote_version_id
            FROM event_portal_requests p
            JOIN events e ON e.id = p.event_id
            WHERE e.id = %s AND p.email_normalized = %s
            FOR UPDATE
        """, (event_id, normalize_portal_email(email)))
        event = cursor.fetchone()
        if not event:
            raise ValueError('Pedido não encontrado.')
        if not event['sent_quote_version_id']:
            raise ValueError('O orçamento ainda não tem uma versão enviada.')
        cursor.execute("""
            SELECT quote_revision, total_gross FROM event_quote_versions
            WHERE id=%s AND event_id=%s FOR UPDATE
        """, (event['sent_quote_version_id'], event_id))
        sent_quote = cursor.fetchone()
        if not sent_quote or presented_revision != sent_quote['quote_revision']:
            raise ValueError('A versão enviada já não está disponível. Contacte a equipa.')
        revision = sent_quote['quote_revision']
        if event['accepted_quote_revision'] == revision:
            return False
        if normalize_event_status(event['status']) != 'enviado':
            raise ValueError('O orçamento ainda não está disponível para aceitação.')
        total = _money(sent_quote['total_gross'])
        if total <= 0:
            raise ValueError('O orçamento não tem um valor positivo para aceitar.')
        cursor.execute("""
            SELECT value_gross FROM event_pricing_settings
            WHERE key = 'sinal_percentagem' AND active = TRUE
        """)
        deposit_row = cursor.fetchone()
        deposit_percentage = _money(deposit_row['value_gross']) if deposit_row else Decimal('0')
        cursor.execute("""
            UPDATE events SET status = 'adjudicado', status_changed_at = NOW(),
                invoice_amount_eur = %s, deposit_amount_eur = %s,
                expected_payment_date = %s, updated_at = NOW()
            WHERE id = %s
        """, (
            total, _money(total * deposit_percentage / Decimal('100')),
            event['event_date'] + timedelta(days=30) if event.get('event_date') else None,
            event_id,
        ))
        cursor.execute("""
            UPDATE event_portal_requests
            SET accepted_quote_revision = %s, quote_accepted_at = NOW(), updated_at = NOW()
            WHERE event_id = %s
        """, (revision, event_id))
        _insert_event_history(
            cursor, event_id, 'portal_quote_accepted', old_status='enviado',
            new_status='adjudicado', actor=f'portal:{normalize_portal_email(email)}',
            details={'quote_revision': revision},
        )
        conn.commit()
        return True


def create_portal_file(event_id, email, metadata):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM event_portal_requests
            WHERE event_id = %s AND email_normalized = %s
        """, (event_id, normalize_portal_email(email)))
        if not cursor.fetchone():
            raise ValueError('Pedido não encontrado.')
        cursor.execute("""
            INSERT INTO event_portal_files (
                event_id, storage_name, original_filename, content_type,
                byte_size, purpose, expires_at
            ) VALUES (%s, %s, %s, %s, %s, 'deposit_proof', %s)
        """, (
            event_id, metadata['storage_name'], metadata['original_filename'],
            metadata['content_type'], metadata['byte_size'], metadata['expires_at'],
        ))
        _insert_event_history(
            cursor, event_id, 'portal_deposit_proof_uploaded',
            actor=f'portal:{normalize_portal_email(email)}',
            details={'filename': metadata['original_filename']},
        )
        conn.commit()


def assert_portal_event_access(event_id, email):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM event_portal_requests
            WHERE event_id = %s AND email_normalized = %s
        """, (event_id, normalize_portal_email(email)))
        if not cursor.fetchone():
            raise ValueError('Pedido não encontrado.')


def expire_portal_files(now=None):
    """Mark expired portal proofs deleted and return their private storage names."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE event_portal_files
            SET deleted_at = NOW()
            WHERE deleted_at IS NULL AND expires_at <= COALESCE(%s, NOW())
            RETURNING storage_name
        """, (now,))
        names = [row[0] for row in cursor.fetchall()]
        conn.commit()
        return names


def get_portal_geocode_cache(address_key):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT latitude, longitude, round_trip_km, provider, failed
            FROM event_portal_geocode_cache WHERE address_key = %s
        """, (address_key,))
        return cursor.fetchone()


def save_portal_geocode_cache(address_key, result):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO event_portal_geocode_cache
                (address_key, latitude, longitude, round_trip_km, provider, failed)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (address_key) DO UPDATE SET
                latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                round_trip_km = EXCLUDED.round_trip_km, provider = EXCLUDED.provider,
                failed = EXCLUDED.failed, updated_at = NOW()
        """, (
            address_key, result.get('latitude'), result.get('longitude'),
            result.get('round_trip_km'), result.get('provider'), bool(result.get('failed')),
        ))
        conn.commit()


# ── event_clients ───────────────────────────────────────────────────────────────

def get_event_clients(marketing_only=False, search=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where = []
        params = []
        if marketing_only:
            where.append("ec.marketing_consent = TRUE")
        if search:
            where.append("(ec.name ILIKE %s OR ec.email ILIKE %s)")
            params += [f'%{search}%', f'%{search}%']
        where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
        cursor.execute(f"""
            SELECT ec.*,
                COUNT(DISTINCT e.id) as event_count,
                MAX(e.event_date) as last_event_date
            FROM event_clients ec
            LEFT JOIN events e ON e.client_id = ec.id
            {where_sql}
            GROUP BY ec.id
            ORDER BY ec.name
        """, params)
        return cursor.fetchall()

def get_event_client(client_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT ec.*,
                COUNT(DISTINCT e.id) as event_count,
                MAX(e.event_date) as last_event_date
            FROM event_clients ec
            LEFT JOIN events e ON e.client_id = ec.id
            WHERE ec.id=%s
            GROUP BY ec.id
        """, (client_id,))
        return cursor.fetchone()

def search_event_clients(q):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, name, email, phone, marketing_consent
            FROM event_clients
            WHERE name ILIKE %s OR email ILIKE %s
            ORDER BY name LIMIT 10
        """, (f'%{q}%', f'%{q}%'))
        return cursor.fetchall()

def create_event_client(data: dict):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO event_clients (name, email, phone, marketing_consent, notes)
            VALUES (%(name)s, %(email)s, %(phone)s, %(marketing_consent)s, %(notes)s)
            RETURNING id
        """, data)
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None

def update_event_client(client_id, data: dict):
    data['id'] = client_id
    data['updated_at'] = datetime.now()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE event_clients SET
                name=%(name)s, email=%(email)s, phone=%(phone)s,
                marketing_consent=%(marketing_consent)s, notes=%(notes)s,
                updated_at=%(updated_at)s
            WHERE id=%(id)s
        """, data)
        conn.commit()

def get_client_events(client_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, event_name, event_type, event_date, event_time, event_end_time,
                   status, estimated_guests, venue, invoice_amount_eur
            FROM events WHERE client_id=%s
            ORDER BY event_date DESC NULLS LAST
        """, (client_id,))
        return cursor.fetchall()

def link_event_client(event_id, client_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE events SET client_id=%s, updated_at=NOW() WHERE id=%s", (client_id, event_id))
        conn.commit()

# ── Suppliers ──────────────────────────────────────────────────────────────────

# ── Fase 2 M-Eventos: Recebimentos & Production Alert ─────────────────────────

def get_eventos_adjudicados_para_producao(alert_date):
    """Returns adjudicated events whose production alert date equals alert_date (event_date - 1)."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """SELECT e.*,
                      array_agg(
                          json_build_object(
                              'id', ei.id,
                              'descricao', ei.descricao,
                              'quantidade', ei.quantidade,
                              'unidade', ei.unidade,
                              'is_production_item', ei.is_production_item
                          ) ORDER BY ei.id
                      ) FILTER (WHERE ei.id IS NOT NULL) as items
               FROM eventos e
               LEFT JOIN evento_items ei ON ei.evento_id = e.id AND ei.is_production_item = TRUE
               WHERE e.status = 'adjudicado' AND e.event_date = %s + INTERVAL '1 day'
               GROUP BY e.id
               ORDER BY e.event_date""",
            (alert_date,)
        )
        rows = cursor.fetchall()
        result = []
        for r in rows:
            row = dict(r)
            if row['items'] is None:
                row['items'] = []
            result.append(row)
        return result


def mark_production_alert_sent(evento_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE eventos SET production_alert_sent = TRUE, updated_at = NOW() WHERE id = %s",
            (evento_id,)
        )
        conn.commit()


def get_all_eventos(status_filter=None, payment_status_filter=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where_clauses = []
        params = []
        if status_filter:
            where_clauses.append("e.status = %s")
            params.append(status_filter)
        if payment_status_filter:
            where_clauses.append("e.payment_status = %s")
            params.append(payment_status_filter)
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        cursor.execute(
            f"""SELECT e.*,
                       COUNT(ei.id) FILTER (WHERE ei.is_production_item) as n_production_items
                FROM eventos e
                LEFT JOIN evento_items ei ON ei.evento_id = e.id
                {where_sql}
                GROUP BY e.id
                ORDER BY e.event_date DESC""",
            params
        )
        return cursor.fetchall()


def get_evento_by_id(evento_id: int):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM eventos WHERE id = %s", (evento_id,))
        row = cursor.fetchone()
        if not row:
            return None, []
        cursor.execute(
            "SELECT * FROM evento_items WHERE evento_id = %s ORDER BY id",
            (evento_id,)
        )
        items = cursor.fetchall()
        return dict(row), [dict(i) for i in items]


def create_evento(data: dict) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO eventos (cliente, descricao, event_date, local, status,
                                   invoice_amount_eur, expected_payment_date, notas)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                data['cliente'], data.get('descricao'), data['event_date'],
                data.get('local'), data.get('status', 'proposta'),
                data.get('invoice_amount_eur') or None,
                data.get('expected_payment_date') or None,
                data.get('notas'),
            )
        )
        evento_id = cursor.fetchone()[0]
        conn.commit()
    return evento_id


def update_evento(evento_id: int, data: dict):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE eventos SET
                   cliente = %s, descricao = %s, event_date = %s, local = %s,
                   status = %s, invoice_amount_eur = %s, expected_payment_date = %s,
                   notas = %s, updated_at = NOW()
               WHERE id = %s""",
            (
                data['cliente'], data.get('descricao'), data['event_date'],
                data.get('local'), data.get('status', 'proposta'),
                data.get('invoice_amount_eur') or None,
                data.get('expected_payment_date') or None,
                data.get('notas'), evento_id,
            )
        )
        conn.commit()


def delete_evento(evento_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM eventos WHERE id = %s", (evento_id,))
        conn.commit()


def upsert_evento_items(evento_id: int, items: list):
    """Replace all items for an event."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM evento_items WHERE evento_id = %s", (evento_id,))
        for item in items:
            cursor.execute(
                """INSERT INTO evento_items (evento_id, descricao, quantidade, is_production_item, unidade)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    evento_id, item['descricao'],
                    float(item.get('quantidade', 1)),
                    bool(item.get('is_production_item', False)),
                    item.get('unidade', 'un'),
                )
            )
        conn.commit()


def registar_pagamento_evento(evento_id: int, payment_amount: float, payment_date, payment_status: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE eventos SET
                   payment_amount_eur = %s,
                   payment_date = %s,
                   payment_status = %s,
                   updated_at = NOW()
               WHERE id = %s""",
            (payment_amount, payment_date, payment_status, evento_id)
        )
        conn.commit()


def get_eventos_recebimentos():
    """Returns adjudicated events with payment info for cash flow integration."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """SELECT * FROM eventos
               WHERE status = 'adjudicado'
               ORDER BY expected_payment_date NULLS LAST, event_date"""
        )
        return cursor.fetchall()

