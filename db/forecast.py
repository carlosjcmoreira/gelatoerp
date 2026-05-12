"""M2b: Motor de Previsão de Vendas — Sales Forecast Engine.

Implements:
- sales_forecasts table schema + migrations
- 5-step forecast algorithm: historical base, YoY factor, meteo adjustment, confidence band, manual override
- Weekly recalibration from real POS sales
- MAPE accuracy dashboard data

NOTE: Several functions in this module read from the `vendas` table (daily cash-close totals).
Per the business rule established in Task #203, `vendas_detalhe` (Gestor uploads) is the
authoritative source for financial analysis. These functions should be updated to query
`vendas_detalhe` (aggregated by data+loja) as primary, with `vendas` as fallback.
Affected: build_forecast_for_loja, _build_forecast_batch, get_ly_mtd_total,
          get_recent_daily_sales, get_forecast_accuracy_data.
"""

from datetime import date, timedelta
from typing import Optional
from db.connection import db_connection
import math
import statistics


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def run_migrations_forecast():
    """Idempotent migrations for M2b: sales_forecasts and meteo_config tables.
    Uses an advisory lock to serialise concurrent worker executions."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            # Try to acquire advisory lock (non-blocking). If another worker has it, skip.
            cursor.execute("SELECT pg_try_advisory_lock(202602)")
            acquired = cursor.fetchone()[0]
            if not acquired:
                return

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sales_forecasts (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    loja VARCHAR(100) NOT NULL,
                    data DATE NOT NULL,
                    previsao_eur NUMERIC(10,2),
                    banda_min NUMERIC(10,2),
                    banda_max NUMERIC(10,2),
                    score_meteo INTEGER,
                    condicao_meteo VARCHAR(100),
                    factor_yoy NUMERIC(6,4),
                    multiplicador_meteo NUMERIC(6,4),
                    base_historica NUMERIC(10,2),
                    peso_semana NUMERIC(6,4),
                    override_manual NUMERIC(10,2),
                    override_motivo TEXT,
                    venda_real NUMERIC(10,2),
                    erro_real_pct NUMERIC(8,4),
                    gerado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(loja, data)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS forecast_meteo_config (
                    id SERIAL PRIMARY KEY,
                    store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    loja VARCHAR(100) NOT NULL,
                    score_min INTEGER NOT NULL,
                    score_max INTEGER NOT NULL,
                    multiplicador NUMERIC(6,4) NOT NULL DEFAULT 1.0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(loja, score_min, score_max)
                )
            ''')

            # Seed default meteo config for known stores if not present
            lojas = _get_lojas(cursor)
            defaults = [
                (0, 29, 0.70),
                (30, 59, 0.85),
                (60, 84, 1.00),
                (85, 100, 1.15),
            ]
            for loja in lojas:
                for score_min, score_max, mult in defaults:
                    cursor.execute("""
                        INSERT INTO forecast_meteo_config (loja, score_min, score_max, multiplicador)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (loja, score_min, score_max) DO NOTHING
                    """, (loja, score_min, score_max, mult))

            conn.commit()
        except Exception as e:
            _log.warning("run_migrations_forecast: %s (continuing)", e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202602)")
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass


def _get_lojas(cursor):
    cursor.execute("SELECT name FROM stores WHERE is_active = TRUE ORDER BY name")
    return [r[0] for r in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Forecast algorithm
# ---------------------------------------------------------------------------

HIGH_SEASON_MONTHS = {4, 5, 6, 7, 8, 9, 10}  # Abril–Outubro


def get_epoch_label(d: date) -> str:
    if d.month in HIGH_SEASON_MONTHS:
        return 'Alta'
    return 'Baixa'


def _weighted_base(sales_by_weekday: dict, target_weekday: int) -> Optional[float]:
    """Fallback: weighted avg of last 8 same-weekday sales, weights 1→8 (most recent = 8)."""
    samples = sales_by_weekday.get(target_weekday, [])
    if not samples:
        return None
    recent = samples[-8:]
    total_weight = 0
    weighted_sum = 0.0
    for i, val in enumerate(recent):
        w = i + 1
        weighted_sum += val * w
        total_weight += w
    return weighted_sum / total_weight if total_weight else None


def _ly_weekday_anchor(ly_rows: list, target_date: date) -> Optional[float]:
    """Signal A — last year's same-weekday anchor within ±21 days of the equivalent calendar date.

    For e.g. next Saturday March 28 2026, looks at all Saturdays between
    March 7 2025 and April 18 2025 in ly_rows and returns their weighted mean
    (most recent Saturday = highest weight). Gives a weekday-specific baseline
    rooted in last year's actual calendar behaviour.
    """
    try:
        ly_center = date(target_date.year - 1, target_date.month, target_date.day)
    except ValueError:
        ly_center = date(target_date.year - 1, target_date.month, 28)
    target_wd = target_date.weekday()
    window_start = ly_center - timedelta(days=21)
    window_end = ly_center + timedelta(days=21)
    candidates = [
        (d, v) for d, v in ly_rows
        if d.weekday() == target_wd and window_start <= d <= window_end
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    weighted_sum = 0.0
    total_weight = 0
    for i, (_, v) in enumerate(candidates):
        w = i + 1
        weighted_sum += v * w
        total_weight += w
    return weighted_sum / total_weight if total_weight else None


def _recent_weekday_signal(sales_by_weekday: dict, wd: int) -> Optional[float]:
    """Signal B — last 3 same-weekday values, weights [1, 2, 4] (most recent ≈ 57%).

    Only uses data from the last 3 occurrences (≈21 days), so the most
    recent weekend dominates heavily over the one 2 weeks ago.
    """
    samples = sales_by_weekday.get(wd, [])
    if not samples:
        return None
    recent = samples[-3:]
    full_weights = [1, 2, 4]
    weights = full_weights[-len(recent):]
    total_weight = sum(weights)
    weighted_sum = sum(v * w for v, w in zip(recent, weights))
    return weighted_sum / total_weight if total_weight else None


def _yoy_rate(recent_30: list, ly_30: list) -> float:
    """YoY growth rate: mean of last 30 days / mean of same 30-day window last year.

    Uses a 30-day window (vs the old 60 days) so the rate reflects the most
    recent month's growth trajectory rather than a 2-month average.
    Clamped 0.50–2.50 to exclude extreme outliers.
    """
    if not recent_30 or not ly_30:
        return 1.0
    mean_now = statistics.mean(recent_30)
    mean_ly = statistics.mean(ly_30)
    if mean_ly <= 0:
        return 1.0
    return max(0.50, min(2.50, mean_now / mean_ly))


def _meteo_multiplier(score: Optional[int], config: list) -> tuple[float, str]:
    """Return (multiplier, label) for the given weather score."""
    if score is None:
        return 1.0, 'Sem dados'
    for row in config:
        if row['score_min'] <= score <= row['score_max']:
            return float(row['multiplicador']), _score_label(score)
    return 1.0, _score_label(score)


def _score_label(score: int) -> str:
    if score < 30:
        return 'Mau tempo'
    if score < 60:
        return 'Tempo incerto'
    if score < 85:
        return 'Bom tempo'
    return 'Excelente'


def _momentum_factor(recent_15: list, recent_30: list) -> float:
    """Compare last 15 days mean vs last 30 days mean to capture very short-term demand.

    Using 15 vs 30 days (was 21 vs 60) makes the signal more reactive to the
    current week's performance — important during season transitions.
    Clamped 0.75–1.35 (tighter than before, enough for a sharp nudge),
    blended 50/50 with neutral (1.0) so it adjusts without overriding.
    """
    if len(recent_15) < 3 or not recent_30:
        return 1.0
    mean_15 = statistics.mean(recent_15)
    mean_30 = statistics.mean(recent_30)
    if mean_30 <= 0:
        return 1.0
    raw = mean_15 / mean_30
    clamped = max(0.75, min(1.35, raw))
    return (clamped + 1.0) / 2.0


def _confidence_band(base: float, std_dev: float, meteo_scores: list) -> tuple[float, float]:
    """Compute min/max band. Wider if meteo sources diverge."""
    if std_dev <= 0:
        std_dev = base * 0.05
    # Penalise divergence between meteo sources (passed as list of scores or None)
    non_null = [s for s in meteo_scores if s is not None]
    if len(non_null) >= 2:
        divergence_penalty = (max(non_null) - min(non_null)) / 100.0 * base * 0.10
    else:
        divergence_penalty = 0
    margin = std_dev + divergence_penalty
    return max(0, base - margin), base + margin


# ---------------------------------------------------------------------------
# Public API: generate forecasts
# ---------------------------------------------------------------------------

def generate_forecasts(loja: str, horizon_days: int = 7, force: bool = False) -> list[dict]:
    """Generate sales forecasts for `loja` for the next `horizon_days` days.
    Returns list of forecast dicts; also persists them to sales_forecasts.

    If valid forecasts already exist in the DB for all horizon days and were
    generated today, skips the heavy re-computation and returns the stored rows.
    """
    today = date.today()
    results = []

    with db_connection() as conn:
        conn.rollback()  # ensure clean state
        cursor = conn.cursor()

        # --- Fast path: check if up-to-date forecasts already exist ---
        from_date = today + timedelta(days=1)
        to_date = today + timedelta(days=horizon_days)
        cursor.execute("""
            SELECT data, previsao_eur, banda_min, banda_max, score_meteo, condicao_meteo,
                   factor_yoy, multiplicador_meteo, base_historica, override_manual,
                   override_motivo, gerado_em
            FROM sales_forecasts
            WHERE loja = %s AND data >= %s AND data <= %s
              AND gerado_em::date = %s
            ORDER BY data
        """, (loja, from_date, to_date, today))
        cached_rows = cursor.fetchall()
        if not force and len(cached_rows) == horizon_days:
            # Fast path: load only what the new UI needs
            # (a) 30/15-day windows for momentum
            # (b) MTD YoY for the header badge
            cursor.execute("""
                SELECT data, valor_euros FROM vendas
                WHERE loja = %s AND data >= %s AND data < %s
                ORDER BY data
            """, (loja, today - timedelta(days=30), today))
            r30_raw = cursor.fetchall()
            recent_30_vals = [float(r[1]) for r in r30_raw]
            cutoff_15 = today - timedelta(days=15)
            recent_15_vals = [float(r[1]) for r in r30_raw if r[0] >= cutoff_15]
            cache_momentum = _momentum_factor(recent_15_vals, recent_30_vals)
            cache_mtd_yoy = _compute_mtd_yoy(cursor, loja, today)

            # Count same-group days in last 21 days (for signal_n display on cache hits)
            # Use merged vendas + historico so the count matches the live compute path.
            cutoff_21_c = today - timedelta(days=21)
            recent_dates_cache: set = {rd for rd, _ in r30_raw if rd >= cutoff_21_c}
            try:
                cursor.execute("SAVEPOINT sp_hist_cache")
                cursor.execute("""
                    SELECT DISTINCT data FROM sales_historico
                    WHERE loja = %s AND data >= %s AND data < %s
                """, (loja, cutoff_21_c, today))
                for r in cursor.fetchall():
                    recent_dates_cache.add(r[0])
                cursor.execute("RELEASE SAVEPOINT sp_hist_cache")
            except Exception:
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT sp_hist_cache")
                except Exception:
                    pass
            group_count_cache: dict[str, int] = {'semana': 0, 'sexta': 0, 'fds': 0}
            for rd in recent_dates_cache:
                group_count_cache[_day_group(rd.weekday())] += 1

            for row in cached_rows:
                d = row[0]
                override_val = float(row[9]) if row[9] is not None else None
                previsao = float(row[1]) if row[1] is not None else 0.0
                results.append({
                    'loja': loja,
                    'data': d,
                    'previsao_eur': previsao,
                    'banda_min': float(row[2]) if row[2] is not None else 0.0,
                    'banda_max': float(row[3]) if row[3] is not None else 0.0,
                    'score_meteo': row[4],
                    'condicao_meteo': row[5],
                    'factor_yoy': round(cache_mtd_yoy, 4) if cache_mtd_yoy else None,
                    'multiplicador_meteo': float(row[7]) if row[7] is not None else 1.0,
                    'base_historica': float(row[8]) if row[8] is not None else 0.0,
                    'override_manual': override_val,
                    'override_motivo': row[10],
                    'homolog_eur': None,
                    'epoch': get_epoch_label(d),
                    'day_type': _day_type(d),
                    'day_group': _day_group(d.weekday()),
                    'peso_semana': None,
                    'factor_momentum': round(cache_momentum, 4),
                    'ly_anchor_eur': None,
                    'signal_n': group_count_cache.get(_day_group(d.weekday())) or None,
                    'effective_value': float(override_val or previsao or 0),
                })
            return results

        # --- 1. Load historical sales (last 21 days primary; 16 weeks for fallback) ---
        # Primary source: live vendas table
        cursor.execute("""
            SELECT data, valor_euros FROM vendas
            WHERE loja = %s AND data >= %s AND data < %s
            ORDER BY data
        """, (loja, today - timedelta(days=112), today))
        recent_rows_raw = cursor.fetchall()

        # Secondary source: sales_historico (fills gaps not covered by live vendas)
        cursor.execute("""
            SELECT data, SUM(valor_euros) AS total
            FROM sales_historico
            WHERE loja = %s AND data >= %s AND data < %s
            GROUP BY data
            ORDER BY data
        """, (loja, today - timedelta(days=112), today))
        historico_recent_raw = cursor.fetchall()

        # Merge: vendas takes priority; historico fills missing dates
        def _merge_sources(primary, secondary):
            merged = {r[0]: float(r[1]) for r in secondary}
            for r in primary:
                merged[r[0]] = float(r[1])
            return sorted(merged.items())

        recent_rows = _merge_sources(recent_rows_raw, historico_recent_raw)  # list of (date, value)

        # Group by weekday for fallback base when group signal has no data
        sales_by_weekday: dict[int, list] = {i: [] for i in range(7)}
        for d, v in recent_rows:
            wd = d.weekday()
            sales_by_weekday[wd].append(v)

        # Momentum: compare last 15 days vs last 30 days
        cutoff_30 = today - timedelta(days=30)
        cutoff_15 = today - timedelta(days=15)
        recent_30 = [v for d, v in recent_rows if d >= cutoff_30]
        recent_15 = [v for d, v in recent_rows if d >= cutoff_15]
        momentum_factor = _momentum_factor(recent_15, recent_30)

        # MTD YoY for header badge
        mtd_yoy = _compute_mtd_yoy(cursor, loja, today)

        # Load meteo config
        cursor.execute("""
            SELECT score_min, score_max, multiplicador
            FROM forecast_meteo_config
            WHERE loja = %s
            ORDER BY score_min
        """, (loja,))
        meteo_config = [{'score_min': r[0], 'score_max': r[1], 'multiplicador': r[2]} for r in cursor.fetchall()]

        # --- Load meteo scores: future (for forecast) + past 21 days (for similarity) ---
        meteo_by_date = _load_meteo_scores(cursor, today, horizon_days)
        past_meteo = _load_past_meteo_scores(cursor, today - timedelta(days=21), today - timedelta(days=1))

        # Pre-compute per-group statistics over the last 21 days
        # Used for confidence band floor and intra-group std dev
        cutoff_21 = today - timedelta(days=21)
        group_vals: dict[str, list] = {'semana': [], 'sexta': [], 'fds': []}
        group_floors: dict[str, Optional[float]] = {'semana': None, 'sexta': None, 'fds': None}
        for d, v in recent_rows:
            if d >= cutoff_21:
                g = _day_group(d.weekday())
                group_vals[g].append(v)
                if group_floors[g] is None or v < group_floors[g]:
                    group_floors[g] = v

        # Last-year homólogo map (simple day offset for display, not used in forecast)
        cursor.execute("""
            SELECT data, valor_euros FROM vendas
            WHERE loja = %s AND data >= %s AND data < %s
        """, (loja, today - timedelta(days=365 + 15), today - timedelta(days=358)))
        ly_vendas_map = {r[0]: float(r[1]) for r in cursor.fetchall()}
        cursor.execute("""
            SELECT data, SUM(valor_euros) FROM sales_historico
            WHERE loja = %s AND data >= %s AND data < %s
            GROUP BY data
        """, (loja, today - timedelta(days=365 + 15), today - timedelta(days=358)))
        for r in cursor.fetchall():
            if r[0] not in ly_vendas_map:
                ly_vendas_map[r[0]] = float(r[1])

        for i in range(1, horizon_days + 1):
            target = today + timedelta(days=i)
            wd = target.weekday()
            dg = _day_group(wd)

            # Step 1: Day-group + meteo-similarity signal
            # Looks at the last 21 days for same-group days, weights by meteo closeness.
            # anchor_date=today-1 ensures all forecast horizons share the same window.
            score = meteo_by_date.get(target)
            anchor = today - timedelta(days=1)
            base, signal_n = _day_group_signal(recent_rows, past_meteo, target, score, anchor_date=anchor)

            if base is None:
                recent_sig = _recent_weekday_signal(sales_by_weekday, wd)
                if recent_sig is not None:
                    base = recent_sig
                    signal_n = min(3, len(sales_by_weekday.get(wd, [])))
                else:
                    fb = _weighted_base(sales_by_weekday, wd)
                    all_values = [v for _, v in recent_rows]
                    base = fb if fb is not None else (statistics.mean(all_values) if all_values else 0.0)
                    signal_n = 0

            # Step 2: Momentum nudge (last 15 vs last 30 days)
            adjusted = base * momentum_factor

            # Step 3: Meteo multiplier (configured bands per store)
            mult, cond = _meteo_multiplier(score, meteo_config)
            forecast = adjusted * mult

            # Step 4: Confidence band with group floor (never drops to 0€)
            gvals = group_vals.get(dg, [])
            std_within = (
                statistics.stdev(gvals) if len(gvals) >= 2
                else (statistics.mean(gvals) * 0.15 if gvals else 50.0)
            )
            floor_val = group_floors.get(dg) or 0.0
            band_min = max(floor_val, forecast - 1.5 * std_within)
            band_max = forecast + 1.5 * std_within

            # Step 5: Manual override
            cursor.execute("""
                SELECT override_manual, override_motivo FROM sales_forecasts
                WHERE loja = %s AND data = %s AND override_manual IS NOT NULL
            """, (loja, target))
            override_row = cursor.fetchone()
            override_val = float(override_row[0]) if override_row else None
            override_mot = override_row[1] if override_row else None

            final = override_val if override_val is not None else forecast

            # Homólogo last year (for comparison display)
            ly_date = date(target.year - 1, target.month, target.day)
            homolog = ly_vendas_map.get(ly_date)

            epoch = get_epoch_label(target)
            day_type = _day_type(target)

            row_data = {
                'loja': loja,
                'data': target,
                'previsao_eur': round(final, 2),
                'banda_min': round(band_min, 2),
                'banda_max': round(band_max, 2),
                'score_meteo': score,
                'condicao_meteo': cond,
                'factor_yoy': round(mtd_yoy, 4) if mtd_yoy else None,
                'factor_momentum': round(momentum_factor, 4),
                'multiplicador_meteo': round(mult, 4),
                'base_historica': round(base, 2),
                'ly_anchor_eur': None,
                'override_manual': override_val,
                'override_motivo': override_mot,
                'homolog_eur': round(homolog, 2) if homolog else None,
                'epoch': epoch,
                'day_type': day_type,
                'day_group': dg,
                'signal_n': signal_n,
                'peso_semana': None,
                'effective_value': float(override_val or round(final, 2) or 0),
            }
            results.append(row_data)

            # Upsert forecast (don't overwrite existing override)
            cursor.execute("""
                INSERT INTO sales_forecasts
                    (loja, data, previsao_eur, banda_min, banda_max, score_meteo, condicao_meteo,
                     factor_yoy, multiplicador_meteo, base_historica, override_manual, override_motivo, gerado_em)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (loja, data) DO UPDATE SET
                    previsao_eur = EXCLUDED.previsao_eur,
                    banda_min = EXCLUDED.banda_min,
                    banda_max = EXCLUDED.banda_max,
                    score_meteo = EXCLUDED.score_meteo,
                    condicao_meteo = EXCLUDED.condicao_meteo,
                    factor_yoy = EXCLUDED.factor_yoy,
                    multiplicador_meteo = EXCLUDED.multiplicador_meteo,
                    base_historica = EXCLUDED.base_historica,
                    gerado_em = NOW()
            """, (
                loja, target, round(final, 2), round(band_min, 2), round(band_max, 2),
                score, cond, round(mtd_yoy, 4) if mtd_yoy else None, round(mult, 4), round(base, 2),
                override_val, override_mot
            ))

        conn.commit()

    return results


def _load_meteo_scores(cursor, today: date, horizon_days: int) -> dict:
    """Try to load weather scores from meteo_cache table (if exists). Returns {date: score}.
    Uses a savepoint to avoid aborting the outer transaction if meteo_cache doesn't exist."""
    scores = {}
    try:
        cursor.execute("SAVEPOINT sp_meteo")
        cursor.execute("""
            SELECT data, score FROM meteo_cache
            WHERE data >= %s AND data <= %s
        """, (today + timedelta(days=1), today + timedelta(days=horizon_days)))
        for r in cursor.fetchall():
            scores[r[0]] = int(r[1]) if r[1] is not None else None
        cursor.execute("RELEASE SAVEPOINT sp_meteo")
    except Exception:
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_meteo")
        except Exception:
            pass
    return scores


