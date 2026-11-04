"""Partial points for unfinished "points from a list" tasks (web156a).

Pure maths (``partial_due``) plus the sync against the real event models on
in-memory SQLite: the payout row, the team score, idempotent re-runs, late
corrections, the setting turned off, and an admin's revoke standing.

Loaded straight from the file: ``services`` is a stub package in the unit
suite, and the module only needs ``utils`` + ``db.models``.
"""
import importlib
import importlib.util
import json
import os
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_partial_points.py",
)
_spec = importlib.util.spec_from_file_location("_event_partial_points_ut", _PATH)
pp = importlib.util.module_from_spec(_spec)
sys.modules["_event_partial_points_ut"] = pp
_spec.loader.exec_module(pp)

POINTS_CFG = json.dumps({"kind": "point_collection", "items": [
    {"item_name": "Scythe of vitur (uncharged)", "points": 6},
    {"item_name": "Holy ornament kit", "points": 3},
]})


class TestPartialDue:
    def test_share_of_goal(self):
        # The reported case: a 10-point task with a 10-point goal, the team
        # got a scythe (6) and nothing else.
        assert pp.partial_due(10, 10, 6, False) == 6

    def test_scaled_and_rounded_down(self):
        assert pp.partial_due(20, 10, 6, False) == 12
        assert pp.partial_due(5, 10, 7, False) == 3  # 3.5 -> 3
        assert pp.partial_due(10, 7, 0.7, False) == 1  # float noise stays 1

    def test_finished_or_empty_owes_nothing(self):
        assert pp.partial_due(10, 10, 10, True) == 0
        assert pp.partial_due(10, 10, 0, False) == 0
        assert pp.partial_due(0, 10, 6, False) == 0

    def test_never_more_than_the_task(self):
        # Progress at the goal without the completed flag (should not
        # happen) still can't pay more than a completion would.
        assert pp.partial_due(10, 10, 25, False) == 10

    def test_repeatable_pays_the_unfinished_lap(self):
        # Unlimited laps of a 10-point goal: 2 laps paid, 4 of the third.
        assert pp.partial_due(10, 10, 24, False, cap=None) == 4
        # Capped at 2 and both done: nothing more to earn.
        assert pp.partial_due(10, 10, 24, True, cap=2) == 0


class TestIsPartialTask:
    def test_only_points_lists(self):
        assert pp.is_partial_task("item_collection", POINTS_CFG)
        assert not pp.is_partial_task("item_collection", {"kind": "any_of"})
        assert not pp.is_partial_task("kc_target", POINTS_CFG)
        assert not pp.is_partial_task("item_collection", None)


@pytest.fixture
def db():
    m = importlib.import_module("db.models")
    eng = sa.create_engine("sqlite://")
    m.Event.metadata.create_all(eng, tables=[
        m.Event.__table__, m.EventTask.__table__, m.EventTeam.__table__,
        m.EventProgress.__table__, m.EventCompletion.__table__])
    s = sessionmaker(bind=eng)()
    ev = m.Event(name="Bingo", status="past", kind="standard", partial_points=True)
    s.add(ev)
    s.flush()
    task = m.EventTask(event_id=ev.id, type="item_collection", label="ToB uniques",
                       target_value=10, points=10, config=POINTS_CFG)
    other = m.EventTask(event_id=ev.id, type="item_collection", label="Any pet",
                        target_value=1, points=5, config=json.dumps({"kind": "any_of"}))
    a = m.EventTeam(event_id=ev.id, name="A", score=20)
    b = m.EventTeam(event_id=ev.id, name="B", score=10)
    s.add_all([task, other, a, b])
    s.flush()
    s.add_all([
        m.EventProgress(event_id=ev.id, task_id=task.id, team_id=a.id, progress=6),
        m.EventProgress(event_id=ev.id, task_id=task.id, team_id=b.id,
                        progress=10, completed=True),
        m.EventProgress(event_id=ev.id, task_id=other.id, team_id=a.id, progress=0),
    ])
    s.commit()
    return s, m, ev, task, a, b


def _partial_rows(s, m):
    return s.query(m.EventCompletion).filter(
        m.EventCompletion.submission_guid == pp.PARTIAL_GUID).all()


