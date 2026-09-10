import os
import logging
import requests
import threading
import time
import os
from datetime import datetime, date

logger = logging.getLogger(__name__)

SPREADSHEET_ID = "18uOVTPh74uz47zJTw9hCeTikNZDarm0hQUDmhrQMTgk"
SHEET_RANGE = "A:Z"

CONNECTORS_HOSTNAME = os.environ.get("REPLIT_CONNECTORS_HOSTNAME", "connectors.replit.com")
REPL_IDENTITY = os.environ.get("REPL_IDENTITY")
WEB_REPL_RENEWAL = os.environ.get("WEB_REPL_RENEWAL")


def _get_replit_token():
    if REPL_IDENTITY:
        return "repl " + REPL_IDENTITY
    if WEB_REPL_RENEWAL:
        return "depl " + WEB_REPL_RENEWAL
    return None


def _get_access_token():
    token = _get_replit_token()
    if not token:
        raise RuntimeError("No Replit identity token available")
    url = f"https://{CONNECTORS_HOSTNAME}/api/v2/connection?include_secrets=true&connector_names=google-sheet"
    resp = requests.get(url, headers={"Accept": "application/json", "X-Replit-Token": token}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    if not items:
        raise RuntimeError("Google Sheets not connected")
    settings = items[0].get("settings", {})
    access_token = settings.get("access_token") or (settings.get("oauth") or {}).get("credentials", {}).get("access_token")
    if not access_token:
        raise RuntimeError("No access token in Google Sheets connection")
    return access_token


def fetch_sheet_rows():
    access_token = _get_access_token()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{SHEET_RANGE}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("values", [])


_DATETIME_FMTS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
)

_DATE_ONLY_FMTS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
)


def _parse_date(val):
    if not val:
        return None
    v = val.strip()
    for fmt in _DATE_ONLY_FMTS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    for fmt in _DATETIME_FMTS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    return None


def _parse_datetime(val):
    """Parse a value as a full datetime (preserving time); falls back to date-only."""
    if not val:
        return None
    v = val.strip()
    for fmt in _DATETIME_FMTS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            pass
    for fmt in _DATE_ONLY_FMTS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            pass
    return None


def _parse_guests(val):
    if not val:
        return None, val
    raw = str(val).strip()
    import re
    m = re.search(r'\d+', raw)
    if m:
        return int(m.group()), raw
    return None, raw


def _normalize_bool(val):
    if not val:
        return False
    return str(val).strip().lower() in ("sim", "yes", "true", "1", "s", "y")


def _safe_col(row, idx, default=""):
    try:
        v = row[idx]
        return v.strip() if isinstance(v, str) else str(v)
    except (IndexError, AttributeError):
        return default


def _parse_orcamento(val):
    """Parse orçamento value to float. Returns None if empty/invalid."""
    if not val:
        return None
    import re
    v = str(val).strip().replace(',', '.').replace('€', '').replace(' ', '')
    m = re.search(r'[\d.]+', v)
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return None


def _map_sheet_status(raw_estado):
    """Map the 'Estado' column from Google Sheets to pipeline status codes."""
    if not raw_estado:
        return 'lead'
    v = raw_estado.strip().lower()
    if 'aceite' in v:
        return 'won'
    if 'realizado' in v:
        return 'won'
    if 'enviado' in v:
        return 'proposal_sent'
    if 'rejeitado' in v:
        return 'lost'
    if 'andamento' in v or 'progresso' in v:
        return 'negotiating'
    if 'incompativel' in v or 'incompatível' in v:
        return 'cancelled'
    if 'cancelado' in v:
        return 'cancelled'
    if 'contactado' in v or 'contacted' in v:
        return 'contacted'
    return 'lead'


