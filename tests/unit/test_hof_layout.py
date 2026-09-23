"""Hall of Fame layouts: validation and rendering (services/hof_layout.py).

A Hall of Fame layout is user input that becomes the payload of a message the
bot keeps editing, so the cases that matter are the ones that would make
Discord reject the edit (the board silently freezes) or render nonsense:
lines about data a boss does not have, raid modes interleaved out of order,
leaderboards that grow past Discord's limits.
"""
from services.hof_layout import (
    DEFAULT_LAYOUT,
    MAX_RENDERED_TEXT,
    MAX_ROWS,
    HofEntry,
    Row,
    Scope,
    count_components,
    default_layout,
    emoji_refs,
    needed_boards,
    render_entry,
    render_layout,
    total_text,
    validate_layout,
    within_limits,
)

COMMON = {
    "{site_url}": "https://www.droptracker.io",
    "{pbs_url}": "https://www.droptracker.io/personal-bests",
    "{directory_url}": "",
    "{coins_emoji}": "<:item_coins:1>",
    "{month_name}": "September",
}


def rows(n, value="1"):
    return [Row(player=f"[P{i}](u{i})", player_plain=f"P{i}", value=value) for i in range(1, n + 1)]


def scope(name="Zulrah", mode="", pbs=True, kc=True, loot=True):
    tokens = {
        "{boss_name}": name,
        "{boss_link}": f"[{name}](https://x/{name})",
        "{boss_url}": f"https://x/{name}",
        "{boss_image_url}": "https://x/img.png",
        "{boss_emoji}": "",
        "{mode_name}": mode,
        "{total_pbs}": "12" if pbs else "",
        "{fastest_time}": "0:36.0" if pbs else "",
        "{fastest_team_size}": "Solo" if pbs else "",
        "{fastest_player}": "[P1](u1)" if pbs else "",
        "{top_kc_player}": "[P1](u1)" if kc else "",
        "{top_kc}": "6,366" if kc else "",
        "{top_looter_month}": "[P2](u2)" if loot else "",
        "{top_loot_month}": "9.66M" if loot else "",
        "{top_looter_all}": "",
        "{top_loot_all}": "",
        "{total_loot}": "747.88M" if loot else "",
    }
    return Scope(
        tokens=tokens,
        boards={"kc": rows(8) if kc else [], "loot_month": rows(3) if loot else []},
        pb_brackets=[("Solo", rows(8, "0:40.0")), ("Duo", rows(2, "0:50.0"))] if pbs else [],
    )


def single(**kw):
    s = scope(**kw)
    return HofEntry(scope=s, modes=[s])


def texts(payload):
    out = []
    for c in payload["components"][0]["components"]:
        if c["type"] == 10:
            out.append(c["content"])
        elif c["type"] == 9:
            out.append(c["components"][0]["content"])
    return "\n".join(out)


class TestValidation:
    def test_default_is_valid(self):
        ok, errors = validate_layout(default_layout())
        assert ok, errors

    def test_leaderboard_needs_a_board(self):
        ok, errors = validate_layout({"blocks": [{"type": "leaderboard", "board": "nope"}]})
        assert not ok and "ranks" in errors[0]

    def test_count_bounds(self):
        for bad in (0, MAX_ROWS + 1, "3", True):
            ok, _ = validate_layout({"blocks": [{"type": "leaderboard", "board": "kc", "count": bad}]})
            assert not ok, bad
        ok, _ = validate_layout({"blocks": [{"type": "leaderboard", "board": "kc", "count": None}]})
        assert ok

    def test_a_line_without_player_or_value_is_rejected(self):
        ok, errors = validate_layout(
            {"blocks": [{"type": "leaderboard", "board": "kc", "line": "-# {medal}"}]}
        )
        assert not ok and "{player}" in errors[0]

    def test_errors_point_at_the_right_block(self):
        ok, errors = validate_layout({"blocks": [
            {"type": "text", "content": "fine"},
            {"type": "text", "content": ""},
        ]})
        assert not ok and errors[0].startswith("Block 2")

    def test_dividers_alone_are_not_a_layout(self):
        ok, _ = validate_layout({"blocks": [{"type": "separator"}]})
        assert not ok


