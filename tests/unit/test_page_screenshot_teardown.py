"""services/page_screenshot.py — browser teardown must not block the loop.

``screenshot_url`` ends every render by SIGTERMing chromium and waiting for it
to exit. ``Popen.wait`` is a blocking syscall (~100ms on a quiet box, seconds
under load) and it ran inline in the handler's ``finally`` — one event-loop
stall per gear render, tens of thousands a day. The teardown now runs in a
thread; these tests pin that it still stops the process, still cleans up, and
does so off the loop.

Loaded from the file path; ``db.app_logger`` is a conftest stub and the
websocket/http clients are real but never touched here.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


shot = _load("_page_screenshot_under_test", "services", "page_screenshot.py")


class _Proc:
    """Stands in for a chromium Popen; records what was done to it and where."""

    def __init__(self, *, exits_on_terminate=True, returncode=None):
        self.returncode = returncode
        self.exits_on_terminate = exits_on_terminate
        self.events = []
        self.wait_thread = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append("terminate")
        if self.exits_on_terminate:
            self.returncode = 0

    def kill(self):
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.events.append("wait")
        self.wait_thread = threading.current_thread()
        if self.returncode is None:
            raise subprocess.TimeoutExpired("chromium", timeout)
        return self.returncode


def _tmpdir():
    tmp = tempfile.mkdtemp(prefix="dt-shot-test-")
    with open(os.path.join(tmp, "DevToolsActivePort"), "w") as fh:
        fh.write("0\n")
    return tmp


class TestTeardown:
    def test_terminates_waits_and_removes_profile(self):
        proc = _Proc()
        tmp = _tmpdir()

        shot._teardown(proc, tmp)

        assert proc.events == ["terminate", "wait"]
        assert not os.path.exists(tmp)

    def test_kills_a_browser_that_ignores_sigterm(self):
        proc = _Proc(exits_on_terminate=False)
        tmp = _tmpdir()

        shot._teardown(proc, tmp)

        assert proc.events[:3] == ["terminate", "wait", "kill"]
        assert not os.path.exists(tmp)

    def test_already_exited_browser_is_left_alone(self):
        proc = _Proc(returncode=1)
        tmp = _tmpdir()

        shot._teardown(proc, tmp)

        assert proc.events == []
        assert not os.path.exists(tmp)

    def test_no_process_still_cleans_up(self):
        tmp = _tmpdir()
        shot._teardown(None, tmp)
        assert not os.path.exists(tmp)


class TestScreenshotUrlTeardown:
    async def test_teardown_runs_off_the_event_loop_even_on_failure(self, monkeypatch):
        proc = _Proc()
        created = []

        def fake_launch(profile_dir, home):
            created.append(os.path.dirname(profile_dir))
            return proc

        async def port_never_ready(profile_dir, p, timeout):
            raise RuntimeError("chromium DevTools port not ready in time")

        monkeypatch.setattr(shot, "_launch", fake_launch)
        monkeypatch.setattr(shot, "_devtools_port", port_never_ready)

        with pytest.raises(RuntimeError, match="not ready"):
            await shot.screenshot_url("http://127.0.0.1/never")

        assert proc.events == ["terminate", "wait"]
        assert proc.wait_thread is not threading.main_thread(), \
            "Popen.wait must not block the event loop"
        assert created and not os.path.exists(created[0])
