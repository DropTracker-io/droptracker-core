"""Merge policy for the PB team-size bracket repair (suggestion #153).

The script rewrites production PB rows and deletes the duplicates it absorbs,
so the two functions that decide *which* row's values survive are pinned here.
"""

from datetime import datetime

import pytest

from scripts.merge_pb_team_size_brackets import _latest_row, _record_row, _repair_class


def row(id, personal_best, date_added, kill_time=0):
    return {
        "id": id,
        "personal_best": personal_best,
        "kill_time": kill_time,
        "date_added": date_added,
    }


D1 = datetime(2026, 1, 1)
D2 = datetime(2026, 6, 1)
D3 = datetime(2026, 8, 1)


def test_record_row_is_the_fastest_time():
    rows = [row(1, 900_000, D1), row(2, 700_000, D2), row(3, 800_000, D3)]
    assert _record_row(rows)["id"] == 2


def test_record_row_never_picks_an_unset_time():
    """A zero or NULL personal_best is "no time recorded", not an instant one —
    picking it would wipe the board's actual record."""
    rows = [row(1, 0, D1), row(2, 900_000, D2), row(3, None, D3)]
    assert _record_row(rows)["id"] == 2

    # All unset: still returns a row rather than raising, so the merge can run.
    assert _record_row([row(1, 0, D1), row(2, None, D2)])["id"] == 1


def test_record_row_breaks_ties_on_the_earlier_claim():
    rows = [row(2, 700_000, D3), row(1, 700_000, D1)]
    assert _record_row(rows)["id"] == 1


def test_latest_row_is_the_most_recent_kill():
    rows = [row(1, 900_000, D1), row(3, 700_000, D3), row(2, 800_000, D2)]
    assert _latest_row(rows)["id"] == 3


def test_latest_row_tolerates_missing_dates():
    rows = [row(1, 900_000, None), row(2, 800_000, D1)]
    assert _latest_row(rows)["id"] == 2
    # With no dates at all, the highest id is the newest row.
    assert _latest_row([row(1, 900_000, None), row(2, 800_000, None)])["id"] == 2


@pytest.mark.parametrize(
    "npc_name, raw, expected",
    [
        # Truncated bucket labels — suggestion #153.
        ("Chambers of Xeric", "16", "bracket"),
        ("Chambers of Xeric Challenge Mode", "24", "bracket"),
        ("The Nightmare", "6", "bracket"),
        # Contaminated rosters above the game's party ceiling — suggestion #140.
        ("Theatre of Blood", "9", "cap"),
        ("Tombs of Amascut: Expert Mode", "15", "cap"),
    ],
)
def test_repair_class_separates_the_two_bugs(npc_name, raw, expected):
    assert _repair_class(npc_name, raw) == expected
