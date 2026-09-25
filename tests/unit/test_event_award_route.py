"""POST /events/{id}/award driven through the route (Quart test client + the
scripted session from test_event_auth_modes).

Every manual award 500'd from 2026-09-24, and no test noticed:
test_event_award_competition only calls ``_competition_award_fields``, so
the closure that does the work (``_apply``) never ran under pytest. It had
two faults:

- it rebound ``credit`` and ``matched_target`` without ``nonlocal``, which
  made both unbound at their first read (UnboundLocalError on EVERY award,
  race or not);
- its roster check selected ``EventTeamMember.id``, a column that doesn't
  exist (the key is ``(team_id, player_id)``). The stubbed ``db`` can't see
  that one; test_model_attribute_refs.py does.

These run the whole route so the closure executes. The engine and the
receipt are stubbed; apply/fold are covered elsewhere.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import web_api.routes.event_admin as ea

from tests.unit.test_event_auth_modes import _S, _SessionCM, _event, _team
from tests.unit.test_event_award_competition import BOTW, comp

URL = "/api/v1/events/1/award"
RACE = SimpleNamespace(id=10, event_id=1, type="competition", config=json.dumps(BOTW))
BINGO = SimpleNamespace(id=11, event_id=1, type="item_collection", config="{}")


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session):
    """Route the handler at ``session``; returns the rows handed to apply."""
    applied = []
    engine = SimpleNamespace(
        _task_to_dict=lambda t: {"id": t.id, "type": t.type},
        # A point-weighted item: 3 credit per unit; anything else isn't listed.
        item_match_quantity=lambda task, target, qty: (
            qty * 3 if target == "Odium shard 1" else None),
        apply_completion=lambda s, c: applied.append(c),
    )
    monkeypatch.setitem(sys.modules, "services.competition", comp)
    monkeypatch.setattr(sys.modules["services"], "event_credit_receipt",
                        SimpleNamespace(capture=lambda *a, **k: {},
                                        finish=lambda *a, **k: None))
    monkeypatch.setattr(ea, "current_user_id", lambda: 7)
    monkeypatch.setattr(ea, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(ea, "_assert_event_admin", lambda *a, **k: None)
    monkeypatch.setattr(ea, "_bump", lambda *a, **k: None)
    monkeypatch.setattr(ea, "_engine", lambda: engine)
    monkeypatch.setattr(ea, "EventCompletion",
                        lambda **kw: SimpleNamespace(id=501, **kw))
    return applied


def _session(task, *, roster=None):
    """Event, task and team lookups, plus the roster check when a player is
    named (``roster`` = that query's rows)."""
    batches = [[_event()], [task], [_team(3, "A1 Steaks")]]
    if roster is not None:
        batches.append(roster)
    return _S(*batches)


class TestRaceAward:
    async def test_bonus_points_credit_the_named_player(self, client, monkeypatch):
        s = _session(RACE, roster=[(5,)])
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={
            "task_id": 10, "team_id": 3, "player_id": 5, "credit": "bonus",
            "quantity": 1000, "note": "Dupe Baron pet"})
        assert r.status_code == 200, await r.get_json()
        assert (await r.get_json())["id"] == 501
        row = applied[0]
        assert (row.player_id, row.quantity, row.matched_target) == (5, 1000, None)
        assert comp.parse_bonus_note(row.note) == ("manual", comp.MANUAL_RULE_ID)
        assert row.submission_guid.startswith("manual:10:3:7:5b:")
        assert s.committed and not s._batches  # the roster check ran

    async def test_gained_kills_under_a_raced_boss(self, client, monkeypatch):
        s = _session(RACE, roster=[(5,)])
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={
            "task_id": 10, "team_id": 3, "player_id": 5, "credit": "gained",
            "quantity": 4, "matched_target": "Dagannoth Rex"})
        assert r.status_code == 200, await r.get_json()
        row = applied[0]
        assert (row.matched_target, row.quantity, row.note) == ("Dagannoth Rex", 4, None)

    async def test_player_off_the_roster_is_refused(self, client, monkeypatch):
        s = _session(RACE, roster=[])
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={
            "task_id": 10, "team_id": 3, "player_id": 99, "credit": "bonus",
            "quantity": 10})
        assert r.status_code == 422
        assert (await r.get_json())["title"] == "Player not on team"
        assert not applied and not s.committed

    async def test_a_race_award_needs_a_player(self, client, monkeypatch):
        s = _session(RACE)
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 10, "team_id": 3, "quantity": 10})
        assert r.status_code == 422
        assert (await r.get_json())["title"] == "Choose a player"
        assert not applied and not s.committed


class TestOrdinaryAward:
    async def test_team_only_award(self, client, monkeypatch):
        s = _session(BINGO)
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 11, "team_id": 3, "note": "missed"})
        assert r.status_code == 200, await r.get_json()
        row = applied[0]
        assert (row.player_id, row.quantity, row.note) == (None, 1, "missed")
        assert s.committed

    async def test_award_can_name_a_player(self, client, monkeypatch):
        s = _session(BINGO, roster=[(5,)])
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 11, "team_id": 3, "player_id": 5})
        assert r.status_code == 200, await r.get_json()
        assert applied[0].player_id == 5
        assert not s._batches

    async def test_item_part_takes_the_matched_credit(self, client, monkeypatch):
        s = _session(BINGO)
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={
            "task_id": 11, "team_id": 3, "quantity": 2,
            "matched_target": "Odium shard 1"})
        assert r.status_code == 200, await r.get_json()
        assert (applied[0].matched_target, applied[0].quantity) == ("Odium shard 1", 6)

    async def test_unlisted_item_is_refused(self, client, monkeypatch):
        s = _session(BINGO)
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={
            "task_id": 11, "team_id": 3, "matched_target": "Twisted bow"})
        assert r.status_code == 422
        assert (await r.get_json())["title"] == "Item not part of task"
        assert not applied

    async def test_credit_is_refused_off_a_race(self, client, monkeypatch):
        s = _session(BINGO)
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 11, "team_id": 3, "credit": "bonus"})
        assert r.status_code == 422
        assert (await r.get_json())["title"] == "Invalid credit"
        assert not applied
