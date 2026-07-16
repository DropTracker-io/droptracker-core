"""Integration scenario for the event completion engine (Task 17).

Run standalone (NOT collected by pytest — see test_event_engine_db.py which
invokes it as a subprocess so the unit-test sys.modules stubs never apply):

    cd /store/droptracker/disc && venv/bin/python tests/integration/event_engine_it.py

Uses the fully-migrated ``dt_migrate_test`` MySQL schema (creds from
alembic.ini) inside ONE transaction that is rolled back at the end, plus real
Redis with per-event throwaway keys that are deleted afterwards. The prod
``data`` DB is never touched.
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
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import (  # noqa: E402
    Event, EventBingoCell, EventBingoCompletion, EventCompletion,
    EventPlayerPoints, EventProgress, EventTask, EventTeam, EventTeamMember,
    NotificationQueue, Player,
)
from services import event_engine  # noqa: E402

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


def env(kind, data, guid=None, player_id=None, ts=None):
    if guid is None:
        _guid_counter[0] += 1
        guid = f"evtit-{os.getpid()}-{_guid_counter[0]}"
    return {"v": 1, "kind": kind, "guid": guid, "player_id": player_id,
            "player_name": "EventEngineIT", "ts": int(ts), "data": data}


def notifications(session, ntype, player_id):
    return (session.query(NotificationQueue)
            .filter(NotificationQueue.notification_type == ntype,
                    NotificationQueue.player_id == player_id)
            .all())


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

        player = Player(wom_id=99_000_000 + os.getpid(), player_name="EvtEngineIT",
                        account_hash=f"evtit-{os.getpid()}")
        session.add(player)
        session.flush()
        pid = player.player_id

        ev = Event(name="IT event", status="active", group_id=None,
                   starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1),
                   has_bingo=True, formation_mode="admin_assign",
                   requires_confirmation=False, board_size=5,
                   bonus_line_points=0, bonus_blackout_points=0)
        session.add(ev)
        session.flush()
        event_id = ev.id

        t_item = EventTask(event_id=ev.id, type="item_collection", label="2 whips",
                           target="Abyssal whip", target_value=2, points=10)
        t_pending = EventTask(event_id=ev.id, type="item_collection", label="d pick",
                              target="Dragon pickaxe", target_value=1, points=5,
                              requires_confirmation=True)
        t_kc = EventTask(event_id=ev.id, type="kc_target", label="2 zulrah kc",
                         target="Zulrah", target_value=2, points=7)
        t_pb = EventTask(event_id=ev.id, type="pb_target", label="sub-60s zulrah",
                         target="Zulrah", target_value=60, points=3)
        t_xp = EventTask(event_id=ev.id, type="xp_target", label="1k slayer xp",
                         target="Slayer", target_value=1000, points=2)
        t_skill = EventTask(event_id=ev.id, type="skill_target", label="90 agility",
                            target="Agility", target_value=90, points=1)
        session.add_all([t_item, t_pending, t_kc, t_pb, t_xp, t_skill])

        team_a = EventTeam(event_id=ev.id, name="A", score=0)
        team_b = EventTeam(event_id=ev.id, name="B", score=15)  # initial leader
        session.add_all([team_a, team_b])
        session.flush()
        session.add(EventTeamMember(team_id=team_a.id, player_id=pid, joined_at=joined))
        cell = EventBingoCell(event_id=ev.id, idx=0, label="2 whips", task_id=t_item.id)
        session.add(cell)
        session.flush()

        state = event_engine.load_matcher_state(session)
        assert ev.id in state.events, "active event loaded"
        assert pid in state.participants, "roster loaded"
        assert len(state.tasks_by_event[ev.id]) == 6
        assert state.cells_by_task[t_item.id][0]["idx"] == 0

        def handle(e):
            return event_engine.handle_envelope(session, r, state, e)

        def ledger(task):
            return session.query(EventCompletion).filter(
                EventCompletion.task_id == task.id).all()

        def prog(task):
            return session.query(EventProgress).filter(
                EventProgress.task_id == task.id,
                EventProgress.team_id == team_a.id).first()

        # 1. item_collection: first whip = progress only
        res = handle(env("drop", {"item_name": "Abyssal whip", "quantity": 1,
                                  "npc_name": "Abyssal demon", "kill_count": 5},
                         player_id=pid, ts=ts_now))
        assert [x["kind"] for x in res] == ["progress"], res
        assert prog(t_item).progress == 1 and not prog(t_item).completed

        # 2. second whip completes: score, bingo cell, notification
        dup_guid = "evtit-dup-guid"
        res = handle(env("drop", {"item_name": "abyssal WHIP", "quantity": 1,
                                  "npc_name": "Abyssal demon", "kill_count": 6},
                         guid=dup_guid, player_id=pid, ts=ts_now))
        assert [x["kind"] for x in res] == ["completion"], res
        assert prog(t_item).completed and prog(t_item).progress == 2
        session.refresh(team_a)
        assert team_a.score == 10, team_a.score
        cells_done = session.query(EventBingoCompletion).filter(
            EventBingoCompletion.cell_id == cell.id,
            EventBingoCompletion.team_id == team_a.id).all()
        assert len(cells_done) == 1, "bingo cell completed once"
        assert len(notifications(session, "event_completion", pid)) == 1
        # Contribution points: sole contributor gets the full 10 as a float.
        ppoints = session.query(EventPlayerPoints).filter(
            EventPlayerPoints.task_id == t_item.id,
            EventPlayerPoints.team_id == team_a.id).all()
        assert len(ppoints) == 1 and ppoints[0].player_id == pid
        assert abs(float(ppoints[0].points) - 10.0) < 1e-9, ppoints[0].points

        # 2b. post-completion whip: dropped entirely — no ledger row, no
        # popup, no contribution pollution.
        res = handle(env("drop", {"item_name": "Abyssal whip", "quantity": 1,
                                  "npc_name": "Abyssal demon", "kill_count": 7},
                         player_id=pid, ts=ts_now))
        assert res == [], "completed task must not keep recording"
        assert len(ledger(t_item)) == 2, "no post-completion ledger rows"

        # 3. replay same guid = idempotent no-op
        res = handle(env("drop", {"item_name": "Abyssal whip", "quantity": 1,
                                  "npc_name": "Abyssal demon", "kill_count": 6},
                         guid=dup_guid, player_id=pid, ts=ts_now))
        assert res == [], "duplicate guid must be a no-op"
        assert len(ledger(t_item)) == 2 and prog(t_item).progress == 2

        # 4. joined_at cutoff (D10): pre-join submission ignored
        res = handle(env("drop", {"item_name": "Abyssal whip", "quantity": 5},
                         player_id=pid, ts=int((joined - timedelta(hours=1)).timestamp())))
        assert res == [], "pre-join submissions never count"

        # 4b. outside event window: ignored
        res = handle(env("drop", {"item_name": "Abyssal whip", "quantity": 5},
                         player_id=pid, ts=int((now + timedelta(days=2)).timestamp())))
        assert res == [], "post-window submissions never count"

        # 5. per-task requires_confirmation => pending, no fold
        res = handle(env("drop", {"item_name": "Dragon pickaxe", "quantity": 1},
                         player_id=pid, ts=ts_now))
        assert [x["kind"] for x in res] == ["pending"], res
        rows = ledger(t_pending)
        assert len(rows) == 1 and rows[0].status == "pending"
        assert prog(t_pending) is None, "pending rows must not fold progress"
        assert len(notifications(session, "event_pending", pid)) == 1

        # 6. kc_target with (npc, kill_count) dedupe; completes at 2 kills
        res = handle(env("drop", {"item_name": "Snakeskin", "quantity": 1,
                                  "npc_name": "Zulrah", "kill_count": 100},
                         player_id=pid, ts=ts_now))
        assert [x["kind"] for x in res] == ["progress"], res
        res = handle(env("drop", {"item_name": "Zulrah's scales", "quantity": 200,
                                  "npc_name": "Zulrah", "kill_count": 100},
                         player_id=pid, ts=ts_now))
        assert res == [], "same kill_count must count once"
        res = handle(env("drop", {"item_name": "Snakeskin", "quantity": 1,
                                  "npc_name": "Zulrah", "kill_count": 101},
                         player_id=pid, ts=ts_now))
        assert [x["kind"] for x in res] == ["completion"], res
        session.refresh(team_a)
        assert team_a.score == 17, team_a.score
        # A (17) just overtook B (15): lead-change notification enqueued
        assert len(notifications(session, "event_lead_change", pid)) == 1

        # 7. pb_target: completes on first qualifying time; repeats don't re-score
        res = handle(env("pb", {"npc_name": "Zulrah", "time_ms": 59_000,
                                "team_size": 1}, player_id=pid, ts=ts_now))
        assert [x["kind"] for x in res] == ["completion"], res
        session.refresh(team_a)
        assert team_a.score == 20
        pb_rows = len(ledger(t_pb))
        res = handle(env("pb", {"npc_name": "Zulrah", "time_ms": 58_000,
                                "team_size": 1}, player_id=pid, ts=ts_now))
        assert res == [], "completed task must not keep recording"
        assert len(ledger(t_pb)) == pb_rows, "no post-completion ledger rows"
        session.refresh(team_a)
        assert team_a.score == 20, "completed task must not re-score"
        res = handle(env("pb", {"npc_name": "Zulrah", "time_ms": 61_000,
                                "team_size": 1}, player_id=pid, ts=ts_now))
        assert res == [], "over-target time must not match"

        # 8. xp_target: first report only sets the baseline (D10)
        res = handle(env("experience", {"skill": "slayer", "xp": 500_000, "level": 80},
                         player_id=pid, ts=ts_now))
        assert res == [], "baseline report must not credit xp"
        res = handle(env("experience", {"skill": "slayer", "xp": 501_500, "level": 80},
                         player_id=pid, ts=ts_now))
        assert [x["kind"] for x in res] == ["completion"], res
        assert prog(t_xp).progress == 1500

        # 9. skill_target: level threshold
        res = handle(env("experience", {"skill": "agility", "xp": 1, "level": 89},
                         player_id=pid, ts=ts_now))
        assert res == [], "below-target level must not match"
        res = handle(env("experience", {"skill": "agility", "xp": 2, "level": 90},
                         player_id=pid, ts=ts_now))
        # xp baseline for agility was set by the level-89 report; this second
        # report only matches the skill_target task.
        assert [x["kind"] for x in res] == ["completion"], res

        # 10. events:active gate maintenance + producer LPUSH gate — tested
        # against throwaway key names so prod keys are never touched.
        real_active, real_queue = event_engine.ACTIVE_EVENTS_KEY, event_engine.QUEUE_KEY
        tmp_active = f"evtit:{os.getpid()}:active"
        tmp_queue = f"evtit:{os.getpid()}:queue"
        try:
            event_engine.ACTIVE_EVENTS_KEY = tmp_active
            event_engine.QUEUE_KEY = tmp_queue

            # Gate closed (no active set): queue_submission must push nothing.
            event_engine.queue_submission("drop", pid, "evtit-gate-1",
                                          {"item_name": "x"}, world_type="main")
            assert r.llen(tmp_queue) == 0, "gate must skip pushes when closed"

            event_engine.set_active_events(r, [ev.id])
            assert r.exists(tmp_active)
            members = {m.decode() for m in r.smembers(tmp_active)}
            assert str(ev.id) in members

            # Gate open: envelope lands on the queue; seasonal is skipped.
            event_engine.queue_submission("drop", pid, "evtit-gate-2",
                                          {"item_name": "x"}, world_type="seasonal")
            assert r.llen(tmp_queue) == 0, "non-main worlds must be skipped"
            event_engine.queue_submission("drop", pid, "evtit-gate-3",
                                          {"item_name": "x"}, world_type="main",
                                          player_name="EvtEngineIT")
            import json as _json
            raw = r.rpop(tmp_queue)
            envelope = _json.loads(raw)
            assert envelope["v"] == 1 and envelope["kind"] == "drop"
            assert envelope["player_id"] == pid and envelope["guid"] == "evtit-gate-3"
            event_engine.set_active_events(r, [])
            assert not r.exists(tmp_active), "empty set clears the gate"
        finally:
            event_engine.ACTIVE_EVENTS_KEY = real_active
            event_engine.QUEUE_KEY = real_queue
            r.delete(tmp_active, tmp_queue)

        print("ALL EVENT ENGINE INTEGRATION ASSERTIONS PASSED")
    finally:
        session.rollback()
        session.close()
        # Throwaway Redis keys only; never flush anything.
        if event_id is not None:
            for key in r.scan_iter(match=f"events:{event_id}:*"):
                r.delete(key)
        r.close()


if __name__ == "__main__":
    main()
