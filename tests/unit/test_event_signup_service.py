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
    # allow_late_signups on by default so these fixtures exercise the guard
    # under test rather than web70a's window (a started event closes sign-ups
    # otherwise); TestSignupWindow covers the window itself.
    base = dict(id=1, mode="standard", formation_mode="self_join", status="active",
                group_id=5, starts_at=None, ends_at=None, join_code=None,
                activated_at=None, allow_late_signups=True)
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


class TestSignupWindow:
    """web70a: self sign-ups close when the event begins unless the event opted
    into late sign-ups. This is what retires the Discord prompt's button."""

    def _modes(self, monkeypatch):
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)

    def test_scheduled_event_before_start_is_open(self):
        ev = _ev(status="draft", allow_late_signups=False,
                 starts_at=datetime.now() + timedelta(days=1))
        assert sus.signups_closed(ev) is None
        assert not sus.event_started(ev)

    def test_active_event_closes_by_default(self):
        ev = _ev(status="active", allow_late_signups=False)
        assert sus.signups_closed(ev) == "Sign-ups closed when the event began."

    def test_draft_past_its_start_time_closes(self):
        # The lifecycle sweep is about to activate it; don't take entries in
        # the gap.
        ev = _ev(status="draft", allow_late_signups=False,
                 starts_at=datetime.now() - timedelta(minutes=5))
        assert sus.signups_closed(ev) is not None

    def test_late_signups_keeps_a_running_event_open(self):
        ev = _ev(status="active", allow_late_signups=True,
                 ends_at=datetime.now() + timedelta(days=1))
        assert sus.signups_closed(ev) is None

    def test_late_signups_still_closes_at_the_end(self):
        ev = _ev(status="active", allow_late_signups=True,
                 ends_at=datetime.now() - timedelta(minutes=1))
        assert sus.signups_closed(ev) == "This event is over — sign-ups are closed."

    def test_close_time_is_the_start_by_default(self):
        starts = datetime.now() + timedelta(hours=2)
        ends = starts + timedelta(days=3)
        assert sus.signup_close_at(
            _ev(allow_late_signups=False, starts_at=starts, ends_at=ends)) == starts
        # Activation wins over the schedule (an admin may start it early).
        activated = datetime.now()
        assert sus.signup_close_at(
            _ev(allow_late_signups=False, starts_at=starts, ends_at=ends,
                activated_at=activated)) == activated

    def test_close_time_is_the_end_with_late_signups(self):
        ends = datetime.now() + timedelta(days=3)
        assert sus.signup_close_at(
            _ev(allow_late_signups=True, starts_at=datetime.now(), ends_at=ends)) == ends

    def test_perform_signup_refuses_a_started_event(self, monkeypatch):
        self._modes(monkeypatch)
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(status="active", allow_late_signups=False),
                               player, 7)
        assert exc.value.status == 409
        assert "began" in exc.value.detail


class TestMultiClanGuard:
    """G7: a player in more than one participating clan can't self/auto sign up."""

    def test_standard_event_is_noop_no_db(self):
        # Standard events short-circuit before any membership query (session=None
        # would blow up if it were touched).
        sus.assert_single_participating_clan(None, _ev(mode="standard"), 3)
        assert sus.participating_clans_for_player(None, _ev(mode="standard"), 3) == set()

    def test_intersection_is_the_players_participating_clans(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {20, 99})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        assert sus.participating_clans_for_player(
            None, _ev(mode="clan_vs_clan"), 3
        ) == {20}

    def test_single_clan_is_allowed(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {20})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        sus.assert_single_participating_clan(None, _ev(mode="clan_vs_clan"), 3)  # no raise

    def test_multi_clan_blocks_409(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {10, 20})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20, 30})
        with pytest.raises(sus.SignupError) as exc:
            sus.assert_single_participating_clan(None, _ev(mode="clan_vs_clan"), 3)
        assert exc.value.status == 409

    def test_signup_group_picks_the_single_clan(self, monkeypatch):
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {20, 99})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        assert sus.signup_group_for_player(None, _ev(mode="clan_vs_clan"), 3) == 20

    def test_perform_signup_blocks_multi_clan_before_recording(self, monkeypatch):
        import types
        fake = types.ModuleType("db.models")
        fake.EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")
        monkeypatch.setitem(sys.modules, "db.models", fake)
        # Get past eligibility so the multi-clan guard is what fires.
        monkeypatch.setattr(sus, "assert_player_eligible", lambda s, ev, pid: None)
        monkeypatch.setattr(sus, "player_group_ids", lambda s, pid: {10, 20})
        monkeypatch.setattr(sus, "participating_group_ids", lambda s, ev: {10, 20})
        player = SimpleNamespace(player_id=3)
        with pytest.raises(sus.SignupError) as exc:
            sus.perform_signup(None, _ev(mode="clan_vs_clan"), player, 7)
        assert exc.value.status == 409
        assert "clan" in exc.value.detail  # the multi-clan guard, not the window


class TestSignupError:
    def test_carries_problem_fields(self):
        e = sus.SignupError(422, "Nope", "because")
        assert (e.status, e.title, e.detail) == (422, "Nope", "because")
        assert str(e) == "because"
