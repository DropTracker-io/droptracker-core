"""Staff-hosted clan-vs-clan events (web119a).

A clan_vs_clan event with no host group is run by DropTracker staff. The
properties that matter, and that would fail quietly if they regressed:

* **Clans can't edit the event.** An accepted clan's admins co-manage an
  ordinary clan-vs-clan event; on a staff-hosted one they must NOT pass the
  event-admin gate at all (tasks, board, scoring, lifecycle are staff-only).
* **Clans run their own side, and only their own.** Roster, leaders, team
  cosmetics and sign-ups are reopened to a clan's managers — for their clan's
  team, never a rival's.
* **The roster limits hold.** Nobody but staff (with an explicit override)
  takes a clan past ``clan_roster_max``; a clan under ``clan_roster_min`` at
  the start is dropped, inside the activation transaction.
* **One cascade deletes a team.** ``services/event_team_purge.py`` is shared
  by team delete, clan withdrawal/removal and the start-of-event drop, so it
  pins every child table (the P0-5 lesson).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import web_api.routes.event_participants as epr
import web_api.routes.events as evr
from web_api import event_invite_candidates as cand
from web_api import event_scope
from web_api.common import ProblemException

from tests.unit.test_event_auth_modes import _ASSOC, _S, _SessionCM, _event, _team

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, *parts: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _OuterS(_S):
    """_S whose queries also accept outerjoin (the lifecycle's roster count)."""

    def query(self, *a, **k):
        q = super().query(*a, **k)
        q.outerjoin = lambda *a, **k: q
        return q


def _staff(**kw):
    kw.setdefault("mode", "clan_vs_clan")
    kw.setdefault("group_id", None)
    kw.setdefault("status", "draft")
    kw.setdefault("clan_roster_min", 2)
    kw.setdefault("clan_roster_max", 3)
    kw.setdefault("clan_roster_locked_at_start", True)
    kw.setdefault("per_group_discord", True)
    kw.setdefault("formation_mode", "signup_pool")
    return _event(**kw)


def _patch_roles(monkeypatch, *, superadmin=False, roles=None, managers=()):
    roles = roles or {}
    managers = set(managers)
    monkeypatch.setattr(evr, "load_user", lambda s, uid: "USER")
    monkeypatch.setattr(evr, "is_superadmin", lambda user: superadmin)
    monkeypatch.setattr(evr, "manageable_guild_ids", lambda uid: set())
    monkeypatch.setattr(evr, "resolve_group_role",
                        lambda s, uid, gid, mg, user=None: roles.get(gid))
    monkeypatch.setattr(evr, "is_event_manager", lambda s, uid, gid: gid in managers)

    def _assert_superadmin(user):
        if not superadmin:
            evr.abort_problem(403, "Forbidden", "Superadmins only.")

    monkeypatch.setattr(evr, "assert_superadmin", _assert_superadmin)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


# ── event_scope (pure) ────────────────────────────────────────────────────────

class TestEventScope:
    def test_only_hostless_clan_vs_clan_is_staff_hosted(self):
        assert event_scope.is_staff_hosted(_staff())
        assert not event_scope.is_staff_hosted(_staff(group_id=10))
        assert not event_scope.is_staff_hosted(_event(group_id=None))  # plain global

    @pytest.mark.parametrize("lo, hi, ok", [
        (None, None, True), (1, 1, True), (5, 10, True), (None, 8, True),
        (0, 5, False), (6, 5, False), (1, 501, False), (True, 5, False), ("3", 5, False),
    ])
    def test_validate_roster_limits(self, lo, hi, ok):
        assert (event_scope.validate_roster_limits(lo, hi) is None) is ok

    def test_capacity_problem(self):
        ev = _staff(clan_roster_max=3)
        assert event_scope.capacity_problem(ev, 2, 1) is None
        assert "full" in event_scope.capacity_problem(ev, 3, 1)
        assert "room for 1 more player" in event_scope.capacity_problem(ev, 2, 2)
        assert event_scope.capacity_problem(_staff(clan_roster_max=None), 99, 5) is None
        # Limits never bind on a group-hosted event.
        assert event_scope.capacity_problem(_staff(group_id=10), 3, 1) is None

    def test_roster_lock(self):
        assert not event_scope.clan_roster_locked(_staff(), "draft")
        assert event_scope.clan_roster_locked(_staff(), "active")
        assert not event_scope.clan_roster_locked(
            _staff(clan_roster_locked_at_start=False), "active")
        assert event_scope.clan_roster_locked(
            _staff(clan_roster_locked_at_start=False), "past")
        assert not event_scope.clan_roster_locked(_staff(group_id=10), "active")


# ── The event-admin gate is staff-only ────────────────────────────────────────

class TestEventAdminGate:
    def test_clan_owner_is_not_an_event_admin(self, monkeypatch):
        _patch_roles(monkeypatch, roles={10: "owner"})
        # No query at all: the staff-hosted check comes before the clan branch.
        assert evr._is_event_admin(_S(), 7, _staff()) is False
        with pytest.raises(ProblemException) as exc:
            evr._assert_event_admin(_S(), 7, _staff())
        assert exc.value.status == 403

    def test_superadmin_is(self, monkeypatch):
        _patch_roles(monkeypatch, superadmin=True)
        assert evr._is_event_admin(_S(), 7, _staff()) is True
        evr._assert_event_admin(_S(), 7, _staff())

    def test_group_hosted_clan_vs_clan_unchanged(self, monkeypatch):
        # An accepted opponent still co-manages an ordinary clan battle.
        _patch_roles(monkeypatch, roles={20: "admin"})
        evr._assert_event_admin(_S([(10,), (20,)]), 7, _staff(group_id=10))


class TestClanManagerGate:
    def test_manager_ids_are_accepted_clans_the_user_runs(self, monkeypatch):
        _patch_roles(monkeypatch, roles={10: "owner", 30: "admin"}, managers={20})
        s = _S([(10,), (20,), (40,)])  # accepted participants
        assert evr._clan_manager_group_ids(s, 7, _staff()) == {10, 20}

    def test_own_clan_passes_as_clan(self, monkeypatch):
        _patch_roles(monkeypatch, roles={10: "admin"})
        assert evr._assert_clan_manager(_S([(10,), (20,)]), 7, _staff(), 10) is False

    def test_rival_clan_is_refused(self, monkeypatch):
        _patch_roles(monkeypatch, roles={10: "admin"})
        with pytest.raises(ProblemException) as exc:
            evr._assert_clan_manager(_S([(10,), (20,)]), 7, _staff(), 20)
        assert exc.value.status == 403

    def test_roster_lock_applies_to_clans_not_staff(self, monkeypatch):
        live = _staff(status="active")
        _patch_roles(monkeypatch, roles={10: "admin"})
        with pytest.raises(ProblemException) as exc:
            evr._assert_clan_manager(_S([(10,)]), 7, live, 10, roster=True)
        assert exc.value.status == 409
        # Leadership/cosmetics don't pass roster=True, so they stay open.
        assert evr._assert_clan_manager(_S([(10,)]), 7, live, 10) is False
        _patch_roles(monkeypatch, superadmin=True)
        assert evr._assert_clan_manager(_S(), 7, live, 10, roster=True) is True

    def test_group_hosted_events_delegate_to_the_event_admin_gate(self, monkeypatch):
        calls = []
        monkeypatch.setattr(evr, "_assert_event_admin", lambda s, uid, ev: calls.append(ev.id))
        assert evr._assert_clan_manager(_S(), 7, _staff(group_id=10), 99) is True
        assert calls == [1]


# ── Roster capacity ───────────────────────────────────────────────────────────

class TestRosterCapacity:
    def _cap(self, monkeypatch, *, current, already=()):
        monkeypatch.setattr(evr, "_team_member_count", lambda s, tid: current)
        return _S([(pid,) for pid in already])

    def test_add_within_limit(self, monkeypatch):
        s = self._cap(monkeypatch, current=2)
        evr._assert_roster_capacity(s, _staff(), 4, [101])

    def test_add_past_limit_is_409(self, monkeypatch):
        s = self._cap(monkeypatch, current=3)
        with pytest.raises(ProblemException) as exc:
            evr._assert_roster_capacity(s, _staff(), 4, [101])
        assert exc.value.status == 409

    def test_players_already_on_the_team_are_not_additions(self, monkeypatch):
        s = self._cap(monkeypatch, current=3, already=[101])
        evr._assert_roster_capacity(s, _staff(), 4, [101])

    def test_bulk_is_all_or_nothing(self, monkeypatch):
        s = self._cap(monkeypatch, current=1)
        with pytest.raises(ProblemException):
            evr._assert_roster_capacity(s, _staff(), 4, [101, 102, 103])

    def test_override_and_other_events_skip_the_check(self, monkeypatch):
        evr._assert_roster_capacity(_S(), _staff(), 4, [101], override=True)
        evr._assert_roster_capacity(_S(), _staff(group_id=10), 4, [101])


class TestRosterRoutes:
    def _wire(self, monkeypatch, session, *, clan_ids=frozenset({10}), current=0):
        monkeypatch.setattr(evr, "current_user_id", lambda: 7)
        monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))
        monkeypatch.setattr(evr, "user_group_association", _ASSOC)
        monkeypatch.setattr(evr, "_bump", lambda *a, **k: None)
        monkeypatch.setattr(evr, "_assert_player_eligible", lambda *a, **k: None)
        monkeypatch.setattr(evr, "_event_membership", lambda *a, **k: None)
        monkeypatch.setattr(evr, "_sync_buyin_team", lambda *a, **k: None)
        monkeypatch.setattr(evr, "_mark_team_discord_dirty", lambda *a, **k: None)
        monkeypatch.setattr(evr, "_clan_manager_group_ids", lambda s, uid, ev: set(clan_ids))
        monkeypatch.setattr(evr, "_team_member_count", lambda s, tid: current)
        _patch_roles(monkeypatch)

    async def test_clan_leader_adds_to_own_team(self, client, monkeypatch):
        team = _team(4, group_id=10)
        # event, team, player, in-clan row, capacity "already on team"
        s = _S([_staff()], [team], [SimpleNamespace(player_id=101)], [(1,)], [])
        self._wire(monkeypatch, s, current=1)
        r = await client.post("/api/v1/events/1/teams/4/members", json={"player_id": 101})
        assert r.status_code == 200
        assert s.committed

    async def test_clan_leader_cannot_touch_a_rival_team(self, client, monkeypatch):
        s = _S([_staff()], [_team(4, group_id=20)])
        self._wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/teams/4/members", json={"player_id": 101})
        assert r.status_code == 403
        assert not s.committed

    async def test_full_roster_is_refused(self, client, monkeypatch):
        s = _S([_staff()], [_team(4, group_id=10)], [SimpleNamespace(player_id=101)],
               [(1,)], [])
        self._wire(monkeypatch, s, current=3)
        r = await client.post("/api/v1/events/1/teams/4/members", json={"player_id": 101})
        assert r.status_code == 409
        assert (await r.get_json())["code"] == "roster_full"
        assert not s.committed

    async def test_clan_leader_cannot_override_the_limit(self, client, monkeypatch):
        s = _S([_staff()], [_team(4, group_id=10)], [SimpleNamespace(player_id=101)],
               [(1,)], [])
        self._wire(monkeypatch, s, current=3)
        r = await client.post("/api/v1/events/1/teams/4/members",
                              json={"player_id": 101, "override": True})
        assert r.status_code == 409

    async def test_clan_leader_cannot_edit_the_event(self, client, monkeypatch):
        s = _S([_staff()])
        self._wire(monkeypatch, s)
        r = await client.patch("/api/v1/events/1", json={"name": "Hijacked"})
        assert r.status_code == 403
        assert not s.committed

    async def test_clan_leader_restyles_own_team(self, client, monkeypatch):
        team = _team(4, group_id=10, name="Old")
        s = _S([_staff()], [team])
        self._wire(monkeypatch, s)
        monkeypatch.setattr(evr, "_sync_team_discord", lambda *a, **k: None)
        r = await client.patch("/api/v1/events/1/teams/4", json={"name": "New"})
        assert r.status_code == 200
        assert team.name == "New"


