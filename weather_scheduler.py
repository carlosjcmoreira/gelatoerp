import logging
import os
import threading
from datetime import datetime

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
            _scheduler.start()
            logger.info("Weather scheduler started (bi-daily: 07-08h and 13-14h Lisbon time; meteo calibration: Mon 03:00)")
            t = threading.Thread(target=_run_weather_update, daemon=True, name="weather-init")
            t.start()
            logger.info("Weather scheduler: triggered immediate update on startup")
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


def get_scheduler_status() -> dict:
    if _scheduler is None:
        return {"running": False, "next_run": None}
    try:
        job = _scheduler.get_job("weather_update")
        next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
        return {"running": _scheduler.running, "next_run": next_run}
    except Exception:
        return {"running": False, "next_run": None}
