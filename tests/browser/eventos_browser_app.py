"""Deterministic Flask server used only by the Eventos browser specs."""

from datetime import date
from pathlib import Path

from flask import Blueprint, Flask, jsonify, redirect, session, url_for
from flask_app.routes import eventos as eventos_routes


STATE = {
    "preferences": {},
    "historical_occurrence": {
        "id": 9,
        "event_id": 1,
        "venue": "Quinta da Serra",
        "venue_address": "",
        "venue_id": None,
    },
    "discard_occurrence": {
        "id": 10,
        "event_id": 2,
        "venue": "",
        "venue_address": "Estrada sem número",
        "venue_id": None,
        "dismissed": False,
    },
}

EVENTS = [
    {
        "id": 1,
        "event_name": "Festa Grande",
        "client_name": "Ana Silva",
        "client_email": "ana@example.test",
        "client_phone": "910000001",
        "event_date": date(2026, 6, 20),
        "event_time": "18:00",
        "event_type": "Casamento",
        "venue": "Quinta da Serra",
        "venue_address": "Rua Principal 1",
        "estimated_guests": 120,
        "source": "manual",
        "status": "novos",
        "quote_total": 500,
    },
    {
        "id": 2,
        "event_name": "Festa Pequena",
        "client_name": "Bruno Costa",
        "client_email": "bruno@example.test",
        "client_phone": "910000002",
        "event_date": date(2026, 6, 10),
        "event_time": "12:00",
        "event_type": "Aniversário",
        "venue": "Casa do Lago",
        "venue_address": "Avenida do Lago 2",
        "estimated_guests": 20,
        "source": "portal",
        "status": "orcamentado",
        "quote_total": 100,
    },
]

VENUE = {
    "id": 3,
    "name": "Quinta da Serra",
    "address": "Rua Principal 1",
    "contact_name": "Maria Local",
    "contact_phone": "910000003",
    "contact_email": "maria@quinta.test",
    "logistics_notes": "",
    "event_count": 1,
    "last_event_date": date(2026, 6, 20),
    "latitude": 38.72,
    "longitude": -9.14,
    "geocode_provider": "nominatim",
    "geocode_failed": False,
}


def _events_for_pipeline(**filters):
    events = list(EVENTS)
    search = (filters.get("search") or "").lower()
    if search:
        events = [
            event for event in events
            if search in " ".join(
                str(event.get(field) or "")
                for field in ("event_name", "client_name", "client_email")
            ).lower()
        ]
    key = {
        "event": "event_name",
        "client": "client_name",
        "date": "event_date",
        "type": "event_type",
        "venue": "venue",
        "guests": "estimated_guests",
        "source": "source",
        "status": "status",
        "budget": "quote_total",
        "email": "client_email",
        "phone": "client_phone",
    }.get(filters.get("sort_by"), "event_date")
    return sorted(
        events,
        key=lambda event: event.get(key) or "",
        reverse=filters.get("sort_direction") == "desc",
    )


def _incomplete_occurrences():
    occurrence = STATE["historical_occurrence"]
    discarded = STATE["discard_occurrence"]
    result = []
    if occurrence["venue_id"] is None:
        result.append({
            **occurrence, "event_name": "Festa Grande", "client_name": "Ana Silva",
            "event_date": date(2026, 6, 20), "candidate_venues": [VENUE],
        })
    if discarded["venue_id"] is None and not discarded["dismissed"]:
        result.append({
            **discarded, "event_name": "Festa Pequena", "client_name": "Bruno Costa",
            "event_date": date(2026, 6, 10), "candidate_venues": [],
        })
    return result


def _link_incomplete_occurrence(occurrence_id, venue_id, actor):
    occurrence = STATE["historical_occurrence"]
    if occurrence_id != occurrence["id"] or venue_id != VENUE["id"]:
        raise ValueError("A ocorrência não está disponível para associação.")
    occurrence["venue_id"] = venue_id
    occurrence["actor"] = actor