class TestRendering:
    def test_default_leads_with_top_kc_and_looter_not_lists(self):
        payload, used_default = render_entry(None, single(), COMMON, 5)
        body = texts(payload)
        assert used_default
        assert "Highest KC: `6,366` kc by [P1](u1)" in body
        assert "Most loot this month: <:item_coins:1> `9.66M` gp by [P2](u2)" in body
        # One leader each, not a ranked list of looters or killers.
        assert "Loot Leaderboard" not in body
        assert body.count(" kc") == 1
        # Personal bests still ranked, up to the configured count per bracket.
        assert "🥇 `0:40.0` - [P1](u1)" in body and "5. `0:40.0` - [P5](u5)" in body
        assert "6. `0:40.0`" not in body

    def test_a_missing_value_drops_its_line_even_beside_an_icon(self):
        payload, _ = render_entry(None, single(kc=False, loot=False), COMMON, 5)
        body = texts(payload)
        assert "Highest KC" not in body
        assert "Most loot" not in body
        assert "``" not in body

    def test_directory_line_drops_without_a_directory(self):
        payload, _ = render_entry(None, single(), COMMON, 5)
        assert "Back to Directory" not in texts(payload)
        payload, _ = render_entry(None, single(), {**COMMON, "{directory_url}": "https://d"}, 5)
        assert "[Back to Directory](https://d)" in texts(payload)

    def test_mode_heading_drops_for_an_ordinary_boss(self):
        payload, _ = render_entry(None, single(), COMMON, 5)
        assert "###" not in texts(payload)

    def test_each_mode_runs_repeat_together_in_mode_order(self):
        normal, cm = scope("CoX", "Normal"), scope("CM CoX", "Challenge Mode")
        entry = HofEntry(scope=scope("Chambers of Xeric"), modes=[normal, cm])
        payload, _ = render_entry(None, entry, COMMON, 2)
        body = texts(payload)
        assert body.index("### Normal") < body.index("-# **Solo**") < body.index("### Challenge Mode")
        assert body.count("Personal Best Leaderboards") == 1

    def test_leaderboard_count_and_line(self):
        layout = {"blocks": [{
            "type": "leaderboard", "board": "kc", "count": 3,
            "title": "**Most kills**", "line": "{rank}) {player_plain} {value}",
        }]}
        payload = render_layout(layout, single(), COMMON, 5)
        assert texts(payload) == "**Most kills**\n1) P1 1\n2) P2 1\n3) P3 1"

    def test_empty_board_uses_empty_text_or_disappears(self):
        board = {"type": "leaderboard", "board": "kc", "title": "**KC**"}
        layout = {"blocks": [{"type": "text", "content": "hi"}, board]}
        assert texts(render_layout(layout, single(kc=False), COMMON, 5)) == "hi"
        board["empty"] = "-# nobody yet"
        assert texts(render_layout(layout, single(kc=False), COMMON, 5)).endswith("**KC**\n-# nobody yet")

    def test_emoji_refs_resolve_or_vanish(self):
        layout = {"blocks": [{"type": "text", "content": "{emoji:npc_zulrah} **{boss_name}** {emoji:item_nope}"}]}
        assert emoji_refs(layout) == ["npc_zulrah", "item_nope"]
        payload = render_layout(layout, single(), COMMON, 5, emojis={"npc_zulrah": "<:npc_zulrah:9>"})
        assert texts(payload) == "<:npc_zulrah:9> **Zulrah**"

    def test_oversized_layout_shrinks_then_falls_back(self):
        big = rows(10, "x" * 60)
        s = scope()
        s.boards["kc"] = big
        entry = HofEntry(scope=s, modes=[s])
        layout = {"blocks": [
            {"type": "leaderboard", "board": "kc", "count": 10, "line": "{player} " + "y" * 300 + " {value}"}
        ] * 3}
        payload, used_default = render_entry(layout, entry, COMMON, 5)
        assert payload is not None and within_limits(payload)
        assert total_text(payload["components"]) <= MAX_RENDERED_TEXT
        # Shrunk rather than replaced while it can be made to fit.
        assert not used_default

        huge = {"blocks": [{"type": "text", "content": "z" * 3400}] * 2}
        payload, used_default = render_entry(huge, entry, COMMON, 5)
        assert used_default and "Highest KC" in texts(payload)

    def test_needed_boards_reads_only_what_is_shown(self):
        needs = needed_boards({"blocks": [{"type": "text", "content": "{top_loot_all}"}]}, 5)
        assert needs == {"pb": 5, "loot_all": 1}
        needs = needed_boards(DEFAULT_LAYOUT, 4)
        assert needs["kc"] == 1 and needs["loot_month"] == 1 and needs["pb"] == 4

    def test_accent_colour(self):
        layout = {**default_layout(), "accent_color": "#c8aa6e"}
        payload = render_layout(layout, single(), COMMON, 5)
        assert payload["components"][0]["accent_color"] == 0xC8AA6E
        assert count_components(payload["components"]) < 38
