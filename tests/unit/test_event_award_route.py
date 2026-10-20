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
        parse_task_config=lambda raw: json.loads(raw) if raw else {},
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


ORIGINAL = SimpleNamespace(id=20, event_id=1, type="item_collection", config="{}")


def _instance(id, source_task_id, team_id=3):
    return SimpleNamespace(id=id, event_id=1, type="item_collection", config=json.dumps({
        "board_instance": True, "source_task_id": source_task_id,
        "team_id": team_id, "tile_idx": 37}))


def _board_session(task, *, position, instance=None):
    """Board-game event: event, task, team, then the position lookup and
    (when the position holds a task) the instance lookup."""
    batches = [[_event(kind="board_game")], [task], [_team(3, "A1 Steaks")], [position]]
    if position is not None and position.current_task_id:
        batches.append([instance])
    return _S(*batches)


class TestBoardAward:
    """Suggestion #200: an award on a tile's original task lands on the team's
    live instance, the only task that moves the piece."""

    async def test_original_task_redirects_to_the_live_instance(self, client, monkeypatch):
        pos = SimpleNamespace(team_id=3, event_id=1, current_task_id=88)
        s = _board_session(ORIGINAL, position=pos, instance=_instance(88, 20))
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 20, "team_id": 3, "quantity": 2})
        assert r.status_code == 200, await r.get_json()
        assert (await r.get_json())["task_id"] == 88
        assert applied[0].task_id == 88
        assert applied[0].submission_guid.startswith("manual:88:3:")
        assert not s._batches

    async def test_other_tile_keeps_the_picked_task(self, client, monkeypatch):
        pos = SimpleNamespace(team_id=3, event_id=1, current_task_id=89)
        s = _board_session(ORIGINAL, position=pos, instance=_instance(89, 21))
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 20, "team_id": 3})
        assert r.status_code == 200, await r.get_json()
        assert applied[0].task_id == 20

    async def test_no_position_keeps_the_picked_task(self, client, monkeypatch):
        s = _board_session(ORIGINAL, position=None)
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 20, "team_id": 3})
        assert r.status_code == 200, await r.get_json()
        assert applied[0].task_id == 20

    async def test_an_instance_is_never_redirected(self, client, monkeypatch):
        s = _S([_event(kind="board_game")], [_instance(88, 20)], [_team(3, "A1 Steaks")])
        applied = _wire(monkeypatch, s)
        r = await client.post(URL, json={"task_id": 88, "team_id": 3})
        assert r.status_code == 200, await r.get_json()
        assert applied[0].task_id == 88
