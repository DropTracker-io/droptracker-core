"""Regression guardrails for the clan-vs-clan work (Implementation Plan B, §9).

These pin the standard/global behavior of the event authorization and
eligibility helpers and the join/roster flows BEFORE the clan-vs-clan changes
land, and must keep passing byte-for-byte afterwards — clan-vs-clan is
additive and the standard/global code paths must not change behavior.

The scripted fake session doubles as a "standard events read none of the new
tables" probe: every ``query()`` pops the next scripted result, so an
unexpected extra query (e.g. a stray ``web_event_groups`` read on the
standard path) misaligns the script and fails the test.

The conftest stubs ``db``/``services``, so routes run against fake sessions
with module-level monkeypatching (same approach as test_event_discord_auth).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import web_api.routes.events as evr
from web_api.common import ProblemException


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Q:
    def __init__(self, result):
        self._r = result

    def filter(self, *a, **k):
        return self

    def with_for_update(self, *a, **k):
        # Row lock is a no-op in the scripted fake (P0-4/P0-5 lock the target
        # row in the real path); keep the chain fluent.
        return self

    def join(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def group_by(self, *a, **k):
        return self

    def distinct(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def first(self):
        return self._r[0] if self._r else None

    def all(self):
        return list(self._r)

    def count(self):
        return len(self._r)

    def delete(self, *a, **k):
        # Bulk delete: SQLAlchemy returns the affected row count.
        return len(self._r)

    def update(self, *a, **k):
        # Bulk update: SQLAlchemy returns the affected row count.
        return len(self._r)


class _S:
    """Scripted session: query() returns the next batch, in order. Running
    out of batches means the code under test issued a query the standard
    path never used to issue — that's a regression, so it raises."""

    def __init__(self, *batches):
        self._batches = list(batches)
        self.added = []
        self.committed = False

    def query(self, *a, **k):
        assert self._batches, "unexpected extra query on the standard/global path"
        return _Q(self._batches.pop(0))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        pass

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def begin_nested(self):
        """No-op savepoint context (routes use it for dedupe-tolerant adds)."""
        class _NestedCM:
            def __enter__(cm):
                return cm

            def __exit__(cm, *exc):
                return False

        return _NestedCM()


class _Col:
    """Column stand-in usable in filter expressions (==, in_)."""

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0

    def in_(self, other):
        return True


_ASSOC = SimpleNamespace(c=SimpleNamespace(id=_Col(), player_id=_Col(), group_id=_Col()))


