"""Before/after receipt for a manual event credit (services.event_credit_receipt):
the snapshot reads, the diff shaping, and that a failed read never raises."""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_event_credit_receipt_ut", os.path.join(_ROOT, "services", "event_credit_receipt.py"))
ecr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecr)


class _Q:
    def __init__(self, first=None, scalar=None, boom=False):
        self._first, self._scalar, self._boom = first, scalar, boom

    def filter(self, *a, **k):
        return self

    def first(self):
        if self._boom:
            raise RuntimeError("db down")
        return self._first

    def scalar(self):
        if self._boom:
            raise RuntimeError("db down")
        return self._scalar


class _Session:
    """Answers ``query(col, ...)`` by the first column asked for."""

    def __init__(self, answers):
        self.answers = answers

    def query(self, col, *more):
        return self.answers.get(col, _Q())


@pytest.fixture
def models(monkeypatch):
    m = SimpleNamespace(
        EventTeam=SimpleNamespace(score=object(), name=object(), id=object()),
        EventProgress=SimpleNamespace(progress=object(), completed=object(),
                                      task_id=object(), team_id=object()),
        Player=SimpleNamespace(player_name=object(), player_id=object()),
    )
    monkeypatch.setitem(sys.modules, "db.models", m)
    return m


TASK = {"id": 5, "type": "kc_target", "label": "Kill Zulrah", "config": {}}


def test_capture_reads_team_progress_and_player(models, monkeypatch):
    monkeypatch.setattr(ecr, "_player_event_points", lambda s, e, p: 2.5)
    s = _Session({
        models.EventTeam.score: _Q(first=(120,)),
        models.EventProgress.progress: _Q(first=(40.0, False)),
    })
    snap = ecr.capture(s, event_id=1, task=TASK, team_id=3, player_id=9)
    assert snap == {"team_score": 120.0, "progress": 40.0, "completed": False,
                    "player_value": 2.5, "player_unit": "points"}


def test_capture_no_progress_row_is_zero(models):
    s = _Session({models.EventTeam.score: _Q(first=(0,))})
    snap = ecr.capture(s, event_id=1, task=TASK, team_id=3)
    assert snap["progress"] == 0.0 and snap["completed"] is False
    assert snap["player_value"] is None


def test_capture_swallows_failed_reads(models, monkeypatch):
    def _boom(*a):
        raise RuntimeError("nope")
    monkeypatch.setattr(ecr, "_player_event_points", _boom)
    s = _Session({
        models.EventTeam.score: _Q(boom=True),
        models.EventProgress.progress: _Q(boom=True),
    })
    snap = ecr.capture(s, event_id=1, task=TASK, team_id=3, player_id=9)
    assert snap["team_score"] is None and snap["progress"] is None
    assert snap["player_value"] is None


def test_capture_race_uses_ranked_value(models, monkeypatch):
    monkeypatch.setattr(ecr, "_player_race_value", lambda s, t, p: (1500.0, "xp"))
    race = {"id": 7, "type": "competition", "config": {}}
    snap = ecr.capture(_Session({}), event_id=1, task=race, team_id=3, player_id=9)
    assert snap["player_value"] == 1500.0 and snap["player_unit"] == "xp"


def test_receipt_shapes_deltas():
    before = {"team_score": 100.0, "progress": 45.0, "completed": False,
              "player_value": 0.0}
    after = {"team_score": 110.0, "progress": 50.0, "completed": True,
             "player_value": 5.0, "player_unit": "points"}
    out = ecr.receipt(before, after, team={"id": 3, "name": "Blue"}, task=TASK,
                      player={"id": 9, "name": "Zezima"}, threshold=50.0)
    assert out["team"] == {"id": 3, "name": "Blue", "unit": "points",
                           "score": {"before": 100.0, "after": 110.0, "delta": 10.0}}
    assert out["task"]["progress"] == {"before": 45.0, "after": 50.0, "delta": 5.0}
    assert out["task"]["completed_before"] is False
    assert out["task"]["completed_after"] is True
    assert out["task"]["threshold"] == 50.0
    assert out["player"]["value"] == {"before": 0.0, "after": 5.0, "delta": 5.0}


def test_receipt_unreadable_side_is_none_and_no_player():
    out = ecr.receipt({"team_score": None}, {"team_score": 5.0},
                      team={"id": 3, "name": None}, task=TASK)
    assert out["team"]["score"] is None
    assert out["player"] is None


def test_receipt_rounds_float_delta():
    out = ecr.receipt({"team_score": 0.1}, {"team_score": 0.3},
                      team={"id": 1, "name": "A"}, task=TASK)
    assert out["team"]["score"]["delta"] == 0.2


@pytest.mark.parametrize("cfg,unit", [
    (SimpleNamespace(ranking_mode="points", metric_kind="boss"), "points"),
    (SimpleNamespace(ranking_mode="gained", metric_kind="boss"), "kills"),
    (SimpleNamespace(ranking_mode="gained", metric_kind="skill"), "xp"),
])
def test_race_unit(cfg, unit):
    assert ecr._race_unit(cfg) == unit


def test_score_unit_for_non_race_is_points():
    assert ecr.score_unit_for(TASK) == "points"


def test_threshold_for_race_is_none():
    assert ecr.threshold_for(None, {"type": "competition"}, 1) is None


def test_finish_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(ecr, "capture", _boom)
    assert ecr.finish(None, {}, event_id=1, task=TASK, team_id=1) is None


def test_finish_builds_receipt(models, monkeypatch):
    monkeypatch.setattr(ecr, "capture", lambda *a, **k: {
        "team_score": 12.0, "progress": 1.0, "completed": True,
        "player_value": None, "player_unit": "points"})
    monkeypatch.setattr(ecr, "threshold_for", lambda *a: 1.0)
    s = _Session({models.EventTeam.name: _Q(first=("Red",)),
                  models.Player.player_name: _Q(first=("Lynx Titan",))})
    out = ecr.finish(s, {"team_score": 10.0, "progress": 0.0, "completed": False,
                         "player_value": None},
                     event_id=1, task=TASK, team_id=2, player_id=4)
    assert out["team"]["name"] == "Red"
    assert out["team"]["score"]["delta"] == 2.0
    assert out["player"]["name"] == "Lynx Titan"
    assert out["player"]["value"] is None
