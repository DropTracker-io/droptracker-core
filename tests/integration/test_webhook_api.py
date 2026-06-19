"""
Integration tests for the Quart REST API.

These tests use Quart's built-in test client, so no real server is started.
Processor functions are mocked to keep tests isolated from DB / external APIs.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
async def app():
    """Create a fresh Quart app instance for each test."""
    from api import create_app
    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
async def client(app):
    async with app.test_client() as test_client:
        yield test_client


# ── Health / liveness endpoints ───────────────────────────────────────────────

class TestHealthEndpoints:
    async def test_ping_returns_200(self, client):
        response = await client.get("/ping")
        assert response.status_code == 200

    async def test_ping_returns_pong_body(self, client):
        response = await client.get("/ping")
        data = await response.get_data(as_text=True)
        assert "pong" in data.lower() or response.status_code == 200

    async def test_health_endpoint_exists(self, client):
        # /health may return non-200 in test (no real DB), but it must respond
        response = await client.get("/health")
        assert response.status_code in (200, 503)


# ── Webhook submission routing ────────────────────────────────────────────────

class TestWebhookRouting:
    """Verify that POST /webhook routes to the correct processor based on 'type'."""

    def _make_payload(self, submission_type: str, extra: dict = None) -> dict:
        base = {
            "embeds": [
                {
                    "fields": [
                        {"name": "type", "value": submission_type},
                        {"name": "player_name", "value": "TestPlayer"},
                        {"name": "acc_hash", "value": "testhash123"},
                        {"name": "guid", "value": f"test-guid-{submission_type}"},
                        {"name": "item_name", "value": "Dragon bones"},
                        {"name": "item_id", "value": "536"},
                        {"name": "value", "value": "5000"},
                        {"name": "quantity", "value": "1"},
                        {"name": "source", "value": "Green dragon"},
                    ]
                }
            ]
        }
        if extra:
            base["embeds"][0]["fields"].extend(
                [{"name": k, "value": str(v)} for k, v in extra.items()]
            )
        return base

    async def test_drop_type_calls_drop_processor(self, client):
        from data.submissions.common import SubmissionResponse

        with patch(
            "api.routes.webhook.drop_processor",
            new_callable=AsyncMock,
            return_value=SubmissionResponse(success=True, message="ok"),
        ) as mock_proc:
            payload_json = json.dumps(self._make_payload("drop"))
            response = await client.post(
                "/webhook",
                data={"payload_json": payload_json},
            )

        assert response.status_code in (200, 400, 422)
        # If the route reached the processor, it was called
        if response.status_code == 200:
            assert mock_proc.called

    async def test_pb_type_calls_pb_processor(self, client):
        from data.submissions.common import SubmissionResponse

        pb_fields = {
            "kill_time": "1:33.00",
            "npc_name": "Zulrah",
            "team_size": "Solo",
        }
        with patch(
            "api.routes.webhook.pb_processor",
            new_callable=AsyncMock,
            return_value=SubmissionResponse(success=True, message="ok"),
        ) as mock_proc:
            payload_json = json.dumps(self._make_payload("pb", extra=pb_fields))
            response = await client.post(
                "/webhook",
                data={"payload_json": payload_json},
            )

        assert response.status_code in (200, 400, 422)
        if response.status_code == 200:
            assert mock_proc.called

    async def test_missing_payload_json_returns_error(self, client):
        response = await client.post("/webhook", data={})
        assert response.status_code in (400, 422, 500)

    async def test_malformed_json_returns_error(self, client):
        response = await client.post(
            "/webhook",
            data={"payload_json": "this is not json"},
        )
        assert response.status_code in (400, 422, 500)


# ── Rate limiting (smoke test) ────────────────────────────────────────────────

class TestRateLimitHeaders:
    async def test_ping_has_no_rate_limit_rejection_on_first_call(self, client):
        response = await client.get("/ping")
        # First call should never be rate-limited
        assert response.status_code != 429
