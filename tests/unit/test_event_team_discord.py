"""Unit tests for the pure config/routing layer of
services/event_team_discord.py (web53a per-team Discord channels & roles).

The module is loaded directly from its file path (module-level imports are
stdlib-only) so the conftest ``db``/``services`` stubs never interfere —
the same pattern as test_event_notifications.py.
"""

import importlib.util
import os
import sys

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_team_discord.py",
)
_spec = importlib.util.spec_from_file_location("_event_team_discord_under_test", _MODULE_PATH)
etd = importlib.util.module_from_spec(_spec)
sys.modules["_event_team_discord_under_test"] = etd
_spec.loader.exec_module(etd)


# ── effective_team_discord_config ────────────────────────────────────────────

class TestEffectiveConfig:
    def test_null_yields_defaults_off(self):
        config = etd.effective_team_discord_config(None)
        assert config["channels_enabled"] is False
        assert config["roles_enabled"] is False
        assert config["forum_channel_id"] is None
        assert config["retention"] == "delete_48h"
        assert config["captain_config"] is True
        assert config["teams"] == {}
        assert not etd.config_enabled(config)

    def test_corrupt_json_yields_defaults(self):
        assert etd.effective_team_discord_config("{not json") == \
            etd.effective_team_discord_config(None)
        assert etd.effective_team_discord_config('[1,2]') == \
            etd.effective_team_discord_config(None)

    def test_overrides_and_unknown_keys(self):
        config = etd.effective_team_discord_config(
            '{"channels_enabled": true, "retention": "keep", '
            '"forum_channel_id": 123456, "bogus": 1, '
            '"teams": {"9": {"role": false, "junk": 2, '
            '"toggles": {"event_lead_change": false, "unknown_toggle": true}, '
            '"task_progress": "milestones"}}}'
        )
        assert config["channels_enabled"] is True
        assert config["retention"] == "keep"
        assert config["forum_channel_id"] == "123456"
        assert "bogus" not in config
        team = config["teams"]["9"]
        assert team["role"] is False
        assert "junk" not in team
        assert team["toggles"] == {"event_lead_change": False}
        assert team["task_progress"] == "milestones"

    def test_bad_values_ignored(self):
        config = etd.effective_team_discord_config(
            '{"retention": "forever", "forum_channel_id": "abc", '
            '"channels_enabled": "yes"}'
        )
        assert config["retention"] == "delete_48h"
        assert config["forum_channel_id"] is None
        assert config["channels_enabled"] is False

    def test_category_channel_id_normalizes(self):
        # Per-team private channels live under this category (int or str id).
        assert etd.effective_team_discord_config(
            '{"category_channel_id": 987654}')["category_channel_id"] == "987654"
        assert etd.effective_team_discord_config(
            '{"category_channel_id": "111"}')["category_channel_id"] == "111"

    def test_category_channel_id_defaults_and_rejects_garbage(self):
        assert etd.effective_team_discord_config(None)["category_channel_id"] is None
        assert etd.effective_team_discord_config(
            '{"category_channel_id": "not-a-number"}')["category_channel_id"] is None


# ── per-team flags & toggles ─────────────────────────────────────────────────

class TestTeamFlags:
    def test_scope_toggle_ands_with_per_team(self):
        config = etd.effective_team_discord_config(
            '{"channels_enabled": true, "roles_enabled": true, '
            '"teams": {"5": {"role": false}}}'
        )
        assert etd.team_flags(config, 5) == {"role": False, "channel": True}
        # Absent team entry = both on.
        assert etd.team_flags(config, 6) == {"role": True, "channel": True}

    def test_disabled_scope_beats_per_team_on(self):
        config = etd.effective_team_discord_config(
            '{"roles_enabled": false, "teams": {"5": {"role": true}}}'
        )
        assert etd.team_flags(config, 5)["role"] is False

    def test_message_toggles_merge_defaults(self):
        config = etd.effective_team_discord_config(
            '{"teams": {"5": {"toggles": {"event_board_turn": false}}}}'
        )
        toggles = etd.team_message_toggles(config, 5)
        assert toggles["event_board_turn"] is False
        assert toggles["event_completion"] is True
        assert toggles["event_board_roll_prompt"] is True  # team-channel default ON

    def test_task_progress_static_default_milestones(self):
        config = etd.effective_team_discord_config(None)
        assert etd.team_task_progress_mode(config, 1) == "milestones"
        config = etd.effective_team_discord_config(
            '{"teams": {"1": {"task_progress": "off"}}}')
        assert etd.team_task_progress_mode(config, 1) == "off"


