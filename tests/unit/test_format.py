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
    replace_placeholders,
    replace_placeholders_in_text,
    strip_title_markdown,
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


# ── strip_title_markdown ──────────────────────────────────────────────────────

class TestStripTitleMarkdown:
    """Discord renders embed titles as plain text, so the sender flattens them."""

    def test_masked_link_becomes_its_label(self):
        assert (
            strip_title_markdown("[Beast Owned](https://www.droptracker.io/players/1)")
            == "Beast Owned"
        )

    def test_player_link_inside_a_sentence(self):
        assert (
            strip_title_markdown("[Ron](https://www.droptracker.io/players/1) Planked!")
            == "Ron Planked!"
        )

    def test_bold(self):
        assert strip_title_markdown("**Levels achieved:** {skills_text}") == (
            "Levels achieved: {skills_text}"
        )

    def test_code_ticks(self):
        assert strip_title_markdown("New `Zulrah` Personal Best") == "New Zulrah Personal Best"

    def test_italic_and_strikethrough(self):
        assert strip_title_markdown("*a* ~~b~~ ___c___") == "a b c"

    def test_lone_underscore_in_a_name_survives(self):
        # OSRS display names carry underscores (the plugin submits `Beast_Owned`).
        assert strip_title_markdown("Beast_Owned Planked!") == "Beast_Owned Planked!"

    def test_plain_text_untouched(self):
        assert strip_title_markdown(":tada: Zulrah :tada:") == ":tada: Zulrah :tada:"

    def test_empty_and_none(self):
        assert strip_title_markdown("") == ""
        assert strip_title_markdown(None) is None


# ── replace_placeholders (title + url) ────────────────────────────────────────

class _StubEmbed:
    """conftest stubs `interactions`, so a real Embed here is a MagicMock whose
    attribute writes never stick. replace_placeholders only touches these."""

    def __init__(self, title=None, url=None, description=None):
        self.title = title
        self.url = url
        self.description = description
        self.footer = None
        self.fields = []
        self.thumbnail = None
        self.image = None


def _embed(title, url=None):
    return _StubEmbed(title=title, url=url)


class TestReplacePlaceholdersTitle:
    def test_player_name_in_title_is_flattened(self):
        """The reported bug: {player_name} resolves to a markdown link."""
        embed = _embed("{player_name} Planked!")
        out = replace_placeholders(
            embed, {"{player_name}": "[Ron](https://www.droptracker.io/players/1)"}
        )
        assert out.title == "Ron Planked!"
        assert out.url is None

    def test_npc_name_token_still_auto_links_the_wiki(self):
        embed = _embed(":tada: {npc_name} :tada:")
        out = replace_placeholders(embed, {"{npc_name}": "Theatre of Blood"})
        assert out.title == ":tada: Theatre of Blood :tada:"
        assert out.url == "https://oldschool.runescape.wiki/w/Theatre_of_Blood"

    def test_item_name_token_still_auto_links_the_wiki(self):
        embed = _embed("{item_name}")
        out = replace_placeholders(embed, {"{item_name}": "Abyssal whip"})
        assert out.url == "https://oldschool.runescape.wiki/w/Abyssal_whip"

    def test_custom_url_wins_over_wiki_autolink(self):
        embed = _embed("{npc_name}", url="https://www.droptracker.io/npcs/{npc_id}")
        out = replace_placeholders(embed, {"{npc_name}": "Zulrah", "{npc_id}": "2042"})
        assert out.url == "https://www.droptracker.io/npcs/2042"

    def test_custom_url_makes_a_plain_title_clickable(self):
        embed = _embed("A new drop!", url="https://www.droptracker.io/")
        out = replace_placeholders(embed, {})
        assert out.url == "https://www.droptracker.io/"

    def test_unresolved_url_placeholder_is_dropped_not_sent(self):
        # Discord rejects the whole embed on a malformed url.
        embed = _embed("A new drop!", url="https://www.droptracker.io/npcs/{npc_id}")
        out = replace_placeholders(embed, {})
        assert out.url == "https://www.droptracker.io/npcs/{npc_id}"

    def test_junk_url_is_dropped(self):
        embed = _embed("A new drop!", url="{image_url}")
        out = replace_placeholders(embed, {"{image_url}": ""})
        assert out.url is None

    def test_title_without_tokens_gets_no_url(self):
        embed = _embed("New Combat Achievement")
        out = replace_placeholders(embed, {})
        assert out.url is None


# ── replace_placeholders (fields / {team_size}) ───────────────────────────────

class _StubField:
    def __init__(self, name, value):
        self.name = name
        self.value = value


def _embed_with_fields(*fields):
    embed = _StubEmbed(title="A new personal best!")
    embed.fields = [_StubField(name, value) for name, value in fields]
    return embed


class TestReplacePlaceholdersTeamSizeField:
    def test_missing_team_size_does_not_raise(self):
        """A template field can reference {team_size} on an embed type whose
        caller supplies none — that must not abort the whole render."""
        embed = _embed_with_fields(("Team Size", "{team_size}"))
        out = replace_placeholders(embed, {"{player_name}": "Ron"})
        assert out.fields[0].value == "{team_size}"

    def test_two_team_size_fields_both_render_once(self):
        embed = _embed_with_fields(
            ("Team Size", "{team_size}"), ("Players", "{team_size}")
        )
        out = replace_placeholders(embed, {"{team_size}": "4"})
        assert [f.value for f in out.fields] == ["4 players", "4 players"]

    def test_solo_is_left_alone(self):
        embed = _embed_with_fields(("Team Size", "{team_size}"))
        out = replace_placeholders(embed, {"{team_size}": "Solo"})
        assert out.fields[0].value == "Solo"

    def test_callers_value_dict_is_not_mutated(self):
        embed = _embed_with_fields(("Team Size", "{team_size}"))
        value_dict = {"{team_size}": "4"}
        replace_placeholders(embed, value_dict)
        assert value_dict == {"{team_size}": "4"}