def _day_type(d: date) -> str:
    wd = d.weekday()
    if wd >= 5:
        return 'fim-de-semana'
    return 'semana'


def _day_group(weekday: int) -> str:
    """Three behavioral groups identified by the business.

    semana  — Mon, Tue, Wed, Thu (0–3): similar low-to-medium base.
    sexta   — Fri (4): consistently above semana, below fds.
    fds     — Sat, Sun (5–6): highest potential, strongly meteo-sensitive.
    """
    if weekday in (0, 1, 2, 3):
        return 'semana'
    elif weekday == 4:
        return 'sexta'
    else:
        return 'fds'


def _meteo_bucket(score: Optional[int]) -> str:
    """Classify a meteo score into one of three buckets.

    bom    ≥ 60: good/excellent weather → typically drives high sales.
    neutro 30–59: mixed conditions.
    mau    < 30 or None: rain/storm → strongly suppresses sales.
    """
    if score is None or score < 30:
        return 'mau'
    elif score < 60:
        return 'neutro'
    else:
        return 'bom'


def _day_group_signal(
    recent_rows: list,
    past_meteo: dict,
    target_date: date,
    target_score: Optional[int],
    anchor_date: Optional[date] = None,
) -> tuple[Optional[float], int]:
    """Primary forecast signal: 3-week lookback within day-group + meteo similarity.

    For the target day, collects all days in `recent_rows` that share the same
    behavioral group (semana / sexta / fds) within the last 21 days.  Weights
    each candidate day by how similar its recorded meteo was to the target:
        same bucket   → weight 3  (e.g. sunny vs sunny)
        adjacent bucket → weight 1  (e.g. sunny vs neutral)
        opposite bucket → weight 0  (excluded: sunny target ignores rainy past days)
    If no same/adjacent days exist, falls back to all same-group days equally.

    The lookback window is always anchored to `anchor_date` (default: today, i.e.
    the day generate_forecasts is called) so that near and far forecast horizons all
    use the same 21 days of real history.  Using target_date as the anchor would
    shrink the window for targets further in the future.

    Returns (weighted_mean, n_contributing_days).
    """
    target_group = _day_group(target_date.weekday())
    target_bucket = _meteo_bucket(target_score)
    bucket_order = ['mau', 'neutro', 'bom']
    target_idx = bucket_order.index(target_bucket)

    # Fixed 21-day window anchored at the generation date (today), NOT the target date.
    # This ensures day+1 and day+21 see exactly the same historical window.
    # window_end=ref, window_start=ref-20 → exactly 21 days inclusive (ref-20 … ref).
    ref = anchor_date if anchor_date is not None else (target_date - timedelta(days=1))
    window_end = ref
    window_start = ref - timedelta(days=20)

    candidates = []
    for d, v in recent_rows:
        if not (window_start <= d <= window_end):
            continue
        if _day_group(d.weekday()) != target_group:
            continue
        past_score = past_meteo.get(d)
        past_bucket = _meteo_bucket(past_score)
        past_idx = bucket_order.index(past_bucket)
        distance = abs(target_idx - past_idx)
        weight = {0: 3, 1: 1, 2: 0}[distance]
        candidates.append((v, weight))

    valid = [(v, w) for v, w in candidates if w > 0]
    if not valid and candidates:
        valid = [(v, 1) for v, _ in candidates]

    if not valid:
        return None, 0

    total_weight = sum(w for _, w in valid)
    weighted_sum = sum(v * w for v, w in valid)
    return (weighted_sum / total_weight if total_weight > 0 else None), len(valid)


