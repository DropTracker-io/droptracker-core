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
    return importlib.reload(mod)


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
                intake="http://127.0.0.1:31323", skip_health_check=True)
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
