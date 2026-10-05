"""Tick snapping for PB times — the inverse of the game's non-precise display."""

import pytest

from utils.pb_time import TICK_MS, board_sort_key, is_tick_aligned, snap_to_tick, time_is_precise


def test_every_tick_aligned_time_is_a_fixed_point():
    """The safety property the whole design rests on: snapping cannot disturb a
    time a precise client could have produced, so it is safe to apply to every
    row without knowing which client sent it."""
    for ticks in range(1, 20_000):
        ms = ticks * TICK_MS
        assert snap_to_tick(ms) == ms, f"{ms} moved"


def test_whole_seconds_snap_up_to_the_slowest_consistent_tick():
    # The game rounds a duration to the nearest second, so a display of S
    # seconds means the truth was in [1000S-500, 1000S+500). We record the
    # slowest tick in that window, never the fastest.
    assert snap_to_tick(1000) == 1200
    assert snap_to_tick(2000) == 2400
    assert snap_to_tick(3000) == 3000
    assert snap_to_tick(4000) == 4200
    assert snap_to_tick(5000) == 5400
    assert snap_to_tick(6000) == 6000
    # A real raid time: 20:02 -> 20:02.4
    assert snap_to_tick(1_202_000) == 1_202_400


def test_a_non_precise_time_is_never_credited_as_faster_than_the_truth():
    """Ticket #182: two clan-mates on one raid, and the one with precise timing
    off came out ahead. For every true tick duration, what a non-precise client
    displays must never snap below it."""
    for ticks in range(1, 20_000):
        true_ms = ticks * TICK_MS
        # What the game prints with precise timing off: nearest whole second.
        displayed = ((true_ms + 500) // 1000) * 1000
        assert snap_to_tick(displayed) >= true_ms, f"{true_ms} recorded as faster"


def test_the_pessimism_is_bounded_to_a_single_tick():
    """The flip side of never crediting an unearned record: a non-precise time
    may be recorded one tick slow, but never more."""
    for ticks in range(1, 20_000):
        true_ms = ticks * TICK_MS
        displayed = ((true_ms + 500) // 1000) * 1000
        assert snap_to_tick(displayed) - true_ms <= TICK_MS


def test_multiples_of_three_seconds_never_move():
    """These are the values a precise and a non-precise client agree on, so
    moving one would corrupt a genuine time."""
    for k in range(1, 2000):
        assert snap_to_tick(k * 3000) == k * 3000


def test_nothing_moves_by_more_than_four_hundred_ms():
    for ms in range(1000, 4_000_000, 1000):
        assert 0 <= snap_to_tick(ms) - ms <= 400


def test_snapping_never_moves_a_time_downwards():
    for ms in range(1, 200_000, 7):
        assert snap_to_tick(ms) >= ms


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
    assert snap_to_tick("2000") == 2400
    assert snap_to_tick(2000.0) == 2400


@pytest.mark.parametrize(
    "ms, aligned",
    [(600, True), (1200, True), (3000, True), (1000, False), (2000, False), (0, False), (-600, False), (None, False)],
)
def test_is_tick_aligned(ms, aligned):
    assert is_tick_aligned(ms) is aligned


class TestTimeIsPrecise:
    def test_hundredths_mean_precise(self):
        assert time_is_precise("28:14.40") is True
        assert time_is_precise("28:15.00") is True
        assert time_is_precise("1:02:03.60") is True

    def test_whole_seconds_mean_not_precise(self):
        assert time_is_precise("28:14") is False
        assert time_is_precise("00:43") is False

    def test_a_sentence_full_stop_is_not_a_fraction(self):
        # Clan broadcasts end the time with the sentence: "personal best: 28:14."
        assert time_is_precise("28:14.") is False

    def test_unknown_when_the_value_cannot_say(self):
        assert time_is_precise(1_694_000) is None
        assert time_is_precise("N/A") is None
        assert time_is_precise(None) is None
        assert time_is_precise("") is None

    def test_the_plugin_flag_wins(self):
        assert time_is_precise("28:14", "true") is True
        assert time_is_precise("28:14.40", False) is False
        assert time_is_precise(1_694_000, True) is True

    def test_a_blank_flag_falls_back_to_the_format(self):
        assert time_is_precise("28:14", "") is False
        assert time_is_precise("28:14.40", None) is True


class TestBoardSortKey:
    def test_time_first(self):
        assert board_sort_key(60_000, False, 9) < board_sort_key(60_600, True, 1)

    def test_measured_before_approximate_or_unknown_on_a_tie(self):
        assert board_sort_key(60_600, True, 9) < board_sort_key(60_600, False, 1)
        assert board_sort_key(60_600, True, 9) < board_sort_key(60_600, None, 1)

    def test_then_the_earlier_date(self):
        assert board_sort_key(60_600, False, 1) < board_sort_key(60_600, False, 2)
