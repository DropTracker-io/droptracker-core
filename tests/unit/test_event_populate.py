"""Unit tests for the admin random-populate tool in services/event_signup.py.

Loaded directly from the file path (like test_event_signup_service) so the
conftest db/services stubs never interfere — the module's top-level imports are
stdlib-only by design (DB models are lazy-imported inside functions). We drive
the pure distribution planner and the no-DB guard branches with tiny fakes.
"""
import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_signup.py",
)
_spec = importlib.util.spec_from_file_location("_event_populate_under_test", _PATH)
sus = importlib.util.module_from_spec(_spec)
sys.modules["_event_populate_under_test"] = sus
_spec.loader.exec_module(sus)


def _ev(**kw):
    base = dict(id=1, mode="standard", group_id=5)
    base.update(kw)
    return SimpleNamespace(**base)


def _sizes(placements):
    out = {}
    for _, tid in placements:
        out[tid] = out.get(tid, 0) + 1
    return out


class TestPlanDistribution:
    """The pure placement planner: balanced, clan-aware, no DB."""

    def test_balances_evenly_across_teams(self):
        placements = sus._plan_distribution(
            {None: [1, 2, 3, 4]}, {None: [10, 11]}, {10: 0, 11: 0}
        )
        assert dict(_sizes(placements)) == {10: 2, 11: 2}
        # every selected player placed exactly once
        assert sorted(p for p, _ in placements) == [1, 2, 3, 4]

    def test_respects_existing_team_sizes(self):
        # Team 10 already has 2 members; both newcomers go to the emptier team.
        placements = sus._plan_distribution(
            {None: [1, 2]}, {None: [10, 11]}, {10: 2, 11: 0}
        )
        assert [t for _, t in placements] == [11, 11]

    def test_clan_buckets_stay_on_their_clan_teams(self):
        placements = sus._plan_distribution(
            {100: [1, 2], 200: [3]},
            {100: [10], 200: [20]},
            {10: 0, 20: 0},
        )
        assert sorted(placements) == [(1, 10), (2, 10), (3, 20)]

    def test_drops_players_whose_bucket_has_no_team(self):
        placements = sus._plan_distribution(
            {100: [1], 999: [2]}, {100: [10]}, {10: 0}
        )
        assert placements == [(1, 10)]


class _Col:
    """Stand-in for a SQLAlchemy column so filter/order_by expressions evaluate."""

    def __eq__(self, other):
        return True

    def asc(self):
        return self

    def in_(self, other):
        return True

    def isnot(self, other):
        return True


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


def _stub_models(monkeypatch):
    import types

    fake = types.ModuleType("db.models")
    for name in ("EventTeam", "EventTeamMember", "Player", "EventGroup"):
        setattr(fake, name, SimpleNamespace(
            event_id=_Col(), id=_Col(), player_id=_Col(), team_id=_Col(),
            date_updated=_Col(), group_id=_Col(), status=_Col(),
        ))
    fake.user_group_association = SimpleNamespace(
        c=SimpleNamespace(player_id=_Col(), group_id=_Col())
    )
    monkeypatch.setitem(sys.modules, "db.models", fake)
    return fake


class TestPopulateGuards:
    def test_invalid_source_rejected(self):
        with pytest.raises(sus.SignupError) as exc:
            sus.populate_random(None, _ev(), source="everyone")
        assert exc.value.status == 422

    def test_invalid_count_rejected(self):
        with pytest.raises(sus.SignupError) as exc:
            sus.populate_random(None, _ev(), source="group", count=0)
        assert exc.value.status == 422

    def test_no_teams_rejected(self, monkeypatch):
        _stub_models(monkeypatch)
        # The first query (teams) returns [] -> "No teams" before anything else.
        with pytest.raises(sus.SignupError) as exc:
            sus.populate_random(_FakeSession([]), _ev(), source="group")
        assert exc.value.status == 409
        assert exc.value.title == "No teams"

    def test_group_source_on_global_event_rejected(self, monkeypatch):
        _stub_models(monkeypatch)
        # One team exists, but a global event (group_id=None) has no linked group.
        team = SimpleNamespace(id=10, name="A", group_id=None)
        with pytest.raises(sus.SignupError) as exc:
            sus.populate_random(_FakeSession([team]), _ev(group_id=None), source="group")
        assert exc.value.status == 409
        assert exc.value.title == "No linked group"