def _load_past_meteo_scores(cursor, from_date: date, to_date: date) -> dict:
    """Load historical weather scores from meteo_cache for a date range.

    Returns {date: score}. Uses a savepoint so missing table is handled gracefully.
    """
    scores: dict = {}
    try:
        cursor.execute("SAVEPOINT sp_past_meteo")
        cursor.execute("""
            SELECT data, score FROM meteo_cache
            WHERE data >= %s AND data <= %s
        """, (from_date, to_date))
        for r in cursor.fetchall():
            scores[r[0]] = int(r[1]) if r[1] is not None else None
        cursor.execute("RELEASE SAVEPOINT sp_past_meteo")
    except Exception:
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_past_meteo")
        except Exception:
            pass
    return scores


def _compute_mtd_yoy(cursor, loja: str, today: date) -> Optional[float]:
    """Month-to-date YoY rate: current month vendas vs same period last year.

    Uses `sales_historico` for last year (imported 2025 data); for the same
    date range this year reads from `vendas` (live POS data).  Vendas takes
    priority over historico for the LY period if both exist.

    Returns the ratio (e.g. 1.67 = +67%) or None when data is insufficient.
    """
    current_start = date(today.year, today.month, 1)
    ly_start = date(today.year - 1, today.month, 1)
    # Compare same number of elapsed days
    try:
        ly_end = date(today.year - 1, today.month, today.day - 1) if today.day > 1 else ly_start
    except ValueError:
        ly_end = ly_start

    if ly_start > ly_end:
        return None

    # Current month (live POS data)
    cursor.execute("""
        SELECT COALESCE(SUM(valor_euros), 0) FROM vendas
        WHERE loja = %s AND data >= %s AND data < %s
    """, (loja, current_start, today))
    current = float(cursor.fetchone()[0])

    # Last year: merge historico + vendas (vendas priority per-date)
    cursor.execute("""
        SELECT data, SUM(valor_euros) AS total FROM sales_historico
        WHERE loja = %s AND data >= %s AND data <= %s
        GROUP BY data
    """, (loja, ly_start, ly_end))
    ly_by_date = {r[0]: float(r[1]) for r in cursor.fetchall()}

    cursor.execute("""
        SELECT data, valor_euros FROM vendas
        WHERE loja = %s AND data >= %s AND data <= %s
    """, (loja, ly_start, ly_end))
    for r in cursor.fetchall():
        ly_by_date[r[0]] = float(r[1])

    ly_total = sum(ly_by_date.values())

    if current <= 0 or ly_total <= 0:
        return None

    return round(current / ly_total, 4)


