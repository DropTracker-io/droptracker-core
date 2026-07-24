"""WOM reconciliation for events v2 — hybrid plugin + hiscores tracking.

Periodically pulls each participating group's WiseOldMan **bulk gains**
(``GET /groups/:id/bulk-gained`` — every member, every metric, window-bounded
start/end values) and turns them into *synthetic envelopes* on the ordinary
``events:submissions`` queue. The reconciler itself never writes credit: the
event consumer serializes all crediting through the same monotonic
absolute-value watermarks the plugin path uses (``_fold_xp_baseline`` /
``_fold_kc_watermark`` in ``services/event_engine.py``), which is what makes
double-counting impossible — whichever source is ahead wins and the other
folds to zero.

Reconciliation is enabled automatically for any event whose group(s) have
``groups.wom_id`` set; the ``event_wom_reconciliation`` group config key
force-disables it. Freshness (WOM snapshots only advance when a player gets
updated on WOM) comes from key-moment passes (event activation and a lead
window before the event ends — via the group-wide ``update-all`` endpoint when
the group configured its ``wom_verification_code``, else a budgeted
per-player ``update_player`` pass) plus an activity-tiered rotation: only
participants actually progressing on event-relevant metrics stay on a fast
update cadence; idle participants back off exponentially (see the constants
below — WOM asked us to stop mass-updating players daily, 2026-07).

Hosted by ``workers/event_consumer.py`` (same asyncio loop as the queue
drain); all DB access goes through short-lived sessions in worker threads.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("event_wom_reconciler")

RELEVANT_TASK_TYPES = ("xp_target", "skill_target", "kc_target")

WOM_RECONCILE_SECONDS = int(os.getenv("WOM_RECONCILE_SECONDS", "300"))
WOM_EVENT_UPDATE_BUDGET = int(os.getenv("WOM_EVENT_UPDATE_BUDGET", "10"))
WOM_UPDATE_ALL_LEAD_SECONDS = int(os.getenv("WOM_UPDATE_ALL_LEAD_SECONDS", "1800"))

# Activity-tiered freshness rotation: every reconcile cycle, participants who
# are actually progressing on event-relevant metrics rotate through forced WOM
# updates at a roster-scaled cadence (MIN/MAX clamp below), while participants
# whose snapshots keep showing no relevant gains — and ones whose updates keep
# failing (unranked / rename limbo) — back off exponentially from
# IDLE_BASE_INTERVAL to IDLE_MAX_INTERVAL. The per-cycle cap bounds the worst
# case at MAX_PER_CYCLE per ~5-min cycle (~1.4k WOM calls/day at 5, down from
# ~10k with the old always-on rotation that WOM asked us to stop).
WOM_EVENT_UPDATE_SECONDS_PER_PLAYER = int(os.getenv("WOM_EVENT_UPDATE_SECONDS_PER_PLAYER", "60"))
WOM_EVENT_UPDATE_MIN_INTERVAL = int(os.getenv("WOM_EVENT_UPDATE_MIN_INTERVAL", "1800"))
WOM_EVENT_UPDATE_MAX_INTERVAL = int(os.getenv("WOM_EVENT_UPDATE_MAX_INTERVAL", "10800"))
WOM_EVENT_UPDATE_MAX_PER_CYCLE = int(os.getenv("WOM_EVENT_UPDATE_MAX_PER_CYCLE", "5"))
WOM_EVENT_IDLE_BASE_INTERVAL = int(os.getenv("WOM_EVENT_IDLE_BASE_INTERVAL", "3600"))
WOM_EVENT_IDLE_MAX_INTERVAL = int(os.getenv("WOM_EVENT_IDLE_MAX_INTERVAL", "86400"))
# Floor for retry pacing after a failed / rate-skipped update attempt (the
# idle backoff from the player's failure-bumped streak usually exceeds it).
_UPDATE_FAIL_COOLDOWN_SECONDS = 900
_STATE_KEY_TTL = 60 * 86400

CONFIG_KEY_ENABLED = "event_wom_reconciliation"
CONFIG_KEY_VERIFICATION = "wom_verification_code"

_FALSEY = {"0", "false", "off", "no", "disabled"}


def _womseen_key(event_id: int, player_id: int) -> str:
    return f"events:{event_id}:womseen:{player_id}"


def _womfinal_key(event_id: int) -> str:
    return f"events:{event_id}:womfinal"


def _womupdall_key(event_id: int, stage: str) -> str:
    return f"events:{event_id}:womupdall:{stage}"


def _womact_key(event_id: int) -> str:
    """Per-event activity ledger hash: player_id -> "epoch:streak:sig"."""
    return f"events:{event_id}:womact"


def _parse_wom_ts(value) -> Optional[int]:
    """WOM ISO-8601 'Z' timestamp → unix epoch seconds."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _level_for_xp(xp: int) -> int:
    """Standard OSRS level for an XP total (levels 1-99)."""
    total = 0
    for level in range(1, 100):
        total += int(level + 300 * (2 ** (level / 7.0)))
        if xp < total // 4:
            return level
    return 99


