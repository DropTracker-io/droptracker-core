"""Replay of edge-captured submissions (scripts/drain_r2_spool.py).

The edge Worker answers 200 for a submission the origin could not take, on the
strength of having written the raw body to R2. That promise is only kept if the
body is later put back through the intake, so the contract these tests pin down
is about not losing an object:

  * an object leaves R2 only after the intake has accepted it;
  * a body the intake will never accept is set aside, not deleted and not
    retried forever;
  * anything else defers, and the object stays exactly where it is.

Dead-letter replay has the same shape one level down: push onto the queue
first, remove from the dead list second, because a crash between the two must
re-deliver rather than lose (GUID dedup absorbs the repeat).
"""

import importlib
import sys
import types

import pytest


@pytest.fixture()
def drain(monkeypatch):
    # boto3/requests/redis are imported lazily inside the functions, so the
    # module itself imports cleanly with nothing stubbed.
    mod = importlib.import_module("scripts.drain_r2_spool")
    mod = importlib.reload(mod)
    # Never reach the box's real Redis: the recovery marker holds live event
    # ends (t274). Tests that care install a FakeStore.
    monkeypatch.setattr(mod, "_recovery_redis", lambda: None)
    return mod


class FakeR2:
    """Minimal stand-in for the S3 client surface the script actually uses."""

    def __init__(self, objects):
        self.objects = dict(objects)  # key -> (body, content_type, guid)
        self.deleted = []
        self.copied = []

    def get_paginator(self, _op):
        outer = self

        class P:
            def paginate(self, **_kw):
                yield {"Contents": [{"Key": k} for k in sorted(outer.objects)]}

        return P()

    def get_object(self, Bucket, Key):
        body, ctype, guid = self.objects[Key]

        class B:
            def read(self_inner):
                return body

        return {"Body": B(), "ContentType": ctype, "Metadata": {"guid": guid}}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def copy_object(self, Bucket, Key, CopySource):
        self.copied.append((CopySource["Key"], Key))


def _args(**over):
    base = dict(apply=True, source="r2", limit=500, rate=0, prefix="webhook/",
                intake="http://127.0.0.1:31323", skip_health_check=True,
                workers=1, max_seconds=0, max_queue_depth=0)
    base.update(over)
    return types.SimpleNamespace(**base)


def _obj(n):
    return (f"body-{n}".encode(), "multipart/form-data; boundary=xyz", f"guid-{n}")


