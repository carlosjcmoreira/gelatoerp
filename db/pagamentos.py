"""M0b: Pagamentos, IVA e Liquidez Semanal — payment scheduling, VAT periods, weekly liquidity."""
from datetime import date as _date, timedelta
from psycopg2.extras import RealDictCursor
from db.connection import db_connection, get_connection, release_connection

VAT_RATES = {
    'pos_food': 0.06,
    'pos_beverages': 0.13,
    'events_catering': 0.13,
    'events_services': 0.23,
    'default_collected': 0.06,
    'default_supplier': 0.23,
}


def run_migrations_m0():
    """Idempotent migrations for M0a/M0b: Suppliers, Invoices, Payments, VAT.
    Uses an advisory lock to serialise concurrent worker executions."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT pg_try_advisory_lock(202603)")
        acquired = cursor.fetchone()[0]
        if not acquired:
            release_connection(conn)
            return

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS suppliers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                nif VARCHAR(20),
                categoria VARCHAR(100),
                store_id INTEGER REFERENCES stores(id) ON DELETE SET NULL,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(nif)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
                supplier_name VARCHAR(255) NOT NULL,
                supplier_nif VARCHAR(20),
                invoice_number VARCHAR(100),
                amount_eur NUMERIC(12,2) NOT NULL DEFAULT 0,
                vat_amount_eur NUMERIC(12,2) DEFAULT 0,
                issue_date DATE,
                due_date DATE,
                store_id INTEGER REFERENCES stores(id) ON DELETE SET NULL,
                categoria VARCHAR(100),
                status VARCHAR(50) NOT NULL DEFAULT 'pending_review',
                notes TEXT,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoices_due_date ON invoices(due_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoices_supplier ON invoices(supplier_id)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoice_payments (
                id SERIAL PRIMARY KEY,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                proposed_date DATE,
                confirmed_date DATE,
                paid_date DATE,
                amount_eur NUMERIC(12,2),
                status VARCHAR(50) NOT NULL DEFAULT 'proposed',
                confirmed_by VARCHAR(100),
                confirmed_at TIMESTAMP,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(invoice_id)
            )
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice ON invoice_payments(invoice_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoice_payments_status ON invoice_payments(status)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vat_periods (
                id SERIAL PRIMARY KEY,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                vat_collected_eur NUMERIC(12,2) DEFAULT 0,
                vat_deductible_eur NUMERIC(12,2) DEFAULT 0,
                vat_due_eur NUMERIC(12,2) DEFAULT 0,
                vat_collected_estimated NUMERIC(12,2) DEFAULT 0,
                vat_deductible_estimated NUMERIC(12,2) DEFAULT 0,
                vat_due_estimated NUMERIC(12,2) DEFAULT 0,
                declaration_date DATE,
                payment_date DATE,
                declaration_submitted_at DATE,
                payment_executed_at DATE,
                status VARCHAR(50) NOT NULL DEFAULT 'estimated',
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(year, month)
            )
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vat_periods_year_month ON vat_periods(year, month)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vat_periods_status ON vat_periods(status)")

        conn.commit()
    finally:
        try:
            cursor.execute("SELECT pg_advisory_unlock(202603)")
            conn.commit()
        except Exception:
            pass
        release_connection(conn)


def get_invoices_with_payments(status: str = None, store_id: int = None,
                                search: str = None, limit: int = 500) -> list:
    """Get invoices joined with their payment scheduling info from invoice_payments."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where = []
        params = []
        if status == 'overdue':
            where.append("i.status = 'scheduled' AND i.due_date < CURRENT_DATE")
        elif status:
            where.append("i.status = %s")
            params.append(status)
        if store_id:
            where.append("i.store_id = %s")
            params.append(store_id)
        if search:
            where.append("(LOWER(i.supplier_name) LIKE %s OR LOWER(i.invoice_number) LIKE %s)")
            s = f'%{search.lower()}%'
            params.extend([s, s])
        where_clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        params.append(limit)
        cursor.execute(f"""
            SELECT i.id, i.supplier_id, i.supplier_name, i.supplier_nif,
                   i.invoice_number, i.amount_eur, i.vat_amount_eur,
                   i.issue_date, i.due_date, i.store_id, i.category AS categoria,
                   i.status, i.notes, i.created_by, i.created_at,
                   st.name AS store_name,
                   ip.id AS payment_id, ip.proposed_date, ip.confirmed_date,
                   ip.paid_date AS payment_paid_date, ip.amount_eur AS payment_amount,
                   ip.status AS payment_status, ip.confirmed_by, ip.notes AS payment_notes,
                   COALESCE(i.document_type, 'fatura') AS document_type
            FROM invoices i
            LEFT JOIN stores st ON i.store_id = st.id
            LEFT JOIN invoice_payments ip ON ip.invoice_id = i.id
            {where_clause}
            ORDER BY i.due_date ASC NULLS LAST, i.created_at DESC
            LIMIT %s
        """, params)
        return cursor.fetchall()


