"""Clan-vs-clan events (Implementation Plan B): the new-behavior tests.

Covers the §9 unit matrix: ``participating_group_ids`` across modes, the
``_assert_event_admin`` clan branch (host ✓ / accepted opponent ✓ /
non-participant ✗ / superadmin ✓), ``_assert_player_eligible`` with multiple
clans, clan-team routing in ``join_event``/``add_team``/``admin_add_member``,
the participant invite/accept/decline/remove endpoints, activation blockers,
and the dual-guild desired-state expansion.

Shares the scripted-session fakes with test_event_auth_modes (the guardrail
file) — a query the code should not issue fails the script.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import web_api.routes.event_participants as epr
import web_api.routes.events as evr
from web_api.common import ProblemException

from tests.unit.test_event_auth_modes import (
    _ASSOC,
    _S,
    _SessionCM,
    _event,
    _player,
    _team,
)


def _cvc(**kw):
    kw.setdefault("mode", "clan_vs_clan")
    kw.setdefault("group_id", 10)  # host clan
    return _event(**kw)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire_events(monkeypatch, session, user_id=7):
    monkeypatch.setattr(evr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
    monkeypatch.setattr(evr, "user_group_association", _ASSOC)
    monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)


def _wire_participants(monkeypatch, session, user_id=7):
    monkeypatch.setattr(epr, "current_user_id", lambda: user_id)
    monkeypatch.setattr(epr, "db_session", lambda: _SessionCM(session))


def _patch_roles(monkeypatch, module, *, superadmin=False, roles=None):
    """resolve_group_role answers from ``roles`` (gid -> role)."""
    roles = roles or {}
    monkeypatch.setattr(module, "load_user", lambda s, uid: "USER")
    monkeypatch.setattr(module, "is_superadmin", lambda user: superadmin)
    monkeypatch.setattr(module, "manageable_guild_ids", lambda uid: set())
    monkeypatch.setattr(
        module, "resolve_group_role",
        lambda s, uid, gid, mg, user=None: roles.get(gid),
    )


# ── participating_group_ids ───────────────────────────────────────────────────

class TestParticipatingGroupIds:
    def test_standard_is_the_single_owner_no_queries(self):
        assert evr.participating_group_ids(_S(), _event(group_id=42)) == {42}

    def test_global_is_empty_no_queries(self):
        assert evr.participating_group_ids(_S(), _event(group_id=None)) == set()

    def test_clan_vs_clan_is_the_accepted_set(self):
        s = _S([(10,), (20,)])
        assert evr.participating_group_ids(s, _cvc()) == {10, 20}

    def test_clan_vs_clan_before_any_acceptance(self):
        assert evr.participating_group_ids(_S([]), _cvc()) == set()


# ── _assert_event_admin: clan branch ──────────────────────────────────────────

class TestAssertEventAdminClan:
    def test_host_admin_may_manage(self, monkeypatch):
        _patch_roles(monkeypatch, evr, roles={10: "owner"})
        evr._assert_event_admin(_S([(10,), (20,)]), 7, _cvc())

    def test_accepted_opponent_admin_may_manage(self, monkeypatch):
        _patch_roles(monkeypatch, evr, roles={20: "admin"})
        evr._assert_event_admin(_S([(10,), (20,)]), 7, _cvc())

    def test_non_participant_admin_is_rejected(self, monkeypatch):
        _patch_roles(monkeypatch, evr, roles={99: "owner"})
        with pytest.raises(ProblemException) as exc:
            evr._assert_event_admin(_S([(10,), (20,)]), 7, _cvc())
        assert exc.value.status == 403

    def test_invited_but_unaccepted_opponent_is_rejected(self, monkeypatch):
        # Only 10 accepted; 20's admin has no rights until they accept.
        _patch_roles(monkeypatch, evr, roles={20: "owner"})
        with pytest.raises(ProblemException) as exc:
            evr._assert_event_admin(_S([(10,)]), 7, _cvc())
        assert exc.value.status == 403

    def test_superadmin_bypasses(self, monkeypatch):
        _patch_roles(monkeypatch, evr, superadmin=True)
        evr._assert_event_admin(_S(), 7, _cvc())  # no participant query needed

    def test_opponent_admin_needs_no_entitlement(self, monkeypatch):
        # The challenged (non-subscriber) clan's admin can co-manage: the clan
        # branch must NEVER consult the events entitlement. If it does, this
        # blows up loudly.
        _patch_roles(monkeypatch, evr, roles={20: "admin"})

        def _boom(*a, **k):
            raise AssertionError("entitlement must not be checked for opponents")

        monkeypatch.setattr(evr, "assert_group_entitlement", _boom)
        evr._assert_event_admin(_S([(10,), (20,)]), 7, _cvc())  # no raise

    def test_standard_event_object_takes_entitlement_path(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(evr, "load_user", lambda s, uid: "USER")
        monkeypatch.setattr(evr, "manageable_guild_ids", lambda uid: set())

        def fake_entitlement(s, uid, gid, key, *, manage_guild_ids=None, user=None):
            seen.update(gid=gid, key=key)

        monkeypatch.setattr(evr, "assert_group_entitlement", fake_entitlement)
        evr._assert_event_admin(_S(), 7, _event(group_id=42))
        assert seen == dict(gid=42, key="events")


# ── _is_event_admin: clan branch ──────────────────────────────────────────────

class TestIsEventAdminClan:
    def test_opponent_admin_is_event_admin(self, monkeypatch):
        _patch_roles(monkeypatch, evr, roles={20: "admin"})
        assert evr._is_event_admin(_S([(10,), (20,)]), 7, _cvc()) is True

    def test_plain_member_is_not(self, monkeypatch):
        _patch_roles(monkeypatch, evr, roles={10: "member", 20: "member"})
        assert evr._is_event_admin(_S([(10,), (20,)]), 7, _cvc()) is False


# ── _assert_player_eligible: clan branch ─────────────────────────────────────

class TestPlayerEligibleClan:
    def test_member_of_any_accepted_clan_is_eligible(self, monkeypatch):
        monkeypatch.setattr(evr, "user_group_association", _ASSOC)
        s = _S([(10,), (20,)], [(1,)])  # participants, then membership hit
        evr._assert_player_eligible(s, _cvc(), 3)

    def test_outsider_is_rejected(self, monkeypatch):
        monkeypatch.setattr(evr, "user_group_association", _ASSOC)
        s = _S([(10,), (20,)], [])
        with pytest.raises(ProblemException) as exc:
            evr._assert_player_eligible(s, _cvc(), 3)
        assert exc.value.status == 403


# ── create_event: mode validation ─────────────────────────────────────────────

class TestCreateEventMode:
    def _patch_constants(self, monkeypatch):
        # The conftest stubs `db`, so the imported tuples are MagicMocks whose
        # `in` checks always fail — restore the real values for these tests.
        monkeypatch.setattr(evr, "EVENT_MODES", ("standard", "clan_vs_clan"))
        monkeypatch.setattr(evr, "EVENT_KINDS", ("standard", "bingo", "board_game"))
        monkeypatch.setattr(
            evr, "EVENT_FORMATION_MODES", ("self_join", "auto_assign", "admin_assign")
        )
        monkeypatch.setattr(
            evr, "EVENT_SUBMISSION_POLICIES", ("all", "confirm_non_api", "api_only")
        )
        monkeypatch.setattr(
            evr, "EVENT_DISCORD_POLICIES", ("on_activate", "immediate")
        )
        monkeypatch.setattr(
            evr, "EVENT_PING_KEYS", ("event_created", "event_started", "event_ended")
        )

    async def test_clan_vs_clan_requires_host_group(self, client, monkeypatch):
        self._patch_constants(monkeypatch)
        monkeypatch.setattr(evr, "current_user_id", lambda: 7)
        r = await client.post(
            "/api/v1/events", json={"name": "Clash", "mode": "clan_vs_clan"}
        )
        assert r.status_code == 422
        body = await r.get_json()
        assert "Host group" in body.get("title", "")

    async def test_invalid_mode_rejected(self, client, monkeypatch):
        self._patch_constants(monkeypatch)
        monkeypatch.setattr(evr, "current_user_id", lambda: 7)
        r = await client.post(
            "/api/v1/events", json={"name": "X", "mode": "banana", "group_id": 1}
        )
        assert r.status_code == 422
        body = await r.get_json()
        assert "Invalid mode" in body.get("title", "")

    async def test_clan_vs_clan_seeds_accepted_host_row(self, client, monkeypatch):
        created = []

        class _RecEventGroup:
            event_id = group_id = status = None  # class-level column stand-ins

            def __init__(self, **kw):
                created.append(kw)

        class _RecEvent:
            def __init__(self, **kw):
                self.__dict__.update(kw)
                self.id = 55

        s = _S([None])  # Group lookup for the default discord guild (miss)
        self._patch_constants(monkeypatch)
        _wire_events(monkeypatch, s)
        monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
        monkeypatch.setattr(evr, "Event", _RecEvent)
        monkeypatch.setattr(evr, "EventGroup", _RecEventGroup)
        r = await client.post(
            "/api/v1/events",
            json={"name": "Clash", "mode": "clan_vs_clan", "group_id": 10},
        )
        assert r.status_code == 200
        assert (await r.get_json())["id"] == 55
        assert len(created) == 1
        host = created[0]
        assert host["group_id"] == 10
        assert host["role"] == "host"
        assert host["status"] == "accepted"


# ── add_team: clan binding ────────────────────────────────────────────────────

class TestAddTeamClan:
    def _setup(self, monkeypatch, s):
        created = {}

        class _RecTeam:
            def __init__(self, **kw):
                created.update(kw)
                self.id = 99

        _wire_events(monkeypatch, s)
        monkeypatch.setattr(evr, "EventTeam", _RecTeam)
        monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
        return created

    async def test_team_bound_to_accepted_clan(self, client, monkeypatch):
        s = _S([_cvc()], [(10,), (20,)])
        created = self._setup(monkeypatch, s)
        r = await client.post(
            "/api/v1/events/1/teams", json={"name": "Hosts", "group_id": 10}
        )
        assert r.status_code == 200
        assert created["group_id"] == 10

    async def test_missing_group_id_rejected(self, client, monkeypatch):
        s = _S([_cvc()])
        self._setup(monkeypatch, s)
        r = await client.post("/api/v1/events/1/teams", json={"name": "Hosts"})
        assert r.status_code == 422

    async def test_unaccepted_clan_rejected(self, client, monkeypatch):
        s = _S([_cvc()], [(10,), (20,)])
        self._setup(monkeypatch, s)
        r = await client.post(
            "/api/v1/events/1/teams", json={"name": "X", "group_id": 99}
        )
        assert r.status_code == 422


# ── join_event: clan-team routing ────────────────────────────────────────────

class TestJoinClanRouting:
    async def test_self_join_lands_on_own_clans_single_team(self, client, monkeypatch):
        s = _S(
            [_cvc()],                       # event
            [_player()],                    # owned player
            [(10,), (20,)],                 # participating groups (eligibility)
            [(1,)],                         # player is in a participating clan
            [],                             # not already on a team
            [(3,)],                         # one-RSN: user's players (only self)
            [_team(1, group_id=10), _team(2, group_id=20)],
            [(20,)],                        # the player's clans -> clan 20
        )
        _wire_events(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3})
        assert r.status_code == 200
        assert (await r.get_json())["team_id"] == 2

    async def test_cannot_pick_other_clans_team(self, client, monkeypatch):
        s = _S(
            [_cvc()], [_player()], [(10,), (20,)], [(1,)], [], [(3,)],
            [_team(1, group_id=10), _team(2, group_id=20)],
            [(20,)],
        )
        _wire_events(monkeypatch, s)
        r = await client.post(
            "/api/v1/events/1/join", json={"player_id": 3, "team_id": 1}
        )
        assert r.status_code == 404  # other clan's team is not selectable

    async def test_auto_assign_balances_within_own_clan(self, client, monkeypatch):
        s = _S(
            [_cvc(formation_mode="auto_assign")], [_player()],
            [(10,), (20,)], [(1,)], [], [(3,)],
            [_team(1, group_id=10), _team(2, group_id=10), _team(3, group_id=20)],
            [(10,)],                        # player's clan: 10 -> teams 1 and 2
            [(1, 4), (2, 1), (3, 0)],       # team 3 is smallest overall but foreign
        )
        _wire_events(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3})
        assert r.status_code == 200
        assert (await r.get_json())["team_id"] == 2

    async def test_no_team_for_own_clan_yet(self, client, monkeypatch):
        s = _S(
            [_cvc()], [_player()], [(10,), (20,)], [(1,)], [], [(3,)],
            [_team(1, group_id=10)],
            [(20,)],                        # own clan 20 has no team
        )
        _wire_events(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3})
        assert r.status_code == 404

    async def test_second_account_of_same_user_rejected(self, client, monkeypatch):
        # One-RSN: the user already entered with player 9; player 3 is refused.
        s = _S(
            [_cvc()], [_player()], [(10,), (20,)], [(1,)], [],
            [(3,), (9,)],   # user owns 3 and 9
            [(9,)],         # player 9 already signed up (other account)
        )
        _wire_events(monkeypatch, s)
        r = await client.post("/api/v1/events/1/join", json={"player_id": 3})
        assert r.status_code == 409


# ── admin_add_member: team clan constraint ───────────────────────────────────

class TestAdminAddMemberClan:
    async def test_wrong_clan_rejected(self, client, monkeypatch):
        s = _S(
            [_cvc()],                # event
            [_team(4, group_id=20)],  # clan-bound team
            [_player()],             # player exists
            [(10,), (20,)],          # participants (eligibility)
            [(1,)],                  # member of a participating clan
            [],                      # but NOT of clan 20 (team's clan)
        )
        _wire_events(monkeypatch, s)
        monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
        r = await client.post(
            "/api/v1/events/1/teams/4/members", json={"player_id": 3}
        )
        assert r.status_code == 403

    async def test_right_clan_added(self, client, monkeypatch):
        s = _S(
            [_cvc()], [_team(4, group_id=20)], [_player()],
            [(10,), (20,)], [(1,)],
            [(1,)],                  # member of clan 20
            [],                      # no existing membership
        )
        _wire_events(monkeypatch, s)
        monkeypatch.setattr(evr, "_assert_event_admin", lambda *a, **k: None)
        r = await client.post(
            "/api/v1/events/1/teams/4/members", json={"player_id": 3}
        )
        assert r.status_code == 200
        assert len(s.added) == 2  # membership + audit
        assert s.committed


# ── participant endpoints ────────────────────────────────────────────────────

class TestInviteEndpoint:
    async def test_host_admin_invites(self, client, monkeypatch):
        created = []

        class _RecEventGroup:
            event_id = group_id = status = None  # class-level column stand-ins

            def __init__(self, **kw):
                created.append(kw)

        s = _S(
            [_cvc()],           # event
            [SimpleNamespace(group_id=20)],  # target group exists
            [],                 # not already on the roster
        )
        _wire_participants(monkeypatch, s)
        monkeypatch.setattr(epr, "_assert_event_admin", lambda *a, **k: None)
        monkeypatch.setattr(epr, "EventGroup", _RecEventGroup)
        r = await client.post("/api/v1/events/1/participants", json={"group_id": 20})
        assert r.status_code == 200
        assert created[0]["role"] == "opponent"
        assert created[0]["status"] == "invited"
        assert s.committed

    async def test_duplicate_invite_conflicts(self, client, monkeypatch):
        s = _S(
            [_cvc()],
            [SimpleNamespace(group_id=20)],
            [SimpleNamespace(status="invited")],
        )
        _wire_participants(monkeypatch, s)
        monkeypatch.setattr(epr, "_assert_event_admin", lambda *a, **k: None)
        r = await client.post("/api/v1/events/1/participants", json={"group_id": 20})
        assert r.status_code == 409

    async def test_standard_event_rejected(self, client, monkeypatch):
        s = _S([_event()])
        _wire_participants(monkeypatch, s)
        r = await client.post("/api/v1/events/1/participants", json={"group_id": 20})
        assert r.status_code == 422


class TestAcceptDecline:
    def _row(self, status="invited", role="opponent"):
        return SimpleNamespace(status=status, role=role, responded_at=None)

    async def test_group_admin_accepts(self, client, monkeypatch):
        row = self._row()
        synced = []
        s = _S([_cvc()], [row])
        _wire_participants(monkeypatch, s)
        _patch_roles(monkeypatch, epr, roles={20: "admin"})
        monkeypatch.setattr(epr, "_sync_event_guilds", lambda *a: synced.append(True))
        r = await client.post("/api/v1/events/1/participants/20/accept", json={})
        assert r.status_code == 200
        assert row.status == "accepted"
        assert row.responded_at is not None
        assert synced  # accepted clan's guild joins the desired set
        assert s.committed

    async def test_non_admin_cannot_accept(self, client, monkeypatch):
        s = _S([_cvc()])
        _wire_participants(monkeypatch, s)
        _patch_roles(monkeypatch, epr, roles={20: "member"})
        r = await client.post("/api/v1/events/1/participants/20/accept", json={})
        assert r.status_code == 403

    async def test_double_respond_conflicts(self, client, monkeypatch):
        s = _S([_cvc()], [self._row(status="accepted")])
        _wire_participants(monkeypatch, s)
        _patch_roles(monkeypatch, epr, roles={20: "owner"})
        r = await client.post("/api/v1/events/1/participants/20/accept", json={})
        assert r.status_code == 409

    async def test_decline_does_not_touch_guild_sync(self, client, monkeypatch):
        row = self._row()
        synced = []
        s = _S([_cvc()], [row])
        _wire_participants(monkeypatch, s)
        _patch_roles(monkeypatch, epr, roles={20: "admin"})
        monkeypatch.setattr(epr, "_sync_event_guilds", lambda *a: synced.append(True))
        r = await client.post("/api/v1/events/1/participants/20/decline", json={})
        assert r.status_code == 200
        assert row.status == "declined"
        assert not synced


class TestRemoveParticipant:
    async def test_host_cannot_be_removed(self, client, monkeypatch):
        s = _S([_cvc()], [SimpleNamespace(role="host", status="accepted")])
        _wire_participants(monkeypatch, s)
        _patch_roles(monkeypatch, epr, roles={10: "owner"})
        r = await client.delete("/api/v1/events/1/participants/10")
        assert r.status_code == 409

    async def test_clan_with_teams_blocks_removal(self, client, monkeypatch):
        s = _S(
            [_cvc()],
            [SimpleNamespace(role="opponent", status="accepted")],
            [_team(1, group_id=20)],
        )
        _wire_participants(monkeypatch, s)
        _patch_roles(monkeypatch, epr, roles={10: "owner"})
        r = await client.delete("/api/v1/events/1/participants/20")
        assert r.status_code == 409

    async def test_host_admin_removes_teamless_opponent(self, client, monkeypatch):
        synced = []
        s = _S(
            [_cvc()],
            [SimpleNamespace(role="opponent", status="accepted")],
            [],
        )
        _wire_participants(monkeypatch, s)
        _patch_roles(monkeypatch, epr, roles={10: "owner"})
        monkeypatch.setattr(epr, "_sync_event_guilds", lambda *a: synced.append(True))
        r = await client.delete("/api/v1/events/1/participants/20")
        assert r.status_code == 200
        assert synced
        assert s.committed


# ── activation blockers (real module by file path) ───────────────────────────

_LC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_lifecycle.py",
)
_spec = importlib.util.spec_from_file_location("_event_lifecycle_cvc", _LC_PATH)
lc = importlib.util.module_from_spec(_spec)
sys.modules["_event_lifecycle_cvc"] = lc
_spec.loader.exec_module(lc)

_SE_PATH = os.path.join(os.path.dirname(_LC_PATH), "event_scheduled_events.py")
_spec2 = importlib.util.spec_from_file_location("_event_sched_cvc", _SE_PATH)
se = importlib.util.module_from_spec(_spec2)
sys.modules["_event_sched_cvc"] = se
_spec2.loader.exec_module(se)


class TestActivationBlockersClan:
    def _ready_event(self):
        return _cvc(status="draft", ends_at=datetime.now() + timedelta(days=1))

    def test_two_clans_with_teams_no_blockers(self):
        s = _S(
            [_team(1), _team(2)],     # team count
            [(10,), (20,)],           # accepted clans
            [(10,), (20,)],           # team group ids
        )
        assert lc.activation_blockers(s, self._ready_event()) == []

    def test_one_clan_blocks(self):
        s = _S([_team(1)], [(10,)], [(10,)])
        blockers = lc.activation_blockers(s, self._ready_event())
        assert any("two accepted clans" in b for b in blockers)

    def test_clan_without_team_blocks(self):
        s = _S([_team(1)], [(10,), (20,)], [(10,)])
        blockers = lc.activation_blockers(s, self._ready_event())
        assert any("at least one team" in b and "clan" in b for b in blockers)

    def test_no_teams_is_ready_whole_clan(self):
        # Teams are optional: two accepted clans and zero teams is ready —
        # activation seeds a whole-clan team per clan (anyone-vs-anyone). Only
        # two queries fire (team count + accepted clans); the team-group_id
        # query is skipped when there are no teams.
        s = _S([], [(10,), (20,)])
        assert lc.activation_blockers(s, self._ready_event()) == []


# ── dual-guild desired-state expansion ───────────────────────────────────────

class TestDualGuildDesiredState:
    def test_participant_guilds_join_the_desired_set(self):
        ev = _cvc(discord_guild_id="111")
        assert se.desired_guild_ids(ev, {"222", "333"}) == {"111", "222", "333"}

    def test_standard_event_never_queries_participants(self):
        assert se._participant_guild_ids(_S(), _event()) == set()

    def test_clan_vs_clan_collects_accepted_guilds(self):
        s = _S([("111",), ("222",)])
        assert se._participant_guild_ids(s, _cvc()) == {"111", "222"}