# ---------------------------------------------------------------------------
# Get saved forecasts for display
# ---------------------------------------------------------------------------

def get_forecasts(loja: Optional[str] = None, from_date: Optional[date] = None, to_date: Optional[date] = None) -> list[dict]:
    """Retrieve saved forecasts. If loja is None, returns all stores (consolidated)."""
    today = date.today()
    if from_date is None:
        from_date = today + timedelta(days=1)
    if to_date is None:
        to_date = today + timedelta(days=7)

    with db_connection() as conn:
        cursor = conn.cursor()
        if loja:
            cursor.execute("""
                SELECT loja, data, previsao_eur, banda_min, banda_max, score_meteo, condicao_meteo,
                       factor_yoy, multiplicador_meteo, base_historica, override_manual, override_motivo,
                       venda_real, erro_real_pct, gerado_em
                FROM sales_forecasts
                WHERE loja = %s AND data BETWEEN %s AND %s
                ORDER BY data
            """, (loja, from_date, to_date))
        else:
            cursor.execute("""
                SELECT loja, data, previsao_eur, banda_min, banda_max, score_meteo, condicao_meteo,
                       factor_yoy, multiplicador_meteo, base_historica, override_manual, override_motivo,
                       venda_real, erro_real_pct, gerado_em
                FROM sales_forecasts
                WHERE data BETWEEN %s AND %s
                ORDER BY data, loja
            """, (from_date, to_date))

        rows = cursor.fetchall()
        cols = ['loja', 'data', 'previsao_eur', 'banda_min', 'banda_max', 'score_meteo',
                'condicao_meteo', 'factor_yoy', 'multiplicador_meteo', 'base_historica',
                'override_manual', 'override_motivo', 'venda_real', 'erro_real_pct', 'gerado_em']
        results = []
        for r in rows:
            d = dict(zip(cols, r))
            d['epoch'] = get_epoch_label(d['data'])
            d['day_type'] = _day_type(d['data'])
            d['day_group'] = _day_group(d['data'].weekday())
            d['effective_value'] = float(d['override_manual'] or d['previsao_eur'] or 0)
            d.setdefault('factor_momentum', None)
            d.setdefault('ly_anchor_eur', None)
            d.setdefault('signal_n', None)
            results.append(d)

    return results


