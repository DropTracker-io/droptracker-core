"""Ticket ping mute (web149a): ``/pings`` and the welcome card's toggle button.

* **One switch, both directions.** A muted ticket sends no ghost ping to the
  opener when staff reply, and none to the support role when the opener (or
  anyone else) posts.
* **The button toggles, the command sets.** ``/pings off`` on an already muted
  ticket changes nothing; the button always flips it.

services/ticket_system.py is loaded from its file path, as in
test_ticket_autoclose.py.
"""
from types import SimpleNamespace

from tests.unit.test_ticket_autoclose import ADMIN_ROLE, ts

OPENER_ID = 111
STAFF_ID = 222


def _message(author_id, roles=()):
    author = SimpleNamespace(id=author_id, roles=[SimpleNamespace(id=r) for r in roles])
    return SimpleNamespace(author=author)


def _target(muted, message):
    return ts._ping_target(muted, message.author, OPENER_ID)


# --------------------------------------------------------------------------- #
# _next_pings_muted
# --------------------------------------------------------------------------- #
def test_no_option_only_reads():
    assert ts._next_pings_muted(False, None) is None
    assert ts._next_pings_muted(True, None) is None


def test_off_mutes_and_on_unmutes_regardless_of_current():
    assert ts._next_pings_muted(False, "off") is True
    assert ts._next_pings_muted(True, "off") is True
    assert ts._next_pings_muted(True, "on") is False
    assert ts._next_pings_muted(False, "on") is False


def test_button_flips():
    assert ts._next_pings_muted(False, "toggle") is True
    assert ts._next_pings_muted(True, "toggle") is False


# --------------------------------------------------------------------------- #
# _ping_target
# --------------------------------------------------------------------------- #
def test_opener_message_pings_staff_when_unmuted():
    assert _target(False, _message(OPENER_ID)) == ("role", ts.SUPPORT_ROLE_ID)


def test_staff_message_pings_opener_when_unmuted():
    assert _target(False, _message(STAFF_ID, roles=[ADMIN_ROLE])) == ("user", OPENER_ID)


def test_other_member_pings_staff_when_unmuted():
    assert _target(False, _message(333)) == ("role", ts.SUPPORT_ROLE_ID)


def test_muted_ticket_pings_nobody():
    assert _target(True, _message(OPENER_ID)) is None
    assert _target(True, _message(STAFF_ID, roles=[ADMIN_ROLE])) is None
    assert _target(True, _message(333)) is None
