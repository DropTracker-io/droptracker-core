"""Doom of Mokhaiotl delve levels (utils/doom_delve.py) and the Hall of Fame
deepest-delve board.

The chat lines behind this are in the module docstring. The cases that matter:
an "8+" name must reach the shared 8+ row, not the boss row (it used to, which
put a single level-9 split at the top of every Doom PB board); only a level
completion may raise the record; and every boss that is not Doom must render
exactly as it did before the board existed.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.hof_layout import (
    DEFAULT_LAYOUT,
    HofEntry,
    Row,
    Scope,
    needed_boards,
    render_entry,
    validate_layout,
)
from utils import doom_delve
from utils.doom_delve import (
    DEEP_NPC_ID,
    DOOM_NPC_ID,
    completed_level,
    format_level,
    resolve_level_name,
)


class TestNames:
    @pytest.mark.parametrize("name,expected", [
        ("Doom of Mokhaiotl (Level:3)", (14710, "Doom of Mokhaiotl (Level 3)")),
        ("Doom of Mokhaiotl (Level: 1)", (14708, "Doom of Mokhaiotl (Level 1)")),
        ("Doom of Mokhaiotl (Level:8)", (14715, "Doom of Mokhaiotl (Level 8)")),
        ("Doom of Mokhaiotl (Level:8+)", (DEEP_NPC_ID, "Doom of Mokhaiotl (Level 8+)")),
        ("Doom of Mokhaiotl (Level 8+)", (DEEP_NPC_ID, "Doom of Mokhaiotl (Level 8+)")),
    ])
    def test_level_names(self, name, expected):
        assert resolve_level_name(name) == expected

    @pytest.mark.parametrize("name", [
        "Doom of Mokhaiotl",
        "Doom of Mokhaiotl (Level:0)",
        # A level past 8 always arrives as 8+; a bare 9 would be id 14716,
        # which is the 8+ row, so it must not be mapped by arithmetic.
        "Doom of Mokhaiotl (Level:9)",
        "Zulrah (Level:3)",
        "",
    ])
    def test_not_a_level_name(self, name):
        assert resolve_level_name(name) is None


class TestCompletedLevel:
    def test_levels_one_to_eight_are_exact(self):
        assert completed_level(14708, None) == (1, True)
        assert completed_level(14715, "") == (8, True)

    def test_deep_board_takes_the_reported_level(self):
        assert completed_level(DEEP_NPC_ID, "14") == (14, True)
        assert completed_level(DEEP_NPC_ID, 9) == (9, True)

    @pytest.mark.parametrize("reported", [None, "", "abc", 8, 3, 100000])
    def test_deep_board_without_a_usable_level_is_nine_plus(self, reported):
        assert completed_level(DEEP_NPC_ID, reported) == (9, False)

    @pytest.mark.parametrize("npc_id", [DOOM_NPC_ID, None, 2042, 14717])
    def test_nothing_else_counts(self, npc_id):
        assert completed_level(npc_id, "12") is None

    def test_format(self):
        assert format_level(12, True) == "12"
        assert format_level(9, False) == "9+"


def test_record_completed_is_one_upsert_that_updates_the_level_last():
    calls = []
    session = SimpleNamespace(execute=lambda stmt, params: calls.append((str(stmt), params)))
    doom_delve.record_completed(session, 42, 11, True)
    sql, params = calls[0]
    assert "ON DUPLICATE KEY UPDATE" in sql
    # MySQL applies the assignments in order; the comparisons above it must
    # still see the stored level.
    assert sql.rindex("deepest_level = GREATEST") > sql.index("achieved_at = IF")
    assert params["player_id"] == 42 and params["level"] == 11 and params["exact"] == 1


# ── Hall of Fame ────────────────────────────────────────────────────────────

COMMON = {
    "{site_url}": "https://www.droptracker.io",
    "{pbs_url}": "https://www.droptracker.io/personal-bests",
    "{directory_url}": "",
    "{coins_emoji}": "",
    "{month_name}": "September",
}


def _entry(delve_rows):
    top = delve_rows[0] if delve_rows else None
    scope = Scope(
        tokens={
            "{boss_name}": "Doom of Mokhaiotl",
            "{boss_link}": "[Doom of Mokhaiotl](https://x/doom)",
            "{boss_url}": "https://x/doom",
            "{boss_image_url}": "https://x/doom.png",
            "{boss_emoji}": "",
            "{mode_name}": "",
            "{total_pbs}": "3",
            "{fastest_time}": "11:56.00",
            "{fastest_team_size}": "Solo",
            "{fastest_player}": "[P1](u1)",
            "{deepest_delve_player}": top.player if top else "",
            "{deepest_delve}": top.value if top else "",
        },
        boards={"delve": delve_rows},
        pb_brackets=[("Solo", [Row("[P1](u1)", "P1", "11:56.00")])],
    )
    return HofEntry(scope=scope, modes=[scope])


def _texts(payload):
    out = []
    for c in payload["components"][0]["components"]:
        if c["type"] == 10:
            out.append(c["content"])
        elif c["type"] == 9:
            out.append(c["components"][0]["content"])
    return "\n".join(out)


def test_default_layout_shows_the_deepest_delves_on_doom():
    delves = [Row("[P2](u2)", "P2", "14"), Row("[P1](u1)", "P1", "9+")]
    payload, used_default = render_entry(None, _entry(delves), COMMON, 5)
    body = _texts(payload)
    assert used_default
    assert "Deepest delve: level `14` by [P2](u2)" in body
    assert "Deepest Delves" in body
    assert "🥇 [P2](u2) - level `14`" in body and "🥈 [P1](u1) - level `9+`" in body


def test_a_boss_without_delves_renders_as_before():
    payload, _ = render_entry(None, _entry([]), COMMON, 5)
    body = _texts(payload)
    assert "elve" not in body
    kinds = [c["type"] for c in payload["components"][0]["components"]]
    # The empty board leaves no doubled divider behind.
    assert all(not (a == b == 14) for a, b in zip(kinds, kinds[1:]))


def test_default_layout_is_valid_and_asks_for_the_board():
    ok, errors = validate_layout(DEFAULT_LAYOUT)
    assert ok, errors
    assert needed_boards(DEFAULT_LAYOUT, 4)["delve"] == 4
    assert needed_boards({"blocks": [{"type": "text", "content": "{deepest_delve}"}]}, 5)["delve"] == 1


def _real_hof_data():
    # ``services`` is stubbed by conftest; load the real collector from its
    # file (its db.models imports resolve to the stubs, which is all the gate
    # below needs).
    path = Path(__file__).resolve().parents[2] / "services" / "hof_data.py"
    spec = importlib.util.spec_from_file_location("_real_hof_data", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_real_hof_data"] = module
    spec.loader.exec_module(module)
    return module


def test_collector_skips_the_query_for_other_bosses():
    HofDataCollector = _real_hof_data().HofDataCollector

    def boom(*_a, **_k):
        raise AssertionError("queried player_deepest_delve for a non-Doom boss")

    collector = HofDataCollector(SimpleNamespace(query=boom), 5, [1, 2], display_name=str)
    assert collector.delve_rows([2042], 5) == []
    # A per-level Doom message is not the boss's own message either.
    assert collector.delve_rows([14710], 5) == []
