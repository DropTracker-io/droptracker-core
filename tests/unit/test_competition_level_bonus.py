"""A SOTW "Reach level N" bonus pays for crossing the level DURING the event,
never for already having it (event 186, 2026-11-22: eleven pre-existing 99
Minings were paid the "Level 99 Mining" bonus in the race's first hour).

``skill_target`` matches a STATE (the envelope's current level), so the gate
lives in handle_envelope: the player's start XP — WOM's window-start value, or
the race's own baseline before this report — must sit below the level.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


comp = _load("services.competition", os.path.join("services", "competition.py"))
if "services" in sys.modules:
    setattr(sys.modules["services"], "competition", comp)
engine = _load("_competition_level_bonus_ut", os.path.join("services", "event_engine.py"))

XP_99 = 13_034_431

CONFIG = {
    "kind": "competition", "metric_kind": "skill", "skill": "mining",
    "format": "individual",
    "ranking": {"mode": "points", "gained_per_point": 10_000},
    "bonus_rules": [
        {"id": 1, "type": "task", "points": 100, "max_awards": 1,
         "label": "Level 99 Mining",
         "task": {"type": "skill_target", "target": "Mining",
                  "target_value": 99, "config": {"progress_notify": "off"}},
         "progress_kind": "count", "need": 1, "kinds": ["experience"],
         "scope": [], "unscoped": False},
    ],
}


def _task(config, task_id=10):
    cfg = comp.CompetitionConfig(config)
    d = {"id": task_id, "type": "competition", "label": "Race",
         "target": config["skill"], "target_value": 0, "points": 0,
         "config": config, "competition": cfg.matcher_index(),
         "kc_npcs": [], "wom_metrics": {}, "requires_confirmation": False,
         "event_id": 1}
    for rule in (d["competition"].get("task_rules") or ()):
        embedded = rule["task"]
        embedded.update(id=task_id, event_id=1)
        embedded.setdefault("points", 0)
        embedded.setdefault("requires_confirmation", False)
        embedded["config"] = engine.parse_task_config(embedded.get("config"))
        engine._enrich_matcher_precompute(embedded)
    return d


class _FakeRedis:
    def __init__(self):
        self.kv = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = str(value)

    def sadd(self, key, member):
        return 1

    def sismember(self, key, member):
        return False

    def expire(self, key, ttl):
        return True

    def exists(self, key):
        return 1 if key in self.kv else 0

    def delete(self, key):
        self.kv.pop(key, None)


def _event():
    return {"id": 1, "name": "SOTW - Mining", "group_id": 126, "kind": "sotw",
            "requires_confirmation": False, "submission_policy": "all",
            "has_bingo": False, "board_size": 5, "bonus_line_points": 0,
            "bonus_blackout_points": 0, "window_start": None, "window_end": None}


def _xp(xp, guid, wom_start=None):
    data = {"skill": "mining", "xp": xp, "level": 99 if xp >= XP_99 else 98}
    env = {"v": 1, "kind": "experience", "guid": guid, "player_id": 5,
           "player_name": "Me", "used_api": True, "data": data}
    if wom_start is not None:
        data.update(xp_start=wom_start, source="wom", target_event_id=1)
        env["source"] = "wom"
    return env


@pytest.fixture
def run(monkeypatch):
    recorded = []

    def fake_record(session, redis_conn, event, task, team_id, player_id,
                    quantity, envelope, cells=None, matched_target=None,
                    path_idx=None, bonus=None, matched_npc=None):
        recorded.append("bonus" if bonus else ("gained", quantity))
        return {"kind": "competition"}

    monkeypatch.setattr(engine, "record_match", fake_record)
    redis = _FakeRedis()
    state = engine.MatcherState(events={1: _event()},
                                tasks_by_event={1: [_task(CONFIG)]},
                                participants={5: [(1, 77, None)]})

    def go(*envelopes):
        for env in envelopes:
            engine.handle_envelope(None, redis, state, env)
        return recorded

    return go


class TestLevelBonusNeedsACrossing:
    def test_already_99_at_the_first_report_earns_nothing(self, run):
        assert run(_xp(50_000_000, "a")) == []

    def test_already_99_keeps_earning_xp_but_never_the_bonus(self, run):
        got = run(_xp(50_000_000, "a"), _xp(50_020_000, "b"))
        assert got == [("gained", 20_000)]

    def test_crossing_99_during_the_race_pays_once_with_its_xp(self, run):
        got = run(_xp(XP_99 - 5_000, "a"), _xp(XP_99 + 1_000, "b"))
        assert got == [("gained", 6_000), "bonus"]

    def test_wom_window_start_below_99_pays(self, run):
        assert "bonus" in run(_xp(XP_99 + 1_000, "w", wom_start=XP_99 - 50_000))

    def test_wom_window_start_already_99_does_not(self, run):
        got = run(_xp(30_000_000, "w", wom_start=29_000_000))
        assert got == [("gained", 1_000_000)]


class TestReachedLevelHelper:
    @pytest.mark.parametrize("start, xp, ok", [
        (None, XP_99, False),
        (XP_99, XP_99 + 1, False),
        (XP_99 - 1, XP_99, True),
        (XP_99 - 1, XP_99 - 1, False),
    ])
    def test_edges(self, start, xp, ok):
        assert engine._reached_level_during_event(99, xp, start) is ok
