"""Unit tests for the point-award split arithmetic in
``data/submissions/point_awards.py``.

Regression coverage for the "point awards always round up" bug: equal_split
previously used ceil division, so a 10-point drop shared among 3 participants
awarded 4 to everyone (12 distributed for a 10-point item). The fix floor-divides
and hands the indivisible remainder to the receiving player, so the distributed
total never exceeds the item's point value.
"""

import pytest

from data.submissions.point_awards import _compute_split_shares, _floor_div


def _distributed_total(per_target, receiver, target_count):
    """Total points handed out: one share per target + the receiver's share."""
    return per_target * target_count + receiver


class TestEqualSplitShares:
    def test_reported_bug_case_10_across_3(self):
        # Receiver + 2 other participants = 3 people sharing a 10-point item.
        per_target, receiver = _compute_split_shares(10, 2, "equal_split")
        assert per_target == 3
        assert receiver == 4  # floor share (3) + remainder (1)
        assert _distributed_total(per_target, receiver, 2) == 10  # never 12

    def test_even_division_has_no_remainder(self):
        per_target, receiver = _compute_split_shares(9, 2, "equal_split")
        assert per_target == 3
        assert receiver == 3
        assert _distributed_total(per_target, receiver, 2) == 9

    def test_award_smaller_than_participant_count(self):
        # 2 points across 3 participants: floor share is 0, receiver keeps the 2.
        per_target, receiver = _compute_split_shares(2, 2, "equal_split")
        assert per_target == 0
        assert receiver == 2
        assert _distributed_total(per_target, receiver, 2) == 2

    def test_zero_award(self):
        assert _compute_split_shares(0, 3, "equal_split") == (0, 0)

    def test_negative_award_clamped_to_zero(self):
        assert _compute_split_shares(-5, 3, "equal_split") == (0, 0)

    def test_equal_alias_behaves_like_equal_split(self):
        assert _compute_split_shares(10, 2, "equal") == _compute_split_shares(10, 2, "equal_split")

    @pytest.mark.parametrize("point_award", range(0, 51))
    @pytest.mark.parametrize("target_count", range(1, 8))
    def test_invariant_never_over_awards(self, point_award, target_count):
        per_target, receiver = _compute_split_shares(point_award, target_count, "equal_split")
        total_participants = target_count + 1
        # Distributed total exactly equals the award — never more, never less.
        assert _distributed_total(per_target, receiver, target_count) == point_award
        # Each target gets the floor share; the receiver absorbs the remainder.
        assert per_target == _floor_div(point_award, total_participants)
        assert receiver >= per_target
        assert receiver - per_target < total_participants  # remainder is bounded


class TestAwardAllShares:
    def test_everyone_gets_full_award(self):
        per_target, receiver = _compute_split_shares(10, 2, "award_all")
        assert per_target == 10
        assert receiver == 10

    def test_zero_award(self):
        assert _compute_split_shares(0, 2, "award_all") == (0, 0)


class TestNoSplitFallbacks:
    def test_unknown_method_targets_get_nothing_receiver_keeps_all(self):
        per_target, receiver = _compute_split_shares(10, 2, "something_else")
        assert per_target == 0
        assert receiver == 10

    def test_no_targets_receiver_keeps_full_award(self):
        assert _compute_split_shares(10, 0, "equal_split") == (0, 10)
        assert _compute_split_shares(10, 0, "award_all") == (0, 10)

    def test_negative_target_count_treated_as_no_split(self):
        assert _compute_split_shares(10, -3, "equal_split") == (0, 10)


class TestFloorDivGuard:
    def test_floor_div_does_not_round_up(self):
        assert _floor_div(10, 3) == 3
        assert _floor_div(11, 3) == 3
        assert _floor_div(12, 3) == 4

    def test_floor_div_guards(self):
        assert _floor_div(10, 0) == 0
        assert _floor_div(0, 3) == 0
        assert _floor_div(-1, 3) == 0
