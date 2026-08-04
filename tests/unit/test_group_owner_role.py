"""Group owner/admin split (web86a).

Pins the three things the split actually rests on:

1. ``resolve_group_role`` consults the explicit ``group_admins`` grant BEFORE
   the Discord guild, and MANAGE_GUILD now yields ``admin`` — not ``owner``.
2. The owner predicates (``group_owner_user_id`` / ``is_group_owner`` /
   ``assert_group_owner``) treat "who IS the owner" and "who may ACT as owner"
   as different questions, so a superadmin never displaces or masks a real one.
3. The Discord-perms policy switch actually gates the implicit admin path.

Same monkeypatch-against-fakes approach as test_event_manager_role.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import web_api.deps as deps
from web_api.common import ProblemException


class _Q:
    """Query stub: `filter(...).first()/.all()` return a scripted result."""

    def __init__(self, result):
        self._r = result

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._r

    def all(self):
        return list(self._r) if isinstance(self._r, list) else []


class _S:
    """Fake session answering successive query() calls from a script.

    Dispatching on the queried entity isn't an option: tests/conftest.py stubs
    ``db`` as a MagicMock, so ``Group``/``GroupAdmin`` carry no usable identity.
    Ordering is the contract instead — ``resolve_group_role`` queries Group,
    then GroupAdmin. Past the end of the script every query returns None.
    """

    def __init__(self, *results):
        self._results = list(results)
        self.queries = 0

    def query(self, *a, **k):
        idx = self.queries
        self.queries += 1
        return _Q(self._results[idx] if idx < len(self._results) else None)


def _group(guild_id="999"):
    return SimpleNamespace(group_id=42, guild_id=guild_id)


def _grant(user_id=7, role="admin"):
    return SimpleNamespace(group_id=42, user_id=user_id, role=role)


def _user(groups=()):
    return SimpleNamespace(user_id=7, groups=list(groups))


# ── resolve_group_role ───────────────────────────────────────────────────────

class TestResolveGroupRole:
    def _patch(self, mp, *, superadmin=False, policy=True):
        mp.setattr(deps, "load_user", lambda s, uid: _user())
        mp.setattr(deps, "is_superadmin", lambda user: superadmin)
        mp.setattr(deps, "discord_perms_grant_admin", lambda s, gid: policy)

    def test_superadmin_is_owner_everywhere(self, monkeypatch):
        self._patch(monkeypatch, superadmin=True)
        s = _S(_group())
        assert deps.resolve_group_role(s, 7, 42, set()) == "owner"

    def test_manage_guild_is_admin_not_owner(self, monkeypatch):
        """The whole point of web86a: Discord managers configure, they don't own."""
        self._patch(monkeypatch)
        s = _S(_group(guild_id="999"), None)
        assert deps.resolve_group_role(s, 7, 42, {"999"}) == "admin"

    def test_owner_grant_beats_manage_guild(self, monkeypatch):
        """Grant is consulted first, so a MANAGE_GUILD holder can't displace
        the one real owner — nor be shown as a second one."""
        self._patch(monkeypatch)
        s = _S(_group(guild_id="999"), _grant(role="owner"))
        assert deps.resolve_group_role(s, 7, 42, {"999"}) == "owner"

    def test_admin_grant_with_manage_guild_stays_admin(self, monkeypatch):
        self._patch(monkeypatch)
        s = _S(_group(guild_id="999"), _grant(role="admin"))
        assert deps.resolve_group_role(s, 7, 42, {"999"}) == "admin"

    def test_policy_off_drops_implicit_admin(self, monkeypatch):
        """With the owner's switch off, Discord perms grant nothing; the user
        falls through to plain membership."""
        self._patch(monkeypatch, policy=False)
        monkeypatch.setattr(
            deps, "load_user", lambda s, uid: _user(groups=[SimpleNamespace(group_id=42)])
        )
        s = _S(_group(guild_id="999"), None)
        assert deps.resolve_group_role(s, 7, 42, {"999"}) == "member"

    def test_policy_off_leaves_explicit_grant_alone(self, monkeypatch):
        self._patch(monkeypatch, policy=False)
        s = _S(_group(guild_id="999"), _grant(role="admin"))
        assert deps.resolve_group_role(s, 7, 42, {"999"}) == "admin"

    def test_unknown_role_value_collapses_to_admin(self, monkeypatch):
        """A stray enum value must never read as owner."""
        self._patch(monkeypatch)
        s = _S(_group(), _grant(role="superduper"))
        assert deps.resolve_group_role(s, 7, 42, set()) == "admin"

    def test_no_group_is_none(self, monkeypatch):
        self._patch(monkeypatch)
        assert deps.resolve_group_role(_S(None), 7, 42, {"999"}) is None

    def test_plain_member(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.setattr(
            deps, "load_user", lambda s, uid: _user(groups=[SimpleNamespace(group_id=42)])
        )
        s = _S(_group(), None)
        assert deps.resolve_group_role(s, 7, 42, set()) == "member"

    def test_outsider_is_none(self, monkeypatch):
        self._patch(monkeypatch)
        s = _S(_group(), None)
        assert deps.resolve_group_role(s, 7, 42, set()) is None


# ── discord_perms_grant_admin ────────────────────────────────────────────────

class TestDiscordPermsPolicy:
    def _patch_config(self, mp, value):
        import utils.group_config as gc

        mp.setattr(gc, "get", lambda s, gid, key, default=None: value)

    @pytest.mark.parametrize("value", [None, "", "true", "1", "yes", "anything"])
    def test_defaults_and_truthy_values_enable(self, monkeypatch, value):
        self._patch_config(monkeypatch, value)
        assert deps.discord_perms_grant_admin(_S(), 42) is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " Off "])
    def test_falsy_values_disable(self, monkeypatch, value):
        self._patch_config(monkeypatch, value)
        assert deps.discord_perms_grant_admin(_S(), 42) is False


