"""Team edit (rename) and delete routes — the admin escape hatch for typos
and mistakenly-created teams.

Mirrors the scripted-session harness in ``test_event_auth_modes``: each
``_S(...)`` batch answers the next query the handler issues, in order, so an
extra or missing query is caught as a regression. ``_assert_event_admin`` is
stubbed to a no-op (its own contract is covered in the auth tests); these
exercise the route wiring, validation, cascade order and audit logging.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.routes.events as evr
from web_api.common import ProblemException

from tests.unit.test_event_auth_modes import (
    _ASSOC,
    _S,
    _SessionCM,
    _event,
    _team,
)


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


# ── update_team (rename) ─────────────────────────────────────────────────────

class TestUpdateTeam:
    async def test_rename_persists_and_audits(self, client, monkeypatch):
        team = _team(4, name="Tpyo")
        s = _S([_event()], [team])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/events/1/teams/4", json={"name": "Typo Fixed"})
        assert r.status_code == 200
        assert team.name == "Typo Fixed"
        assert s.committed
        # One audit row for the rename.
        assert len(s.added) == 1

    async def test_rename_to_same_name_is_a_noop(self, client, monkeypatch):
        team = _team(4, name="Same")
        s = _S([_event()], [team])
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/events/1/teams/4", json={"name": "Same"})
        assert r.status_code == 200
        # No audit row, no commit — nothing changed.
        assert s.added == []
        assert not s.committed

    async def test_blank_name_rejected(self, client, monkeypatch):
        s = _S()  # never reaches the DB — validation is up front
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/events/1/teams/4", json={"name": "   "})
        assert r.status_code == 422

    async def test_overlong_name_rejected(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/events/1/teams/4", json={"name": "x" * 81})
        assert r.status_code == 422

    async def test_unknown_team_404(self, client, monkeypatch):
        s = _S([_event()], [])  # team lookup finds nothing
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/events/1/teams/4", json={"name": "New"})
        assert r.status_code == 404

    async def test_unknown_event_404(self, client, monkeypatch):
        s = _S([])  # event lookup finds nothing
        _wire(monkeypatch, s)
        r = await client.patch("/api/v1/events/9/teams/4", json={"name": "New"})
        assert r.status_code == 404


# ── delete_team ──────────────────────────────────────────────────────────────

class TestDeleteTeam:
    def _script(self, team, *, event=None):
        # Query order: event, team, then the four child-row bulk deletes
        # (bingo completions, completions, progress, members).
        return _S([event or _event()], [team], [], [], [], [])

    async def test_delete_clears_children_then_team_and_audits(self, client, monkeypatch):
        team = _team(4, name="Mistake")
        s = self._script(team)
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/teams/4")
        assert r.status_code == 200
        assert (await r.get_json())["ok"] is True
        assert s.committed
        # Exactly the six scripted queries were consumed (no more, no fewer).
        assert s._batches == []
        # One audit row for the deletion.
        assert len(s.added) == 1

    async def test_delete_on_past_event_blocked(self, client, monkeypatch):
        # A past event's roster (and history) is read-only.
        s = _S([_event(status="past")], [_team(4)], [], [], [], [])
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/teams/4")
        assert r.status_code == 409
        assert not s.committed

    async def test_delete_unknown_team_404(self, client, monkeypatch):
        s = _S([_event()], [])  # team lookup finds nothing
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/1/teams/4")
        assert r.status_code == 404
        assert not s.committed

    async def test_delete_unknown_event_404(self, client, monkeypatch):
        s = _S([])  # event lookup finds nothing
        _wire(monkeypatch, s)
        r = await client.delete("/api/v1/events/9/teams/4")
        assert r.status_code == 404
