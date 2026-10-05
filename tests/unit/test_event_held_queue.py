"""Event credit while the events worker is down (t275).

The producer gate ``events:active`` carries a 180 s TTL that only the events
worker refreshes, so a dead worker used to mean every submission until its
restart was never enqueued for events at all. Producers now park envelopes in
a capped holding list while a sticky marker says there were events to score,
and the worker requeues them, oldest first, once it is back.
"""

import importlib.util
import json
import os
import sys

import pytest

from utils import event_end_hold as eh

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_event_engine_for_held_queue", os.path.join(_ROOT, "services", "event_engine.py"))
engine = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = engine
_spec.loader.exec_module(engine)


class FakeRedis:
    def __init__(self):
        self.kv, self.lists, self.sets = {}, {}, {}

    # keys
    def exists(self, key):
        return 1 if (key in self.kv or key in self.lists or key in self.sets) else 0

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = str(value)
        return True

    def delete(self, key):
        for d in (self.kv, self.lists, self.sets):
            d.pop(key, None)

    def rename(self, src, dst):
        self.lists[dst] = self.lists.pop(src)

    def expire(self, key, ttl):
        return True

    # sets
    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    # lists
    def lpush(self, key, *vals):
        lst = self.lists.setdefault(key, [])
        for v in vals:
            lst.insert(0, v)
        return len(lst)

    def rpush(self, key, *vals):
        lst = self.lists.setdefault(key, [])
        lst.extend(vals)
        return len(lst)

    def ltrim(self, key, start, end):
        lst = self.lists.get(key, [])
        end = len(lst) if end == -1 else end + 1
        self.lists[key] = lst[start:end]
        if not self.lists[key]:
            self.lists.pop(key)

    def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        return lst[start:(len(lst) if end == -1 else end + 1)]

    def pipeline(self):
        outer = self

        class P:
            def __init__(self):
                self.ops = []

            def __getattr__(self, name):
                def call(*a, **k):
                    self.ops.append((name, a, k))
                    return self
                return call

            def execute(self):
                return [getattr(outer, n)(*a, **k) for n, a, k in self.ops]
        return P()


@pytest.fixture()
def redis(monkeypatch):
    import utils.redis as ur

    r = FakeRedis()
    monkeypatch.setattr(ur, "redis_client", type("RC", (), {"client": r})(), raising=False)
    return r


def _push(ts=1000, guid="g"):
    engine.queue_submission("drop", 5, guid, {"item_name": "Twisted bow"}, ts=ts)


class TestProducers:
    def test_gate_up_goes_to_the_live_queue(self, redis):
        engine.set_active_events(redis, [10])
        _push()
        assert len(redis.lists[engine.QUEUE_KEY]) == 1
        assert engine.HELD_KEY not in redis.lists

    def test_gate_down_with_sticky_is_parked(self, redis):
        engine.set_active_events(redis, [10])
        redis.delete(engine.ACTIVE_EVENTS_KEY)      # the TTL lapsed: worker dead
        _push()
        assert engine.QUEUE_KEY not in redis.lists
        held = [json.loads(x) for x in redis.lists[engine.HELD_KEY]]
        assert held[0]["ts"] == 1000 and held[0]["guid"] == "g"

    def test_no_events_at_all_enqueues_nothing(self, redis):
        engine.set_active_events(redis, [10])
        engine.set_active_events(redis, [])         # empty view drops sticky too
        _push()
        assert redis.lists == {}

    def test_overflow_keeps_the_newest_and_flags_it(self, redis, monkeypatch):
        monkeypatch.setattr(engine, "HELD_MAX_ENTRIES", 3)
        redis.set(engine.STICKY_GATE_KEY, 1)
        for i in range(5):
            _push(ts=1000 + i, guid=f"g{i}")
        held = [json.loads(x)["guid"] for x in redis.lists[engine.HELD_KEY]]
        assert held == ["g4", "g3", "g2"]
        assert engine.HELD_OVERFLOW_KEY in redis.kv


class TestRequeue:
    def test_oldest_is_consumed_first(self, redis):
        redis.set(engine.STICKY_GATE_KEY, 1)
        for i in range(5):
            _push(ts=1000 + i, guid=f"g{i}")
        redis.lpush(engine.QUEUE_KEY, json.dumps({"guid": "live"}))
        assert engine.requeue_held(redis, batch=2) == 5
        assert not any(k.startswith(engine.HELD_KEY) for k in redis.lists)
        # The consumer pops from the right.
        popped = [json.loads(x)["guid"] for x in reversed(redis.lists[engine.QUEUE_KEY])]
        assert popped == ["g0", "g1", "g2", "g3", "g4", "live"]

    def test_nothing_held_is_a_no_op(self, redis):
        assert engine.requeue_held(redis) == 0
        assert engine.requeue_held(None) == 0


class TestStallAlert:
    def test_alerts_once_per_outage_and_rearms(self):
        r = FakeRedis()
        eh.write_heartbeat(r, 1000)
        assert eh.check_events_worker(r, 1000 + 60) is None
        assert eh.check_events_worker(r, 1000 + 400) == 400
        assert eh.check_events_worker(r, 1000 + 460) is None    # already said
        eh.write_heartbeat(r, 2000)
        assert eh.check_events_worker(r, 2010) is None          # healthy: re-arms
        assert eh.check_events_worker(r, 2000 + 600) == 600

    def test_never_beaten_or_unreadable_is_silent(self):
        assert eh.check_events_worker(FakeRedis(), 5000) is None

        class Broken:
            def get(self, k):
                raise ConnectionError
        assert eh.check_events_worker(Broken(), 5000) is None
