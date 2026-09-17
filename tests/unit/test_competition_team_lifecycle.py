"""Unit tests for the SOTW/BOTW team-race read side in
services/event_lifecycle.py — the team standings read model, the frozen-result
parser, the wrap-up standings, the launch checks, and the averaged-score roster
sync — plus the format-aware scaffold in services/competition_setup.py.

Both modules are loaded by file path with the REAL services/competition
scoring routed in (the conftest stubs the whole ``services`` package); the
database is a small fake keyed on the conftest's db-stub model identities.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


comp = _load("_competition_for_team_lifecycle", "services/competition.py")
lc = _load("_event_lifecycle_for_team_races", "services/event_lifecycle.py")


@pytest.fixture(autouse=True)
def _real_scoring(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.competition", comp)
    setup = _load("_competition_setup_for_team_lifecycle", "services/competition_setup.py")
    monkeypatch.setitem(sys.modules, "services.competition_setup", setup)
    stub_models = sys.modules.get("db.models")
    if stub_models is not None:
        monkeypatch.setattr(stub_models, "COMPETITION_EVENT_KINDS",
                            ("sotw", "botw"), raising=False)
    yield setup


BOTW = {"kind": "competition", "metric_kind": "boss", "npcs": ["Zulrah"],
        "ranking": {"mode": "gained"}}
TEAM_BOTW = {**BOTW, "format": "teams"}


class _Q:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def join(self, *a, **k):
        return self

    def with_for_update(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def count(self):
        return len(self._rows)


class _Session:
    """Answers the handful of reads the code under test makes, told apart by
    the conftest db-stub identities of the queried model/columns."""

    def __init__(self, *, task=None, teams=(), members=(), names=None, row=None):
        self.task = task
        self.teams = list(teams)
        self.members = list(members)       # (team_id, player_id)
        self.names = dict(names or {})
        self.row = row
        self.deleted = []
        self.added = []
        self.commits = 0

    def query(self, model, *cols):
        from db.models import (EventCompetition, EventTask, EventTeam,
                               EventTeamMember, Player)

        if model is EventTask:
            return _Q([self.task] if self.task is not None else [])
        if model is EventTeam:
            return _Q(self.teams)
        if model is EventCompetition:
            return _Q([self.row] if self.row is not None else [])
        if model is EventTeamMember.team_id:
            return _Q(self.members)
        if model is Player.player_id:
            return _Q(self.names.items())
        return _Q([])

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1


def _task(config, task_id=10):
    return SimpleNamespace(id=task_id, type="competition", config=json.dumps(config))


def _row(rid, player_id, qty, team_id, created=None):
    return SimpleNamespace(id=rid, player_id=player_id, quantity=qty, note=None,
                           team_id=team_id, created_at=created, status="auto",
                           matched_target=None, source_type="drop")


def _route_engine(monkeypatch, rows):
    """services.event_engine's two row readers, honouring the team filter."""
    engine = sys.modules["services"].event_engine
    monkeypatch.setattr(engine, "_competition_event_rows",
                        lambda s, task: list(rows), raising=False)
    monkeypatch.setattr(engine, "_competition_applied_rows",
                        lambda s, task, team_id: [r for r in rows if r.team_id == team_id],
                        raising=False)
    return engine


