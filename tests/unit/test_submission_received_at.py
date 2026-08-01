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
for _name in ("api", "api.core", "osrs_api", "interactions"):
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
