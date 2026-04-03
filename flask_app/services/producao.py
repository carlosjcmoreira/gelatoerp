"""
Producao Service — orchestrates production plan operations.

Sits between routes and the db.plano / db.producao modules,
adding business rules, coordinating cross-domain operations,
and providing KPI-aware cache invalidation after data writes.
"""
import logging
from datetime import date

logger = logging.getLogger(__name__)


# ── Production plan helpers ───────────────────────────────────────────────────

def get_plano_do_dia_com_stock(data: date) -> list:
    """
    Returns the production plan for `data` enriched with current stock levels.
    Each item: {sabor, pesagem_matosinhos, pesagem_bolhao, stock_matosinhos,
                stock_bolhao, real_bolhao, real_matosinhos, real_mouzinho, ...}
    """
    from database import get_plano_do_dia, get_stock_producao
    plano = get_plano_do_dia(data)
    for item in plano:
        sabor = item.get('sabor')
        item['stock_matosinhos'] = get_stock_producao(data, sabor, 'Matosinhos') if sabor else 0
        item['stock_bolhao'] = get_stock_producao(data, sabor, 'Bolhão') if sabor else 0
    return plano


def registar_producao(data: date, loja: str, quantidade_kg: float,
                      tipo: str = 'producao', sabor: str = None) -> None:
    """
    Registers a production record and invalidates KPI cache for that date/store.
    """
    from database import add_producao
    from flask_app.services.kpi import invalidate_for_date
    add_producao(data=data, loja=loja, quantidade_kg=quantidade_kg, tipo=tipo, sabor=sabor)
    invalidate_for_date(data, loja=loja)


def registar_venda(data: date, loja: str, valor_euros: float) -> None:
    """
    Registers a venda and invalidates KPI cache for that date/store.
    """
    from database import add_venda
    from flask_app.services.kpi import invalidate_for_date
    add_venda(data=data, loja=loja, valor_euros=valor_euros)
    invalidate_for_date(data, loja=loja)


def registar_quebra(data: date, loja: str, quantidade_kg: float,
                    motivo: str = None, sabor: str = None, lote: str = None) -> None:
    """
    Registers a quebra and invalidates KPI cache for that date/store.
    """
    from database import add_quebra
    from flask_app.services.kpi import invalidate_for_date
    add_quebra(data=data, loja=loja, quantidade_kg=quantidade_kg,
               motivo=motivo, sabor=sabor, lote=lote)
    invalidate_for_date(data, loja=loja)


# ── Plan creation ─────────────────────────────────────────────────────────────

def guardar_plano_sabor(data: date, sabor: str, pesagem_mat: float, est_bolhao: float,
                        est_matosinhos: float, est_outros: float, est_mouzinho: float,
                        add_to_plan: bool = False) -> None:
    """
    Upsert the production plan for a single sabor on the given date.

    If ``add_to_plan`` is True, also marks the sabor as active in today's plan
    (``marcar_sabor_no_plano``).
    """
    from database import upsert_plano_producao, marcar_sabor_no_plano

    upsert_plano_producao(data, sabor, pesagem_mat, est_bolhao, est_matosinhos,
                          est_outros, est_mouzinho)
    if add_to_plan:
        marcar_sabor_no_plano(data, sabor)


# ── Plan adjustment ───────────────────────────────────────────────────────────

def ajustar_plano_dia(data: date, ajustes: dict) -> int:
    """
    Update estimated quantities for all sabores in an existing day's plan.

    ``ajustes`` maps sabor name → ``{'pesagem_mat', 'est_bol', 'est_mat', 'est_outros', 'est_mou'}``.

    Returns the number of sabores saved.
    """
    from database import upsert_plano_producao

    saved = 0
    for sabor, vals in ajustes.items():
        upsert_plano_producao(
            data, sabor,
            float(vals.get('pesagem_mat', 0) or 0),
            float(vals.get('est_bol', 0) or 0),
            float(vals.get('est_mat', 0) or 0),
            float(vals.get('est_outros', 0) or 0),
            float(vals.get('est_mou', 0) or 0),
        )
        saved += 1
    return saved


# ── Plan execution ────────────────────────────────────────────────────────────

