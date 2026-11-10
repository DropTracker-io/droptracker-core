"""Ticket 521: a raid room's time filed as the whole raid.

Theatre of Blood and Tombs of Amascut print a duration for every room as well
as the raid, and a client that took the room line for the raid put a 0:42.6
solo Theatre of Blood on top of a clan's Hall of Fame. pb_floors refuses those
times on every intake path, the same way it refuses Colosseum waves.
"""
from __future__ import annotations

import pytest

from utils.pb_floors import below_floor, floor_ms


@pytest.mark.parametrize("name", [
    "Theatre of Blood",
    "Theatre of Blood: Hard Mode",
    "Theatre of Blood: Entry Mode",
    "Tombs of Amascut",
    "Tombs of Amascut: Expert Mode",
    "Tombs of Amascut: Entry Mode",
])
def test_every_raid_mode_has_a_floor(name):
    assert floor_ms(name) is not None


@pytest.mark.parametrize("name, room_ms", [
    ("Theatre of Blood", 42_600),               # the ticket's solo "PB"
    ("Theatre of Blood", 348_600),              # a Verzik room as a trio raid
    ("Theatre of Blood: Hard Mode", 258_000),
    ("Theatre of Blood: Entry Mode", 170_400),
    ("Tombs of Amascut: Expert Mode", 213_000),
])
def test_room_times_are_refused(name, room_ms):
    assert below_floor(name, room_ms)


@pytest.mark.parametrize("name, run_ms", [
    ("Theatre of Blood", 11 * 60_000 + 21_000),             # 5-man record
    ("Theatre of Blood: Hard Mode", 14 * 60_000),
    ("Theatre of Blood: Entry Mode", 12 * 60_000),
    ("Tombs of Amascut", 10 * 60_000),
    ("Tombs of Amascut: Expert Mode", 13 * 60_000),
    ("Tombs of Amascut: Entry Mode", 8 * 60_000),
])
def test_fastest_real_runs_are_kept(name, run_ms):
    assert not below_floor(name, run_ms)


def test_the_floor_follows_the_name_not_its_spelling():
    assert floor_ms("theatre of blood: hard mode") == floor_ms("Theatre of Blood: Hard Mode")
