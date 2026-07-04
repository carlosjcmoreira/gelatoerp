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


def get_variaveis_previsao() -> dict:
    """Return sales × weather pivot data for 2026 for the Variáveis de Previsão tile.

    Returns a dict with:
      summary — list of {month, dow, n_days, avg_total, avg_mat, avg_bol,
                          avg_tmax_mat, avg_tmax_bol, avg_precip}
      detail  — list of {date, month, dow, total, mat, bol,
                          tmax_mat, tmax_bol, precip}

    store_id 1 = Matosinhos, store_id 2 = Bolhão.
    Weather: AVG(temperatura_max) and AVG(precipitacao_mm) across all sources per store×date.
    Precipitation for detail/summary: average of both stores when both available, otherwise
    whichever is present.
    """
    _CTE = """
        WITH daily_sales AS (
            SELECT
                vd.data,
                SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Matosinhos') AS sales_mat,
                SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Bolhão')     AS sales_bol,
                SUM(vd.valor_euros)                                         AS sales_total
            FROM vendas_detalhe vd
            LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
            WHERE EXTRACT(YEAR FROM vd.data) = 2026
              AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
            GROUP BY vd.data
        ),
        wx_mat AS (
            SELECT data,
                   AVG(temperatura_max)  AS tmax,
                   AVG(precipitacao_mm)  AS precip
            FROM weather_data
            WHERE store_id = 1 AND EXTRACT(YEAR FROM data) = 2026
            GROUP BY data
        ),
        wx_bol AS (
            SELECT data,
                   AVG(temperatura_max)  AS tmax,
                   AVG(precipitacao_mm)  AS precip
            FROM weather_data
            WHERE store_id = 2 AND EXTRACT(YEAR FROM data) = 2026
            GROUP BY data
        )
    """

    _PRECIP_EXPR = """
        CASE
            WHEN wx_mat.precip IS NOT NULL AND wx_bol.precip IS NOT NULL
                THEN (wx_mat.precip + wx_bol.precip) / 2.0
            ELSE COALESCE(wx_mat.precip, wx_bol.precip)
        END
    """

    with db_connection() as conn:
        cur = conn.cursor()

        cur.execute(_CTE + f"""
            SELECT
                EXTRACT(MONTH  FROM ds.data)::int AS month,
                EXTRACT(ISODOW FROM ds.data)::int AS dow,
                COUNT(*)                           AS n_days,
                ROUND(AVG(ds.sales_total)::numeric, 2)  AS avg_total,
                ROUND(AVG(ds.sales_mat)::numeric,   2)  AS avg_mat,
                ROUND(AVG(ds.sales_bol)::numeric,   2)  AS avg_bol,
                ROUND(AVG(wx_mat.tmax)::numeric,    1)  AS avg_tmax_mat,
                ROUND(AVG(wx_bol.tmax)::numeric,    1)  AS avg_tmax_bol,
                ROUND(AVG({_PRECIP_EXPR})::numeric, 1)  AS avg_precip
            FROM daily_sales ds
            LEFT JOIN wx_mat ON wx_mat.data = ds.data
            LEFT JOIN wx_bol ON wx_bol.data = ds.data
            GROUP BY month, dow
            ORDER BY month, dow
        """)
        summary_rows = cur.fetchall()

        cur.execute(_CTE + f"""
            SELECT
                ds.data,
                EXTRACT(MONTH  FROM ds.data)::int AS month,
                EXTRACT(ISODOW FROM ds.data)::int AS dow,
                ROUND(ds.sales_total::numeric, 2) AS total,
                ROUND(ds.sales_mat::numeric,   2) AS mat,
                ROUND(ds.sales_bol::numeric,   2) AS bol,
                ROUND(wx_mat.tmax::numeric,    1) AS tmax_mat,
                ROUND(wx_bol.tmax::numeric,    1) AS tmax_bol,
                ROUND(({_PRECIP_EXPR})::numeric, 1) AS precip
            FROM daily_sales ds
            LEFT JOIN wx_mat ON wx_mat.data = ds.data
            LEFT JOIN wx_bol ON wx_bol.data = ds.data
            ORDER BY ds.data
        """)
        detail_rows = cur.fetchall()

    def _f(v):
        return float(v) if v is not None else None

    summary = [
        {
            'month':       int(r[0]),
            'dow':         int(r[1]),
            'n_days':      int(r[2]),
            'avg_total':   _f(r[3]),
            'avg_mat':     _f(r[4]),
            'avg_bol':     _f(r[5]),
            'avg_tmax_mat': _f(r[6]),
            'avg_tmax_bol': _f(r[7]),
            'avg_precip':  _f(r[8]),
        }
        for r in summary_rows
    ]

    detail = [
        {
            'date':     r[0].isoformat(),
            'month':    int(r[1]),
            'dow':      int(r[2]),
            'total':    _f(r[3]),
            'mat':      _f(r[4]),
            'bol':      _f(r[5]),
            'tmax_mat': _f(r[6]),
            'tmax_bol': _f(r[7]),
            'precip':   _f(r[8]),
        }
        for r in detail_rows
    ]

    return {'summary': summary, 'detail': detail}


