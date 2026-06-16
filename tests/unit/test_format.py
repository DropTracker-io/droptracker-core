"""
Unit tests for utils/format.py — pure utility functions with no external deps.
"""

import pytest
from utils.format import (
    convert_from_ms,
    convert_to_ms,
    format_number,
    get_current_partition,
    get_extension_from_content_type,
    normalize_player_display_equivalence,
    normalize_claim_rsn_input,
    replace_placeholders_in_text,
)


# ── format_number ─────────────────────────────────────────────────────────────

class TestFormatNumber:
    def test_billions(self):
        assert format_number(1_500_000_000) == "1.500B"

    def test_billions_exact(self):
        assert format_number(1_000_000_000) == "1.000B"

    def test_millions(self):
        assert format_number(2_500_000) == "2.50M"

    def test_millions_exact(self):
        assert format_number(1_000_000) == "1.00M"

    def test_thousands(self):
        assert format_number(15_000) == "15.00K"

    def test_thousands_exact(self):
        assert format_number(1_000) == "1.00K"

    def test_small_number(self):
        assert format_number(500) == "500"

    def test_single_digit(self):
        assert format_number(7) == "7"

    def test_zero(self):
        assert format_number(0) == "0"

    def test_none(self):
        assert format_number(None) == "0"

    def test_bytes_input(self):
        # format_number decodes bytes before processing
        assert format_number(b"2500000") == "2.50M"

    def test_float_string(self):
        # Accepts numeric strings via float() → int() conversion
        assert format_number(1000000.0) == "1.00M"


# ── convert_to_ms / convert_from_ms ──────────────────────────────────────────

class TestConvertToMs:
    def test_minutes_seconds_with_ticks(self):
        # 1:33.00 → 93 seconds + 0 ticks → 93000 ms
        assert convert_to_ms("1:33.00") == 93000

    def test_sub_minute_with_ticks(self):
        # 0:50.40 → 50 seconds + 40 ticks (400 ms) → 50400 ms
        assert convert_to_ms("0:50.40") == 50400

    def test_no_ticks(self):
        # 2:00 (no decimal) → 120 seconds → 120000 ms
        assert convert_to_ms("2:00") == 120000

    def test_hours_minutes_seconds(self):
        # 1:02:30.00 → 3750 seconds → 3750000 ms
        assert convert_to_ms("1:02:30.00") == 3750000

    def test_two_digit_minutes(self):
        # 10:00.00 → 600 seconds → 600000 ms
        assert convert_to_ms("10:00.00") == 600000

    def test_returns_none_for_invalid(self):
        # No colons → neither branch matches
        assert convert_to_ms("invalid") is None


class TestConvertFromMs:
    def test_minutes_seconds(self):
        # 93000 ms → 1:33.0
        assert convert_from_ms(93000) == "1:33.0"

    def test_hours_minutes_seconds(self):
        # 3750000 ms → 1:02:30.0
        assert convert_from_ms(3750000) == "1:02:30.0"

    def test_sub_minute(self):
        # 45200 ms → 0:45.2
        assert convert_from_ms(45200) == "0:45.2"

    def test_zero(self):
        assert convert_from_ms(0) == "0:00.0"

    def test_roundtrip_note(self):
        # convert_to_ms parses RuneLite's hundredths-of-a-second ticks (e.g. "1:23.40")
        # convert_from_ms formats as tenths-of-a-second (e.g. "1:23.4")
        # Both represent the same duration; verify the ms value is stable.
        ms_in = convert_to_ms("1:23.40")   # 83400 ms
        display = convert_from_ms(ms_in)     # "1:23.4"
        assert display == "1:23.4"
        # The displayed tenths value (4) × 100ms == original fractional part (400ms)
        ticks_tenths = int(display.split(".")[-1])
        assert ticks_tenths * 100 == ms_in % 1000


# ── normalize_player_display_equivalence ──────────────────────────────────────

