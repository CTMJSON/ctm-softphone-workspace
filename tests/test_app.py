import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("CTM_ACCOUNT_ID", "12345")
os.environ.setdefault("DEFAULT_ACCOUNT_ID", "12345")

import app as subject


class SoftphoneAppTests(unittest.TestCase):
    def setUp(self):
        subject.app.config.update(TESTING=True)
        self.client = subject.app.test_client()
        with self.client.session_transaction() as session:
            # OAuth-authenticated session.
            session["auth_mode"] = "oauth"
            session["oauth"] = {
                "access_token": "test-token",
                "refresh_token": "test-refresh",
                "expires_at": 4102444800,  # year 2100, avoids refresh calls
            }
            session["oauth_account_id"] = "12345"
            session["email"] = "agent@example.com"
            session["first_name"] = "Alex"
            session["last_name"] = "Agent"
            session["session_id"] = "session-123"

    def test_calls_route_renders_normalized_call_data(self):
        payload = {
            "calls": [
                {
                    "called_at": "2026-04-25 01:45 PM -04:00",
                    "direction": "inbound",
                    "status": "answered",
                    "duration": 125,
                    "caller_name": "Taylor",
                    "caller_number": "+15551234567",
                    "agent": {"name": "Dana"},
                    "summary": "Asked about rescheduling and pricing.",
                    "source_name": "Google Ads",
                }
            ]
        }

        with patch("app.ctm_get", return_value=payload):
            response = self.client.get("/ctm/calls")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Taylor", html)
        self.assertIn("Dana", html)
        self.assertIn("Asked about rescheduling and pricing.", html)
        self.assertIn("Answered", html)
        self.assertIn("2:05", html)
        self.assertIn("1:45 PM", html)
        self.assertNotIn("Unknown time", html)
        self.assertIn("Missed Callbacks", html)

    def test_messages_route_renders_primary_messages_payload(self):
        payload = {
            "calls": [
                {
                    "called_at": "2026-04-25 02:05 PM -04:00",
                    "direction": "msg_inbound",
                    "caller_number": "+15557654321",
                    "tracking_number": "+15559876543",
                    "message_body": "Can someone call me back this afternoon?",
                    "status": "received",
                    "name": "Jordan",
                }
            ]
        }

        with patch("app.ctm_get", return_value=payload):
            response = self.client.get("/ctm/messages")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Text Log", html)
        self.assertIn("Inbound", html)
        self.assertIn("Can someone call me back this afternoon?", html)
        self.assertIn("+15557654321", html)
        self.assertIn("Jordan", html)
        self.assertIn("Reply Queue", html)

    def test_messages_route_falls_back_to_activity_payload(self):
        with patch(
            "app.ctm_get",
            side_effect=[
                RuntimeError("messages unavailable"),
                RuntimeError("texts unavailable"),
                {
                    "activities": [
                        {
                            "activity_type": "sms_inbound",
                            "created_at": "2026-04-25T15:10:00Z",
                            "from": "+15550001111",
                            "to": "+15559990000",
                            "content": "Following up on my missed call.",
                            "contact": {"name": "Jordan"},
                            "status": "delivered",
                        }
                    ]
                },
            ],
        ):
            response = self.client.get("/ctm/messages")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Jordan", html)
        self.assertIn("Following up on my missed call.", html)
        self.assertIn("Call Back", html)

    def test_chat_token_falls_back_for_known_account(self):
        token = subject.resolve_chat_token("11774", "")
        self.assertTrue(token.startswith("eyJhbGciOiJub25lIn0"))

    def test_login_page_offers_oauth_and_no_api_key_fields(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.get("/login")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Connect with CTM", html)
        self.assertNotIn("CTM API Key", html)
        self.assertNotIn("CTM API Secret", html)

    def test_oauth_login_redirects_to_ctm_authorize(self):
        with patch.object(subject, "get_oauth_client_id", return_value="client-abc"):
            response = self.client.get("/oauth/login")
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn("oauth2/authorize", location)
        self.assertIn("client_id=client-abc", location)
        self.assertIn("state=", location)

    def test_ops_panel_renders_agents_and_feed(self):
        with patch("app.fetch_agents", return_value=[
            {
                "name": "Alex Agent",
                "initials": "AA",
                "email": "alex@example.com",
                "availability": "Available",
                "availability_key": "available",
                "queue_total": 0,
                "duration_label": "4m 12s",
                "accepting": True,
            }
        ]), patch("app.build_news_items", return_value=[
            {"label": "Weather New York, NY", "detail": "65F, wind 8 mph", "source": "News", "category": "news"},
            {"label": "Headline", "detail": "Sample story", "source": "Tech", "category": "tech"},
        ]):
            response = self.client.get("/ctm/ops-panel")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Agent Availability", html)
        self.assertIn("Alex Agent", html)
        self.assertIn("Available", html)

    def test_local_weather_route_returns_widget_payload(self):
        with patch("app.fetch_local_weather_snapshot", return_value={
            "location": "Boston, MA",
            "temperature": 63,
            "condition": "Partly cloudy",
            "wind_speed": 9,
            "high": 68,
            "low": 54,
            "is_day": True,
            "observed_at": "2026-04-25T19:30",
            "source": "ip",
        }):
            response = self.client.get("/ctm/local-weather")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["location"], "Boston, MA")
        self.assertEqual(payload["temperature"], 63)


if __name__ == "__main__":
    unittest.main()
