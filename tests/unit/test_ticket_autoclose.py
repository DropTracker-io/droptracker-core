"""Ticket auto-close exemption (web117a): ``/autoclose`` in a ticket channel.

* **The sweep's rule is one pure function.** ``_inactivity_decision`` takes the
  exemption as an input, so the 5-minute scan and the fresh re-check right
  before an auto-close agree: an exempt ticket is never warned and never closed.
* **Turning auto-close back on never closes straight away.** Exempting drops a
  pending warning, and re-enabling restarts the idle clock. Otherwise a warning
  left from before the exemption would be older than the 24h grace window and
  the next sweep would close the ticket with no fresh warning.
* **Only the roles that can close a ticket can exempt one.** Ticket helpers
  reply everywhere but can't close, so they can't keep a ticket open forever.

services/ticket_system.py is loaded from its file path (conftest stubs the
module and the Discord library); the extra stubs its imports need are scoped
to the load.
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
    # conftest's `interactions` is a flat MagicMock, not a package, and
    # `commands` would import every command module; stub both only for the load.
    with pytest.MonkeyPatch.context() as mp:
        for name in ("interactions.api", "interactions.api.events", "interactions.models", "commands"):
            mp.setitem(sys.modules, name, MagicMock())
        spec = importlib.util.spec_from_file_location(
            "_ticket_system_ut", os.path.join(_ROOT, "services", "ticket_system.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


ts = _load_ticket_system()

NOW = datetime(2026, 9, 21, 12, 0, 0)
ADMIN_ROLE = 1342871954885050379


def _ticket(**overrides):
    fields = dict(
        status="open",
        autoclose_exempt=False,
        inactivity_warned_at=None,
        date_updated=NOW - timedelta(days=9),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _decide(ticket, now):
    """The sweep's call, as process_ticket_inactivity makes it."""
    return ts._inactivity_decision(
        ticket.status, ticket.date_updated, ticket.inactivity_warned_at, now,
        exempt=ticket.autoclose_exempt,
    )


# --------------------------------------------------------------------------- #
# _inactivity_decision
# --------------------------------------------------------------------------- #
def test_idle_ticket_is_warned():
    assert _decide(_ticket(), NOW) == "warn"


def test_recent_ticket_is_left_alone():
    assert _decide(_ticket(date_updated=NOW - timedelta(days=4)), NOW) is None


def test_warned_ticket_closes_after_the_grace_window():
    assert _decide(_ticket(inactivity_warned_at=NOW - timedelta(hours=25)), NOW) == "close"


def test_exempt_ticket_is_never_warned():
    ticket = _ticket(autoclose_exempt=True, date_updated=NOW - timedelta(days=400))
    assert _decide(ticket, NOW) is None


def test_exempt_ticket_is_never_closed():
    # The close-time re-check passes last_activity=None; exemption must win
    # even for a row that somehow still carries an expired warning.
    ticket = _ticket(autoclose_exempt=True, inactivity_warned_at=NOW - timedelta(days=3))
    assert _decide(ticket, NOW) is None
    assert ts._inactivity_decision("open", None, ticket.inactivity_warned_at, NOW, exempt=True) is None


def test_existing_four_argument_calls_still_work():
    assert ts._inactivity_decision("open", NOW - timedelta(days=6), None, NOW) == "warn"


def test_closed_ticket_is_ignored():
    assert _decide(_ticket(status="closed"), NOW) is None


# --------------------------------------------------------------------------- #
# _set_autoclose_exempt: the row change /autoclose makes
# --------------------------------------------------------------------------- #
def test_exempting_drops_a_pending_warning():
    ticket = _ticket(inactivity_warned_at=NOW - timedelta(hours=20))
    assert ts._set_autoclose_exempt(ticket, True, NOW) is True
    assert ticket.autoclose_exempt is True
    assert ticket.inactivity_warned_at is None


def test_turning_it_back_on_never_closes_straight_away():
    # Warned, then exempted mid grace window, then turned back on days later.
    ticket = _ticket(inactivity_warned_at=NOW - timedelta(hours=20))
    ts._set_autoclose_exempt(ticket, True, NOW)
    later = NOW + timedelta(days=3)
    assert _decide(ticket, later) is None

    assert ts._set_autoclose_exempt(ticket, False, later) is True
    assert ticket.autoclose_exempt is False
    assert ticket.date_updated == later
    # A full 5 days before the next warning, and a warning before any close.
    assert _decide(ticket, later + timedelta(minutes=5)) is None
    assert _decide(ticket, later + timedelta(days=4, hours=23)) is None
    assert _decide(ticket, later + timedelta(days=5, minutes=1)) == "warn"