# ── ownership predicates ─────────────────────────────────────────────────────

class TestGroupOwnerUserId:
    def test_returns_owner_id(self):
        assert deps.group_owner_user_id(_S((11,)), 42) == 11

    def test_none_when_ownerless(self):
        assert deps.group_owner_user_id(_S(None), 42) is None

    def test_user_id_zero_is_a_real_owner(self):
        """user_id 0 exists in production; truthiness checks would drop it."""
        assert deps.group_owner_user_id(_S((0,)), 42) == 0


class TestIsGroupOwner:
    def test_true_for_the_owner(self, monkeypatch):
        monkeypatch.setattr(deps, "is_superadmin", lambda user: False)
        monkeypatch.setattr(deps, "group_owner_user_id", lambda s, gid: 7)
        assert deps.is_group_owner(_S(), 7, 42, user="U") is True

    def test_false_for_an_admin(self, monkeypatch):
        monkeypatch.setattr(deps, "is_superadmin", lambda user: False)
        monkeypatch.setattr(deps, "group_owner_user_id", lambda s, gid: 11)
        assert deps.is_group_owner(_S(), 7, 42, user="U") is False

    def test_false_when_ownerless(self, monkeypatch):
        monkeypatch.setattr(deps, "is_superadmin", lambda user: False)
        monkeypatch.setattr(deps, "group_owner_user_id", lambda s, gid: None)
        assert deps.is_group_owner(_S(), 7, 42, user="U") is False

    def test_superadmin_may_act_as_owner(self, monkeypatch):
        monkeypatch.setattr(deps, "is_superadmin", lambda user: True)
        monkeypatch.setattr(deps, "group_owner_user_id", lambda s, gid: 11)
        assert deps.is_group_owner(_S(), 7, 42, user="U") is True

    def test_superadmin_does_not_become_the_owner(self, monkeypatch):
        """Staff may ACT as owner, but the group's owner is still whoever holds
        the grant — otherwise every group would look owned to them and the
        ownerless-claim prompt would never appear."""
        monkeypatch.setattr(deps, "is_superadmin", lambda user: True)
        assert deps.group_owner_user_id(_S(None), 42) is None


class TestAssertGroupOwner:
    def test_passes_for_owner(self, monkeypatch):
        monkeypatch.setattr(deps, "is_group_owner", lambda s, uid, gid, user=None: True)
        deps.assert_group_owner(_S(), 7, 42)  # no raise

    def test_403_for_admin(self, monkeypatch):
        monkeypatch.setattr(deps, "is_group_owner", lambda s, uid, gid, user=None: False)
        with pytest.raises(ProblemException) as exc:
            deps.assert_group_owner(_S(), 7, 42)
        assert exc.value.status == 403
        assert exc.value.extra.get("code") == "group_owner_required"


# ── assert_group_admin still admits both roles ───────────────────────────────

class TestAssertGroupAdminUnchanged:
    """The split must not narrow the 45 existing admin gates."""

    @pytest.mark.parametrize("role", ["owner", "admin"])
    def test_owner_and_admin_both_pass(self, monkeypatch, role):
        monkeypatch.setattr(
            deps, "resolve_group_role", lambda s, uid, gid, mg, user=None: role
        )
        assert deps.assert_group_admin(_S(), 7, 42, set()) == role

    @pytest.mark.parametrize("role", ["member", None])
    def test_member_and_outsider_rejected(self, monkeypatch, role):
        monkeypatch.setattr(
            deps, "resolve_group_role", lambda s, uid, gid, mg, user=None: role
        )
        with pytest.raises(ProblemException) as exc:
            deps.assert_group_admin(_S(), 7, 42, set())
        assert exc.value.status == 403