class TestNormalizePlayerDisplayEquivalence:
    def test_already_lowercase(self):
        assert normalize_player_display_equivalence("player name") == "player name"

    def test_uppercase(self):
        assert normalize_player_display_equivalence("Player Name") == "player name"

    def test_underscores_to_spaces(self):
        assert normalize_player_display_equivalence("player_name") == "player name"

    def test_hyphens_to_spaces(self):
        assert normalize_player_display_equivalence("player-name") == "player name"

    def test_mixed_separators(self):
        assert normalize_player_display_equivalence("My_Cool-Player") == "my cool player"

    def test_extra_whitespace_collapsed(self):
        assert normalize_player_display_equivalence("player  name") == "player name"

    def test_none_returns_empty_string(self):
        assert normalize_player_display_equivalence(None) == ""

    def test_equivalence_between_underscore_and_space(self):
        # The core OSRS display rule: "zezima" == "ze_zima" (spaces ~ underscores)
        a = normalize_player_display_equivalence("ze zima")
        b = normalize_player_display_equivalence("ze_zima")
        assert a == b


# ── normalize_claim_rsn_input ─────────────────────────────────────────────────

class TestNormalizeClaimRsnInput:
    def test_strips_whitespace(self):
        assert normalize_claim_rsn_input("  Zezima  ") == "Zezima"

    def test_collapses_internal_spaces(self):
        assert normalize_claim_rsn_input("Player  Name") == "Player Name"

    def test_none_returns_empty(self):
        assert normalize_claim_rsn_input(None) == ""

    def test_empty_string(self):
        assert normalize_claim_rsn_input("") == ""

    def test_preserves_case(self):
        # normalize_claim_rsn_input does NOT lowercase (it's for lookup normalization)
        assert normalize_claim_rsn_input("Zezima") == "Zezima"


# ── get_extension_from_content_type ──────────────────────────────────────────

class TestGetExtensionFromContentType:
    def test_png(self):
        assert get_extension_from_content_type("image/png") == "png"

    def test_jpeg(self):
        assert get_extension_from_content_type("image/jpeg") == "jpg"

    def test_jpg_alias(self):
        assert get_extension_from_content_type("image/jpg") == "jpg"

    def test_gif(self):
        assert get_extension_from_content_type("image/gif") == "gif"

    def test_webp(self):
        assert get_extension_from_content_type("image/webp") == "webp"

    def test_uppercase(self):
        assert get_extension_from_content_type("image/PNG") == "png"

    def test_none_defaults_to_jpg(self):
        assert get_extension_from_content_type(None) == "jpg"

    def test_no_slash_defaults_to_jpg(self):
        assert get_extension_from_content_type("png") == "jpg"

    def test_with_charset_param(self):
        # Parameters after semicolon should be ignored
        result = get_extension_from_content_type("image/jpeg; charset=utf-8")
        assert result == "jpg"


# ── get_current_partition ─────────────────────────────────────────────────────

class TestGetCurrentPartition:
    def test_returns_int(self):
        partition = get_current_partition()
        assert isinstance(partition, int)

    def test_six_digit_yyyymm_format(self):
        partition = get_current_partition()
        # Should be between 202001 and 209912 for any foreseeable date
        assert 202001 <= partition <= 209912

    def test_month_in_valid_range(self):
        partition = get_current_partition()
        month = partition % 100
        assert 1 <= month <= 12


# ── replace_placeholders_in_text ─────────────────────────────────────────────

class TestReplacePlaceholdersInText:
    def test_single_placeholder(self):
        result = replace_placeholders_in_text(
            "Hello, {player_name}!",
            {"{player_name}": "Alice"},
        )
        assert result == "Hello, Alice!"

    def test_multiple_placeholders(self):
        result = replace_placeholders_in_text(
            "{player_name} received {item_name} worth {value}",
            {"{player_name}": "Bob", "{item_name}": "Dragon claws", "{value}": "1.50M"},
        )
        assert result == "Bob received Dragon claws worth 1.50M"

    def test_no_placeholders(self):
        text = "No placeholders here"
        result = replace_placeholders_in_text(text, {"{player_name}": "Alice"})
        assert result == "No placeholders here"

    def test_empty_value_replaces_with_empty_string(self):
        result = replace_placeholders_in_text(
            "Hello {player_name}",
            {"{player_name}": ""},
        )
        assert result == "Hello "

    def test_numeric_value_becomes_string(self):
        result = replace_placeholders_in_text(
            "Value: {value}",
            {"{value}": 2500000},
        )
        assert result == "Value: 2500000"

    def test_empty_text(self):
        result = replace_placeholders_in_text("", {"{player_name}": "Alice"})
        assert result == ""