# ══════════════════════════════════════════════════════════════════════════════
# Target planning (which events / WOM groups / metrics / participants)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ReconcileTarget:
    event_id: int
    event_name: str
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    # [(group_id, wom_group_id, verification_code_or_None)]
    wom_groups: list = field(default_factory=list)
    skills: dict = field(default_factory=dict)        # dt skill key -> wom slug
    boss_metrics: set = field(default_factory=set)    # wom slugs
    # wom_player_id -> (player_id, player_name, joined_at, is_stub)
    participants_by_wom: dict = field(default_factory=dict)
    # normalized username fallback for players missing players.wom_id
    participants_by_name: dict = field(default_factory=dict)


def _relevant_metrics(state, event_id) -> tuple[dict, set]:
    from utils.wiseoldman import wom_skill_metric

    skills, bosses = {}, set()
    for task in state.tasks_by_event.get(event_id, []):
        ttype, target = task.get("type"), task.get("target")
        if ttype in ("xp_target", "skill_target"):
            if not target:
                continue
            key = str(target).strip().lower()
            slug = wom_skill_metric(key)
            if slug:
                skills[key] = slug
            else:
                log.warning("Event %s task %s: no WOM metric for skill %r",
                            event_id, task.get("id"), target)
        elif ttype == "kc_target":
            if not target:
                continue
            # Multi-NPC tasks (config.npcs) carry one metric per NPC in
            # ``wom_metrics``; legacy dicts fall back to the single field.
            metrics = task.get("wom_metrics")
            slugs = set(metrics) if isinstance(metrics, dict) else set()
            if not slugs and task.get("wom_metric"):
                slugs.add(task["wom_metric"])
            if slugs:
                bosses.update(slugs)
            else:
                log.info("Event %s task %s: no WOM metric for NPC %r (plugin-only)",
                         event_id, task.get("id"), target)
        else:
            # any_path metric paths (item_collection): KC paths resolve their
            # NPCs to boss metrics at state load, merged into ``wom_metrics``
            # (services.event_engine._task_to_dict). Target is empty here.
            metrics = task.get("wom_metrics")
            if isinstance(metrics, dict) and metrics:
                bosses.update(metrics)
    return skills, bosses


