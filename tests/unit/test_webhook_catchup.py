"""Automatic catch-up for the Discord webhook reader (services/webhook_catchup.py).

Webhook-only clients post to Discord, and the reader bot processes each message
as it arrives with no queue behind it. Anything posted while the bot was down
used to wait for a manual replay. These pin down the automatic version: where
it starts, where it stops, how it dates what it recovers, and that an
interrupted catch-up resumes instead of skipping the rest of the gap.
"""
import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# conftest stubs the `services` package, so load the real module by path.
_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "services.webhook_catchup", _ROOT / "services" / "webhook_catchup.py")
wc = importlib.util.module_from_spec(_spec)
sys.modules["services.webhook_catchup"] = wc
_spec.loader.exec_module(wc)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, **data):
        self.data = dict(data)
        self.history = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.history.append(("set", key, value))
        self.data[key] = value

    def delete(self, key):
        self.history.append(("delete", key))
        self.data.pop(key, None)


def _msg(at: datetime, mid=None, embeds=True, author_id=1, system=False):
    return SimpleNamespace(
        id=mid if mid is not None else wc.snowflake_for(at),
        created_at=at,
        embeds=[object()] if embeds else [],
        author=SimpleNamespace(id=author_id, system=system),
    )


class FakeChannel:
    def __init__(self, name, messages):
        self.name = name
        self.messages = sorted(messages, key=lambda m: m.id)
        self.after_seen = None

    def history(self, limit=0, after=None, before=None):
        # Mirrors interactions.py: with `after`, `before` is ignored and the
        # iterator pages forward to the present.
        self.after_seen = after
        msgs = [m for m in self.messages if m.id > after]

        async def gen():
            for m in msgs:
                yield m
        return gen()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(wc, "PER_MESSAGE_YIELD", 0)


def _run(channels, store, *, bundle=lambda m: [("drop", {})], own_id=99):
    dispatched = []

    async def collect(client, guild_ids, only_channels=None, log=print):
        return [(SimpleNamespace(name="g"), c) for c in channels]

    async def process(message, b):
        dispatched.append((message, b))
        return len(b)

    client = SimpleNamespace(user=SimpleNamespace(id=own_id))
    orig = wc.collect_channels
    wc.collect_channels = collect
    try:
        counts = asyncio.run(wc.run_catchup(client, ["1"], bundle, process,
                                            store=store, now=NOW, log=lambda *_: None))
    finally:
        wc.collect_channels = orig
    return counts, dispatched


class TestPlanWindow:
    def test_no_reference_point_means_nothing_to_do(self):
        assert wc.plan_window(None, None, NOW) is None

    def test_starts_a_margin_before_the_watermark(self):
        wm = wc.snowflake_for(NOW - timedelta(hours=3))
        after, before, clamped = wc.plan_window(wm, None, NOW)
        assert wc.snowflake_time(after) == NOW - timedelta(hours=3) - wc.MARGIN
        assert before == wc.snowflake_for(NOW)
        assert not clamped

    def test_an_unfinished_earlier_catchup_wins(self):
        wm = wc.snowflake_for(NOW - timedelta(minutes=5))
        pending = wc.snowflake_for(NOW - timedelta(hours=4))
        after, _, _ = wc.plan_window(wm, pending, NOW)
        assert wc.snowflake_time(after) == NOW - timedelta(hours=4) - wc.MARGIN

    def test_lookback_is_capped(self):
        wm = wc.snowflake_for(NOW - timedelta(days=30))
        after, _, clamped = wc.plan_window(wm, None, NOW)
        assert clamped
        assert wc.snowflake_time(after) == NOW - wc.MAX_LOOKBACK