def propose_invoice_payment(invoice_id, proposed_date, amount_eur):
    """Create or update the payment proposal for an invoice."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO invoice_payments (invoice_id, proposed_date, amount_eur, status)
            VALUES (%s, %s, %s, 'proposed')
            ON CONFLICT (invoice_id) DO UPDATE
                SET proposed_date = EXCLUDED.proposed_date,
                    amount_eur = EXCLUDED.amount_eur,
                    status = 'proposed',
                    updated_at = NOW()
        """, (invoice_id, proposed_date, amount_eur))
        cursor.execute(
            "UPDATE invoices SET status = 'scheduled', updated_at = NOW() "
            "WHERE id = %s AND status = 'pending_review'",
            (invoice_id,)
        )
        conn.commit()


def confirm_invoice_payment(invoice_id, confirmed_date, amount_eur, confirmed_by, notes=None):
    """Confirm payment date for an invoice."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO invoice_payments (invoice_id, confirmed_date, amount_eur, status,
                                          confirmed_by, confirmed_at, notes)
            VALUES (%s, %s, %s, 'confirmed', %s, NOW(), %s)
            ON CONFLICT (invoice_id) DO UPDATE
                SET confirmed_date = EXCLUDED.confirmed_date,
                    amount_eur = EXCLUDED.amount_eur,
                    status = 'confirmed',
                    confirmed_by = EXCLUDED.confirmed_by,
                    confirmed_at = NOW(),
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
        """, (invoice_id, confirmed_date, amount_eur, confirmed_by, notes))
        cursor.execute(
            "UPDATE invoices SET status = 'scheduled', updated_at = NOW() WHERE id = %s",
            (invoice_id,)
        )
        conn.commit()


def mark_payment_executed(invoice_id, paid_date, confirmed_by, notes=None):
    """Mark invoice payment as executed/paid (updates both invoices and invoice_payments)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        if notes is not None:
            cursor.execute(
                "UPDATE invoice_payments SET paid_date = %s, status = 'paid', "
                "notes = %s, updated_at = NOW() WHERE invoice_id = %s",
                (paid_date, notes, invoice_id)
            )
        else:
            cursor.execute(
                "UPDATE invoice_payments SET paid_date = %s, status = 'paid', updated_at = NOW() "
                "WHERE invoice_id = %s",
                (paid_date, invoice_id)
            )
        cursor.execute(
            "UPDATE invoices SET status = 'paid', paid_date = %s, updated_at = NOW() WHERE id = %s",
            (paid_date, invoice_id)
        )
        conn.commit()


def get_invoice_payments(status_filter=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where = ""
        params = []
        if status_filter:
            where = "WHERE ip.status = %s"
            params.append(status_filter)
        cursor.execute(f"""
            SELECT ip.*, i.supplier_name, i.invoice_number, i.amount_eur AS invoice_amount,
                   i.due_date, i.vat_amount_eur
            FROM invoice_payments ip
            JOIN invoices i ON i.id = ip.invoice_id
            {where}
            ORDER BY COALESCE(ip.confirmed_date, ip.proposed_date) ASC
        """, params)
        return cursor.fetchall()


def get_vat_periods(limit=24):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT * FROM vat_periods
            ORDER BY year DESC, month DESC
            LIMIT %s
        """, (limit,))
        return cursor.fetchall()


def get_vat_period(year, month):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT * FROM vat_periods WHERE year = %s AND month = %s",
            (year, month)
        )
        return cursor.fetchone()


