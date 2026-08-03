"""Unit tests for split-size handling — the divisor used when a drop is split.

Origin: a 4-way Tumeken's shadow split (2026-08-02) where only 3 of the 4
players were tracked. The divisor collapsed to the people the pipeline could
resolve, so a 4-way split paid out thirds and over-credited the group by a
whole share. `split_size` carries the real party size so uncreditable shares
still shrink everyone's cut.

The processor itself needs a DB + Redis, so these cover the pure decision
logic: `_normalize_split_size` (what the pipeline will trust) and the divisor
rule it feeds.
"""

import pytest

from data.submissions.drop import MAX_SPLIT_SIZE, _normalize_split_size


# ── _normalize_split_size ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [(4, 4), ("4", 4), (" 8 ", 8), (2, 2)])
def test_accepts_a_plausible_party_size(raw, expected):
    assert _normalize_split_size(raw, []) == expected


@pytest.mark.parametrize("raw", [None, "", "abc", [], {}, 0, 1, -3, True, False])
def test_rejects_junk_and_meaningless_sizes(raw):
    """A 1-way 'split' is not a split; True/False are not party sizes."""
    assert _normalize_split_size(raw, []) is None


def test_rejects_absurd_party_size():
    assert _normalize_split_size(MAX_SPLIT_SIZE + 1, []) is None
    assert _normalize_split_size(MAX_SPLIT_SIZE, []) == MAX_SPLIT_SIZE


def test_size_below_the_named_players_is_ignored():
    """A size that contradicts the names in the same payload must not be used
    to shrink the divisor — that would over-credit everyone."""
    assert _normalize_split_size(2, ["a", "b", "c"]) is None


def test_size_matching_or_exceeding_named_players_is_kept():
    assert _normalize_split_size(4, ["a", "b", "c"]) == 4   # exactly the named party
    assert _normalize_split_size(6, ["a", "b", "c"]) == 6   # 2 more, untracked


def test_the_incident_case():
    """3 tracked players named + 1 unnamed = a 4-way split."""
    assert _normalize_split_size(4, ["stuffmyvoid", "puzzled life"]) == 4


# ── the divisor rule ─────────────────────────────────────────────────────────
# Mirrors _award_split_gp_credits: total_count = max(split_size, resolved).

def _divisor(split_size, resolved_participants):
    return max(int(split_size or 0), 1 + resolved_participants)


def test_untracked_member_still_shrinks_everyones_share():
    """The regression this whole change exists for."""
    drop_value = 777_800_000
    # 2 resolved participants + receiver = 3 resolved, but a 4-way split.
    assert drop_value // _divisor(4, 2) == 194_450_000   # correct quarter
    assert drop_value // _divisor(None, 2) == 259_266_666  # the old, wrong third


def test_split_size_can_only_raise_the_divisor():
    """A stale or under-counted size must never inflate anyone's credit."""
    assert _divisor(2, 4) == 5      # 4 resolved + receiver wins over a stale 2
    assert _divisor(None, 4) == 5


def test_all_tracked_matches_the_old_behaviour():
    assert _divisor(4, 3) == 4
    assert _divisor(None, 3) == 4


def test_uncreditable_share_goes_to_nobody_not_redistributed():
    """3 tracked of 4 => the group is credited 3/4 of the drop, not all of it."""
    drop_value = 777_800_000
    share = drop_value // _divisor(4, 2)
    credited_total = share * 3          # receiver + 2 participants
    assert credited_total == 583_350_000
    assert credited_total < drop_value
    assert drop_value - credited_total == share   # exactly one share unassigned
