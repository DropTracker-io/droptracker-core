"""Unit tests for the scoring-side helpers of services/event_engine.py:

- ``_current_leader(strict=True)`` — a shared top score has NO leader, so a
  team that merely ties can never fire a lead-change embed (the id-order
  tiebreak used to crown them).
- ``_award_contribution_points`` — task points split across contributors by
  net quantity share (floats), mutating each contributor's ``points_share``.
- ``_row_advances_progress`` — the record gate that drops ledger rows whose
  matched item is already satisfied.

Loaded the same way as test_event_engine_matcher: straight from the file so
the conftest sys.modules stubs (db.models is a MagicMock) never interfere —
the fake sessions below stand in for the ORM.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_engine.py",
)
_spec = importlib.util.spec_from_file_location("_event_engine_scoring_ut", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_scoring_ut"] = engine
_spec.loader.exec_module(engine)


class _Q:
    """Chainable query fake returning pre-sorted rows."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def limit(self, n):
        return _Q(self._rows[:n])

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def delete(self, synchronize_session=False):
        n = len(self._rows)
        self._rows.clear()
        return n


class _Session:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.added = []

    def query(self, *a):
        return _Q(self.rows)

    def add(self, obj):
        self.added.append(obj)


def _team(tid, score):
    return SimpleNamespace(id=tid, score=score)


class TestCurrentLeaderStrict:
    def test_tie_has_no_strict_leader(self):
        # The reported bug: 20-20 after a catch-up completion crowned the
        # lower-id team and fired "New leader".
        s = _Session([_team(1, 20), _team(2, 20)])
        assert engine._current_leader(s, 99, strict=True) is None

    def test_tie_still_reports_loose_leader(self):
        s = _Session([_team(1, 20), _team(2, 20)])
        assert engine._current_leader(s, 99) == (1, 20)

    def test_distinct_scores_report_top(self):
        s = _Session([_team(2, 25), _team(1, 20)])
        assert engine._current_leader(s, 99, strict=True) == (2, 25)

    def test_single_team_is_strict_leader(self):
        s = _Session([_team(7, 0)])
        assert engine._current_leader(s, 99, strict=True) == (7, 0)

    def test_no_teams(self):
        s = _Session([])
        assert engine._current_leader(s, 99, strict=True) is None


class TestAwardContributionPoints:
    EVENT = {"id": 5}
    TASK = {"id": 9, "points": 5}

    def test_split_by_net_share(self):
        contributors = [
            {"player_id": 1, "player_name": "A", "quantity": 3},
            {"player_id": 2, "player_name": "B", "quantity": 1},
        ]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 5)
        assert contributors[0]["points_share"] == 3.75
        assert contributors[1]["points_share"] == 1.25
        assert len(s.added) == 2

    def test_even_split_halves(self):
        contributors = [
            {"player_id": 1, "quantity": 2},
            {"player_id": 2, "quantity": 2},
        ]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 5)
        assert [c["points_share"] for c in contributors] == [2.5, 2.5]

    def test_zero_points_awards_nothing(self):
        contributors = [{"player_id": 1, "quantity": 2}]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 0)
        assert s.added == [] and "points_share" not in contributors[0]

    def test_zero_total_quantity_awards_nothing(self):
        contributors = [{"player_id": 1, "quantity": 0}]
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, contributors, 5)
        assert s.added == []

    def test_no_contributors(self):
        s = _Session()
        engine._award_contribution_points(s, self.EVENT, self.TASK, 4, [], 5)
        assert s.added == []


class _Row:
    """EventCompletion stand-in for the rollup queries (needs .id for the
    include-dedupe check)."""

    def __init__(self, matched_target=None, quantity=1, source_type="drop", rid=1):
        self.id = rid
        self.matched_target = matched_target
        self.quantity = quantity
        self.source_type = source_type


class TestRowAdvancesProgress:
    ALL_OF = {
        "id": 3, "type": "item_collection", "target_value": 2,
        "config": {"kind": "all_of", "items": [
            {"item_name": "Bones"}, {"item_name": "Coins"},
        ]},
    }

    def test_duplicate_item_is_dead_weight(self):
        s = _Session([_Row("Bones", rid=10)])
        candidate = _Row("Bones", rid=None)
        assert engine._row_advances_progress(s, self.ALL_OF, 4, candidate) is False

    def test_new_item_advances(self):
        s = _Session([_Row("Bones", rid=10)])
        candidate = _Row("Coins", rid=None)
        assert engine._row_advances_progress(s, self.ALL_OF, 4, candidate) is True

    def test_plain_count_tasks_always_advance(self):
        task = {"id": 3, "type": "item_collection", "target_value": 5, "config": {}}
        s = _Session([])
        assert engine._row_advances_progress(s, task, 4, _Row("Bones", rid=None)) is True