class TestSync:
    def test_pays_unfinished_team_once(self, db):
        s, m, ev, task, a, b = db
        assert pp.sync_partial_points(s, ev) == {a.id: 6}
        assert a.score == 26 and b.score == 10  # B finished: paid on completion
        (row,) = _partial_rows(s, m)
        assert (row.team_id, row.task_id, row.quantity, row.source_type) == (
            a.id, task.id, 6, "bonus")
        # Idempotent: a second pass changes nothing.
        assert pp.sync_partial_points(s, ev) == {}
        assert a.score == 26 and len(_partial_rows(s, m)) == 1

    def test_late_progress_reprices_in_place(self, db):
        s, m, ev, task, a, b = db
        pp.sync_partial_points(s, ev)
        prog = s.query(m.EventProgress).filter_by(task_id=task.id, team_id=a.id).one()
        prog.progress = 3  # an admin revoked the scythe, a 3-pointer remains
        assert pp.resync_after_correction(s, ev, task.id, a.id) == {a.id: -3}
        assert a.score == 23
        assert [r.quantity for r in _partial_rows(s, m)] == [3]
        # Then the team is credited the rest late and finishes the task: the
        # completion pays the task points, the partial row goes away.
        prog.progress, prog.completed = 10, True
        pp.resync_after_correction(s, ev, task.id, a.id)
        assert a.score == 20 and _partial_rows(s, m) == []

    def test_setting_off_takes_it_back(self, db):
        s, m, ev, task, a, b = db
        pp.sync_partial_points(s, ev)
        ev.partial_points = False
        assert pp.sync_partial_points(s, ev) == {a.id: -6}
        assert a.score == 20 and _partial_rows(s, m) == []

    def test_live_event_pays_nothing(self, db):
        s, m, ev, task, a, b = db
        ev.status = "active"
        assert pp.sync_partial_points(s, ev) == {}
        assert pp.resync_after_correction(s, ev, task.id, a.id) == {}
        assert a.score == 20

    def test_other_event_kinds_pay_nothing(self, db):
        s, m, ev, task, a, b = db
        ev.kind = "board_game"
        assert pp.sync_partial_points(s, ev) == {}

    def test_admin_revoke_stands(self, db):
        s, m, ev, task, a, b = db
        pp.sync_partial_points(s, ev)
        (row,) = _partial_rows(s, m)
        row.status = "revoked"
        a.score = 20  # what revoke_ledger_row's bonus path does
        # Revoking the partial row itself never triggers a re-pay...
        assert pp.resync_after_correction(s, ev, task.id, a.id, completion=row) == {}
        # ...and nor does a later full pass.
        assert pp.sync_partial_points(s, ev) == {}
        assert a.score == 20


def test_bingo_unwind_leaves_partial_rows_alone(db):
    """A revoke on a bingo board re-checks which line bonuses still hold and
    revokes the rest. A partial-points row is a bonus row too, and must not be
    mistaken for a line the team lost."""
    s, m, ev, task, a, b = db
    m.EventBingoCell.metadata.create_all(s.get_bind(), tables=[
        m.EventBingoCell.__table__, m.EventBingoCompletion.__table__])
    pp.sync_partial_points(s, ev)
    stale = m.EventCompletion(event_id=ev.id, task_id=task.id, team_id=a.id,
                              status="auto", quantity=5, source_type="bonus",
                              note="line:r0")
    s.add(stale)
    a.score += 5
    s.flush()

    engine_path = os.path.join(os.path.dirname(_PATH), "event_engine.py")
    spec = importlib.util.spec_from_file_location("_event_engine_partial_ut", engine_path)
    engine = importlib.util.module_from_spec(spec)
    sys.modules["_event_engine_partial_ut"] = engine
    spec.loader.exec_module(engine)
    assert engine.PARTIAL_POINTS_GUID == pp.PARTIAL_GUID

    revoked = engine._unwind_bonuses(s, {"id": ev.id, "board_size": 5}, a.id)
    assert revoked == ["line:r0"]
    assert stale.status == "revoked"
    (row,) = _partial_rows(s, m)
    assert row.status == "auto" and row.quantity == 6
    assert a.score == 26