class TestRunCatchup:
    def test_first_run_only_starts_a_watermark(self):
        store = FakeStore()
        counts, dispatched = _run([FakeChannel("a", [_msg(NOW - timedelta(hours=1))])], store)
        assert counts is None and dispatched == []
        assert int(store.data[wc.WATERMARK_KEY]) == wc.snowflake_for(NOW)

    def test_replays_the_gap_and_stamps_original_time(self):
        down_at = NOW - timedelta(hours=3)
        store = FakeStore(**{wc.WATERMARK_KEY: str(wc.snowflake_for(down_at))})
        missed = _msg(NOW - timedelta(hours=2))
        counts, dispatched = _run([FakeChannel("a", [missed])], store)

        assert counts["messages"] == 1 and len(dispatched) == 1
        (_, data), = dispatched[0][1]
        assert data["_received_at"] == (NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat()
        assert data["_received_at_trusted"] is True

    def test_stops_at_the_moment_it_started(self):
        # Live messages after `now` are the live listener's; history() would
        # otherwise run on to the present.
        store = FakeStore(**{wc.WATERMARK_KEY: str(wc.snowflake_for(NOW - timedelta(hours=1)))})
        in_gap = _msg(NOW - timedelta(minutes=30))
        live = _msg(NOW + timedelta(seconds=5))
        _, dispatched = _run([FakeChannel("a", [in_gap, live])], store)
        assert [m for m, _ in dispatched] == [in_gap]

    def test_skips_what_the_live_listener_skips(self):
        store = FakeStore(**{wc.WATERMARK_KEY: str(wc.snowflake_for(NOW - timedelta(hours=1)))})
        at = NOW - timedelta(minutes=30)
        msgs = [_msg(at, mid=wc.snowflake_for(at) + 1, embeds=False),
                _msg(at, mid=wc.snowflake_for(at) + 2, system=True),
                _msg(at, mid=wc.snowflake_for(at) + 3, author_id=99)]
        _, dispatched = _run([FakeChannel("a", msgs)], store)
        assert dispatched == []

    def test_pending_is_pinned_during_the_run_and_cleared_after(self):
        wm = str(wc.snowflake_for(NOW - timedelta(hours=1)))
        store = FakeStore(**{wc.WATERMARK_KEY: wm})
        _run([FakeChannel("a", [])], store)
        assert ("set", wc.PENDING_KEY, wm) in store.history
        assert wc.PENDING_KEY not in store.data

    def test_an_unreadable_channel_does_not_stop_the_rest(self):
        store = FakeStore(**{wc.WATERMARK_KEY: str(wc.snowflake_for(NOW - timedelta(hours=1)))})

        class Broken(FakeChannel):
            def history(self, **kw):
                raise RuntimeError("403 Missing Access")

        ok = FakeChannel("b", [_msg(NOW - timedelta(minutes=10))])
        counts, dispatched = _run([Broken("a", []), ok], store)
        assert counts["unreadable_channels"] == 1 and len(dispatched) == 1

    def test_a_failed_dispatch_keeps_going(self):
        store = FakeStore(**{wc.WATERMARK_KEY: str(wc.snowflake_for(NOW - timedelta(hours=1)))})
        a, b = _msg(NOW - timedelta(minutes=20)), _msg(NOW - timedelta(minutes=10))
        calls = []

        async def collect(client, guild_ids, only_channels=None, log=print):
            return [(SimpleNamespace(name="g"), FakeChannel("a", [a, b]))]

        async def process(message, bundle):
            calls.append(message)
            if message is a:
                raise RuntimeError("db blip")
            return 1

        orig = wc.collect_channels
        wc.collect_channels = collect
        try:
            counts = asyncio.run(wc.run_catchup(
                SimpleNamespace(user=None), ["1"], lambda m: [("drop", {})], process,
                store=store, now=NOW, log=lambda *_: None))
        finally:
            wc.collect_channels = orig
        assert calls == [a, b]
        assert counts["failed"] == 1 and counts["dispatched"] == 1


class TestWatermark:
    def test_writes_are_throttled_and_only_move_forward(self):
        t = [0.0]
        store = FakeStore()
        w = wc.Watermark(store=store, clock=lambda: t[0])
        w.note(100)
        assert store.data[wc.WATERMARK_KEY] == "100"
        w.note(200)                       # inside the interval: held
        assert store.data[wc.WATERMARK_KEY] == "100"
        t[0] += wc.WATERMARK_WRITE_INTERVAL
        w.note(150)                       # older id never moves it back
        assert store.data[wc.WATERMARK_KEY] == "200"

    def test_a_store_failure_never_reaches_the_listener(self):
        class Boom:
            def set(self, *a):
                raise RuntimeError("redis down")

        w = wc.Watermark(store=Boom(), clock=lambda: 0.0)
        w.note(1)  # must not raise


class TestStampBundle:
    def test_converts_aware_times_to_naive_utc(self):
        bundle = [("drop", {}), ("pb", {})]
        wc.stamp_bundle(bundle, datetime(2026, 10, 5, 14, 0, tzinfo=timezone(timedelta(hours=2))))
        assert all(d["_received_at"] == "2026-10-05T12:00:00" for _, d in bundle)
        assert all(d["_received_at_trusted"] is True for _, d in bundle)


class TestEventEndHoldMarker:
    """t274: while a catch-up replays, ``webhookbot:catchup_active`` names its
    start so the events sweep holds scheduled ends inside the gap. It must be
    gone when the catch-up finishes."""

    def test_raised_during_the_run_and_cleared_after(self):
        from utils.event_end_hold import CATCHUP_ACTIVE_KEY, _from_snowflake

        gap_start = NOW - timedelta(hours=2)
        store = FakeStore(**{wc.WATERMARK_KEY: str(wc.snowflake_for(gap_start))})
        _run([FakeChannel("a", [_msg(NOW - timedelta(hours=1))])], store)
        raised = [v for op, k, *v in store.history if op == "set" and k == CATCHUP_ACTIVE_KEY]
        assert raised, "marker never raised"
        since = _from_snowflake(raised[0][0])
        assert since == (gap_start - wc.MARGIN).replace(tzinfo=None)
        assert CATCHUP_ACTIVE_KEY not in store.data
