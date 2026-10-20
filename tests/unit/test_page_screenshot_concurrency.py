"""services/page_screenshot.py — renders past the cap wait, they do not launch.

On the 2026-10-20 game update a login burst put ~24 gear renders (each its own
chromium, ~80 threads) in flight at once, and the intake API hit the service
user's LimitNPROC and died with "can't start new thread". ``screenshot_url``
now holds a per-process slot for the whole launch-capture-teardown; these tests
pin the cap and that a failed render gives its slot back.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


shot = _load("_page_screenshot_concurrency_under_test", "services", "page_screenshot.py")


def test_never_more_than_the_cap_in_flight(monkeypatch):
    monkeypatch.setattr(shot, "_MAX_CONCURRENT", 2)
    monkeypatch.setattr(shot, "_slots", {})
    state = {"now": 0, "peak": 0}

    async def fake_render(url, **kwargs):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return url.encode()

    monkeypatch.setattr(shot, "_screenshot", fake_render)

    async def main():
        return await asyncio.gather(*(shot.screenshot_url(f"u{i}") for i in range(10)))

    out = asyncio.run(main())
    assert out == [f"u{i}".encode() for i in range(10)]
    assert state["peak"] == 2


def test_a_failed_render_frees_its_slot(monkeypatch):
    monkeypatch.setattr(shot, "_MAX_CONCURRENT", 1)
    monkeypatch.setattr(shot, "_slots", {})

    async def boom(url, **kwargs):
        raise RuntimeError("chromium died")

    monkeypatch.setattr(shot, "_screenshot", boom)

    async def main():
        for _ in range(3):
            try:
                await asyncio.wait_for(shot.screenshot_url("x"), timeout=1)
            except RuntimeError:
                pass

    asyncio.run(main())  # a leaked slot would time out on the second call


def test_each_event_loop_gets_its_own_semaphore(monkeypatch):
    """Scripts call asyncio.run() repeatedly; a semaphore bound to a closed
    loop must not be reused."""
    monkeypatch.setattr(shot, "_slots", {})

    async def fake_render(url, **kwargs):
        return b"ok"

    monkeypatch.setattr(shot, "_screenshot", fake_render)
    assert asyncio.run(shot.screenshot_url("a")) == b"ok"
    assert asyncio.run(shot.screenshot_url("b")) == b"ok"
