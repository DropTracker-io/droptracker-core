"""Integration scenario for clan-vs-clan events (Implementation Plan B, §9 flow).

Run standalone (NOT collected by pytest — see test_event_cvc_db.py which
invokes it as a subprocess so the unit-test sys.modules stubs never apply):

    cd /store/droptracker/disc && venv/bin/python tests/integration/event_cvc_it.py

Uses the ``dt_migrate_test`` MySQL schema (creds from alembic.ini) inside ONE
transaction that is rolled back at the end. The prod ``data`` DB is never
touched. Covers the flow the routes can't reach in unit tests: participant
roster semantics on real rows, activation blockers gating on 2 accepted clans
+ per-clan teams, clan-bound teams scoring through the *unchanged* engine, and
the dual-guild desired-state rows.
"""
import configparser
import os
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# Real env (the pytest wrapper may hand us conftest's fake creds).
from dotenv import dotenv_values  # noqa: E402
_env = dotenv_values(os.path.join(ROOT, ".env"))
for _k in ("DB_USER", "DB_PASS"):
    if _env.get(_k):
        os.environ[_k] = _env[_k]

import redis as _redis  # noqa: E402
from sqlalchemy import create_engine, delete, insert  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import (  # noqa: E402
    Event, EventGroup, EventGuild, EventTask, EventTeam, EventTeamMember,
    Group, Player, user_group_association,
)
from services import event_engine, event_lifecycle  # noqa: E402
from services.event_scheduled_events import sync_event_guilds  # noqa: E402

TEST_DB = "dt_migrate_test"


def _test_engine():
    ini = configparser.ConfigParser()
    ini.read(os.path.join(ROOT, "alembic.ini"))
    url = ini.get("alembic", "sqlalchemy.url")
    base, _, _dbname = url.rpartition("/")
    assert TEST_DB != "data"
    return create_engine(f"{base}/{TEST_DB}")


def _redis_conn():
    return _redis.Redis(host="127.0.0.1", port=6379, db=0,
                        password=os.environ.get("DB_PASS"))


_guid_counter = [0]


def env(kind, data, player_id=None, ts=None):
    _guid_counter[0] += 1
    return {"v": 1, "kind": kind, "guid": f"cvcit-{os.getpid()}-{_guid_counter[0]}",
            "player_id": player_id, "player_name": "CvcIT", "ts": int(ts),
            "data": data}