# ── Participants: accept creates the team, withdraw, staff bulk cap ──────────

class TestParticipants:
    def _wire(self, monkeypatch, session, user_id=7):
        monkeypatch.setattr(epr, "current_user_id", lambda: user_id)
        monkeypatch.setattr(epr, "db_session", lambda: _SessionCM(session))
        monkeypatch.setattr(epr, "_assert_admin_of_group", lambda *a, **k: None)
        monkeypatch.setattr(epr, "_sync_event_guilds", lambda *a, **k: None)
        import services.event_invites as invites

        for name in ("announce_invite", "announce_response", "announce_withdrawal",
                     "announce_status_change"):
            monkeypatch.setattr(invites, name, lambda *a, **k: None)

    async def test_accept_gives_the_clan_its_team(self, client, monkeypatch):
        row = SimpleNamespace(status="invited", group_id=20, id=5)
        s = _S([_staff()], [row])
        self._wire(monkeypatch, s)
        made = []
        monkeypatch.setattr(epr, "_ensure_clan_team", lambda s, ev, gid: made.append(gid))
        r = await client.post("/api/v1/events/1/participants/20/accept", json={})
        assert r.status_code == 200
        assert row.status == "accepted"
        assert made == [20]

    async def test_accept_on_group_hosted_event_makes_no_team(self, client, monkeypatch):
        row = SimpleNamespace(status="invited", group_id=20, id=5)
        s = _S([_staff(group_id=10)], [row])
        self._wire(monkeypatch, s)
        made = []
        monkeypatch.setattr(epr, "_ensure_clan_team", lambda s, ev, gid: made.append(gid))
        r = await client.post("/api/v1/events/1/participants/20/accept", json={})
        assert r.status_code == 200
        assert made == []

    async def test_withdraw_before_start(self, client, monkeypatch):
        row = SimpleNamespace(status="accepted", group_id=20, id=5, responded_at=None)
        s = _S([_staff()], [row])
        self._wire(monkeypatch, s)
        purged = []
        monkeypatch.setattr(epr, "_purge_clan_teams", lambda s, ev, gid: purged.append(gid))
        r = await client.post("/api/v1/events/1/participants/20/withdraw")
        assert r.status_code == 200
        assert row.status == "withdrawn"
        assert purged == [20]
        assert s.committed

    async def test_withdraw_after_start_is_refused(self, client, monkeypatch):
        s = _S([_staff(status="active")])
        self._wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/participants/20/withdraw")
        assert r.status_code == 409

    async def test_withdraw_only_on_staff_hosted(self, client, monkeypatch):
        s = _S([_staff(group_id=10)])
        self._wire(monkeypatch, s)
        r = await client.post("/api/v1/events/1/participants/20/withdraw")
        assert r.status_code == 422

    async def test_bulk_cap_is_higher_for_staff_hosted(self, client, monkeypatch):
        ids = list(range(100, 140))  # 40 clans: over the normal cap of 30
        s = _S([_staff()], [], [(g, f"Clan {g}") for g in ids])
        self._wire(monkeypatch, s)
        monkeypatch.setattr(epr, "_assert_event_admin", lambda *a, **k: None)
        r = await client.post("/api/v1/events/1/participants/bulk", json={"group_ids": ids})
        assert r.status_code == 200
        assert len((await r.get_json())["invited"]) == 40

        s = _S([_staff(group_id=10)])
        self._wire(monkeypatch, s)
        monkeypatch.setattr(epr, "_assert_event_admin", lambda *a, **k: None)
        r = await client.post("/api/v1/events/1/participants/bulk", json={"group_ids": ids})
        assert r.status_code == 422