def _event(**kw):
    base = dict(
        id=1, group_id=42, name="Ev", description=None, status="active",
        starts_at=None, ends_at=None, has_bingo=False,
        formation_mode="self_join", requires_confirmation=False,
        submission_policy="all", join_code=None, discord_guild_id=None,
        board_size=5, bonus_line_points=0, bonus_blackout_points=0,
        activated_at=None, ended_at=None, mode="standard",
        # These fixtures are about join *mechanics* on a running event. Since
        # web70a a started event closes self sign-ups unless it opted into late
        # ones, so the factory opts in; the window itself is covered by
        # TestJoinSignupWindow below.
        allow_late_signups=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _team(id, name="Team", group_id=None, color=None):
    return SimpleNamespace(id=id, event_id=1, name=name, score=0, group_id=group_id, color=color)


def _player(user_id=7, player_id=3):
    return SimpleNamespace(player_id=player_id, user_id=user_id, player_name="P")


class _SessionCM:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


# ── _assert_event_admin: standard/global contract ────────────────────────────

class TestAssertEventAdminStandard:
    def test_global_requires_superadmin(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(evr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(evr, "assert_superadmin", lambda user: calls.setdefault("user", user))
        evr._assert_event_admin(_S(), 7, None)
        assert calls["user"] == "USER"

    def test_group_delegates_to_event_editor(self, monkeypatch):
        # web64a: the standard path now routes through assert_event_editor (group
        # admin OR event manager, + the events entitlement) instead of the
        # admin-only assert_group_entitlement.
        seen = {}
        monkeypatch.setattr(evr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(evr, "manageable_guild_ids", lambda uid: {"g1"})

        def fake_editor(s, uid, gid, *, manage_guild_ids=None, user=None):
            seen.update(uid=uid, gid=gid, manage=manage_guild_ids, user=user)

        monkeypatch.setattr(evr, "assert_event_editor", fake_editor)
        evr._assert_event_admin(_S(), 7, 42)
        assert seen == dict(uid=7, gid=42, manage={"g1"}, user="USER")

    def test_group_editor_denial_propagates(self, monkeypatch):
        monkeypatch.setattr(evr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(evr, "manageable_guild_ids", lambda uid: set())

        def deny(*a, **k):
            evr.abort_problem(403, "Forbidden", "no")

        monkeypatch.setattr(evr, "assert_event_editor", deny)
        with pytest.raises(ProblemException) as exc:
            evr._assert_event_admin(_S(), 7, 42)
        assert exc.value.status == 403


# ── _is_event_admin: standard/global contract ────────────────────────────────

class TestIsEventAdminStandard:
    def _patch(self, monkeypatch, *, superadmin=False, role=None, manager=False):
        monkeypatch.setattr(evr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(evr, "is_superadmin", lambda user: superadmin)
        monkeypatch.setattr(evr, "manageable_guild_ids", lambda uid: set())
        monkeypatch.setattr(
            evr, "resolve_group_role", lambda s, uid, gid, mg, user=None: role
        )
        # web64a: _is_event_admin now also consults is_event_manager when the
        # role isn't owner/admin — stub it off by default for the role gate.
        monkeypatch.setattr(evr, "is_event_manager", lambda s, uid, gid: manager)

    def test_anonymous_is_not_admin(self):
        assert evr._is_event_admin(_S(), None, _event()) is False

    def test_superadmin_is_admin_everywhere(self, monkeypatch):
        self._patch(monkeypatch, superadmin=True)
        assert evr._is_event_admin(_S(), 7, _event()) is True
        assert evr._is_event_admin(_S(), 7, _event(group_id=None)) is True

    def test_global_event_non_superadmin_is_not_admin(self, monkeypatch):
        self._patch(monkeypatch, role="owner")  # role would say yes; group is None
        assert evr._is_event_admin(_S(), 7, _event(group_id=None)) is False

    @pytest.mark.parametrize("role,expected", [
        ("owner", True), ("admin", True), ("member", False), (None, False),
    ])
    def test_group_role_gate(self, monkeypatch, role, expected):
        self._patch(monkeypatch, role=role)
        assert evr._is_event_admin(_S(), 7, _event()) is expected


# ── _assert_player_eligible: standard/global contract ────────────────────────

class TestPlayerEligibleStandard:
    def test_global_event_everyone_eligible_no_queries(self):
        # An empty script: ANY query would raise — global events must not
        # touch the DB here.
        evr._assert_player_eligible(_S(), _event(group_id=None), 3)

    def test_group_member_is_eligible(self, monkeypatch):
        monkeypatch.setattr(evr, "user_group_association", _ASSOC)
        evr._assert_player_eligible(_S([(1,)]), _event(), 3)

    def test_non_member_is_rejected(self, monkeypatch):
        monkeypatch.setattr(evr, "user_group_association", _ASSOC)
        with pytest.raises(ProblemException) as exc:
            evr._assert_player_eligible(_S([]), _event(), 3)
        assert exc.value.status == 403


# ── join / roster routes: standard behavior through the app ──────────────────

@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session, user_id=7):
    monkeypatch.setattr(evr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "user_group_association", _ASSOC)
    monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)


class TestJoinEventStandard:
    async def test_admin_assign_refuses_self_join(self, client, monkeypatch):
        s = _S([_event(formation_mode="admin_assign")], [_player()], [(1,)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3})
        assert r.status_code == 403

    async def test_self_join_picks_requested_team(self, client, monkeypatch):
        s = _S([_event()], [_player()], [(1,)], [], [_team(1), _team(2)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3, "team_id": 2})
        assert r.status_code == 200
        assert (await r.get_json())["team_id"] == 2
        assert s.committed

    async def test_self_join_single_team_auto_picks(self, client, monkeypatch):
        s = _S([_event()], [_player()], [(1,)], [], [_team(5)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3})
        assert r.status_code == 200
        assert (await r.get_json())["team_id"] == 5

    async def test_self_join_wrong_code_rejected(self, client, monkeypatch):
        s = _S([_event(join_code="secret")], [_player()], [(1,)])
        _wire(monkeypatch, s)
        r = await client.post(
            "/api/v1/events/1/join",
            json={"player_id": 3, "team_id": 1, "join_code": "nope"},
        )
        assert r.status_code == 403

    async def test_already_joined_conflicts(self, client, monkeypatch):
        s = _S([_event()], [_player()], [(1,)], [SimpleNamespace(team_id=1)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3, "team_id": 1})
        assert r.status_code == 409

    async def test_auto_assign_places_on_smallest_team(self, client, monkeypatch):
        s = _S(
            [_event(formation_mode="auto_assign")],
            [_player()],
            [(1,)],
            [],
            [_team(1), _team(2), _team(3)],
            [(1, 5), (2, 2), (3, 9)],  # member counts by team
        )
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3})
        assert r.status_code == 200
        assert (await r.get_json())["team_id"] == 2

    async def test_non_member_cannot_join_group_event(self, client, monkeypatch):
        s = _S([_event()], [_player()], [])  # eligibility query finds nothing
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3, "team_id": 1})
        assert r.status_code == 403


