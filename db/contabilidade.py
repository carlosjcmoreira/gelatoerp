"""Database functions for the Contabilidade module.

Covers:
- Invoice listing with accounting_status columns
- Updating accounting_status (single + bulk)
- Summary dashboard stats
- Tickets (contabilidade_tickets + contabilidade_ticket_respostas)
"""
import logging
from datetime import date, datetime
from db.connection import db_connection

logger = logging.getLogger(__name__)

ACCOUNTING_STATUS_LABELS = {
    'por_contabilizar': 'Por Contabilizar',
    'contabilizado':    'Contabilizado',
    'excluido':         'Excluído',
}

ACCOUNTING_STATUS_BADGE = {
    'por_contabilizar': 'bg-warning text-dark',
    'contabilizado':    'bg-success text-white',
    'excluido':         'bg-secondary text-white',
}

TICKET_STATUS_LABELS = {
    'aberto':    'Aberto',
    'em_curso':  'Em curso',
    'resolvido': 'Resolvido',
}

TICKET_STATUS_BADGE = {
    'aberto':    'bg-danger text-white',
    'em_curso':  'bg-warning text-dark',
    'resolvido': 'bg-success text-white',
}


# ── Invoice listing ───────────────────────────────────────────────────────────

def _build_cont_where(
    supplier_name: str = None,
    document_type: str = None,
    accounting_status: str = None,
    date_from=None,
    date_to=None,
    search: str = None,
):
    """Build WHERE clause for contabilidade invoice queries."""
    clauses = ["i.status NOT IN ('draft', 'cancelled')"]
    params = []

    if supplier_name:
        clauses.append("LOWER(i.supplier_name) LIKE LOWER(%s)")
        params.append(f'%{supplier_name}%')

    if document_type:
        clauses.append("i.document_type = %s")
        params.append(document_type)

    if accounting_status:
        if accounting_status == 'por_contabilizar':
            clauses.append("(i.accounting_status = 'por_contabilizar' OR i.accounting_status IS NULL)")
        else:
            clauses.append("i.accounting_status = %s")
            params.append(accounting_status)

    if date_from:
        clauses.append("i.issue_date >= %s")
        params.append(date_from)

    if date_to:
        clauses.append("i.issue_date <= %s")
        params.append(date_to)

    if search:
        clauses.append(
            "(i.invoice_number ILIKE %s OR i.supplier_name ILIKE %s OR i.notes ILIKE %s)"
        )
        pattern = f'%{search}%'
        params.extend([pattern, pattern, pattern])

    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    return where, params


