"""Deterministic Flask server used only by the Eventos browser specs."""

from datetime import date
from pathlib import Path

import pandas as pd
from flask import Blueprint, Flask, jsonify, redirect, session, url_for
from flask_app.routes import eventos as eventos_routes
from flask_app.routes import eurokg as eurokg_routes
from flask_app.routes import gestor as gestor_routes


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

GESTOR_STORES = [
    {
        "id": 1,
        "name": "Bolhão",
        "store_type": "loja",
        "requires_eod_weighing": True,
        "is_active": True,
        "supports_vendas": True,
    },
    {
        "id": 2,
        "name": "Matosinhos",
        "store_type": "loja",
        "requires_eod_weighing": True,
        "is_active": True,
        "supports_vendas": True,
    },
]
GESTOR_TILE_STATE = {}

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
    eventos_routes.db.get_event_venue_geocode_status = lambda: {
        "validated": 1, "pending": 0, "failed": 0, "total": 1,
    }
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


def _configure_eurokg_data():
    """Replace Euro/kg database calls with a deterministic grouped table."""
    stores = [
        {"id": 1, "name": "Bolhão", "store_type": "loja"},
        {"id": 2, "name": "Matosinhos", "store_type": "loja"},
    ]

    def consumo_data(loja, **_kwargs):
        quantities = {
            "Bolhão": (4, 3),
            "Matosinhos": (5, 2),
        }
        chocolate_qty, morango_qty = quantities.get(loja, (4, 3))
        return pd.DataFrame([
            {
                "produto": "Palito Chocolate",
                "mes": "2026-01",
                "quantidade_vendida": chocolate_qty,
                "gramas_por_unidade": 100,
                "consumo_kg": chocolate_qty / 10,
                "consumo_incompleto": False,
            },
            {
                "produto": "Palito Morango",
                "mes": "2026-01",
                "quantidade_vendida": morango_qty,
                "gramas_por_unidade": 100,
                "consumo_kg": morango_qty / 10,
                "consumo_incompleto": False,
            },
        ])

    eurokg_routes.get_consumo_gelado_mensal = consumo_data
    eurokg_routes.get_historical_dose_coverage = (
        lambda *_args, **_kwargs: ([], [])
    )
    eurokg_routes.get_historical_dose_preview = (
        lambda _preview_id, _actor: None
    )
    eurokg_routes.get_dose_product_configuration_queue = lambda: ([], [])
    eurokg_routes._build_tabs = lambda _loja, _is_gestor, _active: []

    from db import auth as auth_db
    auth_db.get_vendas_module_stores = lambda: stores
    auth_db.get_store_by_id = lambda _store_id: None


def _configure_gestor_data():
    """Replace tile configuration persistence with isolated in-memory state."""
    from db import tiles as tiles_db
    import database

    GESTOR_TILE_STATE.clear()
    for store in GESTOR_STORES:
        GESTOR_TILE_STATE[store["id"]] = {}

    def seed_store_tile_config(store_id, tiles):
        store_state = GESTOR_TILE_STATE.setdefault(store_id, {})
        for tile in tiles:
            store_state.setdefault(tile["id"], {
                "label": tile.get("label", ""),
                "visible": True,
                "icon": "",
            })

    def get_store_tile_config(store_id):
        return {
            tile_id: dict(config)
            for tile_id, config in GESTOR_TILE_STATE.get(store_id, {}).items()
        }

    def update_store_tile(store_id, tile_id, **changes):
        store_state = GESTOR_TILE_STATE.setdefault(store_id, {})
        config = store_state.setdefault(tile_id, {
            "label": "",
            "visible": True,
            "icon": "",
        })
        for key, value in changes.items():
            if value is not None:
                config[key] = value

    def set_tile_visibility(module, tile_id, visible, label="", store_id=None):
        assert module == "vendas"
        assert store_id is not None
        update_store_tile(store_id, tile_id, visible=visible, label=label or None)

    def set_tile_label(module, tile_id, label, store_id=None):
        assert module == "vendas"
        assert store_id is not None
        update_store_tile(store_id, tile_id, label=label.strip())

    def set_tile_icon(module, tile_id, icon, store_id=None):
        assert module == "vendas"
        assert store_id is not None
        update_store_tile(store_id, tile_id, icon=icon.strip())

    tiles_db.get_all_tile_config = lambda: []
    tiles_db.get_module_labels = lambda: {}
    tiles_db.get_store_tile_config = get_store_tile_config
    tiles_db.seed_store_tile_config = seed_store_tile_config
    tiles_db.set_tile_visibility = set_tile_visibility
    tiles_db.set_tile_label = set_tile_label
    tiles_db.set_tile_icon = set_tile_icon
    database.get_vendas_module_stores = lambda: list(GESTOR_STORES)
    database.get_store_by_id = lambda store_id: next(
        (store for store in GESTOR_STORES if store["id"] == store_id),
        None,
    )


def create_test_app():
    _configure_eventos_data()
    _configure_eurokg_data()
    _configure_gestor_data()
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
    app.register_blueprint(eurokg_routes.eurokg_bp, url_prefix="/eurokg")
    app.register_blueprint(gestor_routes.gestor_bp, url_prefix="/gestor")

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
            "acesso_eurokg": True,
            "acesso_gestor": True,
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