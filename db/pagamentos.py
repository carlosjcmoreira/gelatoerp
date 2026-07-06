"""M0b: Pagamentos, IVA e Liquidez Semanal — payment scheduling, VAT periods, weekly liquidity."""
from calendar import monthrange
from datetime import date as _date, timedelta
from psycopg2.extras import RealDictCursor
from db.connection import db_connection

_MESES_PT = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
             'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

_INVOICE_DOC_TYPES = frozenset({'fatura', 'nota_credito', 'nota_debito'})


def _require_supplier_for_invoice(cursor, invoice_id):
    """Shared guard: raise ValueError if an invoice-type document lacks a supplier_id.

    Must be called inside an open transaction before any status transition to
    scheduled/paid so that no payment path can bypass the supplier requirement.
    """
    cursor.execute(
        "SELECT supplier_id, document_type FROM invoices WHERE id = %s",
        (invoice_id,)
    )
    row = cursor.fetchone()
    if row and row[1] in _INVOICE_DOC_TYPES and not row[0]:
        raise ValueError(
            f'Não é possível processar pagamento: documento #{invoice_id} '
            f'(tipo {row[1]}) não tem fornecedor ligado.'
        )


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

            # Configurable VAT fallback rates — used only for sales rows imported
            # BEFORE real per-line IVA data was captured (see valor_sem_iva_euros
            # on vendas_detalhe). Confirmed with the user on 2026-07-05: the real
            # Ravagnan POS export ("Evolução de Vendas por Produto") contains a
            # "Valor Total S/IVA" column per product line, so real IVA collected
            # should be computed directly (Valor Total - Valor Total S/IVA)
            # instead of assumed from a fixed rate whenever that data is present.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS vat_config (
                    key VARCHAR(50) PRIMARY KEY,
                    rate NUMERIC(6,4) NOT NULL,
                    label VARCHAR(255),
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                INSERT INTO vat_config (key, rate, label) VALUES
                    ('rate_pos_fallback', 0.06, 'Vendas POS (fallback p/ dados sem IVA real)'),
                    ('rate_events_fallback', 0.13, 'Eventos/Catering (estimativa)')
                ON CONFLICT (key) DO NOTHING
            ''')

            cursor.execute(
                "ALTER TABLE vendas_detalhe ADD COLUMN IF NOT EXISTS valor_sem_iva_euros REAL"
            )

            conn.commit()
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202603)")
                conn.commit()
            except Exception:
                pass


def get_vat_config() -> dict:
    """Return the current configurable VAT fallback rates as {key: rate}."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, rate FROM vat_config")
        rows = cursor.fetchall()
    config = {k: float(v) for k, v in rows}
    config.setdefault('rate_pos_fallback', VAT_RATES['pos_food'])
    config.setdefault('rate_events_fallback', VAT_RATES['events_catering'])
    return config


def update_vat_config(rate_pos_fallback: float = None, rate_events_fallback: float = None):
    """Update the configurable VAT fallback rates (used only when real IVA data is unavailable)."""
    updates = []
    if rate_pos_fallback is not None:
        updates.append(('rate_pos_fallback', rate_pos_fallback))
    if rate_events_fallback is not None:
        updates.append(('rate_events_fallback', rate_events_fallback))
    if not updates:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        for key, rate in updates:
            cursor.execute('''
                INSERT INTO vat_config (key, rate) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET rate = EXCLUDED.rate, updated_at = NOW()
            ''', (key, rate))
        conn.commit()


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
        _require_supplier_for_invoice(cursor, invoice_id)
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
        _require_supplier_for_invoice(cursor, invoice_id)
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
        _require_supplier_for_invoice(cursor, invoice_id)
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
    """Compute VAT collected/deductible for a given month.

    IVA dedutível is always real (sum of ``vat_amount_eur`` recorded on paid/scheduled
    supplier invoices).

    IVA liquidado (collected) from POS sales uses REAL per-line IVA whenever it is
    available: sales rows imported since the "Valor Total S/IVA" column was added to
    the import (see ``valor_sem_iva_euros`` on ``vendas_detalhe``) have their exact
    IVA amount computed as ``valor_euros - valor_sem_iva_euros``. Only rows imported
    before that (``valor_sem_iva_euros IS NULL``) fall back to an estimated rate
    (``rate_pos``, configurable via ``vat_config`` / the IVA settings screen).

    Eventos/Catering has REAL per-line IVA whenever the quote item was created (or
    edited) with a ``taxa_iva`` selected on the "Orçamento" screen — the amount is
    computed as ``total * taxa_iva`` for those lines. Older quote items created
    before this field existed have ``taxa_iva IS NULL`` and fall back to the
    configurable estimated rate (``rate_events``).

    All values are written to the *_estimated columns; ``is_estimate`` tells the
    caller whether the collected figure is fully real or partially/fully estimated.
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
            SELECT
                COALESCE(SUM(qi.total * qi.taxa_iva) FILTER (WHERE qi.taxa_iva IS NOT NULL), 0) AS vat_events_real,
                COALESCE(SUM(qi.total) FILTER (WHERE qi.taxa_iva IS NOT NULL), 0) AS events_base_real,
                COALESCE(SUM(qi.total) FILTER (WHERE qi.taxa_iva IS NULL), 0) AS events_base_fallback
            FROM events e
            JOIN quote_items qi ON qi.event_id = e.id
            WHERE EXTRACT(YEAR FROM e.event_date) = %s
              AND EXTRACT(MONTH FROM e.event_date) = %s
              AND e.status = 'won'
        """, (year, month))
        vat_events_real, events_base_real, events_base_fallback = (float(v) for v in cursor.fetchone())
        vat_events_fallback = events_base_fallback * rate_events
        vat_events = vat_events_real + vat_events_fallback
        events_base = events_base_real + events_base_fallback

        cursor.execute("""
            SELECT
                COALESCE(SUM(valor_euros - valor_sem_iva_euros)
                         FILTER (WHERE valor_sem_iva_euros IS NOT NULL), 0) AS vat_pos_real,
                COALESCE(SUM(valor_euros) FILTER (WHERE valor_sem_iva_euros IS NOT NULL), 0) AS base_real,
                COALESCE(SUM(valor_euros) FILTER (WHERE valor_sem_iva_euros IS NULL), 0) AS base_fallback
            FROM vendas_detalhe
            WHERE EXTRACT(YEAR FROM data) = %s
              AND EXTRACT(MONTH FROM data) = %s
        """, (year, month))
        vat_pos_real, pos_base_real, pos_base_fallback = (float(v) for v in cursor.fetchone())
        vat_pos_fallback = pos_base_fallback * rate_pos
        vat_pos = vat_pos_real + vat_pos_fallback
        pos_base = pos_base_real + pos_base_fallback

        vat_collected = vat_events + vat_pos
        vat_due = max(0, vat_collected - vat_deductible)

        is_estimate = pos_base_fallback > 0.005 or events_base_fallback > 0.005

        return {
            'vat_collected_estimated': round(vat_collected, 2),
            'vat_deductible_estimated': round(vat_deductible, 2),
            'vat_due_estimated': round(vat_due, 2),
            'rate_pos': rate_pos,
            'rate_events': rate_events,
            'events_base': round(events_base, 2),
            'events_base_real': round(events_base_real, 2),
            'events_base_fallback': round(events_base_fallback, 2),
            'pos_base': round(pos_base, 2),
            'pos_base_real': round(pos_base_real, 2),
            'pos_base_fallback': round(pos_base_fallback, 2),
            'is_estimate': is_estimate,
        }


def run_migrations_tesouraria_manuais():
    """Idempotent: create tesouraria_entradas_manuais table for manual Eventos/B2B inflow entries."""
    import logging as _log
    import psycopg2.errors as _pgerr
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tesouraria_entradas_manuais (
                    tipo          VARCHAR(50)   NOT NULL,
                    semana_inicio DATE          NOT NULL,
                    valor         NUMERIC(12,2) NOT NULL DEFAULT 0,
                    updated_at    TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tipo, semana_inicio)
                )
            """)
            conn.commit()
    except (_pgerr.DuplicateTable, _pgerr.UniqueViolation) as _exc:
        _log.getLogger(__name__).info(
            'run_migrations_tesouraria_manuais: table already exists, skipping: %s', _exc
        )
    except Exception as _exc:
        _log.getLogger(__name__).error(
            'run_migrations_tesouraria_manuais: unexpected error: %s', _exc
        )
        raise


def get_tesouraria_manuais(week_starts: list) -> dict:
    """Return manual Tesouraria entries indexed by tipo → week_start_date → valor.

    Args:
        week_starts: list of date objects for the week start dates to query.
    Returns:
        {tipo: {week_start_date: valor_float}}
    """
    if not week_starts:
        return {}
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tipo, semana_inicio, valor
                FROM tesouraria_entradas_manuais
                WHERE semana_inicio = ANY(%s)
            """, (week_starts,))
            rows = cursor.fetchall()
        result: dict = {}
        for tipo, semana, valor in rows:
            result.setdefault(tipo, {})[semana] = float(valor)
        return result
    except Exception:
        return {}


def set_tesouraria_manual(tipo: str, semana_inicio, valor: float):
    """Upsert a manual Tesouraria Previsional entry (Eventos or B2B)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tesouraria_entradas_manuais (tipo, semana_inicio, valor, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (tipo, semana_inicio) DO UPDATE
            SET valor = EXCLUDED.valor, updated_at = NOW()
        """, (tipo, semana_inicio, round(valor, 2)))
        conn.commit()


def get_overdue_unscheduled_invoices() -> dict:
    """Return count and total of invoices that are overdue and have no scheduled/confirmed payment."""
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) AS cnt, COALESCE(SUM(i.amount_eur), 0) AS total
                FROM invoices i
                WHERE i.status IN ('pending_review', 'scheduled')
                  AND i.due_date < CURRENT_DATE
                  AND NOT EXISTS (
                      SELECT 1 FROM invoice_payments ip
                      WHERE ip.invoice_id = i.id
                        AND ip.status IN ('proposed', 'confirmed')
                  )
            """)
            row = cursor.fetchone()
        return {'count': int(row[0]), 'total': float(row[1])}
    except Exception:
        return {'count': 0, 'total': 0.0}


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

        excl_clause_sched = "AND ip.invoice_id != %s" if exclude_invoice_id else ""
        excl_clause_unsched = "AND i.id != %s" if exclude_invoice_id else ""
        excl_params = [exclude_invoice_id] if exclude_invoice_id else []
        cursor.execute(f"""
            SELECT
                i.id              AS invoice_id,
                i.supplier_name,
                i.invoice_number,
                COALESCE(ip.amount_eur, i.amount_eur) AS amount,
                COALESCE(ip.confirmed_date, ip.proposed_date) AS payment_date,
                cc.id             AS category_id,
                COALESCE(cc.name, 'Sem categoria de custo') AS category_name,
                'scheduled'       AS source
            FROM invoice_payments ip
            JOIN invoices i ON i.id = ip.invoice_id
            LEFT JOIN cost_categories cc ON cc.id = i.categoria_custo_id
            WHERE COALESCE(ip.confirmed_date, ip.proposed_date) BETWEEN %s AND %s
              AND ip.status IN ('proposed', 'confirmed')
              {excl_clause_sched}

            UNION ALL

            SELECT
                i.id              AS invoice_id,
                i.supplier_name,
                i.invoice_number,
                i.amount_eur      AS amount,
                i.due_date        AS payment_date,
                cc.id             AS category_id,
                COALESCE(cc.name, 'Sem categoria de custo') AS category_name,
                'due_date_fallback' AS source
            FROM invoices i
            LEFT JOIN cost_categories cc ON cc.id = i.categoria_custo_id
            WHERE i.due_date BETWEEN %s AND %s
              AND i.status NOT IN ('paid', 'cancelled')
              AND NOT EXISTS (
                  SELECT 1 FROM invoice_payments ip2
                  WHERE ip2.invoice_id = i.id
                    AND ip2.status IN ('proposed', 'confirmed')
              )
              {excl_clause_unsched}

            ORDER BY category_name NULLS LAST, supplier_name
        """, [range_start, range_end] + excl_params + [range_start, range_end] + excl_params)
        invoice_rows = cursor.fetchall()

        cursor.execute("""
            SELECT cc.id, cc.label, cc.banco, cc.tipo, cc.prestacao_mensal, cc.dia_debito,
                   cc.categoria_custo_id,
                   COALESCE(cat.name, 'Sem categoria de custo') AS categoria_custo_nome
            FROM credit_contracts cc
            LEFT JOIN cost_categories cat ON cat.id = cc.categoria_custo_id
            WHERE cc.estado = 'ativo' AND cc.tipo != 'overdraft'
            ORDER BY cc.label
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

        # Primary: vendas_detalhe (Gestor uploads) for past dates
        cursor.execute("""
            SELECT data, SUM(valor_euros) AS daily_total
            FROM vendas_detalhe
            WHERE data >= %s AND data < %s
            GROUP BY data
        """, (range_start, today))
        _vendas_reais_by_date = {r['data']: float(r['daily_total']) for r in cursor.fetchall()}

        # Fallback: vendas table for past dates not covered by vendas_detalhe
        cursor.execute("""
            SELECT data, SUM(valor_euros) AS daily_total
            FROM vendas
            WHERE data >= %s AND data < %s
            GROUP BY data
        """, (range_start, today))
        for r in cursor.fetchall():
            if r['data'] not in _vendas_reais_by_date:
                _vendas_reais_by_date[r['data']] = float(r['daily_total'])

    # ── Forecast-based per-week POS estimates ───────────────────────────────
    # Call get_previsao_30dias() and build a per-date lookup of est_total.
    # For each liquidity week, blend: days covered by the forecast use est_total;
    # days beyond the 30-day horizon use inflows_pos_fallback / 7 (daily 28d avg).
    # Weeks with no forecast coverage at all stay as 'media_28d'.
    _forecast_by_date: dict = {}
    try:
        from db.vendas_diarias import get_previsao_30dias as _get_forecast
        _fc = _get_forecast()
        for _f in _fc.get('forecast', []):
            _d = _date.fromisoformat(_f['date'])
            _est = _f.get('est_total')
            if _est is not None:
                _forecast_by_date[_d] = _est
    except Exception as _fc_err:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            'get_weekly_liquidity: forecast unavailable, using 28d fallback for all weeks: %s',
            _fc_err,
        )

    _DOW_PT = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']

    def _pos_for_week(ws, we):
        """Return (amount, fonte, daily) for a Mon-Sun week blending real/forecast/28d fallback.

        For each of the 7 days in [ws, we]:
          - if the day is past and has real vendas_detalhe data → use it (fonte='real')
          - elif the day has a forecast estimate → use est_total (fonte='previsao')
          - otherwise → use inflows_pos_fallback / 7 (daily 28d avg, fonte='media_28d')
        Week-level fonte: 'real' if any real day, 'previsao' if any forecast day, else 'media_28d'.
        daily is a list of {date, label, amount, fonte} for drill-down.
        """
        daily_fallback = inflows_pos_fallback / 7.0
        total = 0.0
        real_days = 0
        forecast_days = 0
        daily = []
        cur = ws
        while cur <= we:
            if cur < today and cur in _vendas_reais_by_date:
                amt = round(_vendas_reais_by_date[cur], 2)
                fonte_d = 'real'
                real_days += 1
            elif cur in _forecast_by_date:
                amt = round(_forecast_by_date[cur], 2)
                fonte_d = 'previsao'
                forecast_days += 1
            else:
                amt = round(daily_fallback, 2)
                fonte_d = 'media_28d'
            total += amt
            daily.append({
                'date': cur,
                'label': f"{_DOW_PT[cur.weekday()]} {cur.strftime('%d/%m')}",
                'amount': amt,
                'fonte': fonte_d,
            })
            cur += timedelta(days=1)
        if real_days > 0:
            fonte = 'real'
        elif forecast_days > 0:
            fonte = 'previsao'
        else:
            fonte = 'media_28d'
        return round(total, 2), fonte, daily

    from db.avencas import get_avencas as _get_avencas, next_due_date as _avenca_next_due_date
    avencas_ativas = list(_get_avencas(ativo_only=True))

    _manuais = get_tesouraria_manuais(week_starts)

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
                'invoice_id': row['invoice_id'],
                'source': row.get('source', 'scheduled'),
            })

        for av in avencas_ativas:
            due = _avenca_next_due_date(av, week_start)
            if due is None or not (week_start <= due <= week_end):
                continue
            cid = av['categoria_custo_id']
            cname = av['categoria_custo_nome'] or 'Sem categoria de custo'
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

        for cc in credit_contracts:
            debit_date = _credit_debit_date_in_week(int(cc['dia_debito'] or 1), week_start, week_end)
            if debit_date is None:
                continue
            cid = cc['categoria_custo_id']
            cname = cc['categoria_custo_nome'] or 'Sem categoria de custo'
            if cid not in cats:
                cats[cid] = {'category_id': cid, 'category_name': cname, 'amount': 0.0, 'items': []}
            prestacao = float(cc['prestacao_mensal'] or 0)
            cats[cid]['amount'] = round(cats[cid]['amount'] + prestacao, 2)
            label = cc['label'] or ''
            banco = cc['banco'] or ''
            description = f"{label} – {banco}" if banco and banco not in label else label
            cats[cid]['items'].append({
                'type': 'credito',
                'description': description or '(contrato)',
                'reference': str(cc['tipo'] or ''),
                'amount': round(prestacao, 2),
                'date': debit_date,
            })

        outflows_by_category = sorted(
            cats.values(),
            key=lambda c: (c['category_id'] is None, (c['category_name'] or '').lower()),
        )
        outflows_invoices = sum(c['amount'] for c in outflows_by_category)

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

        inflows_pos_week, vendas_fonte, pos_daily = _pos_for_week(week_start, week_end)

        inflows_eventos_manual = round(_manuais.get('eventos', {}).get(week_start, 0.0), 2)
        inflows_b2b_manual = round(_manuais.get('b2b', {}).get(week_start, 0.0), 2)

        total_out = round(outflows_invoices + outflows_vat, 2)
        total_in = round(
            inflows_events + inflows_pos_week + inflows_eventos_manual + inflows_b2b_manual, 2
        )
        balance = round(total_in - total_out, 2)

        result.append({
            'week': week_num,
            'week_start': week_start,
            'week_end': week_end,
            'outflows_by_category': outflows_by_category,
            'outflows_invoices': round(outflows_invoices, 2),
            'outflows_vat': round(outflows_vat, 2),
            'outflows_vat_items': vat_items,
            'inflows_pos': round(inflows_pos_week, 2),
            'vendas_fonte': vendas_fonte,
            'pos_daily': pos_daily,
            'inflows_events': round(inflows_events, 2),
            'inflows_eventos_manual': inflows_eventos_manual,
            'inflows_b2b_manual': inflows_b2b_manual,
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
