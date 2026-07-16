"""Unit tests for the pure event-notification layer (Task 19):
channel-kind mapping / fallback resolution + embed content specs.

Loaded directly from the file path (like test_event_engine_matcher.py) so the
conftest sys.modules stubs for db/services never interfere — the module's
top-level imports are stdlib-only by design.
"""

import importlib.util
import os
import sys

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_notifications.py",
)
_spec = importlib.util.spec_from_file_location("_event_notifications_under_test", _MODULE_PATH)
en = importlib.util.module_from_spec(_spec)
sys.modules["_event_notifications_under_test"] = en
_spec.loader.exec_module(en)

ALL_TYPES = (
    "event_started", "event_ended", "event_completion",
    "event_line", "event_blackout", "event_lead_change", "event_pending",
    "event_activation_failed", "event_signup_prompt", "event_task_progress",
    "event_board_turn",
)


# ── channel-kind mapping ─────────────────────────────────────────────────────

class TestKindMapping:
    def test_spec_table(self):
        assert en.KIND_FOR_TYPE == {
            "event_started": "announcements",
            "event_ended": "announcements",
            "event_completion": "completions",
            "event_line": "completions",
            "event_blackout": "completions",
            "event_lead_change": "leaderboard",
            "event_pending": "admin",
            "event_activation_failed": "admin",
            "event_signup_prompt": "announcements",
            "event_task_progress": "completions",
            "event_board_turn": "completions",
        }

    def test_all_families_covered(self):
        assert set(en.EVENT_NOTIFICATION_TYPES) == set(ALL_TYPES)


# ── channel resolution + fallbacks ───────────────────────────────────────────

class TestResolveEventChannel:
    FULL = {
        "announcements": "100",
        "completions": "200",
        "leaderboard": "300",
        "admin": "400",
    }

    def test_direct_hits(self):
        assert en.resolve_event_channel(self.FULL, "event_started") == "100"
        assert en.resolve_event_channel(self.FULL, "event_ended") == "100"
        assert en.resolve_event_channel(self.FULL, "event_completion") == "200"
        assert en.resolve_event_channel(self.FULL, "event_line") == "200"
        assert en.resolve_event_channel(self.FULL, "event_blackout") == "200"
        assert en.resolve_event_channel(self.FULL, "event_lead_change") == "300"
        assert en.resolve_event_channel(self.FULL, "event_pending") == "400"
        assert en.resolve_event_channel(self.FULL, "event_activation_failed") == "400"

    def test_fallback_to_announcements(self):
        only_ann = {"announcements": "100"}
        for t in ("event_completion", "event_line",
                  "event_blackout", "event_lead_change", "event_pending",
                  "event_activation_failed"):
            assert en.resolve_event_channel(only_ann, t) == "100"

    def test_announcements_has_no_fallback(self):
        assert en.resolve_event_channel({"completions": "200"}, "event_started") is None

    def test_nothing_configured_skips_silently(self):
        for t in ALL_TYPES:
            assert en.resolve_event_channel({}, t) is None
            assert en.resolve_event_channel(None, t) is None

    def test_specific_kind_wins_over_announcements(self):
        both = {"announcements": "100", "admin": "400"}
        assert en.resolve_event_channel(both, "event_pending") == "400"

    def test_unknown_type_is_none(self):
        assert en.resolve_event_channel(self.FULL, "drop") is None

    def test_channel_id_coerced_to_string(self):
        assert en.resolve_event_channel({"announcements": 123}, "event_started") == "123"


# ── role pings (web_events.ping_config) ─────────────────────────────────────

class TestPingRoles:
    def test_unset_config_pings_nobody(self):
        assert en.event_ping_role_ids(None, "event_started") == []
        assert en.event_ping_role_ids("", "event_started") == []

    def test_corrupt_config_pings_nobody(self):
        assert en.event_ping_role_ids("not json", "event_started") == []
        assert en.event_ping_role_ids('["list"]', "event_started") == []
        assert en.event_ping_role_ids('{"event_started": "123"}', "event_started") == []

    def test_configured_key_returns_role_strings(self):
        raw = '{"event_started": ["111", 222], "event_ended": ["333"]}'
        assert en.event_ping_role_ids(raw, "event_started") == ["111", "222"]
        assert en.event_ping_role_ids(raw, "event_ended") == ["333"]
        # Types without a configured key stay silent.
        assert en.event_ping_role_ids(raw, "event_completion") == []

    def test_ping_content_mentions(self):
        assert en.ping_content(["1", "2"]) == "<@&1> <@&2>"
        assert en.ping_content([]) is None


