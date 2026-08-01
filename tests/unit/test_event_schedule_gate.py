"""The scoring gate for recurring-schedule events (web82a): which submissions
count, and how per-window baselines keep closed-period activity out.

The engine module is loaded directly from its file path (its module-level
imports are stdlib + sqlalchemy.exc only) so the conftest sys.modules stubs
for db/redis/services never interfere.
"""

import importlib.util
import os
import sys
from datetime import datetime

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_schedule_under_test",
                                               _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_schedule_under_test"] = engine
_spec.loader.exec_module(engine)

# Two August weekends, the canonical "runs every weekend for a month" shape.
WEEKENDS = [
    (datetime(2026, 8, 1), datetime(2026, 8, 3)),
    (datetime(2026, 8, 8), datetime(2026, 8, 10)),
]


def _event(**kw):
    base = {"id": 10, "name": "E", "window_start": datetime(2026, 8, 1),
            "window_end": datetime(2026, 9, 1), "scheduled": True,
            "windows": list(WEEKENDS)}
    base.update(kw)
    return base


class TestWindowSeq:
    def test_inside_a_window_returns_its_sequence(self):
        assert engine.schedule_window_seq(_event(), datetime(2026, 8, 2)) == 0
        assert engine.schedule_window_seq(_event(), datetime(2026, 8, 9)) == 1

    def test_between_windows_is_closed(self):
        # A Wednesday drop on a weekends-only event credits nothing — the whole
        # point of the feature.
        assert engine.schedule_window_seq(_event(), datetime(2026, 8, 5)) is None
        assert not engine.schedule_open(_event(), datetime(2026, 8, 5))

    def test_boundaries_are_half_open(self):
        assert engine.schedule_window_seq(_event(), datetime(2026, 8, 1)) == 0
        # The closing instant belongs to the gap: no timestamp can ever land in
        # two windows and be credited twice.
        assert engine.schedule_window_seq(_event(), datetime(2026, 8, 3)) is None

    def test_continuous_events_are_always_open(self):
        ev = _event(scheduled=False, windows=[])
        assert engine.schedule_window_seq(ev, datetime(2026, 8, 5)) == engine.CONTINUOUS
        assert engine.schedule_open(ev, datetime(2026, 8, 5))

    def test_missing_windows_key_is_continuous(self):
        # Defensive: a scheduled event whose windows failed to load must keep
        # scoring rather than silently freeze an entire event.
        assert engine.schedule_window_seq({"id": 1}, datetime(2026, 8, 5)) \
            == engine.CONTINUOUS


class TestWindowScope:
    def test_each_window_gets_its_own_state_suffix(self):
        assert engine.window_scope(0) == ":w0"
        assert engine.window_scope(1) == ":w1"

    def test_continuous_events_keep_their_historical_keys(self):
        # Existing events must not have their KC watermarks / XP baselines
        # renamed out from under them by this feature.
        assert engine.window_scope(engine.CONTINUOUS) == ""
        assert engine.window_scope(None) == ""

    def test_scopes_isolate_watermarks_between_windows(self):
        base = "7:npc"
        assert base + engine.window_scope(0) != base + engine.window_scope(1)


class TestWindowStartFor:
    def test_uses_the_sub_window_start(self):
        # WOM seeding compares the player's join time against the boundary that
        # actually matters — the scoring window, not the whole event.
        assert engine._window_start_for(_event(), 1) == datetime(2026, 8, 8)

    def test_falls_back_to_the_event_window(self):
        assert engine._window_start_for(_event(), engine.CONTINUOUS) \
            == datetime(2026, 8, 1)
        assert engine._window_start_for(_event(), 99) == datetime(2026, 8, 1)


class TestPerWindowBaselines:
    """A boss killed while scoring is closed must not credit when it reopens.

    The watermark stores an absolute counter, so without per-window scoping the
    first submission of weekend two would fold in every weekday kill.
    """

    class _FakeRedis:
        def __init__(self):
            self.store = {}

        def get(self, key):
            return self.store.get(key)

        def set(self, key, value, ex=None):
            self.store[key] = value

        def delete(self, key):
            self.store.pop(key, None)

        def incr(self, key):
            self.store[key] = int(self.store.get(key, 0)) + 1

        def expire(self, key, ttl):
            pass

        def smembers(self, key):
            return set()

    def _fold(self, r, scope, kc):
        return engine._fold_kc_watermark(r, 10, scope, 5, kc,
                                         first_credit_offset=1)

    def test_closed_period_kills_do_not_credit_at_reopen(self):
        r = self._FakeRedis()
        # Weekend one: kill count 100 -> first credit is the kill itself.
        assert self._fold(r, "1" + engine.window_scope(0), 100) == 1
        assert self._fold(r, "1" + engine.window_scope(0), 105) == 5
        # Weekday grinding takes them to 200 while scoring is closed; the gate
        # drops those submissions entirely, so nothing is folded.
        # Weekend two: a fresh baseline means only THIS window's kills count.
        assert self._fold(r, "1" + engine.window_scope(1), 200) == 1
        assert self._fold(r, "1" + engine.window_scope(1), 203) == 3

    def test_continuous_events_still_fold_across_the_whole_event(self):
        r = self._FakeRedis()
        scope = "1" + engine.window_scope(engine.CONTINUOUS)
        assert self._fold(r, scope, 100) == 1
        assert self._fold(r, scope, 150) == 50
