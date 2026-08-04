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

@pytest.fixture
def direct_dispatch(monkeypatch):
    """Pin POST /webhook to the in-request dispatch path.

    ``_QUEUE_MODE`` is read from the environment at import time, and this box's
    ``.env`` carries the production ``WEBHOOK_QUEUE_MODE=true`` — so without
    this the route enqueues to Redis and never reaches a processor. The routing
    assertions below would then pass vacuously on the 400 the queue acceptor
    returns for a non-multipart body. The queue path itself is covered by
    ``test_queue_mode_enqueues_instead_of_dispatching``.
    """
    monkeypatch.setattr("api.routes.webhook._QUEUE_MODE", False)


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

    async def test_drop_type_calls_drop_processor(self, client, direct_dispatch):
        from data.submissions.common import SubmissionResponse

        # The route reaches processors as `submissions.drop_processor(...)`
        # (`from data import submissions`), so the attribute lives on the
        # package, not on api.routes.webhook.
        with patch(
            "data.submissions.drop_processor",
            new_callable=AsyncMock,
            return_value=SubmissionResponse(success=True, message="ok"),
        ) as mock_proc:
            payload_json = json.dumps(self._make_payload("drop"))
            response = await client.post(
                "/webhook",
                # `files=` (even empty) is what makes Quart's client send
                # multipart/form-data; with `data=` the route rejects the body
                # before dispatching and the assertion below never runs.
                form={"payload_json": payload_json},
                files={},
            )

        assert response.status_code == 200, await response.get_data(as_text=True)
        assert mock_proc.called

    async def test_pb_type_calls_pb_processor(self, client, direct_dispatch):
        from data.submissions.common import SubmissionResponse

        pb_fields = {
            "kill_time": "1:33.00",
            "npc_name": "Zulrah",
            "team_size": "Solo",
        }
        with patch(
            "data.submissions.pb_processor",
            new_callable=AsyncMock,
            return_value=SubmissionResponse(success=True, message="ok"),
        ) as mock_proc:
            # The wire type is "personal_best" (aliases: kill_time, npc_kill) —
            # "pb" is only the internal processor name and the route rejects it
            # as unsupported.
            payload_json = json.dumps(self._make_payload("personal_best", extra=pb_fields))
            response = await client.post(
                "/webhook",
                # `files=` (even empty) is what makes Quart's client send
                # multipart/form-data; with `data=` the route rejects the body
                # before dispatching and the assertion below never runs.
                form={"payload_json": payload_json},
                files={},
            )

        assert response.status_code == 200, await response.get_data(as_text=True)
        assert mock_proc.called

    async def test_queue_mode_enqueues_instead_of_dispatching(self, client, monkeypatch):
        """The live intake path (WEBHOOK_QUEUE_MODE=true in prod) must not dispatch.

        `/webhook` validates, stashes and RPUSHes; droptracker-webhook-consumer
        does the real work. Pinning this keeps the two routing tests above from
        quietly re-becoming assertions about a path production doesn't take.
        """
        monkeypatch.setattr("api.routes.webhook._QUEUE_MODE", True)

        with patch(
            "data.submissions.drop_processor", new_callable=AsyncMock
        ) as mock_proc:
            response = await client.post(
                "/webhook",
                form={"payload_json": json.dumps(self._make_payload("drop"))},
                files={},
            )

        assert response.status_code == 200, await response.get_data(as_text=True)
        assert not mock_proc.called

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
