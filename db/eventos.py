import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
import json

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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM events WHERE google_sheet_row_id=%s", (row_id,))
        existing = cursor.fetchone()
        if existing:
            event_id = existing[0]
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
            is_new = True
        if event_id and orcamento and float(orcamento) > 0:
            cursor.execute(
                "DELETE FROM quote_items WHERE event_id=%s AND descricao='Orçamento (Sheets)'",
                (event_id,)
            )
            cursor.execute("""
                INSERT INTO quote_items (event_id, artigo_codigo, descricao, quantidade, preco_unitario)
                VALUES (%s, 'outro', 'Orçamento (Sheets)', 1, %s)
            """, (event_id, float(orcamento)))
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
            WHERE e.status='won' AND e.event_date >= %s
        """, (today,))
        won_future = cursor.fetchone()

        return {
            'by_status': by_status,
            'won_future_value': float(won_future['won_future_value'] or 0),
            'won_future_count': int(won_future['won_future_count'] or 0),
        }


# ── events ─────────────────────────────────────────────────────────────────────

def get_events(status=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if status:
            cursor.execute("""
                SELECT e.*, 
                    COALESCE((SELECT SUM(total) FROM quote_items WHERE event_id=e.id), 0) as quote_total
                FROM events e
                WHERE e.status=%s
                ORDER BY e.event_date ASC NULLS LAST, e.created_at DESC
            """, (status,))
        else:
            cursor.execute("""
                SELECT e.*,
                    COALESCE((SELECT SUM(total) FROM quote_items WHERE event_id=e.id), 0) as quote_total
                FROM events e
                ORDER BY e.event_date ASC NULLS LAST, e.created_at DESC
            """)
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

def create_event(data: dict):
    data.setdefault('event_end_time', None)
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
        conn.commit()
        return row[0] if row else None

def update_event(event_id, data: dict):
    data['updated_at'] = datetime.now()
    data['id'] = event_id
    data.setdefault('event_end_time', None)
    with db_connection() as conn:
        cursor = conn.cursor()
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
        conn.commit()

def transition_event_status(event_id, new_status, loss_reason=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM events WHERE id=%s", (event_id,))
        ev = cursor.fetchone()
        if not ev:
            return False, "Evento não encontrado"

        valid_transitions = {
            'lead': ['contacted', 'lost', 'cancelled'],
            'contacted': ['proposal_sent', 'negotiating', 'lost', 'cancelled'],
            'proposal_sent': ['negotiating', 'won', 'lost', 'cancelled'],
            'negotiating': ['won', 'lost', 'cancelled'],
            'won': ['cancelled'],
            'lost': ['lead'],
            'cancelled': ['lead'],
        }
        if new_status not in valid_transitions.get(ev['status'], []):
            return False, f"Transição inválida de '{ev['status']}' para '{new_status}'"

        updates = {'status': new_status, 'loss_reason': None}

        if new_status == 'lost':
            if not loss_reason or not loss_reason.strip():
                return False, "É obrigatório indicar o motivo de perda"
            updates['loss_reason'] = loss_reason.strip()

        if new_status == 'won':
            cursor.execute("SELECT COALESCE(SUM(total),0) FROM quote_items WHERE event_id=%s", (event_id,))
            total = cursor.fetchone()[0] or 0
            updates['invoice_amount_eur'] = float(total)
            if ev['event_date']:
                updates['expected_payment_date'] = ev['event_date'] + timedelta(days=30)
            else:
                updates['expected_payment_date'] = None
        else:
            updates['invoice_amount_eur'] = ev['invoice_amount_eur']
            updates['expected_payment_date'] = ev['expected_payment_date']

        updates['id'] = event_id
        cursor.execute("""
            UPDATE events SET status=%(status)s, loss_reason=%(loss_reason)s,
                invoice_amount_eur=%(invoice_amount_eur)s,
                expected_payment_date=%(expected_payment_date)s,
                updated_at=NOW()
            WHERE id=%(id)s
        """, updates)
        conn.commit()
    return True, "OK"

def delete_event(event_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM events WHERE id=%s", (event_id,))
        conn.commit()

# ── quote_items ────────────────────────────────────────────────────────────────

def get_quote_items(event_id):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM quote_items WHERE event_id=%s ORDER BY id", (event_id,))
        return cursor.fetchall()

def add_quote_item(event_id, artigo_codigo, descricao, quantidade, preco_unitario, taxa_iva=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO quote_items (event_id, artigo_codigo, descricao, quantidade, preco_unitario, taxa_iva)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (event_id, artigo_codigo, descricao, quantidade, preco_unitario, taxa_iva))
        conn.commit()

def update_quote_item(item_id, event_id, descricao, quantidade, preco_unitario, taxa_iva=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE quote_items SET descricao=%s, quantidade=%s, preco_unitario=%s, taxa_iva=%s
            WHERE id=%s AND event_id=%s
        """, (descricao, quantidade, preco_unitario, taxa_iva, item_id, event_id))
        conn.commit()

def delete_quote_item(item_id, event_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM quote_items WHERE id=%s AND event_id=%s", (item_id, event_id))
        conn.commit()

def recalc_event_invoice(event_id):
    """Recalculate and store invoice_amount_eur from quote_items (only for 'won' events)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, event_date FROM events WHERE id=%s", (event_id,))
        row = cursor.fetchone()
        if not row or row[0] != 'won':
            return
        cursor.execute("SELECT COALESCE(SUM(total),0) FROM quote_items WHERE event_id=%s", (event_id,))
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
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'lead',%s)
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
            cursor.execute("UPDATE lead_requests SET status='contacted', updated_at=NOW() WHERE id=%s", (lead_id,))
        conn.commit()
    return event_id

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

