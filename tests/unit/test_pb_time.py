"""Tick snapping for PB times — the inverse of the game's non-precise display."""

import pytest

from utils.pb_time import TICK_MS, is_tick_aligned, snap_to_tick


def test_every_tick_aligned_time_is_a_fixed_point():
    """The safety property the whole design rests on: snapping cannot disturb a
    time a precise client could have produced, so it is safe to apply to every
    row without knowing which client sent it."""
    for ticks in range(1, 20_000):
        ms = ticks * TICK_MS
        assert snap_to_tick(ms) == ms, f"{ms} moved"


def test_whole_seconds_snap_to_the_nearest_tick():
    # A whole second sits 200ms from a tick on one side or the other, and
    # multiples of 3000 sit exactly on one.
    assert snap_to_tick(1000) == 1200
    assert snap_to_tick(2000) == 1800
    assert snap_to_tick(3000) == 3000
    assert snap_to_tick(4000) == 4200
    assert snap_to_tick(5000) == 4800
    assert snap_to_tick(6000) == 6000
    # A real raid time: 20:02 -> 20:01.8
    assert snap_to_tick(1_202_000) == 1_201_800


def test_multiples_of_three_seconds_never_move():
    """These are the values a precise and a non-precise client agree on, so
    moving one would corrupt a genuine time."""
    for k in range(1, 2000):
        assert snap_to_tick(k * 3000) == k * 3000


def test_nothing_moves_by_more_than_two_hundred_ms():
    for ms in range(1000, 4_000_000, 1000):
        assert abs(snap_to_tick(ms) - ms) <= 200


def test_snapping_is_idempotent():
    for ms in (1000, 2000, 4000, 5000, 1_202_000, 999_000):
        once = snap_to_tick(ms)
        assert snap_to_tick(once) == once


def test_snapping_is_monotonic():
    """Ranking depends on order, so the snap must never reorder two times."""
    previous = 0
    for ms in range(0, 200_000, 137):
        current = snap_to_tick(ms)
        assert current >= previous
        previous = current


def test_zero_is_the_unset_sentinel_and_survives():
    assert snap_to_tick(0) == 0
    assert snap_to_tick(-5) == -5
    assert snap_to_tick(None) == 0
    assert snap_to_tick("nonsense") == 0


def test_a_positive_time_never_collapses_into_the_sentinel():
    """The game cannot produce a sub-tick duration, but junk data can, and
    rounding one to zero would silently turn it into "no time recorded"."""
    for ms in range(1, TICK_MS):
        assert snap_to_tick(ms) == TICK_MS


def test_string_and_float_inputs_coerce():
    assert snap_to_tick("2000") == 1800
    assert snap_to_tick(2000.0) == 1800


@pytest.mark.parametrize(
    "ms, aligned",
    [(600, True), (1200, True), (3000, True), (1000, False), (2000, False), (0, False), (-600, False), (None, False)],
)
def test_is_tick_aligned(ms, aligned):
    assert is_tick_aligned(ms) is aligned
