"""DB layer for B2B/Events client invoices (faturas_clientes table)."""
import logging
from datetime import date
from db.connection import db_connection

logger = logging.getLogger(__name__)

VALID_STATUSES = ('pendente', 'pago', 'vencido')


DOCUMENT_TYPE_LABELS = {
    'fatura': 'Fatura',
    'nota_credito': 'Nota de Crédito',
    'nota_debito': 'Nota de Débito',
}


def signed_total_sql(column='fc.total', document_type_column='fc.document_type'):
    """Return the SQL expression used for B2B net sales/receivables totals."""
    return (
        f"CASE WHEN {document_type_column} = 'nota_credito' "
        f"THEN -({column}) ELSE {column} END"
    )


def upsert_fatura(cliente_id: int, numero: str, data_fatura: date,
                  data_vencimento: date = None, documento: str = None,
                  document_type: str = 'fatura',
                  armazem: str = None, total_bruto: float = 0.0,
                  total_liquido: float = 0.0, desconto_global: float = 0.0,
                  total_imposto: float = 0.0, total: float = 0.0,
                  observacoes: str = None, anulado: bool = False) -> tuple:
    """Insert or update invoice by numero. Returns (id, is_new)."""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO faturas_clientes
                (cliente_id, numero, data, data_vencimento, documento,
                 document_type, armazem,
                 total_bruto, total_liquido, desconto_global, total_imposto,
                 total, observacoes, anulado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (numero) DO UPDATE
                SET cliente_id      = EXCLUDED.cliente_id,
                    data            = EXCLUDED.data,
                    data_vencimento = EXCLUDED.data_vencimento,
                    documento       = EXCLUDED.documento,
                    document_type   = EXCLUDED.document_type,
                    armazem         = EXCLUDED.armazem,
                    total_bruto     = EXCLUDED.total_bruto,
                    total_liquido   = EXCLUDED.total_liquido,
                    desconto_global = EXCLUDED.desconto_global,
                    total_imposto   = EXCLUDED.total_imposto,
                    total           = EXCLUDED.total,
                    observacoes     = EXCLUDED.observacoes,
                    anulado         = EXCLUDED.anulado,
                    updated_at      = NOW()
            RETURNING id, (xmax = 0) AS is_new
        """, (
            cliente_id, numero.strip(), data_fatura, data_vencimento,
            documento, document_type if document_type in DOCUMENT_TYPE_LABELS else 'fatura', armazem,
            total_bruto or 0.0, total_liquido or 0.0,
            desconto_global or 0.0, total_imposto or 0.0,
            total or 0.0, observacoes, anulado,
        ))
        row = cur.fetchone()
        conn.commit()
        return row[0], row[1]


def update_status(fatura_id: int, status: str, data_pagamento: date = None) -> bool:
    """Set payment status for a single invoice. Returns True if row was found.

    When transitioning to 'pago', data_pagamento is set (defaults to today if not
    provided and not already set). For other statuses, data_pagamento is left
    untouched unless explicitly provided.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}.")
    with db_connection() as conn:
        cur = conn.cursor()
        if status == 'pago':
            cur.execute("""
                UPDATE faturas_clientes
                   SET status = %s,
                       data_pagamento = COALESCE(%s, data_pagamento, CURRENT_DATE),
                       updated_at = NOW()
                 WHERE id = %s AND anulado = FALSE
            """, (status, data_pagamento, fatura_id))
        else:
            cur.execute("""
                UPDATE faturas_clientes
                   SET status = %s, updated_at = NOW()
                 WHERE id = %s AND anulado = FALSE
            """, (status, fatura_id))
        updated = cur.rowcount
        conn.commit()
        return updated > 0


def promote_overdue() -> int:
    """Bulk-update all pendente invoices past their data_vencimento to vencido.

    Meant to be called on app startup (and can be safely re-run anytime,
    e.g. from a scheduled job) so the DB status stays accurate for
    reporting, exports, and summary cards. Returns the number of rows updated.
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE faturas_clientes
               SET status = 'vencido', updated_at = NOW()
             WHERE status = 'pendente'
               AND anulado = FALSE
               AND data_vencimento IS NOT NULL
               AND data_vencimento < CURRENT_DATE
        """)
        updated = cur.rowcount
        conn.commit()
        if updated:
            logger.info('promote_overdue: marked %d invoice(s) as vencido', updated)
        return updated