# ── channel naming ───────────────────────────────────────────────────────────

class TestChannelName:
    def test_slugified_with_prefix(self):
        assert etd.channel_name_for_team("The Reds!") == "team-the-reds"
        assert etd.channel_name_for_team("  A  B  ") == "team-a-b"

    def test_empty_falls_back(self):
        assert etd.channel_name_for_team("") == "team-team"
        assert etd.channel_name_for_team("!!!") == "team-team"

    def test_capped_length(self):
        assert len(etd.channel_name_for_team("x" * 300)) <= 90


# ── notification-destination gating (pure parts) ─────────────────────────────

class TestTeamScopedTypes:
    def test_roll_prompt_and_progress_are_team_scoped(self):
        assert "event_board_roll_prompt" in etd.TEAM_SCOPED_TYPES
        assert "event_task_progress" in etd.TEAM_SCOPED_TYPES
        # Lead change deliberately NOT team-scoped (goes to every team channel).
        assert "event_lead_change" not in etd.TEAM_SCOPED_TYPES

    def test_defaults_cover_every_routable_type(self):
        # Every type load_team_destinations can route must have a default.
        for t in etd.TEAM_SCOPED_TYPES + ("event_lead_change",):
            assert t in etd.DEFAULT_TEAM_MESSAGE_TOGGLES


# ── inherited defaults (event verbosity is the team baseline) ────────────────

class TestInheritedDefaults:
    def _event_cfg(self, **over):
        base = {
            "toggles": {
                "event_completion": True,
                "event_task_progress": True,
                "event_line": False,
                "event_blackout": True,
                "event_lead_change": False,
                "event_board_turn": True,
                # The group left roll prompts at their main-channel default
                # (off) — teams must NOT inherit that (team-channel-native).
                "event_board_roll_prompt": False,
            },
            "task_progress": "milestones",
        }
        base.update(over)
        return base

    def test_group_verbosity_becomes_team_baseline(self):
        inherited = etd.inherited_team_defaults(self._event_cfg())
        assert inherited["toggles"]["event_line"] is False
        assert inherited["toggles"]["event_lead_change"] is False
        assert inherited["toggles"]["event_completion"] is True
        assert inherited["task_progress"] == "milestones"

    def test_roll_prompt_never_inherited(self):
        inherited = etd.inherited_team_defaults(self._event_cfg())
        assert inherited["toggles"]["event_board_roll_prompt"] is True

    def test_none_config_falls_back_to_static_defaults(self):
        inherited = etd.inherited_team_defaults(None)
        assert inherited["toggles"] == etd.DEFAULT_TEAM_MESSAGE_TOGGLES
        assert inherited["task_progress"] == etd.DEFAULT_TEAM_TASK_PROGRESS

    def test_explicit_team_choice_beats_inheritance(self):
        inherited = etd.inherited_team_defaults(self._event_cfg())
        config = etd.effective_team_discord_config(
            '{"teams": {"5": {"toggles": {"event_line": true},'
            ' "task_progress": "all"}}}')
        toggles = etd.team_message_toggles(config, 5, inherited=inherited)
        assert toggles["event_line"] is True          # explicit override
        assert toggles["event_lead_change"] is False  # still inherited
        assert etd.team_task_progress_mode(config, 5, inherited=inherited) == "all"
        # A different team keeps the inherited baseline untouched.
        assert etd.team_task_progress_mode(config, 6, inherited=inherited) == "milestones"


# ── per-type @TeamRole pings ─────────────────────────────────────────────────

class TestTeamMessagePings:
    def test_defaults_quiet_for_noisy_types(self):
        config = etd.effective_team_discord_config(None)
        pings = etd.team_message_pings(config, 1)
        assert pings["event_task_progress"] is False
        assert pings["event_board_turn"] is False
        assert pings["event_completion"] is True
        assert pings["event_board_roll_prompt"] is True

    def test_explicit_ping_choice_wins_and_unknown_keys_dropped(self):
        config = etd.effective_team_discord_config(
            '{"teams": {"5": {"pings": {"event_task_progress": true,'
            ' "bogus_type": true}}}}')
        pings = etd.team_message_pings(config, 5)
        assert pings["event_task_progress"] is True
        assert "bogus_type" not in pings

    def test_every_toggle_type_has_a_ping_default(self):
        assert set(etd.DEFAULT_TEAM_MESSAGE_PINGS) == set(etd.DEFAULT_TEAM_MESSAGE_TOGGLES)