def _plan_targets_db(state) -> list[ReconcileTarget]:
    """Build reconcile targets for the active events in ``state`` (blocking;
    run in a worker thread). Events with no WOM-linked group or no relevant
    tasks produce no target."""
    from api.core import get_db_session, reset_db_connections
    from db.models import Event, EventGroup, Group, GroupConfiguration, Player

    candidates = {}
    for event_id in state.events:
        skills, bosses = _relevant_metrics(state, event_id)
        if skills or bosses:
            candidates[event_id] = (skills, bosses)
    if not candidates:
        return []

    # event_id -> {player_id: joined_at}
    rosters = {}
    for player_id, memberships in state.participants.items():
        for event_id, _team_id, joined_at in memberships:
            if event_id in candidates:
                rosters.setdefault(event_id, {})[player_id] = joined_at

    session = get_db_session()
    try:
        rows = session.query(Event.id, Event.mode, Event.group_id).filter(
            Event.id.in_(list(candidates.keys()))).all()
        group_ids_by_event = {}
        cvc_ids = [r.id for r in rows if r.mode == "clan_vs_clan"]
        for r in rows:
            group_ids_by_event[r.id] = {r.group_id} if r.group_id else set()
        if cvc_ids:
            for eg in session.query(EventGroup).filter(
                    EventGroup.event_id.in_(cvc_ids),
                    EventGroup.status == "accepted").all():
                group_ids_by_event.setdefault(eg.event_id, set()).add(eg.group_id)

        all_group_ids = {g for ids in group_ids_by_event.values() for g in ids if g}
        wom_by_group, cfg = {}, {}
        if all_group_ids:
            for g in session.query(Group.group_id, Group.wom_id).filter(
                    Group.group_id.in_(list(all_group_ids))).all():
                if g.wom_id:
                    wom_by_group[g.group_id] = int(g.wom_id)
            for c in session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id.in_(list(all_group_ids)),
                    GroupConfiguration.config_key.in_(
                        [CONFIG_KEY_ENABLED, CONFIG_KEY_VERIFICATION])).all():
                value = (c.long_value or c.config_value or "").strip()
                cfg[(c.group_id, c.config_key)] = value

        all_player_ids = {p for roster in rosters.values() for p in roster}
        players = {}
        if all_player_ids:
            for p in session.query(
                    Player.player_id, Player.wom_id, Player.player_name,
                    Player.account_hash).filter(
                    Player.player_id.in_(list(all_player_ids))).all():
                players[p.player_id] = p
    finally:
        session.close()
        reset_db_connections()

    targets = []
    for event_id, (skills, bosses) in candidates.items():
        event = state.events.get(event_id) or {}
        wom_groups = []
        for group_id in sorted(group_ids_by_event.get(event_id, ())):
            wom_gid = wom_by_group.get(group_id)
            if not wom_gid:
                continue
            enabled = cfg.get((group_id, CONFIG_KEY_ENABLED), "")
            if enabled.lower() in _FALSEY:
                continue
            code = cfg.get((group_id, CONFIG_KEY_VERIFICATION)) or None
            wom_groups.append((group_id, wom_gid, code))
        if not wom_groups:
            continue
        target = ReconcileTarget(
            event_id=event_id,
            event_name=event.get("name") or f"event {event_id}",
            window_start=event.get("window_start"),
            window_end=event.get("window_end"),
            wom_groups=wom_groups,
            skills=skills,
            boss_metrics=bosses,
        )
        for player_id, joined_at in (rosters.get(event_id) or {}).items():
            p = players.get(player_id)
            if p is None:
                continue
            is_stub = bool(p.account_hash and str(p.account_hash).startswith("wom_temp_"))
            entry = (player_id, p.player_name, joined_at, is_stub)
            if p.wom_id:
                target.participants_by_wom[int(p.wom_id)] = entry
            if p.player_name:
                target.participants_by_name[
                    " ".join(str(p.player_name).strip().lower().split())] = entry
        if target.participants_by_wom or target.participants_by_name:
            targets.append(target)
    return targets


async def plan_targets(state) -> list[ReconcileTarget]:
    return await asyncio.to_thread(_plan_targets_db, state)


# ══════════════════════════════════════════════════════════════════════════════
# Envelope emission
# ══════════════════════════════════════════════════════════════════════════════

def _match_participant(target: ReconcileTarget, player_obj: dict):
    wom_id = player_obj.get("id")
    entry = target.participants_by_wom.get(int(wom_id)) if wom_id else None
    if entry is None:
        name = " ".join(str(player_obj.get("displayName")
                            or player_obj.get("username") or "").strip().lower().split())
        entry = target.participants_by_name.get(name)
    return entry