def get_summary_totals(cliente_id: int = None, data_inicio: date = None,
                       data_fim: date = None) -> dict:
    """Return totals grouped by status (pendente/pago/vencido) for non-cancelled invoices.

    Also auto-promotes pendente invoices past their data_vencimento to vencido
    in the result (without writing to DB — the DB status is authoritative; this
    just provides the display aggregate).
    """
    conditions = ["fc.anulado = FALSE"]
    vals = []
    if cliente_id:
        conditions.append("fc.cliente_id = %s")
        vals.append(cliente_id)
    if data_inicio:
        conditions.append("fc.data >= %s")
        vals.append(data_inicio)
    if data_fim:
        conditions.append("fc.data <= %s")
        vals.append(data_fim)
    where = "WHERE " + " AND ".join(conditions)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT
                SUM(CASE WHEN fc.status = 'pago' THEN {signed_total_sql()} ELSE 0 END)       AS total_pago,
                SUM(CASE WHEN fc.status IN ('pendente','vencido') THEN {signed_total_sql()} ELSE 0 END) AS total_pendente,
                SUM(CASE WHEN fc.status = 'vencido' THEN {signed_total_sql()} ELSE 0 END)    AS total_vencido,
                COUNT(*) FILTER (WHERE fc.status = 'pago')                        AS count_pago,
                COUNT(*) FILTER (WHERE fc.status IN ('pendente','vencido'))       AS count_pendente,
                COUNT(*) FILTER (WHERE fc.status = 'vencido')                     AS count_vencido
            FROM faturas_clientes fc
            {where}
        """, vals)
        row = cur.fetchone()
    return {
        'total_pago':      float(row[0] or 0),
        'total_pendente':  float(row[1] or 0),
        'total_vencido':   float(row[2] or 0),
        'count_pago':      int(row[3] or 0),
        'count_pendente':  int(row[4] or 0),
        'count_vencido':   int(row[5] or 0),
    }


def list_faturas(cliente_id: int = None, data_inicio: date = None,
                 data_fim: date = None, incluir_anuladas: bool = False,
                 limit: int = 500, offset: int = 0) -> list:
    """Return invoices with optional filters. Joins client name/tipo."""
    conditions = []
    vals = []
    if not incluir_anuladas:
        conditions.append("fc.anulado = FALSE")
    if cliente_id:
        conditions.append("fc.cliente_id = %s")
        vals.append(cliente_id)
    if data_inicio:
        conditions.append("fc.data >= %s")
        vals.append(data_inicio)
    if data_fim:
        conditions.append("fc.data <= %s")
        vals.append(data_fim)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    vals += [limit, offset]
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT fc.id, fc.cliente_id, c.nome AS cliente_nome, c.nif AS cliente_nif,
                   c.tipo AS cliente_tipo,
                   fc.numero, fc.data, fc.data_vencimento, fc.documento, fc.armazem,
                   fc.total_bruto, fc.total_liquido, fc.desconto_global,
                   fc.total_imposto, fc.total, fc.observacoes, fc.anulado,
                   fc.document_type,
                   fc.status, fc.data_pagamento,
                   fc.criado_em, fc.updated_at
            FROM faturas_clientes fc
            JOIN clientes_b2b c ON c.id = fc.cliente_id
            {where}
            ORDER BY fc.data DESC, fc.numero DESC
            LIMIT %s OFFSET %s
        """, vals)
        cols = [
            'id', 'cliente_id', 'cliente_nome', 'cliente_nif', 'cliente_tipo',
            'numero', 'data', 'data_vencimento', 'documento', 'armazem',
            'total_bruto', 'total_liquido', 'desconto_global',
            'total_imposto', 'total', 'observacoes', 'anulado', 'document_type',
            'status', 'data_pagamento',
            'criado_em', 'updated_at',
        ]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    today = date.today()
    for row in rows:
        if (row['status'] == 'pendente'
                and row['data_vencimento']
                and row['data_vencimento'] < today
                and not row['anulado']):
            row['status'] = 'vencido'
        row['document_type_label'] = DOCUMENT_TYPE_LABELS.get(
            row['document_type'], row['document_type'] or 'Fatura'
        )
        row['total_assinado'] = (
            -float(row['total'] or 0)
            if row['document_type'] == 'nota_credito'
            else float(row['total'] or 0)
        )
    return rows


def count_faturas(cliente_id: int = None, data_inicio: date = None,
                  data_fim: date = None, incluir_anuladas: bool = False) -> int:
    conditions = []
    vals = []
    if not incluir_anuladas:
        conditions.append("fc.anulado = FALSE")
    if cliente_id:
        conditions.append("fc.cliente_id = %s")
        vals.append(cliente_id)
    if data_inicio:
        conditions.append("fc.data >= %s")
        vals.append(data_inicio)
    if data_fim:
        conditions.append("fc.data <= %s")
        vals.append(data_fim)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT COUNT(*) FROM faturas_clientes fc
            JOIN clientes_b2b c ON c.id = fc.cliente_id
            {where}
        """, vals)
        return cur.fetchone()[0]


def get_totais_por_mes(ano: int = None, incluir_mapas_only: bool = True) -> list:
    """Return monthly totals grouped by year/month and client tipo.

    Used by the sales dashboard B2B section.
    Only includes clients with incluir_mapas=TRUE when incluir_mapas_only=True.
    """
    conditions = ["fc.anulado = FALSE"]
    vals = []
    if incluir_mapas_only:
        conditions.append("c.incluir_mapas = TRUE")
    if ano:
        conditions.append("EXTRACT(YEAR FROM fc.data)::int = %s")
        vals.append(ano)
    where = "WHERE " + " AND ".join(conditions)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT
                EXTRACT(YEAR  FROM fc.data)::int AS ano,
                EXTRACT(MONTH FROM fc.data)::int AS mes,
                c.tipo,
                c.id AS cliente_id,
                c.nome AS cliente_nome,
                SUM({signed_total_sql()}) AS total_eur
            FROM faturas_clientes fc
            JOIN clientes_b2b c ON c.id = fc.cliente_id
            {where}
            GROUP BY ano, mes, c.tipo, c.id, c.nome
            ORDER BY ano, mes, c.tipo, c.nome
        """, vals)
        cols = ['ano', 'mes', 'tipo', 'cliente_id', 'cliente_nome', 'total_eur']
        return [dict(zip(cols, r)) for r in cur.fetchall()]