# ── The shared team cascade ───────────────────────────────────────────────────

class TestPurgeTeam:
    def test_every_child_table_is_cleared_then_the_team(self, monkeypatch):
        models = MagicMock()
        monkeypatch.setitem(sys.modules, "db", models)
        released = []
        buyins = SimpleNamespace(
            release_team_buyins=lambda s, eid, tid: released.append((eid, tid)))
        monkeypatch.setitem(sys.modules, "services.event_buyins", buyins)
        monkeypatch.setitem(sys.modules, "services.event_team_discord", SimpleNamespace(
            enqueue_team_discord_orphans=lambda *a: None,
            orphan_team_discord_payloads=lambda *a: [],
        ))
        purge = _load("_event_team_purge_under_test", "services", "event_team_purge.py")

        queried, deleted = [], []

        class _Session:
            def query(self, model):
                queried.append(model)
                return self

            def filter(self, *a):
                return self

            def delete(self, obj=None, **k):
                if obj is not None and not k:
                    deleted.append(obj)
                return 0

            def flush(self):
                pass

        team = SimpleNamespace(id=4)
        purge.purge_team(_Session(), 1, team)
        expected = [models.EventTeamDiscord, models.EventBingoCompletion,
                    models.EventCompletion, models.EventProgress, models.EventTeamMember,
                    models.EventPlayerPoints, models.EventLeaderVote,
                    models.EventBoardPosition, models.EventTeamInventory,
                    models.EventTeamCooldown, models.EventCoinLedger,
                    models.EventBoardEffect]
        assert queried == expected
        assert released == [(1, 4)]
        assert deleted == [team]


