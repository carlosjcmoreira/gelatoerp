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

_SUPPLIER_DISPLAY_SQL = (
    "COALESCE(NULLIF((SELECT s.common_name FROM suppliers s WHERE s.id = i.supplier_id), ''), "
    "(SELECT s.name FROM suppliers s WHERE s.id = i.supplier_id), i.supplier_name)"
)

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
    centro_custo_id: int = None,
    sem_cc: bool = False,
):
    """Build WHERE clause for contabilidade invoice queries."""
    clauses = ["i.status NOT IN ('draft', 'cancelled')"]
    params = []

    if supplier_name:
        clauses.append(f"LOWER({_SUPPLIER_DISPLAY_SQL}) LIKE LOWER(%s)")
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

    if sem_cc:
        clauses.append("""(
            i.centro_custo_id IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM invoice_centros_custo icc
                WHERE icc.invoice_id = i.id
            )
        )""")
    elif centro_custo_id:
        clauses.append("""(
            i.centro_custo_id = %s
            OR EXISTS (
                SELECT 1 FROM invoice_centros_custo icc
                WHERE icc.invoice_id = i.id
                  AND icc.centro_custo_id = %s
            )
        )""")
        params.extend([centro_custo_id, centro_custo_id])

    if search:
        clauses.append(
            f"(i.invoice_number ILIKE %s OR {_SUPPLIER_DISPLAY_SQL} ILIKE %s OR i.notes ILIKE %s)"
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
    centro_custo_id: int = None,
    sem_cc: bool = False,
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
        centro_custo_id=centro_custo_id,
        sem_cc=sem_cc,
    )

    order_sql = _SUPPLIER_DISPLAY_SQL if order_by == 'supplier_name' else f'i.{order_by}'
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
            (i.pdf_data IS NOT NULL) AS has_pdf,
            s.name AS supplier_legal_name,
            COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name,
            COALESCE(
                (
                    SELECT STRING_AGG(cc2.name, ', ' ORDER BY cc2.code)
                    FROM invoice_centros_custo icc2
                    JOIN cost_centers cc2
                      ON cc2.id = icc2.centro_custo_id
                    WHERE icc2.invoice_id = i.id
                ),
                cc.name
            ) AS centro_custo_name
        FROM invoices i
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        LEFT JOIN cost_centers cc ON cc.id = i.centro_custo_id
        {where}
        ORDER BY {order_sql} {order_dir} NULLS LAST
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
            'supplier_legal_name':  row[17] or row[2],
            'supplier_display_name': row[18] or row[2],
            'centro_custo_name':     row[19],
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
    centro_custo_id: int = None,
    sem_cc: bool = False,
) -> int:
    where, params = _build_cont_where(
        supplier_name=supplier_name,
        document_type=document_type,
        accounting_status=accounting_status,
        date_from=date_from,
        date_to=date_to,
        search=search,
        centro_custo_id=centro_custo_id,
        sem_cc=sem_cc,
    )
    sql = f"SELECT COUNT(*) FROM invoices i {where}"
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()[0] or 0


def get_cont_invoice_cost_center_names(invoice_id: int) -> list[str]:
    """Return unique cost-center names linked through legacy or multi-allocation data."""
    sql = """
        SELECT name
        FROM (
            SELECT cc.name AS name
            FROM invoice_centros_custo icc
            JOIN cost_centers cc ON cc.id = icc.centro_custo_id
            WHERE icc.invoice_id = %s

            UNION ALL

            SELECT cc.name AS name
            FROM invoices i
            JOIN cost_centers cc ON cc.id = i.centro_custo_id
            WHERE i.id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM invoice_centros_custo icc
                  WHERE icc.invoice_id = i.id
              )
        ) AS invoice_cost_centers
        WHERE name IS NOT NULL
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (invoice_id, invoice_id))
        rows = cur.fetchall()

    names_by_key = {}
    for row in rows:
        name = ' '.join(str(row[0] or '').split())
        if name:
            names_by_key.setdefault(name.casefold(), name)
    return sorted(names_by_key.values(), key=lambda name: (name.casefold(), name))


def get_cont_invoice_zip_metadata(invoice_ids: list[int]) -> list[dict]:
    """Fetch eligible invoice names, file sizes, and centers without loading file data."""
    if not invoice_ids:
        return []

    sql = """
        SELECT
            i.id,
            i.pdf_filename,
            octet_length(i.pdf_data),
            COALESCE(
                (
                    SELECT ARRAY_AGG(DISTINCT cc_multi.name::text
                                     ORDER BY cc_multi.name::text)
                    FROM invoice_centros_custo icc
                    JOIN cost_centers cc_multi
                      ON cc_multi.id = icc.centro_custo_id
                    WHERE icc.invoice_id = i.id
                ),
                CASE
                    WHEN cc_legacy.name IS NULL THEN ARRAY[]::text[]
                    ELSE ARRAY[cc_legacy.name::text]
                END
            ) AS cost_center_names
        FROM invoices i
        LEFT JOIN cost_centers cc_legacy
          ON cc_legacy.id = i.centro_custo_id
        WHERE i.id = ANY(%s)
          AND i.status NOT IN ('draft', 'cancelled')
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (invoice_ids,))
        rows = cur.fetchall()

    return [
        {
            'id': row[0],
            'pdf_filename': row[1],
            'pdf_size': row[2],
            'cost_center_names': row[3] or [],
        }
        for row in rows
    ]


