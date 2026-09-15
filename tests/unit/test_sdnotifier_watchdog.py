"""monitor/sdnotifier.py — the opt-in watchdog restart for a stalled event loop.

SystemdWatchdog used to send WATCHDOG=1 on every interval whatever the health
check said, so WatchdogSec could never restart a unit (droptracker-heartbeat sat
dead but "active" 2026-09-07 -> 09-11 and again on 09-15). restart_after_stalls
withholds the ping once the event loop hasn't run a no-op callback for N
consecutive checks. A check that runs and returns False still never withholds:
for the Discord bots that means the gateway is down, and core kept posting over
REST through the whole 2026-09-15 gateway outage.

conftest stubs monitor.sdnotifier, so the real module is loaded by file path.
"""
import ast
import asyncio
import importlib.util
import os
import sys
import threading
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


sdn = _load("_sdnotifier_under_test", "monitor", "sdnotifier.py")


class _RecordingNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, state):
        self.sent.append(state)


class _LoopThread:
    """An event loop running in its own thread, which a test can wedge."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stall(self):
        entered = threading.Event()

        def block():
            entered.set()
            self.release.wait(15)  # bounded, so a broken test can't hang the run

        self.release.clear()
        self.loop.call_soon_threadsafe(block)
        assert entered.wait(5)

    def unstall(self):
        self.release.set()

    def close(self):
        self.release.set()

        async def cancel_pending():
            for task in asyncio.all_tasks():
                if task is not asyncio.current_task():
                    task.cancel()

        asyncio.run_coroutine_threadsafe(cancel_pending(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.close()


def _watchdog(loop=None, **kwargs):
    watchdog = sdn.SystemdWatchdog(heartbeat_interval=0.05, **kwargs)
    watchdog.notifier = _RecordingNotifier()
    watchdog.loop = loop
    return watchdog


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestDefaultIsUnchanged:
    def test_a_stalled_loop_still_pings_without_the_opt_in(self, loop_thread):
        async def check():
            return True

        watchdog = _watchdog(loop_thread.loop)
        watchdog.set_health_check(check)
        loop_thread.stall()

        healthy, stalled = watchdog._run_health_check()

        assert (healthy, stalled) == (False, False)  # timed out, but stalls aren't measured
        assert all(watchdog._should_notify(True, time.monotonic()) for _ in range(50))


class TestStallDetection:
    def test_a_responsive_loop_with_a_passing_check(self, loop_thread):
        async def check():
            return True

        watchdog = _watchdog(loop_thread.loop, restart_after_stalls=3)
        watchdog.set_health_check(check)
        assert watchdog._run_health_check() == (True, False)

    def test_a_check_that_returns_false_is_not_a_stall(self, loop_thread):
        async def check():
            return False  # e.g. bot.is_ready during a gateway outage

        watchdog = _watchdog(loop_thread.loop, restart_after_stalls=3)
        watchdog.set_health_check(check)
        assert watchdog._run_health_check() == (False, False)

    def test_a_slow_check_on_a_responsive_loop_is_not_a_stall(self, loop_thread):
        async def check():
            await asyncio.sleep(3)
            return True

        watchdog = _watchdog(loop_thread.loop, restart_after_stalls=3)
        watchdog.set_health_check(check)
        assert watchdog._run_health_check() == (False, False)

    def test_a_stalled_loop_is_a_stall(self, loop_thread):
        async def check():
            return True

        watchdog = _watchdog(loop_thread.loop, restart_after_stalls=3)
        watchdog.set_health_check(check)
        loop_thread.stall()
        assert watchdog._run_health_check() == (False, True)

    def test_a_stall_is_measured_without_a_health_check(self, loop_thread):
        watchdog = _watchdog(loop_thread.loop, restart_after_stalls=3)
        loop_thread.stall()
        assert watchdog._run_health_check() == (True, True)


class TestWithholding:
    def test_withholds_from_the_nth_consecutive_stall(self):
        watchdog = _watchdog(restart_after_stalls=3, restart_min_uptime=0)
        now = time.monotonic()
        assert [watchdog._should_notify(True, now) for _ in range(4)] == [True, True, False, False]

    def test_a_check_the_loop_ran_resets_the_streak(self):
        watchdog = _watchdog(restart_after_stalls=3, restart_min_uptime=0)
        now = time.monotonic()
        ticks = [True, True, False, True, True, True]
        assert [watchdog._should_notify(stalled, now) for stalled in ticks] == [True, True, True, True, True, False]

    def test_pings_again_once_the_loop_recovers(self):
        watchdog = _watchdog(restart_after_stalls=1, restart_min_uptime=0)
        now = time.monotonic()
        assert watchdog._should_notify(True, now) is False
        assert watchdog.withholding
        assert watchdog._should_notify(False, now) is True
        assert not watchdog.withholding

    def test_never_withholds_before_the_minimum_uptime(self):
        watchdog = _watchdog(restart_after_stalls=1, restart_min_uptime=300)
        watchdog.started_at = 1000.0
        assert watchdog._should_notify(True, 1299.0) is True
        assert watchdog._should_notify(True, 1300.0) is False

    def test_start_restarts_the_uptime_clock_and_the_streak(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_USEC", "30000000")
        watchdog = _watchdog(restart_after_stalls=2, restart_min_uptime=300)
        watchdog.started_at = 0.0
        watchdog.consecutive_stalls = 5
        watchdog.withholding = True

        async def start_and_stop():
            before = time.monotonic()
            await watchdog.start()
            assert watchdog.started_at >= before
            assert (watchdog.consecutive_stalls, watchdog.withholding) == (0, False)
            await watchdog.stop()

        asyncio.run(start_and_stop())

    def test_rejects_a_threshold_below_one(self):
        with pytest.raises(ValueError):
            sdn.SystemdWatchdog(heartbeat_interval=1, restart_after_stalls=0)


class TestHeartbeatLoop:
    def test_withholds_watchdog_while_the_loop_is_stalled_then_resumes(self, loop_thread):
        async def check():
            return True

        watchdog = _watchdog(loop_thread.loop, restart_after_stalls=2, restart_min_uptime=0)
        watchdog.set_health_check(check)
        loop_thread.stall()
        thread = threading.Thread(target=watchdog._heartbeat_loop, daemon=True)
        thread.start()
        try:
            assert _wait_for(lambda: watchdog.withholding)
            assert watchdog.notifier.sent == ["WATCHDOG=1"]  # only the first stalled check pinged
            time.sleep(1.2)  # at least one more stalled check
            assert watchdog.notifier.sent == ["WATCHDOG=1"]

            loop_thread.unstall()
            assert _wait_for(lambda: len(watchdog.notifier.sent) >= 2)
            assert not watchdog.withholding
        finally:
            watchdog.stop_event.set()
            loop_thread.unstall()
            thread.join(5)
        assert not thread.is_alive()

    def test_a_failing_check_never_withholds(self, loop_thread):
        async def check():
            return False

        watchdog = _watchdog(loop_thread.loop, restart_after_stalls=1, restart_min_uptime=0)
        watchdog.set_health_check(check)
        thread = threading.Thread(target=watchdog._heartbeat_loop, daemon=True)
        thread.start()
        try:
            assert _wait_for(lambda: len(watchdog.notifier.sent) >= 5)
            assert not watchdog.withholding
        finally:
            watchdog.stop_event.set()
            thread.join(5)


# ── Which units opt in ────────────────────────────────────────────────────────

BOTS = ["bots/main.py", "bots/webhook_bot.py", "bots/hall_of_fame.py", "bots/heartbeat.py"]


def _watchdog_calls(relpath):
    with open(os.path.join(_ROOT, relpath)) as f:
        tree = ast.parse(f.read())
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SystemdWatchdog"]


def _python_sources():
    skip = {"venv", "tests", "node_modules", "__pycache__", "static"}  # static/: ~770k image files
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.relpath(os.path.join(dirpath, name), _ROOT)


class TestUnitOptIns:
    def test_each_discord_bot_restarts_on_stalls_without_shortening_the_spacing(self):
        for relpath in BOTS:
            calls = _watchdog_calls(relpath)
            assert len(calls) == 1, relpath
            kwargs = {keyword.arg: keyword.value for keyword in calls[0].keywords}
            assert isinstance(kwargs.get("restart_after_stalls"), ast.Constant), relpath
            # Discord resets a token that goes past 1000 identifies in 24h.
            assert "restart_min_uptime" not in kwargs, relpath
        assert 86400 / (sdn.DEFAULT_RESTART_MIN_UPTIME + 30 + 5) < 1000

    def test_the_dev_dummy_watchdog_takes_the_same_arguments(self):
        with open(os.path.join(_ROOT, "bots", "webhook_bot.py")) as f:
            tree = ast.parse(f.read())
        dummy = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_DummyWatchdog"]
        namespace = {}
        exec(compile(ast.Module(body=dummy, type_ignores=[]), "webhook_bot.py", "exec"), namespace)
        namespace["_DummyWatchdog"](restart_after_stalls=20)

    def test_no_unit_that_sends_its_own_watchdog_pings_opts_in(self):
        # Hand-sent WATCHDOG=1 (data/player_total_updater.py) would keep systemd
        # satisfied while the watchdog thread is withholding.
        checked = 0
        for relpath in _python_sources():
            if relpath == os.path.join("monitor", "sdnotifier.py"):
                continue
            with open(os.path.join(_ROOT, relpath), errors="replace") as f:
                source = f.read()
            if '"WATCHDOG=1"' not in source and "'WATCHDOG=1'" not in source:
                continue
            checked += 1
            for call in _watchdog_calls(relpath):
                assert "restart_after_stalls" not in {keyword.arg for keyword in call.keywords}, relpath
        assert checked >= 1
