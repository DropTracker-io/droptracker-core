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