class TestTeamStandingsReadModel:
    def test_teams_and_players_come_back_attributed(self, monkeypatch):
        teams = [SimpleNamespace(id=1, name="Red", color="#f00", score=0),
                 SimpleNamespace(id=2, name="Blue", color=None, score=0)]
        rows = [
            _row(1, 5, 10, 1, datetime(2026, 9, 1)),
            _row(2, 6, 4, 1, datetime(2026, 9, 1)),
            _row(3, 7, 8, 2, datetime(2026, 9, 2)),
            # Player 9 scored for Red, then left every team.
            _row(4, 9, 3, 1, datetime(2026, 9, 3)),
        ]
        _route_engine(monkeypatch, rows)
        session = _Session(task=_task(TEAM_BOTW), teams=teams,
                           members=[(1, 5), (1, 6), (2, 7), (2, 8)],
                           names={5: "Alice", 6: "Bob", 7: "Cara", 9: "Gone"})
        event = SimpleNamespace(id=3, kind="botw", status="active")
        data = lc.competition_standings(session, event)

        assert data["team"] is None and data["config"].is_team_race
        by_player = {r["player_name"]: r for r in data["players"]}
        assert by_player["Alice"]["team_name"] == "Red"
        assert by_player["Cara"]["team_name"] == "Blue"
        # A departed contributor stays under the team they scored for.
        assert by_player["Gone"]["team_id"] == 1
        assert [r["rank"] for r in data["players"]][:1] == [1]

        red, blue = sorted(data["teams"], key=lambda t: t["team_id"])
        assert (red["total"], red["members"]) == (17, 3)   # 5, 6 + departed 9
        assert (blue["total"], blue["members"]) == (8, 2)  # 8 is on the roster
        assert red["rank"] == 1 and red["top_player"]["player_name"] == "Alice"

    def test_an_individual_race_keeps_its_old_shape(self, monkeypatch):
        rows = [_row(1, 5, 10, 1)]
        _route_engine(monkeypatch, rows)
        team = SimpleNamespace(id=1, name="Participants", color=None, score=10)
        session = _Session(task=_task(BOTW), teams=[team], names={5: "Alice"})
        data = lc.competition_standings(session, SimpleNamespace(id=3))
        assert data["teams"] == [] and data["team"] is team
        assert data["players"][0]["player_name"] == "Alice"
        ranked, config, roster_team = lc._competition_ranked_rows(session, SimpleNamespace(id=3))
        assert roster_team is team and ranked[0]["gained"] == 10


class TestFrozenStandings:
    def test_both_shapes_parse(self):
        assert lc.frozen_competition_standings(json.dumps([{"rank": 1}])) == {
            "players": [{"rank": 1}], "teams": []}
        frozen = json.dumps({"format": "teams", "players": [{"rank": 1}],
                             "teams": [{"team_id": 2}]})
        assert lc.frozen_competition_standings(frozen) == {
            "players": [{"rank": 1}], "teams": [{"team_id": 2}]}

    def test_absent_or_unreadable_falls_back(self):
        for raw in (None, "", "not json", json.dumps({"teams": []}), json.dumps(3)):
            assert lc.frozen_competition_standings(raw) is None


class TestWrapUpStandings:
    def test_a_team_race_lists_teams_with_worded_scores(self, monkeypatch):
        cfg = comp.CompetitionConfig({**TEAM_BOTW, "team_scoring": "average"})
        monkeypatch.setattr(lc, "competition_standings", lambda s, ev: {
            "players": [], "config": cfg, "team": None, "task": None,
            "teams": [{"team_id": 2, "name": "Blue", "score": 12.5},
                      {"team_id": 1, "name": "Red", "score": 3}]})
        rows = lc._competition_final_standings(None, SimpleNamespace(id=1), 5)
        assert rows == [
            {"team_id": 2, "name": "Blue", "score": 12.5,
             "score_text": "12.5 KC per member"},
            {"team_id": 1, "name": "Red", "score": 3, "score_text": "3 KC per member"},
        ]


class TestLaunchChecks:
    def _codes(self, config, team_count, mode="standard"):
        teams = [SimpleNamespace(id=i + 1, name=f"T{i}") for i in range(team_count)]
        session = _Session(task=_task(config), teams=teams,
                           row=SimpleNamespace(source_mode="hosted",
                                               wom_competition_id=None,
                                               wom_competition_code=None))
        event = SimpleNamespace(id=4, kind="botw", mode=mode, has_bingo=False,
                                ends_at=None, starts_at=None, schedule_config=None,
                                status="draft")
        return {b["code"] for b in lc.activation_blocker_items(session, event)}

    def test_a_team_race_needs_two_teams(self):
        assert "competition_needs_teams" in self._codes(TEAM_BOTW, 1)
        assert "competition_needs_teams" not in self._codes(TEAM_BOTW, 2)
        # One message for "no teams yet", with the race's number in it.
        none = self._codes(TEAM_BOTW, 0)
        assert "competition_needs_teams" in none and "no_teams" not in none
        # An individual race keeps the generic one.
        assert "no_teams" in self._codes(BOTW, 0)

    def test_an_individual_race_needs_exactly_one_roster(self):
        assert "competition_extra_teams" in self._codes(BOTW, 2)
        assert not self._codes(BOTW, 1)

    def test_races_are_clan_locked(self):
        assert "competition_clan_vs_clan" in self._codes(TEAM_BOTW, 2, mode="clan_vs_clan")