def get_cont_invoice_zip_data(invoice_ids: list[int]) -> list[dict]:
    """Fetch document bytes after ZIP metadata has passed size and scope checks."""
    if not invoice_ids:
        return []

    sql = """
        SELECT i.id, i.pdf_data
        FROM invoices i
        WHERE i.id = ANY(%s)
          AND i.status NOT IN ('draft', 'cancelled')
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (invoice_ids,))
        rows = cur.fetchall()

    return [{'id': row[0], 'pdf_data': row[1]} for row in rows]


def get_cont_invoice_document_folders() -> dict:
    """Group uploaded Contabilidade documents by their effective cost-center assignments."""
    sql = """
        SELECT DISTINCT
            i.id,
            i.invoice_number,
            i.pdf_filename,
            i.issue_date,
            i.document_type,
            COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name),
            i.supplier_name,
            cc.id,
            cc.code,
            cc.name
        FROM invoices i
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        LEFT JOIN invoice_centros_custo icc ON icc.invoice_id = i.id
        LEFT JOIN cost_centers cc
          ON cc.id = COALESCE(icc.centro_custo_id, i.centro_custo_id)
        WHERE i.status NOT IN ('draft', 'cancelled')
          AND i.pdf_data IS NOT NULL
          AND octet_length(i.pdf_data) > 0
        ORDER BY cc.code NULLS LAST, cc.name NULLS LAST,
                 i.issue_date DESC NULLS LAST, i.id DESC
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()

    folders_by_key = {}
    invoice_ids = set()
    for row in rows:
        invoice_id = row[0]
        invoice_ids.add(invoice_id)
        center_id = row[7]
        folder_key = ('center', center_id) if center_id is not None else ('none', None)
        folder = folders_by_key.get(folder_key)
        if folder is None:
            center_name = row[9]
            folder = {
                'id': center_id,
                'code': row[8],
                'name': center_name,
                'label': (
                    f'{row[8]} — {center_name}'
                    if center_id is not None and row[8] and center_name
                    else center_name or f'Centro de custo #{center_id}'
                    if center_id is not None
                    else 'Sem centro de custo'
                ),
                'documents': [],
                '_invoice_ids': set(),
            }
            folders_by_key[folder_key] = folder

        if invoice_id in folder['_invoice_ids']:
            continue
        folder['_invoice_ids'].add(invoice_id)
        folder['documents'].append({
            'id': invoice_id,
            'invoice_number': row[1],
            'pdf_filename': row[2],
            'issue_date': row[3],
            'document_type': row[4],
            'supplier_display_name': row[5] or row[6] or '—',
            'supplier_name': row[6],
        })

    folders = list(folders_by_key.values())
    for folder in folders:
        folder['count'] = len(folder['documents'])
        folder.pop('_invoice_ids', None)
    folders.sort(key=lambda folder: (
        folder['id'] is None,
        (folder['code'] or '').casefold(),
        (folder['name'] or '').casefold(),
        folder['id'] or 0,
    ))
    return {
        'folders': folders,
        'total_documents': len(invoice_ids),
    }


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

def _write_accounting_audit(cur, invoice_id: int, field: str, old_value, new_value, username: str):
    cur.execute(
        """INSERT INTO invoice_audit_log
           (invoice_id, campo_alterado, valor_anterior, valor_novo, alterado_por)
           VALUES (%s, %s, %s, %s, %s)""",
        (invoice_id, field, old_value, new_value, username),
    )


def update_accounting_status(invoice_id: int, status: str, username: str, notes: str = None) -> bool:
    """Update accounting fields and audit each value that actually changes."""
    if status not in ACCOUNTING_STATUS_LABELS:
        return False
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT COALESCE(accounting_status, 'por_contabilizar'), accounting_notes
               FROM invoices WHERE id = %s FOR UPDATE""",
            (invoice_id,),
        )
        current = cur.fetchone()
        if not current:
            return False

        old_status, old_notes = current
        status_changed = old_status != status
        notes_changed = notes is not None and old_notes != notes
        if not status_changed and not notes_changed:
            conn.commit()
            return True

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
        if updated:
            if status_changed:
                _write_accounting_audit(
                    cur, invoice_id, 'accounting_status', old_status, status, username
                )
            if notes_changed:
                _write_accounting_audit(
                    cur, invoice_id, 'accounting_notes', old_notes, notes, username
                )
        conn.commit()
    return updated > 0


def bulk_update_accounting_status(invoice_ids: list, status: str, username: str) -> int:
    """Bulk-update statuses and audit changed invoices atomically."""
    if not invoice_ids or status not in ACCOUNTING_STATUS_LABELS:
        return 0
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, COALESCE(accounting_status, 'por_contabilizar')
               FROM invoices WHERE id = ANY(%s) FOR UPDATE""",
            (invoice_ids,),
        )
        current_rows = cur.fetchall()
        updated = 0
        for invoice_id, old_status in current_rows:
            if old_status == status:
                continue
            cur.execute(
                """UPDATE invoices
                   SET accounting_status = %s,
                       accounting_updated_by = %s,
                       accounting_updated_at = NOW()
                   WHERE id = %s""",
                (status, username, invoice_id),
            )
            if cur.rowcount:
                _write_accounting_audit(
                    cur, invoice_id, 'accounting_status', old_status, status, username
                )
                updated += 1
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
