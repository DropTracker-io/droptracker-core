"""Event delete route — the creator/superadmin escape hatch for abandoned
drafts and finished events (keeps the history from filling with broken drafts).

Reuses the scripted-session harness from ``test_event_auth_modes``:
``_assert_event_admin`` is stubbed (its own contract is covered by the auth
tests), so these exercise the route's own rules — the "not while live" guard,
the type-the-name confirmation, and that a clean delete cascades + audits +
commits. The cascade internals are stubbed here (their FK order is exercised
against a real schema in integration); this locks the wiring and guardrails.
"""

from __future__ import annotations

import pytest

import web_api.routes.events as evr

from tests.unit.test_event_auth_modes import _ASSOC, _S, _SessionCM, _event


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session, user_id=7):
    monkeypatch.setattr(evr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "user_group_association", _ASSOC)
    monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)


def _stub_cascade(monkeypatch):
    """Record the cascade + Discord-teardown calls without touching a DB."""
    calls = {"cascade": 0, "orphans": 0}

    def _cascade(s, ev):
        calls["cascade"] += 1

    def _orphans(s, event_id):
        calls["orphans"] += 1

    monkeypatch.setattr(evr, "_cascade_delete_event", _cascade)
    monkeypatch.setattr(evr, "_enqueue_orphan_scheduled_events", _orphans)
    return calls


class TestDeleteEventGuards:
    async def test_live_event_cannot_be_deleted(self, client, monkeypatch):
        # A running event must be ended first — even with a correct name.
        s = _S([_event(status="active", name="Live One")])
        _wire(monkeypatch, s)
        _stub_cascade(monkeypatch)
        r = await client.delete("/api/v1/events/1", json={"confirm_name": "Live One"})
        assert r.status_code == 409
        assert not s.committed

    async def test_missing_confirmation_rejected(self, client, monkeypatch):
        s = _S([_event(status="draft", name="Draft Ev")])
        _wire(monkeypatch, s)
        calls = _stub_cascade(monkeypatch)
        r = await client.delete("/api/v1/events/1", json={})
        assert r.status_code == 422
        assert calls["cascade"] == 0
        assert not s.committed

    async def test_wrong_confirmation_rejected(self, client, monkeypatch):
        s = _S([_event(status="draft", name="Draft Ev")])
        _wire(monkeypatch, s)
        calls = _stub_cascade(monkeypatch)
        r = await client.delete("/api/v1/events/1", json={"confirm_name": "not it"})
        assert r.status_code == 422
        assert calls["cascade"] == 0
        assert not s.committed


class TestDeleteEventHappyPath:
    async def test_draft_deletes_with_matching_name(self, client, monkeypatch):
        s = _S([_event(status="draft", name="Broken Draft")])
        _wire(monkeypatch, s)
        calls = _stub_cascade(monkeypatch)
        # Confirmation is case/whitespace-insensitive.
        r = await client.delete(
            "/api/v1/events/1", json={"confirm_name": "  broken   draft "}
        )
        assert r.status_code == 200
        assert (await r.get_json())["ok"] is True
        assert calls["orphans"] == 1  # Discord teardown queued before the rows go
        assert calls["cascade"] == 1
        assert s.committed
        # Exactly one audit row for the delete (AuditLog is a conftest mock, so
        # we can only assert a single row was recorded — same as delete_team).
        assert len(s.added) == 1

    async def test_ended_event_can_be_deleted(self, client, monkeypatch):
        s = _S([_event(status="past", name="Old Event")])
        _wire(monkeypatch, s)
        calls = _stub_cascade(monkeypatch)
        r = await client.delete("/api/v1/events/1", json={"confirm_name": "Old Event"})
        assert r.status_code == 200
        assert calls["cascade"] == 1
        assert s.committed
