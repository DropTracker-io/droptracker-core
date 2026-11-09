"""Bug #140: the Colosseum split across two NPCs, and impossible Doom/Colosseum
times reaching the boards.

* Colosseum PBs landed on "Sol Heredit" while its loot landed on "Fortis
  Colosseum"; every intake path now folds the boss onto the activity.
* A Colosseum wave split, or a single Doom delve level, filed as the whole run
  sat at the top of its board forever; pb_floors refuses those times.
* npc_list spelt the delve levels "Mokhaitol"; names saved with that spelling
  still reach the corrected rows.
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from utils.hof import build_boss_plan, canonical_display_name, npc_name_candidates
from utils.npc_names import canonical_encounter_name, npc_match_key
from utils.pb_floors import below_floor, below_level_sum, floor_ms

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "submissions", "pb_lines.py")
_spec = importlib.util.spec_from_file_location("_pb_lines_bug140", _PATH)
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)


def _kills(*messages):
    data = {"player_name": "P", "acc_hash": "42", "guid": "g", "precise_timing": "false",
            "is_pb": "true", "source": "x", "type": "npc_kill"}
    for i, message in enumerate(messages, start=1):
        data[f"chat_line_{i}"] = json.dumps({"m": message, "t": 100, "p": [0, 0, 0, 1]})
    return pl.expand(data, lambda boss, count: False)


# --------------------------------------------------------------------------- #
# Colosseum: one NPC
# --------------------------------------------------------------------------- #
def test_sol_heredit_folds_onto_the_colosseum():
    assert canonical_encounter_name("Sol Heredit") == "Fortis Colosseum"
    assert canonical_encounter_name("sol heredit") == "Fortis Colosseum"
    assert npc_match_key("Sol Heredit") == npc_match_key("Fortis Colosseum")


def test_colosseum_duration_is_recorded_on_the_activity():
    kills = _kills("Your Sol Heredit kill count is: 5.",
                   "Colosseum duration: 24:13.80. Personal best: 22:01.20")
    assert [(k["boss_name"], k["kill_time"]) for k in kills] == [("Fortis Colosseum", "24:13")]


def test_a_wave_split_is_never_the_run_time():
    kills = _kills("Wave 12 completed! Wave duration: 1:34.20",
                   "Your Sol Heredit kill count is: 5.")
    assert all(k["kill_time"] != "01:34" for k in kills)


def test_a_full_run_ignores_its_wave_split():
    kills = _kills("Wave 12 completed! Wave duration: 1:34.20",
                   "Colosseum duration: 24:13.80 (new personal best)",
                   "Your Sol Heredit kill count is: 5.")
    assert [k["kill_time"] for k in kills] == ["24:13"]
    # The count line names the boss; intake folds it onto the activity.
    assert canonical_encounter_name(kills[0]["boss_name"]) == "Fortis Colosseum"


# --------------------------------------------------------------------------- #
# Floors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["Fortis Colosseum", "Sol Heredit"])
def test_colosseum_wave_times_are_below_the_floor(name):
    assert below_floor(name, 22_200)
    assert below_floor(name, 106_200)
    assert not below_floor(name, 862_200)  # the fastest real run on record


def test_doom_total_floor_spares_the_levels():
    assert below_floor("Doom of Mokhaiotl", 120_000)
    assert not below_floor("Doom of Mokhaiotl", 300_000)
    # A level's own board keeps its short times.
    assert floor_ms("Doom of Mokhaiotl (Level 3)") is None
    assert not below_floor("Doom of Mokhaiotl (Level: 8+)", 60_000)


def test_bosses_without_a_floor_are_never_refused():
    assert floor_ms("Vorkath") is None
    assert not below_floor("Vorkath", 1)
    assert not below_floor(None, 1)
    assert not below_floor("Fortis Colosseum", 0)


def test_doom_total_below_the_players_level_bests():
    bests = [30_000] * 8  # 4:00 added up
    assert below_level_sum(200_000, bests)
    assert not below_level_sum(240_000, bests)
    # One tick of rounding per level is tolerated.
    assert not below_level_sum(240_000 - 8 * 600, bests)


def test_level_sum_needs_all_eight_levels():
    assert not below_level_sum(1_000, [30_000] * 7)
    assert not below_level_sum(1_000, [30_000] * 7 + [0])


# --------------------------------------------------------------------------- #
# Hall of Fame lists saved with the old names
# --------------------------------------------------------------------------- #
def test_legacy_mokhaitol_names_reach_the_corrected_rows():
    for level in range(1, 9):
        old = f"Doom of Mokhaitol (Level: {level})"
        assert canonical_encounter_name(old) == f"Doom of Mokhaiotl (Level: {level})"
        assert npc_name_candidates(old)[0] == f"Doom of Mokhaiotl (Level: {level})"


def test_the_misspelt_boss_name_reaches_the_1_to_8_board():
    assert canonical_encounter_name("Doom of Mokhaitol") == "Doom of Mokhaiotl"
    assert npc_name_candidates("Doom of Mokhaitol")[0] == "Doom of Mokhaiotl"


def test_old_and_new_colosseum_names_share_one_hof_message():
    plan = build_boss_plan(["Sol Heredit", "Fortis Colosseum", "Zulrah"])
    assert [e.display_name for e in plan] == ["Fortis Colosseum", "Zulrah"]
    assert canonical_display_name("Sol Heredit") == "Fortis Colosseum"
    assert npc_name_candidates("Sol Heredit")[0] == "Fortis Colosseum"
