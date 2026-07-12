"""Per-NPC loot leaderboard writer contract.

``services/redis_updates.RedisLootTracker._increment_npc_leaderboards`` is the
only writer of the per-NPC sorted sets. Its key format MUST stay identical to
the readers, or the Hall of Fame "Most Loot" section and the website's NPC
leaderboards silently show nothing again:

  * services/hall_of_fame.py  (HOF loot section)
  * web_api/common.py::npc_leaderboard_key  (website)

These tests pin the exact keys and the month-only TTL against a fake pipeline,
so drift is caught without touching a real Redis.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_REAL_PATH = Path(__file__).resolve().parents[2] / "services" / "redis_updates.py"


@pytest.fixture()
def real_redis_updates():
    """Load the real services/redis_updates.py despite the conftest stub."""
    spec = importlib.util.spec_from_file_location("_real_redis_updates_npc", _REAL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_real_redis_updates_npc"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("_real_redis_updates_npc", None)


class _Recorder:
    """Stands in for a Redis pipeline, recording the writes for assertions."""

    def __init__(self):
        self.zincrby_calls = []
        self.expire_calls = []

    def zincrby(self, key, amount, member):
        self.zincrby_calls.append((key, amount, member))

    def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))

    def execute(self):
        pass


class _FakeClient:
    def __init__(self, recorder):
        self._recorder = recorder

    def pipeline(self, transaction=True):
        return self._recorder


class _FakeRedis:
    def __init__(self, recorder):
        self.client = _FakeClient(recorder)


def _run(module, **kwargs):
    recorder = _Recorder()
    module.redis_client = _FakeRedis(recorder)
    module.loot_tracker._increment_npc_leaderboards(**kwargs)
    return recorder


class TestNpcLeaderboardWriter:
    def test_writes_global_and_group_keys(self, real_redis_updates):
        rec = _run(
            real_redis_updates,
            player_id=7, value_delta=1000, npc_id=42,
            partition=202607, group_ids=[5], prefix="",
        )
        keys = {k for k, _, _ in rec.zincrby_calls}
        # Global month key MUST equal web_api.common.npc_leaderboard_key(202607, 42).
        assert "leaderboard:npc:42:202607" in keys
        # Global all-time (HOF "Total loot tracked").
        assert "leaderboard:npc:42" in keys
        # Per-group month + all-time (HOF per-group "Most Loot").
        assert "leaderboard:group:5:npc:42:202607" in keys
        assert "leaderboard:group:5:npc:42" in keys
        # Score credited to the right member, by the drop value.
        assert ("leaderboard:npc:42:202607", 1000, 7) in rec.zincrby_calls

    def test_month_keys_have_ttl_alltime_persists(self, real_redis_updates):
        rec = _run(
            real_redis_updates,
            player_id=7, value_delta=1000, npc_id=42,
            partition=202607, group_ids=[5], prefix="",
        )
        expired = {k for k, _ in rec.expire_calls}
        assert "leaderboard:npc:42:202607" in expired
        assert "leaderboard:group:5:npc:42:202607" in expired
        # All-time boards must never expire.
        assert "leaderboard:npc:42" not in expired
        assert "leaderboard:group:5:npc:42" not in expired

    def test_seasonal_prefix_isolated(self, real_redis_updates):
        rec = _run(
            real_redis_updates,
            player_id=7, value_delta=1000, npc_id=42,
            partition=202607, group_ids=None, prefix="seasonal:",
        )
        keys = {k for k, _, _ in rec.zincrby_calls}
        assert "seasonal:leaderboard:npc:42:202607" in keys
        assert all(k.startswith("seasonal:") for k in keys)

    @pytest.mark.parametrize("npc_id,value", [(0, 1000), (None, 1000), (42, 0)])
    def test_noop_for_missing_npc_or_zero_value(self, real_redis_updates, npc_id, value):
        rec = _run(
            real_redis_updates,
            player_id=7, value_delta=value, npc_id=npc_id,
            partition=202607, group_ids=[5], prefix="",
        )
        assert rec.zincrby_calls == []
        assert rec.expire_calls == []
