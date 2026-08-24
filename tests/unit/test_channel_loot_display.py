"""The voice-channel GP counter must show the lootboard's number, not its own.

``services/channel_names.py`` used to sum every WOM member's *global*
``player:{id}:{partition}:total_loot``, so it counted loot the board excludes:
``ignored_players``, this group's drop-moderation exclusions and split-GP
credits. A Pegasus PvM (group 14) leader hid three members and the channel sat
984,615,122 gp above the group's own board, widening daily (suggestion #138).

These tests pin the fix: the counter reads the total the board published to
``gleaderboard:{partition}``, and it never goes back to re-deriving one.
"""

import ast
import sys
from pathlib import Path

from services.group_loot_totals import board_month_total, group_totals_key


REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeRedis:
    def __init__(self, scores=None, raises=False):
        self.scores = scores or {}
        self.raises = raises
        self.calls = []

    def zscore(self, key, member):
        self.calls.append((key, member))
        if self.raises:
            raise RuntimeError("redis down")
        return self.scores.get((key, member))


class TestBoardMonthTotal:
    def test_reads_the_published_board_total(self):
        conn = FakeRedis({("gleaderboard:202608", 14): 6_584_936_433.0})
        assert board_month_total(14, 202608, redis_conn=conn) == 6_584_936_433
        assert conn.calls == [("gleaderboard:202608", 14)]

    def test_group_id_is_coerced_for_lookup(self):
        """Config rows hand us whatever the column holds; the zset member is an int."""
        conn = FakeRedis({("gleaderboard:202608", 14): 1_000.0})
        assert board_month_total("14", 202608, redis_conn=conn) == 1_000

    def test_unrendered_group_returns_none_rather_than_zero(self):
        """Zero would rename the channel to '0 gp'; None leaves it alone."""
        conn = FakeRedis()
        assert board_month_total(14, 202608, redis_conn=conn) is None

    def test_zero_total_is_not_confused_with_a_missing_board(self):
        conn = FakeRedis({("gleaderboard:202609", 14): 0.0})
        assert board_month_total(14, 202609, redis_conn=conn) == 0

    def test_redis_failure_returns_none(self):
        assert board_month_total(14, 202608, redis_conn=FakeRedis(raises=True)) is None

    def test_no_redis_connection_returns_none(self, monkeypatch):
        """Redis unconfigured: fall through to 'no total', never to 0."""
        redis_module = sys.modules["utils.redis"]
        monkeypatch.setattr(redis_module.redis_client, "client", None, raising=False)
        assert board_month_total(14, 202608) is None

    def test_key_matches_what_the_lootboard_writes(self):
        """A key-format change on either side silently freezes every counter."""
        source = (REPO_ROOT / "lootboard" / "generator.py").read_text()
        assert "zadd(f'gleaderboard:{partition}'" in source, (
            "lootboard/generator.py no longer publishes gleaderboard:{partition}; "
            "services/group_loot_totals.py reads that key"
        )
        assert group_totals_key(202608) == "gleaderboard:202608"

    def test_web_api_reads_the_same_key(self):
        """Board, website groups tab and voice channel must share one total."""
        from web_api.common import group_totals_key as web_key

        assert web_key(202608) == group_totals_key(202608)


class TestCounterHasNoPrivateArithmetic:
    """The counter must not grow its own copy of the board's total again.

    Asserted against the parsed tree rather than the raw text so a comment
    describing the old behaviour doesn't count as the behaviour.
    """

    @staticmethod
    def _tree():
        source = (REPO_ROOT / "services" / "channel_names.py").read_text()
        return ast.parse(source)

    def test_does_not_sum_per_player_redis_totals(self):
        code = ast.dump(self._tree())
        assert "total_loot" not in code, (
            "channel_names.py is reading per-player loot keys again — that skips "
            "the board's ignored_players/exclusion/split adjustments"
        )

    def test_does_not_build_its_own_roster(self):
        code = ast.dump(self._tree())
        for symbol in ("fetch_group_members", "associate_player_ids"):
            assert symbol not in code, (
                f"channel_names.py resolves its own roster via {symbol}; the "
                f"published board total already accounts for membership"
            )

    def test_uses_the_shared_helper(self):
        imported = {
            alias.name
            for node in ast.walk(self._tree())
            if isinstance(node, ast.ImportFrom) and node.module == "services.group_loot_totals"
            for alias in node.names
        }
        assert "board_month_total" in imported