def _map_row_to_lead(row_index, row):
    """
    Map a sheet row to a lead dict. Column order is flexible — we try to detect
    headers on row 0, but also support a fixed positional fallback.
    """
    guests, guests_raw = _parse_guests(_safe_col(row, 4))
    return {
        "google_sheet_row_id": str(row_index),
        "submitted_at": _parse_datetime(_safe_col(row, 0)) or None,
        "client_name": _safe_col(row, 1),
        "client_email": _safe_col(row, 2),
        "client_phone": _safe_col(row, 3),
        "estimated_guests_raw": guests_raw,
        "estimated_guests": guests,
        "event_date": _parse_date(_safe_col(row, 5)),
        "event_time": _safe_col(row, 6),
        "event_end_time": None,
        "event_type": _safe_col(row, 7),
        "customer_type": None,
        "company_name": None,
        "nif": None,
        "venue": _safe_col(row, 8),
        "venue_address": _safe_col(row, 9),
        "referral_source": _safe_col(row, 10),
        "marketing_consent": _normalize_bool(_safe_col(row, 11)),
        "notes": _safe_col(row, 12),
        "sheet_status": None,
        "orcamento": None,
    }


def _map_row_with_headers(headers, row, row_index):
    """Map a row using detected header names."""
    h = {str(hdr).strip().lower(): i for i, hdr in enumerate(headers)}

    def col(keys, default=""):
        for k in keys:
            if k in h and h[k] < len(row):
                v = row[h[k]]
                return v.strip() if isinstance(v, str) else str(v)
        return default

    guests_raw = col([
        "número estimado de participantes", "numero estimado de participantes",
        "convidados", "guests", "nr convidados", "nº convidados", "numero convidados",
    ])
    guests, guests_raw = _parse_guests(guests_raw) if guests_raw else (None, guests_raw)

    submitted_raw = col(["timestamp", "data envio", "data de envio", "submitted_at", "data"])
    event_date_raw = col([
        "data de realização do evento", "data de realizacao do evento",
        "data do evento", "data evento", "event_date", "event date",
    ])
    mc = col([
        "autorizo a nivà a contactar-me para eventos/ campanhas futuras?",
        "autorizo a niva a contactar-me para eventos/ campanhas futuras?",
        "autorizo", "consentimento marketing", "marketing_consent", "marketing", "newsletter",
    ])
    estado_raw = col(["estado", "status", "estado do evento", "estado lead"])
    orcamento_raw = col(["orçamento", "orcamento", "budget", "valor", "preco", "preço"])

    return {
        "google_sheet_row_id": str(row_index),
        "submitted_at": _parse_datetime(submitted_raw) or (datetime.now() if not submitted_raw else None),
        "client_name": col(["nome de contacto", "nome", "name", "client_name", "nome completo"]),
        "client_email": col(["contacto - email", "email", "e-mail", "client_email"]),
        "client_phone": col(["contacto - telefone", "telefone", "phone", "telemovel", "telemóvel", "client_phone"]),
        "estimated_guests_raw": guests_raw,
        "estimated_guests": guests,
        "event_date": _parse_date(event_date_raw),
        "event_time": col([
            "horário do evento", "horario do evento",
            "horário do evento (hora início e fim)", "horario do evento (hora inicio e fim)",
            "hora", "hora inicio", "hora do evento", "event_time", "time",
        ]),
        "event_end_time": col(["hora fim", "hora de fim", "event_end_time", "end_time"]) or None,
        "event_type": col(["tipo de evento", "tipo evento", "event_type", "type"]),
        "customer_type": (
            {"particular": "particular", "empresa": "empresa"}.get(
                col(["tipo de cliente", "customer type", "customer_type"]).strip().casefold()
            )
        ),
        "company_name": col(["empresa", "company", "company_name"]) or None,
        "nif": col(["nif", "nif/vat", "vat", "nif vat"]) or None,
        "venue": col(["localização", "localizacao", "local", "venue", "local do evento"]),
        "venue_address": col(["morada", "address", "venue_address"]),
        "referral_source": col([
            "como conheceu a nivà?", "como conheceu a niva?",
            "como conheceu a nivà", "como conheceu a niva",
            "como nos conheceu", "referral", "fonte", "referral_source",
        ]),
        "marketing_consent": _normalize_bool(mc),
        "notes": col(["notas", "notes", "observacoes", "observações", "mensagem", "message"]),
        "sheet_status": estado_raw,
        "orcamento": _parse_orcamento(orcamento_raw),
    }