class TestFormatGp:
    def test_plain_below_threshold(self):
        assert en.format_gp(0) == "0"
        assert en.format_gp(5000) == "5,000"
        assert en.format_gp(100_000) == "100,000"

    def test_abbreviated_above_threshold(self):
        assert en.format_gp(100_001) == "100.00K"
        assert en.format_gp(10_000_000) == "10.00M"
        assert en.format_gp(2_500_000_000) == "2.50B"

    def test_negative_and_non_numeric(self):
        assert en.format_gp(-10_000_000) == "-10.00M"
        assert en.format_gp("n/a") == "n/a"


# ── embed content specs ──────────────────────────────────────────────────────

def _spec(ntype, data=None, standings=None):
    base = {"event_id": 42, "event_name": "Summer Bingo"}
    base.update(data or {})
    return en.event_embed_spec(ntype, base, standings=standings)


class TestEmbedSpecs:
    def test_every_type_builds_a_titled_linked_spec(self):
        for t in ALL_TYPES:
            spec = _spec(t)
            assert isinstance(spec["title"], str) and spec["title"], t
            assert spec["url"] == "https://www.droptracker.io/events/42", t
            assert spec["author_name"] == "Summer Bingo", t
            assert isinstance(spec["fields"], list), t

    def test_activation_failed_card(self):
        spec = _spec("event_activation_failed", {
            "reason": "The event needs at least one team.",
            "starts_at": 1751700000,
        })
        assert "could not start" in spec["title"]
        assert "The event needs at least one team." in spec["description"]
        names = [f["name"] for f in spec["fields"]]
        assert "Scheduled start" in names
        assert "Fix it" in names

    def test_started_card(self):
        spec = _spec("event_started", {
            "description": "May the best team win",
            "starts_at": 1751700000, "ends_at": 1752300000, "team_count": 4,
        })
        assert "has started" in spec["title"]
        assert "May the best team win" in spec["description"]
        assert "https://www.droptracker.io/events/42" in spec["description"]
        names = [f["name"] for f in spec["fields"]]
        assert "Started" in names and "Ends" in names and "Teams" in names

    def test_ended_final_standings(self):
        spec = _spec("event_ended", standings=[
            {"name": "Red", "score": 30}, {"name": "Blue", "score": 20},
        ])
        assert "has ended" in spec["title"]
        standings = next(f for f in spec["fields"] if f["name"] == "Final standings")
        assert "\U0001F947" in standings["value"] and "**Red** — `30 pts`" in standings["value"]
        assert "\U0001F948" in standings["value"] and "Blue" in standings["value"]

    def test_completion_embed(self):
        spec = _spec("event_completion", {
            "task_label": "Get a whip", "team_name": "Red", "player_name": "Alpha One",
            "points": 10, "team_score": 30, "proof_url": "https://x/proof.png",
        })
        assert "Get a whip" in spec["title"]
        assert "**Red**" in spec["description"]
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Points"] == "`+10`"
        assert fields["Team total"] == "`30 pts`"
        # No ledger-derived contributors were passed — falls back to the
        # single completer.
        assert fields["Completed by"] == "`Alpha One`"
        assert spec["thumbnail"] == "https://x/proof.png"

    def test_completion_contributors_list(self):
        spec = _spec("event_completion", {
            "task_label": "Get a whip", "team_name": "Red",
            "contributors": [
                {"player_name": "Alpha One", "quantity": 3},
                {"player_name": "Beta Two", "quantity": 12_000_000},
            ],
        })
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert "Completed by" not in fields
        assert fields["Contributors"] == "`Alpha One` (3), `Beta Two` (12.00M)"

    def test_completion_solo_contributor_collapses(self):
        # A single contributor is shown as "Completed by", not "Contributors".
        spec = _spec("event_completion", {
            "task_label": "Get a whip", "team_name": "Red",
            "contributors": [{"player_name": "Solo", "quantity": 1}],
        })
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Completed by"] == "`Solo`"
        assert "Contributors" not in fields

    def test_completion_bingo_board_standing(self):
        spec = _spec("event_completion", {
            "task_label": "Any unique", "team_name": "Red", "points": 100,
            "team_score": 115, "tiles_completed": 7, "team_rank": 1, "team_count": 6,
        })
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Total tiles completed"] == "`7`"
        assert fields["Total points earned"] == "`115 pts`"
        assert fields["Team position"] == "`#1/6 teams`"
        assert "Team total" not in fields  # bingo replaces the running total

    def test_completion_item_named_task_omits_received(self):
        spec = _spec("event_completion", {
            "task_label": "Twisted bow", "team_name": "Red",
            "received_item": "Twisted bow", "received_qty": 1,
            "contributed": 1, "target": 1,
        })
        assert "Received" not in {f["name"] for f in spec["fields"]}

    def test_progress_field_abbreviates_large_targets(self):
        spec = _spec("event_task_progress", {
            "task_label": "10M loot value", "team_name": "Red",
            "progress": 3_000_000, "target": 10_000_000,
        })
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Progress"] == "`3.00M / 10.00M`"

    def test_progress_field_stays_exact_below_threshold(self):
        spec = _spec("event_task_progress", {
            "task_label": "5 whips", "team_name": "Red",
            "progress": 2, "target": 5,
        })
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Progress"] == "`2 / 5`"

    def test_completion_zero_points_has_no_points_field(self):
        spec = _spec("event_completion", {"task_label": "Screenshot", "team_name": "Red"})
        assert "Points" not in {f["name"] for f in spec["fields"]}
        assert spec["thumbnail"] is None

    def test_completion_received_item_field(self):
        spec = _spec("event_completion", {
            "task_label": "Collect 100 Dragon bones", "team_name": "Red",
            "received_item": "Dragon bones", "received_qty": 3,
            "contributed": 3, "target": 100,
        })
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Received"] == "**3× Dragon bones** (+3 of 100)"

    def test_completion_received_field_absent_for_non_item(self):
        spec = _spec("event_completion", {"task_label": "T", "team_name": "Red"})
        assert "Received" not in {f["name"] for f in spec["fields"]}

    def test_line_blackout(self):
        line = _spec("event_line", {"team_name": "Red", "bonus_points": 25})
        assert "line" in line["description"]
        assert {f["name"]: f["value"] for f in line["fields"]}["Bonus"] == "`+25 pts`"
        blackout = _spec("event_blackout", {"team_name": "Red", "bonus_points": 100})
        assert "BLACKOUT" in blackout["title"]
        assert {f["name"]: f["value"] for f in blackout["fields"]}["Bonus"] == "`+100 pts`"

    def test_lead_change_top3(self):
        spec = _spec("event_lead_change",
                     {"team_name": "Blue", "task_label": "Get a whip"},
                     standings=[{"name": "Blue", "score": 40},
                                {"name": "Red", "score": 30},
                                {"name": "Green", "score": 10}])
        assert "New leader" in spec["title"] and "Blue" in spec["title"]
        assert "Get a whip" in spec["description"]
        standings = next(f for f in spec["fields"] if f["name"] == "Standings")
        assert standings["value"].count("\n") == 2  # exactly three lines

    def test_pending_review_deep_link(self):
        spec = _spec("event_pending", {
            "task_label": "Screenshot task", "player_name": "Alpha One",
            "team_name": "Red", "review_url": "https://www.droptracker.io/groups/9/events/42",
            "proof_url": "https://x/p.png",
        })
        assert "awaiting review" in spec["title"]
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Player"] == "`Alpha One`"
        assert fields["Team"] == "**Red**"
        assert "https://www.droptracker.io/groups/9/events/42" in fields["Review"]
        assert spec["thumbnail"] == "https://x/p.png"

    def test_pending_falls_back_to_event_url(self):
        spec = _spec("event_pending", {"task_label": "T"})
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert "https://www.droptracker.io/events/42" in fields["Review"]

    def test_team_id_placeholder_when_name_missing(self):
        spec = _spec("event_completion", {"task_label": "T", "team_id": 7})
        assert "Team 7" in spec["description"]

    def test_unknown_type_generic_card(self):
        spec = _spec("event_something_new")
        assert spec["title"] == "Summer Bingo"
        assert "https://www.droptracker.io/events/42" in (spec["description"] or "")