class TestAveragedRosterSync:
    class _Redis:
        def __init__(self):
            self.store = {}

        def get(self, key):
            return self.store.get(key)

        def set(self, key, value, ex=None):
            self.store[key] = value

    def test_recomputes_once_per_roster_change(self, monkeypatch):
        engine = sys.modules["services"].event_engine
        calls, frames = [], []
        monkeypatch.setattr(engine, "recompute_competition_teams",
                            lambda s, eid: calls.append(eid) or {1: 2.5}, raising=False)
        monkeypatch.setattr(lc, "_publish", lambda eid, frame: frames.append(frame))
        redis = self._Redis()
        session = _Session(task=_task({**TEAM_BOTW, "team_scoring": "average"}),
                           members=[(1, 5), (1, 6)])
        event = SimpleNamespace(id=8)

        assert lc.sync_averaged_team_scores(session, redis, event) is True
        assert calls == [8] and frames[0]["kind"] == "recompute"
        # Same rosters: skipped.
        assert lc.sync_averaged_team_scores(session, redis, event) is False
        assert calls == [8]
        # Somebody joined: recomputed again.
        session.members.append((1, 7))
        lc.sync_averaged_team_scores(session, redis, event)
        assert calls == [8, 8]

    def test_totals_and_individual_races_are_left_alone(self, monkeypatch):
        engine = sys.modules["services"].event_engine
        calls = []
        monkeypatch.setattr(engine, "recompute_competition_teams",
                            lambda s, eid: calls.append(eid), raising=False)
        for cfg in (TEAM_BOTW, BOTW):
            assert lc.sync_averaged_team_scores(
                _Session(task=_task(cfg), members=[(1, 5)]), None,
                SimpleNamespace(id=8)) is False
        assert calls == []


class TestScaffold:
    def test_a_team_race_builds_no_roster_team(self, _real_scoring):
        setup = _real_scoring
        event = SimpleNamespace(id=5, group_id=9, formation_mode="self_join")
        session = _Session()
        out = setup.ensure_competition_scaffold(session, event, TEAM_BOTW)
        assert out["team"] is None
        assert "team" not in out["created"] and "task" in out["created"]
        # The organiser's formation mode stands.
        assert event.formation_mode == "self_join"

    def test_an_individual_race_still_builds_its_roster(self, _real_scoring, monkeypatch):
        from unittest.mock import MagicMock

        class _Team(SimpleNamespace):
            # Column stand-ins for the query expressions the scaffold builds.
            id = MagicMock()
            event_id = MagicMock()

        monkeypatch.setattr(sys.modules["db.models"], "EventTeam", _Team)
        setup = _real_scoring
        event = SimpleNamespace(id=5, group_id=9, formation_mode="self_join")
        session = _Session()
        out = setup.ensure_competition_scaffold(session, event, BOTW)
        assert "team" in out["created"]
        assert out["team"].auto_clan is True and out["team"].group_id == 9
        assert event.formation_mode == "admin_assign"

    def test_switching_to_teams_retires_an_empty_roster_scaffold(self, _real_scoring,
                                                                 monkeypatch):
        setup = _real_scoring
        roster = SimpleNamespace(id=1, name="Participants", auto_clan=True, group_id=9)
        session = _Session(task=_task(BOTW), teams=[roster])
        event = SimpleNamespace(id=5, group_id=9, formation_mode="admin_assign")
        out = setup.ensure_competition_scaffold(session, event, TEAM_BOTW)
        assert out["team"] is None and roster in session.deleted

    def test_a_later_save_never_touches_an_organisers_team(self, _real_scoring):
        setup = _real_scoring
        # Already a team race: a team the organiser happened to call
        # "Participants" survives every settings save.
        mine = SimpleNamespace(id=1, name="Participants", auto_clan=False, group_id=None)
        session = _Session(task=_task(TEAM_BOTW), teams=[mine])
        event = SimpleNamespace(id=5, group_id=9, formation_mode="self_join")
        setup.ensure_competition_scaffold(session, event, TEAM_BOTW)
        assert session.deleted == []

    def test_race_format_reads_stored_configs(self, _real_scoring):
        setup = _real_scoring
        assert setup.race_format(json.dumps(TEAM_BOTW)) == "teams"
        assert setup.race_format(json.dumps(BOTW)) == "individual"
        assert setup.race_format("not json") == "individual"
        assert setup.race_format(None) == "individual"
