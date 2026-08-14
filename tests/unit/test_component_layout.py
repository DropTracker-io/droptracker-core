"""Validation and rendering of group-authored Components V2 layouts.

The layouts are user input that becomes a Discord API payload, so the cases
that matter are the ones where a bad template would either be rejected by
Discord (losing the notification entirely) or silently render nonsense.
"""
from services.component_layout import (
    IS_COMPONENTS_V2,
    MAX_BLOCKS,
    MAX_TEXT_LENGTH,
    default_layout,
    parse_accent,
    render_layout,
    validate_layout,
)

VALUES = {
    "{player_name}": "Ra ine",
    "{npc_name}": "Vorkath",
    "{personal_best}": "1:52.20",
    "{team_size}": "Solo",
    "{global_rank}": "#214",
    "{gear_image_url}": "https://example/character.png",
    "{image_url}": "https://example/kill.png",
}


def blocks_of(payload):
    return payload["components"][0]["components"]


def types_of(payload):
    return [c["type"] for c in blocks_of(payload)]


class TestValidation:
    def test_accepts_the_shipped_default(self):
        ok, errors = validate_layout(default_layout("pb"))
        assert ok, errors

    def test_requires_at_least_one_block(self):
        ok, errors = validate_layout({"blocks": []})
        assert not ok and errors

    def test_rejects_unknown_block_types(self):
        ok, errors = validate_layout({"blocks": [{"type": "iframe", "content": "hi"}]})
        assert not ok
        assert any("unknown type" in e for e in errors)

    def test_rejects_text_over_the_discord_limit(self):
        ok, errors = validate_layout({"blocks": [{"type": "text", "content": "x" * (MAX_TEXT_LENGTH + 1)}]})
        assert not ok
        assert any("limit" in e for e in errors)

    def test_rejects_too_many_blocks(self):
        layout = {"blocks": [{"type": "text", "content": "hi"}] * (MAX_BLOCKS + 5)}
        ok, errors = validate_layout(layout)
        assert not ok
        assert any("Too many blocks" in e for e in errors)

    def test_rejects_a_button_without_a_link(self):
        layout = {"blocks": [{"type": "buttons", "buttons": [{"label": "Go"}]}]}
        ok, errors = validate_layout(layout)
        assert not ok
        assert any("link" in e for e in errors)

    def test_rejects_a_bad_accent_colour(self):
        ok, errors = validate_layout(
            {"accent_color": "burgundy", "blocks": [{"type": "text", "content": "hi"}]}
        )
        assert not ok
        assert any("hex" in e for e in errors)

    def test_errors_name_the_block_so_the_editor_can_point_at_it(self):
        layout = {"blocks": [{"type": "text", "content": "fine"}, {"type": "text", "content": ""}]}
        ok, errors = validate_layout(layout)
        assert not ok
        assert any("Block 2" in e for e in errors)


class TestRendering:
    def test_renders_the_default_into_a_v2_payload(self):
        payload = render_layout(default_layout("pb"), VALUES)
        assert payload["flags"] == IS_COMPONENTS_V2
        assert "embeds" not in payload and "content" not in payload
        assert payload["components"][0]["type"] == 17

    def test_substitutes_placeholders(self):
        payload = render_layout(default_layout("pb"), VALUES)
        text = str(payload)
        assert "Ra ine" in text and "Vorkath" in text
        assert "{player_name}" not in text

    def test_drops_a_thumbnail_whose_placeholder_did_not_resolve(self):
        """Most players have no character model. The section must still send —
        an unresolved URL would make Discord reject the whole message."""
        values = dict(VALUES, **{"{gear_image_url}": ""})
        payload = render_layout(default_layout("pb"), values)
        section = next(c for c in blocks_of(payload) if c["type"] == 9)
        assert "accessory" not in section

    def test_drops_only_the_line_holding_an_unresolved_token(self):
        """Matches the embed behaviour: a template line about a value this
        notification lacks disappears, rather than taking the whole block with
        it or rendering a literal {token} to the channel."""
        layout = {"blocks": [{
            "type": "text",
            "content": "**Time** {personal_best}\nPrevious best: {previous_best}",
        }]}
        payload = render_layout(layout, {"{personal_best}": "1:52.20"})
        text = blocks_of(payload)[0]["content"]
        assert "1:52.20" in text
        assert "Previous best" not in text
        assert "{" not in text

    def test_drops_a_media_block_with_no_usable_images(self):
        values = dict(VALUES, **{"{image_url}": ""})
        payload = render_layout(default_layout("pb"), values)
        assert 12 not in types_of(payload)

    def test_unresolved_placeholder_is_not_sent_as_a_url(self):
        """An unsubstituted {token} left in a URL is the subtle failure — it is
        a non-empty string, so a naive check would pass it to Discord."""
        layout = {"blocks": [{"type": "media", "urls": ["{missing_url}"]}]}
        payload = render_layout(layout, {})
        assert payload is None

    def test_returns_none_when_nothing_renderable_survives(self):
        layout = {"blocks": [{"type": "media", "urls": ["{image_url}"]}]}
        assert render_layout(layout, {"{image_url}": ""}) is None

    def test_strips_leading_and_trailing_separators(self):
        layout = {
            "blocks": [
                {"type": "separator"},
                {"type": "text", "content": "body"},
                {"type": "separator"},
            ]
        }
        payload = render_layout(layout, {})
        assert types_of(payload) == [10]

    def test_accent_colour_is_converted_for_discord(self):
        payload = render_layout(
            {"accent_color": "#c8aa6e", "blocks": [{"type": "text", "content": "hi"}]}, {}
        )
        assert payload["components"][0]["accent_color"] == 0xC8AA6E

    def test_buttons_become_an_action_row(self):
        layout = {
            "blocks": [
                {"type": "text", "content": "hi"},
                {"type": "buttons", "buttons": [{"label": "Profile", "url": "https://example/p"}]},
            ]
        }
        payload = render_layout(layout, {})
        row = next(c for c in blocks_of(payload) if c["type"] == 1)
        assert row["components"][0]["style"] == 5  # link


def test_parse_accent_accepts_hex_with_or_without_hash():
    assert parse_accent("#ffffff") == 0xFFFFFF
    assert parse_accent("000000") == 0
    assert parse_accent("nope") is None


class TestPilotGating:
    def test_only_the_global_group_may_use_components(self):
        """This changes what every member of a group receives, so it stays an
        explicit allowlist until it has been proven in the global group."""
        from services.component_layout import components_enabled_for_group

        assert components_enabled_for_group(2) is True
        assert components_enabled_for_group(1) is False
        assert components_enabled_for_group(9999) is False

    def test_non_numeric_group_is_not_enabled(self):
        from services.component_layout import components_enabled_for_group

        assert components_enabled_for_group(None) is False
        assert components_enabled_for_group("two") is False
