"""``/v2/events/{id}/standings``: who may read it, and the shape a clan bot
pays rewards from. Placement maths itself is services/event_point_awards'
(tested there); these tests pin how the endpoint uses it."""
from __future__ import annotations

import sys
import types

import pytest

from data_api.routes import event_standings as es


def _event(**kw):
    base = dict(id=70, name="Relay Test Bingo", kind="bingo", mode="standard",
                status="active", visibility="public", group_id=299,
                starts_at=None, ends_at=None, ended_at=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    join = order_by = filter

    def all(self):
        return list(self.rows)


class _Session:
    """Answers the endpoint's queries by the first entity asked for."""

    def __init__(self, teams=(), members=(), groups=()):
        self.teams, self.members, self.groups = list(teams), list(members), list(groups)

    def query(self, *entities):
        name = getattr(entities[0], "key", None) or getattr(entities[0], "__name__", "")
        owner = getattr(getattr(entities[0], "class_", None), "__name__", "")
        if name == "EventTeam":
            return _Q(self.teams)
        if owner == "EventTeamMember":
            return _Q(self.members)
        if owner == "EventGroup":
            return _Q([(g,) for g in self.groups])
        raise AssertionError(entities)


@pytest.fixture
def epa(monkeypatch):
    mod = types.ModuleType("services.event_point_awards")
    mod.state = {"individual": False}
    mod._is_competition = lambda ev: ev.kind in ("sotw", "botw")
    mod._is_team_race = lambda s, ev: not mod.state["individual"]
    mod._team_placements = lambda s, ev: ({1: 1, 2: 1, 3: 3}, {})
    mod._took_part = lambda s, eid, pids: {10, 30}
    mod._clan_member_ids = lambda s, gid, pids: {10, 20}
    mod._competition_standings = lambda s, ev: ([
        {"player_id": 10, "player_name": "Ann", "registered": True, "gained": 500, "points": 0},
        {"player_id": None, "player_name": "WomOnly", "registered": False, "gained": 400},
        {"player_id": 99, "player_name": "Hidden", "registered": True, "gained": 300},
        {"player_id": 20, "player_name": "Bob", "registered": True, "gained": 0},
    ], "gained")
    mod._competition_placements = lambda ranked, mode: {10: 1, 99: 3}
    monkeypatch.setitem(sys.modules, "services.event_point_awards", mod)
    pkg = sys.modules.get("services")
    if pkg is not None:
        monkeypatch.setattr(pkg, "event_point_awards", mod, raising=False)
    monkeypatch.setattr(es.scope, "visible_player_ids",
                        lambda s, pids: [p for p in pids if p != 99])
    return mod


GROUP_KEY = {"owner_type": "group", "group_id": 299, "scope": "group"}
OTHER_KEY = {"owner_type": "group", "group_id": 5, "scope": "group"}
GLOBAL_KEY = {"owner_type": "global", "group_id": None, "scope": "global"}


class TestAccess:
    def test_public_started_event_is_open_to_any_key(self):
        assert es.may_read_event(_Session(), OTHER_KEY, _event())
        assert es.may_read_event(_Session(), GLOBAL_KEY, _event(status="past"))

    def test_private_event_only_for_participants(self):
        ev = _event(visibility="private")
        assert es.may_read_event(_Session(), GROUP_KEY, ev)
        assert not es.may_read_event(_Session(), OTHER_KEY, ev)
        assert not es.may_read_event(_Session(), GLOBAL_KEY, ev)
        assert es.may_read_event(_Session(groups=[5]), OTHER_KEY, ev)  # accepted CvC clan

    def test_drafts_are_never_public(self):
        assert not es.may_read_event(_Session(), OTHER_KEY, _event(status="draft"))
        assert es.may_read_event(_Session(), GROUP_KEY, _event(status="draft"))


def _team(id, name, score, group_id=None):
    return types.SimpleNamespace(id=id, name=name, score=score, group_id=group_id)


class TestTeams:
    def test_placements_members_and_flags(self, epa):
        s = _Session(
            teams=[_team(3, "Third", 50.0), _team(1, "Alpha", 100.0),
                   _team(2, "Beta", 100.0), _team(4, "Zero", 0.0)],
            members=[(1, 10, "ann"), (1, 99, "hidden"), (2, 20, "Bob"), (3, 30, "Cy")],
        )
        body = es.build_standings(s, _event(), viewer_group_id=299)
        assert body["ranks"] == "teams" and body["event"]["final"] is False
        assert [(t["name"], t["place"], t["score"]) for t in body["teams"]] == [
            ("Alpha", 1, 100), ("Beta", 1, 100), ("Third", 3, 50), ("Zero", None, 0)]
        alpha = body["teams"][0]
        assert alpha["members"] == [{"player_id": 10, "player_name": "ann",
                                     "took_part": True, "in_group": True}]  # hidden left out
        assert body["teams"][1]["members"][0] == {"player_id": 20, "player_name": "Bob",
                                                  "took_part": False, "in_group": True}

    def test_no_in_group_flag_without_a_group_key(self, epa):
        s = _Session(teams=[_team(1, "Alpha", 1.5)], members=[(1, 10, "ann")])
        body = es.build_standings(s, _event(status="past"), viewer_group_id=None)
        assert body["event"]["final"] is True
        assert body["teams"][0]["score"] == 1.5
        assert "in_group" not in body["teams"][0]["members"][0]


class TestIndividualRace:
    def test_players_ranked_with_wom_only_rows_kept(self, epa):
        epa.state["individual"] = True
        body = es.build_standings(_Session(), _event(kind="botw"), viewer_group_id=299)
        assert body["ranks"] == "players" and body["ranking_mode"] == "gained"
        assert body["players"] == [
            {"place": 1, "player_id": 10, "player_name": "Ann", "value": 500,
             "registered": True, "in_group": True},
            {"place": None, "player_id": None, "player_name": "WomOnly", "value": 400,
             "registered": False, "in_group": False},
            {"place": None, "player_id": 20, "player_name": "Bob", "value": 0,
             "registered": True, "in_group": True},
        ]
