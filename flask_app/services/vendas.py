"""
Vendas Service — business logic for the store-facing sales dashboard and quebra registration.

Routes call these functions after parsing HTTP input; the service handles
DB coordination and data transformation, returning plain dicts or raising ServiceError.
"""
import logging
from datetime import date

logger = logging.getLogger(__name__)


# ── Dashboard ─────────────────────────────────────────────────────────────────

def build_dashboard_rows(loja_nome: str) -> dict:
    """
    Fetch and transform today's sales dashboard data for the given store.

    Returns a dict:
        {
            'rows': list of {sabor, pesagem_ontem, recebido_hoje, pesagem_fim},
            'total_ontem': str,
            'total_recebido': str,
            'total_fim': str | None,
        }
    All numeric strings are formatted to 3 decimal places.
    """
    from database import get_vendas_bolhao_dashboard_data

    dashboard_data = get_vendas_bolhao_dashboard_data(loja_nome)
    rows = []
    total_ontem = 0.0
    total_recebido = 0.0
    total_fim = 0.0
    has_fim = False

    for r in dashboard_data:
        pesagem_ontem = r.get('Pesagem Ontem (kg)', 0) or 0
        recebido = r.get('Recebido Hoje (kg)', 0) or 0
        pesagem_fim = r.get('Pesagem Fim Dia (kg)')
        total_ontem += pesagem_ontem
        total_recebido += recebido
        if pesagem_fim is not None:
            has_fim = True
            total_fim += pesagem_fim
        rows.append({
            'sabor': r.get('Sabor', ''),
            'pesagem_ontem': f'{pesagem_ontem:.3f}',
            'recebido_hoje': f'{recebido:.3f}',
            'pesagem_fim': f'{pesagem_fim:.3f}' if pesagem_fim is not None else '—',
        })

    return {
        'rows': rows,
        'total_ontem': f'{total_ontem:.3f}',
        'total_recebido': f'{total_recebido:.3f}',
        'total_fim': f'{total_fim:.3f}' if has_fim else None,
    }


# ── Quebra registration ───────────────────────────────────────────────────────

def registar_quebra_venda(loja: str, data: date, tipo: str, produto: str,
                           quantidade: float, lote: str, motivo: str) -> str:
    """
    Validate and register a quebra for a store.

    ``tipo`` is ``'Gelado'`` or a pastelaria/confeitaria type.

    Returns a success message string.
    Raises ``ServiceError`` with user-visible message on validation failure.
    """
    from flask_app.services import ServiceError
    from database import add_quebra

    if not (quantidade > 0 and produto and lote and motivo):
        raise ServiceError('Preencha todos os campos obrigatórios incluindo Lote e Motivo.')

    add_quebra(data, loja, float(quantidade), motivo, produto, lote)
    unidade_msg = 'kg' if tipo == 'Gelado' else 'unidades'
    return f'Quebra de {quantidade} {unidade_msg} de {produto} (Lote: {lote}) registada!'


# ── Quebra deletion (with ownership check) ────────────────────────────────────

def apagar_quebra_venda(quebra_id: int, loja_nome: str) -> None:
    """
    Delete a quebra record, verifying it belongs to ``loja_nome``.

    Raises ``ServiceError`` if the record does not belong to the store.
    """
    from flask_app.services import ServiceError
    from database import get_quebra_by_id, delete_quebra

    record = get_quebra_by_id(quebra_id)
    if not record or record.get('loja', '') != loja_nome:
        raise ServiceError('Sem permissão para eliminar este registo.')

    delete_quebra(quebra_id)
