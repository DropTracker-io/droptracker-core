"""Which clock stamps a submission row (data/submissions/common.received_at).

Rows were stamped when a WORKER picked the submission up, not when the server
accepted it. Those are the same instant until the queue falls behind — and on
2026-08-01 it ran ~107 minutes behind at peak, so kills earned before midnight
drained after it and landed in the next month's leaderboards, rollups and
recaps. The acceptor has always recorded `enqueued_at`; nothing read it.

The guard rails matter as much as the fix: this stamp decides which month a
row belongs to, so an absent, garbled, future or ancient value must fall back
to now() rather than silently rewrite a closed month.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# common.py imports the world; the conftest stubs `db`/`services`, but the
# module still pulls in api.core and friends, so load it by path with those
# already stubbed the way the other submission tests do.
# NB: deliberately NOT stubbing the `api` package itself. sys.modules is
# global, so replacing it here with a MagicMock leaks into every module
# collected afterwards and breaks their `from api.routes... import ...` —
# which made the suite order-dependent. `api.core` alone is what common.py
# needs, and conftest already stubs it.
for _name in ("api.core", "osrs_api", "interactions"):
    sys.modules.setdefault(_name, MagicMock())

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "submissions", "common.py",
)


@pytest.fixture(scope="module")
def common():
    spec = importlib.util.spec_from_file_location("_submissions_common_under_test",
                                                  _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"common.py not importable under stubs: {exc}")
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _close_to_now(value, seconds=5):
    return abs((value - datetime.now()).total_seconds()) < seconds


class TestReceivedAt:
    def test_uses_the_servers_accept_time_when_present(self, common):
        accepted = datetime.now() - timedelta(minutes=90)
        got = common.received_at({"_received_at": accepted.isoformat()})
        # The whole point: a 90-minute backlog keeps the ACCEPT time, which is
        # what puts a pre-midnight kill in the right month.
        assert abs((got - accepted).total_seconds()) < 1

    def test_falls_back_to_now_when_absent(self, common):
        assert _close_to_now(common.received_at({}))

    def test_falls_back_to_now_on_an_empty_payload(self, common):
        assert _close_to_now(common.received_at(None))

    def test_falls_back_to_now_on_garbage(self, common):
        assert _close_to_now(common.received_at({"_received_at": "not-a-date"}))

    def test_refuses_a_future_stamp(self, common):
        ahead = (datetime.now() + timedelta(hours=2)).isoformat()
        assert _close_to_now(common.received_at({"_received_at": ahead}))

    def test_refuses_an_implausibly_old_stamp(self, common):
        # A hand-requeued or replayed entry must not backdate a row into a
        # month whose totals are already published.
        ancient = (datetime.now() - timedelta(hours=9)).isoformat()
        assert _close_to_now(common.received_at({"_received_at": ancient}))

    def test_accepts_a_stamp_just_inside_the_window(self, common):
        edge = datetime.now() - timedelta(hours=5, minutes=55)
        got = common.received_at({"_received_at": edge.isoformat()})
        assert abs((got - edge).total_seconds()) < 1

    def test_tz_aware_stamps_are_converted_not_rejected(self, common):
        # The acceptor writes utcnow().isoformat() (naive), but a tz-aware
        # value must not be compared against a naive now() and explode.
        aware = datetime.now(timezone.utc) - timedelta(minutes=30)
        got = common.received_at({"_received_at": aware.isoformat()})
        assert got.tzinfo is None
        assert abs((got - datetime.now()).total_seconds() + 1800) < 60


class TestReceivedAtReachesTheProcessors:
    """The envelope stamp must survive the hop into per-embed submission data.

    `received_at()` above was correct from the day it shipped and still did
    nothing: the consumer sets `_received_at` on the ENVELOPE payload, but
    `process_webhook_data` rebuilds each embed's `processed_data` purely from
    `embed["fields"]`, so every top-level key was dropped on the floor. The
    stamp never reached the processors, `received_at()` always read None, and
    the month-boundary fix was inert — a backlog draining across midnight kept
    booking kills into the wrong month. These cover the hop, not the parser.
    """

    @staticmethod
    def _envelope(stamp, **fields):
        payload = {
            "embeds": [{
                "fields": [{"name": k, "value": v} for k, v in
                           {"type": "drop", "player_name": "TestPlayer",
                            "guid": "guid-abc", **fields}.items()],
            }],
        }
        if stamp is not None:
            payload["_received_at"] = stamp
        return payload

    async def _process(self, payload):
        from api.routes.webhook import process_webhook_data
        items = await process_webhook_data(payload)
        assert items and len(items) == 1
        return items[0]

    async def test_envelope_stamp_reaches_processed_data(self):
        stamp = (datetime.now() - timedelta(minutes=90)).isoformat()
        processed = await self._process(self._envelope(stamp))
        assert processed.get("_received_at") == stamp

    async def test_row_is_stamped_with_accept_time_not_pickup_time(self, common):
        """End to end: a 90-minute-late entry dates to when it was ACCEPTED."""
        accepted = datetime.now() - timedelta(minutes=90)
        processed = await self._process(self._envelope(accepted.isoformat()))
        stamped = common.received_at(processed)
        assert abs((stamped - accepted).total_seconds()) < 1, (
            "row must date to server accept time, not worker pickup time"
        )
        assert not _close_to_now(stamped)

    async def test_stamp_survives_alongside_embed_fields(self):
        """Carrying the stamp must not disturb the fields already parsed."""
        stamp = datetime.now().isoformat()
        processed = await self._process(
            self._envelope(stamp, item_name="Twisted bow", source="Chambers of Xeric")
        )
        assert processed["item_name"] == "Twisted bow"
        assert processed["source"] == "Chambers of Xeric"
        assert processed["_received_at"] == stamp

    async def test_absent_stamp_leaves_no_key(self, common):
        """A payload with no envelope stamp falls back to now(), as before."""
        processed = await self._process(self._envelope(None))
        assert "_received_at" not in processed
        assert _close_to_now(common.received_at(processed))
