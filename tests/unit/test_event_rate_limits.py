"""db/event_rate_limits.py — per-tier event frequency caps (web65a).

The conftest stubs ``db`` (callers import the module lazily), so these tests
load the REAL module by file path — the db.entitlements/event_types pattern —
and drive the pure evaluation layer (window_usage / evaluate_rules) plus the
cached-rules grant helpers with a pre-warmed cache. No DB is ever touched.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent.parent / "db" / "event_rate_limits.py"
_spec = importlib.util.spec_from_file_location("_real_event_rate_limits", _PATH)
erl = importlib.util.module_from_spec(_spec)
sys.modules["_real_event_rate_limits"] = erl
_spec.loader.exec_module(erl)

NOW = datetime(2026, 7, 21, 12, 0, 0)
DAY = timedelta(days=1)


def _warm(rules: dict) -> None:
    """Pre-warm the TTL cache so no session is ever touched."""
    erl._cache["rules"] = rules
    erl._cache["ts"] = time.monotonic()


@pytest.fixture(autouse=True)
def _reset_cache():
    yield
    erl.invalidate_cache()


S = object()  # session is never used with a warm cache


class TestWindowUsage:
    def test_open_slot(self):
        used, retry = erl.window_usage([NOW - 2 * DAY], 2, 7, NOW)
        assert used == 1
        assert retry is None

    def test_stamps_outside_window_do_not_count(self):
        used, retry = erl.window_usage([NOW - 8 * DAY, NOW - 30 * DAY], 1, 7, NOW)
        assert used == 0
        assert retry is None

    def test_at_limit_retry_is_oldest_plus_window(self):
        stamps = [NOW - 5 * DAY, NOW - 1 * DAY]
        used, retry = erl.window_usage(stamps, 2, 7, NOW)
        assert used == 2
        assert retry == NOW - 5 * DAY + 7 * DAY

    def test_over_limit_retry_frees_enough_slots(self):
        # 3 used against a cap of 2: a slot opens only when the count drops to
        # 1 — i.e. when the SECOND-oldest stamp ages out, not the oldest.
        stamps = [NOW - 6 * DAY, NOW - 4 * DAY, NOW - 1 * DAY]
        used, retry = erl.window_usage(stamps, 2, 7, NOW)
        assert used == 3
        assert retry == NOW - 4 * DAY + 7 * DAY

    def test_zero_cap_blocks_without_retry(self):
        used, retry = erl.window_usage([], 0, 30, NOW)
        assert used == 0
        assert retry is None

    def test_none_stamps_are_ignored(self):
        used, retry = erl.window_usage([None, NOW - 1 * DAY], 5, 7, NOW)
        assert used == 1
        assert retry is None


class TestEvaluateRules:
    def test_no_rules_is_unlimited(self):
        assert erl.evaluate_rules({}, "standard", {}, NOW) is None

    def test_under_limit_passes(self):
        rules = {"standard": {"max_events": 2, "window_days": 7}}
        stamps = {"standard": [NOW - 1 * DAY]}
        assert erl.evaluate_rules(rules, "standard", stamps, NOW) is None

    def test_per_kind_violation_reports_kind_scope(self):
        rules = {
            "standard": {"max_events": 1, "window_days": 7},
            erl.ALL_TYPES: {"max_events": 10, "window_days": 7},
        }
        stamps = {"standard": [NOW - 1 * DAY], erl.ALL_TYPES: [NOW - 1 * DAY]}
        v = erl.evaluate_rules(rules, "standard", stamps, NOW)
        assert v is not None
        assert v["scope"] == "standard"
        assert v["used"] == 1
        assert v["retry_at"] == NOW - 1 * DAY + 7 * DAY

    def test_global_rule_binds_other_kinds(self):
        # No board_game rule, but the all-kinds cap is exhausted by other runs.
        rules = {erl.ALL_TYPES: {"max_events": 2, "window_days": 30}}
        stamps = {erl.ALL_TYPES: [NOW - 3 * DAY, NOW - 2 * DAY]}
        v = erl.evaluate_rules(rules, "board_game", stamps, NOW)
        assert v is not None
        assert v["scope"] == erl.ALL_TYPES

    def test_rule_for_other_kind_does_not_bind(self):
        rules = {"bingo": {"max_events": 0, "window_days": 30}}
        assert erl.evaluate_rules(rules, "standard", {}, NOW) is None

    def test_zero_cap_violation_has_no_retry(self):
        rules = {"bingo": {"max_events": 0, "window_days": 30}}
        v = erl.evaluate_rules(rules, "bingo", {}, NOW)
        assert v is not None
        assert v["used"] == 0
        assert v["retry_at"] is None


class TestGrants:
    def test_no_rules_grants_nothing(self):
        _warm({})
        assert erl.rules_for_tier(S, "free") == {}

    def test_positive_rule_grants_rate_limited_access(self):
        _warm({"free": {erl.ALL_TYPES: {"max_events": 1, "window_days": 30}}})
        assert any(
            r["max_events"] > 0 for r in erl.rules_for_tier(S, "free").values()
        )

    def test_zero_only_rules_do_not_grant(self):
        _warm({"free": {"board_game": {"max_events": 0, "window_days": 30}}})
        assert not any(
            r["max_events"] > 0 for r in erl.rules_for_tier(S, "free").values()
        )

    def test_unknown_tier_has_no_rules(self):
        _warm({"free": {erl.ALL_TYPES: {"max_events": 1, "window_days": 30}}})
        assert erl.rules_for_tier(S, "t3") == {}
        assert erl.rules_for_tier(S, None) == {}


class TestDescribeViolation:
    def test_all_types_wording_and_retry(self):
        msg = erl.describe_violation({
            "scope": erl.ALL_TYPES, "max_events": 2, "window_days": 30,
            "used": 2, "retry_at": datetime(2026, 8, 1, 9, 30),
        })
        assert "2 event(s) per 30 days" in msg
        assert "already run 2" in msg
        assert "Aug 01, 2026 at 09:30" in msg

    def test_kind_label_is_used(self):
        msg = erl.describe_violation(
            {"scope": "board_game", "max_events": 1, "window_days": 7,
             "used": 1, "retry_at": None},
            kind_labels={"board_game": "Board Game"},
        )
        assert "Board Game event(s)" in msg

    def test_zero_cap_reads_as_not_allowed(self):
        msg = erl.describe_violation({
            "scope": "bingo", "max_events": 0, "window_days": 30,
            "used": 0, "retry_at": None,
        })
        assert "does not currently allow" in msg
