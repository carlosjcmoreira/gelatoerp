import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
import json
import os

PAYMENT_METHOD_LABELS = {
    'transferencia': 'Transferência',
    'debito_direto': 'Débito Direto',
    'confirming': 'Confirming',
    'numerario': 'Numerário',
}

PAYMENT_TERMS_LABELS = {
    'a_pronto': 'A pronto',
    '15_dias': '15 dias',
    '30_dias': '30 dias',
    '60_dias': '60 dias',
    'final_mes': 'Até final do mês',
}


def calculate_due_date(issue_date, payment_terms: str):
    """
    Calculate invoice due date from issue date + supplier payment terms.
    Returns a date object or None if issue_date is None.
    """
    if not issue_date:
        return None
    if isinstance(issue_date, str):
        from datetime import datetime
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                issue_date = datetime.strptime(issue_date.strip(), fmt).date()
                break
            except ValueError:
                continue
    if not payment_terms or payment_terms == 'a_pronto':
        return issue_date
    elif payment_terms == '15_dias':
        return issue_date + timedelta(days=15)
    elif payment_terms == '30_dias':
        return issue_date + timedelta(days=30)
    elif payment_terms == '60_dias':
        return issue_date + timedelta(days=60)
    elif payment_terms == 'final_mes':
        import calendar
        # last day of the month of issue_date
        last_day = calendar.monthrange(issue_date.year, issue_date.month)[1]
        return issue_date.replace(day=last_day)
    return None


def get_suppliers(only_active: bool = False) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.name, s.nif, s.category, s.store_id, s.notes,
                   st.name AS store_name, s.payment_method, s.payment_terms, s.iban
            FROM suppliers s
            LEFT JOIN stores st ON s.store_id = st.id
            ORDER BY s.name
        """)
        rows = cursor.fetchall()
    return [{'id': r[0], 'name': r[1], 'nif': r[2], 'category': r[3],
             'store_id': r[4], 'notes': r[5], 'store_name': r[6],
             'payment_method': r[7], 'payment_terms': r[8], 'iban': r[9]} for r in rows]


def get_supplier_by_nif(nif: str) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.name, s.nif, s.category, s.store_id, s.notes,
                   st.name AS store_name, s.payment_method, s.payment_terms, s.iban
            FROM suppliers s
            LEFT JOIN stores st ON s.store_id = st.id
            WHERE s.nif = %s
        """, (nif,))
        row = cursor.fetchone()
    if row:
        return {'id': row[0], 'name': row[1], 'nif': row[2], 'category': row[3],
                'store_id': row[4], 'notes': row[5], 'store_name': row[6],
                'payment_method': row[7], 'payment_terms': row[8], 'iban': row[9]}
    return None


