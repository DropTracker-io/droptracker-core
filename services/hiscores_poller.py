"""The hiscores poller: keeps `player_stat_snapshots` current for event
participants the plugin is not covering, straight from the official hiscores.

Hosted by ``droptracker-player-updates`` (`run_cycle` once a minute). Each
cycle works out who is due (`utils.hiscores_schedule`), looks them up one at
a time through the shared gate (`utils.osrs_hiscores`: one request per two
seconds platform-wide, a breaker, a daily cap) and stores what it learns
through `utils.stat_snapshots`.

Nothing reads these rows for scoring yet. The WOM reconciler still drives
events exactly as before; this builds the history the shadow comparison
needs (dev tracker project 40, t419).

Switches (all env):
  HISCORES_POLLER=false        turn the whole thing off (default on)
  HISCORES_POLL_BATCH          most lookups per cycle (default 20)
  HISCORES_INTERVAL_MS, HISCORES_DAILY_CAP, HISCORES_USER_AGENT: see the client

Per-player state lives in Redis hash ``hiscores:p:{player_id}`` (when last
looked up, when a reading last changed, when the name was last not found) and
``hiscores:pin:{event_id}:{player_id}`` (which start/end readings exist).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from utils import hiscores_schedule as schedule
from utils import osrs_hiscores as hiscores

log = logging.getLogger("hiscores_poller")

BATCH = int(os.getenv("HISCORES_POLL_BATCH", "20"))
CYCLE_BUDGET_SECONDS = 50.0
ON_DEMAND_TTL = timedelta(hours=6)
_STATE_TTL = 60 * 86400


def enabled() -> bool:
    return (os.getenv("HISCORES_POLLER") or "true").strip().strip('"').strip("'").lower() not in (
        "0", "false", "no", "off")


def _state_key(player_id: int) -> str:
    return f"hiscores:p:{player_id}"


def _pin_key(event_id: int, player_id: int) -> str:
    return f"hiscores:pin:{event_id}:{player_id}"


def _ts(value) -> Optional[datetime]:
    try:
        if isinstance(value, bytes):
            value = value.decode()
        return datetime.utcfromtimestamp(float(value)) if value else None
    except (TypeError, ValueError):
        return None


def _decode_hash(raw) -> dict:
    out = {}
    for key, value in (raw or {}).items():
        out[key.decode() if isinstance(key, bytes) else key] = (
            value.decode() if isinstance(value, bytes) else value)
    return out


# --------------------------------------------------------------------------- #
# Planning (blocking DB; run in a worker thread)
# --------------------------------------------------------------------------- #

def _participants_db(now: datetime) -> list:
    """Participants of events the hiscores can help score:
    ``[{player_id, name, account_type, event_id, starts_at, ends_at, joined_at,
    plugin_seen_at}]``. Active events, plus ones that ended inside the window
    an end reading is still worth taking in."""
    from db.models.base import Session
    from db.models import Event, EventTask, EventTeamMember, Player, PlayerState
    from db.models.player_last_seen import PlayerLastSeen
    from utils.account_types import account_type_from_varbit

    session = Session()
    try:
        relevant = {
            row[0] for row in session.query(EventTask.event_id)
            .filter(EventTask.type.in_(schedule.RELEVANT_TASK_TYPES)).distinct()
        }
        events = {
            e.id: e for e in session.query(Event.id, Event.status, Event.starts_at, Event.ends_at)
            .filter(Event.status.in_(("active", "past")))
            if e.id in relevant and (
                e.status == "active"
                or (e.ends_at is not None and now - e.ends_at <= schedule.PIN_WINDOW))
        }
        if not events:
            return []
        members = session.query(
            EventTeamMember.event_id, EventTeamMember.player_id, EventTeamMember.joined_at
        ).filter(EventTeamMember.event_id.in_(list(events))).all()
        player_ids = {m.player_id for m in members}
        players = {
            p.player_id: p for p in session.query(
                Player.player_id, Player.player_name, Player.account_type, Player.account_hash)
            .filter(Player.player_id.in_(list(player_ids)))
        }
        varbits = dict(
            session.query(PlayerState.player_id, PlayerState.account_type)
            .filter(PlayerState.player_id.in_(list(player_ids))).all())
        seen = dict(
            session.query(PlayerLastSeen.player_id, PlayerLastSeen.last_seen_at)
            .filter(PlayerLastSeen.player_id.in_(list(player_ids))).all())
    finally:
        session.close()

    out = []
    for m in members:
        player, event = players.get(m.player_id), events[m.event_id]
        if player is None or not player.player_name:
            continue
        varbit = varbits.get(m.player_id)
        out.append({
            "player_id": m.player_id, "name": player.player_name,
            "account_type": account_type_from_varbit(varbit) or player.account_type,
            "account_varbit": varbit if isinstance(varbit, int) else None,
            "event_id": m.event_id, "starts_at": event.starts_at, "ends_at": event.ends_at,
            "joined_at": m.joined_at, "plugin_seen_at": seen.get(m.player_id),
        })
    return out


def plan(redis_conn, participants: list, now: datetime) -> list:
    """``[(Due, participant)]``, most urgent first, one entry per player (a
    player in two events is looked up once, for the more urgent reason; pins
    for every event they are due in ride on that lookup)."""
    best = {}
    for p in participants:
        state = _decode_hash(redis_conn.hgetall(_state_key(p["player_id"])))
        pins = _decode_hash(redis_conn.hgetall(_pin_key(p["event_id"], p["player_id"])))
        verdict = schedule.due(
            now, starts_at=p["starts_at"], ends_at=p["ends_at"], joined_at=p["joined_at"],
            plugin_seen_at=p["plugin_seen_at"], last_polled=_ts(state.get("polled")),
            last_changed=_ts(state.get("changed")), first_polled=_ts(state.get("first")),
            not_found_at=_ts(state.get("not_found")),
            has_start="start" in pins, has_end="end" in pins)
        if verdict is None:
            continue
        entry = best.setdefault(p["player_id"], {"due": verdict, "p": p, "pins": []})
        if verdict.pinned:
            entry["pins"].append((p["event_id"], verdict.reason))
        if (verdict.priority, -verdict.overdue_seconds) < (
                entry["due"].priority, -entry["due"].overdue_seconds):
            entry["due"], entry["p"] = verdict, p
    ordered = schedule.order([(e["due"], e) for e in best.values()])
    return [(due, {**e["p"], "pins": e["pins"]}) for due, e in ordered]


# --------------------------------------------------------------------------- #
# One lookup
# --------------------------------------------------------------------------- #

def _store_db(player_id: int, account_varbit, reading, pinned: bool, now: datetime) -> str:
    from db.models.base import Session
    from utils.stat_snapshots import record_hiscores_snapshot

    session = Session()
    try:
        outcome = record_hiscores_snapshot(
            session, player_id, reading.skills, reading.activities, reading.ranks,
            total_xp=reading.total_xp, total_level=reading.total_level,
            account_type=account_varbit, pinned=pinned, now=now)
        session.commit()
        return outcome
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _pin_latest_db(player_id: int) -> bool:
    from db.models.base import Session
    from utils.stat_snapshots import pin_latest

    session = Session()
    try:
        found = pin_latest(session, player_id)
        session.commit()
        return found
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def _mark_pins(redis_conn, player_id: int, pins: list, stamp: float) -> None:
    for event_id, reason in pins:
        key = _pin_key(event_id, player_id)
        redis_conn.hset(key, reason, stamp)
        redis_conn.expire(key, _STATE_TTL)


async def _fetch_player(http, redis_conn, name: str, account_type) -> hiscores.FetchResult:
    """The account type's own board first; the main board if it is not there
    (a de-ironed account, or a type with no board of its own)."""
    board = hiscores.board_for(account_type)
    result = await hiscores.fetch(http, redis_conn, name, board)
    if result.status == hiscores.NOT_FOUND and board != hiscores.MAIN_BOARD:
        result = await hiscores.fetch(http, redis_conn, name, hiscores.MAIN_BOARD)
    return result


async def poll_player(http, redis_conn, participant: dict, due: schedule.Due,
                      now: Optional[datetime] = None) -> str:
    """Look one participant up and record it. Returns what happened:
    written / unchanged / flagged / stale / plugin_pinned / not_found /
    blocked / error."""
    now = now or datetime.utcnow()
    player_id, pins = participant["player_id"], participant.get("pins") or []
    key, stamp = _state_key(player_id), time.time()

    seen = participant.get("plugin_seen_at")
    if due.pinned and seen is not None and now - seen <= schedule.PLUGIN_COVERED:
        # The plugin is ahead of the hiscores for this player: keep its latest
        # row as the start/end reading and spend no request.
        if await asyncio.to_thread(_pin_latest_db, player_id):
            _mark_pins(redis_conn, player_id, pins, stamp)
            return "plugin_pinned"

    result = await _fetch_player(http, redis_conn, participant["name"], participant.get("account_type"))
    if result.status == hiscores.BLOCKED:
        return "blocked"
    if result.status == hiscores.NOT_FOUND:
        redis_conn.hset(key, mapping={"polled": stamp, "not_found": stamp})
        redis_conn.expire(key, _STATE_TTL)
        return "not_found"
    if result.status != hiscores.OK:
        return "error"

    outcome = await asyncio.to_thread(
        _store_db, player_id, participant.get("account_varbit"), result.hiscores, due.pinned, now)
    update = {"polled": stamp}
    previous = _decode_hash(redis_conn.hgetall(key))
    if "first" not in previous:
        update["first"] = stamp
    elif outcome == "written":
        # The very first reading is not a change; later ones that differ are.
        update["changed"] = stamp
    redis_conn.hset(key, mapping=update)
    redis_conn.hdel(key, "not_found")
    redis_conn.expire(key, _STATE_TTL)
    # A flagged reading also settles the pin: asking again a minute later
    # would get the same answer. The rotation keeps trying at its own pace.
    _mark_pins(redis_conn, player_id, pins, stamp)
    return outcome


# --------------------------------------------------------------------------- #
# The cycle
# --------------------------------------------------------------------------- #

async def run_cycle(redis_conn=None, now: Optional[datetime] = None) -> dict:
    """One pass: plan, then look up the most urgent few. Never raises."""
    stats = {"due": 0, "polled": 0, "skipped": None}
    if not enabled():
        stats["skipped"] = "disabled"
        return stats
    try:
        if redis_conn is None:
            from utils.redis import redis_client

            redis_conn = redis_client.client
        reason = hiscores.may_request(redis_conn)
        if reason:
            stats["skipped"] = reason
            return stats
        now = now or datetime.utcnow()
        participants = await asyncio.to_thread(_participants_db, now)
        queue = plan(redis_conn, participants, now)
        stats["due"] = len(queue)
        if not queue:
            return stats

        import aiohttp

        deadline = time.monotonic() + CYCLE_BUDGET_SECONDS
        async with aiohttp.ClientSession() as http:
            for due, participant in queue[:BATCH]:
                if time.monotonic() >= deadline:
                    break
                outcome = await poll_player(http, redis_conn, participant, due)
                stats[outcome] = stats.get(outcome, 0) + 1
                if outcome == "blocked":
                    break
                stats["polled"] += 1
        stats["requests_today"] = hiscores.requests_today(redis_conn)
    except Exception:
        log.exception("hiscores poller cycle failed")
        stats["skipped"] = "error"
    return stats


async def lookup(player_id: int, name: str, account_type: Optional[str] = None,
                 redis_conn=None) -> str:
    """On-demand reading for one player, at most once per six hours.

    Returns the outcome of `poll_player`, or "fresh" when a reading from the
    last six hours already exists. For callers that need a number now (a
    profile, a recap straggler); the snapshot store holds the result.
    """
    if not enabled():
        return "blocked"
    if redis_conn is None:
        from utils.redis import redis_client

        redis_conn = redis_client.client
    last = _ts(_decode_hash(redis_conn.hgetall(_state_key(player_id))).get("polled"))
    now = datetime.utcnow()
    if last is not None and now - last < ON_DEMAND_TTL:
        return "fresh"
    import aiohttp

    async with aiohttp.ClientSession() as http:
        return await poll_player(
            http, redis_conn,
            {"player_id": player_id, "name": name, "account_type": account_type, "pins": []},
            schedule.Due(schedule.ROTATION, False, 2), now)
