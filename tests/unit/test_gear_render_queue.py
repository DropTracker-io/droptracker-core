"""services/gear_render_queue.py — gear renders are queued, never dropped.

Uploads enqueue; drainers render newest first at whatever rate the screenshot
caps allow. These tests pin dedupe, ordering, the age/length bounds, putting a
job back when there is no capacity, and the inline fallback without Redis.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


q = _load("_gear_render_queue_under_test", "services", "gear_render_queue.py")


class _FakeRedis:
    """The sorted-set subset the queue uses."""

    def __init__(self):
        self.z = {}

    def zadd(self, key, mapping, nx=False):
        for m, s in mapping.items():
            m = m.encode() if isinstance(m, str) else m
            if nx and m in self.z:
                continue
            self.z[m] = float(s)

    def zremrangebyrank(self, key, start, stop):
        ordered = sorted(self.z, key=lambda m: self.z[m])
        n = len(ordered)
        stop = n + stop if stop < 0 else stop
        for m in ordered[start:stop + 1]:
            del self.z[m]

    def zpopmax(self, key):
        if not self.z:
            return []
        m = max(self.z, key=lambda k: self.z[k])
        return [(m, self.z.pop(m))]

    def pipeline(self):
        r = self

        class P:
            def __init__(self):
                self.ops = []

            def __getattr__(self, name):
                return lambda *a, **k: self.ops.append((name, a, k))

            def execute(self):
                for name, a, k in self.ops:
                    getattr(r, name)(*a, **k)

        return P()


class Busy(RuntimeError):
    pass


@pytest.fixture
def env(monkeypatch):
    r = _FakeRedis()
    rendered = []
    state = {"busy": 0}

    async def render(pid, fp, *, raise_busy=False, queue_timeout=None, force=False):
        if state["busy"]:
            state["busy"] -= 1
            raise Busy()
        rendered.append((pid, fp))

    monkeypatch.setattr(q, "_redis", lambda: r)
    monkeypatch.setattr(q, "_BUSY_BACKOFF_SECONDS", 0)
    monkeypatch.setitem(sys.modules, "services.gear_image",
                        types.SimpleNamespace(render_gear_image=render))
    monkeypatch.setitem(sys.modules, "services.page_screenshot",
                        types.SimpleNamespace(ScreenshotBusy=Busy))
    return r, rendered, state


def _drain_all(r):
    async def main():
        while await q._drain_once(r):
            pass
    asyncio.run(main())


def test_repeat_uploads_of_one_outfit_are_one_job(env):
    r, rendered, _ = env
    for _ in range(3):
        asyncio.run(q.enqueue(7, "abc"))
    _drain_all(r)
    assert rendered == [(7, "abc")]


def test_newest_outfit_renders_first(env):
    r, rendered, _ = env
    r.zadd(q.QUEUE_KEY, {"1:old": time.time() - 100, "2:new": time.time()})
    _drain_all(r)
    assert rendered == [(2, "new"), (1, "old")]


def test_stale_jobs_are_dropped(env):
    r, rendered, _ = env
    r.zadd(q.QUEUE_KEY, {"1:stale": time.time() - q.MAX_AGE_SECONDS - 5})
    _drain_all(r)
    assert rendered == []


def test_queue_is_trimmed_oldest_first(env, monkeypatch):
    r, _, _ = env
    monkeypatch.setattr(q, "MAX_LEN", 3)
    for i in range(5):
        r.zadd(q.QUEUE_KEY, {f"{i}:fp": float(i)})
    asyncio.run(q.enqueue(9, "newest"))
    assert sorted(r.z) == [b"3:fp", b"4:fp", b"9:newest"]


def test_no_capacity_puts_the_job_back_in_place(env):
    r, rendered, state = env
    score = time.time() - 50
    r.zadd(q.QUEUE_KEY, {"5:fp": score})
    state["busy"] = 1

    asyncio.run(q._drain_once(r))
    assert rendered == []
    assert r.z == {b"5:fp": score}  # original score: keeps its place in line

    _drain_all(r)
    assert rendered == [(5, "fp")]


def test_a_failing_render_is_not_requeued_forever(env, monkeypatch):
    r, _, _ = env

    async def boom(*a, **k):
        raise RuntimeError("page never ready")

    monkeypatch.setitem(sys.modules, "services.gear_image",
                        types.SimpleNamespace(render_gear_image=boom))
    r.zadd(q.QUEUE_KEY, {"5:fp": time.time()})
    _drain_all(r)
    assert r.z == {}


def test_garbage_members_are_skipped(env):
    r, rendered, _ = env
    r.zadd(q.QUEUE_KEY, {"nonsense": time.time(), "x:fp": time.time() - 1})
    _drain_all(r)
    assert rendered == []


def test_without_redis_the_render_happens_inline(env, monkeypatch):
    _, rendered, _ = env
    monkeypatch.setattr(q, "_redis", lambda: None)
    asyncio.run(q.enqueue(3, "fp"))
    assert rendered == [(3, "fp")]


def test_drainers_start_and_stop(env):
    r, rendered, _ = env

    async def main():
        tasks = q.start(2)
        await q.enqueue(1, "a")
        await q.enqueue(2, "b")
        for _ in range(100):
            if len(rendered) == 2:
                break
            await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(main())
    assert sorted(rendered) == [(1, "a"), (2, "b")]
