"""Unit tests for the website submit form's split validation
(``web_api/routes/submissions.py::_parse_split``).

Mirrors tests/unit/test_manual_discord.py's split cases so a split behaves the
same whether it came from the website or Discord. The rule that matters: the
divisor and the credit list are different questions — a share taken by someone
DropTracker can't credit still shrinks everyone else's cut.
"""

import pytest

from web_api.common import ProblemException
from web_api.routes.submissions import MAX_SPLIT_SIZE, _parse_split


def _split(body, receiver="WI Beer Guy"):
    return _parse_split(body, receiver)


# ── the no-split case ────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [{}, {"split_players": []}, {"split_players": ["", "  "]}])
def test_no_split_returns_nothing(body):
    assert _split(body) == ([], None)


# ── names ────────────────────────────────────────────────────────────────────

def test_names_are_kept_in_order():
    others, size = _split({"split_players": ["stuffmyvoid", "puzzled life"]})
    assert others == ["stuffmyvoid", "puzzled life"]
    assert size == 3  # 2 others + the receiver


def test_receiver_is_filtered_out_of_their_own_split():
    """The form pre-fills the submitting account; the pipeline counts the
    receiver separately, so leaving them in would shrink everyone's share."""
    others, size = _split({"split_players": ["WI Beer Guy", "puzzled life"]})
    assert others == ["puzzled life"]
    assert size == 2


def test_receiver_match_ignores_case_and_underscores():
    others, _ = _split({"split_players": ["wi_beer_guy", "puzzled life"]})
    assert others == ["puzzled life"]


def test_duplicates_collapse():
    others, size = _split({"split_players": ["a", "A", "a_b", "a b"]})
    assert others == ["a", "a_b"]
    assert size == 3


def test_non_list_rejected():
    with pytest.raises(ProblemException):
        _split({"split_players": "a,b"})


def test_oversized_name_rejected():
    with pytest.raises(ProblemException):
        _split({"split_players": ["a name far too long"]})


# ── explicit size: the point of the whole feature ────────────────────────────

def test_explicit_size_counts_an_untracked_member():
    """The incident case: 2 named + receiver + 1 unnamed = 4 ways."""
    others, size = _split({"split_players": ["stuffmyvoid", "puzzled life"], "split_size": 4})
    assert others == ["stuffmyvoid", "puzzled life"]
    assert size == 4


def test_size_alone_covers_an_all_untracked_split():
    assert _split({"split_size": 3}) == ([], 3)


def test_size_below_the_named_players_is_rejected():
    with pytest.raises(ProblemException):
        _split({"split_players": ["a", "b", "c"], "split_size": 2})


def test_size_equal_to_the_named_party_is_fine():
    _, size = _split({"split_players": ["a", "b", "c"], "split_size": 4})
    assert size == 4


@pytest.mark.parametrize("size", [1, 0, -4, MAX_SPLIT_SIZE + 1])
def test_absurd_size_rejected(size):
    with pytest.raises(ProblemException):
        _split({"split_size": size})


@pytest.mark.parametrize("size", ["", None])
def test_blank_size_falls_back_to_the_names(size):
    others, resolved = _split({"split_players": ["a", "b"], "split_size": size})
    assert (others, resolved) == (["a", "b"], 3)


def test_non_numeric_size_rejected():
    with pytest.raises(ProblemException):
        _split({"split_size": "four"})
