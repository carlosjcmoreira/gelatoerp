"""
KPI Service — wraps KPI calculation with intelligent caching.

Key insight: KPI results for past months/years are immutable once the month closes.
  - Current month  → short TTL (5 min)
  - Past months    → long TTL (1 hour)
  - Past years     → very long TTL (24 hours)

Routes should call this service instead of calling calculate_kpi_* directly.
"""
import time
import threading
import logging
from datetime import date

logger = logging.getLogger(__name__)

_cache: dict = {}
_lock = threading.Lock()

TTL_CURRENT = 300      # 5 min — today's data changes often
TTL_PAST_MONTH = 3600  # 1 hour — last month mostly closed
TTL_PAST_YEAR = 86400  # 24 hours — past years are immutable


def _get(key: str):
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry and now < entry[1]:
            return entry[0], True
    return None, False


def _set(key: str, value, ttl: int):
    with _lock:
        _cache[key] = (value, time.monotonic() + ttl)


def get_daily_kpi(data_inicio: date, data_fim: date, loja: str = None):
    """
    Returns daily KPI DataFrame for the given period and optional store filter.
    Caches results — past periods get a longer TTL than current-month ranges.
    """
    from database import calculate_kpi_by_day
    today = date.today()
    key = f"kpi_daily:{loja}:{data_inicio}:{data_fim}"
    result, hit = _get(key)
    if hit:
        return result

    result = calculate_kpi_by_day(loja=loja, data_inicio=data_inicio, data_fim=data_fim)

    # Determine TTL based on whether the period is in the past
    if data_fim < date(today.year, today.month, 1):
        ttl = TTL_PAST_MONTH
    else:
        ttl = TTL_CURRENT
    _set(key, result, ttl)
    return result


def get_monthly_kpi(year: int, month: int, loja: str = None):
    """
    Returns monthly KPI dict.
    Past months are cached for 1 hour; current month for 5 minutes.
    """
    from database import calculate_kpi_monthly
    today = date.today()
    key = f"kpi_monthly:{loja}:{year}:{month}"
    result, hit = _get(key)
    if hit:
        return result

    result = calculate_kpi_monthly(year=year, month=month, loja=loja)

    past = (year < today.year) or (year == today.year and month < today.month)
    ttl = TTL_PAST_MONTH if past else TTL_CURRENT
    _set(key, result, ttl)
    return result


def get_annual_kpi(year: int, loja: str = None):
    """
    Returns annual KPI dict.
    Past years are cached for 24 hours; current year for 5 minutes.
    """
    from database import calculate_kpi_annual
    today = date.today()
    key = f"kpi_annual:{loja}:{year}"
    result, hit = _get(key)
    if hit:
        return result

    result = calculate_kpi_annual(year=year, loja=loja)

    ttl = TTL_PAST_YEAR if year < today.year else TTL_CURRENT
    _set(key, result, ttl)
    return result


def invalidate_for_date(data: date, loja: str = None):
    """
    Call after any write that affects KPI data (new venda, producao, quebra etc.)
    for the given date.  Clears cached daily and monthly results that include that date.
    """
    key_prefix_daily = f"kpi_daily:{loja}:"
    key_prefix_monthly = f"kpi_monthly:{loja}:{data.year}:{data.month}"
    with _lock:
        to_del = [
            k for k in _cache
            if k.startswith(key_prefix_daily) or k.startswith(key_prefix_monthly)
        ]
        for k in to_del:
            del _cache[k]
    if to_del:
        logger.debug("KPI cache invalidated %d entries for date %s / loja %s", len(to_del), data, loja)
