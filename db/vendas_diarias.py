"""Vendas Diárias: year-over-year daily sales comparison."""
from datetime import date, timedelta
import logging
from db.connection import db_connection

logger = logging.getLogger(__name__)

_BOLHAO = 'Bolhão'
_MATOSINHOS = 'Matosinhos'

_DAY_ABBR = ['S', 'T', 'Q', 'Q', 'S', 'S', 'D']
_MONTHS_SHORT = ['', 'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']


def get_vendas_diarias_yoy(ano: int, mes: int = None, iso_week: int = None, weekday: int = None) -> list:
    """Return daily sales for *ano* compared with same ISO-week/weekday in *ano-1*.

    Parameters
    ----------
    ano      : target year
    mes      : calendar month filter (1-12), or None for all months
    iso_week : ISO week number filter (1-53), or None for all weeks
    weekday  : weekday filter 0=Monday … 6=Sunday (Python convention), or None

    Returns
    -------
    list of dicts with keys:
        data, iso_week, weekday, day_abbr, date_label, is_future,
        total_atual, bolhao_atual, matosinhos_atual,
        total_anterior, bolhao_anterior, matosinhos_anterior,
        week_rowspan  (>0 for first row in ISO-week group, 0 otherwise)
    """
    today = date.today()

    with db_connection() as conn:
        cur = conn.cursor()

        # ── current year: vendas (priority) ──────────────────────────────────
        cur.execute("""
            SELECT data, loja, SUM(valor_euros)
            FROM vendas
            WHERE EXTRACT(YEAR FROM data) = %s
            GROUP BY data, loja
        """, (ano,))
        current_sales: dict = {}
        for row in cur.fetchall():
            d, loja, total = row[0], row[1], float(row[2] or 0)
            current_sales.setdefault(d, {})[loja] = total

        # ── current year: vendas_detalhe fallback ────────────────────────────
        try:
            cur.execute("""
                SELECT data, loja, SUM(valor_euros)
                FROM vendas_detalhe
                WHERE EXTRACT(YEAR FROM data) = %s
                GROUP BY data, loja
            """, (ano,))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                if loja not in current_sales.get(d, {}):
                    current_sales.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: vendas_detalhe (current) failed: %s", exc)

        # ── prior year: vendas (priority) ────────────────────────────────────
        cur.execute("""
            SELECT data, loja, SUM(valor_euros)
            FROM vendas
            WHERE EXTRACT(YEAR FROM data) = %s
            GROUP BY data, loja
        """, (ano - 1,))
        prior_vendas: dict = {}
        for row in cur.fetchall():
            d, loja, total = row[0], row[1], float(row[2] or 0)
            prior_vendas.setdefault(d, {})[loja] = total

        # ── prior year: sales_historico fallback ─────────────────────────────
        prior_historico: dict = {}
        try:
            cur.execute("""
                SELECT data, loja, SUM(valor_euros)
                FROM sales_historico
                WHERE EXTRACT(YEAR FROM data) = %s
                GROUP BY data, loja
            """, (ano - 1,))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                prior_historico.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: sales_historico failed: %s", exc)

        # ── prior year: vendas_detalhe fallback ──────────────────────────────
        prior_detalhe: dict = {}
        try:
            cur.execute("""
                SELECT data, loja, SUM(valor_euros)
                FROM vendas_detalhe
                WHERE EXTRACT(YEAR FROM data) = %s
                GROUP BY data, loja
            """, (ano - 1,))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                prior_detalhe.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: vendas_detalhe (prior) failed: %s", exc)

    def _get_prior(d, loja):
        if d in prior_vendas and loja in prior_vendas[d]:
            return prior_vendas[d][loja]
        if d in prior_historico and loja in prior_historico[d]:
            return prior_historico[d][loja]
        if d in prior_detalhe and loja in prior_detalhe[d]:
            return prior_detalhe[d][loja]
        return None

    rows = []
    cur_date = date(ano, 1, 1)
    year_end = date(ano, 12, 31)

    while cur_date <= year_end:
        iso_cal = cur_date.isocalendar()
        w = iso_cal[1]
        wd_iso = iso_cal[2]
        wd_py = wd_iso - 1

        if mes is not None and cur_date.month != mes:
            cur_date += timedelta(days=1)
            continue
        if iso_week is not None and w != iso_week:
            cur_date += timedelta(days=1)
            continue
        if weekday is not None and wd_py != weekday:
            cur_date += timedelta(days=1)
            continue

        try:
            prior_date = date.fromisocalendar(ano - 1, w, wd_iso)
        except ValueError:
            prior_date = None

        curr_day = current_sales.get(cur_date, {})
        bolhao_atual = curr_day.get(_BOLHAO)
        matosinhos_atual = curr_day.get(_MATOSINHOS)
        if bolhao_atual is not None or matosinhos_atual is not None:
            total_atual = (bolhao_atual or 0) + (matosinhos_atual or 0)
        else:
            total_atual = None

        if prior_date:
            bolhao_anterior = _get_prior(prior_date, _BOLHAO)
            matosinhos_anterior = _get_prior(prior_date, _MATOSINHOS)
            if bolhao_anterior is not None or matosinhos_anterior is not None:
                total_anterior = (bolhao_anterior or 0) + (matosinhos_anterior or 0)
            else:
                total_anterior = None
        else:
            bolhao_anterior = None
            matosinhos_anterior = None
            total_anterior = None

        rows.append({
            'data': cur_date,
            'iso_week': w,
            'weekday': wd_py,
            'day_abbr': _DAY_ABBR[wd_py],
            'date_label': f"{cur_date.day} {_MONTHS_SHORT[cur_date.month]}",
            'is_future': cur_date > today,
            'total_atual': total_atual,
            'bolhao_atual': bolhao_atual,
            'matosinhos_atual': matosinhos_atual,
            'total_anterior': total_anterior,
            'bolhao_anterior': bolhao_anterior,
            'matosinhos_anterior': matosinhos_anterior,
            'week_rowspan': 0,
        })

        cur_date += timedelta(days=1)

    if rows:
        i = 0
        while i < len(rows):
            w_num = rows[i]['iso_week']
            j = i + 1
            while j < len(rows) and rows[j]['iso_week'] == w_num:
                j += 1
            rows[i]['week_rowspan'] = j - i
            i = j

    return rows
