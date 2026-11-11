"""The hourly WOM membership sync must not be skippable by cosmetic work.

Regression cover for the 2026-09-03 "the bot re-added everyone / ~380 messages"
report. Two defects combined:

  1. the due-check stamped the Redis gate *before returning True*, so the gate
     was spent on every ATTEMPT rather than on work happening; and
  2. the round awaited a cosmetic voice-channel rename BEFORE the sync,
     unguarded — and Discord rate-limits channel renames hard enough that
     interactions.py raises.

Together, one rate-limited rename silently cost a full hour of syncing: it hit
20 of 43 attempts (47%) over three days and stretched the "hourly" sync to gaps
of up to 7 hours. New clan members then accumulated unassociated until a sync
finally landed and announced the whole backlog at once.

Tests target services/group_sync_gate.py — the interactions-free half — so they
run without importing the Discord bot.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from services.group_sync_gate import GATE_INTERVAL, is_sync_due, run_group_sync_round


# ── the gate predicate ────────────────────────────────────────────────────────

def test_due_when_older_than_the_interval():
    assert is_sync_due((datetime.now() - GATE_INTERVAL - timedelta(minutes=1)).isoformat())


def test_not_due_inside_the_interval():
    assert not is_sync_due((datetime.now() - timedelta(minutes=5)).isoformat())


@pytest.mark.parametrize("bad", [None, "", "not-a-timestamp", "2026-13-45T99:99"])
def test_missing_or_corrupt_gate_reads_as_due(bad):
    """An unreadable gate must not wedge the sync forever."""
    assert is_sync_due(bad) is True


def test_is_sync_due_never_writes_anything():
    """PURE. The old version stamped the gate here, spending it on attempts."""
    stamp = (datetime.now() - timedelta(hours=3)).isoformat()
    assert is_sync_due(stamp) is True
    # calling it repeatedly cannot change the answer — nothing was consumed
    assert is_sync_due(stamp) is True
    assert is_sync_due(stamp) is True


# ── the round ─────────────────────────────────────────────────────────────────

def _round(*, due=True, sync_raises=False, label_raises=False):
    calls = []

    def is_due():
        calls.append("check")
        return due

    def claim():
        calls.append("claim")

    async def sync():
        calls.append("sync")
        if sync_raises:
            raise RuntimeError("db exploded")

    async def refresh_label():
        calls.append("label")
        if label_raises:
            raise RuntimeError("Attempted to lock a bucket that is already locked.")

    result = asyncio.run(run_group_sync_round(
        is_due=is_due, claim=claim, sync=sync, refresh_label=refresh_label,
        log=lambda m: calls.append(f"log:{m[:40]}"),
    ))
    return calls, result


def test_sync_runs_even_when_the_cosmetic_rename_raises():
    """THE REGRESSION. A rate-limited channel rename must not cost a sync."""
    calls, result = _round(label_raises=True)
    assert "sync" in calls, (
        "the sync did not run because the cosmetic rename raised — this is "
        "exactly the bug: one rate-limited rename silently costs an hour of "
        "membership syncing. The rename must be guarded and must run last."
    )
    assert result["synced"] is True
    assert result["label_failed"] is True


def test_rename_runs_after_the_sync():
    calls, _ = _round()
    assert calls.index("sync") < calls.index("label"), (
        "the cosmetic rename must not run before the sync it decorates"
    )


def test_gate_is_claimed_before_the_sync_and_immediately_after_the_check():
    """Check-and-claim must be adjacent so overlapping rounds can't both pass."""
    calls, _ = _round()
    assert calls[:3] == ["check", "claim", "sync"]


def test_gate_is_not_claimed_when_not_due():
    calls, result = _round(due=False)
    assert "claim" not in calls
    assert "sync" not in calls
    assert result["synced"] is False


def test_label_still_refreshes_on_a_skipped_tick():
    calls, _ = _round(due=False)
    assert "label" in calls, "the countdown label should still refresh when no sync is due"


def test_a_failing_sync_is_logged_not_propagated():
    calls, result = _round(sync_raises=True)
    assert result["sync_failed"] is True
    assert any(c.startswith("log:") for c in calls), "a lost round must be logged, not silent"
    assert "label" in calls, "a failed sync must not also skip the label"


# --------------------------------------------------------------------------- #
# Per-group cadence (2026-11-11): 12 hours, 6 on a paid tier. WiseOldMan asked
# us to cut volume and the every-round, every-group fetch was 59% of it.
# --------------------------------------------------------------------------- #
import sys

# The real module, as conftest registers it. `import services.x as y` and
# `from services import x` both resolve through the stub package to a MagicMock.
gate = sys.modules["services.group_sync_gate"]

HOUR = 3600
NOW = 1_800_000_000.0


class TestGroupSyncDue:
    def test_never_synced_group_is_due(self):
        assert gate.group_sync_due(None, None, False, NOW)

    def test_unreadable_stamp_is_due(self):
        assert gate.group_sync_due(b"garbage", None, False, NOW)

    def test_free_group_waits_twelve_hours(self):
        assert not gate.group_sync_due(NOW - 6 * HOUR, None, False, NOW)
        assert not gate.group_sync_due(NOW - 11 * HOUR, None, False, NOW)
        assert gate.group_sync_due(NOW - 12 * HOUR, None, False, NOW)

    def test_paid_group_waits_six_hours(self):
        assert not gate.group_sync_due(NOW - 5 * HOUR, None, True, NOW)
        assert gate.group_sync_due(NOW - 6 * HOUR, None, True, NOW)

    def test_hourly_tick_does_not_slip_a_round(self):
        # Stamped a few minutes into the round 12 ticks ago: still due now.
        assert gate.group_sync_due(NOW - 12 * HOUR + 300, None, False, NOW)

    def test_failed_fetch_retries_after_three_hours_not_every_round(self):
        assert not gate.group_sync_due(None, NOW - 1 * HOUR, False, NOW)
        assert gate.group_sync_due(None, NOW - 3 * HOUR, False, NOW)

    def test_a_failure_never_brings_a_fresh_group_forward(self):
        assert not gate.group_sync_due(NOW - 2 * HOUR, NOW - 4 * HOUR, False, NOW)

    def test_redis_bytes_are_read(self):
        assert not gate.group_sync_due(str(int(NOW - HOUR)).encode(), None, False, NOW)


class TestDueWomIds:
    def test_picks_due_groups_and_keeps_order(self):
        ok = {b"1": str(int(NOW - 13 * HOUR)).encode(),   # free, due
              b"2": str(int(NOW - 7 * HOUR)).encode(),    # free, not due
              b"3": str(int(NOW - 7 * HOUR)).encode()}    # paid, due
        due = gate.due_wom_ids([3, "2", 1, 4], {"3"}, ok, {}, NOW)
        assert due == [3, 1, 4]

    def test_empty_schedule_means_everyone(self):
        assert gate.due_wom_ids([1, 2], set(), {}, None, NOW) == [1, 2]

    def test_a_steady_day_is_two_syncs_free_and_four_paid(self):
        def syncs_in_a_day(is_paid):
            last, count = None, 0
            for tick in range(24):
                now = NOW + tick * HOUR
                if gate.group_sync_due(last, None, is_paid, now):
                    last, count = now + 240, count + 1  # stamped mid-round
            return count
        assert syncs_in_a_day(False) == 2
        assert syncs_in_a_day(True) == 4
