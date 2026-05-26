"""
OneDrive retry scheduler — runs hourly, attempts upload for invoices
with pdf_filename set but no onedrive_path yet.

Uses the same APScheduler + single-worker guard pattern as weather_scheduler.py.
Env var ONEDRIVE_SCHEDULER_ENABLED controls which gunicorn worker runs this.
When not set, defaults to True (suitable for single-worker dev mode).
"""
import logging
import os
import threading

logger = logging.getLogger(__name__)

_scheduler = None
_scheduler_lock = threading.Lock()

_SCHEDULER_PROCESS_ROLE_ENV = "ONEDRIVE_SCHEDULER_ENABLED"


def _is_scheduler_process() -> bool:
    env_val = os.environ.get(_SCHEDULER_PROCESS_ROLE_ENV)
    if env_val is None:
        return True
    return env_val.strip() in ("1", "true", "yes", "True")


def _run_onedrive_retry():
    """Hourly job: attempt OneDrive upload for invoices missing onedrive_path."""
    try:
        from db.faturas import (
            get_invoices_missing_onedrive,
            mark_onedrive_retry,
            mark_onedrive_failed,
            get_invoice_pdf,
            update_invoice_onedrive,
        )
        from flask_app.onedrive_archive import upload_invoice_pdf, is_configured

        if not is_configured():
            logger.debug("OneDrive retry: not configured — skipped")
            return

        pending = get_invoices_missing_onedrive()
        if not pending:
            logger.debug("OneDrive retry: no pending invoices")
            return

        logger.info("OneDrive retry: %d invoice(s) pending upload", len(pending))

        for inv in pending:
            invoice_id = inv["id"]
            try:
                pdf_data, pdf_filename = get_invoice_pdf(invoice_id)
                if not pdf_data:
                    logger.debug(
                        "OneDrive retry: invoice %s — no PDF data, skipping", invoice_id
                    )
                    continue

                subfolder = inv.get("onedrive_subfolder") or "Geral (G)"
                issue_date = inv.get("issue_date")

                result = upload_invoice_pdf(
                    pdf_data,
                    pdf_filename or "fatura.pdf",
                    subfolder,
                    issue_date,
                )

                if result.get("onedrive_path"):
                    update_invoice_onedrive(
                        invoice_id,
                        result["onedrive_path"],
                        subfolder,
                        onedrive_web_url=result.get("web_url"),
                    )
                    logger.info(
                        "OneDrive retry: invoice %s uploaded → %s",
                        invoice_id,
                        result["onedrive_path"],
                    )
                else:
                    retry_at = inv.get("onedrive_retry_at")
                    if retry_at:
                        mark_onedrive_failed(invoice_id)
                        logger.warning(
                            "OneDrive retry: invoice %s failed again — marked onedrive_failed=TRUE",
                            invoice_id,
                        )
                    else:
                        mark_onedrive_retry(invoice_id)
                        logger.info(
                            "OneDrive retry: invoice %s first failure recorded — will retry next hour",
                            invoice_id,
                        )

            except Exception as exc:
                logger.error(
                    "OneDrive retry: error processing invoice %s: %s", invoice_id, exc
                )

    except Exception as exc:
        logger.error("OneDrive retry scheduler run error: %s", exc)


def start_onedrive_scheduler():
    global _scheduler
    if not _is_scheduler_process():
        logger.info(
            "OneDrive scheduler skipped in this process "
            "(ONEDRIVE_SCHEDULER_ENABLED not set for this worker)."
        )
        return
    with _scheduler_lock:
        if _scheduler is not None:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            _scheduler = BackgroundScheduler(timezone="Europe/Lisbon")
            _scheduler.add_job(
                _run_onedrive_retry,
                CronTrigger(minute="15", timezone="Europe/Lisbon"),
                id="onedrive_retry",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            _scheduler.start()
            logger.info("OneDrive scheduler started (hourly retry at :15)")

            t = threading.Thread(
                target=_run_onedrive_retry, daemon=True, name="onedrive-init"
            )
            t.start()
            logger.info("OneDrive scheduler: triggered immediate retry on startup")

        except Exception as exc:
            logger.error("Failed to start OneDrive scheduler: %s", exc)


def stop_onedrive_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
                logger.info("OneDrive scheduler stopped.")
            except Exception:
                pass
            _scheduler = None
