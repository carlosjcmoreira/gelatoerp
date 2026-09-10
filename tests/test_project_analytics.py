import unittest

from flask import Flask, redirect, session

from flask_app.analytics import consume_analytics_events, queue_analytics_event


class ProjectAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "analytics-test-secret"

    def test_queues_and_consumes_safe_event_once(self):
        with self.app.test_request_context("/"):
            queue_analytics_event(
                "production_transfer_created",
                destination_type="b2b",
                order_count=3,
            )
            self.assertEqual(
                consume_analytics_events(),
                [{
                    "name": "production_transfer_created",
                    "data": {"destination_type": "b2b", "order_count": 3},
                }],
            )
            self.assertEqual(consume_analytics_events(), [])

    def test_rejects_personal_identifiers_and_free_form_values(self):
        with self.app.test_request_context("/"):
            self.assertFalse(
                queue_analytics_event("event_request_submitted", email="person@example.com")
            )
            self.assertFalse(
                queue_analytics_event("event_request_submitted", event_id=42)
            )
            self.assertFalse(queue_analytics_event(
                "invoice_payment_scheduled",
                payment_method_selected="bank transfer",
            ))
            self.assertNotIn("_analytics_events", session)

    def test_rejects_invalid_event_names_and_nested_data(self):
        with self.app.test_request_context("/"):
            self.assertFalse(queue_analytics_event("Event Submitted"))
            self.assertFalse(
                queue_analytics_event(
                    "event_request_submitted", dimensions={"type": "b2b"}
                )
            )

    def test_invalid_telemetry_cannot_turn_successful_route_into_error(self):
        @self.app.post("/successful-write")
        def successful_write():
            session["write_completed"] = True
            queue_analytics_event(
                "event_request_submitted",
                occurrence_count=999,
                catering_requested=False,
            )
            return redirect("/done")

        client = self.app.test_client()
        response = client.post("/successful-write")
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as current_session:
            self.assertTrue(current_session["write_completed"])
            self.assertNotIn("_analytics_events", current_session)

    def test_queue_is_bounded_to_twenty_events(self):
        with self.app.test_request_context("/"):
            for count in range(25):
                queue_analytics_event(
                    "stock_count_recorded",
                    location="Bolhão",
                    line_count=count + 1,
                )
            self.assertEqual(len(session["_analytics_events"]), 20)
            self.assertEqual(session["_analytics_events"][0]["data"]["line_count"], 6)

    def test_base_template_uses_guarded_umami_wrapper(self):
        with self.app.open_resource(
            "../flask_app/templates/base.html", mode="r"
        ) as source:
            template = source.read()
        self.assertIn("window.umami.track", template)
        self.assertIn("window.trackEvent", template)
        self.assertIn("pop_analytics_events()", template)
        self.assertIn("sessionStorage", template)
        self.assertIn("slice(-100)", template)
        self.assertIn("catch (err)", template)


if __name__ == "__main__":
    unittest.main()