def get_cont_invoices(
    supplier_name: str = None,
    document_type: str = None,
    accounting_status: str = None,
    date_from=None,
    date_to=None,
    search: str = None,
    order_by: str = 'issue_date',
    order_dir: str = 'desc',
    limit: int = 50,
    offset: int = 0,
) -> list:
    """Return invoices with accounting columns for the contabilidade module."""
    _VALID_ORDER = {'issue_date', 'supplier_name', 'amount_eur', 'accounting_status', 'status'}
    if order_by not in _VALID_ORDER:
        order_by = 'issue_date'
    if order_dir not in ('asc', 'desc'):
        order_dir = 'desc'

    where, params = _build_cont_where(
        supplier_name=supplier_name,
        document_type=document_type,
        accounting_status=accounting_status,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )

    sql = f"""
        SELECT
            i.id,
            i.supplier_id,
            i.supplier_name,
            s.nif        AS supplier_nif,
            i.invoice_number,
            i.document_type,
            i.amount_eur,
            i.vat_amount_eur,
            i.issue_date,
            i.status,
            i.paid_date,
            COALESCE(i.accounting_status, 'por_contabilizar') AS accounting_status,
            i.accounting_notes,
            i.accounting_updated_by,
            i.accounting_updated_at,
            i.notes,
            (i.pdf_data IS NOT NULL) AS has_pdf
        FROM invoices i
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        {where}
        ORDER BY i.{order_by} {order_dir} NULLS LAST
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

    result = []
    for row in rows:
        result.append({
            'id':                   row[0],
            'supplier_id':          row[1],
            'supplier_name':        row[2],
            'supplier_nif':         row[3],
            'invoice_number':       row[4],
            'document_type':        row[5],
            'amount_eur':           row[6],
            'vat_amount_eur':       row[7],
            'issue_date':           row[8],
            'status':               row[9],
            'paid_date':            row[10],
            'accounting_status':    row[11],
            'accounting_notes':     row[12],
            'accounting_updated_by':row[13],
            'accounting_updated_at':row[14],
            'notes':                row[15],
            'has_pdf':              bool(row[16]),
            'accounting_status_label': ACCOUNTING_STATUS_LABELS.get(row[11], row[11]),
            'accounting_status_badge': ACCOUNTING_STATUS_BADGE.get(row[11], 'bg-secondary'),
        })
    return result


def count_cont_invoices(
    supplier_name: str = None,
    document_type: str = None,
    accounting_status: str = None,
    date_from=None,
    date_to=None,
    search: str = None,
) -> int:
    where, params = _build_cont_where(
        supplier_name=supplier_name,
        document_type=document_type,
        accounting_status=accounting_status,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )
    sql = f"SELECT COUNT(*) FROM invoices i {where}"
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()[0] or 0


def get_cont_summary() -> dict:
    """Return summary stats for the dashboard cards."""
    today = date.today()
    sql = """
        SELECT
            COUNT(*) FILTER (WHERE COALESCE(accounting_status,'por_contabilizar') = 'por_contabilizar') AS por_contabilizar_count,
            COALESCE(SUM(amount_eur) FILTER (WHERE COALESCE(accounting_status,'por_contabilizar') = 'por_contabilizar'), 0) AS por_contabilizar_eur,
            COUNT(*) FILTER (
                WHERE accounting_status = 'contabilizado'
                  AND accounting_updated_at >= date_trunc('month', NOW())
            ) AS contabilizado_mes
        FROM invoices
        WHERE status NOT IN ('draft', 'cancelled')
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        # tickets abertos
        cur.execute("SELECT COUNT(*) FROM contabilidade_tickets WHERE status IN ('aberto','em_curso')")
        tickets_abertos = cur.fetchone()[0] or 0

    return {
        'por_contabilizar_count': row[0] or 0,
        'por_contabilizar_eur':   float(row[1] or 0),
        'contabilizado_mes':      row[2] or 0,
        'tickets_abertos':        tickets_abertos,
    }


# ── Update accounting status ──────────────────────────────────────────────────

def update_accounting_status(invoice_id: int, status: str, username: str, notes: str = None) -> bool:
    """Update accounting_status for a single invoice."""
    if status not in ACCOUNTING_STATUS_LABELS:
        return False
    with db_connection() as conn:
        cur = conn.cursor()
        if notes is not None:
            cur.execute(
                """UPDATE invoices
                   SET accounting_status = %s,
                       accounting_notes = %s,
                       accounting_updated_by = %s,
                       accounting_updated_at = NOW()
                   WHERE id = %s""",
                (status, notes, username, invoice_id),
            )
        else:
            cur.execute(
                """UPDATE invoices
                   SET accounting_status = %s,
                       accounting_updated_by = %s,
                       accounting_updated_at = NOW()
                   WHERE id = %s""",
                (status, username, invoice_id),
            )
        updated = cur.rowcount
        conn.commit()
    return updated > 0


def bulk_update_accounting_status(invoice_ids: list, status: str, username: str) -> int:
    """Bulk-update accounting_status for multiple invoices. Returns count updated."""
    if not invoice_ids or status not in ACCOUNTING_STATUS_LABELS:
        return 0
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE invoices
               SET accounting_status = %s,
                   accounting_updated_by = %s,
                   accounting_updated_at = NOW()
               WHERE id = ANY(%s)""",
            (status, username, invoice_ids),
        )
        updated = cur.rowcount
        conn.commit()
    return updated


# ── Tickets ───────────────────────────────────────────────────────────────────

def get_tickets(status: str = None, order_by: str = 'criado_em', order_dir: str = 'desc') -> list:
    """Return list of tickets."""
    _VALID_ORDER = {'criado_em', 'prazo', 'status', 'titulo'}
    if order_by not in _VALID_ORDER:
        order_by = 'criado_em'
    if order_dir not in ('asc', 'desc'):
        order_dir = 'desc'

    clauses = []
    params = []
    if status:
        clauses.append("t.status = %s")
        params.append(status)

    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''

    sql = f"""
        SELECT
            t.id, t.invoice_id, t.titulo, t.descricao,
            t.prazo, t.status, t.criado_por, t.criado_em, t.updated_at,
            i.invoice_number, i.supplier_name,
            (SELECT COUNT(*) FROM contabilidade_ticket_respostas r WHERE r.ticket_id = t.id) AS n_respostas
        FROM contabilidade_tickets t
        LEFT JOIN invoices i ON i.id = t.invoice_id
        {where}
        ORDER BY t.{order_by} {order_dir} NULLS LAST
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

    result = []
    for row in rows:
        result.append({
            'id':             row[0],
            'invoice_id':     row[1],
            'titulo':         row[2],
            'descricao':      row[3],
            'prazo':          row[4],
            'status':         row[5],
            'criado_por':     row[6],
            'criado_em':      row[7],
            'updated_at':     row[8],
            'invoice_number': row[9],
            'supplier_name':  row[10],
            'n_respostas':    row[11] or 0,
            'status_label':   TICKET_STATUS_LABELS.get(row[5], row[5]),
            'status_badge':   TICKET_STATUS_BADGE.get(row[5], 'bg-secondary'),
        })
    return result


