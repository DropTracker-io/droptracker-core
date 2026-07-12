"""Unit tests for the pure-decision parts of services/event_signup.py.

Loaded directly from the file path (like test_event_lifecycle_sweep) so the
conftest db/services stubs never interfere — the module's top-level imports are
stdlib-only by design (DB models are lazy-imported inside functions). We drive
the mode/guard branches that need no DB, using tiny fakes.
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_signup.py",
)
_spec = importlib.util.spec_from_file_location("_event_signup_under_test", _PATH)
sus = importlib.util.module_from_spec(_spec)
sys.modules["_event_signup_under_test"] = sus
_spec.loader.exec_module(sus)


def _ev(**kw):
    base = dict(id=1, mode="standard", formation_mode="self_join", status="active",
                group_id=5, ends_at=None, join_code=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestSelfSignupMode:
    def test_signup_modes(self, monkeypatch):
        monkeypatch.setattr(sus, "EVENT_SELF_SIGNUP_MODES",
                            ("self_join", "auto_assign", "signup_pool"), raising=False)
        # Patch the lazy import target used inside is_self_signup_mode.
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)
        for m in ("self_join", "auto_assign", "signup_pool"):
            assert sus.is_self_signup_mode(_ev(formation_mode=m))
        assert not sus.is_self_signup_mode(_ev(formation_mode="admin_assign"))


class TestPerformSignupGuards:
    """Guard branches that raise before any DB query."""

    def _modes(self, monkeypatch):
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)

    def test_past_event_rejected(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(status="past"), player, 7)
        assert exc.value.status == 409

    def test_ended_window_rejected(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        ev = _ev(ends_at=datetime.now() - timedelta(hours=1))
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, ev, player, 7)
        assert exc.value.status == 409

    def test_admin_assign_rejected(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(formation_mode="admin_assign"), player, 7)
        assert exc.value.status == 403


class TestSignupError:
    def test_carries_problem_fields(self):
        e = sus.SignupError(422, "Nope", "because")
        assert (e.status, e.title, e.detail) == (422, "Nope", "because")
        assert str(e) == "because"
