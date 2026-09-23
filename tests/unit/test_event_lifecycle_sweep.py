"""Unit tests for the pure scheduler-sweep decision logic (Task 21).

Loaded directly from the file path (like test_event_engine_matcher.py) so the
conftest sys.modules stubs for db/services never interfere — the module's
top-level imports are stdlib-only by design.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_lifecycle.py",
)
_spec = importlib.util.spec_from_file_location("_event_lifecycle_under_test", _MODULE_PATH)
lc = importlib.util.module_from_spec(_spec)
sys.modules["_event_lifecycle_under_test"] = lc
_spec.loader.exec_module(lc)

NOW = datetime(2026, 7, 5, 12, 0, 0)
PAST = NOW - timedelta(hours=1)
FUTURE = NOW + timedelta(hours=1)


def ev(id, status, starts_at=None, ends_at=None):
    return {"id": id, "status": status, "starts_at": starts_at, "ends_at": ends_at}


class TestSweepDue:
    def test_empty(self):
        assert lc.sweep_due([], NOW) == {"activate": [], "end": []}
        assert lc.sweep_due(None, NOW) == {"activate": [], "end": []}

    def test_draft_with_past_start_activates(self):
        due = lc.sweep_due([ev(1, "draft", starts_at=PAST)], NOW)
        assert due == {"activate": [1], "end": []}

    def test_draft_start_exactly_now_activates(self):
        due = lc.sweep_due([ev(1, "draft", starts_at=NOW)], NOW)
        assert due["activate"] == [1]

    def test_draft_with_future_start_waits(self):
        due = lc.sweep_due([ev(1, "draft", starts_at=FUTURE)], NOW)
        assert due == {"activate": [], "end": []}

    def test_unscheduled_draft_never_auto_activates(self):
        due = lc.sweep_due([ev(1, "draft")], NOW)
        assert due == {"activate": [], "end": []}

    def test_active_with_past_end_ends(self):
        due = lc.sweep_due([ev(2, "active", ends_at=PAST)], NOW)
        assert due == {"activate": [], "end": [2]}

    def test_active_with_future_or_no_end_keeps_running(self):
        due = lc.sweep_due(
            [ev(2, "active", ends_at=FUTURE), ev(3, "active")], NOW)
        assert due == {"activate": [], "end": []}

    def test_past_events_ignored(self):
        # One-way lifecycle: past rows never transition again, even with
        # stale-looking dates.
        due = lc.sweep_due(
            [ev(4, "past", starts_at=PAST, ends_at=PAST)], NOW)
        assert due == {"activate": [], "end": []}

    def test_draft_end_date_alone_never_ends_a_draft(self):
        # A draft whose window fully elapsed activates (then the *next* pass
        # of the same tick would end it — but activation itself is blocked by
        # the ends_at-in-the-past validation, which notifies the admin).
        due = lc.sweep_due([ev(5, "draft", ends_at=PAST)], NOW)
        assert due == {"activate": [], "end": []}

    def test_mixed_batch(self):
        due = lc.sweep_due(
            [
                ev(1, "draft", starts_at=PAST),
                ev(2, "draft", starts_at=FUTURE),
                ev(3, "active", ends_at=PAST),
                ev(4, "active"),
                ev(5, "past", ends_at=PAST),
                ev(6, "draft", starts_at=PAST, ends_at=FUTURE),
            ],
            NOW,
        )
        assert due == {"activate": [1, 6], "end": [3]}


class TestBoardFinishRanking:
    """W1: board-game standings must rank by who reached the finish tile, then
    the tiebreak (default score) — NOT by task score alone, which could crown
    (and pay the prize pot to) a team that never finished."""

    @staticmethod
    def _team(id, score=0, coins=0, name=None):
        return SimpleNamespace(id=id, name=name or f"T{id}", score=score, coins=coins)

    @staticmethod
    def _pos(status="active", tile=0):
        return SimpleNamespace(status=status, tile_idx=tile)

    def test_finisher_beats_higher_score(self):
        # T1 finished at the finish tile with a LOWER task score; T2 has a
        # higher score but is still mid-board. T1 must rank first.
        teams = [self._team(1, score=10), self._team(2, score=999)]
        positions = {1: self._pos("finished", 20), 2: self._pos("active", 15)}
        ranked = lc._rank_board_teams(teams, positions, ["score"])
        assert [t.id for t in ranked] == [1, 2]

    def test_tie_between_finishers_breaks_by_score(self):
        teams = [self._team(1, score=30), self._team(2, score=80)]
        positions = {1: self._pos("finished", 20), 2: self._pos("finished", 20)}
        ranked = lc._rank_board_teams(teams, positions, ["score"])
        assert [t.id for t in ranked] == [2, 1]   # both finished → higher score wins

    def test_unfinished_ranked_by_progress_then_tiebreak(self):
        teams = [self._team(1, score=5), self._team(2, score=5), self._team(3, score=99)]
        positions = {1: self._pos("active", 12), 2: self._pos("active", 3),
                     3: self._pos("active", 12)}
        ranked = lc._rank_board_teams(teams, positions, ["score"])
        # tile 12 teams (1,3) outrank tile 3 (team 2); among the two, higher score first.
        assert [t.id for t in ranked] == [3, 1, 2]

    def test_coins_tiebreak_when_configured(self):
        teams = [self._team(1, score=5, coins=200), self._team(2, score=5, coins=50)]
        positions = {1: self._pos("finished", 9), 2: self._pos("finished", 9)}
        ranked = lc._rank_board_teams(teams, positions, ["coins", "score"])
        assert [t.id for t in ranked] == [1, 2]   # more coins wins the tie


class TestLifecycleError:
    def test_carries_status_title_detail(self):
        err = lc.LifecycleError(409, "Already active", "This event is already active.")
        assert err.status == 409
        assert err.title == "Already active"
        assert err.detail == "This event is already active."
        assert str(err) == "This event is already active."


# ── Auto-start heads-up (leader DM a day before a draft starts on its own) ──

class _NXRedis:
    def __init__(self):
        self.keys = set()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


def _heads_up_harness(monkeypatch):
    import types

    alerts = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
        "_event_alerts_for_sweep",
        os.path.join(os.path.dirname(_MODULE_PATH), "event_alerts.py")))
    alerts.__spec__.loader.exec_module(alerts)
    calls = []
    monkeypatch.setattr(alerts, "enqueue_autostart_heads_up",
                        lambda session, event, starts_at, blockers=None:
                        calls.append((event.id, starts_at, blockers)) or 1)
    pkg = sys.modules.get("services") or types.ModuleType("services")
    monkeypatch.setitem(sys.modules, "services", pkg)
    monkeypatch.setitem(sys.modules, "services.event_alerts", alerts)
    monkeypatch.setattr(pkg, "event_alerts", alerts, raising=False)
    monkeypatch.setattr(lc, "activation_blocker_items",
                        lambda session, event, now=None: [{"message": "No teams yet"}])
    return calls


def test_autostart_heads_up_fires_once_per_start_time(monkeypatch):
    calls = _heads_up_harness(monkeypatch)
    redis = _NXRedis()
    session = SimpleNamespace(rollback=lambda: None)
    now = datetime(2026, 9, 23, 12, 0)
    event = SimpleNamespace(id=5, status="draft", starts_at=now + timedelta(hours=3))

    lc._autostart_heads_up(session, redis, event, now)
    lc._autostart_heads_up(session, redis, event, now + timedelta(minutes=1))
    assert len(calls) == 1
    assert calls[0][2] == ["No teams yet"]

    # Moving the start date re-arms it for the new time.
    event.starts_at = now + timedelta(hours=5)
    lc._autostart_heads_up(session, redis, event, now + timedelta(minutes=2))
    assert len(calls) == 2


def test_autostart_heads_up_waits_for_the_window(monkeypatch):
    calls = _heads_up_harness(monkeypatch)
    now = datetime(2026, 9, 23, 12, 0)
    session = SimpleNamespace(rollback=lambda: None)
    far = SimpleNamespace(id=6, status="draft", starts_at=now + timedelta(days=3))
    undated = SimpleNamespace(id=7, status="draft", starts_at=None)
    lc._autostart_heads_up(session, _NXRedis(), far, now)
    lc._autostart_heads_up(session, _NXRedis(), undated, now)
    # No Redis: skipped rather than risk a DM every tick.
    soon = SimpleNamespace(id=8, status="draft", starts_at=now + timedelta(hours=1))
    lc._autostart_heads_up(session, None, soon, now)
    assert calls == []