def get_consolidated_forecasts(from_date: Optional[date] = None, to_date: Optional[date] = None) -> list[dict]:
    """Sum forecasts across all stores per day."""
    rows = get_forecasts(loja=None, from_date=from_date, to_date=to_date)
    by_date: dict = {}
    for r in rows:
        d = r['data']
        if d not in by_date:
            by_date[d] = {
                'data': d,
                'previsao_eur': 0.0,
                'banda_min': 0.0,
                'banda_max': 0.0,
                'epoch': r['epoch'],
                'day_type': r['day_type'],
                'venda_real': 0.0,
                'homolog_stores': [],
                'condicao_meteo': r.get('condicao_meteo'),
                'factor_yoy': r.get('factor_yoy'),
            }
        by_date[d]['previsao_eur'] += float(r['previsao_eur'] or 0)
        by_date[d]['banda_min'] += float(r['banda_min'] or 0)
        by_date[d]['banda_max'] += float(r['banda_max'] or 0)
        if r.get('venda_real'):
            by_date[d]['venda_real'] += float(r['venda_real'])
    return sorted(by_date.values(), key=lambda x: x['data'])


# ---------------------------------------------------------------------------
# Override manual
# ---------------------------------------------------------------------------

def set_forecast_override(loja: str, data: date, override_eur: Optional[float], motivo: Optional[str]) -> bool:
    """Set or clear a manual override for a given store/day."""
    with db_connection() as conn:
        cursor = conn.cursor()
        if override_eur is None:
            cursor.execute("""
                UPDATE sales_forecasts SET override_manual = NULL, override_motivo = NULL
                WHERE loja = %s AND data = %s
            """, (loja, data))
        else:
            cursor.execute("""
                INSERT INTO sales_forecasts (loja, data, override_manual, override_motivo)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (loja, data) DO UPDATE SET
                    override_manual = EXCLUDED.override_manual,
                    override_motivo = EXCLUDED.override_motivo
            """, (loja, data, override_eur, motivo))
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Associate real sales + recalibration
# ---------------------------------------------------------------------------

