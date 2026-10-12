"""Early arrivals on a live event (2026-10-11, event 129).

Event 129 was activated at 01:09 while its start said 14:00, so every
submission before 14:00 was refused as predating the window. At 02:45 an
admin moved the start back to 02:00 — and the 45 minutes of drops in between
(~4.6M GP) were never scored, because the engine only judged them on arrival.
Now the consumer parks a pinned copy of each envelope a live event refused as
too early, and replays the list once the event's window start moves before
the one they were refused against.

Loaded from the file path like test_event_prestart, so the conftest stubs for
db/redis/services never interfere.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_early_ut", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_early_ut"] = engine
_spec.loader.exec_module(engine)

START = datetime(2026, 10, 11, 14, 0, 0)       # the start as it stood
JOINED = datetime(2026, 10, 11, 1, 14, 34)
EARLY = datetime(2026, 10, 11, 2, 20, 0)       # a drop before that start


def _ts(dt):
    return int(dt.timestamp())


def _env(player_id=5, at=EARLY, **kw):
    env = {"v": 1, "kind": "drop", "guid": "g-1", "player_id": player_id,
           "ts": _ts(at), "used_api": True,
           "data": {"item_name": "Zulrah's scales", "quantity": 782,
                    "total_value": 121210}}
    env.update(kw)
    return env


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def call(*a, **k):
            self.ops.append((name, a, k))
            return self
        return call

    def execute(self):
        return [getattr(self.r, name)(*a, **k) for name, a, k in self.ops]


class _FakeRedis:
    def __init__(self):
        self.lists, self.ttl, self.kv = {}, {}, {}
        self.queue_key = engine.QUEUE_KEY

    def pipeline(self):
        return _Pipe(self)

    def rpush(self, key, *vals):
        self.lists.setdefault(key, []).extend(
            v.encode() if isinstance(v, str) else v for v in vals)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        lst = self.lists.get(key, [])
        self.lists[key] = lst[max(len(lst) + start, 0):] if start < 0 else lst[start:]
        return True

    def expire(self, key, secs):
        self.ttl[key] = secs
        return True

    def set(self, key, val, ex=None):
        self.kv[key] = str(val).encode()
        return True

    def get(self, key):
        return self.kv.get(key)

    def rename(self, src, dst):
        if src not in self.lists:
            raise RuntimeError("no such key")
        self.lists[dst] = self.lists.pop(src)

    def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    def delete(self, key):
        self.lists.pop(key, None)
        self.kv.pop(key, None)


def _state(window_start=START, joined_at=JOINED):
    return engine.MatcherState(
        events={129: {"id": 129, "window_start": window_start, "window_end": None}},
        participants={5: [(129, 252, joined_at)]},
    )


def _parked(r, event_id=129):
    return [json.loads(b) for b in r.lists.get(f"events:early:{event_id}", [])]


class TestParkEarly:
    def test_refused_as_too_early_is_parked_pinned(self):
        r = _FakeRedis()
        assert engine.park_early(r, _state(), _env()) == [129]
        [copy] = _parked(r)
        assert copy["only_event_id"] == 129
        assert copy["guid"] == "g-1"
        # Remembers the start it was refused against.
        assert int(r.kv["events:early:129:start"]) == _ts(START)

    def test_inside_the_window_is_not_parked(self):
        r = _FakeRedis()
        assert engine.park_early(r, _state(), _env(at=START + timedelta(minutes=1))) == []
        assert not r.lists

    def test_before_the_members_join_is_not_parked(self):
        r = _FakeRedis()
        assert engine.park_early(r, _state(), _env(at=JOINED - timedelta(minutes=1))) == []

    def test_no_window_start_is_not_parked(self):
        r = _FakeRedis()
        assert engine.park_early(r, _state(window_start=None), _env()) == []

    def test_non_member_and_wom_are_not_parked(self):
        r = _FakeRedis()
        assert engine.park_early(r, _state(), _env(player_id=6)) == []
        assert engine.park_early(r, _state(), _env(source="wom")) == []
        assert engine.park_early(
            r, _state(), _env(data={"source": "wom"})) == []

    def test_copy_pinned_elsewhere_is_skipped(self):
        r = _FakeRedis()
        assert engine.park_early(r, _state(), _env(only_event_id=77)) == []

    def test_retry_counter_is_dropped(self):
        r = _FakeRedis()
        engine.park_early(r, _state(), _env(_attempts=2))
        assert "_attempts" not in _parked(r)[0]


class TestEarlyReplayDue:
    def test_start_moved_earlier_is_due(self):
        moved = START.replace(hour=2)
        assert engine.early_replay_due({129: _ts(START)}, {129: moved}) == [129]

    def test_unchanged_or_later_start_is_not_due(self):
        assert engine.early_replay_due({129: _ts(START)}, {129: START}) == []
        assert engine.early_replay_due(
            {129: _ts(START)}, {129: START + timedelta(hours=1)}) == []

    def test_cleared_start_is_due(self):
        assert engine.early_replay_due({129: _ts(START)}, {129: None}) == [129]

    def test_event_no_longer_live_is_ignored(self):
        assert engine.early_replay_due({129: _ts(START)}, {}) == []


class TestReplayEarlyIfMoved:
    def test_requeues_oldest_first_once_the_start_moves_earlier(self):
        r = _FakeRedis()
        engine.park_early(r, _state(), _env(guid="a", at=EARLY))
        engine.park_early(r, _state(), _env(guid="b", at=EARLY + timedelta(minutes=5)))
        # The admin moves the start back to 02:00; the consumer reloads.
        moved = _state(window_start=START.replace(hour=2))
        assert engine.replay_early_if_moved(r, moved) == {129: 2}
        queued = [json.loads(b)["guid"] for b in r.lists[engine.QUEUE_KEY]]
        # Consumed from the right end: "a" (oldest) is applied first.
        assert queued == ["b", "a"]
        assert "events:early:129" not in r.lists
        assert "events:early:129:start" not in r.kv

    def test_no_move_no_replay(self):
        r = _FakeRedis()
        engine.park_early(r, _state(), _env())
        assert engine.replay_early_if_moved(r, _state()) == {}
        assert len(_parked(r)) == 1

    def test_nothing_parked_is_a_no_op(self):
        assert engine.replay_early_if_moved(_FakeRedis(), _state()) == {}
