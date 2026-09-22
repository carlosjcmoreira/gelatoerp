import unittest
from datetime import date
from unittest.mock import patch

from flask import Blueprint, Flask, session


class TestEncomendasSemanaisRules(unittest.TestCase):
    def test_cycle_is_sunday_to_following_monday(self):
        from db.encomendas_semanais import weekly_cycle_dates

        cycle = weekly_cycle_dates(date(2026, 9, 27))
        self.assertEqual(cycle["ciclo_domingo"], date(2026, 9, 27))
        self.assertEqual(cycle["entrega_prevista"], date(2026, 9, 28))

    def test_non_sunday_cycle_is_rejected(self):
        from db.encomendas_semanais import normalise_planning_sunday

        with self.assertRaisesRegex(ValueError, "domingo"):
            normalise_planning_sunday("2026-09-28")

    def test_next_cycle_keeps_current_sunday(self):
        from db.encomendas_semanais import next_planning_sunday

        self.assertEqual(
            next_planning_sunday(date(2026, 9, 27)),
            date(2026, 9, 27),
        )
        self.assertEqual(
            next_planning_sunday(date(2026, 9, 28)),
            date(2026, 10, 4),
        )

    def test_status_contract_does_not_allow_reopening(self):
        from db.encomendas_semanais import _ALLOWED_TRANSITIONS

        self.assertEqual(_ALLOWED_TRANSITIONS["rascunho"], {"submetida", "cancelada"})
        self.assertEqual(_ALLOWED_TRANSITIONS["submetida"], {"em_preparacao", "cancelada"})
        self.assertEqual(_ALLOWED_TRANSITIONS["em_preparacao"], {"concluida", "cancelada"})
        self.assertEqual(_ALLOWED_TRANSITIONS["concluida"], set())
        self.assertEqual(_ALLOWED_TRANSITIONS["cancelada"], set())

    def test_quantity_bounds_are_strict(self):
        from db.encomendas_semanais import _decimal_quantity

        self.assertEqual(str(_decimal_quantity("2,345")), "2.345")
        with self.assertRaises(ValueError):
            _decimal_quantity("0")
        with self.assertRaises(ValueError):
            _decimal_quantity("1000001")


class TestComprasSemanaisRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flask_app.routes.compras import compras_bp

        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.config["TESTING"] = True
        auth_bp = Blueprint("auth", __name__)
        home_bp = Blueprint("home", __name__)

        @auth_bp.route("/login")
        def login():
            return "login", 200

        @home_bp.route("/")
        def index():
            return "home", 200

        app.register_blueprint(auth_bp)
        app.register_blueprint(home_bp)
        app.register_blueprint(compras_bp, url_prefix="/compras")
        cls.app = app

    def test_manager_queue_passes_cycle_and_consolidation_to_template(self):
        from db import encomendas_semanais

        with self.app.test_client() as client:
            with client.session_transaction() as current:
                current["user"] = {
                    "acesso_gestor": True,
                    "acesso_compras": False,
                }
            with patch.object(
                encomendas_semanais,
                "list_weekly_orders",
                return_value=[{"id": 7, "status": "submetida"}],
            ), patch.object(
                encomendas_semanais,
                "get_weekly_consolidation",
                return_value=[{"produto_snapshot": "Farinha"}],
            ), patch(
                "flask_app.routes.compras.render_template",
                return_value="ok",
            ) as render:
                response = client.get("/compras/encomendas-semanais?ciclo=2026-09-27")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.kwargs["orders"][0]["id"], 7)
        self.assertEqual(
            render.call_args.kwargs["weekly_cycle"]["entrega_prevista"],
            date(2026, 9, 28),
        )

    def test_non_sunday_manager_cycle_is_redirected_without_querying_data(self):
        with self.app.test_client() as client:
            with client.session_transaction() as current:
                current["user"] = {"acesso_gestor": True}
            with patch(
                "flask_app.routes.compras.render_template",
                return_value="ok",
            ) as render:
                response = client.get("/compras/encomendas-semanais?ciclo=2026-09-28")

        self.assertEqual(response.status_code, 302)
        self.assertIn("ciclo=2026-09-27", response.location)
        render.assert_not_called()
