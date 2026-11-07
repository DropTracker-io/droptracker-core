"""Join-to-create voice rooms (suggestion #237): the pure rules."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "voice_rooms_core", _ROOT / "services" / "voice_rooms_core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


class TestRoomName:
    def test_default_template_uses_the_display_name(self):
        assert core.render_room_name(None, "Fazebook") == "Fazebook's Channel"

    def test_custom_template(self):
        assert core.render_room_name("🎧 {name}", "Zed") == "🎧 Zed"

    def test_blank_template_falls_back(self):
        assert core.render_room_name("   ", "Zed") == "Zed's Channel"

    def test_template_without_placeholder_is_used_as_written(self):
        assert core.render_room_name("Raid room", "Zed") == "Raid room"

    def test_long_names_fit_discord_limit(self):
        assert len(core.render_room_name(None, "x" * 300)) == core.NAME_MAX

    def test_missing_display_name(self):
        assert core.render_room_name(None, "") == "Member's Channel"


class TestHubMap:
    def test_only_real_snowflakes_count(self):
        rows = [(5, "1234567890"), (6, "0"), (7, ""), (8, None), (9, "General")]
        assert core.hub_map(rows) == {1234567890: 5}

    def test_lowest_group_wins_a_shared_hub(self):
        assert core.hub_map([(9, "111"), (2, "111")]) == {111: 2}


class TestRecords:
    def test_round_trip(self):
        raw = core.room_record(1, 2, 3, now=1000)
        rec = core.parse_record(raw.encode())
        assert rec == {"guild_id": "1", "owner_id": "2", "hub_id": "3", "created_at": 1000}

    def test_junk_parses_to_none(self):
        assert core.parse_record(None) is None
        assert core.parse_record(b"not json") is None
        assert core.parse_record(json.dumps([1, 2])) is None

    def test_owner_field(self):
        assert core.owner_field("10", 20) == "10:20"


class TestSweepable:
    def test_occupied_rooms_are_never_swept(self):
        assert core.sweepable({"created_at": 0}, 1, now=10_000) is False

    def test_a_fresh_empty_room_waits_for_its_owner(self):
        assert core.sweepable({"created_at": 1000}, 0, now=1000 + core.SWEEP_GRACE_S - 1) is False

    def test_an_old_empty_room_goes(self):
        assert core.sweepable({"created_at": 1000}, 0, now=1000 + core.SWEEP_GRACE_S) is True


def test_registry_declares_both_keys():
    from web_api.config_registry import GROUP_CONFIG_FIELDS

    defs = {d["key"]: d for d in GROUP_CONFIG_FIELDS}
    assert defs[core.HUB_KEY]["type"] == "channel"
    assert defs[core.HUB_KEY]["category"] == "voice"
    assert defs[core.NAME_KEY]["default"] == core.DEFAULT_NAME


class TestPermissionDiagnosis:
    """Shaped on Pegasus PvM's voice category (2026-11-07), before the bot was
    made Administrator: its role had Manage Channels server-wide, but the
    category's @everyone overwrite denied it, and Connect, back."""

    GUILD = "547922393507823667"
    BOT = "1172933457010245762"
    BOT_ROLE = "900"
    OFFICIAL = "901"

    def _roles(self, bot_role_perms):
        return {self.GUILD: 0, self.BOT_ROLE: bot_role_perms, self.OFFICIAL: 0}

    def _category(self):
        return [
            {"id": self.GUILD, "type": 0, "allow": "0",
             "deny": str(core.MANAGE_CHANNELS | core.CONNECT | core.MANAGE_ROLES)},
            {"id": self.OFFICIAL, "type": 0,
             "allow": str(core.MANAGE_CHANNELS | core.MANAGE_ROLES | (1 << 29)), "deny": "0"},
            {"id": self.BOT, "type": 1, "allow": str(core.MOVE_MEMBERS), "deny": "0"},
        ]

    def test_a_category_deny_takes_manage_channels_from_the_bot(self):
        role = core.MANAGE_CHANNELS | core.VIEW_CHANNEL | core.CONNECT
        perms = core.effective_permissions(self.GUILD, self.BOT, [self.BOT_ROLE],
                                           self._roles(role), self._category())
        missing = core.missing_room_permissions(perms, self._category())
        assert missing == ["Manage Channels", "Connect", "Manage Permissions"]

    def test_administrator_needs_nothing(self):
        perms = core.effective_permissions(self.GUILD, self.BOT, [self.BOT_ROLE],
                                           self._roles(core.ADMINISTRATOR), self._category())
        assert core.missing_room_permissions(perms, self._category()) == []

    def test_a_role_overwrite_on_the_category_fixes_it(self):
        everything = (core.VIEW_CHANNEL | core.MANAGE_CHANNELS | core.CONNECT
                      | core.MOVE_MEMBERS | core.MANAGE_ROLES)
        overwrites = self._category() + [
            {"id": self.BOT_ROLE, "type": 0, "allow": str(everything), "deny": "0"}]
        perms = core.effective_permissions(self.GUILD, self.BOT, [self.BOT_ROLE],
                                           self._roles(core.VIEW_CHANNEL), overwrites)
        assert core.missing_room_permissions(perms, overwrites) == []

    def test_overwrites_the_bot_cannot_copy_need_manage_permissions(self):
        base = core.VIEW_CHANNEL | core.MANAGE_CHANNELS | core.CONNECT | core.MOVE_MEMBERS
        overwrites = [{"id": self.OFFICIAL, "type": 0, "allow": str(1 << 29), "deny": "0"}]
        perms = core.effective_permissions(self.GUILD, self.BOT, [self.BOT_ROLE],
                                           self._roles(base), overwrites)
        assert core.missing_room_permissions(perms, overwrites) == ["Manage Permissions"]
        assert core.missing_room_permissions(perms, []) == []

    def test_notice_names_the_category_and_what_to_allow(self):
        text = core.permissions_notice(["Manage Channels", "Connect"], "Voice Channels")
        assert "**Voice Channels**" in text and "**Manage Channels, Connect**" in text
        assert "—" not in text