def upsert_supplier(name: str, nif: str, category: str = None,
                    store_id: int = None, notes: str = None,
                    payment_method: str = None, payment_terms: str = None,
                    iban: str = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO suppliers (name, nif, category, store_id, notes,
                                   payment_method, payment_terms, iban, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (nif) DO UPDATE SET
                name = EXCLUDED.name,
                category = EXCLUDED.category,
                store_id = EXCLUDED.store_id,
                notes = EXCLUDED.notes,
                payment_method = COALESCE(EXCLUDED.payment_method, suppliers.payment_method),
                payment_terms = COALESCE(EXCLUDED.payment_terms, suppliers.payment_terms),
                iban = COALESCE(EXCLUDED.iban, suppliers.iban),
                updated_at = NOW()
            RETURNING id
        """, (name, nif, category, store_id, notes, payment_method, payment_terms, iban))
        supplier_id = cursor.fetchone()[0]
        conn.commit()
    return supplier_id


def delete_supplier(supplier_id: int) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM invoices WHERE supplier_id = %s", (supplier_id,))
        count = cursor.fetchone()[0]
        if count > 0:
            return False
        cursor.execute("DELETE FROM suppliers WHERE id = %s", (supplier_id,))
        conn.commit()
    return True


# ── Invoices ───────────────────────────────────────────────────────────────────

INVOICE_STATUS_LABELS = {
    'draft': 'Rascunho',
    'pending_review': 'Pendente revisão',
    'scheduled': 'Agendada',
    'paid': 'Paga',
    'overdue': 'Vencida',
    'cancelled': 'Cancelada',
}

ONEDRIVE_SUBFOLDERS = [
    'Bolhão (B)', 'Distribuição (D)', 'Eventos (E)', 'Faturas partilhadas (FP)',
    'Geral (G)', 'Matosinhos (M)', 'Produção (P)', 'Timeout (TOM)',
]

INVOICE_CATEGORIES = [
    'Matérias-primas', 'Embalagens', 'Serviços', 'Utilities', 'Rendas',
    'Equipamentos', 'Marketing', 'Transportes', 'Outros'
]


DOCUMENT_TYPE_LABELS = {
    'fatura':                 'Fatura',
    'nota_credito':           'Nota de Crédito',
    'nota_debito':            'Nota de Débito',
    'nota_pagamento_imposto': 'Nota de Pagamento de Imposto',
    'outro':                  'Outro Documento',
}

# Types that represent a supplier invoice (full supplier fields required)
DOCUMENT_TYPES_INVOICE = {'fatura', 'nota_credito', 'nota_debito'}


def _row_to_invoice(row) -> dict:
    document_type = row[23] if len(row) > 23 else 'fatura'
    if not document_type:
        document_type = 'fatura'
    return {
        'id': row[0],
        'supplier_id': row[1],
        'supplier_name': row[2],
        'supplier_nif': row[3],
        'invoice_number': row[4],
        'amount_eur': float(row[5]) if row[5] is not None else None,
        'vat_amount_eur': float(row[6]) if row[6] is not None else None,
        'issue_date': row[7],
        'due_date': row[8],
        'store_id': row[9],
        'category': row[10],
        'onedrive_subfolder': row[11],
        'onedrive_path': row[12],
        'pdf_filename': row[13],
        'status': row[14],
        'ocr_confidence': float(row[15]) if row[15] is not None else None,
        'created_by': row[16],
        'cfo_confirmed_date': row[17],
        'paid_date': row[18],
        'notes': row[19],
        'created_at': row[20],
        'store_name': row[21],
        'onedrive_web_url': row[22] if len(row) > 22 else None,
        'document_type': document_type,
        'document_type_label': DOCUMENT_TYPE_LABELS.get(document_type, document_type),
        'status_label': INVOICE_STATUS_LABELS.get(row[14], row[14]),
        'centro_custo_id': row[24] if len(row) > 24 else None,
        'categoria_custo_id': row[25] if len(row) > 25 else None,
    }


_ORDER_COL_MAP = {
    'due_date': 'i.due_date',
    'issue_date': 'i.issue_date',
    'amount_eur': 'i.amount_eur',
    'supplier_name': 'LOWER(i.supplier_name)',
    'invoice_number': 'i.invoice_number',
    'category': 'i.category',
    'status': 'i.status',
    'store_name': 'LOWER(st.name)',
    'cfo_confirmed_date': 'i.cfo_confirmed_date',
}


def get_invoice_suppliers() -> list:
    """Return suppliers that have at least one non-draft invoice, sorted by name.

    Each entry is a dict with 'id' (int) and 'name' (str).
    Only suppliers linked via supplier_id are returned; invoices without a
    supplier_id are not represented and remain accessible via the 'All' option.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT s.id, s.name
            FROM suppliers s
            JOIN invoices i ON i.supplier_id = s.id
            WHERE i.status != 'draft'
            ORDER BY s.name
        """)
        return [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]


def get_distinct_supplier_names() -> list:
    """Return all distinct non-null supplier_name values from non-draft invoices, sorted.

    Covers ALL invoices regardless of whether they have a supplier_id, so the
    returned list is the correct source of truth for filter dropdowns.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT supplier_name
            FROM invoices
            WHERE status != 'draft'
              AND supplier_name IS NOT NULL
              AND supplier_name != ''
            ORDER BY supplier_name
        """)
        return [row[0] for row in cursor.fetchall()]


def get_invoices(status: str = None, store_id: int = None,
                 search: str = None, order_by: str = 'due_date',
                 order_dir: str = 'asc',
                 centro_custo_id: int = None,
                 categoria_custo_id: int = None,
                 supplier_name: str = None,
                 supplier_names: list = None,
                 supplier_id: int = None,
                 date_from=None, date_to=None,
                 date_field: str = 'issue_date',
                 document_type: str = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        where = []
        params = []
        # 'overdue' is a virtual status: scheduled invoices with due_date in the past
        if status == 'overdue':
            where.append("i.status = 'scheduled' AND i.due_date < CURRENT_DATE")
        elif status:
            where.append("i.status = %s")
            params.append(status)
        else:
            # Exclude in-progress drafts from the default listing
            where.append("i.status != 'draft'")
        if store_id:
            where.append("i.store_id = %s")
            params.append(store_id)
        if centro_custo_id:
            where.append("i.centro_custo_id = %s")
            params.append(centro_custo_id)
        if categoria_custo_id:
            where.append("i.categoria_custo_id = %s")
            params.append(categoria_custo_id)
        if supplier_id:
            where.append("i.supplier_id = %s")
            params.append(supplier_id)
        if supplier_names:
            where.append("i.supplier_name = ANY(%s)")
            params.append(supplier_names)
        elif supplier_name:
            where.append("LOWER(i.supplier_name) = LOWER(%s)")
            params.append(supplier_name)
        if document_type and document_type in DOCUMENT_TYPE_LABELS:
            where.append("i.document_type = %s")
            params.append(document_type)
        _date_col = 'i.due_date' if date_field == 'due_date' else 'i.issue_date'
        if date_from:
            where.append(f"{_date_col} >= %s")
            params.append(date_from)
        if date_to:
            where.append(f"{_date_col} <= %s")
            params.append(date_to)
        if search:
            where.append("(LOWER(i.supplier_name) LIKE %s OR LOWER(i.invoice_number) LIKE %s)")
            s = f'%{search.lower()}%'
            params.extend([s, s])
        where_clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        order_col = _ORDER_COL_MAP.get(order_by, 'i.due_date')
        direction = 'DESC' if order_dir == 'desc' else 'ASC'
        cursor.execute(f"""
            SELECT i.id, i.supplier_id, i.supplier_name, i.supplier_nif,
                   i.invoice_number, i.amount_eur, i.vat_amount_eur,
                   i.issue_date, i.due_date, i.store_id, i.category,
                   i.onedrive_subfolder, i.onedrive_path, i.pdf_filename,
                   i.status, i.ocr_confidence, i.created_by,
                   i.cfo_confirmed_date, i.paid_date, i.notes, i.created_at,
                   st.name AS store_name,
                   i.onedrive_web_url,
                   i.document_type,
                   i.centro_custo_id,
                   i.categoria_custo_id,
                   ip.confirmed_date AS payment_confirmed_date,
                   i.payment_method
            FROM invoices i
            LEFT JOIN stores st ON i.store_id = st.id
            LEFT JOIN invoice_payments ip ON ip.invoice_id = i.id
            {where_clause}
            ORDER BY {order_col} {direction} NULLS LAST, i.created_at DESC
        """, params)
        rows = cursor.fetchall()
    result = []
    for r in rows:
        inv = _row_to_invoice(r)
        inv['payment_confirmed_date'] = r[26] if len(r) > 26 else None
        inv['payment_method'] = r[27] if len(r) > 27 else None
        result.append(inv)
    return result


def get_invoices_type_totals() -> dict:
    """Return a {document_type: {count, total}} dict for all non-draft invoices.

    Uses a single GROUP BY query rather than fetching all rows.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(document_type, 'fatura') AS dt,
                   COUNT(*) AS n,
                   COALESCE(SUM(amount_eur), 0) AS total
            FROM invoices
            WHERE status != 'draft'
            GROUP BY dt
            ORDER BY dt
        """)
        rows = cursor.fetchall()
    return {r[0]: {'count': int(r[1]), 'total': float(r[2])} for r in rows}


def get_invoice(invoice_id: int) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.id, i.supplier_id, i.supplier_name, i.supplier_nif,
                   i.invoice_number, i.amount_eur, i.vat_amount_eur,
                   i.issue_date, i.due_date, i.store_id, i.category,
                   i.onedrive_subfolder, i.onedrive_path, i.pdf_filename,
                   i.status, i.ocr_confidence, i.created_by,
                   i.cfo_confirmed_date, i.paid_date, i.notes, i.created_at,
                   st.name AS store_name,
                   i.onedrive_web_url,
                   i.document_type,
                   i.centro_custo_id,
                   i.categoria_custo_id,
                   i.ocr_raw,
                   ip.confirmed_date AS payment_confirmed_date,
                   i.stock_registado_at,
                   i.stock_registado_por,
                   i.payment_method
            FROM invoices i
            LEFT JOIN stores st ON i.store_id = st.id
            LEFT JOIN invoice_payments ip ON ip.invoice_id = i.id
            WHERE i.id = %s
        """, (invoice_id,))
        row = cursor.fetchone()
    if not row:
        return None
    inv = _row_to_invoice(row)
    inv['ocr_raw'] = row[26]
    inv['payment_confirmed_date'] = row[27] if len(row) > 27 else None
    inv['stock_registado_at'] = row[28] if len(row) > 28 else None
    inv['stock_registado_por'] = row[29] if len(row) > 29 else None
    inv['payment_method'] = row[30] if len(row) > 30 else None
    return inv


def get_invoice_pdf(invoice_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pdf_data, pdf_filename FROM invoices WHERE id = %s", (invoice_id,))
        row = cursor.fetchone()
    if row:
        return row[0], row[1]
    return None, None


def create_invoice(data: dict) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO invoices (
                supplier_id, supplier_name, supplier_nif, invoice_number,
                amount_eur, vat_amount_eur, issue_date, due_date,
                store_id, category, onedrive_subfolder, onedrive_path,
                onedrive_web_url, pdf_filename, pdf_data, status,
                ocr_confidence, ocr_raw, created_by, notes, document_type, source,
                centro_custo_id, categoria_custo_id, updated_at
            ) VALUES (
                %(supplier_id)s, %(supplier_name)s, %(supplier_nif)s, %(invoice_number)s,
                %(amount_eur)s, %(vat_amount_eur)s, %(issue_date)s, %(due_date)s,
                %(store_id)s, %(category)s, %(onedrive_subfolder)s, %(onedrive_path)s,
                %(onedrive_web_url)s, %(pdf_filename)s, %(pdf_data)s, %(status)s,
                %(ocr_confidence)s, %(ocr_raw)s, %(created_by)s, %(notes)s,
                %(document_type)s, %(source)s,
                %(centro_custo_id)s, %(categoria_custo_id)s, NOW()
            ) RETURNING id
        """, {**data,
              'source': data.get('source', 'email_upload'),
              'centro_custo_id': data.get('centro_custo_id'),
              'categoria_custo_id': data.get('categoria_custo_id')})
        invoice_id = cursor.fetchone()[0]
        conn.commit()
    return invoice_id


def update_invoice(invoice_id: int, data: dict):
    fields = []
    params = []
    allowed = [
        'supplier_name', 'supplier_nif', 'invoice_number', 'amount_eur', 'vat_amount_eur',
        'issue_date', 'due_date', 'store_id', 'category', 'onedrive_subfolder',
        'onedrive_path', 'status', 'cfo_confirmed_date', 'paid_date', 'notes', 'supplier_id',
        'document_type', 'centro_custo_id', 'categoria_custo_id', 'payment_method',
    ]
    for key in allowed:
        if key in data:
            fields.append(f"{key} = %s")
            params.append(data[key])
    if not fields:
        return
    fields.append("updated_at = NOW()")
    params.append(invoice_id)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE invoices SET {', '.join(fields)} WHERE id = %s",
            params
        )
        conn.commit()


def update_invoice_onedrive(invoice_id: int, onedrive_path: str, onedrive_subfolder: str,
                            onedrive_web_url: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET onedrive_path = %s, onedrive_subfolder = %s,
                onedrive_web_url = %s, updated_at = NOW()
            WHERE id = %s
        """, (onedrive_path, onedrive_subfolder, onedrive_web_url, invoice_id))
        conn.commit()


def delete_invoice(invoice_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        conn.commit()


def mark_invoice_confirmed(invoice_id: int, confirmed_date) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET status = 'scheduled', cfo_confirmed_date = %s, updated_at = NOW()
            WHERE id = %s AND status = 'pending_review'
        """, (confirmed_date, invoice_id))
        conn.commit()


def mark_invoice_paid(invoice_id: int, paid_date) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET status = 'paid', paid_date = %s, updated_at = NOW()
            WHERE id = %s
        """, (paid_date, invoice_id))
        conn.commit()


def get_stores_list() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM stores WHERE is_active = TRUE ORDER BY name")
        rows = cursor.fetchall()
    return [{'id': r[0], 'name': r[1]} for r in rows]


# Subfolder suggestion rules: store name → OneDrive subfolder
_STORE_SUBFOLDER_MAP = {
    'matosinhos': 'Matosinhos (M)',
    'bolhão': 'Bolhão (B)',
    'bolhao': 'Bolhão (B)',
    'fp': 'Faturas partilhadas (FP)',
    'faturas partilhadas': 'Faturas partilhadas (FP)',
    'fábrica': 'Produção (P)',
    'fabrica': 'Produção (P)',
    'produção': 'Produção (P)',
    'producao': 'Produção (P)',
    'distribuição': 'Distribuição (D)',
    'distribuicao': 'Distribuição (D)',
    'eventos': 'Eventos (E)',
    'gestão': 'Geral (G)',
    'gestao': 'Geral (G)',
    'geral': 'Geral (G)',
    'timeout': 'Timeout (TOM)',
    'tom': 'Timeout (TOM)',
}

_CATEGORY_SUBFOLDER_MAP = {
    'Matérias-primas': 'Produção (P)',
    'Embalagens': 'Produção (P)',
    'Serviços': 'Geral (G)',
    'Utilities': 'Geral (G)',
    'Rendas': 'Geral (G)',
    'Equipamentos': 'Geral (G)',
    'Marketing': 'Geral (G)',
    'Transportes': 'Distribuição (D)',
    'Outros': 'Geral (G)',
}


def suggest_onedrive_subfolder(supplier_nif: str = None, store_id: int = None,
                                category: str = None) -> str:
    """
    Suggest OneDrive subfolder based on supplier NIF + store association.
    Rules (priority order):
    1. If supplier has a known store association → use store-based mapping
    2. If category provided → use category mapping
    3. Default → 'Geral (G)'
    """
    # Rule 1: look up supplier's associated store
    if supplier_nif:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT st.name FROM suppliers s
                LEFT JOIN stores st ON s.store_id = st.id
                WHERE s.nif = %s
            """, (supplier_nif,))
            row = cursor.fetchone()
            if row and row[0]:
                store_name = row[0].lower()
                if store_name in _STORE_SUBFOLDER_MAP:
                    return _STORE_SUBFOLDER_MAP[store_name]

    # Rule 2: store_id provided directly
    if store_id:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM stores WHERE id = %s", (store_id,))
            row = cursor.fetchone()
            if row:
                store_name = row[0].lower()
                if store_name in _STORE_SUBFOLDER_MAP:
                    return _STORE_SUBFOLDER_MAP[store_name]

    # Rule 3: category-based fallback
    if category and category in _CATEGORY_SUBFOLDER_MAP:
        return _CATEGORY_SUBFOLDER_MAP[category]

    return 'Geral (G)'


def get_contas_por_fornecedor(status_filter: str = None) -> list:
    """
    Returns a list of all non-draft invoices and credit notes, grouped by supplier.
    Each entry contains:
      - supplier_name, supplier_nif
      - n_docs: total number of documents
      - total_faturas: sum of invoice amounts (positive)
      - total_nc: sum of credit note amounts (positive)
      - saldo_liquido: total_nc - total_faturas (negative means owed)
      - invoices: list of individual invoice dicts

    status_filter: optional single status to restrict results
                   (e.g. 'pending_review', 'scheduled', 'paid', 'overdue').
                   Defaults to all non-draft invoices.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        params = []
        if status_filter == 'overdue':
            extra_where = "AND i.status = 'scheduled' AND i.due_date < CURRENT_DATE"
        elif status_filter:
            extra_where = "AND i.status = %s"
            params.append(status_filter)
        else:
            extra_where = ""
        cursor.execute(f"""
            SELECT i.id, i.supplier_id, i.supplier_name, i.supplier_nif,
                   i.invoice_number, i.amount_eur, i.vat_amount_eur,
                   i.issue_date, i.due_date, i.store_id, i.category,
                   i.onedrive_subfolder, i.onedrive_path, i.pdf_filename,
                   i.status, i.ocr_confidence, i.created_by,
                   i.cfo_confirmed_date, i.paid_date, i.notes, i.created_at,
                   st.name AS store_name,
                   i.onedrive_web_url,
                   i.document_type,
                   i.centro_custo_id,
                   i.categoria_custo_id
            FROM invoices i
            LEFT JOIN stores st ON i.store_id = st.id
            WHERE i.status != 'draft' {extra_where}
            ORDER BY LOWER(i.supplier_name), i.due_date ASC NULLS LAST
        """, params)
        rows = cursor.fetchall()

    invoices = [_row_to_invoice(r) for r in rows]

    from collections import defaultdict
    groups = defaultdict(lambda: {
        'supplier_name': None,
        'supplier_nif': None,
        'invoices': [],
        'total_faturas': 0.0,
        'total_nc': 0.0,
    })

    for inv in invoices:
        key = inv['supplier_name'] or '(sem fornecedor)'
        g = groups[key]
        g['supplier_name'] = inv['supplier_name'] or '(sem fornecedor)'
        g['supplier_nif'] = inv['supplier_nif']
        g['invoices'].append(inv)
        amt = abs(float(inv['amount_eur'] or 0))
        if inv['document_type'] == 'nota_credito':
            g['total_nc'] += amt
        else:
            g['total_faturas'] += amt

    result = []
    for g in groups.values():
        g['n_docs'] = len(g['invoices'])
        g['saldo_liquido'] = round(g['total_nc'] - g['total_faturas'], 2)
        g['total_faturas'] = round(g['total_faturas'], 2)
        g['total_nc'] = round(g['total_nc'], 2)
        result.append(g)

    result.sort(key=lambda x: (x['supplier_name'] or '').lower())
    return result
