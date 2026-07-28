"""Unit tests for the buy-in ↔ roster invariant (web71a).

Buy-ins are collected at sign-up, before any draft, so ``EventBuyin.team_id``
is NULL until a player is placed. ``services/event_buyins.py`` owns the rule
that keeps a live buy-in pointed at its payer's *current* placement, and
``services/event_signup.py`` calls it from every pool operation — the two
together are what make "record the payment now, draft later" work without a
placeholder team.

The module is loaded by file path under the conftest's ``services`` stub (the
``test_event_leadership`` idiom); its db imports are function-local, so a
recording fake session is enough to pin the SQL shape.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest


def _load(rel_path: str, name: str):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        *rel_path.split("/"),
    )
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


eb = _load("services/event_buyins.py", "_event_buyins_under_test")
# event_signup imports services.event_buyins at module level; the conftest has
# already registered the real thing under that name, so this loads clean.
es = _load("services/event_signup.py", "_event_signup_under_test")


# --------------------------------------------------------------------------- #
# Recording fakes
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, session, result):
        self._s = session
        self._r = result

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def with_for_update(self, *a, **k):
        return self

    def all(self):
        return list(self._r)

    def first(self):
        return self._r[0] if self._r else None

    def update(self, values, **k):
        self._s.updates.append(values)
        return len(self._r)

    def delete(self, *a, **k):
        return len(self._r)


class _S:
    """Scripted session that also records every bulk ``update()`` payload."""

    def __init__(self, *batches):
        self._batches = list(batches)
        self.updates = []
        self.added = []
        self.queries = 0

    def query(self, *a, **k):
        self.queries += 1
        assert self._batches, "unexpected extra query"
        return _Q(self, self._batches.pop(0))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        pass

    def flush(self):
        pass


def _team_ids(session):
    """The team_id each recorded UPDATE set, in call order."""
    from db.models import EventBuyin

    return [values[EventBuyin.team_id] for values in session.updates]


# --------------------------------------------------------------------------- #
# sync_buyin_teams
# --------------------------------------------------------------------------- #
class TestSyncBuyinTeams:
    def test_no_placements_issues_no_query(self):
        s = _S()  # any query would raise — an empty batch must short-circuit
        assert eb.sync_buyin_teams(s, 1, {}) == 0
        assert s.queries == 0

    def test_one_update_per_destination_team(self):
        # Five players landing on two teams => two UPDATEs, not five.
        s = _S([1, 2, 3], [4, 5])
        moved = eb.sync_buyin_teams(s, 1, {10: 7, 11: 7, 12: 7, 13: 8, 14: 8})
        assert moved == 5
        assert s.queries == 2
        assert _team_ids(s) == [7, 8]

    def test_unassign_sets_null(self):
        s = _S([1])
        assert eb.sync_buyin_team(s, 1, 10, None) == 1
        assert _team_ids(s) == [None]

    def test_players_without_a_buyin_are_a_no_op(self):
        s = _S([])  # the UPDATE matches nothing
        assert eb.sync_buyin_team(s, 1, 99, 4) == 0


class TestReleaseTeamBuyins:
    def test_returns_rows_to_the_unassigned_bucket(self):
        s = _S([1, 2])
        assert eb.release_team_buyins(s, 1, 4) == 2
        assert _team_ids(s) == [None]


# --------------------------------------------------------------------------- #
# The pool operations that carry a pre-draft buy-in onto a team
# --------------------------------------------------------------------------- #
def _ev(**kw):
    base = dict(id=1, mode="standard", formation_mode="signup_pool")
    base.update(kw)
    return SimpleNamespace(**base)


class TestAssignFromPool:
    def test_assigning_carries_the_buyin_onto_the_team(self):
        # signed-up check, team lookup, _place's membership sweep, then the
        # carry-over UPDATE.
        s = _S([(1,)], [SimpleNamespace(id=4, event_id=1, group_id=None)], [], [1])
        es.assign_from_pool(s, _ev(), player_id=10, team_id=4)
        assert _team_ids(s) == [4]

    def test_not_in_the_pool_refuses_before_touching_the_ledger(self):
        s = _S([])   # no sign-up row
        with pytest.raises(es.SignupError) as exc:
            es.assign_from_pool(s, _ev(), player_id=10, team_id=4)
        assert exc.value.status == 404
        assert s.updates == []


class TestUnassignFromPool:
    def test_back_to_the_pool_clears_the_team(self):
        s = _S([(1,)], [], [1])   # signed-up check, memberships, UPDATE
        es.unassign_from_pool(s, _ev(), player_id=10)
        assert _team_ids(s) == [None]


class TestRandomizePool:
    def test_shuffle_repoints_every_buyin_in_one_pass(self):
        signups = [SimpleNamespace(player_id=p, group_id=None) for p in (10, 11, 12, 13)]
        teams = [SimpleNamespace(id=7, group_id=None), SimpleNamespace(id=8, group_id=None)]
        # signups, teams, then _place's per-player membership sweep (4), then
        # one carry-over UPDATE per destination team.
        s = _S(signups, teams, [], [], [], [], [1, 2], [3, 4])
        out = es.randomize_pool(s, _ev())
        assert out == {"assigned": 4, "unassigned": 0}
        # Both teams were dealt players, and each got exactly one UPDATE.
        assert sorted(_team_ids(s)) == [7, 8]

    def test_players_with_no_eligible_team_are_not_repointed(self):
        # clan_vs_clan: the pool's clan has no team, so nobody is placed and
        # no carry-over UPDATE is issued (an empty placements map).
        signups = [SimpleNamespace(player_id=10, group_id=99)]
        s = _S(signups, [SimpleNamespace(id=7, group_id=42)])
        out = es.randomize_pool(s, _ev(mode="clan_vs_clan"))
        assert out == {"assigned": 0, "unassigned": 1}
        assert s.updates == []


class TestRemoveSignup:
    def test_withdrawal_unassigns_the_buyin_but_keeps_the_row(self):
        s = _S([], [], [1])   # signup delete, membership sweep, UPDATE
        es.remove_signup(s, _ev(), player_id=10)
        # team_id cleared — the GP stays in the pot, it just stops crediting a
        # team the player is no longer on.
        assert _team_ids(s) == [None]
