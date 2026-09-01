"""activate_event refuses a scheduled draft before its start unless start_now.

Loaded from the file path like test_event_lifecycle_sweep.py so the conftest
stubs never interfere. The guard runs before the service touches the session
or imports the engine, so a bare SimpleNamespace event and ``session=None``
exercise it completely.

Why: on 2026-09-01 a leader pressed "Activate" on a bingo scheduled for
1 October expecting to open sign-ups; the event started a month early, was
ended to undo the announcement, and became unrecoverable (``past``). The
schedule already activates drafts (the sweep), so an early start must be an
explicit choice.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_lifecycle.py",
)
_spec = importlib.util.spec_from_file_location("_event_lifecycle_guard_under_test", _MODULE_PATH)
lc = importlib.util.module_from_spec(_spec)
sys.modules["_event_lifecycle_guard_under_test"] = lc
_spec.loader.exec_module(lc)

NOW = datetime(2026, 9, 1, 23, 0, 0)


def _draft(starts_at):
    return SimpleNamespace(id=58, status="draft", starts_at=starts_at, ends_at=None)


def test_future_start_is_refused_without_start_now():
    with pytest.raises(lc.LifecycleError) as exc:
        lc.activate_event(None, _draft(NOW + timedelta(days=30)), now=NOW)
    assert exc.value.status == 409
    assert "2026-10-01" in exc.value.detail
    assert "sign-ups are already open" in exc.value.detail.lower()


def test_past_and_active_checks_still_come_first():
    with pytest.raises(lc.LifecycleError) as exc:
        lc.activate_event(None, SimpleNamespace(status="past", starts_at=NOW + timedelta(days=1)), now=NOW)
    assert exc.value.title == "Event is over"
    with pytest.raises(lc.LifecycleError) as exc:
        lc.activate_event(None, SimpleNamespace(status="active", starts_at=NOW + timedelta(days=1)), now=NOW)
    assert exc.value.title == "Already active"


@pytest.mark.parametrize(
    "kwargs, starts_at",
    [
        ({"start_now": True}, NOW + timedelta(days=30)),   # explicit early start
        ({}, NOW - timedelta(minutes=1)),                  # the sweep's case: due
        ({}, NOW),                                         # due exactly now
        ({}, None),                                        # unscheduled draft
    ],
)
def test_guard_lets_the_other_cases_through(kwargs, starts_at):
    # Past the guard the service needs a real session/engine; reaching that
    # point (any error that is NOT the scheduled-start LifecycleError) proves
    # the guard did not fire.
    try:
        lc.activate_event(None, _draft(starts_at), now=NOW, **kwargs)
    except lc.LifecycleError as exc:
        assert exc.title != "Scheduled to start later"
    except Exception:
        pass
