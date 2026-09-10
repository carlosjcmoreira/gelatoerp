"""Privacy-safe custom analytics event queue for server-confirmed outcomes."""
import logging

from flask import session


logger = logging.getLogger(__name__)
_SESSION_KEY = "_analytics_events"
_EVENT_SCHEMAS = {
    "event_request_submitted": {
        "occurrence_count": ("int", 1, 20),
        "catering_requested": ("bool",),
    },
    "event_quote_accepted": {},
    "event_deposit_proof_uploaded": {
        "file_type": ("enum", {"pdf", "image"}),
    },
    "event_quote_version_saved": {},
    "production_transfer_created": {
        "destination_type": ("enum", {"loja", "b2b"}),
        "order_count": ("int", 1, 100),
    },
    "stock_count_recorded": {
        "location": ("enum", {"Bolhão", "Matosinhos", "Garagem"}),
        "line_count": ("int", 1, 1000),
    },
    "invoice_payment_scheduled": {
        "payment_method_selected": ("bool",),
    },
    "invoice_installment_plan_created": {
        "installment_count": ("int", 2, 100),
    },
    "invoice_marked_paid": {
        "payment_method_selected": ("bool",),
        "installment": ("bool",),
    },
    "invoice_installment_paid": {
        "invoice_completed": ("bool",),
    },
}


def queue_analytics_event(name: str, **data) -> bool:
    """Queue an Umami event for the next rendered page.

    Callers must only pass low-cardinality dimensions. IDs, names, emails,
    filenames, free-form text, and other personal data do not belong here.
    """
    try:
        schema = _EVENT_SCHEMAS.get(name)
        if schema is None:
            raise ValueError("unknown event")
        if set(data) != set(schema):
            raise ValueError("properties do not match schema")

        safe_data = {}
        for key, value in data.items():
            rule = schema[key]
            if rule[0] == "bool" and not isinstance(value, bool):
                raise ValueError("invalid boolean")
            if rule[0] == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("invalid count")
                if value < rule[1] or value > rule[2]:
                    raise ValueError("count out of range")
            if rule[0] == "enum" and value not in rule[1]:
                raise ValueError("invalid dimension")
            safe_data[key] = value

        events = list(session.get(_SESSION_KEY, []))
        events.append({"name": name, "data": safe_data})
        session[_SESSION_KEY] = events[-20:]
        return True
    except Exception as exc:
        # Telemetry must never change the result of an operational workflow.
        # Log no submitted values, which could contain private information.
        logger.warning("analytics_event_dropped name=%r reason=%s", name, type(exc).__name__)
        return False


def consume_analytics_events() -> list:
    """Return and remove events queued for this browser session."""
    return session.pop(_SESSION_KEY, [])