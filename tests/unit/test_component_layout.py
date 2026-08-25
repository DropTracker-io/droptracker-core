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

    def test_every_shipped_default_is_saveable(self):
        """The defaults seed the editor, so one that failed its own validator
        would greet the author with errors on a layout they never touched."""
        from services.component_layout import NOTIFICATION_TYPES

        for notification_type in NOTIFICATION_TYPES:
            layout = default_layout(notification_type)
            assert layout, f"{notification_type} has no default layout"
            ok, errors = validate_layout(layout)
            assert ok, f"{notification_type}: {errors}"

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


class TestEntitlementGating:
    """Components are a paid customisation, gated by the same entitlement as
    the embed builder. The lookup is patched here because conftest stubs
    ``db.models``, which would make the real entitlement query answer True for
    every group."""

    def test_follows_the_group_entitlement(self, monkeypatch):
        import importlib

        # Dotted import, not ``import services.component_layout as cl``:
        # conftest stubs the ``services`` package, so the attribute form
        # yields a MagicMock.
        cl = importlib.import_module("services.component_layout")

        entitled = {2, 55}
        monkeypatch.setattr(
            cl, "_group_has_components_entitlement", lambda gid: gid in entitled)

        assert cl.components_enabled_for_group(2) is True
        assert cl.components_enabled_for_group(55) is True
        assert cl.components_enabled_for_group(1) is False
        assert cl.components_enabled_for_group(9999) is False

    def test_a_failing_entitlement_lookup_keeps_the_embed(self, monkeypatch):
        """The send path calls this per notification; an entitlement error must
        cost the customisation, never the message."""
        import importlib

        # Dotted import, not ``import services.component_layout as cl``:
        # conftest stubs the ``services`` package, so the attribute form
        # yields a MagicMock.
        cl = importlib.import_module("services.component_layout")

        def _boom(_gid):
            raise RuntimeError("entitlement backend down")

        monkeypatch.setattr(cl, "_group_has_components_entitlement", _boom)
        assert cl.components_enabled_for_group(2) is False

    def test_non_numeric_group_is_not_enabled(self):
        from services.component_layout import components_enabled_for_group

        assert components_enabled_for_group(None) is False
        assert components_enabled_for_group("two") is False


class TestDefaultsDegradeGracefully:
    """The defaults are what a group sees first, and most notifications are
    sparse: no screenshot, no character model, no points awarded. A default
    that rendered nothing in that case would silently fall back to the embed
    and look like the feature was never switched on."""

    def _bare_values(self, notification_type):
        """Only the tokens a notification of this type always has, with every
        optional one blank."""
        from services.component_layout import tokens_for

        values = {}
        for doc in tokens_for(notification_type):
            values[f"{{{doc['token']}}}"] = "" if doc["optional"] else f"<{doc['token']}>"
        return values

    def test_every_default_still_says_something_for_a_bare_player(self):
        from services.component_layout import NOTIFICATION_TYPES

        for notification_type in NOTIFICATION_TYPES:
            payload = render_layout(
                default_layout(notification_type), self._bare_values(notification_type))
            assert payload is not None, f"{notification_type} rendered nothing"
            assert any(c["type"] != 14 for c in blocks_of(payload)), notification_type

    def test_optional_tokens_take_their_line_with_them(self):
        """The drop default puts the points line on its own line precisely so
        that a group with points disabled loses the line, not the message."""
        values = self._bare_values("drop")
        payload = render_layout(default_layout("drop"), values)
        text = "\n".join(
            c.get("content", "") for c in blocks_of(payload) if c["type"] == 10)
        assert "group_points_awarded" not in text
        assert "<item_name>" in "\n".join(
            str(c) for c in blocks_of(payload))


class TestALineWithNoValueDisappears:
    """A label belongs to the value beside it. Rendering "**Location**" over
    nothing is what an embed avoids by dropping a field whose value resolves
    empty, and a layout has to do the same or switching a type over looks
    broken for every player missing an optional value."""

    def test_a_blank_value_takes_its_label(self):
        payload = render_layout(
            {"blocks": [{"type": "text", "content": "**Killed By** {source}\n**Location** {location}"}]},
            {"{source}": "Abyssal demon", "{location}": ""},
        )
        text = blocks_of(payload)[0]["content"]
        assert text == "**Killed By** Abyssal demon"

    def test_a_line_survives_while_any_value_remains(self):
        payload = render_layout(
            {"blocks": [{"type": "text", "content": "**Rank** {group_rank}/{total_ranked_group}"}]},
            {"{group_rank}": "1", "{total_ranked_group}": ""},
        )
        assert blocks_of(payload)[0]["content"] == "**Rank** 1/"

    def test_a_line_with_no_tokens_is_never_dropped(self):
        payload = render_layout(
            {"blocks": [{"type": "text", "content": "Nice one!\n{missing_thing}"}]},
            {"{source}": "x"},
        )
        assert blocks_of(payload)[0]["content"] == "Nice one!"

    def test_the_death_default_drops_an_unknown_location(self):
        payload = render_layout(
            default_layout("death"),
            {"{player_name}": "Ron", "{source}": "Vet'ion", "{location}": "", "{image_url}": ""},
        )
        text = "\n".join(c.get("content", "") for c in blocks_of(payload) if c["type"] == 10)
        assert "**Killed By** Vet'ion" in text
        assert "Location" not in text


class TestDefaultsMirrorTheEmbeds:
    """A group switching a type over should recognise the message. The
    defaults reproduce the shipped embed templates, so the headline wording,
    the figures and their order all carry across."""

    def test_headlines_match_the_embed_content_lines(self):
        # The embed path sends these as the message content above the embed;
        # a components message has no content line, so the default has to
        # carry the same wording itself.
        expected = {
            "drop": "received a drop:",
            "pb": "has achieved a new personal best:",
            "clog": "has added an item to their collection log!",
            "ca": "has completed a combat achievement!",
            "pet": "has acquired a new pet!",
            "level_up": "levelled-up:",
            "quest": "completed a quest!",
            "death": "has died!",
            "diary": "completed an achievement diary!",
        }
        for notification_type, headline in expected.items():
            first = default_layout(notification_type)["blocks"][0]
            assert first["type"] == "text"
            assert headline in first["content"], notification_type
            assert "{player_name}" in first["content"], notification_type

    def test_personal_best_shows_the_character_render(self):
        """The one thing components can do that the personal-best embed
        cannot, and the reason a group would switch this type over."""
        section = [
            b for b in default_layout("pb")["blocks"] if b["type"] == "section"
        ]
        assert len(section) == 1
        assert section[0]["thumbnail"] == "{gear_image_url}"
        assert "{personal_best}" in section[0]["content"]

    def test_accents_follow_the_embed_colours(self):
        assert default_layout("level_up")["accent_color"] == "#2ECC71"
        assert default_layout("death")["accent_color"] == "#B23B3B"
        assert default_layout("quest")["accent_color"] == "#5A8DEE"
        assert default_layout("diary")["accent_color"] == "#5A8DEE"

    def test_points_lines_only_where_the_sender_supplies_them(self):
        """level_up/quest/death/diary build no points map, so a points line
        there would be dead weight that always drops."""
        from services.component_layout import NOTIFICATION_TYPES, TYPE_META

        for notification_type in NOTIFICATION_TYPES:
            blob = str(default_layout(notification_type))
            documented = "group_points_awarded" in TYPE_META[notification_type]["tokens"]
            assert ("group_points_awarded" in blob) is documented, notification_type
