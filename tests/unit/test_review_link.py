"""One review decision settles both queues (services/review_link.py, 2026-11-23).

A manual drop can be pending in a clan's manual-review queue and an event's
review queue at once. A decision in either now settles the other, for events
hosted by that same clan; only pending rows are touched.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_review_link_under_test",
    os.path.join(_HERE, "..", "..", "services", "review_link.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None


class _Session:
    """query(EventCompletion) -> completions; query(Event) -> the event whose
    id matches the completion being looked at (one event per test)."""

    def __init__(self, completions=(), event=None):
        self.completions = list(completions)
        self.event = event
        self.added, self.commits, self.rollbacks = [], 0, 0

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "EventCompletion":
            return _Q(self.completions)
        if name == "Event":
            return _Q([self.event] if self.event else [])
        return _Q([])

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _ReviewError(Exception):
    pass


@pytest.fixture()
def fakes(monkeypatch):
    calls = {"event": [], "clan": []}

    event_review = SimpleNamespace(
        ReviewError=_ReviewError,
        confirm=lambda s, ev, cid, actor, audit_extra=None, settle_clan=True:
            calls["event"].append(("confirm", ev.id, cid, settle_clan)),
        reject=lambda s, ev, cid, actor, note=None, audit_extra=None, settle_clan=True:
            calls["event"].append(("reject", ev.id, cid, settle_clan)),
    )

    class _ModErr(Exception):
        pass

    rows = {}

    def _approve(s, drop_id, group_id, reviewer_user_id=None):
        calls["clan"].append(("approve", drop_id, group_id))
        return {"status": "approved"}

    def _reject(s, drop_id, group_id, reviewer_user_id=None):
        calls["clan"].append(("reject", drop_id, group_id))
        return {"status": "rejected"}

    drop_moderation = SimpleNamespace(
        ModerationError=_ModErr,
        _load_moderation_row=lambda s, d, g: rows.get((d, g)),
        approve_drop_for_group=_approve,
        reject_drop_for_group=_reject,
    )
    services = sys.modules["services"]
    monkeypatch.setattr(services, "event_review", event_review, raising=False)
    monkeypatch.setattr(services, "drop_moderation", drop_moderation, raising=False)
    monkeypatch.setitem(sys.modules, "services.event_review", event_review)
    monkeypatch.setitem(sys.modules, "services.drop_moderation", drop_moderation)
    return calls, rows


def _comp(cid=1, event_id=175, drop_id=900, status="pending"):
    return SimpleNamespace(id=cid, event_id=event_id, source_type="drop",
                           source_id=drop_id, status=status)


class TestClanDecisionSettlesEvent:
    def test_approve_confirms_the_host_clans_event_completion(self, fakes):
        calls, _ = fakes
        s = _Session([_comp()], SimpleNamespace(id=175, group_id=190, status="active"))
        assert rl.settle_event_for_clan_decision(s, 900, 190, "approve", 7) == [1]
        # settle_clan=False: the clan side is already decided, no ping-pong.
        assert calls["event"] == [("confirm", 175, 1, False)]

    def test_reject_rejects_it(self, fakes):
        calls, _ = fakes
        s = _Session([_comp()], SimpleNamespace(id=175, group_id=190, status="active"))
        assert rl.settle_event_for_clan_decision(s, 900, 190, "reject", 7) == [1]
        assert calls["event"] == [("reject", 175, 1, False)]

    def test_another_clans_event_keeps_its_own_decision(self, fakes):
        calls, _ = fakes
        s = _Session([_comp()], SimpleNamespace(id=175, group_id=555, status="active"))
        assert rl.settle_event_for_clan_decision(s, 900, 190, "approve", 7) == []
        assert calls["event"] == []

    def test_ended_event_is_not_confirmed_after_the_fact(self, fakes):
        calls, _ = fakes
        s = _Session([_comp()], SimpleNamespace(id=175, group_id=190, status="past"))
        assert rl.settle_event_for_clan_decision(s, 900, 190, "approve", 7) == []
        assert calls["event"] == []

    def test_undo_is_not_mirrored(self, fakes):
        calls, _ = fakes
        s = _Session([_comp()], SimpleNamespace(id=175, group_id=190, status="active"))
        assert rl.settle_event_for_clan_decision(s, 900, 190, "undo", 7) == []
        assert calls["event"] == []

    def test_a_concurrent_decision_is_not_an_error(self, fakes, monkeypatch):
        calls, _ = fakes

        def _raise(*a, **k):
            raise _ReviewError("already decided")

        monkeypatch.setattr(sys.modules["services.event_review"], "confirm", _raise)
        s = _Session([_comp()], SimpleNamespace(id=175, group_id=190, status="active"))
        assert rl.settle_event_for_clan_decision(s, 900, 190, "approve", 7) == []
        assert s.rollbacks == 1


class TestEventDecisionSettlesClan:
    EV = SimpleNamespace(id=175, group_id=190)

    def test_confirm_approves_the_pending_clan_row(self, fakes):
        calls, rows = fakes
        rows[(900, 190)] = SimpleNamespace(status="pending")
        s = _Session()
        assert rl.settle_clan_for_event_decision(s, self.EV, _comp(), "approve", 7) is True
        assert calls["clan"] == [("approve", 900, 190)]
        assert s.commits == 1 and len(s.added) == 1  # audited

    def test_reject_rejects_it(self, fakes):
        calls, rows = fakes
        rows[(900, 190)] = SimpleNamespace(status="pending")
        assert rl.settle_clan_for_event_decision(
            _Session(), self.EV, _comp(), "reject", 7) is True
        assert calls["clan"] == [("reject", 900, 190)]

    def test_already_decided_clan_row_is_left_alone(self, fakes):
        calls, rows = fakes
        rows[(900, 190)] = SimpleNamespace(status="approved")
        assert rl.settle_clan_for_event_decision(
            _Session(), self.EV, _comp(), "reject", 7) is False
        assert calls["clan"] == []

    def test_no_clan_row_no_effect(self, fakes):
        calls, _ = fakes
        assert rl.settle_clan_for_event_decision(
            _Session(), self.EV, _comp(), "approve", 7) is False
        assert calls["clan"] == []

    def test_non_drop_completion_is_ignored(self, fakes):
        calls, rows = fakes
        rows[(900, 190)] = SimpleNamespace(status="pending")
        comp = _comp()
        comp.source_type = "clog"
        assert rl.settle_clan_for_event_decision(
            _Session(), self.EV, comp, "approve", 7) is False
        assert calls["clan"] == []
