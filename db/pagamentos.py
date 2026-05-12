"""M0b: Pagamentos, IVA e Liquidez Semanal — payment scheduling, VAT periods, weekly liquidity."""
from calendar import monthrange
from datetime import date as _date, timedelta
from psycopg2.extras import RealDictCursor
from db.connection import db_connection

_MESES_PT = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
             'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']


def _credit_debit_date_in_week(dia_debito: int, week_start: _date, week_end: _date):
    """Return the concrete debit date if the contract falls in this week, else None.

    Fixes the bug where 'dia_debito BETWEEN week_start.day AND week_end.day'
    silently fails for weeks that cross a month boundary (e.g. 28 Apr – 4 May).
    Checks all months that the week touches.
    """
    months = {(week_start.year, week_start.month)}
    if week_end.month != week_start.month or week_end.year != week_start.year:
        months.add((week_end.year, week_end.month))
    for yr, mo in months:
        last_day = monthrange(yr, mo)[1]
        actual_day = min(dia_debito, last_day)
        debit_date = _date(yr, mo, actual_day)
        if week_start <= debit_date <= week_end:
            return debit_date
    return None

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
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202603)")
            acquired = cursor.fetchone()[0]
            if not acquired:
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


def confirm_invoice_payment(invoice_id, confirmed_date, amount_eur, confirmed_by, notes=None,
                            payment_method=None, confirming_contract_id=None):
    """Confirm payment date for an invoice."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO invoice_payments (invoice_id, confirmed_date, amount_eur, status,
                                          confirmed_by, confirmed_at, notes, payment_method, confirming_contract_id)
            VALUES (%s, %s, %s, 'confirmed', %s, NOW(), %s, %s, %s)
            ON CONFLICT (invoice_id) DO UPDATE
                SET confirmed_date = EXCLUDED.confirmed_date,
                    amount_eur = EXCLUDED.amount_eur,
                    status = 'confirmed',
                    confirmed_by = EXCLUDED.confirmed_by,
                    confirmed_at = NOW(),
                    notes = EXCLUDED.notes,
                    payment_method = COALESCE(EXCLUDED.payment_method, invoice_payments.payment_method),
                    confirming_contract_id = COALESCE(EXCLUDED.confirming_contract_id, invoice_payments.confirming_contract_id),
                    updated_at = NOW()
        """, (invoice_id, confirmed_date, amount_eur, confirmed_by, notes,
              payment_method or None, confirming_contract_id or None))
        cursor.execute(
            "UPDATE invoices SET status = 'scheduled', updated_at = NOW() WHERE id = %s",
            (invoice_id,)
        )
        conn.commit()


def mark_payment_executed(invoice_id, paid_date, confirmed_by, notes=None,
                          payment_method=None, confirming_contract_id=None):
    """Mark invoice payment as executed/paid (upserts invoice_payments, updates invoices)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO invoice_payments
                   (invoice_id, paid_date, status, confirmed_by, notes, payment_method, confirming_contract_id, updated_at)
               VALUES (%s, %s, 'paid', %s, %s, %s, %s, NOW())
               ON CONFLICT (invoice_id) DO UPDATE SET
                   paid_date = EXCLUDED.paid_date,
                   status = 'paid',
                   notes = COALESCE(EXCLUDED.notes, invoice_payments.notes),
                   payment_method = COALESCE(EXCLUDED.payment_method, invoice_payments.payment_method),
                   confirming_contract_id = COALESCE(EXCLUDED.confirming_contract_id, invoice_payments.confirming_contract_id),
                   updated_at = NOW()""",
            (invoice_id, paid_date, confirmed_by, notes, payment_method, confirming_contract_id)
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
            FROM vendas_detalhe
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


