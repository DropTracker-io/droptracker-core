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


class TestResolveChannelId:
    """Junk in ``vc_to_display_*`` must cost zero Discord requests.

    Every value here used to reach ``fetch_channel`` once per group per ten
    minutes — the ``'0'`` sentinel for sixteen groups, and ``'Cage'`` for group
    74, which raised ``ID (snowflake) should represent int`` on every pass.
    """

    def test_reads_a_snowflake(self):
        from services.channel_name_render import resolve_channel_id

        assert resolve_channel_id("1542566004398489630") == 1542566004398489630

    def test_unset_values_resolve_to_none(self):
        from services.channel_name_render import resolve_channel_id

        for value in (None, "", "   "):
            assert resolve_channel_id(value) is None

    def test_legacy_zero_sentinel_is_not_a_channel(self):
        """'0' is truthy as a string — the old code fetched channel 0 for it."""
        from services.channel_name_render import resolve_channel_id

        assert resolve_channel_id("0") is None
        assert resolve_channel_id(0) is None

    def test_free_text_is_not_a_channel(self):
        """The picker degrades to a text box with no cached voice channels."""
        from services.channel_name_render import resolve_channel_id

        assert resolve_channel_id("Cage") is None
        assert resolve_channel_id("#general") is None
        assert resolve_channel_id("123abc") is None


class TestRenderChannelName:
    """A template missing its placeholder must still show the number.

    Silently dropping it renders the static prefix and rewrites that same
    prefix forever — indistinguishable from a dead updater (group 30).
    """

    LOOT_DEFAULT = "{month}: {gp_amount} gp"
    MEMBER_DEFAULT = "{member_count} members"

    def _loot(self, template, month="September", gp="5.2M"):
        from services.channel_name_render import render_channel_name

        return render_channel_name(
            template, self.LOOT_DEFAULT, "{gp_amount}",
            {"{month}": month, "{gp_amount}": gp},
        )

    def _members(self, template, count="345"):
        from services.channel_name_render import render_channel_name

        return render_channel_name(
            template, self.MEMBER_DEFAULT, "{member_count}", {"{member_count}": count},
        )

    def test_substitutes_placeholders(self):
        assert self._loot("{month}: {gp_amount} gp") == "September: 5.2M gp"
        assert self._members("{member_count} members") == "345 members"

    def test_empty_template_uses_the_default(self):
        assert self._loot("") == "September: 5.2M gp"
        assert self._members(None) == "345 members"

    def test_template_without_its_placeholder_gets_the_value_appended(self):
        """Group 30's exact configuration — a prefix and no {gp_amount}."""
        assert self._loot("\U0001f4b0⥐Da-Loot:") == "\U0001f4b0⥐Da-Loot: 5.2M"
        assert self._members("\U0001f9d1⥐Member-Count:") == "\U0001f9d1⥐Member-Count: 345"

    def test_braced_whole_name_still_counts(self):
        """Group 30's earlier attempt: braces wrapped around the whole label."""
        assert self._loot("{Da Loot:}") == "{Da Loot:} 5.2M"

    def test_month_alone_does_not_satisfy_the_loot_template(self):
        """{month} is decoration; {gp_amount} is the number the counter exists for."""
        assert self._loot("{month} loot") == "September loot 5.2M"

    def test_appended_value_does_not_double_space(self):
        assert self._loot("Loot: ") == "Loot: 5.2M"

    def test_name_is_capped_at_the_discord_limit(self):
        """Discord rejects the whole edit over 100 chars, freezing the counter."""
        from services.channel_name_render import CHANNEL_NAME_MAX

        assert len(self._loot("x" * 200)) == CHANNEL_NAME_MAX == 100


class TestBothLoopsGateOnChannelType:
    """The member-count loop had no type gate and renamed text channels.

    Groups 286/287/301 had text channels sitting at `3-members`, `6-members`
    and `438-members` — Discord slugifies text-channel names, hence the dashes.
    """

    def test_member_loop_checks_the_channel_type(self):
        source = (REPO_ROOT / "services" / "channel_names.py").read_text()
        assert source.count("RENAMEABLE_CHANNEL_TYPES") >= 3, (
            "both the loot and member loops must gate on RENAMEABLE_CHANNEL_TYPES; "
            "the member loop renamed whatever channel it was pointed at"
        )

    def test_gate_covers_voice_and_stage_but_not_text(self):
        """Asserted on the source: `interactions` is a MagicMock under conftest,
        so importing ChannelType here would compare mocks, not channel kinds.
        GUILD_TEXT is 0 — falsy — which is why the gate is an `in` test against
        a tuple and never a truthiness check."""
        source = (REPO_ROOT / "services" / "channel_names.py").read_text()
        assert "RENAMEABLE_CHANNEL_TYPES = (ChannelType.GUILD_VOICE, ChannelType.GUILD_STAGE_VOICE)" in source
        assert source.count("channel.type not in RENAMEABLE_CHANNEL_TYPES") == 2, (
            "both loops must gate on the tuple"
        )


class TestFailuresNameTheGroup:
    """A 403 with no group_id in it is unattributable across ~90 channels."""

    def test_edit_failures_log_the_group_and_channel(self):
        source = (REPO_ROOT / "services" / "channel_names.py").read_text()
        for phrase in ("Couldn't edit loot channel", "Couldn't edit member channel"):
            assert phrase in source
        assert "for group {group_id}" in source
