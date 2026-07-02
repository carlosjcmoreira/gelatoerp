"""DB layer for B2B/Events client invoices (faturas_clientes table)."""
import logging
from datetime import date
from db.connection import db_connection

logger = logging.getLogger(__name__)


def upsert_fatura(cliente_id: int, numero: str, data_fatura: date,
                  data_vencimento: date = None, documento: str = None,
                  armazem: str = None, total_bruto: float = 0.0,
                  total_liquido: float = 0.0, desconto_global: float = 0.0,
                  total_imposto: float = 0.0, total: float = 0.0,
                  observacoes: str = None, anulado: bool = False) -> tuple:
    """Insert or update invoice by numero. Returns (id, is_new)."""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO faturas_clientes
                (cliente_id, numero, data, data_vencimento, documento, armazem,
                 total_bruto, total_liquido, desconto_global, total_imposto,
                 total, observacoes, anulado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (numero) DO UPDATE
                SET cliente_id      = EXCLUDED.cliente_id,
                    data            = EXCLUDED.data,
                    data_vencimento = EXCLUDED.data_vencimento,
                    documento       = EXCLUDED.documento,
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
            documento, armazem,
            total_bruto or 0.0, total_liquido or 0.0,
            desconto_global or 0.0, total_imposto or 0.0,
            total or 0.0, observacoes, anulado,
        ))
        row = cur.fetchone()
        conn.commit()
        return row[0], row[1]


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
            'total_imposto', 'total', 'observacoes', 'anulado',
            'criado_em', 'updated_at',
        ]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


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
                SUM(fc.total) AS total_eur
            FROM faturas_clientes fc
            JOIN clientes_b2b c ON c.id = fc.cliente_id
            {where}
            GROUP BY ano, mes, c.tipo, c.id, c.nome
            ORDER BY ano, mes, c.tipo, c.nome
        """, vals)
        cols = ['ano', 'mes', 'tipo', 'cliente_id', 'cliente_nome', 'total_eur']
        return [dict(zip(cols, r)) for r in cur.fetchall()]