def get_ticket(ticket_id: int) -> dict | None:
    """Return a single ticket with its responses."""
    sql = """
        SELECT
            t.id, t.invoice_id, t.titulo, t.descricao,
            t.prazo, t.status, t.criado_por, t.criado_em, t.updated_at,
            i.invoice_number, i.supplier_name, i.amount_eur
        FROM contabilidade_tickets t
        LEFT JOIN invoices i ON i.id = t.invoice_id
        WHERE t.id = %s
    """
    resp_sql = """
        SELECT id, ticket_id, mensagem, novo_status, criado_por, criado_em
        FROM contabilidade_ticket_respostas
        WHERE ticket_id = %s
        ORDER BY criado_em ASC
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (ticket_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(resp_sql, (ticket_id,))
        resp_rows = cur.fetchall()

    ticket = {
        'id':             row[0],
        'invoice_id':     row[1],
        'titulo':         row[2],
        'descricao':      row[3],
        'prazo':          row[4],
        'status':         row[5],
        'criado_por':     row[6],
        'criado_em':      row[7],
        'updated_at':     row[8],
        'invoice_number': row[9],
        'supplier_name':  row[10],
        'invoice_amount': row[11],
        'status_label':   TICKET_STATUS_LABELS.get(row[5], row[5]),
        'status_badge':   TICKET_STATUS_BADGE.get(row[5], 'bg-secondary'),
        'respostas': [
            {
                'id':           r[0],
                'ticket_id':    r[1],
                'mensagem':     r[2],
                'novo_status':  r[3],
                'criado_por':   r[4],
                'criado_em':    r[5],
                'status_label': TICKET_STATUS_LABELS.get(r[3], r[3]) if r[3] else None,
            }
            for r in resp_rows
        ],
    }
    return ticket


def create_ticket(titulo: str, descricao: str, criado_por: str,
                  invoice_id: int = None, prazo=None) -> int:
    """Create a new ticket and return its ID."""
    sql = """
        INSERT INTO contabilidade_tickets
            (invoice_id, titulo, descricao, prazo, status, criado_por, criado_em, updated_at)
        VALUES (%s, %s, %s, %s, 'aberto', %s, NOW(), NOW())
        RETURNING id
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (invoice_id, titulo, descricao, prazo, criado_por))
        ticket_id = cur.fetchone()[0]
        conn.commit()
    return ticket_id


def add_ticket_response(ticket_id: int, mensagem: str, novo_status: str, criado_por: str) -> bool:
    """Add a response to a ticket and optionally change its status."""
    if novo_status and novo_status not in TICKET_STATUS_LABELS:
        return False
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO contabilidade_ticket_respostas
               (ticket_id, mensagem, novo_status, criado_por, criado_em)
               VALUES (%s, %s, %s, %s, NOW())""",
            (ticket_id, mensagem, novo_status or None, criado_por),
        )
        if novo_status:
            cur.execute(
                "UPDATE contabilidade_tickets SET status = %s, updated_at = NOW() WHERE id = %s",
                (novo_status, ticket_id),
            )
        conn.commit()
    return True


def get_tickets_for_invoice(invoice_id: int) -> list:
    """Return tickets associated with a given invoice."""
    sql = """
        SELECT id, titulo, status, criado_em, criado_por
        FROM contabilidade_tickets
        WHERE invoice_id = %s
        ORDER BY criado_em DESC
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (invoice_id,))
        rows = cur.fetchall()
    return [
        {
            'id':          r[0],
            'titulo':      r[1],
            'status':      r[2],
            'criado_em':   r[3],
            'criado_por':  r[4],
            'status_label': TICKET_STATUS_LABELS.get(r[2], r[2]),
            'status_badge': TICKET_STATUS_BADGE.get(r[2], 'bg-secondary'),
        }
        for r in rows
    ]
