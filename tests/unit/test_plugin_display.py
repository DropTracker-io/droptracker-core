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


# --------------------------------------------------------------------------- #
# Login catch-up digest (moved from the plugin's summarizeEventGroup)
# --------------------------------------------------------------------------- #
NOW = 1_800_000_000
EV = {"id": 19, "name": "Bingo Extravaganza"}


def env(kind, ts=NOW - 7200, event=EV, **data):
    out = {"id": f"{kind}-{ts}", "type": kind, "ts": ts, "data": data}
    if event is not None:
        out["event"] = event
    return out


def backlog(ts=NOW - 7200):
    return [
        env("event_completion", ts, task_id=218, task_label="Obtain 3 Bandos hilts", points=5),
        env("event_task_progress", ts, task_id=127, task_label="1,000,000 Magic XP"),
        env("event_lead_change", ts, task_id=218, team_name="Team Alpha", team_score=120),
        env("event_line", ts, team_name="Team Alpha", bonus_points=10),
    ]


def test_parse_hidden_tolerates_garbage():
    assert pd.parse_hidden("60:1,2;61:4") == {60: {1, 2}, 61: {4}}
    assert pd.parse_hidden("60:0;x:1;62:;63:a,5;") == {63: {5}}
    assert pd.parse_hidden(None) == {}


def test_catch_up_gate_needs_both_size_and_staleness():
    assert pd.is_catch_up_batch(backlog(), NOW)
    # A live burst (all fresh) is not a backlog.
    assert not pd.is_catch_up_batch(backlog(NOW - 5), NOW)
    # Stale but tiny.
    assert not pd.is_catch_up_batch(backlog()[:3], NOW)
    # A missing ts never counts as stale.
    assert not pd.is_catch_up_batch(backlog(0), NOW)
    # Bridge chat neither counts toward the size nor the staleness.
    chat = [env("clan_chat_message", NOW - 7200, event=None, sender="a", message="b")]
    assert not pd.is_catch_up_batch(backlog(NOW - 5)[:3] + chat * 3, NOW)


def test_digest_is_header_tallies_and_one_card():
    out = pd.digest_batch(backlog(), now=NOW)
    assert len(out) == 1
    d = out[0]
    assert d["type"] == pd.DIGEST_TYPE and d["priority"] == "high"
    assert d["event"] == {"id": 19, "name": "Bingo Extravaganza"}
    disp = d["display"]
    assert disp["title"] == "While you were away"
    assert [(l["text"], l["tone"]) for l in disp["lines"]] == [
        ("1 task completed (+5 pts)", "complete"),
        ("1 bingo line (+10 pts)", "line"),
        ("Team Alpha now leads (120 pts)", "lead"),
        ("Progress on 1 task", "muted"),
    ]
    assert disp["text"] == ("1 task completed (+5 pts), 1 bingo line (+10 pts), "
                            "Team Alpha now leads (120 pts), Progress on 1 task.")
    assert disp["card"]["kind"] == "DIGEST"
    assert disp["card"]["headline"] == "Bingo Extravaganza"


def test_digest_leaves_out_hidden_or_muted_progress():
    hidden = pd.digest_batch(backlog(), {19: {127}}, now=NOW)[0]["display"]
    assert not any(l["text"].startswith("Progress") for l in hidden["lines"])
    other_event = pd.digest_batch(backlog(), {20: {127}}, now=NOW)[0]["display"]
    assert any(l["text"].startswith("Progress") for l in other_event["lines"])
    muted = pd.digest_batch(backlog(), progress_on=False, now=NOW)[0]["display"]
    assert not any(l["text"].startswith("Progress") for l in muted["lines"])


def test_digest_closing_line_and_budget():
    batch = backlog() + [env("event_started"), env("event_board_turn"), env("event_ended")]
    disp = pd.digest_batch(batch, now=NOW)[0]["display"]
    texts = [l["text"] for l in disp["lines"]]
    assert len(texts) == pd.CATCHUP_MAX_DETAIL_LINES
    assert texts[0] == "The event started."
    assert texts[-1] == "The event has ended."
    # The closing line is not part of the pop-up body.
    assert "ended" not in disp["text"]


def test_digest_latest_lead_change_wins():
    batch = backlog() + [env("event_lead_change", NOW - 60, team_name="Team Beta", team_score=1_250_000)]
    texts = [l["text"] for l in pd.digest_batch(batch, now=NOW)[0]["display"]["lines"]]
    assert "Team Beta now leads (1.25M pts)" in texts


def test_digest_groups_per_event_and_passes_the_rest_through():
    other = {"id": 20, "name": "Board"}
    notice = env("submission_notice", event=None, message="hi")
    chat = env("clan_chat_message", event=None, sender="a", message="b")
    batch = [notice] + backlog() + [env("event_board_roll_prompt", event=other), chat,
                                    env("event_kc_milestone", event={"id": 21, "name": "Quiet"})]
    out = pd.digest_batch(batch, now=NOW)
    assert [e["type"] for e in out] == ["submission_notice", "event_digest", "event_digest", "clan_chat_message"]
    assert out[2]["event"]["id"] == 20
    assert out[2]["display"]["lines"] == [{"text": "Your team can roll the dice!", "tone": "action"}]
    assert out[2]["display"]["text"] == "Your team can roll the dice!"


def test_digest_leaves_a_live_batch_alone():
    batch = backlog(NOW - 5)
    assert pd.digest_batch(batch, now=NOW) is batch


def test_digest_never_raises():
    batch = backlog() + [{"type": "event_line", "ts": "x", "event": "nope", "data": None}]
    out = pd.digest_batch(batch, now=NOW)
    assert out and out[0]["type"] == pd.DIGEST_TYPE
