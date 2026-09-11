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
        out = _run(states=[(1, 2, 180)], best=[(1, 343, 343, datetime(2026, 9, 1))])
        assert out[1]["quest_points"] == 343
        assert out[1]["total_quest_points"] == 343
        assert out[1]["quest_cape"] is True
        assert out[1]["finished"] == 180

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
        out = _run(states=[(1, 2, 5)])
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