class TestR2Drain:
    def test_object_is_deleted_only_after_a_200(self, drain, monkeypatch):
        r2 = FakeR2({"webhook/2026/08/21/00/1-a.bin": _obj(1)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (200, "Queued"))

        drain.drain_r2(_args())
        assert r2.deleted == ["webhook/2026/08/21/00/1-a.bin"]

    def test_a_deferred_status_leaves_the_object_in_place(self, drain, monkeypatch):
        r2 = FakeR2({"webhook/2026/08/21/00/1-a.bin": _obj(1)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (503, "unavailable"))

        drain.drain_r2(_args())
        assert r2.deleted == []
        assert "webhook/2026/08/21/00/1-a.bin" in r2.objects

    def test_a_still_sick_intake_stops_the_pass_rather_than_burning_it(
            self, drain, monkeypatch):
        keys = {f"webhook/2026/08/21/00/{i}-a.bin": _obj(i) for i in range(1, 6)}
        r2 = FakeR2(keys)
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        calls = []

        def replay(*a, **k):
            calls.append(1)
            return (502, "bad gateway")

        monkeypatch.setattr(drain, "replay_body", replay)

        drain.drain_r2(_args())
        # One attempt, then break — not five failures against a dead intake.
        assert len(calls) == 1
        assert r2.deleted == []

    def test_a_permanently_rejected_body_is_set_aside_not_retried_forever(
            self, drain, monkeypatch):
        r2 = FakeR2({"webhook/2026/08/21/00/1-a.bin": _obj(1)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (400, "no payload_json"))

        drain.drain_r2(_args())
        assert r2.copied == [("webhook/2026/08/21/00/1-a.bin",
                              "rejected/webhook/2026/08/21/00/1-a.bin")]
        assert r2.deleted == ["webhook/2026/08/21/00/1-a.bin"]

    def test_dry_run_touches_nothing(self, drain, monkeypatch):
        r2 = FakeR2({"webhook/2026/08/21/00/1-a.bin": _obj(1)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body",
                            lambda *a, **k: pytest.fail("dry run must not replay"))

        drain.drain_r2(_args(apply=False))
        assert r2.deleted == [] and r2.copied == []

    def test_oldest_objects_replay_first(self, drain, monkeypatch):
        # Keys embed a zero-padded date path and a ms timestamp, so lexical
        # order is chronological; the drain must not disturb that.
        r2 = FakeR2({
            "webhook/2026/08/21/00/300-c.bin": _obj(3),
            "webhook/2026/08/20/23/100-a.bin": _obj(1),
            "webhook/2026/08/21/00/200-b.bin": _obj(2),
        })
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (200, "Queued"))

        drain.drain_r2(_args())
        assert r2.deleted == [
            "webhook/2026/08/20/23/100-a.bin",
            "webhook/2026/08/21/00/200-b.bin",
            "webhook/2026/08/21/00/300-c.bin",
        ]

    def test_the_pass_is_bounded(self, drain, monkeypatch):
        r2 = FakeR2({f"webhook/2026/08/21/00/{i:04d}-a.bin": _obj(i) for i in range(50)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (200, "Queued"))

        drain.drain_r2(_args(limit=10))
        assert len(r2.deleted) == 10


class TestFasterRecovery:
    """A multi-hour outage must drain in a pass or two, not 500 per 5 minutes."""

    def test_no_cap_drains_everything_in_one_pass(self, drain, monkeypatch):
        r2 = FakeR2({f"webhook/2026/08/21/00/{i:05d}-a.bin": _obj(i) for i in range(1200)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (200, "Queued"))

        drain.drain_r2(_args(limit=0, workers=8))
        assert len(r2.deleted) == 1200 and not r2.objects

    def test_concurrent_workers_still_stop_on_a_sick_intake(self, drain, monkeypatch):
        r2 = FakeR2({f"webhook/2026/08/21/00/{i:04d}-a.bin": _obj(i) for i in range(200)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (503, "down"))

        drain.drain_r2(_args(limit=0, workers=4))
        assert r2.deleted == [] and len(r2.objects) == 200

    def test_the_time_budget_leaves_the_rest_for_next_pass(self, drain, monkeypatch):
        r2 = FakeR2({f"webhook/2026/08/21/00/{i:04d}-a.bin": _obj(i) for i in range(20)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (200, "Queued"))
        clock = iter([0.0] + [0.0] * 5 + [10_000.0] * 100)
        monkeypatch.setattr(drain.time, "monotonic", lambda: next(clock))

        drain.drain_r2(_args(limit=0, max_seconds=60))
        assert 0 < len(r2.deleted) < 20

    def test_pauses_while_the_consumer_is_behind(self, drain, monkeypatch):
        r2 = FakeR2({"webhook/2026/08/21/00/1-a.bin": _obj(1)})
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "replay_body", lambda *a, **k: (200, "Queued"))
        depths = iter([5000, 5000, 10])
        monkeypatch.setattr(drain, "_queue_depth_reader", lambda: lambda: next(depths))
        slept = []
        monkeypatch.setattr(drain.time, "sleep", lambda s: slept.append(s))

        drain.drain_r2(_args(max_queue_depth=1000))
        assert len(slept) == 2 and r2.deleted == ["webhook/2026/08/21/00/1-a.bin"]


class TestOriginalDating:
    def test_the_capture_time_is_sent_signed(self, drain, monkeypatch):
        monkeypatch.setenv("JWT_TOKEN_KEY", "test-secret")
        from utils import replay_stamp

        headers = drain.stamp_headers("2026-10-05T12:21:00.000Z")
        assert headers[replay_stamp.STAMP_HEADER] == "2026-10-05T12:21:00"
        assert replay_stamp.verify(headers[replay_stamp.STAMP_HEADER],
                                   headers[replay_stamp.SIG_HEADER])

    def test_no_capture_time_sends_no_stamp(self, drain, monkeypatch):
        monkeypatch.setenv("JWT_TOKEN_KEY", "test-secret")
        assert drain.stamp_headers("") == {}

    def test_the_drain_passes_the_objects_capture_time(self, drain, monkeypatch):
        r2 = FakeR2({"webhook/2026/08/21/00/1-a.bin": _obj(1)})
        orig_get = r2.get_object

        def get_object(Bucket, Key):
            obj = orig_get(Bucket, Key)
            obj["Metadata"]["captured_at"] = "2026-08-21T00:00:01.000Z"
            return obj

        r2.get_object = get_object
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        seen = {}

        def replay(*a, **k):
            seen.update(k)
            return (200, "Queued")

        monkeypatch.setattr(drain, "replay_body", replay)
        drain.drain_r2(_args())
        assert seen["captured_at"] == "2026-08-21T00:00:01.000Z"


class FakeRedis:
    def __init__(self, dead):
        self.dead = list(dead)
        self.queue = []
        self.ops = []

    def lrange(self, key, start, end):
        return self.dead[start:end + 1]

    def rpush(self, key, val):
        self.ops.append(("rpush", val))
        self.queue.append(val)

    def lrem(self, key, count, val):
        self.ops.append(("lrem", val))
        self.dead.remove(val)


class TestDeadLetterDrain:
    def test_requeue_happens_before_removal(self, drain, monkeypatch):
        fake = FakeRedis(['{"payload":{"embeds":[{"fields":[]}]}}'])
        monkeypatch.setitem(sys.modules, "redis",
                            types.SimpleNamespace(Redis=lambda **kw: fake))

        drain.drain_dead(_args(source="dead"))
        # A crash between the two must re-deliver, not lose. rpush first.
        assert [op for op, _ in fake.ops] == ["rpush", "lrem"]
        assert fake.dead == [] and len(fake.queue) == 1

    def test_dry_run_leaves_the_dead_list_alone(self, drain, monkeypatch):
        fake = FakeRedis(['{"payload":{"embeds":[{"fields":[]}]}}'])
        monkeypatch.setitem(sys.modules, "redis",
                            types.SimpleNamespace(Redis=lambda **kw: fake))

        drain.drain_dead(_args(source="dead", apply=False))
        assert fake.ops == [] and len(fake.dead) == 1

    def test_unparseable_entry_does_not_abort_the_listing(self, drain, monkeypatch):
        fake = FakeRedis(["not json at all", '{"payload":{"embeds":[{"fields":[]}]}}'])
        monkeypatch.setitem(sys.modules, "redis",
                            types.SimpleNamespace(Redis=lambda **kw: fake))

        drain.drain_dead(_args(source="dead", apply=True))
        assert len(fake.queue) == 2


class FakeStore:
    def __init__(self):
        self.kv = {}

    def set(self, key, value, ex=None):
        self.kv[key] = value

    def delete(self, key):
        self.kv.pop(key, None)


class _HeadR2(FakeR2):
    """FakeR2 plus head_object, with per-key sample flags / capture times."""

    def __init__(self, objects, samples=(), captured=None):
        super().__init__(objects)
        self.samples = set(samples)
        self.captured = captured or {}

    def head_object(self, Bucket, Key):
        meta = {"sample": "1" if Key in self.samples else "0"}
        if Key in self.captured:
            meta["captured_at"] = self.captured[Key]
        return {"Metadata": meta}


def _key(ms):
    return f"webhook/2026/10/05/12/{ms}-ray.bin"


class TestRecoveryMarker:
    """t274: while the spool holds real captures, ``intake:recovery:r2``
    carries the oldest one's time so the events sweep holds scheduled ends
    that fall before it. Health samples must never raise it."""

    KEY = "intake:recovery:r2"

    def _run(self, drain, monkeypatch, r2, replay=(200, "Queued"), **args):
        store = FakeStore()
        monkeypatch.setattr(drain, "_recovery_redis", lambda: store)
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        seen = {}

        def _replay(*a, **k):
            seen.setdefault("marker", store.kv.get(self.KEY))
            return replay

        monkeypatch.setattr(drain, "replay_body", _replay)
        drain.drain_r2(_args(**args))
        return store, seen

    def test_real_capture_raises_it_then_a_clean_pass_clears_it(self, drain, monkeypatch):
        k1, k2 = _key(1759665600000), _key(1759665601000)
        r2 = _HeadR2({k1: _obj(1), k2: _obj(2)},
                     captured={k1: "2026-10-05T12:00:00.000Z"})
        store, seen = self._run(drain, monkeypatch, r2)
        assert seen["marker"] == "2026-10-05T12:00:00.000Z"
        assert self.KEY not in store.kv

    def test_samples_only_never_raise_it(self, drain, monkeypatch):
        k1 = _key(1759665600000)
        r2 = _HeadR2({k1: _obj(1)}, samples={k1})
        store, seen = self._run(drain, monkeypatch, r2)
        assert seen["marker"] is None
        assert self.KEY not in store.kv

    def test_oldest_sample_is_skipped_for_the_first_real_capture(self, drain, monkeypatch):
        k1, k2 = _key(1759665600000), _key(1759665700000)
        r2 = _HeadR2({k1: _obj(1), k2: _obj(2)}, samples={k1})
        store, seen = self._run(drain, monkeypatch, r2)
        assert seen["marker"] == "2025-10-05T12:01:40"  # from k2's key time

    def test_a_deferred_pass_keeps_it(self, drain, monkeypatch):
        k1 = _key(1759665600000)
        r2 = _HeadR2({k1: _obj(1)})
        store, _ = self._run(drain, monkeypatch, r2, replay=(503, "down"))
        assert self.KEY in store.kv

    def test_a_sick_intake_still_raises_it(self, drain, monkeypatch):
        k1 = _key(1759665600000)
        r2 = _HeadR2({k1: _obj(1)})
        store = FakeStore()
        monkeypatch.setattr(drain, "_recovery_redis", lambda: store)
        monkeypatch.setattr(drain, "_r2_client", lambda: r2)
        monkeypatch.setattr(drain, "intake_is_healthy", lambda *a, **k: False)
        assert drain.drain_r2(_args(skip_health_check=False)) == 1
        assert self.KEY in store.kv
        assert r2.deleted == []

    def test_empty_spool_clears_it(self, drain, monkeypatch):
        store = FakeStore()
        store.kv[self.KEY] = "stale"
        monkeypatch.setattr(drain, "_recovery_redis", lambda: store)
        monkeypatch.setattr(drain, "_r2_client", lambda: FakeR2({}))
        drain.drain_r2(_args())
        assert self.KEY not in store.kv

    def test_dry_run_never_touches_it(self, drain, monkeypatch):
        calls = []
        monkeypatch.setattr(drain, "_recovery_redis", lambda: calls.append(1))
        monkeypatch.setattr(drain, "_r2_client", lambda: _HeadR2({_key(1): _obj(1)}))
        drain.drain_r2(_args(apply=False))
        assert calls == []