def sync_leads_from_sheet(progress_callback=None):
    """
    Pull all rows from the Google Sheet, upsert into lead_requests AND events (pipeline).
    Returns (inserted, updated, errors) counts (based on lead_requests inserts/updates).
    """
    import database as db_mod

    started = time.monotonic()
    timings = {}
    logger.info("sheet_sync phase=fetch_start")
    try:
        rows = fetch_sheet_rows()
    except Exception as e:
        # A connector/auth failure must make the durable run fail, rather than
        # looking like a successful import with one bad row.
        logger.error("sheet_sync phase=fetch_failed error_type=%s", type(e).__name__)
        raise

    timings["fetch_ms"] = int((time.monotonic() - started) * 1000)
    logger.info("sheet_sync phase=fetch_complete elapsed_ms=%d rows=%d",
                timings["fetch_ms"], len(rows))
    if progress_callback:
        progress_callback({"phase": "mapping", "total_rows": len(rows), "processed_rows": 0})

    if not rows:
        return 0, 0, 0

    inserted = updated = errors = 0
    has_headers = False
    headers = []

    first_row = [c.strip().lower() if isinstance(c, str) else "" for c in rows[0]]
    header_keywords = {"nome", "name", "email", "timestamp", "data", "telefone",
                       "convidados", "numero estimado de participantes",
                       "número estimado de participantes"}
    if header_keywords & set(first_row):
        has_headers = True
        headers = rows[0]
        data_rows = rows[1:]
        row_offset = 2
    else:
        data_rows = rows
        row_offset = 1

    # Parse once and upsert leads in one transaction; event work below retains
    # the established row mapping and history/protected-status behavior.
    mapping_started = time.monotonic()
    batch_items = []
    for i, row in enumerate(data_rows):
        if not row or all(c == "" for c in row):
            continue
        row_id = str(i + row_offset)
        mapped = (_map_row_with_headers(headers, row, row_id)
                  if has_headers else _map_row_to_lead(row_id, row))
        if not mapped.get("client_name") and not mapped.get("client_email"):
            continue
        mapped.pop("sheet_status", None)
        mapped.pop("orcamento", None)
        batch_items.append((row_id, mapped))
    timings["mapping_ms"] = int((time.monotonic() - mapping_started) * 1000)
    logger.info("sheet_sync phase=mapping_complete elapsed_ms=%d rows=%d",
                timings["mapping_ms"], len(batch_items))
    if progress_callback:
        progress_callback({
            "phase": "importing_leads", "total_rows": len(batch_items),
            "processed_rows": 0, "timings": timings, "force_heartbeat": True,
        })

    lead_started = time.monotonic()
    batch_results = {}
    failed_lead_rows = set()
    batch_upsert = getattr(db_mod, "upsert_leads_from_sheet_batch", None)
    if batch_upsert:
        for offset in range(0, len(batch_items), 100):
            chunk = batch_items[offset:offset + 100]
            if progress_callback:
                progress_callback({
                    "phase": "importing_leads", "total_rows": len(batch_items),
                    "processed_rows": offset, "timings": timings,
                    "force_heartbeat": True,
                })
            try:
                results = batch_upsert(chunk)
                batch_results.update(zip((item[0] for item in chunk), results))
            except Exception:
                logger.warning(
                    "sheet_sync phase=lead_batch_fallback offset=%d size=%d",
                    offset, len(chunk),
                )
                for row_id, lead_data in chunk:
                    if progress_callback:
                        progress_callback({
                            "phase": "importing_leads",
                            "total_rows": len(batch_items),
                            "processed_rows": offset,
                            "timings": timings,
                            "force_heartbeat": True,
                        })
                    try:
                        batch_results[row_id] = db_mod.upsert_lead_from_sheet(
                            row_id, lead_data.copy())
                    except Exception as exc:
                        failed_lead_rows.add(row_id)
                        logger.error(
                            "sheet_sync phase=lead_row_failed row_index=%s error_type=%s",
                            row_id, type(exc).__name__,
                        )
    timings["lead_upsert_ms"] = int((time.monotonic() - lead_started) * 1000)
    logger.info("sheet_sync phase=lead_upsert_complete elapsed_ms=%d rows=%d",
                timings["lead_upsert_ms"], len(batch_items))

    events_started = time.monotonic()
    for i, row in enumerate(data_rows):
        if not row or all(c == "" for c in row):
            continue
        row_id = str(i + row_offset)
        if progress_callback:
            # Lease renewal is deliberately outside the per-row error handler:
            # ownership loss must stop the old worker, not be counted as bad data.
            progress_callback({
                "phase": "importing_events", "total_rows": len(batch_items),
                "processed_rows": min(i, len(batch_items)),
                "inserted": inserted, "updated": updated, "errors": errors,
                "timings": timings, "force_heartbeat": True,
            })
        try:
            if has_headers:
                lead_data = _map_row_with_headers(headers, row, row_id)
            else:
                lead_data = _map_row_to_lead(row_id, row)

            if not lead_data.get("client_name") and not lead_data.get("client_email"):
                continue

            sheet_status_raw = lead_data.pop("sheet_status", None)
            orcamento = lead_data.pop("orcamento", None)
            pipeline_status = _map_sheet_status(sheet_status_raw)

            if row_id in failed_lead_rows:
                errors += 1
                continue
            lead_result = batch_results.get(row_id)
            if lead_result is None:
                _, is_new = db_mod.upsert_lead_from_sheet(row_id, lead_data.copy())
            else:
                _, is_new = lead_result
            if is_new:
                inserted += 1
            else:
                updated += 1

            event_data = {
                "event_name": lead_data.get("client_name") or "",
                "event_type": lead_data.get("event_type") or "",
                "event_date": lead_data.get("event_date"),
                "event_time": lead_data.get("event_time") or "",
                "event_end_time": lead_data.get("event_end_time"),
                "estimated_guests": lead_data.get("estimated_guests"),
                "venue": lead_data.get("venue") or "",
                "venue_address": lead_data.get("venue_address") or "",
                "client_name": lead_data.get("client_name") or "",
                "client_email": lead_data.get("client_email") or "",
                "client_phone": lead_data.get("client_phone") or "",
                "customer_type": lead_data.get("customer_type"),
                "company_name": lead_data.get("company_name"),
                "nif": lead_data.get("nif"),
                "orcamento": orcamento,
            }
            db_mod.upsert_event_from_sheet(row_id, event_data, pipeline_status)

        except Exception as e:
            logger.error("sheet_sync phase=row_failed row_index=%s error_type=%s",
                         row_id, type(e).__name__)
            errors += 1
        if progress_callback:
            progress_callback({
                "phase": "importing", "total_rows": len(data_rows),
                "processed_rows": i + 1, "inserted": inserted,
                "updated": updated, "errors": errors, "timings": timings,
            })

    timings["event_upsert_ms"] = int((time.monotonic() - events_started) * 1000)
    timings["total_ms"] = int((time.monotonic() - started) * 1000)
    logger.info(
        "sheet_sync phase=complete elapsed_ms=%d fetch_ms=%d mapping_ms=%d "
        "lead_upsert_ms=%d event_upsert_ms=%d processed=%d errors=%d",
        timings["total_ms"], timings["fetch_ms"], timings["mapping_ms"],
        timings["lead_upsert_ms"], timings["event_upsert_ms"],
        len(data_rows), errors,
    )
    if progress_callback:
        progress_callback({
            "phase": "complete", "total_rows": len(batch_items),
            "processed_rows": len(batch_items), "inserted": inserted,
            "updated": updated, "errors": errors, "timings": timings,
        })
    return inserted, updated, errors


