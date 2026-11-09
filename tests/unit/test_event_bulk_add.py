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
    use_real_signup_service,
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
    use_real_signup_service(monkeypatch)


def _script_standard(*, event=None, team=None, players=(), eligible=(), placed=(),
                     adds=True, guests=False):
    """Query order on a standard event with an unbound team:
    event, team, player resolve, clan memberships (``(player_id, group_id)``;
    the event's group is 42), existing placements, the event's guest list
    when someone joins as a guest (web160a), and — when at least one name is
    actually added — the web71a buy-in carry-over UPDATE. (Everything after
    the resolve is only issued when a name resolved.)"""
    batches = [[event or _event()], [team if team is not None else _team(4)], list(players)]
    if players:
        batches.append([(pid, 42) for (pid,) in eligible])
        batches.append(list(placed))
        if guests:
            batches.append([])   # existing guests (mark_guests)
        if adds:
            batches.append([])   # buy-in carry-over UPDATE (web71a)
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
        # alice+bob resolve and are members; bob is already on this team,
        # carol is on team 9, "ghost" isn't tracked, dave isn't in the clan
        # and joins as a guest (web160a).
        s = _script_standard(
            players=[(1, "Alice"), (2, "Bob"), (3, "Carol"), (5, "Dave")],
            eligible=[(1,), (2,), (3,)],
            placed=[(2, 4), (3, 9)],
            guests=True,
        )
        _wire(monkeypatch, s)
        r = await client.post(
            BULK, json={"names": ["alice", "bob", "carol", "ghost", "dave"]}
        )
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == [{"id": 1, "name": "Alice", "guest": False},
                                 {"id": 5, "name": "Dave", "guest": True}]
        reasons = {row["name"]: row["reason"] for row in body["skipped"]}
        assert reasons["Bob"] == "Already on this team."
        assert reasons["Carol"] == "Already on another team in this event."
        assert reasons["ghost"] == "No tracked player by that name."
        assert "Dave" not in reasons
        # Two membership rows + Dave's guest row + one audit row; committed.
        kinds = [type(o).__name__ for o in s.added]
        assert kinds.count("EventTeamMember") == 2 and kinds.count("EventGuest") == 1
        assert len(s.added) == 4
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
        assert body["added"] == [{"id": 1, "name": "Alice", "guest": False}]
        assert body["skipped"] == []
        assert len(s.added) == 2  # one member + one audit — not three

    async def test_game_spelling_resolves_the_stored_wom_spelling(self, client, monkeypatch):
        # OSRS treats '-', '_' and ' ' as one character; the row carries WOM's
        # space. "1-19" used to be skipped as untracked, and its two spellings
        # counted as two names.
        s = _script_standard(
            players=[(5766416, "1 19"), (5755902, "tzuk kal lag")],
            eligible=[(5766416,), (5755902,)],
            placed=[],
        )
        _wire(monkeypatch, s)
        r = await client.post(
            BULK, json={"names": ["1-19", "1 19", "1_19", "Tzuk-Kal-Lag"]})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == [
            {"id": 5766416, "name": "1 19", "guest": False},
            {"id": 5755902, "name": "tzuk kal lag", "guest": False},
        ]
        assert body["skipped"] == []

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

    async def test_a_rival_clans_member_is_refused(self, client, monkeypatch):
        # Clan-vs-clan: team 4 is clan 43's; Alice plays for clan 42.
        s = _S(
            [_event(mode="clan_vs_clan")],
            [_team(4, group_id=43)],
            [(1, "Alice")],   # player resolve
            [(42,), (43,)],   # participating groups
            [(1, 42)],        # Alice's clan memberships
            [],               # placements
        )
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == []
        assert body["skipped"][0]["reason"] == "In another clan taking part in this event."
        assert not s.committed

    async def test_someone_in_no_clan_joins_a_clan_team_as_a_guest(self, client, monkeypatch):
        s = _S(
            [_event(mode="clan_vs_clan")],
            [_team(4, group_id=43)],
            [(1, "Alice")],   # player resolve
            [(42,), (43,)],   # participating groups
            [],               # in none of them
            [],               # placements
            [],               # existing guests
            [],               # buy-in carry-over UPDATE (web71a)
        )
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        body = await r.get_json()
        assert body["added"] == [{"id": 1, "name": "Alice", "guest": True}]
        assert s.committed
        assert s._batches == []

    async def test_clan_vs_clan_reads_participants(self, client, monkeypatch):
        # cvc adds one EventGroup query between the team lookup and resolve.
        s = _S(
            [_event(mode="clan_vs_clan")],
            [_team(4)],
            [(1, "Alice")],   # player resolve
            [(42,), (43,)],   # participating groups
            [(1, 42)],        # clan memberships
            [],               # placements
            [],               # buy-in carry-over UPDATE (web71a)
        )
        _wire(monkeypatch, s)
        r = await client.post(BULK, json={"names": ["alice"]})
        assert r.status_code == 200
        body = await r.get_json()
        assert body["added"] == [{"id": 1, "name": "Alice", "guest": False}]
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
