import logging
import os
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_scheduler = None
_scheduler_lock = threading.Lock()

_SCHEDULER_PROCESS_ROLE_ENV = "WEATHER_SCHEDULER_ENABLED"


def _is_scheduler_process() -> bool:
    """Return True only if this process should run the scheduler.

    In multi-worker deployments (e.g. gunicorn with multiple workers),
    set the env var WEATHER_SCHEDULER_ENABLED=1 only in one worker/process
    (e.g. via --preload + os.getpid() guard or a dedicated worker class).
    In single-process dev mode the env var is not needed and defaults to True.
    """
    env_val = os.environ.get(_SCHEDULER_PROCESS_ROLE_ENV)
    if env_val is None:
        return True
    return env_val.strip() in ("1", "true", "yes", "True")


def _run_weather_update():
    try:
        import database as db
        import weather_service as ws

        stores = db.get_all_stores()
        active_stores = [s for s in stores if s.get("is_active")]
        logger.info("Weather scheduler: updating %d active stores", len(active_stores))

        for store in active_stores:
            try:
                forecasts = ws.fetch_all_forecasts_for_store(store)
                if forecasts:
                    db.upsert_weather_data_batch(forecasts)
                    logger.info("Weather update for store '%s': %d records saved", store["name"], len(forecasts))
                else:
                    logger.warning("Weather update for store '%s': no data returned", store["name"])
            except Exception as e:
                logger.error("Weather update failed for store '%s': %s", store.get("name"), e)

    except Exception as e:
        logger.error("Weather scheduler run error: %s", e)


def _run_meteo_calibration():
    """Weekly auto-calibration of weather multipliers for all active stores."""
    try:
        import database as db
        from db.forecast import calibrate_meteo_multipliers

        stores = db.get_all_stores()
        active_stores = [s for s in stores if s.get("is_active")]
        logger.info("Meteo calibration: running for %d stores", len(active_stores))

        for store in active_stores:
            name = store.get("name", "")
            try:
                result = calibrate_meteo_multipliers(name, days=90)
                if "error" in result:
                    logger.warning("Meteo calibration skipped for '%s': %s", name, result["error"])
                else:
                    logger.info(
                        "Meteo calibration for '%s': %d matched days, multipliers updated",
                        name, result.get("matched_days", 0),
                    )
            except Exception as e:
                logger.error("Meteo calibration failed for store '%s': %s", name, e)

    except Exception as e:
        logger.error("Meteo calibration scheduler run error: %s", e)


def _run_gap_fill():
    """Detect and backfill missing historical weather data (open-meteo) for all active stores.

    The expected history start date is read from the WEATHER_HISTORY_START_DATE environment
    variable (ISO format, e.g. "2026-01-01").  It defaults to 2026-01-01 if not set.
    Gaps from that date up to and including yesterday are detected per-store and filled
    automatically using the Open-Meteo historical archive API.
    """
    try:
        import database as db
        import weather_service as ws
        from db.meteorologia import get_missing_weather_dates, upsert_weather_data_batch

        history_start_str = os.environ.get("WEATHER_HISTORY_START_DATE", "2026-01-01")
        try:
            start_date = date.fromisoformat(history_start_str)
        except ValueError:
            logger.warning("Gap fill: invalid WEATHER_HISTORY_START_DATE '%s', defaulting to 90 days ago",
                           history_start_str)
            start_date = date.today() - timedelta(days=90)

        yesterday = date.today() - timedelta(days=1)

        if start_date > yesterday:
            logger.info("Gap fill: nothing to check — start_date=%s is after yesterday=%s",
                        start_date, yesterday)
            return

        stores = db.get_all_stores()
        active_stores = [
            s for s in stores
            if s.get("is_active") and s.get("latitude") is not None and s.get("longitude") is not None
        ]

        if not active_stores:
            logger.info("Gap fill: no active stores with coordinates — skipping")
            return

        logger.info("Gap fill: checking %d active stores for missing weather data [%s → %s]",
                    len(active_stores), start_date, yesterday)

        total_filled = 0
        stores_with_gaps = 0

        for store in active_stores:
            store_id = store["id"]
            name = store.get("name", f"store#{store_id}")
            lat = store["latitude"]
            lon = store["longitude"]

            try:
                missing_dates = get_missing_weather_dates(store_id, start_date, yesterday)
            except Exception as e:
                logger.error("Gap fill: failed to query missing dates for store '%s': %s", name, e)
                continue

            if not missing_dates:
                logger.debug("Gap fill: store '%s' — no gaps found", name)
                continue

            stores_with_gaps += 1
            logger.info("Gap fill: store '%s' — %d missing date(s) detected", name, len(missing_dates))

            ranges = []
            r_start = r_end = missing_dates[0]
            for d in missing_dates[1:]:
                if (d - r_end).days == 1:
                    r_end = d
                else:
                    ranges.append((r_start, r_end))
                    r_start = r_end = d
            ranges.append((r_start, r_end))

            store_filled = 0
            for r_start, r_end in ranges:
                try:
                    records = ws.fetch_openmeteo_historical(lat, lon, r_start, r_end)
                    if records:
                        for rec in records:
                            rec["store_id"] = store_id
                        upsert_weather_data_batch(records)
                        store_filled += len(records)
                        logger.info("Gap fill: store '%s' — filled %d record(s) for [%s → %s]",
                                    name, len(records), r_start, r_end)
                    else:
                        logger.warning("Gap fill: store '%s' — no data returned for [%s → %s]",
                                       name, r_start, r_end)
                except Exception as e:
                    logger.error("Gap fill: store '%s' — error fetching [%s → %s]: %s",
                                 name, r_start, r_end, e)

            total_filled += store_filled
            logger.info("Gap fill: store '%s' — %d record(s) filled across %d range(s)",
                        name, store_filled, len(ranges))

        if stores_with_gaps == 0:
            logger.info("Gap fill: complete — no gaps found across %d store(s) [%s → %s]",
                        len(active_stores), start_date, yesterday)
        else:
            logger.info("Gap fill: complete — %d record(s) filled for %d store(s) with gaps",
                        total_filled, stores_with_gaps)

    except Exception as e:
        logger.error("Gap fill run error: %s", e)


