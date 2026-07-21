"""Event Manager role (web64a): the group-scoped event-editing grant.

Pins the new authorization predicates — ``deps.is_event_manager`` /
``deps.assert_event_editor`` — and that the event gates
(``events._is_event_admin`` / ``_assert_event_admin``) admit an event manager
while still rejecting a plain member and honoring the ``events`` entitlement.
Same monkeypatch-against-fakes approach as test_event_auth_modes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.deps as deps
import web_api.entitlements as ent
import web_api.routes.events as evr
from web_api.common import ProblemException


class _Q:
    def __init__(self, result):
        self._r = result

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._r

    def all(self):
        return list(self._r) if isinstance(self._r, list) else []


class _S:
    """Fake session: every query() returns the same scripted result."""

    def __init__(self, result=None):
        self._result = result

    def query(self, *a, **k):
        return _Q(self._result)


def _event(group_id=42, mode="standard"):
    return SimpleNamespace(id=1, group_id=group_id, mode=mode)


# ── is_event_manager ─────────────────────────────────────────────────────────

class TestIsEventManager:
    def test_true_when_grant_row_exists(self):
        assert deps.is_event_manager(_S(result=object()), 7, 42) is True

    def test_false_when_no_row(self):
        assert deps.is_event_manager(_S(result=None), 7, 42) is False

    def test_false_without_ids(self):
        assert deps.is_event_manager(_S(result=object()), None, 42) is False
        assert deps.is_event_manager(_S(result=object()), 7, None) is False


# ── assert_event_editor ──────────────────────────────────────────────────────

class TestAssertEventEditor:
    def _patch(self, mp, *, superadmin=False, role=None, manager=False, events_ent=True):
        mp.setattr(deps, "load_user", lambda s, uid: "USER")
        mp.setattr(deps, "is_superadmin", lambda user: superadmin)
        mp.setattr(deps, "resolve_group_role", lambda s, uid, gid, mg, user=None: role)
        mp.setattr(deps, "is_event_manager", lambda s, uid, gid: manager)
        mp.setattr(ent, "resolve_group_entitlements",
                   lambda s, gid, user=None: {"events": events_ent})

    def test_superadmin_bypasses(self, monkeypatch):
        self._patch(monkeypatch, superadmin=True, events_ent=False)
        deps.assert_event_editor(_S(), 7, 42, manage_guild_ids=set())  # no raise

    def test_admin_with_entitlement_ok(self, monkeypatch):
        self._patch(monkeypatch, role="admin", events_ent=True)
        deps.assert_event_editor(_S(), 7, 42, manage_guild_ids=set())

    def test_manager_with_entitlement_ok(self, monkeypatch):
        self._patch(monkeypatch, role="member", manager=True, events_ent=True)
        deps.assert_event_editor(_S(), 7, 42, manage_guild_ids=set())

    def test_plain_member_rejected(self, monkeypatch):
        self._patch(monkeypatch, role="member", manager=False)
        with pytest.raises(ProblemException) as e:
            deps.assert_event_editor(_S(), 7, 42, manage_guild_ids=set())
        assert e.value.status == 403
        assert e.value.extra.get("code") == "event_admin_required"

    def test_none_role_rejected(self, monkeypatch):
        self._patch(monkeypatch, role=None, manager=False)
        with pytest.raises(ProblemException) as e:
            deps.assert_event_editor(_S(), 7, 42, manage_guild_ids=set())
        assert e.value.status == 403

    def test_manager_without_entitlement_rejected(self, monkeypatch):
        # The role passes, but the group lacks the events tier → still blocked.
        self._patch(monkeypatch, role="member", manager=True, events_ent=False)
        with pytest.raises(ProblemException) as e:
            deps.assert_event_editor(_S(), 7, 42, manage_guild_ids=set())
        assert e.value.status == 403
        assert e.value.extra.get("code") == "entitlement_required"


# ── the event gates admit a manager ──────────────────────────────────────────

class TestEventGatesAdmitManager:
    def _patch(self, mp, *, role=None, manager=False):
        mp.setattr(evr, "load_user", lambda s, uid: "USER")
        mp.setattr(evr, "is_superadmin", lambda user: False)
        mp.setattr(evr, "manageable_guild_ids", lambda uid: set())
        mp.setattr(evr, "resolve_group_role", lambda s, uid, gid, mg, user=None: role)
        mp.setattr(evr, "is_event_manager", lambda s, uid, gid: manager)

    def test_is_event_admin_admits_manager(self, monkeypatch):
        self._patch(monkeypatch, role="member", manager=True)
        assert evr._is_event_admin(_S(), 7, _event()) is True

    def test_is_event_admin_rejects_plain_member(self, monkeypatch):
        self._patch(monkeypatch, role="member", manager=False)
        assert evr._is_event_admin(_S(), 7, _event()) is False

    def test_assert_event_admin_admits_manager_path(self, monkeypatch):
        # Standard path delegates to assert_event_editor; confirm the manager
        # reaches it with the right args.
        seen = {}
        monkeypatch.setattr(evr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(evr, "manageable_guild_ids", lambda uid: set())
        monkeypatch.setattr(
            evr, "assert_event_editor",
            lambda s, uid, gid, *, manage_guild_ids, user: seen.update(uid=uid, gid=gid))
        evr._assert_event_admin(_S(), 7, _event())
        assert seen == {"uid": 7, "gid": 42}