_worker_lock = threading.Lock()
_worker_thread = None
_worker_stop = threading.Event()


def enqueue_sheet_sync(requested_by=None, trigger='manual',
                       idempotency_key=None):
    """Create (or return) the single active persistent import job."""
    import database as db_mod
    job_id = db_mod.enqueue_event_sheet_sync(trigger_source=trigger,
                                             requester=requested_by,
                                             idempotency_key=idempotency_key)
    return db_mod.get_event_sheet_sync_run(job_id)


def get_sheet_sync_status(job_id=None):
    import database as db_mod
    if job_id is None:
        return db_mod.get_latest_event_sheet_sync_run()
    return db_mod.get_event_sheet_sync_run(job_id)


def run_pending_sheet_sync_once():
    """Claim and process one job; intended for workers and deterministic tests."""
    import database as db_mod
    worker_id = f"{os.getpid()}:{threading.get_ident()}"
    job = db_mod.claim_event_sheet_sync(worker=worker_id)
    if not job:
        return None
    job_id = job["id"]
    lease_token = job["lease_token"]
    last_update = [0.0]

    def progress(info):
        now = time.monotonic()
        force_heartbeat = info.pop("force_heartbeat", False)
        # Progress is durable, but do not turn a large sheet into a DB
        # write-amplifier. Always persist completion and occasional heartbeats.
        if (not force_heartbeat and now - last_update[0] < 1.0 and
                info.get("processed_rows", 0) < info.get("total_rows", 0)):
            return
        last_update[0] = now
        updated = db_mod.update_event_sheet_sync_run(
            job_id, lease_token=lease_token, **info)
        if updated is None:
            raise RuntimeError("sheet sync lease lost")

    try:
        inserted, updated, errors = sync_leads_from_sheet(progress_callback=progress)
        completed = db_mod.update_event_sheet_sync_run(
            job_id, status='succeeded', phase='complete',
            inserted=inserted,
            updated=updated, errors=errors, lease_token=lease_token,
        )
        if completed is None:
            raise RuntimeError("sheet sync lease lost")
        # The final callback may have been rate-limited; the import completed,
        # so its total is the authoritative processed count.
        current = db_mod.get_event_sheet_sync_run(job_id) or {}
        if current.get("total_rows"):
            db_mod.update_event_sheet_sync_run(
                job_id, processed_rows=current["total_rows"])
        return db_mod.get_event_sheet_sync_run(job_id)
    except Exception as exc:
        db_mod.update_event_sheet_sync_run(
            job_id, status='failed', phase='failed',
            # Connector exceptions can contain URLs, account identifiers, or
            # provider response bodies.  Keep the public summary non-sensitive.
            error_summary=f"{type(exc).__name__}", lease_token=lease_token)
        return db_mod.get_event_sheet_sync_run(job_id)