def _emit_for_row(redis_conn, target: ReconcileTarget, row: dict,
                  *, clamp_epoch: Optional[int], force: bool, stats: dict) -> int:
    """Queue envelopes for one bulk-gained row. Returns envelopes pushed."""
    # Low-priority lane (batch 2): a reconcile burst must not head-of-line
    # block live plugin drops; the consumer drains this queue when idle.
    from services.event_engine import WOM_QUEUE_KEY as QUEUE_KEY

    player_obj = row.get("player") or {}
    entry = _match_participant(target, player_obj)
    if entry is None:
        stats["players_unmatched"] += 1
        return 0
    player_id, player_name, _joined_at, _is_stub = entry

    updated_epoch = _parse_wom_ts(player_obj.get("updatedAt")) or 0
    seen_key = _womseen_key(target.event_id, player_id)
    if not force:
        try:
            seen = int(redis_conn.get(seen_key) or 0)
        except (TypeError, ValueError):
            seen = 0
        if updated_epoch and updated_epoch <= seen:
            stats["players_stale"] += 1
            return 0

    snap_epoch = _parse_wom_ts(row.get("endDate")) or updated_epoch or int(time.time())
    if clamp_epoch is not None:
        snap_epoch = min(snap_epoch, clamp_epoch)

    metrics = {m.get("metric"): m for m in (row.get("data") or [])
               if isinstance(m, dict)}
    pushed = 0
    for dt_skill, slug in target.skills.items():
        m = metrics.get(slug)
        if not m:
            continue
        try:
            end = int(m.get("end") or 0)
            start = int(m.get("start") or 0)
        except (TypeError, ValueError):
            continue
        if end <= 0:
            continue
        envelope = {
            "v": 1,
            "kind": "experience",
            "guid": f"wom:{target.event_id}:{player_id}:{dt_skill}:{snap_epoch}",
            "player_id": player_id,
            "player_name": player_name,
            "ts": snap_epoch,
            "used_api": True,
            "source": "wom",
            "data": {
                "skill": dt_skill,
                "xp": end,
                "level": _level_for_xp(end),
                "xp_start": start if start > 0 else None,
                "target_event_id": target.event_id,
                "source": "wom",
            },
        }
        redis_conn.lpush(QUEUE_KEY, json.dumps(envelope, default=str))
        pushed += 1
    for slug in target.boss_metrics:
        m = metrics.get(slug)
        if not m:
            continue
        try:
            kc = int(m.get("end") or 0)
            kc_start = int(m.get("start") or 0)
        except (TypeError, ValueError):
            continue
        if kc <= 0:
            continue
        envelope = {
            "v": 1,
            "kind": "wom_kc",
            "guid": f"wom:{target.event_id}:{player_id}:kc:{slug}:{snap_epoch}",
            "player_id": player_id,
            "player_name": player_name,
            "ts": snap_epoch,
            "used_api": True,
            "source": "wom",
            "data": {
                "boss_metric": slug,
                "kc": kc,
                "kc_start": kc_start if kc_start > 0 else None,
                "target_event_id": target.event_id,
                "source": "wom",
            },
        }
        redis_conn.lpush(QUEUE_KEY, json.dumps(envelope, default=str))
        pushed += 1

    if updated_epoch:
        try:
            redis_conn.set(seen_key, updated_epoch, ex=_STATE_KEY_TTL)
        except Exception:
            pass
    if pushed:
        stats["players_emitted"] += 1
    return pushed


async def _reconcile_target(redis_conn, target: ReconcileTarget, *,
                            end_at: datetime, force: bool, stats: dict) -> list:
    """Fetch + emit for one target. Returns the fetched bulk-gained rows
    (all groups concatenated) so the freshness rotation can reuse them."""
    from utils.wiseoldman import get_group_bulk_gained

    start_at = target.window_start
    if start_at is None:
        # Shouldn't happen for active events (activation stamps the window),
        # but never reconcile an unbounded window.
        log.warning("Event %s has no window_start; skipping WOM reconcile",
                    target.event_id)
        return []
    if end_at <= start_at:
        return []
    clamp_epoch = (int(target.window_end.timestamp())
                   if target.window_end is not None else None)
    all_rows = []
    for _group_id, wom_gid, _code in target.wom_groups:
        rows = await get_group_bulk_gained(wom_gid, start_at, end_at)
        if rows is None:
            stats["group_fetch_failures"] += 1
            continue
        stats["groups_fetched"] += 1
        pushed = 0
        for row in rows:
            if isinstance(row, dict):
                all_rows.append(row)
                pushed += _emit_for_row(redis_conn, target, row,
                                        clamp_epoch=clamp_epoch, force=force,
                                        stats=stats)
        stats["envelopes"] += pushed
    return all_rows


