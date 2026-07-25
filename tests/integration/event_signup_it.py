"""Integration scenario for the event sign-up pool (services/event_signup.py).

Run standalone (NOT collected by pytest — see test_event_signup_db.py which
invokes it as a subprocess so the unit-test sys.modules stubs never apply):

    cd /store/droptracker/disc && venv/bin/python tests/integration/event_signup_it.py

Exercises the real service functions against ``dt_migrate_test`` inside ONE
rolled-back transaction: self-service sign-up in each formation mode, the
one-RSN-per-user rule, clan-aware pool randomize/assign, and withdrawal.
"""
import configparser
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402
_env = dotenv_values(os.path.join(ROOT, ".env"))
for _k in ("DB_USER", "DB_PASS"):
    if _env.get(_k):
        os.environ[_k] = _env[_k]

from sqlalchemy import create_engine, insert  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import (  # noqa: E402
    Event, EventGroup, EventSignup, EventTeam, EventTeamMember, Group, Player,
    User, user_group_association,
)
from services import event_signup as sus  # noqa: E402

TEST_DB = "dt_migrate_test"


def _engine():
    ini = configparser.ConfigParser()
    ini.read(os.path.join(ROOT, "alembic.ini"))
    base, _, _db = ini.get("alembic", "sqlalchemy.url").rpartition("/")
    assert TEST_DB != "data"
    return create_engine(f"{base}/{TEST_DB}")


def _team_of(session, event_id, player_id):
    row = (session.query(EventTeamMember.team_id)
           .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
           .filter(EventTeam.event_id == event_id,
                   EventTeamMember.player_id == player_id).first())
    return row[0] if row else None


def main():
    session = sessionmaker(bind=_engine())()
    salt = os.getpid()
    try:
        now = datetime.now()

        host = Group("SU Host", 71_000_000 + salt, str(710_000 + salt))
        opp = Group("SU Opp", 72_000_000 + salt, str(720_000 + salt))
        session.add_all([host, opp]); session.flush()

        # A user with TWO linked accounts (for the one-RSN test), both in host.
        u = User(username="suit", discord_id=str(70_000_000 + salt),
                 auth_token=f"su{salt}"[:16])
        session.add(u); session.flush()
        p_a = Player(wom_id=73_000_000 + salt, player_name="SuA",
                     account_hash=f"su-a-{salt}", user=u)
        p_b = Player(wom_id=74_000_000 + salt, player_name="SuB",
                     account_hash=f"su-b-{salt}", user=u)
        # A second user in the opponent clan.
        u2 = User(username="suit2", discord_id=str(70_500_000 + salt),
                  auth_token=f"su2{salt}"[:16])
        session.add(u2); session.flush()
        p_c = Player(wom_id=75_000_000 + salt, player_name="SuC",
                     account_hash=f"su-c-{salt}", user=u2)
        session.add_all([p_a, p_b, p_c]); session.flush()
        for pid, gid, uid in [(p_a.player_id, host.group_id, u.user_id),
                              (p_b.player_id, host.group_id, u.user_id),
                              (p_c.player_id, opp.group_id, u2.user_id)]:
            session.execute(insert(user_group_association).values(
                player_id=pid, group_id=gid, user_id=uid))

        # ── Clan-vs-clan event in signup_pool mode ───────────────────────────
        # allow_late_signups: this scenario signs players up while the event is
        # already running, which web70a closes by default (sign-ups end at the
        # start unless the event opts in). The window itself is unit-tested.
        ev = Event(name="SU pool", status="active", group_id=host.group_id,
                   mode="clan_vs_clan", formation_mode="signup_pool",
                   starts_at=now - timedelta(hours=1), ends_at=now + timedelta(days=1),
                   has_bingo=False, board_size=5, allow_late_signups=True)
        session.add(ev); session.flush()
        session.add_all([
            EventGroup(event_id=ev.id, group_id=host.group_id, role="host",
                       status="accepted", responded_at=now),
            EventGroup(event_id=ev.id, group_id=opp.group_id, role="opponent",
                       status="accepted", responded_at=now),
        ])
        host_t1 = EventTeam(event_id=ev.id, name="Host A", score=0, group_id=host.group_id)
        host_t2 = EventTeam(event_id=ev.id, name="Host B", score=0, group_id=host.group_id)
        opp_t1 = EventTeam(event_id=ev.id, name="Opp A", score=0, group_id=opp.group_id)
        session.add_all([host_t1, host_t2, opp_t1]); session.flush()

        # ── Sign-ups (pool → no team yet) ────────────────────────────────────
        r = sus.perform_signup(session, ev, p_a, u.user_id, source="web")
        assert r == {"team_id": None, "pooled": True}, r
        sus.perform_signup(session, ev, p_c, u2.user_id, source="discord")
        session.flush()
        assert _team_of(session, ev.id, p_a.player_id) is None, "pool signup places no team"

        # One RSN per user: p_b (same user as p_a) is refused.
        try:
            sus.perform_signup(session, ev, p_b, u.user_id)
            raise AssertionError("expected one-RSN SignupError")
        except sus.SignupError as e:
            assert e.status == 409, e.status

        pool = sus.list_pool(session, ev)
        assert {row["player_id"] for row in pool} == {p_a.player_id, p_c.player_id}
        assert all(row["team_id"] is None for row in pool), "pool starts unassigned"

        # ── Randomize: each player lands on THEIR clan's team ────────────────
        res = sus.randomize_pool(session, ev)
        session.flush()
        assert res["assigned"] == 2, res
        a_team = _team_of(session, ev.id, p_a.player_id)
        c_team = _team_of(session, ev.id, p_c.player_id)
        assert a_team in (host_t1.id, host_t2.id), "host player on a host team"
        assert c_team == opp_t1.id, "opp player on the opp team"

        # Re-randomize is idempotent in clan (still valid placement).
        sus.randomize_pool(session, ev); session.flush()
        assert _team_of(session, ev.id, p_a.player_id) in (host_t1.id, host_t2.id)

        # ── Manual assign: wrong clan refused, right clan works ──────────────
        try:
            sus.assign_from_pool(session, ev, p_c.player_id, host_t1.id)
            raise AssertionError("expected wrong-clan SignupError")
        except sus.SignupError as e:
            assert e.status == 422, e.status
        sus.assign_from_pool(session, ev, p_a.player_id, host_t2.id); session.flush()
        assert _team_of(session, ev.id, p_a.player_id) == host_t2.id

        # ── Withdraw removes signup + placement ──────────────────────────────
        sus.remove_signup(session, ev, p_a.player_id); session.flush()
        assert _team_of(session, ev.id, p_a.player_id) is None
        assert session.query(EventSignup).filter(
            EventSignup.event_id == ev.id,
            EventSignup.player_id == p_a.player_id).first() is None

        # ── self_join mode: immediate placement onto chosen team ─────────────
        ev.formation_mode = "self_join"
        session.flush()
        r = sus.perform_signup(session, ev, p_a, u.user_id, team_id=host_t1.id)
        session.flush()
        assert r["team_id"] == host_t1.id and not r["pooled"], r
        assert _team_of(session, ev.id, p_a.player_id) == host_t1.id

        print("ALL EVENT SIGNUP INTEGRATION ASSERTIONS PASSED")
    finally:
        session.rollback()
        session.close()


if __name__ == "__main__":
    main()
