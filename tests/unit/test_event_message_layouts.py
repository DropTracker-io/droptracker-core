"""Unit tests for the event messaging overhaul's pure layers:

- message_config merge / verbosity gating / milestone detection
  (services/event_notifications.py additions), and
- the component-layout DSL resolution (services/event_message_layouts.py:
  token substitution with line/block dropping, standings blocks, buttons,
  default layouts, notification_context flattening).

Loaded directly from file paths (test_event_notifications.py pattern) so the
conftest sys.modules stubs for db/services never interfere. The layouts
module lazily imports ``services.event_notifications`` inside two functions,
so the loaded event_notifications module is registered under that name.
"""

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, filename):
    path = os.path.join(_ROOT, "services", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


en = _load("services.event_notifications", "event_notifications.py")
# render_message_spec lazily imports services.activity_launch_core for
# launch_link buttons — register the real module over the conftest stub.
alc = _load("services.activity_launch_core", "activity_launch_core.py")
ml = _load("_event_message_layouts_under_test", "event_message_layouts.py")


# ── message config merge ─────────────────────────────────────────────────────

class TestEffectiveMessageConfig:
    def test_none_gives_defaults(self):
        config = en.effective_message_config(None)
        assert config == en.DEFAULT_MESSAGE_CONFIG
        assert config is not en.DEFAULT_MESSAGE_CONFIG  # fresh copy, no aliasing
        assert config["toggles"] is not en.DEFAULT_MESSAGE_CONFIG["toggles"]

    def test_corrupt_json_gives_defaults(self):
        assert en.effective_message_config("{not json") == en.DEFAULT_MESSAGE_CONFIG
        assert en.effective_message_config("[1,2]") == en.DEFAULT_MESSAGE_CONFIG

    def test_partial_overlay(self):
        config = en.effective_message_config(
            '{"toggles": {"event_completion": false}, "task_progress": "milestones"}'
        )
        assert config["toggles"]["event_completion"] is False
        assert config["toggles"]["event_started"] is True  # untouched default
        assert config["task_progress"] == "milestones"
        assert config["leaderboard"] == {"live": True, "top_n": 10, "show_tasks": True}

    def test_unknown_keys_ignored(self):
        config = en.effective_message_config(
            '{"toggles": {"bogus": false}, "task_progress": "sideways", "extra": 1}'
        )
        assert "bogus" not in config["toggles"]
        assert config["task_progress"] == "off"

    def test_top_n_clamped(self):
        assert en.effective_message_config('{"leaderboard": {"top_n": 999}}')["leaderboard"]["top_n"] == 25
        assert en.effective_message_config('{"leaderboard": {"top_n": 1}}')["leaderboard"]["top_n"] == 3
        assert en.effective_message_config('{"leaderboard": {"top_n": "x"}}')["leaderboard"]["top_n"] == 10

    def test_accepts_dict_input(self):
        config = en.effective_message_config({"leaderboard": {"live": False}})
        assert config["leaderboard"]["live"] is False

    def test_item_details_defaults_on_and_overrides(self):
        assert en.effective_message_config(None)["item_details"] is True
        assert en.effective_message_config({"item_details": False})["item_details"] is False
        # truthy/falsy values are coerced to bool
        assert en.effective_message_config({"item_details": 0})["item_details"] is False
        assert en.effective_message_config('{"item_details": true}')["item_details"] is True


class TestShouldSend:
    def test_defaults_send_everything_but_progress(self):
        config = en.effective_message_config(None)
        for t in ("event_started", "event_ended", "event_completion",
                  "event_lead_change", "event_pending", "event_activation_failed"):
            assert en.should_send_event_message(config, t), t
        # task_progress default mode is off
        assert not en.should_send_event_message(config, "event_task_progress")

    def test_muted_type(self):
        config = en.effective_message_config('{"toggles": {"event_completion": false}}')
        assert not en.should_send_event_message(config, "event_completion")
        assert en.should_send_event_message(config, "event_line")

    def test_progress_requires_mode_and_toggle(self):
        on = en.effective_message_config('{"task_progress": "all"}')
        assert en.should_send_event_message(on, "event_task_progress")
        muted = en.effective_message_config(
            '{"task_progress": "all", "toggles": {"event_task_progress": false}}'
        )
        assert not en.should_send_event_message(muted, "event_task_progress")

    def test_untoggleable_types_always_send(self):
        config = en.effective_message_config('{"toggles": {}}')
        assert en.should_send_event_message(config, "event_signup_prompt")
        assert en.should_send_event_message(config, "brand_new_type")


class TestMilestones:
    def test_simple_crossings(self):
        assert en.progress_milestones_crossed(0, 30, 100) == [25]
        assert en.progress_milestones_crossed(30, 80, 100) == [50, 75]
        assert en.progress_milestones_crossed(20, 24, 100) == []

    def test_completion_is_not_a_milestone(self):
        # Crossing into >= target is the completion notification's job.
        assert en.progress_milestones_crossed(80, 100, 100) == []
        assert en.progress_milestones_crossed(0, 100, 100) == []

    def test_no_target_or_regress(self):
        assert en.progress_milestones_crossed(0, 5, 0) == []
        assert en.progress_milestones_crossed(5, 5, 100) == []
        assert en.progress_milestones_crossed(9, 3, 100) == []

    def test_exact_threshold_counts(self):
        assert en.progress_milestones_crossed(0, 25, 100) == [25]
        assert en.progress_milestones_crossed(24, 25, 100) == [25]
        assert en.progress_milestones_crossed(25, 26, 100) == []

    def test_small_targets(self):
        # target 4: thresholds at 1, 2, 3
        assert en.progress_milestones_crossed(0, 1, 4) == [25]
        assert en.progress_milestones_crossed(1, 3, 4) == [50, 75]


# ── layout DSL resolution ────────────────────────────────────────────────────

class TestSubstitute:
    def test_lines_with_unresolved_tokens_drop(self):
        text = "**Points** `+{points}`\n**Team total** `{team_score} pts`"
        out = ml._substitute(text, {"points": 40})
        assert out == "**Points** `+40`"

    def test_none_drops_but_empty_string_substitutes(self):
        assert ml._substitute("by {player_name}", {"player_name": None}) == ""
        # Explicit empty string is a real value (e.g. {cell_plural}).
        assert ml._substitute("cell{cell_plural} marked", {"cell_plural": ""}) == "cell marked"
        assert ml._substitute("cell{cell_plural} marked", {"cell_plural": "s"}) == "cells marked"

    def test_plain_text_untouched(self):
        assert ml._substitute("hello\nworld", {}) == "hello\nworld"


class TestRenderMessageSpec:
    def test_full_render(self):
        layout = {
            "accent_color": "#FFD700",
            "blocks": [
                {"type": "text", "content": "## {event_name}"},
                {"type": "separator"},
                {"type": "text", "content": "{missing}"},
                {"type": "separator"},
                {"type": "standings", "limit": 2, "title": "**Standings**"},
                {"type": "buttons", "buttons": [
                    {"label": "View", "url": "{event_url}"},
                    {"label": "Broken", "url": "{nope}"},
                ]},
            ],
        }
        spec = ml.render_message_spec(
            layout,
            {"event_name": "Bingo Night", "event_url": "https://x/e/1"},
            standings=[{"name": "A", "score": 5}, {"name": "B", "score": 3},
                       {"name": "C", "score": 1}],
        )
        assert spec["accent_color"] == 0xFFD700
        kinds = [b["type"] for b in spec["blocks"]]
        # dropped {missing} text collapses the doubled separator
        assert kinds == ["text", "separator", "text", "buttons"]
        assert spec["blocks"][0]["content"] == "## Bingo Night"
        standings_block = spec["blocks"][2]["content"]
        assert "**A** — `5 pts`" in standings_block and "C" not in standings_block
        assert spec["blocks"][3]["buttons"] == [{"label": "View", "url": "https://x/e/1"}]

    def test_section_falls_back_to_text_without_thumbnail(self):
        layout = {"blocks": [
            {"type": "section", "content": "hi {player_name}", "thumbnail": "{proof_url}"},
        ]}
        with_thumb = ml.render_message_spec(layout, {"player_name": "Zed", "proof_url": "https://p.png"})
        assert with_thumb["blocks"] == [
            {"type": "section", "content": "hi Zed", "thumbnail": "https://p.png"}]
        without = ml.render_message_spec(layout, {"player_name": "Zed"})
        assert without["blocks"] == [{"type": "text", "content": "hi Zed"}]

    def test_leading_trailing_separators_trimmed(self):
        layout = {"blocks": [
            {"type": "separator"},
            {"type": "text", "content": "{gone}"},
            {"type": "text", "content": "kept"},
            {"type": "separator"},
        ]}
        spec = ml.render_message_spec(layout, {})
        assert spec["blocks"] == [{"type": "text", "content": "kept"}]

    def test_empty_standings(self):
        spec = ml.render_message_spec(
            {"blocks": [{"type": "standings", "limit": 3}]}, {}, standings=[])
        assert spec["blocks"][0]["content"] == "No teams yet."

    def test_launch_button_resolves_from_event_id(self):
        layout = {"blocks": [{"type": "buttons", "buttons": [
            {"label": "Open in Discord", "launch": True},
            {"label": "Website", "url": "{event_url}"},
        ]}]}
        spec = ml.render_message_spec(
            layout, {"event_id": 42, "event_url": "https://x/e/42"}, deep_link=True)
        assert spec["blocks"][0]["buttons"] == [
            {"label": "Open in Discord", "launch": True, "event_id": "42"},
            {"label": "Website", "url": "https://x/e/42"},
        ]

    def test_launch_button_view_carried_through(self):
        layout = {"blocks": [{"type": "buttons", "buttons": [
            {"label": "Review in app", "launch": True, "view": "review"},
        ]}]}
        spec = ml.render_message_spec(layout, {"event_id": 42}, deep_link=True)
        assert spec["blocks"][0]["buttons"] == [
            {"label": "Review in app", "launch": True, "event_id": "42", "view": "review"},
        ]
        # unsupported channel: the Activity Link URL carries the view too
        linked = ml.render_message_spec(
            layout, {"event_id": 42}, deep_link=False, launch_link=True)
        assert linked["blocks"][0]["buttons"] == [
            {"label": "Review in app", "url": alc.activity_link_url(42, "review")},
        ]

    def test_launch_button_dropped_when_deeplink_off(self):
        layout = {"blocks": [{"type": "buttons", "buttons": [
            {"label": "Open in Discord", "launch": True},
            {"label": "Website", "url": "{event_url}"},
        ]}]}
        spec = ml.render_message_spec(
            layout, {"event_id": 42, "event_url": "https://x/e/42"}, deep_link=False)
        # only the URL button survives — behaviour identical to the old layouts
        assert spec["blocks"][0]["buttons"] == [{"label": "Website", "url": "https://x/e/42"}]

    def test_launch_button_becomes_activity_link_in_unsupported_channel(self):
        layout = {"blocks": [{"type": "buttons", "buttons": [
            {"label": "Open in Discord", "launch": True},
            {"label": "Website", "url": "{event_url}"},
        ]}]}
        spec = ml.render_message_spec(
            layout, {"event_id": 42, "event_url": "https://x/e/42"},
            deep_link=False, launch_link=True)
        # thread/announcement destination: launch button renders as an
        # Activity Link URL button (client-side launch works there)
        assert spec["blocks"][0]["buttons"] == [
            {"label": "Open in Discord", "url": alc.activity_link_url(42)},
            {"label": "Website", "url": "https://x/e/42"},
        ]

    def test_launch_only_row_dropped_without_event_id(self):
        layout = {"blocks": [{"type": "buttons", "buttons": [
            {"label": "Open in Discord", "launch": True},
        ]}]}
        # no event_id in context → nothing to deep-link to → empty row dropped
        spec = ml.render_message_spec(layout, {}, deep_link=True)
        assert spec["blocks"] == []

    def test_footer_appended_behind_separator(self):
        layout = {"blocks": [{"type": "text", "content": "hi"}]}
        spec = ml.render_message_spec(layout, {}, footer="-# Ev - Ends: <t:1:R>")
        assert spec["blocks"] == [
            {"type": "text", "content": "hi"},
            {"type": "separator"},
            {"type": "text", "content": "-# Ev - Ends: <t:1:R>"},
        ]

    def test_footer_without_body_has_no_leading_separator(self):
        # every layout block dropped → footer stands alone, no dangling separator
        layout = {"blocks": [{"type": "text", "content": "{gone}"}]}
        spec = ml.render_message_spec(layout, {}, footer="-# Ev")
        assert spec["blocks"] == [{"type": "text", "content": "-# Ev"}]

    def test_no_footer_leaves_blocks_untouched(self):
        layout = {"blocks": [{"type": "text", "content": "hi"}]}
        assert ml.render_message_spec(layout, {})["blocks"] == [
            {"type": "text", "content": "hi"}]


class TestDefaultLayouts:
    def test_every_layout_type_has_a_default(self):
        # Keep in sync with db.models.events.EVENT_MESSAGE_LAYOUT_TYPES
        expected = {
            "event_started", "event_ended", "event_completion",
            "event_task_progress", "event_line",
            "event_blackout", "event_lead_change", "event_pending",
            "event_activation_failed", "event_signup_prompt", "event_board",
            "event_board_turn", "event_pot",
        }
        assert set(ml.DEFAULT_LAYOUTS) == expected

    def test_defaults_render_with_typical_payloads(self):
        payloads = {
            "event_started": {"event_id": 7, "event_name": "E", "description": "d",
                              "starts_at": 1700000000, "ends_at": 1700003600, "team_count": 4},
            "event_ended": {"event_id": 7, "event_name": "E"},
            "event_completion": {"event_id": 7, "event_name": "E", "team_id": 1,
                                 "team_name": "Reds", "task_label": "Get a whip",
                                 "points": 10, "team_score": 30, "player_name": "Zed",
                                 "proof_url": "https://p.png",
                                 "completion_icon": "https://p.png", "cell_idxs": [3, 4]},
            "event_task_progress": {"event_id": 7, "event_name": "E", "team_name": "Reds",
                                    "task_label": "Boaters", "progress": 5, "target": 10,
                                    "milestone_pct": 50, "player_name": "Zed",
                                    "task_icon": "https://x/img/metrics/slayer.png"},
            "event_line": {"event_id": 7, "team_name": "Reds", "bonus_points": 25},
            "event_blackout": {"event_id": 7, "team_name": "Reds", "bonus_points": 100},
            "event_lead_change": {"event_id": 7, "team_name": "Reds", "task_label": "T"},
            "event_pending": {"event_id": 7, "task_label": "T", "player_name": "Zed",
                              "team_name": "Reds", "review_url": "https://x/review"},
            "event_activation_failed": {"event_id": 7, "event_name": "E", "reason": "no tasks",
                                        "starts_at": 1700000000},
            "event_signup_prompt": {"event_id": 7, "event_name": "E",
                                    "formation_mode": "self_join", "ends_at": 1700003600},
        }
        standings = [{"name": "Reds", "score": 30}, {"name": "Blues", "score": 20}]
        for message_type, data in payloads.items():
            context = ml.notification_context(message_type, data)
            spec = ml.render_message_spec(
                ml.DEFAULT_LAYOUTS[message_type], context, standings=standings)
            assert spec["blocks"], message_type
            joined = " ".join(
                b.get("content", "") for b in spec["blocks"] if b["type"] in ("text", "section"))
            assert not ml._TOKEN_RE.search(joined), (message_type, joined)

    def test_board_default_renders(self):
        context = {
            "event_name": "E", "event_url": "https://x/e/7",
            "board_status_line": "Live • 4 teams",
            "tasks_summary": "-# 3 task completions",
            "updated_ts": "<t:1700000000:R>", "team_count": 4,
        }
        spec = ml.render_message_spec(
            ml.DEFAULT_LAYOUTS["event_board"], context,
            standings=[{"name": "A", "score": 1}])
        joined = " ".join(b.get("content", "") for b in spec["blocks"] if "content" in b)
        assert "Live • 4 teams" in joined and "**A** — `1 pts`" in joined


class TestNotificationContext:
    def test_zero_and_none_omitted(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "points": 0, "team_score": None, "player_name": None})
        assert "points" not in context
        assert "player_name" not in context
        assert context["event_url"].endswith("/7")

    def test_progress_bar(self):
        context = ml.notification_context("event_task_progress", {
            "event_id": 7, "progress": 5, "target": 10})
        assert context["progress_bar"] == "▰▰▰▰▰▱▱▱▱▱"

    def test_cells(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "cell_idxs": [3]})
        assert context["cell_list"] == "`3`"
        assert context["cell_plural"] == ""

    def test_cells_prefer_labels_over_idxs(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "cell_idxs": [3, 4], "cell_labels": ["A1", "B2"]})
        assert context["cell_list"] == "**A1**, **B2**"
        assert context["cell_plural"] == "s"

    def test_progress_tokens_are_formatted(self):
        context = ml.notification_context("event_task_progress", {
            "event_id": 7, "progress": 3_000_000, "target": 10_000_000})
        assert context["progress"] == "3.00M"
        assert context["target"] == "10.00M"
        # The bar itself is computed from the raw numbers, unaffected.
        assert context["progress_bar"] == "▰▰▰▱▱▱▱▱▱▱"

    def test_contributors_line(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7,
            "contributors": [
                {"player_name": "Alpha", "quantity": 3},
                {"player_name": "Beta", "quantity": 12_000_000},
            ],
        })
        assert context["contributors_line"] == "**Alpha** `3`, **Beta** `12.00M`"

    def test_contributors_line_absent_when_empty(self):
        context = ml.notification_context("event_completion", {"event_id": 7})
        assert "contributors_line" not in context

    def test_received_line_built_from_item_details(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "received_item": "Dragon bones",
            "received_qty": 3, "contributed": 3, "target": 100,
        })
        assert context["received_line"] == "**3× Dragon bones** (+3 of 100)"

    def test_received_line_absent_for_non_item_completion(self):
        # No received_item (non-item task, or item_details toggled off at
        # enqueue) -> no token, so the layout's "Finished with" line drops.
        context = ml.notification_context("event_completion", {"event_id": 7})
        assert "received_line" not in context
        spec = ml.render_message_spec(
            ml.DEFAULT_LAYOUTS["event_completion"],
            {"team_name": "Red", "task_label": "T", "points": 5, "player_name": "Zed"})
        rendered = "\n".join(b.get("content", "") for b in spec["blocks"])
        assert "Finished with" not in rendered

    def test_received_line_singular_drops_multiplier(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "received_item": "Twisted bow",
            "received_qty": 1, "contributed": 1, "target": 1,
        })
        assert context["received_line"] == "**Twisted bow** (+1 of 1)"

    def test_raw_unix_timestamps_carried_for_footer(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "starts_at": 1700000000, "ends_at": 1700003600})
        assert context["starts_at_unix"] == 1700000000
        assert context["ends_at_unix"] == 1700003600
        # ...and dropped when absent (so the footer half-drops)
        bare = ml.notification_context("event_completion", {"event_id": 7})
        assert "starts_at_unix" not in bare and "ends_at_unix" not in bare

    def test_task_and_completion_icons_carried(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "task_icon": "https://x/itemdb/1.png",
            "completion_icon": "https://x/p.png"})
        assert context["task_icon"] == "https://x/itemdb/1.png"
        assert context["completion_icon"] == "https://x/p.png"
        bare = ml.notification_context("event_task_progress", {"event_id": 7})
        assert "task_icon" not in bare and "completion_icon" not in bare