def executar_plano_dia(data: date, sabores_reais: dict) -> dict:
    """
    Apply real production quantities to the day's plan, update stock, and
    auto-create quebras where actual < estimated (first submission only).

    ``sabores_reais`` maps sabor name → ``{'real_b', 'real_m', 'real_mou', 'real_outros'}``.
    ``real_outros`` (Eventos/B2B) is persisted but does NOT update store stock and does NOT
    trigger quebras.

    Returns ``{'registos': int, 'quebras': int}``.
    """
    from database import (
        get_plano_do_dia,
        update_producao_real,
        upsert_stock_producao,
        add_quebra,
    )

    _QUEBRA_IMPLAUSIVEL_LIMITE_KG = 50.0

    entradas = get_plano_do_dia(data)
    registos = 0
    quebras_count = 0

    for e in entradas:
        sabor = e['sabor']
        vals = sabores_reais.get(sabor, {})
        real_b = float(vals.get('real_b', 0) or 0)
        real_m = float(vals.get('real_m', 0) or 0)
        real_mou = float(vals.get('real_mou', 0) or 0)
        real_outros = float(vals.get('real_outros', 0) or 0)

        if not (real_b > 0 or real_m > 0 or real_mou > 0 or real_outros > 0):
            continue

        had_real_b = bool(e['real_bolhao']) and e['real_bolhao'] > 0
        had_real_m = bool(e['real_matosinhos']) and e['real_matosinhos'] > 0
        had_real_mou = bool(e['real_mouzinho']) and e['real_mouzinho'] > 0

        update_producao_real(data, sabor, real_b, real_m,
                             real_mou if real_mou > 0 else None,
                             real_outros if real_outros > 0 else None)
        registos += 1

        if not had_real_b and real_b > 0:
            upsert_stock_producao(data, sabor, 'Bolhão', real_b)
        if not had_real_m and real_m > 0:
            upsert_stock_producao(data, sabor, 'Matosinhos', real_m)
        if not had_real_mou and real_mou > 0:
            upsert_stock_producao(data, sabor, 'Mouzinho', real_mou)

        est_b = e.get('estimado_bolhao') or 0
        est_m = e.get('estimado_matosinhos') or 0
        est_mou = e.get('estimado_mouzinho') or 0

        if not had_real_b and real_b < est_b and est_b > 0:
            diff_b = est_b - real_b
            if diff_b > _QUEBRA_IMPLAUSIVEL_LIMITE_KG:
                logger.warning(
                    "QUEBRA IMPLAUSÍVEL ignorada: %s / Bolhão — estimado=%.2f real=%.2f diff=%.2f kg (limite=%.0f kg)",
                    sabor, est_b, real_b, diff_b, _QUEBRA_IMPLAUSIVEL_LIMITE_KG)
            else:
                add_quebra(data, 'Bolhão', diff_b,
                           'Quebra de produção (estimado vs real)', sabor)
                quebras_count += 1
        if not had_real_m and real_m < est_m and est_m > 0:
            diff_m = est_m - real_m
            if diff_m > _QUEBRA_IMPLAUSIVEL_LIMITE_KG:
                logger.warning(
                    "QUEBRA IMPLAUSÍVEL ignorada: %s / Matosinhos — estimado=%.2f real=%.2f diff=%.2f kg (limite=%.0f kg)",
                    sabor, est_m, real_m, diff_m, _QUEBRA_IMPLAUSIVEL_LIMITE_KG)
            else:
                add_quebra(data, 'Matosinhos', diff_m,
                           'Quebra de produção (estimado vs real)', sabor)
                quebras_count += 1
        if not had_real_mou and real_mou < est_mou and est_mou > 0:
            diff_mou = est_mou - real_mou
            if diff_mou > _QUEBRA_IMPLAUSIVEL_LIMITE_KG:
                logger.warning(
                    "QUEBRA IMPLAUSÍVEL ignorada: %s / Mouzinho — estimado=%.2f real=%.2f diff=%.2f kg (limite=%.0f kg)",
                    sabor, est_mou, real_mou, diff_mou, _QUEBRA_IMPLAUSIVEL_LIMITE_KG)
            else:
                add_quebra(data, 'Mouzinho', diff_mou,
                           'Quebra de produção (estimado vs real)', sabor)
                quebras_count += 1

    return {'registos': registos, 'quebras': quebras_count}


# ── Sabores helpers ───────────────────────────────────────────────────────────

def get_sabores_para_plano() -> list:
    """
    Returns the ordered sabores list for production plan display.
    Combines the ordem_producao ordering with the full sabores list.
    """
    from database import get_ordem_producao, get_sabores_list
    ordem = get_ordem_producao()
    if ordem:
        return [item['sabor'] for item in ordem]
    return get_sabores_list()
