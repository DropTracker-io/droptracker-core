"""Unit tests for the /manual-submit shared-secret gate (X-DT-Manual-Key).

The endpoint substitutes the target player's real account_hash into the
payload, so it must never be publicly callable: it fails closed (503) when
MANUAL_SUBMIT_KEY is unconfigured and rejects a missing/wrong header (401).
The conftest stubs replace db/redis/api.core, so these tests exercise only the
route-level auth wiring — a correctly-keyed request proceeds past the gate and
fails on ordinary payload validation (400) instead.
"""

import pytest
from quart import Quart

from api.routes.webhook import webhook_bp, MANUAL_SUBMIT_KEY_HEADER


@pytest.fixture()
def client():
    app = Quart(__name__)
    app.register_blueprint(webhook_bp)
    return app.test_client()


VALID_BODY = {"submission_type": "drop", "player_name": "Test Player"}


class TestManualSubmitAuth:
    async def test_unconfigured_key_fails_closed_503(self, client, monkeypatch):
        monkeypatch.delenv("MANUAL_SUBMIT_KEY", raising=False)
        resp = await client.post(
            "/manual-submit",
            json=VALID_BODY,
            headers={MANUAL_SUBMIT_KEY_HEADER: "anything"},
        )
        assert resp.status_code == 503
        data = await resp.get_json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    async def test_blank_configured_key_fails_closed_503(self, client, monkeypatch):
        # A present-but-empty env var must behave exactly like an unset one.
        monkeypatch.setenv("MANUAL_SUBMIT_KEY", "   ")
        resp = await client.post("/manual-submit", json=VALID_BODY)
        assert resp.status_code == 503

    async def test_missing_header_is_401(self, client, monkeypatch):
        monkeypatch.setenv("MANUAL_SUBMIT_KEY", "sekrit")
        resp = await client.post("/manual-submit", json=VALID_BODY)
        assert resp.status_code == 401
        data = await resp.get_json()
        assert data == {"success": False, "error": "Unauthorized."}

    async def test_wrong_key_is_401(self, client, monkeypatch):
        monkeypatch.setenv("MANUAL_SUBMIT_KEY", "sekrit")
        resp = await client.post(
            "/manual-submit",
            json=VALID_BODY,
            headers={MANUAL_SUBMIT_KEY_HEADER: "not-the-key"},
        )
        assert resp.status_code == 401

    async def test_empty_header_is_401(self, client, monkeypatch):
        monkeypatch.setenv("MANUAL_SUBMIT_KEY", "sekrit")
        resp = await client.post(
            "/manual-submit",
            json=VALID_BODY,
            headers={MANUAL_SUBMIT_KEY_HEADER: ""},
        )
        assert resp.status_code == 401

    async def test_valid_key_passes_gate(self, client, monkeypatch):
        # With the right key the request clears auth and reaches normal
        # payload validation: an incomplete body draws a 400, never 401/503.
        monkeypatch.setenv("MANUAL_SUBMIT_KEY", "sekrit")
        resp = await client.post(
            "/manual-submit",
            json={"submission_type": "drop"},  # player_name missing
            headers={MANUAL_SUBMIT_KEY_HEADER: "sekrit"},
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "player_name" in data["error"]

    async def test_valid_key_is_whitespace_tolerant(self, client, monkeypatch):
        # Keys copied into .env files often pick up stray whitespace; both
        # sides are stripped before the constant-time compare.
        monkeypatch.setenv("MANUAL_SUBMIT_KEY", " sekrit\n")
        resp = await client.post(
            "/manual-submit",
            json={"submission_type": "drop"},
            headers={MANUAL_SUBMIT_KEY_HEADER: "sekrit"},
        )
        assert resp.status_code == 400

    async def test_gate_runs_before_body_parsing(self, client, monkeypatch):
        # Unauthenticated garbage must be rejected without ever being parsed.
        monkeypatch.setenv("MANUAL_SUBMIT_KEY", "sekrit")
        resp = await client.post(
            "/manual-submit",
            data="not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401
