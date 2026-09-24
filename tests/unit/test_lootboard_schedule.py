"""lootboard/schedule.py: per-tier redraw cadence and instant redraws.

The tiers' promise (Free every 30 min, Supporter 10, Sponsor 10 + instant,
Patron 5 + instant) is data on the tier rows; what this pins is that the
schedule honours whatever the entitlements say, and that the instant path
debounces a burst of drops without losing the last one.
"""
from __future__ import annotations

import pytest

from lootboard import schedule as sch


class FakeRedis:
    """The handful of redis-py calls schedule.py makes, with TTLs ignored
    except for NX semantics."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    def exists(self, key):
        return 1 if key in self.store else 0

    def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def scan_iter(self, match=None, count=None):
        prefix = match.rstrip("*")
        return [k.encode() for k in list(self.store) if k.startswith(prefix)]

    def pipeline(self):
        outer = self

        class _Pipe:
            def __init__(self):
                self.ops = []

            def exists(self, key):
                self.ops.append(key)

            def execute(self):
                return [outer.exists(k) for k in self.ops]

        return _Pipe()


FREE = sch.LootboardPolicy(30, False)
SUPPORTER = sch.LootboardPolicy(10, False)
SPONSOR = sch.LootboardPolicy(10, True)
PATRON = sch.LootboardPolicy(5, True)


class TestPolicy:
    def test_reads_both_entitlements(self):
        assert sch.policy_from_entitlements(
            {"lootboard_refresh_minutes": 5, "lootboard_instant": True}
        ) == PATRON

    @pytest.mark.parametrize("value", [0, None, "junk", -3])
    def test_unset_or_bad_minutes_fall_back_to_default(self, value):
        policy = sch.policy_from_entitlements({"lootboard_refresh_minutes": value})
        assert policy.refresh_minutes == sch.DEFAULT_REFRESH_MINUTES
        assert policy.instant is False

    def test_no_entitlements_is_the_free_schedule(self):
        assert sch.policy_from_entitlements(None) == sch.DEFAULT_POLICY


class TestScheduledDue:
    def test_never_drawn_is_due(self):
        assert sch.scheduled_due(FREE, None, 1000.0)

    def test_due_after_interval(self):
        now = 100_000.0
        assert not sch.scheduled_due(PATRON, now - 4 * 60, now)
        assert sch.scheduled_due(PATRON, now - 5 * 60, now)

    def test_slack_keeps_a_board_on_its_interval(self):
        # Drawn 10 s into the previous pass: the next pass a whole interval
        # later still counts it due rather than leaving it another minute.
        now = 100_000.0
        assert sch.scheduled_due(SUPPORTER, now - (10 * 60 - 10), now)

    def test_next_refresh_is_never_in_the_past(self):
        now = 100_000.0
        assert sch.next_refresh_at(FREE, now - 3600, now) > now
        assert sch.next_refresh_at(FREE, now, now) >= now + 30 * 60


class TestPlanPass:
    def test_instant_only_for_tiers_that_have_it(self):
        now = 100_000.0
        policies = {1: FREE, 2: SPONSOR, 3: PATRON}
        mtimes = {1: now - 60, 2: now - 60, 3: now - 60}
        instant, scheduled, deferred = sch.plan_pass(
            [1, 2, 3], policies, {1, 2, 3}, now, mtimes, 40
        )
        assert instant == [2, 3]
        assert scheduled == [] and deferred == 0

    def test_scheduled_takes_most_overdue_first_and_caps(self):
        now = 100_000.0
        policies = {g: FREE for g in range(1, 6)}
        # Group 5 is the most overdue, group 1 the least; group 6 not due.
        mtimes = {g: now - (30 * 60 + g * 100) for g in range(1, 6)}
        policies[6] = FREE
        mtimes[6] = now - 60
        instant, scheduled, deferred = sch.plan_pass(
            list(range(1, 7)), policies, set(), now, mtimes, 3
        )
        assert instant == []
        assert scheduled == [5, 4, 3]
        assert deferred == 2

    def test_instant_board_is_not_drawn_twice_in_one_pass(self):
        now = 100_000.0
        instant, scheduled, _ = sch.plan_pass(
            [7], {7: PATRON}, {7}, now, {7: now - 3600}, 40
        )
        assert instant == [7] and scheduled == []


class TestInstantDebounce:
    def test_mark_dirty_needs_the_entitlement(self, monkeypatch):
        r = FakeRedis()
        monkeypatch.setattr(sch, "_redis", lambda: r)
        # The unit suite stubs the `db` package, so `import db.entitlements`
        # would bind the stub's attribute; patch the module mark_dirty imports.
        import importlib
        import sys

        importlib.import_module("db.entitlements")
        monkeypatch.setattr(
            sys.modules["db.entitlements"], "group_has_entitlement",
            lambda gid, key: gid == 10 and key == "lootboard_instant", raising=False,
        )
        assert sch.mark_dirty(10) is True
        assert sch.mark_dirty(11) is False
        assert sch.mark_dirty(sch.GLOBAL_GROUP_ID) is False
        assert sch.mark_dirty(None) is False
        assert sch.dirty_group_ids(r) == {10}

    def test_burst_draws_once_then_catches_the_last_drop(self):
        r = FakeRedis()
        r.set(sch.dirty_key(10), "1")
        assert sch.ready_dirty_group_ids(r) == {10}

        # First pass claims it: cooldown starts, flag cleared.
        assert sch.claim_instant(10, r) is True
        assert sch.ready_dirty_group_ids(r) == set()

        # More drops during the cooldown: flagged, but not ready yet.
        r.set(sch.dirty_key(10), "1")
        r.set(sch.dirty_key(10), "1")
        assert sch.dirty_group_ids(r) == {10}
        assert sch.ready_dirty_group_ids(r) == set()
        assert sch.claim_instant(10, r) is False

        # Cooldown lapses: the pending drop is still there to draw.
        r.delete(sch.cooldown_key(10))
        assert sch.ready_dirty_group_ids(r) == {10}
        assert sch.claim_instant(10, r) is True

    def test_scheduled_redraw_consumes_a_pending_drop(self):
        r = FakeRedis()
        r.set(sch.dirty_key(10), "1")
        sch.consume_for_scheduled(10, r)
        assert sch.dirty_group_ids(r) == set()
        assert r.exists(sch.cooldown_key(10))

    def test_scheduled_redraw_without_a_drop_starts_no_cooldown(self):
        r = FakeRedis()
        sch.consume_for_scheduled(10, r)
        assert not r.exists(sch.cooldown_key(10))


class TestPosting:
    def test_posts_only_a_newer_image(self):
        assert sch.needs_post(1000.0, 0.0)
        assert not sch.needs_post(1000.0, 1000.0)
        assert sch.needs_post(1060.0, 1000.0)
        assert not sch.needs_post(None, 0.0)

    def test_posted_round_trip(self):
        r = FakeRedis()
        sch.record_posted(4, 1234.5, r)
        assert sch.posted_mtimes([4, 5], r) == {4: 1234.5, 5: 0.0}
