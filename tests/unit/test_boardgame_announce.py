"""Discord posts for board-game item uses, task picks and mercy skips
(services/boardgame_announce): every item that changes a team's task names the
new one, and nothing that changes the board goes unannounced."""

import importlib.util
import sys
from pathlib import Path

import pytest

# The conftest stubs the ``services`` package, so the real modules load by file
# path (the test_boardgame_engine.py pattern). The announcer's lazy imports of
# the engine and event_notifications are pointed at the real ones per test.
_BASE = Path(__file__).resolve().parent.parent.parent / "services"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _BASE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_fx = _load("services.boardgame_effects", "boardgame_effects.py")
bg = _load("_real_boardgame_engine_announce", "boardgame_engine.py")
en = _load("_real_event_notifications_announce", "event_notifications.py")
ba = _load("_real_boardgame_announce", "boardgame_announce.py")


@pytest.fixture(autouse=True)
def _real_lazy_imports(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.boardgame_engine", bg)
    monkeypatch.setitem(sys.modules, "services.event_notifications", en)


def _use(effect, **kw):
    return {"effect": effect, "item_name": "Magic Item", **kw}


class TestComposeItemUse:
    def test_reroll_task_names_old_and_new(self):
        d = ba.compose_item_use(
            _use("reroll_task", previous_task_label="Zulrah kill",
                 task_label="Vorkath kill"), "Reds")
        assert d["action_title"] == "\U0001F504 Task rerolled"
        assert "**Reds**" in d["action_line"] and "**Zulrah kill**" in d["action_line"]
        assert d["action_detail_line"] == "**New task** Vorkath kill"
        assert d["next_task_label"] == "Vorkath kill"

    def test_skip_manual_prompts_a_roll(self):
        d = ba.compose_item_use(_use("skip_task", previous_task_label="Whip"), "Reds")
        assert "skip **Whip**" in d["action_line"]
        assert d["action_detail_line"] == "Roll the dice to move on."
        assert d["won"] is False

    def test_skip_auto_roll_shows_the_move_and_task(self):
        d = ba.compose_item_use(_use(
            "skip_task", previous_task_label="Whip",
            roll={"dice": [4], "from": 3, "to": 7, "won": False, "task_label": "Tbow"}),
            "Reds")
        detail = d["action_detail_line"]
        assert "Rolled `4`" in detail and "Tile `3` → `7`" in detail
        assert "**New task** Tbow" in detail

    def test_skip_auto_roll_win(self):
        d = ba.compose_item_use(_use(
            "skip_task", roll={"dice": [6], "from": 9, "to": 12, "won": True}), "Reds")
        assert d["won"] is True and "reached the finish" in d["action_title"]

    def test_teleport_win_and_roadblock(self):
        win = ba.compose_item_use(_use("advance", **{"from": 2, "to": 12, "won": True}),
                                  "Reds")
        assert win["won"] and "reached the finish" in win["action_title"]
        stopped = ba.compose_item_use(_use(
            "advance", previous_task_label="Whip", task_label=None,
            blocked=True, **{"from": 2, "to": 4, "won": False},
            tile_effect={"effect_type": "roadblock", "tile_idx": 4, "stopped": True,
                         "consumed": True, "stall_turns": 1}), "Reds")
        assert "leaving **Whip** behind" in stopped["action_line"]
        assert "Stopped by a roadblock on tile `4`" in stopped["action_detail_line"]

    def test_reroll_move_lists_the_new_roll(self):
        d = ba.compose_item_use(_use(
            "reroll_move", dice=[2], task_label="Bandos", **{"from": 1, "to": 3,
                                                             "won": False}), "Reds")
        assert "Rolled `2`" in d["action_detail_line"]
        assert "**New task** Bandos" in d["action_detail_line"]

    def test_attacks_name_victim_and_their_new_task(self):
        kb = ba.compose_item_use(_use(
            "knockback", target_team_id=2, tiles=3, previous_task_label="Whip",
            task_label="Scurrius", **{"from": 8, "to": 5}), "Reds", "Blues")
        assert "knocked **Blues** back 3 tiles" in kb["action_line"]
        assert "**New task for Blues** Scurrius" in kb["action_detail_line"]
        ro = ba.compose_item_use(_use(
            "reroll_opponent_task", target_team_id=2, previous_task_label="Whip",
            task_label="Scurrius"), "Reds", "Blues")
        assert "**Blues**'s task" in ro["action_line"]
        assert ro["action_detail_line"] == "**New task for Blues** Scurrius"
        st = ba.compose_item_use(_use("steal_item", target_team_id=2,
                                      stolen_item_name="Shield"), "Reds", "Blues")
        assert "stole **Shield** from **Blues**" in st["action_line"]
        fr = ba.compose_item_use(_use("freeze_opponent", target_team_id=2,
                                      frozen_rolls=2), "Reds", "Blues")
        assert "froze **Blues** for **2** rolls" in fr["action_line"]

    def test_blocked_attack(self):
        d = ba.compose_item_use(_use("knockback", absorbed=True, absorbed_by="ward",
                                     target_team_id=2), "Reds", "Blues")
        assert d["absorbed"] and "Attack blocked" in d["action_title"]
        assert "with their ward" in d["action_line"]

    def test_self_buffs_are_announced(self):
        for effect, extra, needle in (
            ("boost_coins", {"boost_multiplier": 3}, "multiplied by 3"),
            ("shield", {"shielded": True}, "next attack"),
            ("ward", {"blocks": ["freeze_opponent"]}, "next freeze"),
            ("extra_dice", {"extra_dice": 2}, "2 extra dice"),
            ("extra_dice", {"extra_dice": 1}, "1 extra die "),
            ("choose_roll", {"chosen_roll": 5}, "**5**"),
            ("coin_toll", {"coins_per_team": 25}, "**25** coins"),
            ("roadblock", {"roadblock_tile_idx": 6, "behavior": {"stall_turns": 1}},
             "tile `6`"),
            ("cleanse", {"cleansed": ["freeze_opponent"], "unblocked": False},
             "shook off a freeze"),
        ):
            d = ba.compose_item_use(_use(effect, **extra), "Reds")
            assert d and needle in d["action_line"], (effect, d)

    def test_nothing_for_refunds_or_choice_draws(self):
        assert ba.compose_item_use({"refunded": True, "reason": "disabled"}, "Reds") is None
        assert ba.compose_item_use(_use("choose_task", candidates=3), "Reds") is None

    def test_no_em_dashes_in_new_copy(self):
        d = ba.compose_item_use(_use("reroll_task", previous_task_label="A",
                                     task_label="B"), "Reds")
        assert "—" not in d["action_line"] + d["action_title"]


class TestChoiceAndMercy:
    def test_task_choice(self):
        d = ba.compose_task_choice({"task_label": "Vorkath", "previous_task_label": "Whip",
                                    "candidates": 3}, "Reds")
        assert "from 3 choices, replacing **Whip**" in d["action_line"]
        assert d["action_detail_line"] == "**New task** Vorkath"

    def test_mercy_manual_and_auto(self):
        manual = ba.compose_mercy("Reds", "Whip", None)
        assert "ran out of time on **Whip**" in manual["action_line"]
        assert manual["action_detail_line"] == "Roll the dice to move on."
        auto = ba.compose_mercy("Reds", "Whip", {"dice": [3], "from": 2, "to": 5,
                                                 "won": False, "task_label": "Zulrah"})
        assert "**New task** Zulrah" in auto["action_detail_line"]


class TestEffectLines:
    def test_freeze_roadblock_toll_and_stall(self):
        assert "Frozen" in bg.effect_lines({"frozen": True, "dice": [3]})["frozen_line"]
        rb = bg.effect_lines({"tile_effect": {"stopped": True, "tile_idx": 4,
                                              "stall_turns": 2}})
        assert "stalled for 2 turns" in rb["roadblock_line"]
        toll = bg.effect_lines({"coin_toll": {"total": 50,
                                              "stolen": [{"team_id": 2, "coins": 25},
                                                         {"team_id": 3, "coins": 25}]}})
        assert "`50` coins from 2 teams" in toll["toll_line"]
        held = bg.effect_lines({"blocked": True, "dice": [], "stall_remaining": 1})
        assert "1 more turn to wait" in held["stall_line"]
        free = bg.effect_lines({"blocked": True, "dice": [], "blocked_cleared": True})
        assert "back in play" in free["stall_line"]
        assert bg.effect_lines({"dice": [3], "from": 1, "to": 4}) == {}

    def test_turn_data_flags_a_stalled_turn(self):
        data = bg.turn_notification_data(
            team_id=1, roll={"blocked": True, "dice": [], "from": 4, "to": 4,
                             "stall_remaining": 1})
        assert data["stalled"] is True and data["stall_line"]
