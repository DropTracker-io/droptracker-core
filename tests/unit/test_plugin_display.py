"""Server-composed display for in-game event notifications (plugin_display).

What these pin:

* the wording matches what the plugin composed itself before (ValueFormat
  rounding included), so moving composition server-side changes nothing a
  player sees;
* cards speak from the reader's team: "In the lead!" for the leader, "Behind
  by" for everyone else;
* only plain text in fixed slots goes out: tags are stripped, strings capped,
  colours are tones from a closed set;
* a blind event's tasks lend nothing to a card;
* compose never raises, whatever the payload.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "services", "plugin_display.py")
_spec = importlib.util.spec_from_file_location("_plugin_display_real", _PATH)
pd = importlib.util.module_from_spec(_spec)
sys.modules["_plugin_display_real"] = pd
_spec.loader.exec_module(pd)

EVENT = {"id": 9, "name": "Autumn Bingo"}


def ctx(**over):
    base = {
        "own": {"id": 1, "name": "Red", "score": 1200, "rank": 2, "team_count": 4,
                "icon_item_id": 4151, "icon_path": None},
        "tasks_total": 25, "tasks_completed": 7, "has_bingo": True,
        "tasks": {5: {"label": "Bandos set", "points": 40, "icon_item_id": 11832, "icon_path": None}},
        "ends_at": "2026-10-25T18:00:00", "time_left": "3d 1h",
    }
    base.update(over)
    return base


class TestFormatting:
    @pytest.mark.parametrize("value,expected", [
        (0, "0"), (500, "500"), (9999, "9,999"), (10000, "10K"), (813636, "813.6K"),
        (1250000, "1.25M"), (10000000, "10M"), (2147483647, "2.15B"), (-25000, "-25K"),
        # HALF_UP like Java, not Python's banker's rounding
        (1125000, "1.13M"), (12350, "12.4K"),
    ])
    def test_abbrev_matches_the_plugin(self, value, expected):
        assert pd.abbrev(value) == expected

    @pytest.mark.parametrize("n,expected", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
                                            (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (112, "112th")])
    def test_ordinal(self, n, expected):
        assert pd.ordinal(n) == expected

    def test_clean_strips_tags_and_caps(self):
        assert pd.clean("<col=ff0000>Red</col> team") == "Red team"
        assert pd.clean("   ") is None
        assert pd.clean("x" * 200) == "x" * 119 + "…"

    def test_time_left(self):
        now = datetime(2026, 10, 22, 12, 0, 0)
        assert pd.time_left(now + timedelta(days=2, hours=4, minutes=5), now) == "2d 4h"
        assert pd.time_left(now + timedelta(hours=3, minutes=12), now) == "3h 12m"
        assert pd.time_left(now + timedelta(seconds=20), now) == "1m"
        assert pd.time_left(now - timedelta(minutes=1), now) is None


class TestWording:
    def test_completion_with_an_item(self):
        d = pd.compose("event_completion", EVENT, {
            "player_name": "K0eppy", "received_item": "Bandos hilt", "received_qty": 1,
            "task_label": "Bandos set", "task_id": 5, "points": 40, "team_id": 1, "team_name": "Red",
            "icon_item_id": 11812})
        assert d["title"] == "Task complete!"
        assert d["text"] == "K0eppy received Bandos hilt, completing: Bandos set (+40 pts)"
        assert d["chat"] == {"tag": "COMPLETE", "tone": "complete", "emphasis": "Bandos hilt"}
        card = d["card"]
        assert (card["kind"], card["caption"], card["headline"], card["detail"]) == (
            "COMPLETE", "Task complete", "Bandos set", "Bandos hilt")
        assert (card["leftLabel"], card["leftValue"]) == ("Obtained by:", "K0eppy")
        assert (card["rightLabel"], card["rightValue"]) == ("Points:", "+40")
        assert d["icon_item_id"] == 11812

    def test_tile_completion_names_the_tile_and_standing(self):
        d = pd.compose("event_completion", EVENT, {
            "player_name": "K0eppy", "task_label": "Kill Vorkath", "cell_idxs": [3],
            "cell_labels": ["Dragons", "Kill Vorkath"], "tiles_completed": 7, "team_rank": 2,
            "team_count": 5, "points": 10})
        assert d["title"] == "Tile complete!"
        assert d["text"] == ("K0eppy completed: Kill Vorkath (+10 pts) — tile: Dragons"
                             " — 7 tiles, 2nd of 5")
        assert d["card"]["kind"] == "TILE"
        assert d["card"]["headline"] == "Dragons"
        assert (d["card"]["rightLabel"], d["card"]["rightValue"]) == ("Standing:", "2nd of 5")

    def test_manual_credit_says_so(self):
        d = pd.compose("event_completion", EVENT, {"team_name": "Red", "task_label": "x",
                                                   "source_type": "manual"})
        assert d["text"].endswith("(manual award)")
        assert (d["card"]["rightLabel"], d["card"]["rightValue"]) == ("Credit:", "Manual")

    def test_quantities_but_never_for_points_credit(self):
        base = {"player_name": "a", "received_item": "Coal", "received_qty": 3, "task_label": "t"}
        assert "Coal ×3" in pd.compose("event_task_progress", EVENT, base)["text"]
        assert "×" not in pd.compose("event_task_progress", EVENT, dict(base, points_based=True))["text"]

    def test_progress_pair_and_percentage(self):
        d = pd.compose("event_task_progress", EVENT, {"player_name": "a", "task_label": "XP",
                                                      "progress": 2500000, "target": 10000000})
        assert d["text"] == "a progressed XP (2.5M/10M)"
        assert d["card"]["rightValue"] == "2.5M/10M"
        assert (d["card"]["have"], d["card"]["need"]) == (2500000, 10000000)
        pct = pd.compose("event_task_progress", EVENT, {"task_label": "XP", "milestone_pct": 40})
        assert pct["text"] == "A teammate progressed XP (40%)"
        assert (pct["card"]["have"], pct["card"]["need"]) == (40, 100)

    def test_lead_change(self):
        d = pd.compose("event_lead_change", EVENT, {"team_name": "Blue", "team_score": 15000})
        assert d["text"] == "Blue took the lead (15K pts) in Autumn Bingo!"
        assert d["chat"]["tone"] == "lead"

    def test_bookends_and_windows(self):
        assert pd.compose("event_started", EVENT, {})["text"] == "Autumn Bingo has started!"
        assert pd.compose("event_ended", {"id": 1}, {})["text"] == "Your event has ended."
        assert pd.compose("event_window_opened", EVENT, {})["card"]["kind"] == "STARTED"
        assert pd.compose("event_window_closed", EVENT, {})["chat"]["tag"] == "SCORING PAUSED"

    def test_line_blackout_board_roll(self):
        assert pd.compose("event_line", EVENT, {"team_name": "Red", "bonus_points": 50})["text"] == \
            "Red completed a line (+50 pts)!"
        assert pd.compose("event_blackout", EVENT, {})["text"] == "Your team blacked out the board!"
        turn = pd.compose("event_board_turn", EVENT, {"player_name": "a", "dice_str": "4+2", "tile_to": 12,
                                                      "next_task_label": "Kill Zulrah"})
        assert turn["text"] == "a rolled 4+2 to tile 12: Kill Zulrah"
        assert turn["card"]["rightValue"] == "4+2 > 12"
        roll = pd.compose("event_board_roll_prompt", EVENT, {"coins_awarded": 30})
        assert roll["text"] == "Task complete — your team can roll the dice! (+30 coins)"

    def test_unknown_types_have_no_display(self):
        assert pd.compose("submission_notice", EVENT, {"message": "hi"}) is None
        assert pd.compose("something_new", EVENT, {}) is None


class TestReaderContext:
    def test_completion_fills_corners_from_the_team(self):
        d = pd.compose("event_completion", EVENT, {"player_name": "a", "task_label": "Bandos set",
                                                   "task_id": 5, "team_id": 1, "team_name": "Red",
                                                   "team_score": 1300}, ctx())
        c = d["card"]
        # No points on the envelope left the right corner empty: the extras
        # fill corners in order, exactly as EventPopupCard.extra does.
        assert (c["rightLabel"], c["rightValue"]) == ("Team:", "Red")
        assert (c["left2Label"], c["left2Value"]) == ("Tiles done:", "7 / 25")
        assert (c["right2Label"], c["right2Value"]) == ("Team score:", "1,300 pts")
        assert d["ends_at"] == "2026-10-25T18:00:00"
        # The envelope named no item, so the task's own icon stands in.
        assert d["icon_item_id"] == 11832

    def test_lead_change_reads_differently_per_team(self):
        data = {"team_id": 1, "team_name": "Red", "team_score": 1200}
        leader = pd.compose("event_lead_change", EVENT, data, ctx())["card"]
        assert (leader["leftLabel"], leader["leftValue"]) == ("Your team:", "In the lead!")
        other = pd.compose("event_lead_change", EVENT, data, ctx(own={
            "id": 2, "name": "Blue", "score": 900, "rank": 2, "team_count": 4}))["card"]
        assert (other["leftLabel"], other["leftValue"]) == ("Your team:", "2nd of 4")
        assert (other["left2Label"], other["left2Value"]) == ("Behind by:", "300 pts")

    def test_progress_fills_team_then_worth(self):
        c = pd.compose("event_task_progress", EVENT, {"task_id": 5, "task_label": "Bandos set",
                                                      "progress": 2, "target": 5}, ctx())["card"]
        corners = [(c.get(k + "Label"), c.get(k + "Value")) for k in ("left", "right", "left2", "right2")]
        # Four corners only: "Still needed" loses out to Team and Worth.
        assert corners == [("Progressed by:", "A teammate"), ("Progress:", "2/5"),
                           ("Team:", "Red"), ("Worth:", "40 pts")]

    def test_start_card_has_ends_in_but_no_countdown_note(self):
        d = pd.compose("event_started", EVENT, {}, ctx())
        assert "ends_at" not in d
        values = [d["card"].get(k) for k in ("leftValue", "rightValue", "left2Value", "right2Value")]
        assert "3d 1h" in values and "Red" in values

    def test_blind_event_tasks_lend_nothing(self):
        d = pd.compose("event_task_progress", EVENT, {"task_id": 5, "task_label": "Hidden"}, ctx(tasks={}))
        assert d["icon_item_id"] is None
        assert "Worth:" not in json.dumps(d)

    def test_board_roll_finds_the_task_by_label(self):
        d = pd.compose("event_board_turn", EVENT, {"player_name": "a", "next_task_label": "bandos SET",
                                                   "task_id": 999}, ctx())
        assert d["icon_item_id"] == 11832


class TestSafety:
    def test_no_markup_survives(self):
        d = pd.compose("event_completion", EVENT, {"player_name": "<img=1>Evil", "task_label": "<col=f00>t</col>",
                                                   "team_name": "<br>Red"}, ctx())
        assert "<" not in json.dumps(d)

    def test_tones_and_kinds_are_a_closed_set(self):
        tones = {pd.TONE_COMPLETE, pd.TONE_TILE, pd.TONE_LEAD, pd.TONE_LINE, pd.TONE_BLACKOUT,
                 pd.TONE_ACTION, pd.TONE_INFO, pd.TONE_MUTED}
        for t in ("event_completion", "event_task_progress", "event_lead_change", "event_started",
                  "event_ended", "event_line", "event_blackout", "event_board_turn",
                  "event_board_roll_prompt", "event_window_opened", "event_window_closed"):
            d = pd.compose(t, EVENT, {}, ctx())
            assert d["chat"]["tone"] in tones
            assert d["card"]["kind"] in {"COMPLETE", "TILE", "PROGRESS", "LEAD", "LINE", "BLACKOUT",
                                         "STARTED", "ENDED", "BOARD", "ROLL"}
            json.dumps(d)  # serializable as is

    @pytest.mark.parametrize("data", [
        {"points": "lots", "progress": "x", "target": None, "cell_labels": "not a list"},
        {"team_rank": {}, "team_count": [], "received_qty": 1.5},
        {"task_id": "5", "bonus_points": True},
    ])
    def test_garbage_never_raises(self, data):
        for t in ("event_completion", "event_task_progress", "event_line", "event_lead_change"):
            pd.compose(t, EVENT, data, ctx())
            pd.compose(t, None, data, {"own": "nonsense"})