def _new_stats() -> dict:
    return {"targets": 0, "groups_fetched": 0, "group_fetch_failures": 0,
            "players_emitted": 0, "players_stale": 0, "players_unmatched": 0,
            "envelopes": 0, "updates_requested": 0, "update_candidates": 0,
            "players_active": 0, "players_idle": 0}


async def reconcile_once(state, redis_conn, now: Optional[datetime] = None) -> dict:
    """One periodic pass over every WOM-enabled active event: emit envelopes
    from the current snapshots, then spend the cycle's update cap forcing the
    stalest participants to refresh (their gains land next cycle)."""
    now = now or datetime.now()
    stats = _new_stats()
    targets = await plan_targets(state)
    _cache_targets(targets)
    update_cap = WOM_EVENT_UPDATE_MAX_PER_CYCLE
    for target in targets:
        stats["targets"] += 1
        end_at = min(now, target.window_end) if target.window_end else now
        rows = await _reconcile_target(redis_conn, target, end_at=end_at,
                                       force=False, stats=stats)
        if update_cap > 0:
            fresh = await _freshness_for_target(redis_conn, target, rows,
                                                max_updates=update_cap)
            stats["updates_requested"] += fresh["player_updates"]
            stats["update_candidates"] += fresh["candidates"]
            stats["players_active"] += fresh["active"]
            stats["players_idle"] += fresh["idle"]
            update_cap -= fresh["player_updates"]
    return stats


async def final_reconcile(state, redis_conn, event_id: int) -> Optional[dict]:
    """End-of-event pass: window-bounded, ignores the womseen gate (the ledger
    guid index and the watermarks absorb any rework), flags completion."""
    stats = _new_stats()
    targets = [t for t in await plan_targets(state) if t.event_id == event_id]
    if not targets:
        return None
    target = targets[0]
    end_at = target.window_end or datetime.now()
    await _reconcile_target(redis_conn, target, end_at=end_at, force=True,
                            stats=stats)
    try:
        redis_conn.set(_womfinal_key(event_id), int(time.time()), ex=7 * 86400)
    except Exception:
        pass
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# Freshness: key-moment update-all passes + activity-tiered update_player
# rotation. "Active" = the participant's event-relevant metrics moved between
# their last two observed WOM snapshots; everyone else decays to IDLE_MAX.
# ══════════════════════════════════════════════════════════════════════════════

def _active_interval(active_count: int) -> int:
    """Per-player WOM refresh interval for the roster's active cohort.

    Scales linearly so the steady-state update rate is roster-independent
    (~60/SECONDS_PER_PLAYER calls per minute when saturated): a handful of
    active players refresh every MIN_INTERVAL; hundreds settle near MAX."""
    interval = max(1, active_count) * WOM_EVENT_UPDATE_SECONDS_PER_PLAYER
    return max(WOM_EVENT_UPDATE_MIN_INTERVAL,
               min(WOM_EVENT_UPDATE_MAX_INTERVAL, interval))


def _idle_interval(idle_streak: int, active_interval: int) -> int:
    """Refresh interval for a participant ``idle_streak`` consecutive
    no-progress observations (or failed update attempts) deep. Doubles from
    the base each step, never faster than the active cadence, capped at
    IDLE_MAX_INTERVAL — a firmly idle signup costs ~1 WOM update/day."""
    if idle_streak <= 0:
        return active_interval
    backoff = WOM_EVENT_IDLE_BASE_INTERVAL << min(idle_streak - 1, 12)
    return min(max(backoff, active_interval), WOM_EVENT_IDLE_MAX_INTERVAL)