def associate_real_sales(loja: str, cutoff_date: Optional[date] = None):
    """For each past forecast, look up the actual venda and compute erro_real_pct."""
    if cutoff_date is None:
        cutoff_date = date.today()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sf.id, sf.data, sf.previsao_eur, sf.override_manual, v.valor_euros
            FROM sales_forecasts sf
            LEFT JOIN vendas v ON v.loja = sf.loja AND v.data = sf.data
            WHERE sf.loja = %s AND sf.data < %s AND sf.venda_real IS NULL
            ORDER BY sf.data
        """, (loja, cutoff_date))
        rows = cursor.fetchall()
        for fid, fdata, previsao, override, real in rows:
            if real is None:
                continue
            effective = float(override or previsao or 0)
            if effective > 0:
                erro = (float(real) - effective) / effective
            else:
                erro = 0.0
            cursor.execute("""
                UPDATE sales_forecasts SET venda_real = %s, erro_real_pct = %s
                WHERE id = %s
            """, (real, round(erro, 6), fid))
        conn.commit()


# ---------------------------------------------------------------------------
# Accuracy / MAPE dashboard
# ---------------------------------------------------------------------------

def get_accuracy_data(loja: Optional[str] = None, weeks: int = 12) -> dict:
    """Return MAPE stats by store and day type for the last `weeks` weeks."""
    cutoff = date.today() - timedelta(weeks=weeks)
    with db_connection() as conn:
        cursor = conn.cursor()
        base_cond = "sf.venda_real IS NOT NULL AND sf.previsao_eur IS NOT NULL AND ABS(sf.erro_real_pct) < 5"
        if loja:
            cursor.execute(f"""
                SELECT sf.loja, sf.data, sf.previsao_eur, sf.override_manual,
                       sf.venda_real, sf.erro_real_pct
                FROM sales_forecasts sf
                WHERE sf.loja = %s AND sf.data >= %s AND {base_cond}
                ORDER BY sf.data DESC
            """, (loja, cutoff))
        else:
            cursor.execute(f"""
                SELECT sf.loja, sf.data, sf.previsao_eur, sf.override_manual,
                       sf.venda_real, sf.erro_real_pct
                FROM sales_forecasts sf
                WHERE sf.data >= %s AND {base_cond}
                ORDER BY sf.data DESC
            """, (cutoff,))

        rows = cursor.fetchall()

    if not rows:
        return {
            'by_loja': {},
            'by_day_type': {'semana': None, 'fim-de-semana': None},
            'worst_days': [],
            'error_trend': [],
        }

    by_loja: dict = {}
    by_day_type: dict = {'semana': [], 'fim-de-semana': []}
    worst = []
    trend_by_week: dict = {}

    for row_loja, row_date, previsao, override, real, erro in rows:
        ape = abs(float(erro)) * 100 if erro else 0
        dt = _day_type(row_date)
        eff = float(override or previsao or 0)

        if row_loja not in by_loja:
            by_loja[row_loja] = []
        by_loja[row_loja].append(ape)
        by_day_type[dt].append(ape)
        worst.append({'data': row_date, 'loja': row_loja, 'erro_pct': round(ape, 1),
                      'real': float(real), 'previsao': round(eff, 2)})

        week_key = row_date.isocalendar()[:2]
        if week_key not in trend_by_week:
            trend_by_week[week_key] = []
        trend_by_week[week_key].append(ape)

    mape_by_loja = {l: round(statistics.mean(vals), 1) for l, vals in by_loja.items()}
    mape_day = {
        'semana': round(statistics.mean(by_day_type['semana']), 1) if by_day_type['semana'] else None,
        'fim-de-semana': round(statistics.mean(by_day_type['fim-de-semana']), 1) if by_day_type['fim-de-semana'] else None,
    }
    worst_sorted = sorted(worst, key=lambda x: -x['erro_pct'])[:10]
    trend = []
    for (yr, wk), vals in sorted(trend_by_week.items()):
        trend.append({'year': yr, 'week': wk, 'mape': round(statistics.mean(vals), 1), 'n': len(vals)})

    return {
        'by_loja': mape_by_loja,
        'by_day_type': mape_day,
        'worst_days': worst_sorted,
        'error_trend': trend[-weeks:],
    }


# ---------------------------------------------------------------------------
# Meteo multiplier auto-calibration
# ---------------------------------------------------------------------------

def calibrate_meteo_multipliers(loja: str, days: int = 90) -> dict:
    """Recalibrate weather multipliers for `loja` from historical sales data.

    Reads weather_data (per-store composite score) + vendas/sales_historico
    for the last `days` days (90–180), bins by weather score
    (0–29, 30–59, 60–84, 85–100), computes mean sales per bin, normalises
    so the "good weather" bin (60–84) = 1.00, and updates
    forecast_meteo_config for this store.

    Falls back to meteo_cache if weather_data has no rows for this store.
    Returns a summary dict with the derived multipliers and diagnostics.
    """
    today = date.today()
    cutoff = today - timedelta(days=days)

    bins = [(0, 29), (30, 59), (60, 84), (85, 100)]
    label_map = {
        (0, 29):   'Mau tempo (0–29)',
        (30, 59):  'Tempo incerto (30–59)',
        (60, 84):  'Bom tempo (60–84)',
        (85, 100): 'Excelente (85–100)',
    }

    with db_connection() as conn:
        cursor = conn.cursor()

        # Resolve store_id so weather queries are scoped to this specific store
        cursor.execute(
            "SELECT id FROM stores WHERE name = %s AND is_active = TRUE LIMIT 1",
            (loja,),
        )
        store_row = cursor.fetchone()
        store_id = store_row[0] if store_row else None

        # Load vendas for the window (aggregate per day — safe against duplicate rows)
        # vendas takes priority over sales_historico
        cursor.execute("""
            SELECT data, SUM(valor_euros) FROM vendas
            WHERE loja = %s AND data >= %s AND data < %s
            GROUP BY data
        """, (loja, cutoff, today))
        vendas_by_date = {r[0]: float(r[1]) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT data, SUM(valor_euros) FROM sales_historico
            WHERE loja = %s AND data >= %s AND data < %s
            GROUP BY data
        """, (loja, cutoff, today))
        for r in cursor.fetchall():
            if r[0] not in vendas_by_date:
                vendas_by_date[r[0]] = float(r[1])

        if not vendas_by_date:
            return {'error': 'Sem dados de vendas suficientes para calibrar.', 'multipliers': {}}

        # --- Primary: load per-store composite weather score from weather_data ---
        # AVG(score) across sources (fontes) per day for this store.
        meteo_by_date: dict = {}
        if store_id is not None:
            cursor.execute("""
                SELECT data, ROUND(AVG(score)::numeric, 0)
                FROM weather_data
                WHERE store_id = %s AND score IS NOT NULL
                  AND data >= %s AND data < %s
                GROUP BY data
            """, (store_id, cutoff, today))
            for r in cursor.fetchall():
                if r[1] is not None:
                    meteo_by_date[r[0]] = int(r[1])

        # --- Fallback: meteo_cache (non-store-specific daily composite) ---
        if not meteo_by_date:
            try:
                cursor.execute("SAVEPOINT sp_cal_meteo")
                cursor.execute("""
                    SELECT data, score FROM meteo_cache
                    WHERE data >= %s AND data < %s
                """, (cutoff, today))
                for r in cursor.fetchall():
                    if r[1] is not None:
                        meteo_by_date[r[0]] = int(r[1])
                cursor.execute("RELEASE SAVEPOINT sp_cal_meteo")
            except Exception:
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT sp_cal_meteo")
                except Exception:
                    pass

        if not meteo_by_date:
            return {'error': 'Sem dados meteorológicos suficientes para calibrar.', 'multipliers': {}}

        # Bin sales by weather score
        bin_sales: dict = {b: [] for b in bins}
        matched = 0
        for d, score in meteo_by_date.items():
            if d in vendas_by_date:
                for b in bins:
                    if b[0] <= score <= b[1]:
                        bin_sales[b].append(vendas_by_date[d])
                        matched += 1
                        break

        if matched < 10:
            return {
                'error': (
                    f'Dados insuficientes ({matched} dias com vendas + meteo). '
                    'São necessários pelo menos 10 dias sobrepostos.'
                ),
                'multipliers': {},
            }

        # Compute mean sales per bin
        bin_means: dict = {}
        for b, vals in bin_sales.items():
            if vals:
                bin_means[b] = statistics.mean(vals)

        if not bin_means:
            return {'error': 'Nenhum bin com dados suficientes.', 'multipliers': {}}

        # Reference: "good weather" bin (60–84); fall back to overall mean
        ref_bin = (60, 84)
        ref_value = bin_means.get(ref_bin) or statistics.mean(bin_means.values())
        if ref_value <= 0:
            return {'error': 'Valor de referência inválido para normalização.', 'multipliers': {}}

        # Derive multipliers, clamped to [0.40, 2.00]
        derived: dict = {}
        for b in bins:
            if b in bin_means:
                raw = bin_means[b] / ref_value
                derived[b] = round(max(0.40, min(2.00, raw)), 4)

        # Upsert forecast_meteo_config — insert row if missing, update if present
        for b, mult in derived.items():
            cursor.execute("""
                INSERT INTO forecast_meteo_config (store_id, loja, score_min, score_max, multiplicador, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (loja, score_min, score_max) DO UPDATE
                    SET multiplicador = EXCLUDED.multiplicador,
                        updated_at = NOW()
            """, (store_id, loja, b[0], b[1], mult))

        conn.commit()

        # Build result summary
        multipliers = {}
        for b in bins:
            lbl = label_map[b]
            if b in derived:
                multipliers[lbl] = {
                    'multiplier': derived[b],
                    'mean_sales': round(bin_means[b], 2),
                    'n_days': len(bin_sales[b]),
                }
            else:
                multipliers[lbl] = {
                    'multiplier': None,
                    'mean_sales': None,
                    'n_days': 0,
                    'note': 'Sem dados — multiplicador inalterado',
                }

        return {
            'loja': loja,
            'days_analysed': days,
            'matched_days': matched,
            'reference_bin': f'{ref_bin[0]}–{ref_bin[1]}',
            'reference_mean_sales': round(ref_value, 2),
            'multipliers': multipliers,
        }