def test_repeating_the_current_setting_changes_nothing():
    stamp = NOW - timedelta(days=2)
    exempt = _ticket(autoclose_exempt=True, date_updated=stamp)
    assert ts._set_autoclose_exempt(exempt, True, NOW) is False
    assert exempt.date_updated == stamp

    normal = _ticket(date_updated=stamp, inactivity_warned_at=NOW - timedelta(hours=1))
    assert ts._set_autoclose_exempt(normal, False, NOW) is False
    # "Already on" must not quietly reset the clock or cancel a warning.
    assert normal.date_updated == stamp
    assert normal.inactivity_warned_at == NOW - timedelta(hours=1)


# --------------------------------------------------------------------------- #
# _apply_autoclose_setting: the DB half of the command
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, ticket):
        self.ticket = ticket
        self.commits = 0
        self.closed = False

    def query(self, *_):
        return self

    def filter(self, *_):
        return self

    def first(self):
        return self.ticket

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def session(monkeypatch):
    holder = {}

    def _factory(ticket):
        fake = _FakeSession(ticket)
        monkeypatch.setattr(ts, "Session", lambda: fake)
        holder["s"] = fake
        return fake

    return _factory


def test_not_a_ticket_channel(session):
    s = session(None)
    assert ts._apply_autoclose_setting(123, "off") == ("not_ticket", False)
    assert s.commits == 0 and s.closed


def test_closing_ticket_is_not_touched(session):
    ticket = _ticket(status="close_requested")
    s = session(ticket)
    assert ts._apply_autoclose_setting(123, "off") == ("not_open", False)
    assert ticket.autoclose_exempt is False
    assert s.commits == 0


def test_no_setting_only_reports(session):
    s = session(_ticket(autoclose_exempt=True))
    assert ts._apply_autoclose_setting(123, None) == ("status", True)
    assert s.commits == 0


def test_off_exempts_and_commits(session):
    ticket = _ticket(inactivity_warned_at=NOW - timedelta(hours=3))
    s = session(ticket)
    assert ts._apply_autoclose_setting(123, "off") == ("changed", True)
    assert ticket.autoclose_exempt is True
    assert ticket.inactivity_warned_at is None
    assert s.commits == 1 and s.closed


def test_off_twice_is_unchanged(session):
    s = session(_ticket(autoclose_exempt=True))
    assert ts._apply_autoclose_setting(123, "off") == ("unchanged", True)
    assert s.commits == 0


def test_on_restores_and_commits(session):
    ticket = _ticket(autoclose_exempt=True)
    s = session(ticket)
    assert ts._apply_autoclose_setting(123, "on") == ("changed", False)
    assert ticket.autoclose_exempt is False
    assert s.commits == 1


# --------------------------------------------------------------------------- #
# Who may exempt a ticket
# --------------------------------------------------------------------------- #
def _member(*role_ids):
    return SimpleNamespace(roles=[SimpleNamespace(id=r) for r in role_ids])


def test_closer_roles_can_change_autoclose():
    assert ts._author_has_staff_role(_member(ts.SUPPORT_ROLE_ID), ts.TICKET_CLOSER_ROLE_IDS)
    assert ts._author_has_staff_role(_member(ADMIN_ROLE), ts.TICKET_CLOSER_ROLE_IDS)


def test_ticket_helpers_cannot_change_autoclose():
    helper = _member(ts.TICKETS_ROLE_ID)
    assert not ts._author_has_staff_role(helper, ts.TICKET_CLOSER_ROLE_IDS)
    # ...but still count as staff for the opposing-party pings.
    assert ts._author_has_staff_role(helper)


def test_members_without_roles_cannot_change_autoclose():
    assert not ts._author_has_staff_role(_member(), ts.TICKET_CLOSER_ROLE_IDS)
    assert not ts._author_has_staff_role(SimpleNamespace(), ts.TICKET_CLOSER_ROLE_IDS)