class TestJoinSignupWindow:
    """web70a: a started event stops taking self sign-ups unless it allows late
    ones — the site refusing in step with the Discord prompt losing its button.
    The refusal lands before any player/eligibility query (the scripted session
    would fail the run if one were issued)."""

    async def test_started_event_refuses_by_default(self, client, monkeypatch):
        s = _S([_event(allow_late_signups=False)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3, "team_id": 1})
        assert r.status_code == 409
        assert (await r.get_json())["code"] == "signups_closed"

    async def test_scheduled_event_before_start_still_joins(self, client, monkeypatch):
        ev = _event(allow_late_signups=False, status="draft",
                    starts_at=datetime.now() + timedelta(days=1))
        s = _S([ev], [_player()], [(1,)], [], [_team(1), _team(2)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3, "team_id": 2})
        assert r.status_code == 200

    async def test_late_signups_event_still_joins(self, client, monkeypatch):
        s = _S([_event(allow_late_signups=True)], [_player()], [(1,)], [],
               [_team(1), _team(2)])
        _wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3, "team_id": 2})
        assert r.status_code == 200


class TestAddTeamStandard:
    async def test_team_created_without_clan_binding(self, client, monkeypatch):
        created = {}

        class _RecTeam:
            def __init__(self, **kw):
                created.update(kw)
                self.id = 99

        s = _S([_event()])
        _wire(monkeypatch, s)
        monkeypatch.setattr(evr, "EventTeam", _RecTeam)
        monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
        r = await client.post("/api/v1/events/1/teams", json={"name": "Alpha"})
        assert r.status_code == 200
        assert (await r.get_json())["id"] == 99
        assert created["event_id"] == 1
        assert created["name"] == "Alpha"
        assert created["score"] == 0
        # Standard events never bind a team to a clan.
        assert not created.get("group_id")


class TestAdminRosterStandard:
    async def test_admin_add_member_standard_flow(self, client, monkeypatch):
        s = _S(
            [_event(formation_mode="admin_assign")],
            [_team(4)],
            [_player()],
            [(1,)],   # eligibility: member of the event's group
            [],       # no existing membership
        )
        _wire(monkeypatch, s)
        monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
        r = await client.post("/api/v1/events/1/teams/4/members", json={"player_id": 3})
        assert r.status_code == 200
        # Membership row + audit row.
        assert len(s.added) == 2
        assert s.committed


# ── activation_blockers: standard contract (real module by file path) ────────

_LC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_lifecycle.py",
)
_spec = importlib.util.spec_from_file_location("_event_lifecycle_guardrail", _LC_PATH)
lc = importlib.util.module_from_spec(_spec)
sys.modules["_event_lifecycle_guardrail"] = lc
_spec.loader.exec_module(lc)


class TestActivationBlockersStandard:
    def test_ready_standard_event_has_no_blockers(self):
        ev = _event(status="draft", ends_at=datetime.now() + timedelta(days=1))
        assert lc.activation_blockers(_S([_team(1)]), ev) == []

    def test_needs_a_team(self):
        ev = _event(status="draft")
        blockers = lc.activation_blockers(_S([]), ev)
        assert any("at least one team" in b for b in blockers)

    def test_past_end_date_blocks(self):
        ev = _event(status="draft", ends_at=datetime.now() - timedelta(days=1))
        blockers = lc.activation_blockers(_S([_team(1)]), ev)
        assert any("end date" in b for b in blockers)