# ---------------------------------------------------------------------------
# Meteo config CRUD
# ---------------------------------------------------------------------------

def get_meteo_config(loja: str) -> list[dict]:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, score_min, score_max, multiplicador
            FROM forecast_meteo_config WHERE loja = %s ORDER BY score_min
        """, (loja,))
        return [{'id': r[0], 'score_min': r[1], 'score_max': r[2], 'multiplicador': float(r[3])}
                for r in cursor.fetchall()]


def update_meteo_config(loja: str, score_min: int, score_max: int, multiplicador: float):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE forecast_meteo_config SET multiplicador = %s, updated_at = NOW()
            WHERE loja = %s AND score_min = %s AND score_max = %s
        """, (multiplicador, loja, score_min, score_max))
        conn.commit()


# ---------------------------------------------------------------------------
# Get past homolog for comparison
# ---------------------------------------------------------------------------

def get_past_sales(loja: Optional[str], from_date: date, to_date: date) -> list[dict]:
    """Return daily sales totals for the given date range, merged from vendas + sales_historico.

    If loja is None, returns consolidated totals across all stores (summed per date).
    Each row: {data, valor, loja, weekday_label, day_group, day_type}
    """
    with db_connection() as conn:
        cursor = conn.cursor()

        if loja:
            # Merge vendas (live) + sales_historico; vendas takes priority
            cursor.execute("""
                SELECT data, SUM(valor_euros) FROM sales_historico
                WHERE loja = %s AND data >= %s AND data <= %s
                GROUP BY data
            """, (loja, from_date, to_date))
            by_date = {r[0]: float(r[1]) for r in cursor.fetchall()}

            cursor.execute("""
                SELECT data, valor_euros FROM vendas
                WHERE loja = %s AND data >= %s AND data <= %s
            """, (loja, from_date, to_date))
            for r in cursor.fetchall():
                by_date[r[0]] = float(r[1])
        else:
            # Consolidated: sum all stores per date
            cursor.execute("""
                SELECT data, SUM(valor_euros) FROM sales_historico
                WHERE data >= %s AND data <= %s
                GROUP BY data
            """, (from_date, to_date))
            by_date = {r[0]: float(r[1]) for r in cursor.fetchall()}

            cursor.execute("""
                SELECT data, loja, valor_euros FROM vendas
                WHERE data >= %s AND data <= %s
            """, (from_date, to_date))
            # vendas overrides historico per-date globally (consolidate sum)
            vendas_by_date: dict = {}
            for r in cursor.fetchall():
                d, _, v = r
                vendas_by_date[d] = vendas_by_date.get(d, 0.0) + float(v)
            by_date.update(vendas_by_date)

    _wdpt = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']
    rows = []
    for d in sorted(by_date):
        rows.append({
            'data': d,
            'valor': by_date[d],
            'loja': loja or 'Todas',
            'weekday_label': _wdpt[d.weekday()],
            'day_group': _day_group(d.weekday()),
            'day_type': _day_type(d),
        })
    return rows


def get_homolog_sales(loja: str, from_date: date, to_date: date) -> dict:
    """Return {date: valor} for the same date window last year."""
    ly_from = date(from_date.year - 1, from_date.month, from_date.day)
    ly_to = date(to_date.year - 1, to_date.month, to_date.day)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT data, valor_euros FROM vendas
            WHERE loja = %s AND data BETWEEN %s AND %s
            ORDER BY data
        """, (loja, ly_from, ly_to))
        return {r[0]: float(r[1]) for r in cursor.fetchall()}
