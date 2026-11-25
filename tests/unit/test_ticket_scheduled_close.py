"""Scheduled ticket close (web166a): ``/close hours:`` and the Keep open button.

* **The sweep closes only open tickets whose time has passed.**
* **A notice's button is tied to its own schedule.** Rescheduling makes an
  older notice's Keep open button stale, so it can't cancel the newer close.
* **Only the opener or ticket staff can keep a ticket open.**
* **Stored close times are whole seconds**, so the token in the button
  matches the row MySQL hands back.
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_ticket_system():
    with pytest.MonkeyPatch.context() as mp:
        for name in ("interactions.api", "interactions.api.events", "interactions.models", "commands"):
            mp.setitem(sys.modules, name, MagicMock())
        spec = importlib.util.spec_from_file_location(
            "_ticket_system_sched_ut", os.path.join(_ROOT, "services", "ticket_system.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


ts = _load_ticket_system()

NOW = datetime(2026, 11, 25, 12, 0, 0, 654321)
OPENER = "111"
STAFF = "222"


class _FakeSession:
    """first() answers in call order: the ticket, then any User lookups."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.commits = 0

    def query(self, *_):
        return self

    def filter(self, *_):
        return self

    def first(self):
        return self.answers.pop(0) if self.answers else None

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def session(monkeypatch):
    def _factory(*answers):
        fake = _FakeSession(*answers)
        monkeypatch.setattr(ts, "Session", lambda: fake)
        return fake

    return _factory


def _ticket(**overrides):
    fields = dict(ticket_id=7, status="open", created_by=5, scheduled_close_at=None,
                  scheduled_close_by=None, inactivity_warned_at=None, date_updated=NOW)
    fields.update(overrides)
    return SimpleNamespace(**fields)


# --- the sweep rule ---------------------------------------------------------
def test_due_once_the_time_passes():
    at = NOW - timedelta(seconds=1)
    assert ts._scheduled_close_due("open", at, NOW)
    assert not ts._scheduled_close_due("open", NOW + timedelta(minutes=1), NOW)


def test_not_due_without_a_schedule_or_when_not_open():
    assert not ts._scheduled_close_due("open", None, NOW)
    assert not ts._scheduled_close_due("closed", NOW - timedelta(hours=1), NOW)
    assert not ts._scheduled_close_due("close_requested", NOW - timedelta(hours=1), NOW)


def test_close_time_is_whole_seconds_and_hours_ahead():
    at = ts._scheduled_close_at(NOW, 12)
    assert at.microsecond == 0
    assert at == (NOW + timedelta(hours=12)).replace(microsecond=0)


# --- scheduling -------------------------------------------------------------
def test_schedule_stores_time_scheduler_and_drops_idle_warning(session):
    ticket = _ticket(inactivity_warned_at=NOW - timedelta(hours=2))
    s = session(ticket, SimpleNamespace(user_id=42), (OPENER,))
    outcome, ticket_id, close_at, opener = ts._apply_schedule_close(1, 12, STAFF, NOW)
    assert (outcome, ticket_id, opener) == ("scheduled", 7, OPENER)
    assert ticket.scheduled_close_at == close_at == ts._scheduled_close_at(NOW, 12)
    assert ticket.scheduled_close_by == 42
    assert ticket.inactivity_warned_at is None
    assert s.commits == 1


def test_scheduling_again_reports_a_reschedule(session):
    ticket = _ticket(scheduled_close_at=NOW + timedelta(hours=3))
    session(ticket, None, (OPENER,))
    outcome, _, close_at, _ = ts._apply_schedule_close(1, 24, STAFF, NOW)
    assert outcome == "rescheduled"
    assert ticket.scheduled_close_at == close_at
    assert ticket.scheduled_close_by is None  # scheduler has no DT account


def test_schedule_outside_a_ticket_or_on_a_closing_one(session):
    session(None)
    assert ts._apply_schedule_close(1, 12, STAFF, NOW)[0] == "not_ticket"
    s = session(_ticket(status="close_requested"))
    assert ts._apply_schedule_close(1, 12, STAFF, NOW)[0] == "not_open"
    assert s.commits == 0


# --- keep open --------------------------------------------------------------
def _scheduled():
    at = ts._scheduled_close_at(NOW, 12)
    return _ticket(scheduled_close_at=at, scheduled_close_by=42), ts._close_token(at)


def test_opener_can_keep_it_open(session):
    ticket, token = _scheduled()
    s = session(ticket, (OPENER,))
    assert ts._apply_keep_open(1, token, OPENER, False) == "cancelled"
    assert ticket.scheduled_close_at is None and ticket.scheduled_close_by is None
    assert s.commits == 1


def test_staff_can_keep_it_open_without_an_opener_lookup(session):
    ticket, token = _scheduled()
    session(ticket)
    assert ts._apply_keep_open(1, token, STAFF, True) == "cancelled"
    assert ticket.scheduled_close_at is None


def test_bystanders_cannot_keep_it_open(session):
    ticket, token = _scheduled()
    s = session(ticket, (OPENER,))
    assert ts._apply_keep_open(1, token, "999", False) == "forbidden"
    assert ticket.scheduled_close_at is not None
    assert s.commits == 0


def test_an_old_notice_cannot_cancel_a_newer_schedule(session):
    ticket, old_token = _scheduled()
    ticket.scheduled_close_at = ts._scheduled_close_at(NOW, 24)
    session(ticket)
    assert ts._apply_keep_open(1, old_token, STAFF, True) == "stale"
    assert ticket.scheduled_close_at is not None


def test_keep_open_after_it_was_already_cancelled(session):
    _, token = _scheduled()
    session(_ticket())
    assert ts._apply_keep_open(1, token, OPENER, False) == "stale"


# --- notice -----------------------------------------------------------------
def test_notice_button_carries_the_schedule_token():
    at = ts._scheduled_close_at(NOW, 12)
    content, _components, _allowed = ts._scheduled_close_notice(at, "Staffer", OPENER, False)
    token = ts._close_token(at)
    assert f"<t:{token}:R>" in content
    assert f"<@{OPENER}>" in content
    assert "scheduled to close" in content
    assert "changed" in ts._scheduled_close_notice(at, "Staffer", None, True)[0]


def test_custom_ids_do_not_trip_the_legacy_close_match():
    # on_component closes immediately on any id containing "close_ticket".
    assert "close_ticket" not in ts.SCHEDULE_CLOSE_CUSTOM_ID
    assert "close_ticket" not in ts.KEEP_OPEN_CUSTOM_ID_PREFIX + "1764072000"
