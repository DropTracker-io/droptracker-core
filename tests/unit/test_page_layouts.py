"""web137a page layouts: the block vocabulary and the validator."""
import pytest

from web_api.page_layouts_shared import (
    BLOCK_CATALOG,
    MAX_BLOCKS,
    LayoutError,
    block_types_for,
    validate_layout,
)


def _layout(*blocks, theme=None):
    return {"version": 1, "theme": theme or {}, "blocks": list(blocks)}


def test_minimal_layout_normalizes_defaults():
    out = validate_layout(_layout({"id": "a", "type": "stats_row"}), "group")
    block = out["blocks"][0]
    assert block["width"] == "full"
    assert block["surface"] == "plain"
    assert block["hide_on"] == "none"
    assert block["title"] is None
    assert block["stats"] == ["members", "rank", "monthly_loot", "top_player"]
    assert block["columns"] == "auto"
    assert out["theme"]["preset"] == "default"
    assert out["theme"]["palette"] == {}


def test_unknown_keys_dropped_and_enums_fall_back():
    out = validate_layout(
        _layout({"id": "a", "type": "records", "width": "huge", "surface": "neon",
                 "limit": 9999, "onclick": "alert(1)"}),
        "group",
    )
    block = out["blocks"][0]
    assert "onclick" not in block
    assert block["width"] == "full"
    assert block["surface"] == "plain"
    assert block["limit"] == 24  # clamped


def test_scopes_keep_block_sets_apart():
    group = set(block_types_for("group"))
    player = set(block_types_for("player"))
    assert "support_card" in group and "support_card" not in player
    assert "player_showcase" in player and "player_showcase" not in group
    # member defaults are player profiles
    assert set(block_types_for("member_default")) == player
    # shared basics everywhere
    for t in ("markdown", "image", "buttons", "divider"):
        assert t in group and t in player
    with pytest.raises(LayoutError):
        validate_layout(_layout({"id": "a", "type": "player_showcase"}), "group")
    with pytest.raises(LayoutError):
        validate_layout(_layout({"id": "a", "type": "support_card"}), "player")


def test_every_catalog_type_validates_with_no_props():
    """An unfinished block (fresh from the library) must always save, or the
    builder's autosave breaks the moment someone adds it."""
    for btype in BLOCK_CATALOG:
        scope = "group" if btype in block_types_for("group") else "player"
        validate_layout(_layout({"id": "x", "type": btype}), scope)


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "data:text/html,hi", "//evil.example", "/\\evil.example",
    "http://plain.example", "ftp://x", "",
])
def test_button_links_must_be_https_or_relative(href):
    with pytest.raises(LayoutError):
        validate_layout(
            _layout({"id": "b", "type": "buttons", "items": [{"label": "Go", "href": href}]}),
            "group",
        )


def test_button_links_ok():
    out = validate_layout(
        _layout({"id": "b", "type": "buttons", "items": [
            {"label": "Discord", "href": "https://discord.gg/abc"},
            {"label": "Events", "href": "/events"},
        ]}),
        "group",
    )
    assert [i["href"] for i in out["blocks"][0]["items"]] == ["https://discord.gg/abc", "/events"]


@pytest.mark.parametrize("url", ["javascript:x", "http://x.example/a.png", "/etc/passwd", "data:image/png;base64,AA"])
def test_image_urls_rejected(url):
    with pytest.raises(LayoutError):
        validate_layout(_layout({"id": "i", "type": "image", "url": url}), "group")


def test_image_urls_ok():
    for url in ("https://i.imgur.com/x.png", "/img/npcdb/1.png"):
        validate_layout(_layout({"id": "i", "type": "image", "url": url}), "group")


def test_banner_must_be_an_image_url():
    with pytest.raises(LayoutError):
        validate_layout(_layout(theme={"banner_url": "javascript:alert(1)"}), "group")
    out = validate_layout(_layout(theme={"banner_url": "https://cdn.example/b.png"}), "group")
    assert out["theme"]["banner_url"] == "https://cdn.example/b.png"


def test_palette_filters_keys_and_rejects_css_escape():
    out = validate_layout(
        _layout(theme={"palette": {"--dt-gold": "#ff0000", "--not-a-var": "#fff"}}), "group"
    )
    assert out["theme"]["palette"] == {"--dt-gold": "#ff0000"}
    with pytest.raises(LayoutError):
        validate_layout(_layout(theme={"palette": {"--dt-gold": "red; } body {"}}), "group")


def test_text_too_long_is_an_error_not_a_truncation():
    with pytest.raises(LayoutError, match="8000"):
        validate_layout(_layout({"id": "t", "type": "markdown", "body": "x" * 8001}), "group")


def test_limits():
    with pytest.raises(LayoutError):
        validate_layout(_layout(*[{"id": f"b{i}", "type": "divider"} for i in range(MAX_BLOCKS + 1)]), "group")
    with pytest.raises(LayoutError, match="duplicate"):
        validate_layout(_layout({"id": "a", "type": "divider"}, {"id": "a", "type": "divider"}), "group")
    with pytest.raises(LayoutError):
        validate_layout(_layout({"id": "has space", "type": "divider"}), "group")


def test_group_links_keep_order_and_drop_unknown():
    out = validate_layout(
        _layout({"id": "l", "type": "group_links", "items": ["discord", "lootboard", "bogus", "discord"]}),
        "group",
    )
    assert out["blocks"][0]["items"] == ["discord", "lootboard"]


def test_malformed_input():
    for raw in (None, [], "x", {"blocks": "no"}):
        with pytest.raises(LayoutError):
            validate_layout(raw, "group")


def test_stats_choices_are_the_owners_and_capped():
    out = validate_layout(_layout({"id": "s", "type": "stats_row", "stats": [
        "clan_log", "bank_balance", "pvp_kills", "nonsense", "members", "rank", "top_boss",
        "records_held", "events_running", "points_leader"]}), "group")
    stats = out["blocks"][0]["stats"]
    assert stats[:3] == ["clan_log", "bank_balance", "pvp_kills"]
    assert "nonsense" not in stats and len(stats) == 8
    p = validate_layout(_layout({"id": "s", "type": "player_stats", "stats": ["clog_slots", "ca_tasks"],
                                 "columns": "3"}), "player")["blocks"][0]
    assert p["stats"] == ["clog_slots", "ca_tasks"] and p["columns"] == "3"


def test_highlight_types_filter():
    b = validate_layout(_layout({"id": "h", "type": "highlights", "types": ["pet", "evil", "pb"]}), "group")
    assert b["blocks"][0]["types"] == ["pet", "pb"]
    fresh = validate_layout(_layout({"id": "h", "type": "player_highlights"}), "player")
    assert fresh["blocks"][0]["types"] == ["pet", "clog", "pb"]


def test_window_surface_allowed():
    b = validate_layout(_layout({"id": "w", "type": "records", "surface": "window"}), "group")
    assert b["blocks"][0]["surface"] == "window"


def test_highlight_types_are_what_the_feed_serves():
    from web_api.page_layouts_shared import HIGHLIGHT_TYPES as SHARED
    from web_api.routes.page_layouts import HIGHLIGHT_TYPES as SERVED

    assert set(SHARED) == set(SERVED)
