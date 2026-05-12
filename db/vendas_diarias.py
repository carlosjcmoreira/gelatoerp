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


def _iso_year_bounds(iso_year: int):
    """Return (first_day, last_day) of the given ISO year.

    The ISO year always starts on a Monday and ends on a Sunday.
    Dec 28 is always within the last ISO week of its calendar year,
    so isocalendar()[1] on Dec 28 gives the highest week number.
    """
    first_day = date.fromisocalendar(iso_year, 1, 1)
    last_week = date(iso_year, 12, 28).isocalendar()[1]
    last_day = date.fromisocalendar(iso_year, last_week, 7)
    return first_day, last_day


def get_vendas_diarias_yoy(ano: int, mes: int = None, iso_week: int = None, weekday: int = None) -> list:
    """Return daily sales for ISO year *ano* compared with the same ISO-week/weekday in ISO year *ano-1*.

    Parameters
    ----------
    ano      : target ISO year (all weeks whose ISO year == ano are shown)
    mes      : calendar month filter (1-12), or None for all months
    iso_week : ISO week number filter (1-53), or None for all weeks
    weekday  : weekday filter 0=Monday … 6=Sunday (Python convention), or None

    The iteration covers the full ISO year of *ano* — i.e. from the Monday
    of ISO W1 through the Sunday of the last ISO week.  This means days like
    Dec 29-31 (which belong to ISO W1 of the *following* calendar year) are
    correctly shown in the *preceding* ISO year view, and days like Dec 30-31
    of the previous calendar year (which open ISO W1 of *ano*) are shown at
    the top of the *ano* view.

    Returns
    -------
    list of dicts with keys:
        data, iso_week, weekday, day_abbr, date_label, is_future,
        total_atual, bolhao_atual, matosinhos_atual,
        total_anterior, bolhao_anterior, matosinhos_anterior,
        week_rowspan  (>0 for first row in ISO-week group, 0 otherwise)
    """
    today = date.today()

    iter_start, iter_end = _iso_year_bounds(ano)
    prior_start, prior_end = _iso_year_bounds(ano - 1)

    with db_connection() as conn:
        cur = conn.cursor()

        # ── current ISO year: vendas_detalhe (primary — Gestor uploads) ───────
        # Uses date range matching the full ISO year so ISO-boundary days
        # (e.g. Dec 29-31 that are ISO W1 of ano but calendar year ano-1)
        # are included correctly.
        # Only sums products where conta_vendas_diarias = TRUE (or not yet in
        # config — NULL coalesce keeps backward-compatibility).
        current_sales: dict = {}
        try:
            cur.execute("""
                SELECT vd.data, vd.loja, SUM(vd.valor_euros)
                FROM vendas_detalhe vd
                LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE vd.data BETWEEN %s AND %s
                  AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
                GROUP BY vd.data, vd.loja
            """, (iter_start, iter_end))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                current_sales.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: vendas_detalhe (current) failed: %s", exc)

        # ── current ISO year: vendas fallback (cash-close totals) ────────────
        try:
            cur.execute("""
                SELECT data, loja, SUM(valor_euros)
                FROM vendas
                WHERE data BETWEEN %s AND %s
                GROUP BY data, loja
            """, (iter_start, iter_end))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                if loja not in current_sales.get(d, {}):
                    current_sales.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: vendas (current) failed: %s", exc)

        # ── current ISO year: sales_historico fallback ────────────────────────
        try:
            cur.execute("""
                SELECT data, loja, SUM(valor_euros)
                FROM sales_historico
                WHERE data BETWEEN %s AND %s
                GROUP BY data, loja
            """, (iter_start, iter_end))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                if loja not in current_sales.get(d, {}):
                    current_sales.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: sales_historico (current) failed: %s", exc)

        # ── prior ISO year: vendas_detalhe (primary) ──────────────────────────
        # Load the full prior ISO year so that ISO-boundary days at the start
        # of ano (e.g. Dec 30-31 of ano-1 that open ISO W1 of ano) can be
        # compared against Dec 30-31 of ano-2 that open ISO W1 of ano-1.
        # Apply the same conta_vendas_diarias filter so YoY comparison is
        # consistent — if a product is excluded from ano, it's excluded from
        # ano-1 too.
        prior_detalhe: dict = {}
        try:
            cur.execute("""
                SELECT vd.data, vd.loja, SUM(vd.valor_euros)
                FROM vendas_detalhe vd
                LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE vd.data BETWEEN %s AND %s
                  AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
                GROUP BY vd.data, vd.loja
            """, (prior_start, prior_end))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                prior_detalhe.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: vendas_detalhe (prior) failed: %s", exc)

        # ── prior ISO year: vendas fallback ───────────────────────────────────
        prior_vendas: dict = {}
        try:
            cur.execute("""
                SELECT data, loja, SUM(valor_euros)
                FROM vendas
                WHERE data BETWEEN %s AND %s
                GROUP BY data, loja
            """, (prior_start, prior_end))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                if loja not in prior_detalhe.get(d, {}):
                    prior_vendas.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: vendas (prior) failed: %s", exc)

        # ── prior ISO year: sales_historico fallback ──────────────────────────
        prior_historico: dict = {}
        try:
            cur.execute("""
                SELECT data, loja, SUM(valor_euros)
                FROM sales_historico
                WHERE data BETWEEN %s AND %s
                GROUP BY data, loja
            """, (prior_start, prior_end))
            for row in cur.fetchall():
                d, loja, total = row[0], row[1], float(row[2] or 0)
                if loja not in prior_detalhe.get(d, {}) and loja not in prior_vendas.get(d, {}):
                    prior_historico.setdefault(d, {})[loja] = total
        except Exception as exc:
            logger.warning("get_vendas_diarias_yoy: sales_historico (prior) failed: %s", exc)

    def _get_prior(d, loja):
        if d in prior_detalhe and loja in prior_detalhe[d]:
            return prior_detalhe[d][loja]
        if d in prior_vendas and loja in prior_vendas[d]:
            return prior_vendas[d][loja]
        if d in prior_historico and loja in prior_historico[d]:
            return prior_historico[d][loja]
        return None

    rows = []
    cur_date = iter_start

    while cur_date <= iter_end:
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

        is_boundary = cur_date.year != ano
        if is_boundary:
            yr_suffix = f" '{str(cur_date.year)[2:]}"
        else:
            yr_suffix = ""
        date_label = f"{cur_date.day} {_MONTHS_SHORT[cur_date.month]}{yr_suffix}"

        rows.append({
            'data': cur_date,
            'iso_week': w,
            'weekday': wd_py,
            'day_abbr': _DAY_ABBR[wd_py],
            'date_label': date_label,
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


def get_dashboard_vendas() -> dict:
    """Return aggregated sales data for the Dashboard de Vendas.

    Returns a dict with:
      lojas       — list of store names
      cutoff      — last date with 2026 data (ISO string)
      monthly     — {filter_key: {year: {month: total}}}
                    filter_key ∈ {"total"} ∪ lojas
      ytd         — {filter_key: {y2026, y2025, diff_eur, diff_pct}}
    """
    with db_connection() as conn:
        cur = conn.cursor()

        # ── Monthly aggregates for 2025 and 2026 ─────────────────────────────
        # Applies the same conta_vendas_diarias filter as get_vendas_diarias_yoy()
        # so excluded products (e.g. consultoria, catering) are omitted here too.
        cur.execute("""
            SELECT vd.loja,
                   EXTRACT(YEAR FROM vd.data)::int  AS ano,
                   EXTRACT(MONTH FROM vd.data)::int AS mes,
                   SUM(vd.valor_euros)              AS total
            FROM vendas_detalhe vd
            LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
            WHERE EXTRACT(YEAR FROM vd.data) IN (2025, 2026)
              AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
            GROUP BY vd.loja, ano, mes
            ORDER BY vd.loja, ano, mes
        """)
        monthly_rows = cur.fetchall()

        # ── Last date with 2026 data ──────────────────────────────────────────
        cur.execute("""
            SELECT MAX(data) FROM vendas_detalhe
            WHERE EXTRACT(YEAR FROM data) = 2026
        """)
        cutoff = cur.fetchone()[0]  # date or None

        # ── YTD sums: per loja ────────────────────────────────────────────────
        if cutoff:
            cur.execute("""
                SELECT vd.loja,
                       SUM(CASE WHEN EXTRACT(YEAR FROM vd.data) = 2026 THEN vd.valor_euros ELSE 0 END) AS ytd_2026,
                       SUM(CASE WHEN EXTRACT(YEAR FROM vd.data) = 2025
                                 AND EXTRACT(MONTH FROM vd.data) * 100 + EXTRACT(DAY FROM vd.data)
                                     <= EXTRACT(MONTH FROM %s::date) * 100 + EXTRACT(DAY FROM %s::date)
                                THEN vd.valor_euros ELSE 0 END) AS ytd_2025
                FROM vendas_detalhe vd
                LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE EXTRACT(YEAR FROM vd.data) IN (2025, 2026)
                  AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
                GROUP BY vd.loja
                ORDER BY vd.loja
            """, (cutoff, cutoff))
            ytd_rows = cur.fetchall()
        else:
            ytd_rows = []

    # ── Build monthly structure ───────────────────────────────────────────────
    lojas: list[str] = []
    # monthly[filter_key][year][month] = total
    monthly: dict = {'total': {2025: {}, 2026: {}}}

    for loja, ano, mes, total in monthly_rows:
        if loja not in lojas:
            lojas.append(loja)
        if loja not in monthly:
            monthly[loja] = {2025: {}, 2026: {}}
        monthly[loja].setdefault(ano, {})[mes] = round(float(total or 0), 2)
        monthly['total'].setdefault(ano, {})[mes] = round(
            monthly['total'].get(ano, {}).get(mes, 0.0) + float(total or 0), 2
        )

    lojas.sort()

    # ── Build YTD structure ───────────────────────────────────────────────────
    def _ytd_entry(y2026: float, y2025: float) -> dict:
        diff_eur = round(y2026 - y2025, 2)
        diff_pct = round((y2026 - y2025) / y2025 * 100, 1) if y2025 else None
        return {
            'y2026': round(y2026, 2),
            'y2025': round(y2025, 2),
            'diff_eur': diff_eur,
            'diff_pct': diff_pct,
        }

    ytd: dict = {'total': _ytd_entry(0.0, 0.0)}
    for loja in lojas:
        ytd[loja] = _ytd_entry(0.0, 0.0)

    for loja, y2026, y2025 in ytd_rows:
        entry = _ytd_entry(float(y2026 or 0), float(y2025 or 0))
        ytd[loja] = entry
        ytd['total'] = _ytd_entry(
            ytd['total']['y2026'] + entry['y2026'],
            ytd['total']['y2025'] + entry['y2025'],
        )

    return {
        'lojas': lojas,
        'cutoff': cutoff.isoformat() if cutoff else None,
        'monthly': monthly,
        'ytd': ytd,
    }
