"""Long-poll wake hub (api/notify_wake.py) + wait-param parsing.

Pure asyncio/logic tests: waiter registry semantics, wake dispatch, the
missed-wake-race contract (register before drain), and the /notifications
``wait`` clamp. The Redis pub/sub listener loop itself is exercised in
integration; here we drive :func:`wake` directly, which is all the listener
does per message.

Loaded from file paths (same pattern as test_plugin_notifications.py) so the
conftest's package stubs never get in the way.
"""
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


nw = _load("_notify_wake_under_test", "api", "notify_wake.py")
pn = _load("_plugin_notifications_for_wake_test", "services", "plugin_notifications.py")


def _run(coro):
    return asyncio.run(coro)


class TestWakeHub:
    def setup_method(self):
        nw._waiters.clear()

    def test_wake_channel_matches_publisher(self):
        # The literal in notify_wake must stay in lockstep with the constant
        # push_to_inbox publishes on.
        assert nw.WAKE_CHANNEL == pn.WAKE_CHANNEL

    def test_register_wake_unregister(self):
        event = nw.register(7)
        assert not event.is_set()
        assert nw.wake(7) == 1
        assert event.is_set()
        nw.unregister(7, event)
        assert nw._waiters == {}

    def test_wake_accepts_string_ids(self):
        # The pub/sub payload arrives as a decoded string.
        event = nw.register(7)
        assert nw.wake("7") == 1
        assert event.is_set()
        nw.unregister(7, event)

    def test_wake_ignores_unknown_and_garbage(self):
        assert nw.wake(999) == 0
        assert nw.wake("not-a-player-id") == 0
        assert nw.wake(None) == 0

    def test_wake_hits_every_waiter_for_player_only(self):
        a1, a2 = nw.register(1), nw.register(1)
        b = nw.register(2)
        assert nw.wake(1) == 2
        assert a1.is_set() and a2.is_set() and not b.is_set()
        for pid, ev in ((1, a1), (1, a2), (2, b)):
            nw.unregister(pid, ev)
        assert nw._waiters == {}

    def test_unregister_tolerates_double_and_unknown(self):
        event = nw.register(3)
        nw.unregister(3, event)
        nw.unregister(3, event)
        nw.unregister(4, asyncio.Event())

    def test_wake_before_wait_still_wakes(self):
        # The missed-wake race: push lands after drain but before wait().
        # Because the route registers first, the set flag is already latched.
        async def scenario():
            event = nw.register(5)
            nw.wake(5)
            await asyncio.wait_for(event.wait(), timeout=0.1)
            nw.unregister(5, event)

        _run(scenario())

    def test_wait_times_out_without_wake(self):
        async def scenario():
            event = nw.register(6)
            try:
                try:
                    await asyncio.wait_for(event.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    return True
                return False
            finally:
                nw.unregister(6, event)

        assert _run(scenario()) is True


class TestParseWaitSeconds:
    def _parse(self):
        # Lazy import: pulls in quart; keep it out of module import so the
        # hub tests above stay dependency-free.
        routes = _load("_notifications_routes_under_test", "api", "routes", "notifications.py")
        return routes.parse_wait_seconds, routes.MAX_NOTIFICATIONS_WAIT_SECONDS

    def test_clamps_and_sanitizes(self):
        parse, max_wait = self._parse()
        assert parse(None) == 0
        assert parse("") == 0
        assert parse("abc") == 0
        assert parse("-5") == 0
        assert parse("10") == 10
        assert parse(" 25 ") == 25
        assert parse("9999") == max_wait
