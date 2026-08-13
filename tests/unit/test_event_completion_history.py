"""Completion-history classification (t54): "completed" is not "scored".

``EventTask.points`` defaults to 0 and zero-point tasks are legal and common —
a bingo board scored purely by line/blackout bonuses uses them throughout. The
timeline's default view (``mode=completions``) therefore cannot infer
completion from credited points: it reads the ``completed`` flag
:func:`_history_fold` sets on exactly the row that crossed the task's
threshold (and on every matched loot_sweep receipt).

The real ``services.event_engine`` / ``services.loot_sweep`` are loaded under
private names and swapped into ``sys.modules`` around each test (the
test_plugin_notifications idiom) — the conftest stubs the ``services`` package,
and other test modules register mocks under those dotted names.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import web_api.routes.events as evr
from tests.unit.test_event_auth_modes import _S, _SessionCM

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(_ROOT, *path_parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_real_loot_sweep = _load("_loot_sweep_for_history_test", "services", "loot_sweep.py")
_real_engine = _load("_event_engine_for_history_test", "services", "event_engine.py")


@pytest.fixture(autouse=True)
def _real_service_modules():
    saved = {name: sys.modules.get(name)
             for name in ("services.event_engine", "services.loot_sweep")}
    sys.modules["services.event_engine"] = _real_engine
    sys.modules["services.loot_sweep"] = _real_loot_sweep
    yield
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _task(task_id=1, *, task_type="kc_target", points=0, target_value=3, config=None,
          label="Kill 3 Vorkath", visibility="public"):
    return SimpleNamespace(
        id=task_id, event_id=1, type=task_type, label=label, target="Vorkath",
        target_value=target_value, points=points, requires_confirmation=False,
        config=config, difficulty=None, visibility=visibility,
    )


_T0 = datetime(2026, 8, 1, 12, 0, 0)


def _row(row_id, *, task_id=1, team_id=1, quantity=1, player_id=7,
         matched_target=None, source_type="drop", status="auto", note=None):
    return SimpleNamespace(
        id=row_id, task_id=task_id, team_id=team_id, player_id=player_id,
        quantity=quantity, matched_target=matched_target, source_type=source_type,
        status=status, note=note, proof_url=None,
        created_at=_T0 + timedelta(minutes=row_id),
    )


# --------------------------------------------------------------------------- #
# _history_fold: points and completion are tracked separately
# --------------------------------------------------------------------------- #
class TestZeroPointTaskCompletes:
    def test_the_crossing_row_is_flagged_even_though_it_scores_nothing(self):
        # The defect: a 0-point tile's finishing row priced at 0.0 and was
        # dropped from the default view while the board visibly filled up.
        rows = [_row(i) for i in (1, 2, 3, 4)]
        points, completed = evr._history_fold({1: _task(points=0)}, rows)
        assert set(points.values()) == {0.0}
        assert completed == {3}

    def test_intermediate_and_later_rows_stay_progress(self):
        rows = [_row(i) for i in (1, 2, 3, 4)]
        _, completed = evr._history_fold({1: _task(points=0)}, rows)
        assert [r.id in completed for r in rows] == [False, False, True, False]

    def test_each_team_completes_on_its_own_crossing_row(self):
        rows = [_row(1, team_id=1), _row(2, team_id=2), _row(3, team_id=1),
                _row(4, team_id=2), _row(5, team_id=1), _row(6, team_id=2)]
        _, completed = evr._history_fold({1: _task(points=0)}, rows)
        assert completed == {5, 6}

    def test_quantity_carries_a_team_over_in_one_row(self):
        _, completed = evr._history_fold({1: _task(points=0)}, [_row(1, quantity=5)])
        assert completed == {1}

    def test_a_scoring_task_is_unchanged(self):
        rows = [_row(i) for i in (1, 2, 3, 4)]
        points, completed = evr._history_fold({1: _task(points=50)}, rows)
        assert points == {1: 0.0, 2: 0.0, 3: 50.0, 4: 0.0}
        assert completed == {3}

    def test_an_unknown_task_completes_nothing(self):
        points, completed = evr._history_fold({}, [_row(1)])
        assert points == {1: 0.0}
        assert completed == set()


class TestLootSweepReceipts:
    """A loot_sweep receipt is a discrete acquisition, not a tick toward a
    threshold — decay pricing a duplicate at 0 must not demote it."""

    CONFIG = {"groups": [{"label": "Vorkath", "npcs": ["Vorkath"], "items": [
        {"item_name": "Draconic visage", "points": 10, "max_awards": 1},
    ]}]}

    def _task(self):
        return _task(task_type="loot_sweep", points=0, target_value=0,
                     config=self.CONFIG)

    def test_a_scoring_receipt_completes(self):
        rows = [_row(1, matched_target="Draconic visage")]
        points, completed = evr._history_fold({1: self._task()}, rows)
        assert points[1] == 10
        assert completed == {1}

    def test_a_decayed_zero_point_duplicate_still_completes(self):
        rows = [_row(1, matched_target="Draconic visage"),
                _row(2, matched_target="Draconic visage")]
        points, completed = evr._history_fold({1: self._task()}, rows)
        assert points[2] == 0        # capped by max_awards
        assert completed == {1, 2}

    def test_a_row_matching_no_configured_item_is_not_a_completion(self):
        rows = [_row(1, matched_target="Bones"), _row(2, matched_target=None)]
        _, completed = evr._history_fold({1: self._task()}, rows)
        assert completed == set()


# --------------------------------------------------------------------------- #
# _is_completion_entry / _collapse_progress
# --------------------------------------------------------------------------- #
class TestIsCompletionEntry:
    def test_zero_point_completion_counts(self):
        assert evr._is_completion_entry(
            {"points": 0, "completed": True, "source_type": "drop"}) is True

    def test_progress_tick_does_not(self):
        assert evr._is_completion_entry(
            {"points": 0, "completed": False, "source_type": "drop"}) is False

    @pytest.mark.parametrize("source", ["manual", "bonus"])
    def test_admin_awards_always_count(self, source):
        assert evr._is_completion_entry(
            {"points": 0, "completed": False, "source_type": source}) is True

    def test_a_zero_point_completion_is_never_folded_into_a_run(self):
        entries = [  # newest-first, as the route hands them over
            {"completion_id": 4, "task_id": 1, "task_type": "kc_target", "team_id": 1,
             "player_id": 7, "points": 0, "completed": False, "quantity": 1},
            {"completion_id": 3, "task_id": 1, "task_type": "kc_target", "team_id": 1,
             "player_id": 7, "points": 0, "completed": True, "quantity": 1},
            {"completion_id": 2, "task_id": 1, "task_type": "kc_target", "team_id": 1,
             "player_id": 7, "points": 0, "completed": False, "quantity": 1},
            {"completion_id": 1, "task_id": 1, "task_type": "kc_target", "team_id": 1,
             "player_id": 7, "points": 0, "completed": False, "quantity": 1},
        ]
        out = evr._collapse_progress(entries)
        assert [(e["completion_id"], e.get("collapsed")) for e in out] == [
            (4, None), (3, None), (2, 2),
        ]


# --------------------------------------------------------------------------- #
# GET /events/{id}/completions/history?mode=completions
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, tasks, rows):
    """Script the reads `_load` issues on the teamId-filtered path (which skips
    the Redis cache): event, tasks, ledger rows, team names, player names."""
    session = _S([SimpleNamespace(id=1, kind="standard")], tasks, rows,
                 [(1, "Team A")], [(7, "Zezima", False)])
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "optional_user_id", lambda: None)
    monkeypatch.setattr(evr, "_is_restricted", lambda ev: False)
    monkeypatch.setattr(evr, "_is_event_admin", lambda *a, **k: False)
    monkeypatch.setattr(evr, "hidden_player_ids", lambda: set())


class TestHistoryModeCompletions:
    async def test_a_zero_point_completion_appears_in_the_default_view(
            self, client, monkeypatch):
        _wire(monkeypatch, [_task(points=0)], [_row(i) for i in (1, 2, 3, 4)])
        r = await client.get(
            "/api/v1/events/1/completions/history?mode=completions&teamId=1")
        assert r.status_code == 200
        body = await r.get_json()
        assert [e["completion_id"] for e in body["entries"]] == [3]
        assert body["entries"][0]["points"] == 0
        assert body["entries"][0]["completed"] is True
        assert body["meta"]["completions_total"] == 1
        assert body["meta"]["progress_total"] == 3

    async def test_progress_mode_keeps_the_ticks(self, client, monkeypatch):
        _wire(monkeypatch, [_task(points=0)], [_row(i) for i in (1, 2, 3, 4)])
        r = await client.get(
            "/api/v1/events/1/completions/history?mode=progress&teamId=1&collapse=0")
        body = await r.get_json()
        assert [e["completion_id"] for e in body["entries"]] == [4, 2, 1]
