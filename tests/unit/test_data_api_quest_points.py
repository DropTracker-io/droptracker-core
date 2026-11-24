"""Quest points and grandmaster quests on the `quests` section.

Account sync does not carry the game's quest-point varp, so these come from
quest-completion notifications. The shaping rules worth pinning: the highest
recorded value wins (quest points only rise, and a replayed notification must
not lower them), the quest cape is judged against the total recorded *with*
that value, and a player with no notification reads null rather than zero.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sect = _load("_real_sections", "data_api/sections.py")


class _Session:
    """Answers each execute() with the next canned result set, in order:
    quest states, best quest points, grandmaster completions."""

    def __init__(self, *results):
        self.results = list(results)

    def execute(self, _statement):
        return list(self.results.pop(0))


def _run(states=(), best=(), gm=(), ids=(1,)):
    return sect._load_quests(_Session(states, best, gm), list(ids), {})


class TestQuestPoints:
    def test_quest_points_and_cape(self):
        out = _run(states=[(1, "0:2,1:2,3:2")], best=[(1, 343, 343, datetime(2026, 9, 1))])
        assert out[1]["quest_points"] == 343
        assert out[1]["total_quest_points"] == 343
        assert out[1]["quest_cape"] is True
        assert out[1]["finished"] == 3

    def test_short_of_the_total_is_not_a_cape(self):
        out = _run(best=[(1, 342, 343, datetime(2026, 9, 1))])
        assert out[1]["quest_cape"] is False

    def test_a_tie_on_quest_points_takes_the_newest_row(self):
        # The query orders newest first within a player; the first row wins.
        out = _run(best=[(1, 341, 343, datetime(2026, 9, 5)),
                         (1, 341, 341, datetime(2026, 8, 1))])
        assert out[1]["total_quest_points"] == 343
        assert out[1]["quest_cape"] is False

    def test_no_notification_is_null_not_zero(self):
        # "Unknown" and "none" must stay distinguishable: a milestone reader
        # that saw 0 would conclude the player has no quest points at all.
        out = _run(states=[(1, "0:2")])
        assert out[1]["quest_points"] is None
        assert out[1]["quest_cape"] is False

    def test_a_player_with_points_but_no_sync_still_appears(self):
        out = _run(best=[(7, 120, 343, datetime(2026, 9, 1))], ids=(7,))
        assert out[7]["quest_points"] == 120
        assert out[7]["finished"] == 0

    def test_grandmaster_quests_are_listed_sorted(self):
        out = _run(best=[(1, 300, 343, datetime(2026, 9, 1))],
                   gm=[(1, "Song of the Elves"), (1, "Dragon Slayer II")])
        assert out[1]["grandmaster_quests"] == ["Dragon Slayer II", "Song of the Elves"]


def test_the_grandmaster_list_is_the_wikis():
    assert set(sect.GRANDMASTER_QUESTS) == {
        "While Guthix Sleeps", "Monkey Madness II", "Dragon Slayer II",
        "Song of the Elves", "Desert Treasure II - The Fallen Empire",
        "The Blood Moon Rises"}


class TestQuestList:
    """Suggestion #258: every quest by name, not only how many."""

    def test_each_quest_is_named_with_its_state(self):
        out = _run(states=[(1, "0:2,30:1,32:0")])
        assert out[1]["list"] == [
            {"id": 0, "name": "Animal Magnetism", "state": "finished"},
            {"id": 30, "name": "Doric's Quest", "state": "in_progress"},
            {"id": 32, "name": "Dragon Slayer II", "state": "not_started"},
        ]

    def test_the_counts_are_counts_of_the_list(self):
        out = _run(states=[(1, "0:2,30:1,32:0,1:2")])
        assert (out[1]["finished"], out[1]["in_progress"], out[1]["not_started"]) == (2, 1, 1)

    def test_a_retired_id_is_in_neither_the_list_nor_the_counts(self):
        # Id 21 was renumbered; its old row would count that quest twice.
        out = _run(states=[(1, "0:2,21:2")])
        assert [q["id"] for q in out[1]["list"]] == [0]
        assert out[1]["finished"] == 1

    def test_a_quest_newer_than_the_name_file_is_kept_unnamed_and_last(self):
        out = _run(states=[(1, "99999:2,0:0")])
        assert out[1]["list"][-1] == {"id": 99999, "name": None, "state": "finished"}
        assert out[1]["finished"] == 1

    def test_the_order_is_the_games_ignoring_a_leading_article(self):
        # "The Ascent of Arceuus" (3) sits between "Another Slice" (1) and
        # "Bear Your Soul" (5), as in the in-game list.
        out = _run(states=[(1, "5:2,3:2,1:2")])
        assert [q["id"] for q in out[1]["list"]] == [1, 3, 5]

    def test_a_player_who_never_synced_has_an_empty_list(self):
        out = _run(best=[(7, 120, 343, datetime(2026, 9, 1))], ids=(7,))
        assert out[7]["list"] == []

    def test_players_do_not_share_a_list(self):
        out = _run(states=[(1, "0:2"), (2, "0:0")], ids=(1, 2))
        assert out[1]["list"][0]["state"] == "finished"
        assert out[2]["list"][0]["state"] == "not_started"


def test_the_name_file_is_complete_enough_to_trust():
    from utils import quests

    names = quests.quest_names()
    assert len(names) >= 220
    assert names[81] == "A Kingdom Divided"
    assert "Dream Mentor" in names.values()
    assert not (quests.retired_quest_ids() & set(names))