def start_weather_scheduler():
    global _scheduler
    if not _is_scheduler_process():
        logger.info("Weather scheduler skipped in this process (WEATHER_SCHEDULER_ENABLED not set for this worker).")
        return
    with _scheduler_lock:
        if _scheduler is not None:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            _scheduler = BackgroundScheduler(timezone="Europe/Lisbon")
            _scheduler.add_job(
                _run_weather_update,
                CronTrigger(hour="7,13", minute="30", timezone="Europe/Lisbon"),
                id="weather_update",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            _scheduler.add_job(
                _run_meteo_calibration,
                CronTrigger(day_of_week="mon", hour="3", minute="0", timezone="Europe/Lisbon"),
                id="meteo_calibration",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            _scheduler.add_job(
                _run_gap_fill,
                CronTrigger(hour="2", minute="0", timezone="Europe/Lisbon"),
                id="weather_gap_fill",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            _scheduler.start()
            logger.info("Weather scheduler started (bi-daily: 07:30 and 13:30; gap fill: 02:00; meteo calibration: Mon 03:00)")
            t = threading.Thread(target=_run_weather_update, daemon=True, name="weather-init")
            t.start()
            logger.info("Weather scheduler: triggered immediate update on startup")
            t2 = threading.Thread(target=_run_gap_fill, daemon=True, name="weather-gap-fill")
            t2.start()
            logger.info("Weather scheduler: triggered gap fill check on startup")
        except Exception as e:
            logger.error("Failed to start weather scheduler: %s", e)


def stop_weather_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
                logger.info("Weather scheduler stopped.")
            except Exception:
                pass
            _scheduler = None


def trigger_weather_update_now():
    try:
        import threading
        t = threading.Thread(target=_run_weather_update, daemon=True)
        t.start()
        return True
    except Exception as e:
        logger.error("Failed to trigger weather update: %s", e)
        return False


def trigger_gap_fill_now():
    """Manually trigger a gap-fill run in the background."""
    try:
        t = threading.Thread(target=_run_gap_fill, daemon=True)
        t.start()
        return True
    except Exception as e:
        logger.error("Failed to trigger gap fill: %s", e)
        return False


def get_scheduler_status() -> dict:
    base = {"running": False, "next_run": None, "next_gap_fill_run": None}
    if _scheduler is None:
        return base
    try:
        job = _scheduler.get_job("weather_update")
        gap_job = _scheduler.get_job("weather_gap_fill")
        return {
            "running": _scheduler.running,
            "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
            "next_gap_fill_run": gap_job.next_run_time.isoformat() if gap_job and gap_job.next_run_time else None,
        }
    except Exception:
        return base