# ── Invite candidates (pure) ──────────────────────────────────────────────────

def _stat(gid, **kw):
    base = dict(group_id=gid, group_name=f"Clan {gid}", icon_url=None, members=50,
                active=20, monthly_loot=1_000_000, admins=2, has_guild=True)
    base.update(kw)
    return base


class TestInviteCandidates:
    def test_count_members_dedupes_and_intersects(self):
        pairs = [(1, 10), (1, 10), (1, 11), (2, 11), (2, None), (None, 12)]
        assert cand.count_members(pairs, {11}) == {1: (2, 1), 2: (1, 1)}

    def test_filters(self):
        stats = [_stat(1), _stat(2, members=5), _stat(3, admins=0),
                 _stat(4, has_guild=False), _stat(5, name="x", group_name="Zeta"),
                 _stat(2 + 0, group_id=2)]
        f = cand.parse_filters({"min_members": "10", "require_guild": "1"})
        got = {r["group_id"] for r in cand.select_candidates(stats, f)["rows"]}
        # 2 = the global group (never offered), 3 = no admins, 4 = no guild.
        assert got == {1, 5}

    def test_clans_already_on_the_event_are_hidden_by_default(self):
        stats = [_stat(10), _stat(11), _stat(12)]
        status = {10: "invited", 11: "declined"}
        f = cand.parse_filters({})
        rows = cand.select_candidates(stats, f, status)["rows"]
        assert {r["group_id"] for r in rows} == {11, 12}
        assert next(r for r in rows if r["group_id"] == 11)["event_status"] == "declined"
        f = cand.parse_filters({"exclude_on_event": "0"})
        assert len(cand.select_candidates(stats, f, status)["rows"]) == 3

    def test_sort_and_limit(self):
        stats = [_stat(10, active=5), _stat(11, active=50), _stat(12, active=20)]
        res = cand.select_candidates(stats, cand.parse_filters({"limit": "2"}))
        assert [r["group_id"] for r in res["rows"]] == [11, 12]
        assert res["total"] == 3
        f = cand.parse_filters({"sort": "bogus"})
        assert f["sort"] == "active"