def auto_clan_reconcile_scenario(session, pid_salt, now):
    """Whole-clan (auto_clan) rosters mirror clan membership over the event's
    life: activation materializes the current clan onto its team, and each
    later sync adds players who joined the clan and removes players who left —
    the behaviour a whole-clan event promises its organisers."""
    ca = Group("CvC AC A", 95_000_000 + pid_salt, str(700_000 + pid_salt))
    cb = Group("CvC AC B", 96_000_000 + pid_salt, str(800_000 + pid_salt))
    session.add_all([ca, cb])
    session.flush()

    players = []
    for i in range(4):
        p = Player(wom_id=97_000_000 + pid_salt * 10 + i, player_name=f"ACp{i}",
                   account_hash=f"cvcac-{pid_salt}-{i}")
        players.append(p)
    session.add_all(players)
    session.flush()
    a_members = [players[0].player_id, players[1].player_id, players[2].player_id]
    b_members = [players[3].player_id]
    for pid in a_members:
        session.execute(insert(user_group_association).values(
            player_id=pid, group_id=ca.group_id))
    for pid in b_members:
        session.execute(insert(user_group_association).values(
            player_id=pid, group_id=cb.group_id))

    # clan-vs-clan event with NO teams → whole-clan (auto_clan) mode.
    ev = Event(name="CvC AC event", status="draft", group_id=ca.group_id,
               mode="clan_vs_clan", formation_mode="admin_assign",
               starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1),
               has_bingo=False, board_size=5)
    session.add(ev)
    session.flush()
    session.add(EventGroup(event_id=ev.id, group_id=ca.group_id, role="host",
                           status="accepted", responded_at=now))
    session.add(EventGroup(event_id=ev.id, group_id=cb.group_id, role="opponent",
                           status="accepted", responded_at=now))
    ev.activated_at = now
    ev.status = "active"
    session.flush()

    def roster_for(gid):
        return {pid for (pid,) in
                session.query(EventTeamMember.player_id)
                .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                .filter(EventTeam.event_id == ev.id, EventTeam.group_id == gid).all()}

    # Activation seeds one auto_clan team per clan and materializes the roster.
    assert event_lifecycle._ensure_whole_clan_teams(session, ev) == 2
    event_lifecycle.sync_auto_clan_rosters(session, ev, now=now)
    assert roster_for(ca.group_id) == set(a_members), roster_for(ca.group_id)
    assert roster_for(cb.group_id) == set(b_members), roster_for(cb.group_id)

    # JOIN mid-event: a new player joins clan A → next reconcile adds them.
    newp = Player(wom_id=98_000_000 + pid_salt, player_name="ACnew",
                  account_hash=f"cvcac-new-{pid_salt}")
    session.add(newp)
    session.flush()
    session.execute(insert(user_group_association).values(
        player_id=newp.player_id, group_id=ca.group_id))
    session.flush()
    assert event_lifecycle.sync_auto_clan_rosters(session, ev, now=now) == 1
    assert newp.player_id in roster_for(ca.group_id)

    # LEAVE mid-event: an existing member leaves clan A → next reconcile removes
    # them (their team row goes; any completions would stand).
    session.execute(delete(user_group_association).where(
        user_group_association.c.group_id == ca.group_id,
        user_group_association.c.player_id == a_members[0]))
    session.flush()
    assert event_lifecycle.sync_auto_clan_rosters(session, ev, now=now) == 1
    assert a_members[0] not in roster_for(ca.group_id)
    assert roster_for(ca.group_id) == {a_members[1], a_members[2], newp.player_id}

    # A settled roster is a no-op (idempotent).
    assert event_lifecycle.sync_auto_clan_rosters(session, ev, now=now) == 0
    print("AUTO_CLAN RECONCILE ASSERTIONS PASSED")