def _sheet_worker(schedule_daily):
    last_daily = None
    while not _worker_stop.is_set():
        try:
            if schedule_daily and datetime.now().hour == 8:
                today = datetime.now().date()
                if last_daily != today:
                    daily_key = f"daily:{today.isoformat()}"
                    job = enqueue_sheet_sync(
                        trigger='daily',
                        idempotency_key=daily_key,
                    )
                    # An active manual run can temporarily prevent this insert.
                    # Retry until this exact daily job exists.
                    if job and job.get("idempotency_key") == daily_key:
                        last_daily = today
            run_pending_sheet_sync_once()
        except Exception:
            logger.exception("sheet_sync worker iteration failed")
        _worker_stop.wait(5.0)


def start_sheet_sync_worker(schedule_daily=False):
    """Start one daemon importer thread; repeated calls are harmless."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return _worker_thread
        _worker_stop.clear()
        _worker_thread = threading.Thread(
            target=_sheet_worker, args=(schedule_daily,),
            name="sheet-sync-worker", daemon=True,
        )
        _worker_thread.start()
        return _worker_thread


def stop_sheet_sync_worker(timeout=2.0):
    """Stop this process's importer thread during a graceful worker exit."""
    global _worker_thread
    with _worker_lock:
        thread = _worker_thread
        if not thread:
            return
        _worker_stop.set()
    if thread.is_alive():
        thread.join(timeout=timeout)
    with _worker_lock:
        if _worker_thread is thread:
            _worker_thread = None