def _row_signature(row: dict, relevant_slugs: set) -> str:
    """Stable digest of a bulk-gained row's end values for the event-relevant
    metrics. Two snapshots with the same digest ⇒ no event-relevant progress
    (gains in unrelated skills don't count as event activity)."""
    parts = sorted(
        f"{m.get('metric')}:{m.get('end')}"
        for m in (row.get("data") or [])
        if isinstance(m, dict) and m.get("metric") in relevant_slugs)
    if not parts:
        return ""
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _latest_row_by_participant(target: ReconcileTarget, rows: list) -> dict:
    """Latest-snapshot bulk-gained row per matched participant:
    {player_id: (updated_epoch, username, row)}."""
    latest = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        player_obj = row.get("player") or {}
        entry = _match_participant(target, player_obj)
        if entry is None:
            continue
        player_id = entry[0]
        updated = _parse_wom_ts(player_obj.get("updatedAt")) or 0
        username = player_obj.get("username") or entry[1]
        prev = latest.get(player_id)
        if prev is None or updated > prev[0]:
            latest[player_id] = (updated, username, row)
    return latest


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def _observe_activity(redis_conn, target: ReconcileTarget, latest: dict) -> dict:
    """Fold this cycle's snapshots into the per-event activity ledger.

    A snapshot newer than the recorded one either resets the idle streak (the
    relevant-metric signature changed — the player is progressing) or
    increments it (WOM re-scraped them and nothing event-relevant moved).
    Returns the full ledger as {player_id: (epoch, streak, sig)}; participants
    never observed in-window simply have no entry (streak 0 ⇒ active tier, so
    everyone gets a baseline update early in the event)."""
    key = _womact_key(target.event_id)
    stored = {}
    try:
        for field, value in (redis_conn.hgetall(key) or {}).items():
            try:
                epoch_s, streak_s, sig = (_decode(value).split(":", 2) + ["", ""])[:3]
                stored[int(_decode(field))] = (int(epoch_s or 0), int(streak_s or 0), sig)
            except (TypeError, ValueError):
                continue
    except Exception:
        log.exception("womact read failed for event %s", target.event_id)
    relevant = set(target.skills.values()) | set(target.boss_metrics)
    changed = {}
    for player_id, (updated, _username, row) in latest.items():
        epoch, streak, sig = stored.get(player_id, (0, 0, ""))
        if not updated or updated <= epoch:
            continue
        new_sig = _row_signature(row, relevant)
        streak = 0 if new_sig != sig else streak + 1
        stored[player_id] = (updated, streak, new_sig)
        changed[str(player_id)] = f"{updated}:{streak}:{new_sig}"
    try:
        if changed:
            redis_conn.hset(key, mapping=changed)
        redis_conn.expire(key, _STATE_KEY_TTL)
    except Exception:
        log.exception("womact write failed for event %s", target.event_id)
    return stored


def _select_update_candidates(target: ReconcileTarget, latest: dict,
                              activity: dict, now_epoch: int, *,
                              active_interval: int,
                              flat_min_stale: Optional[int] = None) -> list:
    """Participants due a forced WOM update (pure; cooldown filtering happens
    at request time).

    Tiered mode (default): active participants — idle streak 0, which
    includes anyone never observed in-window — are due after the
    roster-scaled active interval; idle participants after their exponential
    backoff. Flat mode (key-moment passes pass ``flat_min_stale``): everyone
    staler than the bar is due, streaks ignored. Returns sorted
    [(prio, updated_epoch, player_id, username, success_cooldown)] — stubs
    first, then active before idle, stalest first within each tier."""
    out, seen = [], set()
    for entry in list(target.participants_by_wom.values()) + \
            list(target.participants_by_name.values()):
        player_id, player_name, _joined, is_stub = entry
        if player_id in seen:
            continue
        seen.add(player_id)
        updated, username, _row = latest.get(player_id) or (0, player_name, None)
        epoch, streak, _sig = activity.get(player_id, (0, 0, ""))
        updated = max(updated, epoch)
        if not username:
            continue
        if flat_min_stale is not None:
            interval = flat_min_stale
            prio = 0 if is_stub else 1
        else:
            interval = _idle_interval(streak, active_interval)
            prio = 0 if is_stub else (1 if streak == 0 else 2)
        if now_epoch - updated < interval:
            continue
        out.append((prio, updated, player_id, username,
                    max(300, int(interval * 0.9))))
    out.sort()
    return out