def get_previsao_30dias() -> dict:
    """Return 30-day sales forecast using 2026 historical DOW averages × weather × recent performance.

    Model per day:
      est_sales = avg_dow_sales × tmax_ratio × rain_factor × perf_factor
      tmax_ratio   = forecast_tmax / avg_dow_tmax  (capped [0.6, 1.4], default 1 if missing)
      rain_factor  = 0.85 if precip > 3 mm, 0.93 if precip > 0.5 mm, else 1.0
      perf_factor  = avg(actual / hist_dow_avg) over last 30 days with data, capped [0.7, 1.3]

    Per-store estimates (mat/bol) use the same formula independently.
    perf_factor is computed separately for Matosinhos and Bolhão.

    Returns dict with:
      forecast        — list of 30 days {date, dow, dow_name,
                         tmax_mat, tmax_bol, precip,
                         est_total, est_mat, est_bol,
                         real_total, real_mat, real_bol, is_today}
      historical_dow  — {dow: {avg_total, avg_mat, avg_bol,
                                avg_tmax_mat, avg_tmax_bol, n_days}}
      perf_factor_mat — recent performance multiplier for Matosinhos (float or None)
      perf_factor_bol — recent performance multiplier for Bolhão (float or None)
      perf_days       — number of recent days used to compute the factors
      cutoff          — ISO date of last day with actual vendas_detalhe data
    """
    today = date.today()
    start = today
    end = today + timedelta(days=29)
    recent_start = today - timedelta(days=30)
    recent_end = today - timedelta(days=1)

    DOW_NAMES_PT = ['', 'Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']

    with db_connection() as conn:
        cur = conn.cursor()

        # ── 1. Historical 2026 DOW averages (sales + weather), excl. recent 30d ─
        # We include all 2026 past data so the baseline is robust; the recent
        # performance factor will then capture any drift vs. that baseline.
        cur.execute("""
            WITH daily_sales AS (
                SELECT
                    vd.data,
                    SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Matosinhos') AS sales_mat,
                    SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Bolhão')     AS sales_bol,
                    SUM(vd.valor_euros)                                         AS sales_total
                FROM vendas_detalhe vd
                LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE EXTRACT(YEAR FROM vd.data) = 2026
                  AND vd.data < %s
                  AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
                GROUP BY vd.data
            ),
            wx_mat AS (
                SELECT data, AVG(temperatura_max) AS tmax
                FROM weather_data
                WHERE store_id = 1 AND EXTRACT(YEAR FROM data) = 2026 AND data < %s
                GROUP BY data
            ),
            wx_bol AS (
                SELECT data, AVG(temperatura_max) AS tmax
                FROM weather_data
                WHERE store_id = 2 AND EXTRACT(YEAR FROM data) = 2026 AND data < %s
                GROUP BY data
            )
            SELECT
                EXTRACT(ISODOW FROM ds.data)::int AS dow,
                COUNT(*) AS n_days,
                ROUND(AVG(ds.sales_total)::numeric, 2) AS avg_total,
                ROUND(AVG(ds.sales_mat)::numeric, 2)   AS avg_mat,
                ROUND(AVG(ds.sales_bol)::numeric, 2)   AS avg_bol,
                ROUND(AVG(wx_mat.tmax)::numeric, 1)    AS avg_tmax_mat,
                ROUND(AVG(wx_bol.tmax)::numeric, 1)    AS avg_tmax_bol
            FROM daily_sales ds
            LEFT JOIN wx_mat ON wx_mat.data = ds.data
            LEFT JOIN wx_bol ON wx_bol.data = ds.data
            GROUP BY dow
            ORDER BY dow
        """, (today, today, today))
        hist_rows = cur.fetchall()

        # ── 2. Recent actual sales (last 30 days) for performance factor ────────
        cur.execute("""
            SELECT vd.data,
                   SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Matosinhos') AS mat,
                   SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Bolhão')     AS bol
            FROM vendas_detalhe vd
            LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
            WHERE vd.data BETWEEN %s AND %s
              AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
            GROUP BY vd.data
            ORDER BY vd.data
        """, (recent_start, recent_end))
        recent_rows = cur.fetchall()

        # ── 3. Future weather for next 30 days (avg across fontes per store) ────
        cur.execute("""
            SELECT
                data,
                store_id,
                ROUND(AVG(temperatura_max)::numeric, 1) AS tmax,
                ROUND(AVG(precipitacao_mm)::numeric, 1) AS precip
            FROM weather_data
            WHERE data BETWEEN %s AND %s
            GROUP BY data, store_id
            ORDER BY data, store_id
        """, (start, end))
        wx_future_rows = cur.fetchall()

        # ── 3b. Past weather for last 30 days (for accuracy computation) ────────
        cur.execute("""
            SELECT
                data,
                store_id,
                ROUND(AVG(temperatura_max)::numeric, 1) AS tmax,
                ROUND(AVG(precipitacao_mm)::numeric, 1) AS precip
            FROM weather_data
            WHERE data BETWEEN %s AND %s
            GROUP BY data, store_id
            ORDER BY data, store_id
        """, (recent_start, recent_end))
        wx_past_rows = cur.fetchall()

        # ── 4. Actual sales for the forecast window (today onward, for comparison)
        cur.execute("""
            SELECT vd.data,
                   SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Matosinhos') AS mat,
                   SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Bolhão')     AS bol,
                   SUM(vd.valor_euros)                                         AS total
            FROM vendas_detalhe vd
            LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
            WHERE vd.data BETWEEN %s AND %s
              AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
            GROUP BY vd.data
        """, (start, end))
        actual_rows = cur.fetchall()

        # ── 5. Last date with actual data ────────────────────────────────────────
        cur.execute("""
            SELECT MAX(data) FROM vendas_detalhe
            WHERE EXTRACT(YEAR FROM data) = 2026
        """)
        cutoff_row = cur.fetchone()

        # ── 6. Calibrated rain factors per store from forecast_meteo_config ─────
        try:
            cur.execute("SAVEPOINT sp_meteo_rain")
            cur.execute("""
                SELECT COALESCE(fmc.store_id, s.id) AS sid,
                       fmc.score_min, fmc.multiplicador
                FROM forecast_meteo_config fmc
                LEFT JOIN stores s ON s.name = fmc.loja AND s.is_active = TRUE
                WHERE fmc.score_min IN (0, 30, 60)
                  AND fmc.loja IN ('Matosinhos', 'Bolhão')
            """)
            _meteo_cfg_rows = cur.fetchall()
            cur.execute("RELEASE SAVEPOINT sp_meteo_rain")
        except Exception:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sp_meteo_rain")
            except Exception:
                pass
            _meteo_cfg_rows = []

    # ── Build historical DOW lookup ─────────────────────────────────────────────
    hist_by_dow: dict = {}
    for r in hist_rows:
        dow = int(r[0])
        hist_by_dow[dow] = {
            'n_days':       int(r[1]),
            'avg_total':    float(r[2]) if r[2] is not None else None,
            'avg_mat':      float(r[3]) if r[3] is not None else None,
            'avg_bol':      float(r[4]) if r[4] is not None else None,
            'avg_tmax_mat': float(r[5]) if r[5] is not None else None,
            'avg_tmax_bol': float(r[6]) if r[6] is not None else None,
        }

    # ── Compute recent performance factors ─────────────────────────────────────
    # For each recent day with actual sales, compute ratio = actual / hist_dow_avg.
    # Average these ratios per store and cap to [0.7, 1.3].
    ratios_mat: list = []
    ratios_bol: list = []
    for r in recent_rows:
        d_actual = r[0]
        act_mat = float(r[1]) if r[1] is not None else None
        act_bol = float(r[2]) if r[2] is not None else None
        dow = d_actual.isoweekday()
        h = hist_by_dow.get(dow, {})
        if act_mat is not None and h.get('avg_mat') and h['avg_mat'] > 0:
            ratios_mat.append(act_mat / h['avg_mat'])
        if act_bol is not None and h.get('avg_bol') and h['avg_bol'] > 0:
            ratios_bol.append(act_bol / h['avg_bol'])

    def _avg_capped(ratios, lo=0.4, hi=2.5):
        if not ratios:
            return None
        avg = sum(ratios) / len(ratios)
        return round(max(lo, min(hi, avg)), 3)

    perf_factor_mat = _avg_capped(ratios_mat)
    perf_factor_bol = _avg_capped(ratios_bol)
    perf_days = max(len(ratios_mat), len(ratios_bol))

    # ── Build future weather lookup ─────────────────────────────────────────────
    wx_future: dict = {}
    for r in wx_future_rows:
        d, sid = r[0], int(r[1])
        wx_future.setdefault(d, {})[sid] = {
            'tmax':   float(r[2]) if r[2] is not None else None,
            'precip': float(r[3]) if r[3] is not None else None,
        }

    # ── Build past weather lookup (for accuracy computation) ────────────────────
    wx_past: dict = {}
    for r in wx_past_rows:
        d, sid = r[0], int(r[1])
        wx_past.setdefault(d, {})[sid] = {
            'tmax':   float(r[2]) if r[2] is not None else None,
            'precip': float(r[3]) if r[3] is not None else None,
        }

    actual_by_date: dict = {}
    for r in actual_rows:
        actual_by_date[r[0]] = {
            'mat':   float(r[1]) if r[1] is not None else None,
            'bol':   float(r[2]) if r[2] is not None else None,
            'total': float(r[3]) if r[3] is not None else None,
        }

    cutoff = cutoff_row[0] if cutoff_row and cutoff_row[0] else None

    # ── Calibrated rain factors per store ────────────────────────────────────────
    # Band (0–29) = heavy rain proxy; band (30–59) = light rain proxy.
    # Both are normalised by the neutral band (60–84) so the reference = 1.0.
    _raw_meteo: dict = {}  # {store_id: {score_min: multiplier}}
    for _r in _meteo_cfg_rows:
        _sid = int(_r[0]) if _r[0] is not None else None
        if _sid is None:
            continue
        _raw_meteo.setdefault(_sid, {})[int(_r[1])] = float(_r[2])

    _calibrated_rain: dict = {}
    for _sid, _bands in _raw_meteo.items():
        _neutral = _bands.get(60, 1.0)
        if not _neutral or _neutral <= 0:
            _neutral = 1.0
        _raw_heavy = _bands.get(0)
        _raw_light = _bands.get(30)
        _calibrated_rain[_sid] = {
            'heavy': round(_raw_heavy / _neutral, 4) if _raw_heavy is not None else 0.85,
            'light': round(_raw_light / _neutral, 4) if _raw_light is not None else 0.93,
        }

    def _tmax_ratio(forecast_tmax, hist_tmax):
        if forecast_tmax is None or hist_tmax is None or hist_tmax == 0:
            return 1.0
        return max(0.6, min(1.4, forecast_tmax / hist_tmax))

    def _rain_factor(precip, store_id=None):
        cfg = _calibrated_rain.get(store_id) or {}
        heavy = cfg.get('heavy', 0.85)
        light = cfg.get('light', 0.93)
        if precip is None:
            return 1.0
        if precip > 3.0:
            return heavy
        if precip > 0.5:
            return light
        return 1.0

    def _est(avg, tmax_r, rain_f, perf_f):
        if avg is None:
            return None
        pf = perf_f if perf_f is not None else 1.0
        return round(avg * tmax_r * rain_f * pf, 2)

    # ── Build forecast rows ─────────────────────────────────────────────────────
    forecast = []
    cur_date = start
    while cur_date <= end:
        iso_dow = cur_date.isoweekday()  # 1=Mon … 7=Sun
        h = hist_by_dow.get(iso_dow, {})
        wx_day = wx_future.get(cur_date, {})
        wx_mat_d = wx_day.get(1, {})
        wx_bol_d = wx_day.get(2, {})

        tmax_mat = wx_mat_d.get('tmax')
        tmax_bol = wx_bol_d.get('tmax')
        precip_mat = wx_mat_d.get('precip')
        precip_bol = wx_bol_d.get('precip')

        if precip_mat is not None and precip_bol is not None:
            precip = round((precip_mat + precip_bol) / 2.0, 1)
        else:
            precip = precip_mat if precip_mat is not None else precip_bol

        ratio_mat = _tmax_ratio(tmax_mat, h.get('avg_tmax_mat'))
        ratio_bol = _tmax_ratio(tmax_bol, h.get('avg_tmax_bol'))
        rain_mat = _rain_factor(precip_mat, store_id=1)
        rain_bol = _rain_factor(precip_bol, store_id=2)

        est_mat   = _est(h.get('avg_mat'), ratio_mat, rain_mat, perf_factor_mat)
        est_bol   = _est(h.get('avg_bol'), ratio_bol, rain_bol, perf_factor_bol)
        est_total = round((est_mat or 0) + (est_bol or 0), 2) if (est_mat is not None or est_bol is not None) else None

        actual = actual_by_date.get(cur_date, {})

        forecast.append({
            'date':       cur_date.isoformat(),
            'dow':        iso_dow,
            'dow_name':   DOW_NAMES_PT[iso_dow],
            'tmax_mat':   tmax_mat,
            'tmax_bol':   tmax_bol,
            'precip':     precip,
            'est_total':  est_total,
            'est_mat':    est_mat,
            'est_bol':    est_bol,
            'real_total': actual.get('total'),
            'real_mat':   actual.get('mat'),
            'real_bol':   actual.get('bol'),
            'is_today':   cur_date == today,
        })

        cur_date += timedelta(days=1)

    # ── Compute forecast accuracy over last 30 days with actual sales data ───────
    # For each recent day with actual sales, reconstruct the base-model estimate
    # (DOW avg × tmax_ratio × rain_factor, perf_factor excluded to avoid circular
    # self-calibration), then measure MAE/MAPE against real sales.
    acc_errs: dict = {'total': ([], []), 'mat': ([], []), 'bol': ([], [])}

    def _record_err(key, est_v, real_v):
        if est_v is None or real_v is None:
            return
        ae = abs(real_v - est_v)
        acc_errs[key][0].append(ae)
        if real_v != 0:
            acc_errs[key][1].append(ae / abs(real_v) * 100)

    for r in recent_rows:
        d_past = r[0]
        act_mat = float(r[1]) if r[1] is not None else None
        act_bol = float(r[2]) if r[2] is not None else None
        if act_mat is None and act_bol is None:
            continue

        iso_dow = d_past.isoweekday()
        h = hist_by_dow.get(iso_dow, {})
        wx_day = wx_past.get(d_past, {})
        wx_mat_p = wx_day.get(1, {})
        wx_bol_p = wx_day.get(2, {})

        tmax_mat_p = wx_mat_p.get('tmax')
        tmax_bol_p = wx_bol_p.get('tmax')
        precip_mat_p = wx_mat_p.get('precip')
        precip_bol_p = wx_bol_p.get('precip')

        if precip_mat_p is not None and precip_bol_p is not None:
            precip_p = (precip_mat_p + precip_bol_p) / 2.0
        else:
            precip_p = precip_mat_p if precip_mat_p is not None else precip_bol_p

        r_mat = _tmax_ratio(tmax_mat_p, h.get('avg_tmax_mat'))
        r_bol = _tmax_ratio(tmax_bol_p, h.get('avg_tmax_bol'))
        rain_mat_p = _rain_factor(precip_mat_p, store_id=1)
        rain_bol_p = _rain_factor(precip_bol_p, store_id=2)

        est_mat_p = _est(h.get('avg_mat'), r_mat, rain_mat_p, None)
        est_bol_p = _est(h.get('avg_bol'), r_bol, rain_bol_p, None)

        _record_err('mat', est_mat_p, act_mat)
        _record_err('bol', est_bol_p, act_bol)
        if act_mat is not None and act_bol is not None and est_mat_p is not None and est_bol_p is not None:
            _record_err('total', est_mat_p + est_bol_p, act_mat + act_bol)

    def _to_metric(key):
        errors, pct_errors = acc_errs[key]
        if not errors:
            return None
        return {
            'n_days': len(errors),
            'mae':    round(sum(errors) / len(errors), 2),
            'mape':   round(sum(pct_errors) / len(pct_errors), 1) if pct_errors else None,
        }

    accuracy = {
        'total': _to_metric('total'),
        'mat':   _to_metric('mat'),
        'bol':   _to_metric('bol'),
    }

    return {
        'forecast':        forecast,
        'historical_dow':  hist_by_dow,
        'perf_factor_mat': perf_factor_mat,
        'perf_factor_bol': perf_factor_bol,
        'perf_days':       perf_days,
        'cutoff':          cutoff.isoformat() if cutoff else None,
        'accuracy':        accuracy,
    }


