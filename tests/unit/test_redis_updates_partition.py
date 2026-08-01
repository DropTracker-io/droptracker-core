"""
Regression tests for RedisLootTracker._get_partition honouring the drop's
own timestamp (month-boundary incident, 2026-08-01).

The webhook:queue backlog crossed the July→August 2026 UTC month rollover;
``_get_partition(dt)`` ignored ``dt`` and returned the wall-clock month, so
July-earned drops processed after midnight were credited to the 202608
monthly Redis keys (``leaderboard:{YYYYMM}``, ``player:{id}:{YYYYMM}:*``)
while the weekly/daily boards — which already derive their tokens from the
drop timestamp — stayed correct. These tests pin the monthly path to the
same rule: the partition comes from ``drop.date_added``; wall clock is only
the fallback when no timestamp is given.

Same loading idiom as test_redis_updates_wrappers.py: conftest stubs
``services.redis_updates`` in sys.modules, so the real module is loaded by
file path.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REAL_PATH = Path(__file__).resolve().parents[2] / "services" / "redis_updates.py"


@pytest.fixture()
def real_redis_updates():
    """Load the real services/redis_updates.py despite the conftest stub."""
    spec = importlib.util.spec_from_file_location("_real_redis_updates_partition", _REAL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_real_redis_updates_partition"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("_real_redis_updates_partition", None)


def _previous_month_dt() -> datetime:
    """Last minute of the previous calendar month, relative to now."""
    first_of_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_of_month - timedelta(minutes=1)


def _month_partition(dt: datetime) -> int:
    return dt.year * 100 + dt.month


class TestGetPartition:
    def test_no_argument_falls_back_to_current_month(self, real_redis_updates):
        tracker = real_redis_updates.RedisLootTracker()
        # Bracket with two now() reads so a month rollover mid-test can't flake.
        before = datetime.now()
        partition = tracker._get_partition()
        after = datetime.now()
        assert partition in {_month_partition(before), _month_partition(after)}

    def test_datetime_in_previous_month_returns_that_month(self, real_redis_updates):
        tracker = real_redis_updates.RedisLootTracker()
        prev_dt = _previous_month_dt()

        assert tracker._get_partition(prev_dt) == _month_partition(prev_dt)
        assert tracker._get_partition(prev_dt) != tracker._get_partition()

    def test_string_timestamp_is_coerced(self, real_redis_updates):
        tracker = real_redis_updates.RedisLootTracker()
        assert tracker._get_partition("2026-07-31 23:59:59") == 202607
        assert tracker._get_partition("2026-07-31 23:59:59.123456") == 202607

    def test_unparseable_string_falls_back_to_current_month(self, real_redis_updates):
        tracker = real_redis_updates.RedisLootTracker()
        before = datetime.now()
        partition = tracker._get_partition("not a timestamp")
        after = datetime.now()
        assert partition in {_month_partition(before), _month_partition(after)}


class TestLateProcessedDropMonthAttribution:
    """A drop whose date_added is in the previous month must be credited to
    THAT month's Redis keys, no matter what today's wall-clock month is —
    exactly what went wrong when the backlog crossed the 2026-08-01 boundary."""

    def test_add_to_player_credits_previous_month_keys(self, real_redis_updates):
        module = real_redis_updates
        fake_redis = MagicMock()
        module.redis_client = fake_redis
        pipe = fake_redis.client.pipeline.return_value

        tracker = module.RedisLootTracker()
        prev_dt = _previous_month_dt()
        prev_part = _month_partition(prev_dt)
        current_part = _month_partition(datetime.now())

        player = MagicMock()
        player.player_id = 123
        player.groups = []
        drop = SimpleNamespace(
            drop_id=1,
            item_id=4151,
            npc_id=3129,
            value=1000,
            quantity=2,
            date_added=prev_dt,
        )

        assert tracker.add_to_player(player, drop) is True

        zincrby_keys = [c.args[0] for c in pipe.zincrby.call_args_list]
        incrbyfloat_keys = [c.args[0] for c in pipe.incrbyfloat.call_args_list]

        # Monthly leaderboard, player monthly total and the per-NPC monthly
        # board all follow the drop's own month...
        assert f"leaderboard:{prev_part}" in zincrby_keys
        assert f"player:123:{prev_part}:total_loot" in incrbyfloat_keys
        assert f"leaderboard:npc:3129:{prev_part}" in zincrby_keys
        # ...and nothing lands on the current wall-clock month.
        assert f"leaderboard:{current_part}" not in zincrby_keys
        assert f"player:123:{current_part}:total_loot" not in incrbyfloat_keys