async def _request_updates(redis_conn, candidates: list, *, max_updates: int,
                           act_key: str, activity: dict, latest: dict,
                           active_interval: int) -> int:
    """Sequentially force-update up to ``max_updates`` candidates. Sequential
    means we only ever hold one shared-limiter slot, so the rest of the app's
    WOM traffic interleaves fairly; three consecutive failures (WOM down or
    limiter backlogged) abort the batch until next cycle.

    Failed attempts bump the player's idle streak so unrankable / rename-limbo
    accounts back off exponentially instead of burning the cap every cycle.
    Successful updates of participants who never appear in bulk-gained rows
    (event participants outside the WOM group — their updates can't feed
    reconciliation at all) bump the streak too, decaying them to ~1/day."""
    from utils.wiseoldman import request_player_update

    updated = 0
    consecutive_failures = 0
    for _prio, _seen, player_id, username, cooldown in candidates:
        if updated >= max_updates or consecutive_failures >= 3:
            break
        cooldown_key = f"wom:eventupd:{player_id}"
        try:
            if redis_conn.get(cooldown_key):
                continue
        except Exception:
            pass
        ok = await request_player_update(username)
        if not ok or player_id not in latest:
            epoch, streak, sig = activity.get(player_id, (0, 0, ""))
            streak += 1
            activity[player_id] = (epoch, streak, sig)
            try:
                redis_conn.hset(act_key, str(player_id), f"{epoch}:{streak}:{sig}")
            except Exception:
                pass
            backoff = _idle_interval(streak, active_interval)
            cooldown = (max(300, int(backoff * 0.9)) if ok
                        else max(_UPDATE_FAIL_COOLDOWN_SECONDS, backoff))
        try:
            redis_conn.set(cooldown_key, 1, ex=cooldown)
        except Exception:
            pass
        if ok:
            updated += 1
            consecutive_failures = 0
        else:
            consecutive_failures += 1
    return updated


async def _freshness_for_target(redis_conn, target: ReconcileTarget, rows: list,
                                *, max_updates: int,
                                min_stale: Optional[int] = None,
                                use_update_all: bool = False) -> dict:
    """One freshness pass: optional group-wide update-all (needs the group's
    verification code; only touches members >24h stale — WOM's rule), then the
    activity-tiered per-player rotation from this cycle's bulk-gained rows."""
    from utils.wiseoldman import UPDATE_ALL_BADCODE_PREFIX, request_group_update_all

    out = {"update_all": 0, "player_updates": 0, "candidates": 0,
           "active": 0, "idle": 0}
    if use_update_all:
        for _group_id, wom_gid, code in target.wom_groups:
            if not code:
                continue
            try:
                bad = redis_conn.get(f"{UPDATE_ALL_BADCODE_PREFIX}{wom_gid}")
            except Exception:
                bad = None
            if not bad:
                message = await request_group_update_all(wom_gid, code)
                if message:
                    out["update_all"] += 1
                    log.info("WOM update-all queued for group %s (event %s): %s",
                             wom_gid, target.event_id, message)

    latest = _latest_row_by_participant(target, rows)
    activity = _observe_activity(redis_conn, target, latest)
    if max_updates <= 0:
        return out
    participant_ids = ({e[0] for e in target.participants_by_wom.values()}
                       | {e[0] for e in target.participants_by_name.values()})
    out["idle"] = sum(1 for pid in participant_ids
                      if activity.get(pid, (0, 0, ""))[1] > 0)
    out["active"] = len(participant_ids) - out["idle"]
    active_interval = _active_interval(out["active"])
    # Candidate cooldowns sit slightly under each player's interval so they
    # are due again by the cycle after their snapshot ages past it, not one
    # full cycle later.
    candidates = _select_update_candidates(
        target, latest, activity, int(time.time()),
        active_interval=active_interval, flat_min_stale=min_stale)
    out["candidates"] = len(candidates)
    out["player_updates"] = await _request_updates(
        redis_conn, candidates, max_updates=max_updates,
        act_key=_womact_key(target.event_id), activity=activity,
        latest=latest, active_interval=active_interval)
    return out