# ── Lifecycle: the start-of-event drop ────────────────────────────────────────

class TestDropUnderfilled:
    @pytest.fixture()
    def lc(self):
        return _load("_event_lifecycle_staff_under_test", "services", "event_lifecycle.py")

    def test_group_hosted_or_no_minimum_drops_nobody(self, lc):
        assert lc.underfilled_clans(None, _staff(group_id=10)) == []
        assert lc.underfilled_clans(None, _staff(clan_roster_min=None)) == []

    def test_underfilled_is_accepted_clans_below_the_minimum(self, lc):
        rows = [SimpleNamespace(group_id=10), SimpleNamespace(group_id=20),
                SimpleNamespace(group_id=30)]
        s = _OuterS([(10, 3), (20, 1)], rows)  # counts per clan, then accepted rows
        got = lc.underfilled_clans(s, _staff(clan_roster_min=2))
        assert [(r.group_id, n) for r, n in got] == [(20, 1), (30, 0)]

    def test_drop_marks_purges_and_notifies(self, lc, monkeypatch):
        row = SimpleNamespace(group_id=20, status="accepted", responded_at=None)
        monkeypatch.setattr(lc, "underfilled_clans", lambda s, ev: [(row, 1)])
        purged, notes = [], []
        monkeypatch.setitem(sys.modules, "services.event_team_purge", SimpleNamespace(
            purge_team=lambda s, eid, team: purged.append(team.id)))
        import services.event_invites as invites

        monkeypatch.setattr(invites, "announce_status_change",
                            lambda s, **k: notes.append((k["code"], k["commit"])))
        s = _S([_team(8, group_id=20)], [])  # the clan's team, its sign-ups
        dropped = lc.drop_underfilled_clans(s, _staff(), actor_user_id=7,
                                            now=datetime(2026, 9, 24))
        assert dropped == [20]
        assert row.status == "dropped"
        assert purged == [8]
        # Rides the activation transaction: no commit of its own.
        assert notes == [("clan_dropped", False)]
        assert not s.committed


# ── Invite wording ────────────────────────────────────────────────────────────

class TestInviteEmbed:
    def test_staff_variant(self):
        import services.event_invites as invites

        ev = SimpleNamespace(id=9, name="Autumn Clash", group_id=None, mode="clan_vs_clan",
                             starts_at=None, ends_at=None, description=None,
                             clan_roster_min=5, clan_roster_max=10)
        embed = invites.build_invite_embed(event=ev, host_group_name=None,
                                           invited_group_name="Realists")
        assert embed["title"] == "Global event: Autumn Clash"
        assert "The DropTracker" in embed["description"]
        assert {"name": "Players per clan", "value": "5 to 10", "inline": True} in embed["fields"]

    def test_roster_size_text(self):
        import services.event_invites as invites

        assert invites.roster_size_text(None, None) is None
        assert invites.roster_size_text(5, None) == "at least 5"
        assert invites.roster_size_text(None, 8) == "up to 8"
        assert invites.roster_size_text(4, 4) == "exactly 4"