def get_backtesting_rolling() -> dict:
    """Backtest the forecast model against the most recent complete calendar month.

    Training DOW averages: 90 days ending the day before the test period.
    Test period: last complete calendar month with data.
    Formula (no perf_factor to avoid circularity):
        est = avg_dow_sales × tmax_ratio × rain_factor

    Returns:
        metrics      — {total, mat, bol} each with {mae, mape, n_days} or None
        weeks        — list of {week_label, iso_week,
                                est_total, real_total,
                                est_mat, real_mat,
                                est_bol, real_bol, n_days}
        n_train_days — int: distinct training days used
        train_start, train_end, test_start, test_end — ISO date strings
        train_label, test_label — human-readable period labels (pt-PT)
    """
    import calendar as _cal
    today = date.today()
    _MONTH_PT = ['', 'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

    with db_connection() as conn:
        cur = conn.cursor()

        # ── 0. Determine test/train windows from actual data availability ─────
        cur.execute("SELECT MAX(data) FROM vendas_detalhe")
        _max_r = cur.fetchone()
        _max_sales = _max_r[0] if _max_r and _max_r[0] else None

        if _max_sales is not None:
            _last_day_of_max = _cal.monthrange(_max_sales.year, _max_sales.month)[1]
            _end_of_max_month = date(_max_sales.year, _max_sales.month, _last_day_of_max)
            if today > _end_of_max_month:
                TEST_END = _end_of_max_month
            else:
                TEST_END = _max_sales.replace(day=1) - timedelta(days=1)
        else:
            TEST_END = today.replace(day=1) - timedelta(days=1)

        TEST_START  = TEST_END.replace(day=1)
        TRAIN_END   = TEST_START - timedelta(days=1)
        TRAIN_START = TRAIN_END - timedelta(days=89)

        if TRAIN_START.month == TRAIN_END.month:
            train_label = _MONTH_PT[TRAIN_START.month]
        else:
            train_label = f"{_MONTH_PT[TRAIN_START.month]}–{_MONTH_PT[TRAIN_END.month]}"
        test_label = f"{_MONTH_PT[TEST_START.month]} {TEST_END.year}"

        # ── 1. Training DOW averages, sales + weather ─────────────────────────
        cur.execute("""
            WITH daily_sales AS (
                SELECT
                    vd.data,
                    SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Matosinhos') AS sales_mat,
                    SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Bolhão')     AS sales_bol,
                    SUM(vd.valor_euros)                                         AS sales_total
                FROM vendas_detalhe vd
                LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE vd.data BETWEEN %s AND %s
                  AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
                GROUP BY vd.data
            ),
            wx_mat AS (
                SELECT data, AVG(temperatura_max) AS tmax
                FROM weather_data
                WHERE store_id = 1 AND data BETWEEN %s AND %s
                GROUP BY data
            ),
            wx_bol AS (
                SELECT data, AVG(temperatura_max) AS tmax
                FROM weather_data
                WHERE store_id = 2 AND data BETWEEN %s AND %s
                GROUP BY data
            )
            SELECT
                EXTRACT(ISODOW FROM ds.data)::int AS dow,
                COUNT(*)                           AS n_days,
                ROUND(AVG(ds.sales_total)::numeric, 2) AS avg_total,
                ROUND(AVG(ds.sales_mat)::numeric,   2) AS avg_mat,
                ROUND(AVG(ds.sales_bol)::numeric,   2) AS avg_bol,
                ROUND(AVG(wx_mat.tmax)::numeric,    1) AS avg_tmax_mat,
                ROUND(AVG(wx_bol.tmax)::numeric,    1) AS avg_tmax_bol
            FROM daily_sales ds
            LEFT JOIN wx_mat ON wx_mat.data = ds.data
            LEFT JOIN wx_bol ON wx_bol.data = ds.data
            GROUP BY dow
            ORDER BY dow
        """, (TRAIN_START, TRAIN_END,
              TRAIN_START, TRAIN_END,
              TRAIN_START, TRAIN_END))
        hist_rows = cur.fetchall()

        # ── 2. Actual sales for test period ──────────────────────────────────
        cur.execute("""
            SELECT vd.data,
                   SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Matosinhos') AS mat,
                   SUM(vd.valor_euros) FILTER (WHERE vd.loja = 'Bolhão')     AS bol,
                   SUM(vd.valor_euros)                                         AS total
            FROM vendas_detalhe vd
            LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
            WHERE vd.data BETWEEN %s AND %s
              AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
            GROUP BY vd.data
            ORDER BY vd.data
        """, (TEST_START, TEST_END))
        actual_rows = cur.fetchall()

        # ── 3. Weather for test period ────────────────────────────────────────
        cur.execute("""
            SELECT data, store_id,
                   ROUND(AVG(temperatura_max)::numeric, 1) AS tmax,
                   ROUND(AVG(precipitacao_mm)::numeric,  1) AS precip
            FROM weather_data
            WHERE data BETWEEN %s AND %s
            GROUP BY data, store_id
            ORDER BY data, store_id
        """, (TEST_START, TEST_END))
        wx_rows = cur.fetchall()

    # ── Build training DOW lookup ───────────────────────────────────────────
    hist_by_dow: dict = {}
    n_train_days = 0
    for r in hist_rows:
        dow = int(r[0])
        n = int(r[1])
        n_train_days += n
        hist_by_dow[dow] = {
            'n_days':       n,
            'avg_total':    float(r[2]) if r[2] is not None else None,
            'avg_mat':      float(r[3]) if r[3] is not None else None,
            'avg_bol':      float(r[4]) if r[4] is not None else None,
            'avg_tmax_mat': float(r[5]) if r[5] is not None else None,
            'avg_tmax_bol': float(r[6]) if r[6] is not None else None,
        }

    # ── Build weather lookup ─────────────────────────────────────────────────
    wx_by_date: dict = {}
    for r in wx_rows:
        d, sid = r[0], int(r[1])
        wx_by_date.setdefault(d, {})[sid] = {
            'tmax':   float(r[2]) if r[2] is not None else None,
            'precip': float(r[3]) if r[3] is not None else None,
        }

    # ── Build actual sales lookup ────────────────────────────────────────────
    actual_by_date: dict = {}
    for r in actual_rows:
        actual_by_date[r[0]] = {
            'mat':   float(r[1]) if r[1] is not None else None,
            'bol':   float(r[2]) if r[2] is not None else None,
            'total': float(r[3]) if r[3] is not None else None,
        }

    def _tmax_ratio(forecast_tmax, hist_tmax):
        if forecast_tmax is None or hist_tmax is None or hist_tmax == 0:
            return 1.0
        return max(0.6, min(1.4, forecast_tmax / hist_tmax))

    def _rain_factor(precip):
        if precip is None:
            return 1.0
        if precip > 3.0:
            return 0.85
        if precip > 0.5:
            return 0.93
        return 1.0

    def _est(avg, tmax_r, rain_f):
        if avg is None:
            return None
        return round(avg * tmax_r * rain_f, 2)

    # ── Compute per-day errors and weekly aggregates ──────────────────────────
    acc_errs: dict = {'total': ([], []), 'mat': ([], []), 'bol': ([], [])}
    # weekly: {iso_week: {est_mat, est_bol, real_mat, real_bol, n_days}}
    weekly: dict = {}

    def _record_err(key, est_v, real_v):
        if est_v is None or real_v is None:
            return
        ae = abs(real_v - est_v)
        acc_errs[key][0].append(ae)
        if real_v != 0:
            acc_errs[key][1].append(ae / abs(real_v) * 100)

    cur_date = TEST_START
    while cur_date <= TEST_END:
        iso_dow = cur_date.isoweekday()
        h = hist_by_dow.get(iso_dow, {})
        wx_day = wx_by_date.get(cur_date, {})
        wx_mat = wx_day.get(1, {})
        wx_bol = wx_day.get(2, {})

        tmax_mat  = wx_mat.get('tmax')
        tmax_bol  = wx_bol.get('tmax')
        prec_mat  = wx_mat.get('precip')
        prec_bol  = wx_bol.get('precip')

        if prec_mat is not None and prec_bol is not None:
            precip = (prec_mat + prec_bol) / 2.0
        else:
            precip = prec_mat if prec_mat is not None else prec_bol

        ratio_mat = _tmax_ratio(tmax_mat, h.get('avg_tmax_mat'))
        ratio_bol = _tmax_ratio(tmax_bol, h.get('avg_tmax_bol'))
        rain      = _rain_factor(precip)

        est_mat   = _est(h.get('avg_mat'), ratio_mat, rain)
        est_bol   = _est(h.get('avg_bol'), ratio_bol, rain)
        est_total = round((est_mat or 0) + (est_bol or 0), 2) if (est_mat is not None or est_bol is not None) else None

        actual  = actual_by_date.get(cur_date, {})
        real_mat   = actual.get('mat')
        real_bol   = actual.get('bol')
        real_total = actual.get('total')

        _record_err('mat',   est_mat,   real_mat)
        _record_err('bol',   est_bol,   real_bol)
        _record_err('total', est_total, real_total)

        # ISO week key
        iso_week = cur_date.isocalendar()[1]
        year_w   = cur_date.isocalendar()[0]
        wkey     = (year_w, iso_week)
        if wkey not in weekly:
            weekly[wkey] = {
                'iso_week': iso_week, 'year': year_w,
                'est_mat': 0.0, 'est_bol': 0.0,
                'real_mat': None, 'real_bol': None,
                'has_est': False, 'n_days': 0,
            }
        w = weekly[wkey]
        if est_mat is not None:
            w['est_mat'] += est_mat
            w['has_est'] = True
        if est_bol is not None:
            w['est_bol'] += est_bol
            w['has_est'] = True
        if real_mat is not None:
            w['real_mat'] = (w['real_mat'] or 0) + real_mat
        if real_bol is not None:
            w['real_bol'] = (w['real_bol'] or 0) + real_bol
        if real_mat is not None or real_bol is not None:
            w['n_days'] += 1

        cur_date += timedelta(days=1)

    def _to_metric(key):
        errors, pct_errors = acc_errs[key]
        if not errors:
            return None
        return {
            'n_days': len(errors),
            'mae':    round(sum(errors) / len(errors), 2),
            'mape':   round(sum(pct_errors) / len(pct_errors), 1) if pct_errors else None,
        }

    # ── Serialise weekly rows ────────────────────────────────────────────────
    weeks_out = []
    for wkey in sorted(weekly.keys()):
        w = weekly[wkey]
        est_t  = round(w['est_mat'] + w['est_bol'], 2) if w['has_est'] else None
        real_m = round(w['real_mat'], 2)  if w['real_mat'] is not None else None
        real_b = round(w['real_bol'], 2)  if w['real_bol'] is not None else None
        real_t = round((real_m or 0) + (real_b or 0), 2) if (real_m is not None or real_b is not None) else None
        weeks_out.append({
            'iso_week':   w['iso_week'],
            'week_label': f"W{w['iso_week']}",
            'est_total':  est_t,
            'real_total': real_t,
            'est_mat':    round(w['est_mat'], 2) if w['has_est'] else None,
            'real_mat':   real_m,
            'est_bol':    round(w['est_bol'], 2) if w['has_est'] else None,
            'real_bol':   real_b,
            'n_days':     w['n_days'],
        })

    return {
        'metrics':      {'total': _to_metric('total'), 'mat': _to_metric('mat'), 'bol': _to_metric('bol')},
        'weeks':        weeks_out,
        'n_train_days': n_train_days,
        'test_start':   TEST_START.isoformat(),
        'test_end':     TEST_END.isoformat(),
        'train_start':  TRAIN_START.isoformat(),
        'train_end':    TRAIN_END.isoformat(),
        'train_label':  train_label,
        'test_label':   test_label,
    }


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
        # 2025 source: vendas_detalhe if it has data, otherwise sales_historico
        # (the NOT EXISTS guard prevents double-counting if both ever coexist).
        cur.execute("""
            SELECT vd.loja,
                   EXTRACT(YEAR FROM vd.data)::int  AS ano,
                   EXTRACT(MONTH FROM vd.data)::int AS mes,
                   SUM(vd.valor_euros)              AS total
            FROM (
                SELECT loja, data, valor_euros, produto
                FROM vendas_detalhe
                WHERE EXTRACT(YEAR FROM data) IN (2025, 2026)
                UNION ALL
                SELECT sh.loja, sh.data, sh.valor_euros, sh.produto
                FROM sales_historico sh
                WHERE EXTRACT(YEAR FROM sh.data) = 2025
                  AND NOT EXISTS (
                      SELECT 1 FROM vendas_detalhe vd_chk
                      WHERE EXTRACT(YEAR FROM vd_chk.data) = 2025 LIMIT 1
                  )
            ) vd
            LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
            WHERE (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
              AND (pvc.b2b IS NULL OR pvc.b2b = FALSE)
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
                FROM (
                    SELECT loja, data, valor_euros, produto
                    FROM vendas_detalhe
                    WHERE EXTRACT(YEAR FROM data) IN (2025, 2026)
                    UNION ALL
                    SELECT sh.loja, sh.data, sh.valor_euros, sh.produto
                    FROM sales_historico sh
                    WHERE EXTRACT(YEAR FROM sh.data) = 2025
                      AND NOT EXISTS (
                          SELECT 1 FROM vendas_detalhe vd_chk
                          WHERE EXTRACT(YEAR FROM vd_chk.data) = 2025 LIMIT 1
                      )
                ) vd
                LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
                  AND (pvc.b2b IS NULL OR pvc.b2b = FALSE)
                GROUP BY vd.loja
                ORDER BY vd.loja
            """, (cutoff, cutoff))
            ytd_rows = cur.fetchall()

            # ── YTD sums: per produto × loja ─────────────────────────────────
            # Only vendas_detalhe here — sales_historico contains only daily
            # totals ("Total Diário"), not per-product breakdowns, so it
            # cannot provide meaningful 2025 comparison data at product level.
            cur.execute("""
                SELECT vd.produto, vd.loja,
                       SUM(CASE WHEN EXTRACT(YEAR FROM vd.data) = 2026 THEN vd.valor_euros ELSE 0 END) AS ytd_2026,
                       SUM(CASE WHEN EXTRACT(YEAR FROM vd.data) = 2025
                                 AND EXTRACT(MONTH FROM vd.data) * 100 + EXTRACT(DAY FROM vd.data)
                                     <= EXTRACT(MONTH FROM %s::date) * 100 + EXTRACT(DAY FROM %s::date)
                                THEN vd.valor_euros ELSE 0 END) AS ytd_2025
                FROM vendas_detalhe vd
                LEFT JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE EXTRACT(YEAR FROM vd.data) IN (2025, 2026)
                  AND (pvc.conta_vendas_diarias IS NULL OR pvc.conta_vendas_diarias = TRUE)
                  AND (pvc.b2b IS NULL OR pvc.b2b = FALSE)
                GROUP BY vd.produto, vd.loja
            """, (cutoff, cutoff))
            produto_rows = cur.fetchall()
        else:
            ytd_rows = []
            produto_rows = []

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

    # ── Build produtos structure ──────────────────────────────────────────────
    # produtos[filter_key] = [{produto, y2026, y2025, diff_eur, diff_pct}, ...]
    # Accumulate per-produto totals across lojas for the 'total' key,
    # and keep per-loja lists for individual loja filter keys.
    # Products that were renamed keep separate rows across years in the raw
    # data (old name in 2025, new name in 2026). Map old -> current name so
    # the Top10/Top5/Bottom5 analysis treats them as a single product.
    _PRODUTO_RENAME = {
        'Copo Mini': 'Copo Piccolo',
        'Copo Pequeno': 'Copo Classico',
        'Cone Pequeno': 'Cone Classico',
    }

    _prod_total: dict = {}   # produto -> {y2026, y2025}
    _prod_loja: dict = {}    # loja -> {produto -> {y2026, y2025}}

    for produto, loja, y26, y25 in produto_rows:
        produto = _PRODUTO_RENAME.get(produto, produto)
        y26, y25 = float(y26 or 0), float(y25 or 0)
        # total aggregation
        if produto not in _prod_total:
            _prod_total[produto] = {'y2026': 0.0, 'y2025': 0.0}
        _prod_total[produto]['y2026'] += y26
        _prod_total[produto]['y2025'] += y25
        # per-loja
        _prod_loja.setdefault(loja, {}).setdefault(produto, {'y2026': 0.0, 'y2025': 0.0})
        _prod_loja[loja][produto]['y2026'] += y26
        _prod_loja[loja][produto]['y2025'] += y25

    def _prod_list(mapping: dict) -> list:
        result = []
        for produto, vals in mapping.items():
            y26, y25 = round(vals['y2026'], 2), round(vals['y2025'], 2)
            if y26 == 0 and y25 == 0:
                continue
            diff_eur = round(y26 - y25, 2)
            diff_pct = round((y26 - y25) / y25 * 100, 1) if y25 else None
            result.append({
                'produto': produto,
                'y2026': y26,
                'y2025': y25,
                'diff_eur': diff_eur,
                'diff_pct': diff_pct,
            })
        result.sort(key=lambda x: x['y2026'], reverse=True)
        return result

    produtos: dict = {'total': _prod_list(_prod_total)}
    for loja in lojas:
        produtos[loja] = _prod_list(_prod_loja.get(loja, {}))

    return {
        'lojas': lojas,
        'cutoff': cutoff.isoformat() if cutoff else None,
        'monthly': monthly,
        'ytd': ytd,
        'produtos': produtos,
    }


def get_produtos_b2b_totals() -> dict:
    """Return monthly/YTD totals for products flagged as b2b in produtos_vendas_config.

    Only vendas_detalhe is used (not sales_historico), since sales_historico
    only has daily store totals ("Total Diário"), not per-product rows, so a
    b2b product cannot be identified within it. This mirrors the existing
    per-produto YTD query in get_dashboard_vendas().

    The classification is retroactive: it applies across the full available
    history (2025 and 2026), not just sales going forward.
    """
    with db_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT EXTRACT(YEAR FROM vd.data)::int  AS ano,
                   EXTRACT(MONTH FROM vd.data)::int AS mes,
                   SUM(vd.valor_euros)              AS total
            FROM vendas_detalhe vd
            JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
            WHERE EXTRACT(YEAR FROM vd.data) IN (2025, 2026)
              AND pvc.b2b = TRUE
            GROUP BY ano, mes
        """)
        monthly_rows = cur.fetchall()

        cur.execute("""
            SELECT MAX(data) FROM vendas_detalhe
            WHERE EXTRACT(YEAR FROM data) = 2026
        """)
        cutoff = cur.fetchone()[0]

        ytd = {'y2026': 0.0, 'y2025': 0.0}
        if cutoff:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN EXTRACT(YEAR FROM vd.data) = 2026 THEN vd.valor_euros ELSE 0 END) AS ytd_2026,
                    SUM(CASE WHEN EXTRACT(YEAR FROM vd.data) = 2025
                              AND EXTRACT(MONTH FROM vd.data) * 100 + EXTRACT(DAY FROM vd.data)
                                  <= EXTRACT(MONTH FROM %s::date) * 100 + EXTRACT(DAY FROM %s::date)
                             THEN vd.valor_euros ELSE 0 END) AS ytd_2025
                FROM vendas_detalhe vd
                JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
                WHERE EXTRACT(YEAR FROM vd.data) IN (2025, 2026)
                  AND pvc.b2b = TRUE
            """, (cutoff, cutoff))
            row = cur.fetchone()
            if row:
                ytd = {'y2026': float(row[0] or 0), 'y2025': float(row[1] or 0)}

    monthly: dict = {}
    for ano, mes, total in monthly_rows:
        monthly.setdefault(ano, {})[mes] = round(float(total or 0), 2)

    return {'monthly': monthly, 'ytd': ytd}