class TestTaskIconLayouts:
    def test_progress_renders_icon_as_section_thumbnail(self):
        context = ml.notification_context("event_task_progress", {
            "event_id": 7, "team_name": "Reds", "task_label": "Boaters",
            "progress": 5, "target": 10, "player_name": "Zed",
            "task_icon": "https://x/img/metrics/slayer.png"})
        spec = ml.render_message_spec(ml.DEFAULT_LAYOUTS["event_task_progress"], context)
        section = spec["blocks"][0]
        assert section["type"] == "section"
        assert section["thumbnail"] == "https://x/img/metrics/slayer.png"
        assert "Reds — Boaters" in section["content"]

    def test_progress_degrades_to_text_without_icon(self):
        context = ml.notification_context("event_task_progress", {
            "event_id": 7, "team_name": "Reds", "task_label": "Boaters",
            "progress": 5, "target": 10})
        spec = ml.render_message_spec(ml.DEFAULT_LAYOUTS["event_task_progress"], context)
        assert spec["blocks"][0]["type"] == "text"  # no thumbnail → plain text

    def test_completion_uses_completion_icon_thumbnail(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "team_name": "Reds", "task_label": "Whip",
            "points": 10, "team_score": 30, "player_name": "Zed",
            "completion_icon": "https://x/img/itemdb/4151.png"})
        spec = ml.render_message_spec(ml.DEFAULT_LAYOUTS["event_completion"], context)
        section = next(b for b in spec["blocks"] if b["type"] == "section")
        assert section["thumbnail"] == "https://x/img/itemdb/4151.png"

    def test_completion_solo_collapses_to_completed_by(self):
        # A single contributor is shown as one "Completed by" line, not the
        # full multi-person "Contributors" breakdown.
        context = ml.notification_context("event_completion", {
            "event_id": 7, "team_name": "Reds", "task_label": "Whip",
            "points": 10, "team_score": 30,
            "contributors": [{"player_name": "Zed", "quantity": 2}],
        })
        assert context["completed_by_line"] == "**Completed by** `Zed`"
        assert "contributors_block" not in context
        spec = ml.render_message_spec(ml.DEFAULT_LAYOUTS["event_completion"], context)
        joined = " ".join(b.get("content", "") for b in spec["blocks"])
        assert "**Completed by** `Zed`" in joined
        assert "**Team total** `30 pts`" in joined  # non-bingo → running total

    def test_completion_multi_contributors_get_header_on_own_line(self):
        context = ml.notification_context("event_completion", {
            "event_id": 7, "team_name": "Reds", "task_label": "Whip",
            "points": 10, "team_score": 30,
            "contributors": [
                {"player_name": "Zed", "quantity": 2, "points_share": 6},
                {"player_name": "Al", "quantity": 1, "points_share": 4},
            ],
        })
        # Header, then one contributor per line (ranked, medals for the top 3).
        assert context["contributors_block"] == (
            "**Contributors**\n"
            "\U0001F947 **Zed** `2` (+6 pts)\n"
            "\U0001F948 **Al** `1` (+4 pts)")
        assert "completed_by_line" not in context

    def test_completion_bingo_shows_board_standing(self):
        # Bingo events summarize the board instead of naming the marked tile.
        context = ml.notification_context("event_completion", {
            "event_id": 7, "team_name": "Reds", "task_label": "Any unique",
            "points": 100, "team_score": 115,
            "tiles_completed": 7, "team_rank": 1, "team_count": 6,
        })
        assert context["bingo_stats"] == (
            "**Total tiles completed** `7`\n"
            "**Total points earned** `115 pts`\n"
            "**Team position** #1/6 teams")
        assert "team_total_line" not in context  # bingo → no separate running total

    def test_completion_item_named_task_drops_finished_with(self):
        # target 1 + label == item -> the "Finished with" line is redundant.
        context = ml.notification_context("event_completion", {
            "event_id": 7, "team_name": "Reds", "task_label": "Twisted bow",
            "points": 50, "received_item": "Twisted bow",
            "received_qty": 1, "contributed": 1, "target": 1,
        })
        assert "received_line" not in context
        # ...but a differently-named task keeps it.
        keep = ml.notification_context("event_completion", {
            "event_id": 7, "task_label": "Any Nightmare Unique",
            "received_item": "Inquisitor's hauberk",
            "received_qty": 1, "contributed": 1, "target": 1,
        })
        assert keep["received_line"] == "**Inquisitor's hauberk** (+1 of 1)"


class TestEventFooterLine:
    def test_full_line(self):
        assert en.event_footer_line("Bingo Night", 1700000000, 1700003600) == (
            "-# Bingo Night - Starts: <t:1700000000:R> - Ends: <t:1700003600:R>")

    def test_half_drops_when_timestamp_missing(self):
        assert en.event_footer_line("E", None, 1700003600) == (
            "-# E - Ends: <t:1700003600:R>")
        assert en.event_footer_line("E", 1700000000, None) == (
            "-# E - Starts: <t:1700000000:R>")
        assert en.event_footer_line("E") == "-# E"

    def test_none_without_event_name(self):
        assert en.event_footer_line(None, 1, 2) is None
        assert en.event_footer_line("", 1, 2) is None

    def test_bad_timestamps_ignored(self):
        assert en.event_footer_line("E", "nope", 1700003600) == (
            "-# E - Ends: <t:1700003600:R>")


class TestProgressBar:
    def test_bounds(self):
        assert ml.text_progress_bar(0, 10) == "▱" * 10
        assert ml.text_progress_bar(10, 10) == "▰" * 10
        assert ml.text_progress_bar(99, 10) == "▰" * 10  # clamped
        assert ml.text_progress_bar(5, 0) == ""
