"""services/page_screenshot.py — render capacity and the shared browser.

On the 2026-10-20 game update a login burst put ~24 one-shot gear-render
browsers (~80 threads each) in flight and the intake API hit the service user's
LimitNPROC: "can't start new thread". Renders now share one long-lived browser
per process, are capped per process and box-wide (flock slot files), and give
up with ScreenshotBusy rather than queue without bound. These tests pin that.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


shot = _load("_page_screenshot_concurrency_under_test", "services", "page_screenshot.py")


class _Proc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Browser launch/teardown and the capture itself, faked."""
    state = {"launched": [], "retired": [], "now": 0, "peak": 0,
             "capture": None}

    async def start():
        b = shot._Browser(_Proc(), str(tmp_path / f"b{len(state['launched'])}"),
                          f"ws://{len(state['launched'])}")
        state["launched"].append(b)
        shot._all_browsers.add(b)
        return b

    async def retire(b):
        shot._all_browsers.discard(b)
        state["retired"].append(b)

    async def capture(ws_url, url, **kwargs):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        try:
            if state["capture"]:
                return await state["capture"](ws_url, url)
            await asyncio.sleep(0.01)
            return url.encode()
        finally:
            state["now"] -= 1

    monkeypatch.setattr(shot, "_start_browser", start)
    monkeypatch.setattr(shot, "_retire", retire)
    monkeypatch.setattr(shot, "_capture", capture)
    monkeypatch.setattr(shot, "_pools", {})
    monkeypatch.setattr(shot, "_all_browsers", set())
    monkeypatch.setattr(shot, "_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setattr(shot, "_GLOBAL_SLOTS", 4)
    monkeypatch.setattr(shot, "_MAX_CONCURRENT", 2)
    monkeypatch.setattr(shot, "_QUEUE_TIMEOUT", 5.0)
    monkeypatch.setattr(shot, "_RECYCLE_AFTER", 200)
    monkeypatch.setattr(shot, "_IDLE_SECONDS", 60.0)
    return state


def test_renders_share_one_browser_and_respect_the_process_cap(fake):
    async def main():
        return await asyncio.gather(*(shot.screenshot_url(f"u{i}") for i in range(10)))

    assert asyncio.run(main()) == [f"u{i}".encode() for i in range(10)]
    assert fake["peak"] == 2
    assert len(fake["launched"]) == 1


def test_box_wide_slots_bound_concurrency_across_processes(fake, monkeypatch):
    """flock() conflicts between separate opens even inside one process, so
    holding slot files here stands in for other services rendering."""
    monkeypatch.setattr(shot, "_MAX_CONCURRENT", 10)
    monkeypatch.setattr(shot, "_GLOBAL_SLOTS", 3)
    held = [shot._try_global_slot() for _ in range(2)]  # another service has 2
    assert all(isinstance(fd, int) for fd in held)

    async def main():
        return await asyncio.gather(*(shot.screenshot_url(f"u{i}") for i in range(6)))

    asyncio.run(main())
    assert fake["peak"] == 1
    for fd in held:
        shot._release_global_slot(fd)


def test_gives_up_with_busy_instead_of_queueing_forever(fake, monkeypatch):
    monkeypatch.setattr(shot, "_GLOBAL_SLOTS", 1)
    held = shot._try_global_slot()
    try:
        with pytest.raises(shot.ScreenshotBusy):
            asyncio.run(shot.screenshot_url("x", queue_timeout=0.2))
    finally:
        shot._release_global_slot(held)
    assert fake["launched"] == []  # never started a browser it could not use


def test_a_dead_slot_holder_frees_its_slot(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(shot, "_GLOBAL_SLOTS", 1)
    fd = shot._try_global_slot()
    os.close(fd)  # what the kernel does when the holder dies
    assert asyncio.run(shot.screenshot_url("x", queue_timeout=0.5)) == b"x"


def test_failures_release_every_slot(fake, monkeypatch):
    monkeypatch.setattr(shot, "_MAX_CONCURRENT", 1)
    monkeypatch.setattr(shot, "_GLOBAL_SLOTS", 1)

    async def boom(ws_url, url):
        raise RuntimeError("page failed")

    fake["capture"] = boom

    async def main():
        for _ in range(3):
            with pytest.raises(RuntimeError, match="page failed"):
                await shot.screenshot_url("x", queue_timeout=1)

    asyncio.run(main())


def test_browser_recycled_after_n_renders(fake, monkeypatch):
    monkeypatch.setattr(shot, "_RECYCLE_AFTER", 3)

    async def main():
        for i in range(7):
            await shot.screenshot_url(f"u{i}")

    asyncio.run(main())
    assert len(fake["launched"]) == 3
    assert fake["retired"] == fake["launched"][:2]


def test_recycle_while_busy_lets_running_renders_finish(fake, monkeypatch):
    monkeypatch.setattr(shot, "_RECYCLE_AFTER", 1)
    release = asyncio.Event

    async def main():
        gate = release()
        used = []

        async def slow(ws_url, url):
            used.append(ws_url)
            if url == "first":
                await gate.wait()
            return url.encode()

        fake["capture"] = slow
        first = asyncio.create_task(shot.screenshot_url("first"))
        await asyncio.sleep(0.05)
        second = await shot.screenshot_url("second")  # gets a fresh browser
        assert fake["retired"] == []  # the busy one is not stopped under it
        gate.set()
        await first
        return used, second

    used, second = asyncio.run(main())
    assert second == b"second"
    assert used[0] != used[1]
    assert fake["launched"][0] in fake["retired"]


def test_dead_browser_is_replaced(fake):
    async def main():
        await shot.screenshot_url("a")
        fake["launched"][0].proc.returncode = -9  # chromium crashed while idle
        await shot.screenshot_url("b")

    asyncio.run(main())
    assert len(fake["launched"]) == 2
    assert fake["retired"] == [fake["launched"][0]]


def test_wedged_browser_replaced_after_repeated_failures(fake):
    async def boom(ws_url, url):
        raise RuntimeError("websocket timed out")

    async def main():
        fake["capture"] = boom
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await shot.screenshot_url("x")
        fake["capture"] = None
        return await shot.screenshot_url("ok")

    assert asyncio.run(main()) == b"ok"
    assert len(fake["launched"]) == 2
    assert fake["retired"] == [fake["launched"][0]]


def test_one_page_failure_keeps_the_browser(fake):
    calls = {"n": 0}

    async def flaky(ws_url, url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("site 502")
        return b"ok"

    async def main():
        fake["capture"] = flaky
        with pytest.raises(RuntimeError):
            await shot.screenshot_url("x")
        return await shot.screenshot_url("x")

    assert asyncio.run(main()) == b"ok"
    assert len(fake["launched"]) == 1


def test_idle_browser_is_shut_down(fake, monkeypatch):
    monkeypatch.setattr(shot, "_IDLE_SECONDS", 0.05)

    async def main():
        await shot.screenshot_url("a")
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert fake["retired"] == fake["launched"]


def test_work_cancels_the_idle_shutdown(fake, monkeypatch):
    monkeypatch.setattr(shot, "_IDLE_SECONDS", 0.1)

    async def main():
        await shot.screenshot_url("a")
        await asyncio.sleep(0.06)
        await shot.screenshot_url("b")
        await asyncio.sleep(0.06)
        return list(fake["retired"])

    assert asyncio.run(main()) == []
    assert len(fake["launched"]) == 1


def test_each_event_loop_gets_its_own_pool(fake, monkeypatch):
    stopped = []
    monkeypatch.setattr(shot, "_stop_browser_sync", lambda b: stopped.append(b))
    assert asyncio.run(shot.screenshot_url("a")) == b"a"
    assert asyncio.run(shot.screenshot_url("b")) == b"b"
    # the first loop's browser was stopped when the second loop took over
    assert stopped == [fake["launched"][0]]


def test_slot_cap_off_falls_back_to_process_cap(fake, monkeypatch):
    monkeypatch.setattr(shot, "_GLOBAL_SLOTS", 0)
    assert shot._try_global_slot() is True
    assert asyncio.run(shot.screenshot_url("a")) == b"a"


def test_unwritable_slot_dir_fails_open(fake, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(shot, "_SLOT_DIR", str(blocker / "slots"))
    assert shot._try_global_slot() is True
