"""The "current total" an award notification prints, when a clan combines RSNs.

``_check_and_award_points`` reports each paid member's running total, which
notifications render as ``{group_points_receiver_total}`` / "total N". On a
clan with ``points_combine_accounts`` on, the boards show a Discord user's
in-group RSNs as one number, so the notification has to quote that same number
-- decided by ``db/point_standings.counted_player_ids``, not re-derived here.

What is pinned: whose ledger rows the total is summed over, that the setting
is read once per pass, and that nothing about this lookup can cost the award.
Reuses the boost/split harness, which stubs everything except the pipeline.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import data.submissions.point_awards as pa

# The real module, by its sys.modules name (conftest registers it). ``import
# db.point_standings as x`` resolves through the ``db`` package attribute, and
# conftest stubs ``db`` with a MagicMock -- patching THAT would leave the
# engine's lazy import untouched and let the assertions below pass or fail for
# the wrong reason.
standings = sys.modules["db.point_standings"]

from tests.unit.test_point_awards_boost_split import (
    GROUP_ID,
    NOW,
    RECEIVER_ID,
    FakeSession,
    _patch_pipeline,
)


class _Column:
    """Stands in for a PlayerPoints column; remembers what it was filtered by."""

    def __init__(self):
        self.in_calls = []

    def in_(self, ids):
        self.in_calls.append(sorted(int(i) for i in ids))
        return ("in", tuple(ids))

    def __eq__(self, other):  # noqa: D105 - filter expression stand-in
        return ("eq", other)

    __hash__ = object.__hash__


class _LedgerSession(FakeSession):
    """FakeSession whose amount query returns a ledger, so totals are non-zero."""

    def all(self):
        return [(40,), (2,)]

    def first(self):
        return None


def _fake_points(monkeypatch):
    player_id = _Column()
    monkeypatch.setattr(
        pa, "PlayerPoints",
        SimpleNamespace(amount="amount", group_id=_Column(), player_id=player_id),
    )
    return player_id


async def _run(monkeypatch, *, participants=(), sharing=False):
    _patch_pipeline(monkeypatch, sharing=sharing)
    # modify_for_event reads boosts through the same session; none are active.
    async def no_boost(reason, group_id, player_id, points, **kw):
        return points

    monkeypatch.setattr(pa, "modify_for_event", no_boost)
    return await pa._check_and_award_points(
        "drop", GROUP_ID, RECEIVER_ID, 5_000_000,
        players_included=json.dumps(list(participants)),
        item_id=13652, npc_id=None, quantity=1, entry_id=555,
        submission_guid="test-guid", submission_timestamp=NOW,
        external_session=_LedgerSession([]),
    )


async def test_per_rsn_clan_totals_the_player_alone(monkeypatch):
    column = _fake_points(monkeypatch)
    monkeypatch.setattr(standings, "combine_enabled", lambda s, gid: False)
    result = await _run(monkeypatch)
    assert column.in_calls == [[RECEIVER_ID]]
    assert result["receiver_points_awarded"] == 5
    assert result["receiver_current_points"] == 42


async def test_combining_clan_totals_the_users_in_group_accounts(monkeypatch):
    column = _fake_points(monkeypatch)
    asked = []

    def counted(s, gid, pid, combine=None):
        asked.append((gid, pid, combine))
        return [RECEIVER_ID, 77]

    monkeypatch.setattr(standings, "combine_enabled", lambda s, gid: True)
    monkeypatch.setattr(standings, "counted_player_ids", counted)
    result = await _run(monkeypatch)
    assert asked == [(GROUP_ID, RECEIVER_ID, True)]
    assert column.in_calls == [[RECEIVER_ID, 77]]
    assert result["awarded_members"][0]["current_points"] == 42


async def test_setting_is_read_once_however_many_members_are_paid(monkeypatch):
    _fake_points(monkeypatch)
    reads = []

    def combine(s, gid):
        reads.append(gid)
        return True

    monkeypatch.setattr(standings, "combine_enabled", combine)
    monkeypatch.setattr(standings, "counted_player_ids", lambda s, gid, pid, combine=None: [pid])
    result = await _run(monkeypatch, participants=["Partner One", "Partner Two"], sharing=True)
    assert len(result["awarded_members"]) == 3
    assert reads == [GROUP_ID]


async def test_a_failing_lookup_never_costs_the_award(monkeypatch):
    column = _fake_points(monkeypatch)

    def boom(s, gid):
        raise RuntimeError("group_configurations unavailable")

    monkeypatch.setattr(standings, "combine_enabled", boom)
    result = await _run(monkeypatch)
    assert result["receiver_points_awarded"] == 5
    assert column.in_calls == [[RECEIVER_ID]]


async def test_an_empty_answer_falls_back_to_the_player(monkeypatch):
    column = _fake_points(monkeypatch)
    monkeypatch.setattr(standings, "combine_enabled", lambda s, gid: True)
    monkeypatch.setattr(standings, "counted_player_ids", lambda s, gid, pid, combine=None: [])
    await _run(monkeypatch)
    assert column.in_calls == [[RECEIVER_ID]]


async def test_a_truthy_non_bool_setting_is_not_on(monkeypatch):
    # Under a mocked session combine_enabled can hand back a MagicMock; only a
    # real True may switch the combined path on.
    column = _fake_points(monkeypatch)
    monkeypatch.setattr(standings, "combine_enabled", lambda s, gid: "1")
    await _run(monkeypatch)
    assert column.in_calls == [[RECEIVER_ID]]