def upsert_vat_period(year, month,
                      vat_collected_estimated=None, vat_deductible_estimated=None,
                      vat_due_estimated=None,
                      vat_collected_eur=None, vat_deductible_eur=None,
                      vat_due_eur=None,
                      status=None, notes=None,
                      declaration_submitted_at=None, payment_executed_at=None):
    """Insert or update a VAT period.

    Lifecycle:
    - On compute: write *_estimated columns only (status stays 'estimated').
    - On declare: write *_eur (real declared values) + declaration_submitted_at, status='declared'.
    - On pay: write payment_executed_at, status='paid'.
    """
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        decl_month = month + 2
        decl_year = year
        if decl_month > 12:
            decl_month -= 12
            decl_year += 1
        decl_date = _date(decl_year, decl_month, 10)
        pay_date = _date(decl_year, decl_month, 25)
        cursor.execute("""
            INSERT INTO vat_periods (year, month, declaration_date, payment_date, status)
            VALUES (%s, %s, %s, %s, 'estimated')
            ON CONFLICT (year, month) DO NOTHING
        """, (year, month, decl_date, pay_date))

        updates = {}
        if vat_collected_estimated is not None:
            updates['vat_collected_estimated'] = vat_collected_estimated
        if vat_deductible_estimated is not None:
            updates['vat_deductible_estimated'] = vat_deductible_estimated
        if vat_due_estimated is not None:
            updates['vat_due_estimated'] = vat_due_estimated
        if vat_collected_eur is not None:
            updates['vat_collected_eur'] = vat_collected_eur
        if vat_deductible_eur is not None:
            updates['vat_deductible_eur'] = vat_deductible_eur
        if vat_due_eur is not None:
            updates['vat_due_eur'] = vat_due_eur
        if status is not None:
            updates['status'] = status
        if notes is not None:
            updates['notes'] = notes
        if declaration_submitted_at is not None:
            updates['declaration_submitted_at'] = declaration_submitted_at
        if payment_executed_at is not None:
            updates['payment_executed_at'] = payment_executed_at

        if updates:
            set_parts = [f"{k} = %s" for k in updates]
            params = list(updates.values()) + [year, month]
            cursor.execute(
                f"UPDATE vat_periods SET {', '.join(set_parts)}, updated_at = NOW() "
                f"WHERE year = %s AND month = %s",
                params
            )
        conn.commit()
        cursor.execute(
            "SELECT * FROM vat_periods WHERE year = %s AND month = %s",
            (year, month)
        )
        return cursor.fetchone()


