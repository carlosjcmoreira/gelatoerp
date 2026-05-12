"""Lightweight widget queries for the global dashboard.

Each function returns a small dict of display-ready values.
Functions must never raise — they return safe defaults on any error.

NOTE: Dashboard sales widgets that read from `vendas` should eventually be updated to use
`vendas_detalhe` (Gestor uploads) as the primary source per the business rule in Task #203.
"""
import logging
from datetime import date, timedelta
from db.connection import db_connection

logger = logging.getLogger(__name__)


def _safe(fn):
    """Decorator: catch all exceptions and return an error-state dict."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning("dashboard widget %s failed: %s", fn.__name__, exc)
            return {'_error': True}
    return wrapper


@_safe
def widget_eurokg() -> dict:
    today = date.today()
    first_of_month = today.replace(day=1)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(data) FROM stock_gelado WHERE tipo IN ('fim','inicio')"
        )
        ultima_pesagem = cur.fetchone()[0]
        cur.execute(
            """SELECT COALESCE(SUM(v.valor_euros), 0), COALESCE(SUM(p.quantidade_kg), 0)
               FROM vendas v
               CROSS JOIN (
                   SELECT COALESCE(SUM(quantidade_kg), 0) as quantidade_kg
                   FROM producao
                   WHERE data >= %s AND tipo = 'producao'
               ) p
               WHERE v.data >= %s""",
            (first_of_month, first_of_month)
        )
        row = cur.fetchone()
        total_vendas = float(row[0]) if row else 0
        total_prod = float(row[1]) if row else 0
        media_euro_kg = round(total_vendas / total_prod, 2) if total_prod > 0 else None
    dias_sem_pesagem = (today - ultima_pesagem).days if ultima_pesagem else None
    return {
        'ultima_pesagem': ultima_pesagem,
        'dias_sem_pesagem': dias_sem_pesagem,
        'media_euro_kg': media_euro_kg,
    }


@_safe
def widget_producao_gelado() -> dict:
    today = date.today()
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(quantidade_kg), 0) FROM producao WHERE data = %s AND tipo = 'producao'",
            (today,)
        )
        kg_hoje = float(cur.fetchone()[0])
        cur.execute(
            "SELECT MAX(data) FROM producao WHERE tipo = 'producao'"
        )
        ultima_producao = cur.fetchone()[0]
    return {
        'kg_hoje': kg_hoje,
        'ultima_producao': ultima_producao,
    }


@_safe
def widget_pastelaria() -> dict:
    today = date.today()
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(quantidade), 0) FROM producao_pastelaria WHERE data = %s",
            (today,)
        )
        itens_hoje = int(cur.fetchone()[0])
        cur.execute(
            """SELECT COUNT(*) FROM ordens_transferencia
               WHERE area_origem = 'Pastelaria' AND status = 'pendente'"""
        )
        transferencias_pendentes = int(cur.fetchone()[0])
    return {
        'itens_hoje': itens_hoje,
        'transferencias_pendentes': transferencias_pendentes,
    }


@_safe
def widget_confeitaria() -> dict:
    today = date.today()
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(quantidade), 0) FROM producao_confeitaria WHERE data = %s",
            (today,)
        )
        itens_hoje = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COALESCE(SUM(quantidade), 0) FROM producao_confeitaria"
        )
        stock_total = int(cur.fetchone()[0])
    return {
        'itens_hoje': itens_hoje,
        'stock_total': stock_total,
    }


@_safe
def widget_faturas() -> dict:
    today = date.today()
    first_of_month = today.replace(day=1)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM invoices WHERE status = 'pending_review'"
        )
        pending = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM invoices WHERE status = 'scheduled'"
        )
        agendadas = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM invoices WHERE status = 'scheduled' AND due_date < %s",
            (today,)
        )
        vencidas = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM invoices WHERE status = 'paid' AND paid_date >= %s",
            (first_of_month,)
        )
        pagas_mes = int(cur.fetchone()[0])
    return {
        'pending': pending,
        'agendadas': agendadas,
        'vencidas': vencidas,
        'pagas_mes': pagas_mes,
    }


@_safe
def widget_logistica() -> dict:
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM ordens_transferencia WHERE status = 'pendente'"
        )
        pendentes = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM ordens_transferencia WHERE status = 'em_curso'"
        )
        em_curso = int(cur.fetchone()[0])
    return {
        'pendentes': pendentes,
        'em_curso': em_curso,
    }


@_safe
def widget_gestor() -> dict:
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users WHERE ativo = TRUE")
        ativos = int(cur.fetchone()[0])
        cur.execute(
            """SELECT MAX(expires_at - INTERVAL '30 days')
               FROM sessions
               WHERE expires_at > NOW()"""
        )
        ultimo_login = cur.fetchone()[0]
    return {
        'ativos': ativos,
        'ultimo_login': ultimo_login,
    }


@_safe
def widget_financeiro() -> dict:
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(plafond), 0) FROM credit_contracts WHERE estado = 'ativo' AND tipo != 'confirming'"
        )
        row = cur.fetchone()
        creditos = int(row[0])
        cur.execute(
            """SELECT COUNT(DISTINCT c.id), COALESCE(SUM(cp.montante), 0)
               FROM credit_contracts c
               LEFT JOIN confirming_parcelas cp ON cp.confirming_contract_id = c.id
                   AND cp.estado IN ('scheduled', 'confirmed')
               WHERE c.tipo = 'confirming' AND c.estado = 'ativo'"""
        )
        row2 = cur.fetchone()
        confirming = int(row2[0])
        exposicao = float(row2[1]) if row2[1] else 0.0
    return {
        'creditos': creditos,
        'confirming': confirming,
        'exposicao': exposicao,
    }


@_safe
def widget_vendas(store_id: int, store_name: str) -> dict:
    today = date.today()
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT total_caixa, updated_at
               FROM fecho_caixa
               WHERE loja_id = %s AND data = %s
               LIMIT 1""",
            (store_id, today)
        )
        row = cur.fetchone()
        vendas_hoje = float(row[0]) if row and row[0] is not None else None
        registado_hoje = row is not None
        cur.execute(
            "SELECT MAX(data) FROM fecho_caixa WHERE loja_id = %s",
            (store_id,)
        )
        ultima_data = cur.fetchone()[0]
    return {
        'store_id': store_id,
        'store_name': store_name,
        'vendas_hoje': vendas_hoje,
        'registado_hoje': registado_hoje,
        'ultima_data': ultima_data,
    }


@_safe
def widget_eventos() -> dict:
    today = date.today()
    em_7_dias = today + timedelta(days=7)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT COUNT(*) FROM events
               WHERE status = 'won' AND event_date BETWEEN %s AND %s""",
            (today, em_7_dias)
        )
        proximos_confirmados = int(cur.fetchone()[0])
        cur.execute(
            """SELECT event_name, event_date FROM events
               WHERE status = 'won' AND event_date >= %s
               ORDER BY event_date ASC LIMIT 1""",
            (today,)
        )
        prox = cur.fetchone()
        proximo_nome = prox[0] if prox else None
        proximo_data = prox[1] if prox else None
        cur.execute(
            "SELECT COUNT(*) FROM lead_requests WHERE status = 'lead'"
        )
        leads_pendentes = int(cur.fetchone()[0])
    return {
        'proximos_confirmados': proximos_confirmados,
        'proximo_nome': proximo_nome,
        'proximo_data': proximo_data,
        'leads_pendentes': leads_pendentes,
    }