def get_dashboard_b2b(ano: int = None) -> dict:
    """Return B2B/Events monthly revenue for the sales dashboard.

    Only clients with incluir_mapas=TRUE are included.

    Returns:
        {
            'monthly_b2b':    {year: {month: total}},          # aggregated B2B
            'monthly_eventos':{year: {month: total}},          # aggregated Eventos
            'monthly_por_cliente': [                           # per-client monthly series
                {nome, tipo, series: {year: {month: total}}}
            ],
            'ytd_b2b':        {y2026, y2025},
            'ytd_eventos':    {y2026, y2025},
            'top_clientes':   list of {nome, tipo, y2026, y2025},
        }
    """
    from datetime import date as _date
    today = _date.today()
    anos = [today.year, today.year - 1]
    if ano and ano not in anos:
        anos.append(ano)

    with db_connection() as conn:
        cur = conn.cursor()
        try:
            # Per-client × year × month aggregation (used for stacked charts and totals)
            cur.execute("""
                SELECT
                    c.nome,
                    c.tipo,
                    EXTRACT(YEAR  FROM fc.data)::int AS ano,
                    EXTRACT(MONTH FROM fc.data)::int AS mes,
                    SUM(fc.total) AS total
                FROM faturas_clientes fc
                JOIN clientes_b2b c ON c.id = fc.cliente_id
                WHERE fc.anulado = FALSE
                  AND c.incluir_mapas = TRUE
                  AND EXTRACT(YEAR FROM fc.data) IN %s
                GROUP BY c.nome, c.tipo, ano, mes
                ORDER BY c.tipo, c.nome, ano, mes
            """, (tuple(anos),))
            monthly_rows = cur.fetchall()
        except Exception as exc:
            logger.warning("get_dashboard_b2b monthly failed: %s", exc)
            monthly_rows = []

        try:
            cutoff_mmdd = today.month * 100 + today.day
            cur.execute("""
                SELECT
                    c.tipo,
                    c.id, c.nome,
                    SUM(fc.total) FILTER (
                        WHERE EXTRACT(YEAR FROM fc.data) = %s
                    ) AS y2026,
                    SUM(fc.total) FILTER (
                        WHERE EXTRACT(YEAR FROM fc.data) = %s
                          AND EXTRACT(MONTH FROM fc.data)::int * 100
                            + EXTRACT(DAY FROM fc.data)::int <= %s
                    ) AS y2025
                FROM faturas_clientes fc
                JOIN clientes_b2b c ON c.id = fc.cliente_id
                WHERE fc.anulado = FALSE
                  AND c.incluir_mapas = TRUE
                  AND EXTRACT(YEAR FROM fc.data) IN (%s, %s)
                GROUP BY c.tipo, c.id, c.nome
                ORDER BY c.tipo, y2026 DESC NULLS LAST
            """, (today.year, today.year - 1, cutoff_mmdd, today.year, today.year - 1))
            ytd_rows = cur.fetchall()
        except Exception as exc:
            logger.warning("get_dashboard_b2b ytd failed: %s", exc)
            ytd_rows = []

    # Build per-client monthly series and aggregated type totals
    monthly_b2b: dict = {}
    monthly_eventos: dict = {}
    # {nome: {tipo, series: {ano: {mes: total}}}}
    _client_monthly: dict = {}
    for nome, tipo, ano_val, mes_val, total in monthly_rows:
        total = float(total or 0)
        # Aggregated by tipo
        agg = monthly_b2b if tipo == 'b2b' else monthly_eventos
        agg.setdefault(ano_val, {})
        agg[ano_val][mes_val] = agg[ano_val].get(mes_val, 0) + total
        # Per-client
        if nome not in _client_monthly:
            _client_monthly[nome] = {'nome': nome, 'tipo': tipo, 'series': {}}
        _client_monthly[nome]['series'].setdefault(ano_val, {})[mes_val] = \
            _client_monthly[nome]['series'].get(ano_val, {}).get(mes_val, 0) + total

    # Sort per-client series by descending current-year total for chart legend order
    def _client_ytd(c):
        return sum(c['series'].get(today.year, {}).values())
    monthly_por_cliente = sorted(_client_monthly.values(), key=_client_ytd, reverse=True)

    ytd_b2b = {'y2026': 0.0, 'y2025': 0.0}
    ytd_eventos = {'y2026': 0.0, 'y2025': 0.0}
    top_clientes = []
    for tipo, cid, nome, y26, y25 in ytd_rows:
        y26 = float(y26 or 0)
        y25 = float(y25 or 0)
        if tipo == 'b2b':
            ytd_b2b['y2026'] += y26
            ytd_b2b['y2025'] += y25
        else:
            ytd_eventos['y2026'] += y26
            ytd_eventos['y2025'] += y25
        top_clientes.append({'nome': nome, 'tipo': tipo, 'y2026': y26, 'y2025': y25})

    top_clientes.sort(key=lambda x: x['y2026'], reverse=True)
    top_clientes = top_clientes[:10]

    # ── Merge in vendas diárias products flagged as B2B ─────────────────────
    # These come from the "Filtro Vendas Diárias" tile (Gestor), and are
    # retroactively applied to all available history (2025 and 2026), not
    # only sales going forward.
    try:
        extra = get_produtos_b2b_totals()
        for ano_val, meses in extra['monthly'].items():
            monthly_b2b.setdefault(ano_val, {})
            for mes_val, total in meses.items():
                monthly_b2b[ano_val][mes_val] = monthly_b2b[ano_val].get(mes_val, 0) + total
        ytd_b2b['y2026'] += extra['ytd']['y2026']
        ytd_b2b['y2025'] += extra['ytd']['y2025']
    except Exception as exc:
        logger.warning("get_dashboard_b2b: merging produtos b2b totals failed: %s", exc)

    return {
        'monthly_b2b': monthly_b2b,
        'monthly_eventos': monthly_eventos,
        'monthly_por_cliente': monthly_por_cliente,
        'ytd_b2b': ytd_b2b,
        'ytd_eventos': ytd_eventos,
        'top_clientes': top_clientes,
    }
