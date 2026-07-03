"""
Regression tests for the module-level convenience wrappers in
services/redis_updates.py.

data/submissions/drop.py imports the *module* (`from services import
redis_updates`) and calls `redis_updates.add_to_player(...)` /
`redis_updates.add_split_credit(...)` on it — not on the `loot_tracker`
instance. Because the surrounding try/except in drop_processor swallows
errors, a signature mismatch between the call site and the module-level
wrapper silently disables all Redis player/leaderboard updates while the
rest of the pipeline (DB row, notifications) keeps working.

That exact failure shipped once: the RedisLootTracker.add_to_player METHOD
gained `item_name`/`npc_name` kwargs, the call site in drop.py started
passing them, but the module-level wrapper kept the old signature and every
call raised TypeError. These tests load the real module (conftest stubs it
in sys.modules for other tests) and pin the wrapper contracts.
"""

import importlib.util
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REAL_PATH = Path(__file__).resolve().parents[2] / "services" / "redis_updates.py"


@pytest.fixture()
def real_redis_updates():
    """Load the real services/redis_updates.py despite the conftest stub."""
    spec = importlib.util.spec_from_file_location("_real_redis_updates", _REAL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_real_redis_updates"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("_real_redis_updates", None)


class TestModuleLevelWrappers:
    def test_add_to_player_accepts_drop_processor_call_signature(self, real_redis_updates):
        """drop_processor passes item_name/npc_name — the wrapper must accept them."""
        sig = inspect.signature(real_redis_updates.add_to_player)
        # Must not raise TypeError:
        sig.bind(
            MagicMock(),  # player
            MagicMock(),  # drop
            world_type="main",
            item_name="Twisted bow",
            npc_name="Chambers of Xeric",
        )

    def test_add_to_player_forwards_to_loot_tracker(self, real_redis_updates):
        tracker = MagicMock()
        real_redis_updates.loot_tracker = tracker
        player, drop = MagicMock(), MagicMock()

        real_redis_updates.add_to_player(
            player, drop, world_type="seasonal", item_name="Enhanced crystal weapon seed", npc_name="The Gauntlet",
        )

        tracker.add_to_player.assert_called_once_with(
            player,
            drop,
            world_type="seasonal",
            item_name="Enhanced crystal weapon seed",
            npc_name="The Gauntlet",
        )

    def test_add_split_credit_exists_and_accepts_drop_processor_call_signature(self, real_redis_updates):
        """_award_split_gp_credits calls redis_updates.add_split_credit(...) on the module."""
        assert hasattr(real_redis_updates, "add_split_credit")
        sig = inspect.signature(real_redis_updates.add_split_credit)
        # Positional style used in drop.py:
        sig.bind(123, -500000, 202607, 42, "main")

    def test_add_split_credit_forwards_to_loot_tracker(self, real_redis_updates):
        tracker = MagicMock()
        real_redis_updates.loot_tracker = tracker

        real_redis_updates.add_split_credit(123, 250000, 202607, 42, "main")

        tracker.add_split_credit.assert_called_once_with(123, 250000, 202607, 42, "main")
