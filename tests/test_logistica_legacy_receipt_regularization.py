import unittest
from datetime import date
from unittest.mock import patch

from flask import Flask, render_template as render_flask_template

from flask_app.routes import logistica as logistica_routes
from flask_app.routes.logistica import logistica_bp


class LogisticaLegacyReceiptRegularizationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(
            __name__, template_folder="../flask_app/templates"
        )
        self.app.secret_key = "test-secret"
        self.app.add_url_rule(
            "/home", endpoint="home.index", view_func=lambda: ""
        )
        self.app.add_url_rule(
            "/login", endpoint="auth.login", view_func=lambda: ""
        )
        self.app.add_url_rule(
            "/vendas", endpoint="vendas.index", view_func=lambda: ""
        )
        self.app.register_blueprint(logistica_bp, url_prefix="/logistica")
        self.client = self.app.test_client()
        self.preview = {
            "max_order_id": 1085,
            "total_legacy": 3,
            "eligible_count": 2,
            "eligible_by_store": [{"store": "Bolhão", "count": 2}],
            "excluded_count": 1,
            "excluded_by_reason": [{"reason": "Destino B2B", "count": 1}],
            "snapshot": "a" * 64,
            "eligible_ids": [1, 2],
        }

    def _set_user(self, *, manager=True, administrative=False):
        with self.client.session_transaction() as current_session:
            current_session["user"] = {
                "username": "gestor-teste" if manager else "logistica-teste",
                "acesso_gestor": manager,
                "acesso_administrativo": administrative,
            }

    def _patch_history_data(self):
        return (
            patch(
                "flask_app.routes.logistica.get_ordens_transferencia_with_events",
                return_value={
                    "ordens": [],
                    "total": 0,
                    "page": 1,
                    "per_page": 50,
                    "total_pages": 1,
                },
            ),
            patch(
                "flask_app.routes.logistica.get_ordens_transferencia",
                return_value=[],
            ),
        )

    def test_history_receipt_filter_is_passed_and_kept_in_pagination(self):
        self._set_user(manager=False, administrative=True)
        b2b_order = {
            "id": 12,
            "data": date(2026, 9, 10),
            "data_prevista": None,
            "area_origem": "Gelado",
            "produto": "Chocolate",
            "sabor": "Chocolate",
            "quantidade": 2.0,
            "unidade": "kg",
            "loja_destino": "B2B",
            "status": "confirmada",
            "destino_tipo": "b2b",
            "destino_nome": "Cliente externo",
            "rececao_estado": "nao_aplicavel",
            "rececao_regularizada_admin": False,
            "motivo_problema": None,
            "motivo_rejeicao": None,
            "eventos": [{
                "event_type": "criado",
                "utilizador": "gestor-teste",
                "motivo": None,
                "created_at": None,
                "is_admin_regularization": False,
            }],
        }
        query = (
            "/logistica/transferencias?tab=historico&area_origem=Gelado"
            "&status=confirmada&loja_destino=B2B"
            "&rececao_estado=nao_aplicavel&data_inicio=2026-09-09"
            "&data_fim=2026-09-15&page=1"
        )
        with (
            patch(
                "flask_app.routes.logistica.get_ordens_transferencia_with_events",
                return_value={
                    "ordens": [b2b_order],
                    "total": 2,
                    "page": 1,
                    "per_page": 1,
                    "total_pages": 2,
                },
            ) as get_history,
            patch(
                "flask_app.routes.logistica.get_ordens_transferencia",
                return_value=[{
                    "loja_destino": "B2B",
                    "destino_tipo": "b2b",
                }],
            ),
            patch(
                "flask_app.routes.logistica.render_template",
                return_value="history",
            ) as render_template,
        ):
            response = self.client.get(query)

        self.assertEqual(response.status_code, 200)
        get_history.assert_called_once_with(
            status="confirmada",
            loja_destino="B2B",
            area_origem="Gelado",
            origem_registo=None,
            rececao_estado="nao_aplicavel",
            data_inicio=date(2026, 9, 9),
            data_fim=date(2026, 9, 15),
            page=1,
            per_page=50,
        )
        context = render_template.call_args.kwargs
        self.assertEqual(context["filtro_rececao"], "nao_aplicavel")
        self.assertEqual(context["total"], 2)

        with self.app.test_request_context(query):
            html = render_flask_template(
                "logistica/transferencias.html", **context
            )
        self.assertIn(
            '<option value="nao_aplicavel" selected>',
            html,
        )
        self.assertIn("B2B", html)
        self.assertIn("Não aplicável", html)
        self.assertIn("rececao_estado=nao_aplicavel", html)
        self.assertIn("area_origem=Gelado", html)
        self.assertIn("loja_destino=B2B", html)
        self.assertIn("data_inicio=2026-09-09", html)
        self.assertIn("data_fim=2026-09-15", html)
        self.assertIn("page=2", html)

    def test_active_tab_requests_only_pending_orders_and_shows_empty_copy(self):
        self._set_user(manager=False, administrative=True)
        with (
            patch(
                "flask_app.routes.logistica.get_ordens_transferencia",
                return_value=[],
            ) as get_orders,
            patch(
                "flask_app.routes.logistica.render_template",
                return_value="active",
            ) as render_template,
        ):
            response = self.client.get("/logistica/transferencias?tab=ativas")

        self.assertEqual(response.status_code, 200)
        get_orders.assert_called_once_with(status="pendente")
        context = render_template.call_args.kwargs
        self.assertEqual(context["transferencias"], [])
        with self.app.test_request_context(
            "/logistica/transferencias?tab=ativas"
        ):
            html = render_flask_template(
                "logistica/transferencias.html", **context
            )
        self.assertIn("Sem transferências pendentes.", html)
        self.assertNotIn("Sem transferências agendadas.", html)

    def test_manager_get_receives_full_server_preview_and_csrf_token(self):
        self._set_user(manager=True)
        history_order = {
            "id": 1,
            "data": date(2026, 9, 29),
            "data_prevista": date(2026, 9, 30),
            "area_origem": "Gelado",
            "produto": "Pistachio",
            "sabor": "Pistachio",
            "quantidade": 4.0,
            "unidade": "kg",
            "loja_destino": "Bolhão",
            "status": "confirmada",
            "destino_tipo": "loja",
            "destino_nome": None,
            "rececao_estado": "por_verificar",
            "motivo_problema": None,
            "motivo_rejeicao": None,
            "eventos": [{
                "event_type": "criado",
                "utilizador": "gestor-teste",
                "motivo": None,
                "created_at": None,
                "is_admin_regularization": False,
            }],
        }
        history_result = {
            "ordens": [history_order],
            "total": 2,
            "page": 1,
            "per_page": 1,
            "total_pages": 2,
        }
        with (
            patch(
                "flask_app.routes.logistica.get_ordens_transferencia_with_events",
                return_value=history_result,
            ),
            patch(
                "flask_app.routes.logistica.get_ordens_transferencia",
                return_value=[],
            ),
            patch(
                "flask_app.routes.logistica.get_legacy_transfer_receipt_preview",
                return_value=self.preview,
            ),
            patch(
                "flask_app.routes.logistica.render_template",
                return_value="history",
            ) as render_template,
        ):
            response = self.client.get(
                "/logistica/transferencias?tab=historico"
            )

        self.assertEqual(response.status_code, 200)
        context = render_template.call_args.kwargs
        self.assertEqual(
            context["legacy_receipt_preview"]["eligible_count"], 2
        )
        self.assertTrue(context["legacy_receipt_csrf"])
        with self.app.test_request_context(
            "/logistica/transferencias?tab=historico"
        ):
            html = render_flask_template(
                "logistica/transferencias.html", **context
            )
        self.assertIn("Regularizar receções antigas", html)
        self.assertIn("Bolhão: 2", html)
        self.assertIn("Destino B2B: 1", html)
        self.assertIn("Escreva 2 para confirmar", html)
        order_row_index = html.index(
            '<tr class="transfer-history-order-row"'
        )
        self.assertLess(order_row_index, html.index('<tr id="eventos-1"'))
        self.assertIn('id="mob-eventos-1"', html)
        pagination_index = html.index('class="pagination')
        regularization_index = html.index(
            '<section class="card border-warning'
        )
        self.assertLess(order_row_index, pagination_index)
        self.assertLess(pagination_index, regularization_index)
        self.assertNotIn('<tr class="ordem-row"', html)

        empty_context = {
            **context,
            "ordens": [],
            "total": 0,
            "total_pages": 1,
        }
        with self.app.test_request_context(
            "/logistica/transferencias?tab=historico"
        ):
            empty_html = render_flask_template(
                "logistica/transferencias.html", **empty_context
            )
        self.assertLess(
            empty_html.index("Sem ordens de transferência"),
            empty_html.index('<section class="card border-warning'),
        )
        with self.client.session_transaction() as current_session:
            self.assertEqual(
                current_session[
                    logistica_routes._LEGACY_RECEIPT_PREVIEW_KEY
                ]["snapshot"],
                self.preview["snapshot"],
            )

    def test_admin_without_gestor_never_gets_preview_and_cannot_post(self):
        self._set_user(manager=False, administrative=True)
        with (
            self._patch_history_data()[0],
            self._patch_history_data()[1],
            patch(
                "flask_app.routes.logistica.get_legacy_transfer_receipt_preview"
            ) as get_preview,
            patch(
                "flask_app.routes.logistica.render_template",
                return_value="history",
            ) as render_template,
        ):
            response = self.client.get(
                "/logistica/transferencias?tab=historico"
            )
            post_response = self.client.post(
                "/logistica/transferencias/regularizar-rececoes-antigas",
                data={"csrf_token": "forged"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            render_template.call_args.kwargs["legacy_receipt_preview"]
        )
        get_preview.assert_not_called()
        self.assertEqual(post_response.status_code, 403)

    def test_post_rejects_missing_csrf_token(self):
        self._set_user(manager=True)
        with patch(
            "flask_app.routes.logistica.regularize_legacy_transfer_receipts"
        ) as regularize:
            response = self.client.post(
                "/logistica/transferencias/regularizar-rececoes-antigas",
                data={
                    "expected_count": "2",
                    "confirmation_count": "2",
                    "expected_snapshot": self.preview["snapshot"],
                },
            )

        self.assertEqual(response.status_code, 400)
        regularize.assert_not_called()

    def test_post_requires_exact_preview_count_then_reports_success(self):
        self._set_user(manager=True)
        with self.client.session_transaction() as current_session:
            current_session[logistica_routes._LEGACY_RECEIPT_CSRF_KEY] = "csrf"
            current_session[logistica_routes._LEGACY_RECEIPT_PREVIEW_KEY] = {
                "eligible_count": 2,
                "snapshot": self.preview["snapshot"],
            }

        endpoint = "/logistica/transferencias/regularizar-rececoes-antigas"
        with patch(
            "flask_app.routes.logistica.regularize_legacy_transfer_receipts",
            return_value={
                "stale": False, "updated_count": 2, "replayed": False
            },
        ) as regularize:
            mismatch = self.client.post(
                endpoint,
                data={
                    "csrf_token": "csrf",
                    "expected_count": "2",
                    "confirmation_count": "1",
                    "expected_snapshot": self.preview["snapshot"],
                },
            )
            self.assertEqual(mismatch.status_code, 302)
            regularize.assert_not_called()

            response = self.client.post(
                endpoint,
                data={
                    "csrf_token": "csrf",
                    "expected_count": "2",
                    "confirmation_count": "2",
                    "expected_snapshot": self.preview["snapshot"],
                },
            )

        self.assertEqual(response.status_code, 302)
        regularize.assert_called_once_with(
            2, self.preview["snapshot"], "gestor-teste"
        )
        with self.client.session_transaction() as current_session:
            self.assertTrue(
                any(
                    "2 receção(ões) regularizada(s) administrativamente"
                    in message
                    for _category, message in current_session["_flashes"]
                )
            )


if __name__ == "__main__":
    unittest.main()