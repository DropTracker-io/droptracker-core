"""Bulk roster add — POST /events/{id}/teams/{teamId}/members/bulk (the
"paste your team" flow: a comma-separated list of RSNs from the web UI).

Mirrors the scripted-session harness in ``test_event_auth_modes``: each
``_S(...)`` batch answers the next query the handler issues, in order.
``_assert_event_admin`` is stubbed to a no-op (its contract is covered in the
auth tests); these exercise validation, name resolution, eligibility, the
never-moves guarantee, per-name outcomes and audit logging.
"""

from __future__ import annotations

import pytest

import web_api.routes.events as evr

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


def _script_standard(*, event=None, team=None, players=(), eligible=(), placed=()):
    """Query order on a standard event with an unbound team:
    event, team, player resolve, eligibility, existing placements.
    (The last two are only issued when at least one name resolved.)"""
    batches = [[event or _event()], [team if team is not None else _team(4)], list(players)]
    if players:
        batches.append(list(eligible))
        batches.append(list(placed))
    return _S(*batches)


BULK = "/api/v1/events/1/teams/4/members/bulk"


class TestBulkAddValidation:
    async def test_names_must_be_a_list(self, client, monkeypatch):
        s = _S()  # validation is up front — never reaches the DB
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": "a, b"})
        assert r.status_code == 422

    async def test_empty_list_rejected(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": []})
        assert r.status_code == 422

    async def test_non_string_entry_rejected(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["ok", 3]})
        assert r.status_code == 422

    async def test_all_blank_rejected(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["  ", ""]})
        assert r.status_code == 422

    async def test_over_cap_rejected(self, client, monkeypatch):
        s = _S()
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": [f"p{i}" for i in range(201)]})
        assert r.status_code == 422


class TestBulkAddOutcomes:
    async def test_mixed_outcomes_report_per_name(self, client, monkeypatch):
        # alice+bob resolve and are eligible; bob is already on this team,
        # carol is on team 9, "ghost" isn't tracked, dave isn't in the clan.
        s = _script_standard(
            players=[(1, "Alice"), (2, "Bob"), (3, "Carol"), (5, "Dave")],
            eligible=[(1,), (2,), (3,)],
            placed=[(2, 4), (3, 9)],
        )
        _wire(monkeypatch, s)
        r = await client.post(
            BULK, json={"names": ["alice", "bob", "carol", "ghost", "dave"]}
        )
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == [{"id": 1, "name": "Alice"}]
        reasons = {row["name"]: row["reason"] for row in body["skipped"]}
        assert reasons["Bob"] == "Already on this team."
        assert reasons["Carol"] == "Already on another team in this event."
        assert reasons["ghost"] == "No tracked player by that name."
        assert reasons["Dave"] == "Not a member of a participating clan."
        # One membership row + one audit row; committed.
        assert len(s.added) == 2
        assert s.committed
        # Exactly the scripted queries were consumed.
        assert s._batches == []

    async def test_duplicate_names_deduped_case_insensitively(self, client, monkeypatch):
        s = _script_standard(
            players=[(1, "Alice")],
            eligible=[(1,)],
            placed=[],
        )
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["Alice", " alice ", "ALICE"]})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == [{"id": 1, "name": "Alice"}]
        assert body["skipped"] == []
        assert len(s.added) == 2  # one member + one audit — not three

    async def test_nothing_added_means_no_commit_or_audit(self, client, monkeypatch):
        s = _script_standard(players=[])  # nobody resolves
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["ghost1", "ghost2"]})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == []
        assert len(body["skipped"]) == 2
        assert s.added == []
        assert not s.committed

    async def test_clan_bound_team_reason(self, client, monkeypatch):
        # Team bound to clan 9; the resolved player is not in it.
        s = _script_standard(
            team=_team(4, group_id=9),
            players=[(1, "Alice")],
            eligible=[],
            placed=[],
        )
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == []
        assert body["skipped"][0]["reason"] == "Not a member of the clan this team represents."

    async def test_clan_vs_clan_reads_participants(self, client, monkeypatch):
        # cvc adds one EventGroup query between the team lookup and resolve.
        s = _S(
            [_event(mode="clan_vs_clan")],
            [_team(4)],
            [(1, "Alice")],   # player resolve
            [(42,), (43,)],   # participating groups
            [(1,)],           # eligibility
            [],               # placements
        )
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == [{"id": 1, "name": "Alice"}]
        assert s._batches == []


class TestBulkAddGuards:
    async def test_past_event_blocked(self, client, monkeypatch):
        s = _S([_event(status="past")])
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        assert r.status_code == 409
        assert not s.committed

    async def test_unknown_team_404(self, client, monkeypatch):
        s = _S([_event()], [])
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        assert r.status_code == 404

    async def test_unknown_event_404(self, client, monkeypatch):
        s = _S([])
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        assert r.status_code == 404
