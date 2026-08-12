"""Unit tests for the raid-party evidence gate — the server-side check that a
raid submission's participant list is consistent with the party size the game
itself reported.

Origin: a solo Chambers of Xeric (2026-08-11, drop 187463658) whose plugin-side
roster fell back to RuneLite party membership and credited a clanmate who had
logged off 57 minutes earlier. The plugin now proves solo and sends its
evidence (``raid_party_size``, ``roster_source``) with every raid submission;
this gate makes the server enforce it, so a roster bug or stale client can
never again share credit from a solo raid.

The processor needs a DB + Redis, so these cover the pure decision logic:
``_normalize_raid_party_size`` (what the pipeline will trust) and
``_apply_raid_party_evidence`` (what the evidence does to the payload).
"""

import pytest

from data.submissions.drop import (
    MAX_SPLIT_SIZE,
    _apply_raid_party_evidence,
    _normalize_raid_party_size,
)


# ── _normalize_raid_party_size ───────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [(1, 1), ("1", 1), (2, 2), ("5", 5), (" 8 ", 8)])
def test_accepts_plausible_sizes_including_solo(raw, expected):
    """Unlike split_size, 1 is valid here — it is the proof of a solo raid."""
    assert _normalize_raid_party_size(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "abc", [], {}, 0, -3, True, False])
def test_rejects_junk(raw):
    assert _normalize_raid_party_size(raw) is None


def test_rejects_absurd_sizes():
    assert _normalize_raid_party_size(MAX_SPLIT_SIZE + 1) is None
    assert _normalize_raid_party_size(MAX_SPLIT_SIZE) == MAX_SPLIT_SIZE


# ── _apply_raid_party_evidence ───────────────────────────────────────────────

def test_the_incident_case():
    """Solo CoX claiming a participant: the claim is impossible and is removed."""
    players, split_size, note, stripped = _apply_raid_party_evidence(
        ["Mike Lamitch"], None, 1, "solo"
    )
    assert players is None
    assert split_size is None
    assert stripped is True
    assert "Mike Lamitch" in note


def test_party_size_one_alone_is_enough():
    """The gate must not depend on roster_source agreeing."""
    players, split_size, note, stripped = _apply_raid_party_evidence(
        ["Someone"], 3, 1, "proximity-fallback"
    )
    assert players is None and split_size is None and stripped is True


def test_solo_roster_source_alone_is_enough():
    players, split_size, note, stripped = _apply_raid_party_evidence(
        ["Someone"], None, None, "solo"
    )
    assert players is None and split_size is None and stripped is True


def test_proven_solo_with_no_claims_changes_nothing_visible():
    players, split_size, note, stripped = _apply_raid_party_evidence(
        None, None, 1, "solo"
    )
    assert players is None and split_size is None
    assert note is None and stripped is False


def test_team_party_size_floors_the_divisor():
    """3-man raid, 1 tracked participant: the divisor must be 3, not 2, so the
    untracked raider's share is credited to nobody instead of inflating cuts."""
    players, split_size, note, stripped = _apply_raid_party_evidence(
        ["Teammate"], None, 3, "authoritative"
    )
    assert players == ["Teammate"]
    assert split_size == 3
    assert stripped is False


def test_party_size_only_ever_raises_the_divisor():
    players, split_size, _, _ = _apply_raid_party_evidence(
        ["a", "b", "c", "d"], 6, 3, "authoritative"
    )
    assert split_size == 6  # the manual/explicit size already exceeded it


def test_team_evidence_without_participants_stays_dormant():
    """No one to credit -> no split will run; don't wake the split block."""
    players, split_size, note, stripped = _apply_raid_party_evidence(
        None, None, 3, "authoritative"
    )
    assert players is None and split_size is None
    assert note is None and stripped is False


def test_no_evidence_changes_nothing():
    """Pre-5.4.3 clients and non-raid submissions carry no evidence."""
    players, split_size, note, stripped = _apply_raid_party_evidence(
        ["a", "b"], 4, None, None
    )
    assert players == ["a", "b"]
    assert split_size == 4
    assert note is None and stripped is False


def test_unknown_roster_source_is_not_solo():
    players, _, _, stripped = _apply_raid_party_evidence(
        ["a"], None, None, "authoritative"
    )
    assert players == ["a"] and stripped is False


@pytest.mark.parametrize("source", ["solo", " SOLO ", "Solo"])
def test_roster_source_matching_is_case_and_space_insensitive(source):
    players, _, _, stripped = _apply_raid_party_evidence(["a"], None, None, source)
    assert players is None and stripped is True