def _dismiss_incomplete_occurrence(occurrence_id, actor):
    occurrence = STATE["discard_occurrence"]
    if occurrence_id != occurrence["id"] or occurrence["dismissed"]:
        raise ValueError("A ocorrência já foi tratada ou descartada.")
    occurrence["dismissed"] = True
    occurrence["actor"] = actor


def _configure_eventos_data():
    """Replace database calls with the small data set needed for UI interaction."""
    eventos_routes.db.get_events = _events_for_pipeline
    eventos_routes.db.get_event_resources = lambda: []
    eventos_routes.db.get_pipeline_view_preferences = (
        lambda user_key: STATE["preferences"].get(user_key)
    )
    eventos_routes.db.save_pipeline_view_preferences = (
        lambda user_key, columns: STATE["preferences"].__setitem__(user_key, columns)
    )
    eventos_routes.db.get_event = lambda event_id: next(
        (event for event in EVENTS if event["id"] == event_id), None
    )
    eventos_routes.db.get_quote_items = lambda _event_id: [
        {"descricao": "Serviço de catering", "total": 500}
    ]
    eventos_routes.db.get_event_quote_totals = lambda _event_id: {
        "total_net": 400, "total_vat": 100,
    }
    eventos_routes.db.get_event_history = lambda _event_id: []
    eventos_routes.db.get_event_primary_venue = lambda _event_id: VENUE
    eventos_routes.db.get_event_venues = lambda _search=None: [VENUE]
    eventos_routes.db.get_incomplete_venue_occurrences = _incomplete_occurrences
    eventos_routes.db.link_incomplete_event_occurrence_to_venue = (
        _link_incomplete_occurrence
    )
    eventos_routes.db.dismiss_incomplete_event_occurrence = (
        _dismiss_incomplete_occurrence
    )
    eventos_routes._get_tabs = lambda: []

    # The pipeline handler imports this function when a page is rendered.
    from flask_app import google_sheets_sync
    google_sheets_sync.get_sheet_sync_status = lambda: None


def create_test_app():
    _configure_eventos_data()
    project_root = Path(__file__).resolve().parents[2]
    app = Flask(
        __name__,
        template_folder=str(project_root / "flask_app" / "templates"),
        static_folder=str(project_root / "flask_app" / "static"),
    )
    app.secret_key = "eventos-browser-test"
    app.config["TESTING"] = True

    home = Blueprint("home", __name__)

    @home.get("/")
    def index():
        return "Início"

    auth = Blueprint("auth", __name__)

    @auth.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home.index"))

    app.register_blueprint(home)
    app.register_blueprint(auth, url_prefix="/auth")

    # This route deliberately precedes the blueprint's full editor route. It lets
    # the browser spec follow the real panel link without requiring unrelated
    # event-editor data fixtures.
    @app.get("/eventos/evento/<int:event_id>")
    def test_budget_editor(event_id):
        return f"<h1>Editar orçamento do evento {event_id}</h1>"

    app.register_blueprint(eventos_routes.eventos_bp, url_prefix="/eventos")

    @app.context_processor
    def inject_user():
        return {"user": session.get("user"), "nav_pages": []}

    @app.get("/test-login")
    def test_login():
        user_id = request_user_id()
        session["user"] = {
            "id": user_id,
            "username": f"browser-{user_id}",
            "acesso_eventos": True,
        }
        return redirect(url_for("eventos.pipeline"))

    @app.get("/_health")
    def health():
        return "ok"

    @app.get("/_test-state")
    def test_state():
        occurrence = STATE["historical_occurrence"]
        return jsonify({
            "preferences": STATE["preferences"],
            "historical_occurrence": occurrence,
            "discard_occurrence": STATE["discard_occurrence"],
        })

    return app


def request_user_id():
    from flask import request
    return int(request.args.get("user", "7"))


if __name__ == "__main__":
    create_test_app().run(host="127.0.0.1", port=8765, use_reloader=False)