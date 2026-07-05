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
    "event_started", "event_ended", "event_completion", "event_cell",
    "event_line", "event_blackout", "event_lead_change", "event_pending",
)


# ── channel-kind mapping ─────────────────────────────────────────────────────

class TestKindMapping:
    def test_spec_table(self):
        assert en.KIND_FOR_TYPE == {
            "event_started": "announcements",
            "event_ended": "announcements",
            "event_completion": "completions",
            "event_cell": "completions",
            "event_line": "completions",
            "event_blackout": "completions",
            "event_lead_change": "leaderboard",
            "event_pending": "admin",
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
        assert en.resolve_event_channel(self.FULL, "event_cell") == "200"
        assert en.resolve_event_channel(self.FULL, "event_line") == "200"
        assert en.resolve_event_channel(self.FULL, "event_blackout") == "200"
        assert en.resolve_event_channel(self.FULL, "event_lead_change") == "300"
        assert en.resolve_event_channel(self.FULL, "event_pending") == "400"

    def test_fallback_to_announcements(self):
        only_ann = {"announcements": "100"}
        for t in ("event_completion", "event_cell", "event_line",
                  "event_blackout", "event_lead_change", "event_pending"):
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
            "cell_idxs": [3, 7],
        })
        assert "Get a whip" in spec["title"]
        assert "**Red**" in spec["description"] and "Alpha One" in spec["description"]
        fields = {f["name"]: f["value"] for f in spec["fields"]}
        assert fields["Points"] == "`+10`"
        assert fields["Team total"] == "`30 pts`"
        assert "3, 7" in fields["Bingo"]
        assert spec["thumbnail"] == "https://x/proof.png"

    def test_completion_zero_points_has_no_points_field(self):
        spec = _spec("event_completion", {"task_label": "Screenshot", "team_name": "Red"})
        assert "Points" not in {f["name"] for f in spec["fields"]}
        assert spec["thumbnail"] is None

    def test_cell_line_blackout(self):
        cell = _spec("event_cell", {"team_name": "Red", "cell_label": "Any pet", "points": 5})
        assert "Any pet" in cell["description"] and "**Red**" in cell["description"]
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
