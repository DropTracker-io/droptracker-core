"""Unit tests for services/split_credits.py — the per-player GP deltas that
make the Discord lootboard's leaderboard agree with the website's group
leaderboard.

Origin: the board builds its leaderboard panel from each player's *global*
`player:{id}:{partition}:total_loot`, which is split-unaware. A split receiver
therefore showed the drop's full value on the board while the site showed only
their share, and participants' credits were missing entirely.
"""

import importlib.util
import os

import pytest

# The suite stubs the whole `services` package as a MagicMock (tests/conftest.py),
# so import the real module straight from its source file — the same approach
# tests/unit/test_check_drop_failopen.py uses for osrs_api. split_credits only
# touches the stdlib at import time; its db models are imported inside the
# function, which these tests never reach (the session is stubbed).
_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services",
    "split_credits.py",
)
_spec = importlib.util.spec_from_file_location("_real_split_credits_for_test", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
group_split_deltas = _mod.group_split_deltas


class _Drop:
    def __init__(self, drop_id, player_id, value, quantity=1, partition=202608, hidden=False):
        self.drop_id = drop_id
        self.player_id = player_id
        self.value = value
        self.quantity = quantity
        self.partition = partition
        self.hidden = hidden


class _Split:
    def __init__(self, drop_id, player_id, split_value, group_id=296):
        self.drop_id = drop_id
        self.player_id = player_id
        self.split_value = split_value
        self.group_id = group_id


class _Session:
    """Minimal stand-in for the query chain group_split_deltas uses."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, *args):
        return self

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


# The incident: a 777,800,000 shadow split 4 ways (194,450,000 each) between
# the receiver, two tracked participants, and one untracked player.
SHADOW = _Drop(180517207, player_id=5755880, value=777_800_000)
SPLITS = [
    (_Split(180517207, 5758194, 194_450_000), SHADOW),
    (_Split(180517207, 5033, 194_450_000), SHADOW),
]
MEMBERS = [5755880, 5758194, 5033]


def test_participants_are_credited_a_share_each():
    deltas = group_split_deltas(_Session(SPLITS), 296, MEMBERS, 202608)
    assert deltas[5758194] == 194_450_000
    assert deltas[5033] == 194_450_000


def test_receiver_is_reduced_to_their_own_share():
    deltas = group_split_deltas(_Session(SPLITS), 296, MEMBERS, 202608)
    assert deltas[5755880] == -(777_800_000 - 194_450_000)
    assert deltas[5755880] == -583_350_000


def test_receiver_is_adjusted_once_not_per_participant():
    """Two participant rows on one drop must not reduce the receiver twice."""
    deltas = group_split_deltas(_Session(SPLITS), 296, MEMBERS, 202608)
    assert deltas[5755880] == -583_350_000


def test_untracked_share_is_not_redistributed():
    """The group keeps 3/4 of the drop; the 4th share goes to nobody."""
    deltas = group_split_deltas(_Session(SPLITS), 296, MEMBERS, 202608)
    net = sum(deltas.values())
    # Receiver loses 583,350,000, participants gain 388,900,000 between them:
    # the group's board total drops by exactly the uncreditable share.
    assert net == -194_450_000


def test_hidden_drops_are_skipped():
    """Hidden drops are already out of the board's totals — applying their
    split too would double-subtract."""
    hidden = _Drop(1, player_id=5755880, value=777_800_000, hidden=True)
    rows = [(_Split(1, 5758194, 194_450_000), hidden)]
    assert group_split_deltas(_Session(rows), 296, MEMBERS, 202608) == {}


def test_players_outside_the_group_board_are_ignored():
    """A participant who isn't on this board's member list gets no delta, but
    the receiver's reduction still applies."""
    deltas = group_split_deltas(_Session(SPLITS), 296, [5755880], 202608)
    assert 5758194 not in deltas and 5033 not in deltas
    assert deltas[5755880] == -583_350_000


def test_receiver_not_on_the_board_still_credits_participants():
    deltas = group_split_deltas(_Session(SPLITS), 296, [5758194, 5033], 202608)
    assert deltas == {5758194: 194_450_000, 5033: 194_450_000}


def test_no_rows_means_no_deltas():
    assert group_split_deltas(_Session([]), 296, MEMBERS, 202608) == {}


@pytest.mark.parametrize("members", [[], None])
def test_empty_member_list_is_safe(members):
    assert group_split_deltas(_Session(SPLITS), 296, members, 202608) == {}


def test_none_member_ids_are_skipped_not_fatal():
    """Real data: group 296 carries a NULL player_id association row. int(None)
    used to blow up the whole adjustment."""
    deltas = group_split_deltas(_Session(SPLITS), 296, [None, 5758194], 202608)
    assert deltas == {5758194: 194_450_000}


@pytest.mark.parametrize("partition", ["not-a-partition", None])
def test_unparseable_partition_is_safe(partition):
    assert group_split_deltas(_Session(SPLITS), 296, MEMBERS, partition) == {}


def test_daily_partition_string_is_normalised():
    """Daily partitions arrive as '2026-08-02'."""
    drop = _Drop(9, player_id=5755880, value=1000, partition=20260802)
    rows = [(_Split(9, 5758194, 250), drop)]
    deltas = group_split_deltas(_Session(rows), 296, MEMBERS, "2026-08-02")
    assert deltas[5758194] == 250


def test_quantity_is_included_in_the_drop_value():
    """A stack's value is value*quantity — the receiver's reduction must use
    the full stack, not the unit price."""
    drop = _Drop(7, player_id=5755880, value=1_000_000, quantity=10)
    rows = [(_Split(7, 5758194, 2_500_000), drop)]
    deltas = group_split_deltas(_Session(rows), 296, MEMBERS, 202608)
    assert deltas[5755880] == -(10_000_000 - 2_500_000)


def test_one_way_split_produces_no_receiver_reduction():
    drop = _Drop(8, player_id=5755880, value=1000)
    rows = [(_Split(8, 5758194, 1000), drop)]
    deltas = group_split_deltas(_Session(rows), 296, MEMBERS, 202608)
    assert 5755880 not in deltas