def get_weekly_liquidity(weeks=6, exclude_invoice_id: int = None):
    """Return projected weekly cash flows for the next N weeks.

    Returns enriched per-category data with individual item detail for drill-down.
    Also keeps top-level total fields (total_in, total_out, balance) for backward
    compatibility with suggest_payment_date() and the pagamentos index mini-table.

    Args:
        weeks: Number of weeks to project (default 6).
        exclude_invoice_id: If set, exclude this invoice's payment from outflows
                            (used by suggest_payment_date to avoid double-counting).
    """
    today = _date.today()
    monday = today - timedelta(days=today.weekday())
    week_starts = [monday + timedelta(weeks=w) for w in range(weeks)]
    week_ends = [ws + timedelta(days=6) for ws in week_starts]
    range_start = week_starts[0]
    range_end = week_ends[-1]

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        excl_clause = "AND ip.invoice_id != %s" if exclude_invoice_id else ""
        excl_params = [exclude_invoice_id] if exclude_invoice_id else []
        cursor.execute(f"""
            SELECT
                i.id              AS invoice_id,
                i.supplier_name,
                i.invoice_number,
                COALESCE(ip.amount_eur, i.amount_eur) AS amount,
                COALESCE(ip.confirmed_date, ip.proposed_date) AS payment_date,
                cc.id             AS category_id,
                COALESCE(cc.name, 'Sem categoria') AS category_name
            FROM invoice_payments ip
            JOIN invoices i ON i.id = ip.invoice_id
            LEFT JOIN cost_categories cc ON cc.id = i.categoria_custo_id
            WHERE COALESCE(ip.confirmed_date, ip.proposed_date) BETWEEN %s AND %s
              AND ip.status IN ('proposed', 'confirmed')
              {excl_clause}
            ORDER BY cc.name NULLS LAST, i.supplier_name
        """, [range_start, range_end] + excl_params)
        invoice_rows = cursor.fetchall()

        cursor.execute("""
            SELECT id, label, banco, tipo, prestacao_mensal, dia_debito
            FROM credit_contracts
            WHERE estado = 'ativo' AND tipo != 'overdraft'
            ORDER BY label
        """)
        credit_contracts = cursor.fetchall()

        cursor.execute("""
            SELECT year, month, payment_date,
                   CASE
                       WHEN status = 'declared' AND vat_due_eur > 0 THEN vat_due_eur
                       ELSE COALESCE(vat_due_estimated, 0)
                   END AS amount
            FROM vat_periods
            WHERE payment_date BETWEEN %s AND %s
              AND status IN ('estimated', 'declared')
            ORDER BY payment_date
        """, (range_start, range_end))
        vat_rows = cursor.fetchall()

        cursor.execute("""
            SELECT expected_payment_date, COALESCE(SUM(invoice_amount_eur), 0) AS total
            FROM events
            WHERE expected_payment_date BETWEEN %s AND %s
              AND payment_status = 'pending'
              AND status = 'won'
            GROUP BY expected_payment_date
        """, (range_start, range_end))
        event_rows = {r['expected_payment_date']: float(r['total']) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT COALESCE(AVG(daily_total), 0) * 7 AS weekly_pos
            FROM (
                SELECT data, SUM(valor_euros) AS daily_total
                FROM vendas_detalhe
                WHERE data >= %s
                GROUP BY data
            ) sub
        """, (today - timedelta(days=28),))
        inflows_pos_fallback = float(cursor.fetchone()['weekly_pos'])

    # ── Forecast-based per-week POS estimates ───────────────────────────────
    # Call get_previsao_30dias() and aggregate est_total by date.
    # For each liquidity week, sum forecast estimates for days in [week_start, week_end].
    # Scale to 7 days when only partial coverage exists.
    # Fall back to inflows_pos_fallback (28d rolling average) when no forecast data.
    _forecast_by_date: dict = {}
    try:
        from db.vendas_diarias import get_previsao_30dias as _get_forecast
        _fc = _get_forecast()
        for _f in _fc.get('forecast', []):
            _d = _date.fromisoformat(_f['date'])
            _est = _f.get('est_total')
            if _est is not None:
                _forecast_by_date[_d] = _est
    except Exception:
        pass  # silent fallback: all weeks use 28d average

    def _pos_for_week(ws, we):
        """Return (amount, fonte) for a Mon-Sun week blending forecast and 28d fallback.

        For each of the 7 days in [ws, we]:
          - if the day has a forecast estimate, use est_total for that day
          - otherwise use inflows_pos_fallback / 7 (daily 28d average)
        fonte is 'previsao' if at least one day used the forecast, else 'media_28d'.
        This avoids any synthetic scaling and correctly handles horizon-boundary weeks.
        """
        daily_fallback = inflows_pos_fallback / 7.0
        total = 0.0
        forecast_days = 0
        cur = ws
        while cur <= we:
            if cur in _forecast_by_date:
                total += _forecast_by_date[cur]
                forecast_days += 1
            else:
                total += daily_fallback
            cur += timedelta(days=1)
        fonte = 'previsao' if forecast_days > 0 else 'media_28d'
        return round(total, 2), fonte

    from db.avencas import get_avencas as _get_avencas, next_due_date as _avenca_next_due_date
    avencas_ativas = list(_get_avencas(ativo_only=True))

    result = []
    for w in range(weeks):
        week_start = week_starts[w]
        week_end = week_ends[w]
        week_num = w + 1

        cats: dict = {}

        for row in invoice_rows:
            pd = row['payment_date']
            if pd is None or not (week_start <= pd <= week_end):
                continue
            cid = row['category_id']
            cname = row['category_name']
            if cid not in cats:
                cats[cid] = {'category_id': cid, 'category_name': cname, 'amount': 0.0, 'items': []}
            amt = float(row['amount'] or 0)
            cats[cid]['amount'] = round(cats[cid]['amount'] + amt, 2)
            ref = row['invoice_number'] or ''
            cats[cid]['items'].append({
                'type': 'fatura',
                'description': row['supplier_name'] or '(sem fornecedor)',
                'reference': ref,
                'amount': round(amt, 2),
                'date': pd,
            })

        for av in avencas_ativas:
            due = _avenca_next_due_date(av, week_start)
            if due is None or not (week_start <= due <= week_end):
                continue
            cid = av['categoria_custo_id']
            cname = av['categoria_custo_nome'] or 'Sem categoria'
            if cid not in cats:
                cats[cid] = {'category_id': cid, 'category_name': cname, 'amount': 0.0, 'items': []}
            amt = float(av['valor'] or 0)
            cats[cid]['amount'] = round(cats[cid]['amount'] + amt, 2)
            cats[cid]['items'].append({
                'type': 'avenca',
                'description': av['nome'],
                'reference': av['periodicidade'].capitalize() if av['periodicidade'] else '',
                'amount': round(amt, 2),
                'date': due,
            })

        outflows_by_category = sorted(
            cats.values(),
            key=lambda c: (c['category_id'] is None, (c['category_name'] or '').lower()),
        )
        outflows_invoices = sum(c['amount'] for c in outflows_by_category)

        credit_items = []
        outflows_credit = 0.0
        for cc in credit_contracts:
            debit_date = _credit_debit_date_in_week(int(cc['dia_debito'] or 1), week_start, week_end)
            if debit_date is None:
                continue
            prestacao = float(cc['prestacao_mensal'] or 0)
            outflows_credit = round(outflows_credit + prestacao, 2)
            label = cc['label'] or ''
            banco = cc['banco'] or ''
            description = f"{label} – {banco}" if banco and banco not in label else label
            credit_items.append({
                'type': 'credito',
                'description': description or '(contrato)',
                'reference': str(cc['tipo'] or ''),
                'amount': round(prestacao, 2),
                'date': debit_date,
            })

        vat_items = []
        outflows_vat = 0.0
        for vr in vat_rows:
            if vr['payment_date'] is None:
                continue
            vpd = vr['payment_date']
            if not (week_start <= vpd <= week_end):
                continue
            vamt = float(vr['amount'] or 0)
            if vamt <= 0:
                continue
            outflows_vat = round(outflows_vat + vamt, 2)
            mes = _MESES_PT[int(vr['month'])] if vr['month'] else '?'
            vat_items.append({
                'type': 'iva',
                'description': f"IVA {mes} {vr['year']}",
                'reference': '',
                'amount': round(vamt, 2),
                'date': vpd,
            })

        inflows_events = sum(
            v for d, v in event_rows.items()
            if week_start <= d <= week_end
        )

        inflows_pos_week, vendas_fonte = _pos_for_week(week_start, week_end)

        total_out = round(outflows_invoices + outflows_credit + outflows_vat, 2)
        total_in = round(inflows_events + inflows_pos_week, 2)
        balance = round(total_in - total_out, 2)

        result.append({
            'week': week_num,
            'week_start': week_start,
            'week_end': week_end,
            'outflows_by_category': outflows_by_category,
            'outflows_invoices': round(outflows_invoices, 2),
            'outflows_credit': round(outflows_credit, 2),
            'outflows_credit_items': credit_items,
            'outflows_vat': round(outflows_vat, 2),
            'outflows_vat_items': vat_items,
            'inflows_pos': round(inflows_pos_week, 2),
            'vendas_fonte': vendas_fonte,
            'inflows_events': round(inflows_events, 2),
            'total_in': total_in,
            'total_out': total_out,
            'balance': balance,
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