def compute_vat_period(year, month, rate_pos: float = None, rate_events: float = None):
    """Compute estimated VAT collected/deductible for a given month.

    Uses configurable VAT rates (defaults from VAT_RATES config).
    All values are estimates written to the *_estimated columns only.
    """
    if rate_pos is None:
        rate_pos = VAT_RATES['pos_food']
    if rate_events is None:
        rate_events = VAT_RATES['events_catering']

    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COALESCE(SUM(vat_amount_eur), 0)
            FROM invoices
            WHERE EXTRACT(YEAR FROM issue_date) = %s
              AND EXTRACT(MONTH FROM issue_date) = %s
              AND status IN ('paid', 'scheduled')
        """, (year, month))
        vat_deductible = float(cursor.fetchone()[0])

        cursor.execute("""
            SELECT COALESCE(SUM(qi.total), 0)
            FROM events e
            JOIN quote_items qi ON qi.event_id = e.id
            WHERE EXTRACT(YEAR FROM e.event_date) = %s
              AND EXTRACT(MONTH FROM e.event_date) = %s
              AND e.status = 'won'
        """, (year, month))
        events_base = float(cursor.fetchone()[0])
        vat_events = events_base * rate_events

        cursor.execute("""
            SELECT COALESCE(SUM(valor_euros), 0)
            FROM vendas
            WHERE EXTRACT(YEAR FROM data) = %s
              AND EXTRACT(MONTH FROM data) = %s
        """, (year, month))
        pos_base = float(cursor.fetchone()[0])
        vat_pos = pos_base * rate_pos

        vat_collected = vat_events + vat_pos
        vat_due = max(0, vat_collected - vat_deductible)

        return {
            'vat_collected_estimated': round(vat_collected, 2),
            'vat_deductible_estimated': round(vat_deductible, 2),
            'vat_due_estimated': round(vat_due, 2),
            'rate_pos': rate_pos,
            'rate_events': rate_events,
            'events_base': round(events_base, 2),
            'pos_base': round(pos_base, 2),
        }


def get_weekly_liquidity(weeks=8, exclude_invoice_id: int = None):
    """Return projected weekly cash flows for the next N weeks.

    Args:
        weeks: Number of weeks to project.
        exclude_invoice_id: If set, exclude this invoice's payment from outflows
                            (used by suggest_payment_date to avoid double-counting).
    """
    today = _date.today()
    monday = today - timedelta(days=today.weekday())
    result = []
    with db_connection() as conn:
        cursor = conn.cursor()
        for w in range(weeks):
            week_start = monday + timedelta(weeks=w)
            week_end = week_start + timedelta(days=6)

            excl_clause = "AND ip.invoice_id != %s" if exclude_invoice_id else ""
            excl_params = [exclude_invoice_id] if exclude_invoice_id else []
            cursor.execute(f"""
                SELECT COALESCE(SUM(COALESCE(ip.amount_eur, i.amount_eur)), 0)
                FROM invoice_payments ip
                JOIN invoices i ON i.id = ip.invoice_id
                WHERE COALESCE(ip.confirmed_date, ip.proposed_date) BETWEEN %s AND %s
                  AND ip.status IN ('proposed', 'confirmed')
                  {excl_clause}
            """, [week_start, week_end] + excl_params)
            outflows_invoices = float(cursor.fetchone()[0])

            cursor.execute("""
                SELECT COALESCE(SUM(prestacao_mensal), 0)
                FROM credit_contracts
                WHERE estado = 'ativo'
                  AND tipo != 'overdraft'
                  AND dia_debito BETWEEN %s AND %s
            """, (week_start.day, week_end.day))
            outflows_credit = float(cursor.fetchone()[0])

            cursor.execute("""
                SELECT COALESCE(SUM(
                    CASE
                        WHEN status = 'declared' AND vat_due_eur > 0 THEN vat_due_eur
                        ELSE COALESCE(vat_due_estimated, 0)
                    END
                ), 0)
                FROM vat_periods
                WHERE payment_date BETWEEN %s AND %s
                  AND status IN ('estimated', 'declared')
            """, (week_start, week_end))
            outflows_vat = float(cursor.fetchone()[0])

            cursor.execute("""
                SELECT COALESCE(SUM(invoice_amount_eur), 0)
                FROM events
                WHERE expected_payment_date BETWEEN %s AND %s
                  AND payment_status = 'pending'
                  AND status = 'won'
            """, (week_start, week_end))
            inflows_events = float(cursor.fetchone()[0])

            cursor.execute("""
                SELECT COALESCE(AVG(valor_euros), 0) * 7
                FROM vendas
                WHERE data >= %s
            """, (today - timedelta(days=28),))
            inflows_pos_est = float(cursor.fetchone()[0])

            total_out = outflows_invoices + outflows_credit + outflows_vat
            total_in = inflows_events + inflows_pos_est
            balance = total_in - total_out

            result.append({
                'week': w + 1,
                'week_start': week_start,
                'week_end': week_end,
                'inflows_pos': round(inflows_pos_est, 2),
                'inflows_events': round(inflows_events, 2),
                'total_in': round(total_in, 2),
                'outflows_invoices': round(outflows_invoices, 2),
                'outflows_credit': round(outflows_credit, 2),
                'outflows_vat': round(outflows_vat, 2),
                'total_out': round(total_out, 2),
                'balance': round(balance, 2),
                'is_critical': balance < 0,
            })
    return result


def suggest_payment_date(invoice_id: int, amount_eur: float, due_date=None, weeks_ahead: int = 8):
    """Suggest the earliest payment date that falls in a non-critical week.

    Algorithm:
    1. Build weekly liquidity projection (excluding this invoice if already scheduled).
    2. Start from the earlier of: due_date or today+3 days.
    3. Find the first week where balance - amount_eur >= 0.
    4. If no non-critical week is found, fall back to due_date.

    Returns: (suggested_date, is_critical_fallback, weekly_projection)
    """
    today = _date.today()
    monday = today - timedelta(days=today.weekday())

    weekly = get_weekly_liquidity(weeks=weeks_ahead, exclude_invoice_id=invoice_id)

    anchor = due_date if due_date else (today + timedelta(days=3))
    if anchor < today:
        anchor = today

    anchor_week_offset = max(0, (anchor - monday).days // 7)

    for w in weekly[anchor_week_offset:]:
        if w['balance'] - amount_eur >= 0:
            return w['week_start'], False, weekly

    return (due_date or anchor), True, weekly