def main():
    engine_db = _test_engine()
    Session = sessionmaker(bind=engine_db)
    session = Session()
    r = _redis_conn()
    r.ping()
    event_id = None
    try:
        now = datetime.now()
        joined = now - timedelta(hours=1)
        ts_now = int(time.time())
        pid_salt = os.getpid()

        # ── Two clans, one player in each ────────────────────────────────────
        host = Group("CvC IT Host", 91_000_000 + pid_salt, str(500_000 + pid_salt))
        opp = Group("CvC IT Opp", 92_000_000 + pid_salt, str(600_000 + pid_salt))
        session.add_all([host, opp])
        session.flush()

        p_host = Player(wom_id=93_000_000 + pid_salt, player_name="CvcITHost",
                        account_hash=f"cvcit-h-{pid_salt}")
        p_opp = Player(wom_id=94_000_000 + pid_salt, player_name="CvcITOpp",
                       account_hash=f"cvcit-o-{pid_salt}")
        session.add_all([p_host, p_opp])
        session.flush()
        session.execute(insert(user_group_association).values(
            player_id=p_host.player_id, group_id=host.group_id))
        session.execute(insert(user_group_association).values(
            player_id=p_opp.player_id, group_id=opp.group_id))

        # ── Draft clan-vs-clan event; host seeded accepted ────────────────────
        # discord_event_policy="immediate" so the mirror is desired while still
        # a draft (the default on_activate would desire nothing until activation).
        ev = Event(name="CvC IT event", status="draft", group_id=host.group_id,
                   mode="clan_vs_clan", formation_mode="self_join",
                   starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1),
                   has_bingo=False, requires_confirmation=False, board_size=5,
                   bonus_line_points=0, bonus_blackout_points=0,
                   discord_event_policy="immediate",
                   discord_guild_id=str(host.guild_id))
        session.add(ev)
        session.flush()
        event_id = ev.id
        session.add(EventGroup(event_id=ev.id, group_id=host.group_id, role="host",
                               status="accepted", responded_at=now))
        # mirror_discord_event=True: the opponent opted in to mirroring the
        # scheduled event into its own guild (accept-time checkbox).
        session.add(EventGroup(event_id=ev.id, group_id=opp.group_id,
                               role="opponent", status="invited",
                               mirror_discord_event=True))
        session.flush()

        def accepted_ids():
            return {gid for (gid,) in session.query(EventGroup.group_id).filter(
                EventGroup.event_id == ev.id, EventGroup.status == "accepted").all()}

        assert accepted_ids() == {host.group_id}, "only the host before acceptance"

        # ── Activation blocked: one accepted clan, no teams ──────────────────
        blockers = event_lifecycle.activation_blockers(session, ev, now=now)
        assert any("two accepted clans" in b for b in blockers), blockers

        # ── Opponent accepts ─────────────────────────────────────────────────
        invite = session.query(EventGroup).filter(
            EventGroup.event_id == ev.id, EventGroup.group_id == opp.group_id).one()
        invite.status = "accepted"
        invite.responded_at = now
        session.flush()
        assert accepted_ids() == {host.group_id, opp.group_id}

        # Teams are optional for clan-vs-clan: with two accepted clans and no
        # teams, the event is ready — activation seeds a whole-clan team per
        # clan so it runs anyone-vs-anyone.
        assert event_lifecycle.activation_blockers(session, ev, now=now) == []

        # ── Half-built roster is ambiguous: one clan has a team, one doesn't ──
        team_h = EventTeam(event_id=ev.id, name="Hosts", score=0, group_id=host.group_id)
        session.add(team_h)
        session.flush()
        blockers = event_lifecycle.activation_blockers(session, ev, now=now)
        assert any("at least one team" in b for b in blockers), blockers

        # ── Both clans get a team; blockers clear ────────────────────────────
        team_o = EventTeam(event_id=ev.id, name="Opps", score=0, group_id=opp.group_id)
        session.add(team_o)
        session.flush()
        assert event_lifecycle.activation_blockers(session, ev, now=now) == []

        # ── Dual-guild desired state: both clans' guilds ─────────────────────
        sync_event_guilds(session, ev)
        session.flush()
        guild_rows = {g.guild_id: g.sync_status for g in
                      session.query(EventGuild).filter(EventGuild.event_id == ev.id)}
        assert guild_rows == {str(host.guild_id): "pending",
                              str(opp.guild_id): "pending"}, guild_rows

        # ── Roster: each player on their own clan's team ─────────────────────
        session.add(EventTeamMember(team_id=team_h.id, player_id=p_host.player_id,
                                    joined_at=joined))
        session.add(EventTeamMember(team_id=team_o.id, player_id=p_opp.player_id,
                                    joined_at=joined))
        t_item = EventTask(event_id=ev.id, type="item_collection", label="1 whip",
                           target="Abyssal whip", target_value=1, points=10)
        session.add(t_item)
        ev.status = "active"  # engine only matches active events
        session.flush()

        # ── Scoring through the UNCHANGED engine: credit lands on the
        #    opponent clan's team, host team untouched ──────────────────────
        state = event_engine.load_matcher_state(session)
        assert ev.id in state.events
        res = event_engine.handle_envelope(
            session, r, state,
            env("drop", {"item_name": "Abyssal whip", "quantity": 1,
                         "npc_name": "Abyssal demon", "kill_count": 1},
                player_id=p_opp.player_id, ts=ts_now))
        assert [x["kind"] for x in res] == ["completion"], res
        session.refresh(team_o)
        session.refresh(team_h)
        assert team_o.score == 10, team_o.score
        assert team_h.score == 0, team_h.score

        # ── Whole-clan (auto_clan) roster reconcile: join adds, leave removes ─
        auto_clan_reconcile_scenario(session, pid_salt, now)

        # ── End: guild rows retired for BOTH guilds ──────────────────────────
        event_lifecycle.end_event(session, ev, now=now)
        session.flush()
        guild_rows = {g.guild_id: g.sync_status for g in
                      session.query(EventGuild).filter(EventGuild.event_id == ev.id)}
        assert set(guild_rows.values()) == {"delete_pending"}, guild_rows
        assert ev.status == "past"

        print("ALL CLAN-VS-CLAN INTEGRATION ASSERTIONS PASSED")
    finally:
        session.rollback()
        session.close()
        try:
            if event_id is not None:
                r.srem(event_engine.ACTIVE_EVENTS_KEY, int(event_id))
                r.delete(f"rt:event:{event_id}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
