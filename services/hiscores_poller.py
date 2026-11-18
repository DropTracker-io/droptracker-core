"""The hiscores poller: keeps `player_stat_snapshots` current for players
the plugin is not covering, straight from the official hiscores.

Hosted by ``droptracker-player-updates`` (`run_cycle` in a loop). Each cycle
works out who is due (`utils.hiscores_schedule`), looks them up one at a time
through the shared gate (`utils.osrs_hiscores`: one request per two seconds
platform-wide, a breaker, a daily cap) and stores what it learns through
`utils.stat_snapshots`.

Two tiers share each cycle. Event participants go first. Whatever is left of
the batch goes to the background tier: every player in the database, on a
slow per-player cadence, drawn from a Redis sorted set of next-due times
(``hiscores:queue``). The background tier stops for the day at
HISCORES_BACKGROUND_DAILY requests so events always have headroom under the
overall cap.

Nothing reads these rows for scoring yet. The WOM reconciler still drives
events exactly as before; this builds the history the shadow comparison
needs (dev tracker project 40, t419).

Switches (all env):
  HISCORES_POLLER=false        turn the whole thing off (default on)
  HISCORES_POLL_ALL=false      event participants only, no background tier (default on)
  HISCORES_BACKGROUND_DAILY    background tier's share of the daily cap (default 130000)
  HISCORES_POLL_BATCH          most lookups per cycle (default 120)
  HISCORES_CONCURRENCY         lookups in flight at once (default 4)
  HISCORES_INTERVAL_MS, HISCORES_DAILY_CAP, HISCORES_USER_AGENT: see the client

Per-player state lives in Redis hash ``hiscores:p:{player_id}`` (when last
looked up, when a reading last changed, when the name was last not found) and
``hiscores:pin:{event_id}:{player_id}`` (which start/end readings exist).

"Covered by the plugin" means the plugin sent the player's STATS within the
last hour (player_state.last_synced_at), not that the plugin was seen.
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

BATCH = int(os.getenv("HISCORES_POLL_BATCH", "120"))
# Lookups in flight at once. The gate still spaces request STARTS by the
# tuned interval; this only stops a slow answer (about a second each) from
# being what limits the pace instead of the gate.
CONCURRENCY = max(1, int(os.getenv("HISCORES_CONCURRENCY", "4")))
PARTICIPANTS_CACHE_SECONDS = 60
_participants_cache = {"at": 0.0, "rows": []}
BACKGROUND_DAILY = int(os.getenv("HISCORES_BACKGROUND_DAILY", "130000"))
QUEUE_KEY = "hiscores:queue"
SEED_EVERY_SECONDS = 3600
_last_seed = 0.0
_last_session_scan = 0.0
CYCLE_BUDGET_SECONDS = 50.0
ON_DEMAND_TTL = timedelta(hours=6)
# A detected account type is trusted this long. Types change rarely (an
# account de-irons, a hardcore dies), so a monthly look is plenty.
TYPE_RECHECK = timedelta(days=30)
_STATE_TTL = 60 * 86400


def _on(name: str) -> bool:
    return (os.getenv(name) or "true").strip().strip('"').strip("'").lower() not in (
        "0", "false", "no", "off")


def enabled() -> bool:
    return _on("HISCORES_POLLER")


def background_enabled() -> bool:
    return _on("HISCORES_POLL_ALL")


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
    """Participants of running events:
    ``[{player_id, name, account_type, event_id, starts_at, ends_at, joined_at,
    plugin_synced_at}]``. Active events, plus ones that ended inside the window
    an end reading is still worth taking in.

    Every active event, not only ones with XP or kill-count tasks: the
    reconciler also tracks boss kills for effort (bingo EHB) on events whose
    tasks are all item drops, and which metrics that touches is decided in
    the event consumer's state, not in the task types."""
    from db.models.base import Session
    from db.models import Event, EventTeamMember, Player, PlayerState
    from utils.account_types import account_type_from_varbit

    session = Session()
    try:
        events = {
            e.id: e for e in session.query(Event.id, Event.status, Event.starts_at, Event.ends_at)
            .filter(Event.status.in_(("active", "past")))
            if e.status == "active"
            or (e.ends_at is not None and now - e.ends_at <= schedule.PIN_WINDOW)
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
        # When the plugin last sent this player's stats (a state sync), which
        # is not the same as when the plugin was last seen.
        seen = dict(
            session.query(PlayerState.player_id, PlayerState.last_synced_at)
            .filter(PlayerState.player_id.in_(list(player_ids))).all())
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
            "joined_at": m.joined_at, "plugin_synced_at": seen.get(m.player_id),
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
            plugin_synced_at=p["plugin_synced_at"], last_polled=_ts(state.get("polled")),
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
        if outcome == "written":
            _shadow_player_ehb(session, player_id, reading, account_varbit)
        session.commit()
        return outcome
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _shadow_player_ehb(session, player_id: int, reading, account_varbit) -> None:
    """Shadow mode for ``player_ehb``: the EHB WOM gave us for this player
    (``players.ehb``, copied whenever WOM is consulted) beside the one we
    compute from the reading just stored. Never raises."""
    try:
        from utils import stats_shadow

        if stats_shadow.source("player_ehb") != stats_shadow.SHADOW:
            return
        from db.models import Player
        from utils.stat_snapshots import efficiency_for

        scored = efficiency_for(reading.skills, reading.activities, account_varbit)
        wom_ehb = session.query(Player.ehb).filter(Player.player_id == player_id).scalar()
        if scored is None or wom_ehb is None:
            return
        with session.begin_nested():
            stats_shadow.record(session, "player_ehb", f"type:{scored['type']}", [
                stats_shadow.entry("ehb", float(wom_ehb), scored["ehb"], player_id=player_id,
                                   our_source="hiscores", our_age_seconds=0,
                                   detail=f"build {scored['build']}; players.ehb is WOM's figure "
                                          "as of whenever we last asked WOM")])
    except Exception:
        log.debug("player_ehb shadow comparison failed", exc_info=True)


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


def _varbit_for(account_type: Optional[str]) -> Optional[int]:
    """Our account type string as the varbit number snapshots store."""
    from utils.account_types import ACCOUNT_TYPES_BY_VARBIT

    try:
        return ACCOUNT_TYPES_BY_VARBIT.index(account_type)
    except ValueError:
        return None


def _carry_forward_db(player_id: int, now: datetime) -> bool:
    from db.models.base import Session
    from utils.stat_snapshots import carry_forward

    session = Session()
    try:
        written = carry_forward(session, player_id, now)
        session.commit()
        return written
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


async def poll_player(http, redis_conn, participant: dict, due: schedule.Due,
                      now: Optional[datetime] = None) -> str:
    """Look one participant up and record it. Returns what happened:
    written / unchanged / flagged / stale / plugin_pinned / not_found /
    blocked / error."""
    now = now or datetime.utcnow()
    player_id, pins = participant["player_id"], participant.get("pins") or []
    key, stamp = _state_key(player_id), time.time()

    seen = participant.get("plugin_synced_at")
    if due.pinned and seen is not None and now - seen <= schedule.PLUGIN_COVERED:
        # The plugin is ahead of the hiscores for this player, so no request
        # is spent. A start reading repeats the plugin's numbers in a row of
        # its own at this moment, because gains are counted from the first
        # reading INSIDE the window; an end reading only has to be kept.
        if due.reason == schedule.START:
            done = await asyncio.to_thread(_carry_forward_db, player_id, now)
        else:
            done = await asyncio.to_thread(_pin_latest_db, player_id)
        if done:
            _mark_pins(redis_conn, player_id, pins, stamp)
            return "plugin_pinned"

    state = _decode_hash(redis_conn.hgetall(key))
    # The plugin (or a submission) telling us the type beats anything worked
    # out from the boards.
    known = participant.get("account_type")
    detected_at = _ts(state.get("type_at"))
    detected = state.get("type") if detected_at and now - detected_at < TYPE_RECHECK else None
    account_type = known or detected

    result = await _fetch_player(http, redis_conn, participant["name"], account_type)
    if result.status == hiscores.BLOCKED:
        return "blocked"
    if result.status == hiscores.NOT_FOUND:
        streak = int(state.get("nf") or 0) + 1
        redis_conn.hset(key, mapping={"polled": stamp, "not_found": stamp, "nf": streak})
        redis_conn.expire(key, _STATE_TTL)
        return "not_found"
    if result.status != hiscores.OK:
        return "error"

    update = {"polled": stamp}
    if not known and not detected and result.board == hiscores.MAIN_BOARD:
        # Nobody has told us what kind of account this is: ask the boards.
        # Without it an ironman would be scored on a main's EHP and EHB rates.
        found = await hiscores.detect_account_type(
            lambda board: hiscores.fetch(http, redis_conn, participant["name"], board),
            result.hiscores)
        if found:
            account_type = found
            update["type"], update["type_at"] = found, stamp

    varbit = participant.get("account_varbit")
    if varbit is None:
        varbit = _varbit_for(account_type)
    outcome = await asyncio.to_thread(
        _store_db, player_id, varbit, result.hiscores, due.pinned, now)
    if "first" not in state:
        update["first"] = stamp
    elif outcome == "written":
        # The very first reading is not a change; later ones that differ are.
        update["changed"] = stamp
    redis_conn.hset(key, mapping=update)
    redis_conn.hdel(key, "not_found")
    redis_conn.hdel(key, "nf")
    redis_conn.expire(key, _STATE_TTL)
    # A flagged reading also settles the pin: asking again a minute later
    # would get the same answer. The rotation keeps trying at its own pace.
    _mark_pins(redis_conn, player_id, pins, stamp)
    return outcome


# --------------------------------------------------------------------------- #
# The background tier: every player, slowly
# --------------------------------------------------------------------------- #

def _all_players_db() -> list:
    """``[(player_id, is_stub)]`` for every player. Stubs are roster entries
    we have never heard from; they are worth having, after everyone else."""
    from db.models import Player
    from db.models.base import Session

    session = Session()
    try:
        return [
            (pid, bool(acc_hash and str(acc_hash).startswith("wom_temp_")))
            for pid, acc_hash in session.query(Player.player_id, Player.account_hash).all()
        ]
    finally:
        session.close()


def seed_queue(redis_conn, players: list) -> dict:
    """Bring the queue in line with the players table: add who is missing
    (due at once, real accounts ahead of stubs), drop who is gone. Existing
    entries keep their place."""
    known = {int(pid) for pid, _ in players}
    queued = {int(m) for m in redis_conn.zrange(QUEUE_KEY, 0, -1)}
    missing = [(str(int(pid)), 1.0 if is_stub else 0.0)
               for pid, is_stub in players if int(pid) not in queued]
    for start in range(0, len(missing), 1000):
        redis_conn.zadd(QUEUE_KEY, dict(missing[start:start + 1000]), nx=True)
    added = len(missing)
    gone = [str(m) for m in queued - known]
    for start in range(0, len(gone), 500):
        redis_conn.zrem(QUEUE_KEY, *gone[start:start + 500])
    return {"added": added, "removed": len(gone)}


def _players_db(player_ids: list) -> dict:
    """``{player_id: participant-shaped dict}`` for a batch of ids."""
    from db.models import Player, PlayerState
    from db.models.base import Session
    from utils.account_types import account_type_from_varbit

    if not player_ids:
        return {}
    session = Session()
    try:
        players = session.query(Player.player_id, Player.player_name, Player.account_type).filter(
            Player.player_id.in_(player_ids)).all()
        varbits = dict(session.query(PlayerState.player_id, PlayerState.account_type).filter(
            PlayerState.player_id.in_(player_ids)).all())
        seen = dict(session.query(PlayerState.player_id, PlayerState.last_synced_at).filter(
            PlayerState.player_id.in_(player_ids)).all())
    finally:
        session.close()
    out = {}
    for p in players:
        if not p.player_name:
            continue
        varbit = varbits.get(p.player_id)
        out[p.player_id] = {
            "player_id": p.player_id, "name": p.player_name,
            "account_type": account_type_from_varbit(varbit) or p.account_type,
            "account_varbit": varbit if isinstance(varbit, int) else None,
            "plugin_synced_at": seen.get(p.player_id), "pins": [],
        }
    return out


def _next_due(redis_conn, player_id: int, now: datetime) -> float:
    """Unix time of this player's next background lookup, from their state."""
    state = _decode_hash(redis_conn.hgetall(_state_key(player_id)))
    wait = schedule.background_interval(
        now, last_changed=_ts(state.get("changed")), first_polled=_ts(state.get("first")),
        not_found_streak=int(state.get("nf") or 0))
    return time.time() + wait.total_seconds()


def reschedule(redis_conn, player_id: int, now: datetime, *, only_if_queued: bool = False) -> None:
    redis_conn.zadd(QUEUE_KEY, {str(int(player_id)): _next_due(redis_conn, player_id, now)},
                    xx=only_if_queued)


def rebase_queue(redis_conn, now: Optional[datetime] = None) -> dict:
    """Re-plan every queued player from when they were last read, pulling
    forward anyone the current cadence would have read sooner. For after the
    cadence is made faster: a turn already booked under the old cadence would
    otherwise stand. Never pushes anyone later. Returns counts."""
    now = now or datetime.utcnow()
    moved = kept = 0
    for member, score in redis_conn.zrange(QUEUE_KEY, 0, -1, withscores=True):
        player_id = int(member)
        state = _decode_hash(redis_conn.hgetall(_state_key(player_id)))
        polled = _ts(state.get("polled"))
        if polled is None:
            kept += 1
            continue
        wait = schedule.background_interval(
            now, last_changed=_ts(state.get("changed")), first_polled=_ts(state.get("first")),
            not_found_streak=int(state.get("nf") or 0))
        due = (polled - datetime(1970, 1, 1)).total_seconds() + wait.total_seconds()
        if due < float(score):
            redis_conn.zadd(QUEUE_KEY, {str(player_id): due})
            moved += 1
        else:
            kept += 1
    return {"moved_earlier": moved, "unchanged": kept}


def _recent_contacts_db(now: datetime) -> dict:
    """``{player_id: last contact}`` for players whose plugin fell silent in
    the session-end window: last contact is the later of their last
    submission and their last stats sync."""
    from db.models import PlayerState
    from db.models.base import Session
    from db.models.player_last_seen import PlayerLastSeen

    since = now - schedule.SESSION_END_WINDOW
    session = Session()
    try:
        contacts = dict(session.query(PlayerLastSeen.player_id, PlayerLastSeen.last_seen_at)
                        .filter(PlayerLastSeen.last_seen_at >= since).all())
        for player_id, synced in session.query(PlayerState.player_id, PlayerState.last_synced_at) \
                .filter(PlayerState.last_synced_at >= since).all():
            if synced and (player_id not in contacts or synced > contacts[player_id]):
                contacts[player_id] = synced
    finally:
        session.close()
    return contacts


def enqueue_session_ends(redis_conn, contacts: dict, now: datetime) -> int:
    """Bring forward every player who has just stopped playing and has not
    been read since. Returns how many were brought forward."""
    moved = 0
    for player_id, contact in contacts.items():
        state = _decode_hash(redis_conn.hgetall(_state_key(player_id)))
        if "not_found" in state:
            continue
        if not schedule.session_ended(now, contact, _ts(state.get("polled"))):
            continue
        # Only players already in the queue (a deleted player is not).
        if redis_conn.zscore(QUEUE_KEY, str(int(player_id))) is None:
            continue
        redis_conn.zadd(QUEUE_KEY, {str(int(player_id)): 2.0})
        moved += 1
    return moved


async def background_items(redis_conn, slots: int, skip: set, stats: dict) -> list:
    """The players who have waited longest for a background read, as
    ``[player dict]``. Players the plugin is covering are pushed back here
    and cost nothing; players who no longer exist leave the queue."""
    global _last_seed
    if time.time() - _last_seed >= SEED_EVERY_SECONDS:
        players = await asyncio.to_thread(_all_players_db)
        stats["seed"] = seed_queue(redis_conn, players)
        _last_seed = time.time()
    if slots <= 0 or hiscores.requests_today(redis_conn) >= BACKGROUND_DAILY:
        return []
    global _last_session_scan
    if time.time() - _last_session_scan >= 60:
        _last_session_scan = time.time()
        try:
            now_ = datetime.utcnow()
            ended = enqueue_session_ends(redis_conn, await asyncio.to_thread(_recent_contacts_db, now_), now_)
            if ended:
                stats["session_ends"] = ended
        except Exception:
            log.exception("session-end scan failed")
    ids = [int(m) for m in redis_conn.zrangebyscore(QUEUE_KEY, "-inf", time.time(), start=0, num=slots)]
    info = await asyncio.to_thread(_players_db, ids)
    now = datetime.utcnow()
    out = []
    for player_id in ids:
        if player_id in skip:
            continue
        player = info.get(player_id)
        if player is None:
            redis_conn.zrem(QUEUE_KEY, str(player_id))
            continue
        seen = player["plugin_synced_at"]
        if seen is not None and now - seen <= schedule.BACKGROUND_PLUGIN_COVERED:
            # The plugin is sending their stats right now. Come back just
            # after the window from that sync: if the syncs have stopped by
            # then, that reading is the end of their session.
            back = (seen + schedule.BACKGROUND_PLUGIN_COVERED + schedule.AFTER_LAST_SYNC - now)
            redis_conn.zadd(QUEUE_KEY, {str(player_id): time.time() + max(60.0, back.total_seconds())})
            stats["bg_covered"] = stats.get("bg_covered", 0) + 1
            continue
        out.append(player)
    return out


async def work(http, redis_conn, items: list, deadline: float, stats: dict) -> None:
    """Look up ``items`` (``[("event" | "bg", Due, player)]``, most urgent
    first) with up to CONCURRENCY in flight. Stops at the deadline or the
    moment the gate refuses; whatever was not reached keeps its place."""
    pending = list(reversed(items))
    stopped = False

    async def worker():
        nonlocal stopped
        while pending and not stopped and time.monotonic() < deadline:
            kind, due, player = pending.pop()
            outcome = await poll_player(http, redis_conn, player, due)
            if outcome == "blocked":
                stopped = True
                stats["blocked"] = True
                return
            stats["polled"] += 1
            if kind == "event":
                stats[outcome] = stats.get(outcome, 0) + 1
                # They have just been read: push their background turn back too.
                reschedule(redis_conn, player["player_id"], datetime.utcnow(), only_if_queued=True)
            else:
                stats["bg_" + outcome] = stats.get("bg_" + outcome, 0) + 1
                if outcome == "error":
                    redis_conn.zadd(QUEUE_KEY, {str(player["player_id"]): time.time()
                                                + schedule.BACKGROUND_RETRY.total_seconds()})
                else:
                    reschedule(redis_conn, player["player_id"], datetime.utcnow())

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    # Work was left over (the cycle ran out of time, not of players): the
    # host loop should come straight back instead of resting a minute.
    stats["more"] = bool(pending) and not stopped


# --------------------------------------------------------------------------- #
# The cycle
# --------------------------------------------------------------------------- #

async def _participants(now: datetime) -> list:
    """Event participants, re-read from the database at most once a minute."""
    if time.time() - _participants_cache["at"] >= PARTICIPANTS_CACHE_SECONDS:
        _participants_cache["rows"] = await asyncio.to_thread(_participants_db, now)
        _participants_cache["at"] = time.time()
    return _participants_cache["rows"]


async def run_cycle(redis_conn=None, now: Optional[datetime] = None) -> dict:
    """One pass: plan, then look up the most urgent few. Never raises."""
    stats = {"due": 0, "polled": 0, "skipped": None}
    try:
        if redis_conn is None:
            from utils.redis import redis_client

            redis_conn = redis_client.client
        if not enabled():
            stats["skipped"] = "disabled"
            hiscores.record_cycle(redis_conn, "disabled")
            return stats
        reason = hiscores.may_request(redis_conn)
        # The heartbeat scripts/health_watch.py reads: the loop is alive, and
        # whether it is working or holding back (and why).
        hiscores.record_cycle(redis_conn, reason or "running")
        change = hiscores.tune_pace(redis_conn)
        if change:
            stats["pace"] = change
        if reason:
            stats["skipped"] = reason
            return stats
        now = now or datetime.utcnow()
        queue = plan(redis_conn, await _participants(now), now)
        stats["due"] = len(queue)
        items = [("event", due, participant) for due, participant in queue[:BATCH]]
        if background_enabled():
            rotation = schedule.Due(schedule.ROTATION, False, 2)
            taken = {participant["player_id"] for _kind, _due, participant in items}
            for player in await background_items(redis_conn, BATCH - len(items), taken, stats):
                items.append(("bg", rotation, player))

        import aiohttp

        deadline = time.monotonic() + CYCLE_BUDGET_SECONDS
        async with aiohttp.ClientSession() as http:
            await work(http, redis_conn, items, deadline, stats)
        stats.pop("blocked", None)
        stats["requests_today"] = hiscores.requests_today(redis_conn)
        stats["interval_ms"] = hiscores.current_interval(redis_conn)
        if background_enabled():
            stats["queue_due"] = int(redis_conn.zcount(QUEUE_KEY, "-inf", time.time()))
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