async def _fetch_target_rows(target: ReconcileTarget, now: datetime) -> list:
    """Bulk-gained rows for a target outside the reconcile loop (key-moment
    passes) — usually a cache hit on the last cycle's fetch."""
    from utils.wiseoldman import get_group_bulk_gained

    if target.window_start is None:
        return []
    end_at = min(now, target.window_end) if target.window_end else now
    if end_at <= target.window_start:
        return []
    rows = []
    for _group_id, wom_gid, _code in target.wom_groups:
        fetched = await get_group_bulk_gained(wom_gid, target.window_start, end_at)
        rows.extend(fetched or [])
    return rows


# plan_targets is a DB round-trip; the key-moment scan runs every consumer
# tick, so it works off the copy cached by the last reconcile cycle.
_cached_targets: list = []
_cached_targets_at: float = 0.0


def _cache_targets(targets: list) -> None:
    global _cached_targets, _cached_targets_at
    _cached_targets = targets
    _cached_targets_at = time.time()


# Key-moment freshness passes run as fire-and-forget tasks (an update batch
# awaits one limiter slot per player — the consumer loop must not stall behind
# it). The once-per-event Redis flags are set BEFORE scheduling, so a crashed
# task is skipped rather than double-fired.
_background_tasks: set = set()


def _spawn_freshness(target: ReconcileTarget, redis_conn, now: datetime,
                     stage: str, **kwargs) -> None:
    async def _run():
        try:
            rows = await _fetch_target_rows(target, now)
            out = await _freshness_for_target(redis_conn, target, rows, **kwargs)
            log.info("WOM %s freshness pass for event %s: %s",
                     stage, target.event_id, out)
        except Exception:
            log.exception("WOM %s freshness pass failed for event %s",
                          stage, target.event_id)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def run_key_moment_updates(state, redis_conn,
                                 now: Optional[datetime] = None) -> None:
    """Fire the freshness pass at event activation and at the pre-end lead.

    Cheap per tick: Redis flag checks against the target list cached by the
    last reconcile cycle. The once-per-event flags make retries idempotent.
    """
    now = now or datetime.now()
    for target in _cached_targets:
        event = state.events.get(target.event_id)
        if event is None:
            continue
        try:
            if not redis_conn.get(_womupdall_key(target.event_id, "start")):
                redis_conn.set(_womupdall_key(target.event_id, "start"),
                               int(time.time()), ex=_STATE_KEY_TTL)
                # Deploy transition: events already hours into their window
                # get the flag without a spurious "activation" update burst.
                started_ago = ((now - target.window_start).total_seconds()
                               if target.window_start else 0)
                if started_ago < 2 * 3600:
                    _spawn_freshness(target, redis_conn, now, "activation",
                                     use_update_all=True,
                                     max_updates=WOM_EVENT_UPDATE_BUDGET)
            if target.window_end is not None:
                lead = target.window_end - timedelta(seconds=WOM_UPDATE_ALL_LEAD_SECONDS)
                if (now >= lead and now < target.window_end
                        and not redis_conn.get(_womupdall_key(target.event_id, "end"))):
                    redis_conn.set(_womupdall_key(target.event_id, "end"),
                                   int(time.time()), ex=_STATE_KEY_TTL)
                    # Final standings deserve the freshest data: tighter
                    # staleness bar and a doubled cap for this one pass.
                    _spawn_freshness(target, redis_conn, now, "pre-end",
                                     use_update_all=True,
                                     min_stale=WOM_EVENT_UPDATE_MIN_INTERVAL,
                                     max_updates=WOM_EVENT_UPDATE_MAX_PER_CYCLE * 2)
        except Exception:
            log.exception("Key-moment freshness pass failed for event %s",
                          target.event_id)


def pending_final_event_ids(state, redis_conn,
                            now: Optional[datetime] = None) -> list:
    """Active WOM-enabled events whose window has closed but whose final WOM
    pass hasn't run — the consumer runs these (and drains the queue) *before*
    the lifecycle sweep ends the event."""
    now = now or datetime.now()
    out = []
    for target in _cached_targets:
        event = state.events.get(target.event_id)
        if event is None or target.window_end is None:
            continue
        if now < target.window_end:
            continue
        try:
            if not redis_conn.get(_womfinal_key(target.event_id)):
                out.append(target.event_id)
        except Exception:
            continue
    return out
