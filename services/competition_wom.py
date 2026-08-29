"""SOTW/BOTW × WiseOldMan competition linkage (the ``linked``/``created``
source modes).

A linked event mirrors a real WOM competition: every poll cycle fetches the
competition detail (ONE request per event — 20 concurrent competitions cost
~4% of the shared rate budget), emits each matched participant's window
deltas as synthetic envelopes onto the low-priority WOM queue — the same
shapes, seen-gates and clamps as the group reconciler, whose emission helper
this module reuses wholesale — and caches the participants DropTracker
doesn't know (display-only greyed rows). Plugin data for the same players
keeps flowing live in between; the absolute-value baselines/watermarks make
double-counting structurally impossible.

The competition detail is fetched RAW (``utils.wiseoldman.get_competition_raw``)
because the pinned wom.py fork's typed model predates multi-metric
competitions — the validator must SEE the modern ``metrics`` array to refuse
what it can't mirror (multi-metric, team-type) instead of silently racing
only the primary metric.

Dates are coupled WOM→DT: at link time the event adopts the competition's
window, and every poll re-syncs drift (a window disagreeing with the comp
would freeze legitimate gains out at the envelope window gate). The DT-side
PATCH rejects date edits for linked events.

``created`` mode (phase 3) adds the write half — DropTracker creates the
competition, holds its verification code, and mirrors edits/deletes — and
then behaves exactly like ``linked``.

Runs inside the ``droptracker-events`` consumer loop, next to the group
reconciler. Module-level imports stay stdlib-only (the unit tests load this
file directly; DB/Redis/WOM are lazy-imported inside functions).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("competition_wom")

WOM_COMPETITION_POLL_SECONDS = int(os.getenv("WOM_COMPETITION_POLL_SECONDS", "300"))

# Date drift below this is ignored (ISO parsing jitter, not an edit).
_DRIFT_TOLERANCE_SECONDS = 60

_STATE_KEY_TTL = 60 * 86400


def _womcompfinal_key(event_id: int) -> str:
    return f"events:{event_id}:womcompfinal"


def _parse_wom_ts_dt(value) -> Optional[datetime]:
    """WOM ISO-8601 'Z' timestamp → naive-UTC datetime (server tz is UTC)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Competition detail parsing (pure)
# ══════════════════════════════════════════════════════════════════════════════

def parse_competition(raw) -> Optional[dict]:
    """Normalize a raw ``GET /competitions/:id`` response.

    Returns ``{id, title, type, metric, metrics, multi_metric, group_id,
    participant_count, starts_at, ends_at (naive-UTC datetimes),
    participations: [{wom_player_id, username, display_name, updated_at,
    start, end, gained, player_raw}]}`` or None for garbage. Values the
    response lacks come back None/empty — callers decide what is fatal."""
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    metrics = [m.get("metric") for m in (raw.get("metrics") or ())
               if isinstance(m, dict) and m.get("metric")]
    primary = raw.get("metric") or (metrics[0] if metrics else None)
    participations = []
    for p in raw.get("participations") or ():
        if not isinstance(p, dict):
            continue
        player = p.get("player") or {}
        progress = p.get("progress") if isinstance(p.get("progress"), dict) else {}
        if not progress:
            # Modern shape: per-metric deltas; the first entry mirrors the
            # ranked metric for a single-metric comp (the only kind we link).
            deltas = p.get("deltas") or ()
            first = deltas[0] if deltas and isinstance(deltas[0], dict) else {}
            progress = first.get("values") if isinstance(first.get("values"), dict) else {}

        def _num(key):
            try:
                value = progress.get(key)
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        participations.append({
            "wom_player_id": player.get("id"),
            "username": player.get("username"),
            "display_name": player.get("displayName") or player.get("username"),
            "updated_at": player.get("updatedAt"),
            "start": _num("start"),
            "end": _num("end"),
            "gained": _num("gained") or 0,
            "player_raw": player,
        })
    return {
        "id": raw.get("id"),
        "title": (str(raw.get("title") or "").strip() or None),
        "type": raw.get("type"),
        "metric": primary,
        "metrics": metrics or ([primary] if primary else []),
        "multi_metric": len(metrics) > 1,
        "group_id": raw.get("groupId"),
        "participant_count": (raw.get("participantCount")
                              if raw.get("participantCount") is not None
                              else len(participations)),
        "starts_at": _parse_wom_ts_dt(raw.get("startsAt")),
        "ends_at": _parse_wom_ts_dt(raw.get("endsAt")),
        "participations": participations,
    }


def competition_link_problems(comp: Optional[dict], event_kind: str,
                              now: Optional[datetime] = None) -> list:
    """Why this competition can NOT back a sotw/botw event — [] when linkable.
    Reasons are short machine codes; the routes turn them into copy."""
    from utils.wiseoldman import wom_metric_kind

    now = now or datetime.now()
    if comp is None:
        return ["not_found"]
    problems = []
    if comp.get("type") == "team":
        problems.append("team_competition")
    if comp.get("multi_metric"):
        problems.append("multi_metric")
    kind_for_metric = wom_metric_kind(comp.get("metric"))
    if kind_for_metric is None:
        problems.append("unsupported_metric")
    elif event_kind == "sotw" and kind_for_metric != "skill":
        problems.append("metric_kind_mismatch")
    elif event_kind == "botw" and kind_for_metric != "boss":
        problems.append("metric_kind_mismatch")
    ends_at = comp.get("ends_at")
    if ends_at is not None and ends_at <= now:
        problems.append("finished")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
# Target planning (blocking; run in a worker thread)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CompetitionTarget:
    event_id: int
    competition_id: int
    source_mode: str                       # linked | created
    verification_code: Optional[str]       # created mode only (phase 3)
    metric_kind: Optional[str]             # skill | boss (from the task config)
    # The group reconciler's target shape, reused verbatim so its emission
    # helper (matching, seen-gate, clamps, envelope shapes) needs no fork.
    recon: object = field(default=None, repr=False)


def _plan_targets_db(state) -> list:
    """CompetitionTargets for the active linked/created competition events in
    ``state``. Mirrors the group reconciler's planner: roster maps come from
    the matcher state, player identities from one bulk query."""
    from api.core import get_db_session, reset_db_connections
    from db.models import COMPETITION_EVENT_KINDS, EventCompetition, Player
    from services.event_wom_reconciler import ReconcileTarget

    candidates = {}
    for event_id, event in state.events.items():
        if (event.get("kind") or "standard") not in COMPETITION_EVENT_KINDS:
            continue
        comp_task = next(
            (t for t in state.tasks_by_event.get(event_id, [])
             if t.get("type") == "competition"), None)
        if comp_task is None:
            continue
        comp_index = comp_task.get("competition") or {}
        metric_kind = comp_index.get("metric_kind")
        skills, bosses = {}, set()
        if metric_kind == "skill" and comp_index.get("skill"):
            from utils.wiseoldman import wom_skill_metric

            slug = wom_skill_metric(comp_index["skill"])
            if slug:
                skills[comp_index["skill"]] = slug
        elif metric_kind == "boss":
            metrics = comp_task.get("wom_metrics")
            if isinstance(metrics, dict):
                bosses = set(metrics)
        if not skills and not bosses:
            continue
        candidates[event_id] = (metric_kind, skills, bosses)
    if not candidates:
        return []

    rosters = {}
    for player_id, memberships in state.participants.items():
        for event_id, _team_id, joined_at in memberships:
            if event_id in candidates:
                rosters.setdefault(event_id, {})[player_id] = joined_at

    session = get_db_session()
    try:
        comp_rows = {
            row.event_id: row
            for row in session.query(EventCompetition).filter(
                EventCompetition.event_id.in_(list(candidates.keys())),
                EventCompetition.source_mode.in_(("linked", "created")),
                EventCompetition.wom_competition_id.isnot(None)).all()
        }
        all_player_ids = {
            p for eid, roster in rosters.items()
            if eid in comp_rows for p in roster
        }
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
    for event_id, (metric_kind, skills, bosses) in candidates.items():
        comp_row = comp_rows.get(event_id)
        if comp_row is None:
            continue
        event = state.events.get(event_id) or {}
        recon = ReconcileTarget(
            event_id=event_id,
            event_name=event.get("name") or f"event {event_id}",
            window_start=event.get("window_start"),
            window_end=event.get("window_end"),
            windows=list(event.get("windows") or ()),
            wom_groups=[],
            skills=skills,
            boss_metrics=bosses,
            effort_metrics=set(),
        )
        for player_id, joined_at in (rosters.get(event_id) or {}).items():
            p = players.get(player_id)
            if p is None:
                continue
            is_stub = bool(p.account_hash
                           and str(p.account_hash).startswith("wom_temp_"))
            entry = (player_id, p.player_name, joined_at, is_stub)
            if p.wom_id:
                recon.participants_by_wom[int(p.wom_id)] = entry
            if p.player_name:
                recon.participants_by_name[
                    " ".join(str(p.player_name).strip().lower().split())] = entry
        targets.append(CompetitionTarget(
            event_id=event_id,
            competition_id=int(comp_row.wom_competition_id),
            source_mode=comp_row.source_mode,
            verification_code=comp_row.wom_competition_code,
            metric_kind=metric_kind,
            recon=recon,
        ))
    return targets


async def plan_competition_targets(state) -> list:
    return await asyncio.to_thread(_plan_targets_db, state)


# ══════════════════════════════════════════════════════════════════════════════
# Sync-state persistence (blocking; run in a worker thread)
# ══════════════════════════════════════════════════════════════════════════════

def _store_sync_state_db(event_id: int, *, standings=None, comp=None,
                         error: Optional[str] = None) -> dict:
    """Persist one poll's outcome onto ``web_event_competitions`` (+ the
    event's dates when the competition window drifted). Returns
    ``{"drifted": bool}``."""
    from api.core import get_db_session, reset_db_connections
    from db.models import Event, EventCompetition

    drifted = False
    session = get_db_session()
    try:
        row = (session.query(EventCompetition)
               .filter(EventCompetition.event_id == event_id).first())
        if row is None:
            return {"drifted": False}
        now = datetime.now()
        if error is not None:
            row.wom_sync_error = str(error)[:255]
            session.commit()
            return {"drifted": False}
        row.wom_sync_error = None
        row.wom_synced_at = now
        if standings is not None:
            row.wom_standings = json.dumps(standings)
        if comp is not None:
            row.wom_title = (comp.get("title") or None)
            starts_at, ends_at = comp.get("starts_at"), comp.get("ends_at")
            event = session.query(Event).filter(Event.id == event_id).first()

            def _drift(a, b) -> bool:
                if a is None or b is None:
                    return a is not b
                return abs((a - b).total_seconds()) > _DRIFT_TOLERANCE_SECONDS

            if event is not None and event.status != "past":
                # WOM owns the window for linked/created events: an admin who
                # edits the comp on WOM sees the DT event follow within one
                # poll (and the DT PATCH refuses date edits on these events).
                # A PAST event's window is history — never rewritten.
                if starts_at is not None and _drift(event.starts_at, starts_at):
                    event.starts_at = starts_at
                    drifted = True
                if ends_at is not None and _drift(event.ends_at, ends_at):
                    event.ends_at = ends_at
                    drifted = True
            row.wom_starts_at = starts_at
            row.wom_ends_at = ends_at
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        reset_db_connections()
    return {"drifted": drifted}


def _standings_cache(comp: dict, recon) -> list:
    """The ``wom_standings`` cache rows: EVERY participation, with the DT
    player resolved where possible (the standings merge dedupes resolved/
    same-named rows against the richer ledger fold)."""
    from services.event_wom_reconciler import _match_participant

    out = []
    for p in comp.get("participations") or ():
        entry = _match_participant(recon, p.get("player_raw") or {})
        out.append({
            "wom_player_id": p.get("wom_player_id"),
            "display_name": p.get("display_name"),
            "gained": max(int(p.get("gained") or 0), 0),
            "start": p.get("start"),
            "end": p.get("end"),
            "player_id": entry[0] if entry is not None else None,
        })
    out.sort(key=lambda r: -r["gained"])
    return out


# ══════════════════════════════════════════════════════════════════════════════
# The poll cycle
# ══════════════════════════════════════════════════════════════════════════════

def _new_stats() -> dict:
    # Superset of the keys the group reconciler's ``_emit_for_row`` touches
    # (players_emitted/stale/unmatched) — it fills this dict directly.
    return {"targets": 0, "fetched": 0, "fetch_failed": 0, "not_linkable": 0,
            "envelopes": 0, "players_emitted": 0, "players_unmatched": 0,
            "players_stale": 0, "players_no_metrics": 0, "drifted": 0}


def _participation_row(p: dict, slug) -> dict:
    """Adapt one parsed participation into the bulk-gained row shape the
    group reconciler's ``_emit_for_row`` consumes — one metric entry whose
    start is the COMPETITION-window opening value (the authoritative WOM
    baseline the folds seed from). ``endDate`` stays None so the snap epoch
    falls back to the player's own updatedAt (then window-clamps)."""
    return {
        "player": p.get("player_raw") or {},
        "endDate": None,
        "data": [{"metric": slug, "start": p.get("start") or 0,
                  "end": p.get("end") or 0}],
    }


async def _poll_target(redis_conn, target: CompetitionTarget, *, now: datetime,
                       force: bool, final: bool, stats: dict) -> None:
    from services.event_wom_reconciler import _emit_for_row, scoring_bounds
    from utils.wiseoldman import get_competition_raw

    raw = await get_competition_raw(target.competition_id)
    if raw is None:
        stats["fetch_failed"] += 1
        await asyncio.to_thread(
            _store_sync_state_db, target.event_id,
            error="WiseOldMan fetch failed (rate-limited or unavailable)")
        return
    stats["fetched"] += 1
    comp = parse_competition(raw)
    event_kind = "sotw" if target.metric_kind == "skill" else "botw"
    problems = [p for p in competition_link_problems(comp, event_kind, now=now)
                if p != "finished"]  # a finished comp still serves its result
    if problems:
        stats["not_linkable"] += 1
        await asyncio.to_thread(
            _store_sync_state_db, target.event_id,
            error=f"competition no longer linkable: {', '.join(problems)}")
        return

    bounds = scoring_bounds(target.recon, now, final=final)
    clamp_lo = clamp_hi = None
    if bounds is not None:
        clamp_lo = int(bounds[0].timestamp())
        clamp_hi = int(bounds[1].timestamp())

    slug = comp.get("metric")
    for p in comp.get("participations") or ():
        if p.get("end") is None:
            stats["players_no_metrics"] += 1
            continue
        stats["envelopes"] += _emit_for_row(
            redis_conn, target.recon, _participation_row(p, slug),
            clamp_epoch=clamp_hi, clamp_lo=clamp_lo, force=force, stats=stats)

    result = await asyncio.to_thread(
        _store_sync_state_db, target.event_id,
        standings=_standings_cache(comp, target.recon), comp=comp)
    if result.get("drifted"):
        stats["drifted"] += 1
        log.info("Competition %s (event %s): window/title drift synced from WOM",
                 target.competition_id, target.event_id)


async def poll_linked_once(state, redis_conn, now: Optional[datetime] = None,
                           *, force: bool = False) -> dict:
    """One poll cycle over every linked/created competition event. One WOM
    request per event; failures stamp ``wom_sync_error`` and never raise."""
    now = now or datetime.now()
    stats = _new_stats()
    targets = await plan_competition_targets(state)
    stats["targets"] = len(targets)
    for target in targets:
        try:
            await _poll_target(redis_conn, target, now=now, force=force,
                               final=False, stats=stats)
            await _maybe_update_all(redis_conn, target, now)
        except Exception:
            log.error("Competition poll failed for event %s (comp %s)",
                      target.event_id, target.competition_id, exc_info=True)
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# Created-mode write mirroring
# ══════════════════════════════════════════════════════════════════════════════

def _peek_created_db(event_id: int) -> Optional[dict]:
    from api.core import get_db_session, reset_db_connections
    from db.models import Event, EventCompetition

    session = get_db_session()
    try:
        row = (session.query(EventCompetition)
               .filter(EventCompetition.event_id == event_id).first())
        if (row is None or row.source_mode != "created"
                or not row.wom_competition_id or not row.wom_competition_code):
            return None
        ev = session.query(Event).filter(Event.id == event_id).first()
        return {
            "competition_id": int(row.wom_competition_id),
            "code": row.wom_competition_code,
            "ended_at": getattr(ev, "ended_at", None) if ev is not None else None,
        }
    finally:
        session.close()
        reset_db_connections()


async def mirror_competition_end(event_id: int) -> bool:
    """A DT-side end — manual cut-short or the scheduled sweep — closes the
    created WOM competition too (``endsAt`` = the actual end), so the public
    WOM page stops collecting gains the DT event no longer counts. Best-effort;
    linked/hosted events no-op. Failure stamps ``wom_sync_error``."""
    info = await asyncio.to_thread(_peek_created_db, event_id)
    if info is None:
        return False
    from utils.wiseoldman import edit_wom_competition

    ended_at = info["ended_at"] or datetime.now()
    ok = await edit_wom_competition(info["competition_id"], info["code"],
                                    ends_at=ended_at)
    if not ok:
        def _stamp():
            from api.core import get_db_session, reset_db_connections
            from db.models import EventCompetition

            session = get_db_session()
            try:
                row = (session.query(EventCompetition)
                       .filter(EventCompetition.event_id == event_id).first())
                if row is not None:
                    row.wom_sync_error = ("end mirror failed — the WOM "
                                          "competition is still open")[:255]
                    session.commit()
            finally:
                session.close()
                reset_db_connections()

        await asyncio.to_thread(_stamp)
    return ok


# Update-all lead before the competition's start/end — the created comp's own
# code buys competition-scoped refresh rights (the reconciler's group-level
# pass needs the GROUP code, which linked-only groups may not have shared).
WOM_UPDATE_ALL_LEAD_SECONDS = int(os.getenv("WOM_UPDATE_ALL_LEAD_SECONDS", "1800"))


def _womcompupdall_key(event_id: int, stage: str) -> str:
    return f"events:{event_id}:womcompupdall:{stage}"


async def _maybe_update_all(redis_conn, target: CompetitionTarget,
                            now: datetime) -> None:
    """Fire the one-shot competition update-all at the key moments (inside
    the lead window before start and before end). Created mode only — it
    needs the competition's own verification code."""
    if target.source_mode != "created" or not target.verification_code:
        return
    recon = target.recon
    lead = timedelta(seconds=WOM_UPDATE_ALL_LEAD_SECONDS)
    stages = []
    if recon.window_start is not None and \
            recon.window_start - lead <= now < recon.window_start:
        stages.append("start")
    if recon.window_end is not None and \
            recon.window_end - lead <= now < recon.window_end:
        stages.append("end")
    for stage in stages:
        key = _womcompupdall_key(target.event_id, stage)
        try:
            if not redis_conn.set(key, int(time.time()), nx=True,
                                  ex=_STATE_KEY_TTL):
                continue
        except Exception:
            continue
        from utils.wiseoldman import request_competition_update_all

        message = await request_competition_update_all(
            target.competition_id, target.verification_code)
        log.info("Competition update-all (%s) for event %s: %s",
                 stage, target.event_id, message or "failed")


# ══════════════════════════════════════════════════════════════════════════════
# End-of-event final pass (mirrors the group reconciler's)
# ══════════════════════════════════════════════════════════════════════════════

def pending_final_competition_ids(state, redis_conn,
                                  now: Optional[datetime] = None) -> list:
    """Active linked/created competition events whose window has closed but
    whose final competition poll hasn't run — the consumer drains these
    before the lifecycle sweep ends the event."""
    from db.models import COMPETITION_EVENT_KINDS

    now = now or datetime.now()
    out = []
    for event_id, event in state.events.items():
        if (event.get("kind") or "standard") not in COMPETITION_EVENT_KINDS:
            continue
        window_end = event.get("window_end")
        if window_end is None or now < window_end:
            continue
        try:
            if not redis_conn.get(_womcompfinal_key(event_id)):
                out.append(event_id)
        except Exception:
            continue
    return out


async def final_competition_poll(state, redis_conn, event_id: int) -> Optional[dict]:
    """End-of-event pass: busts the short raw cache, ignores the womseen gate
    (guid index + watermarks absorb rework), flags completion. Events whose
    source mode is hosted produce no target and just get flagged (so the
    pending list converges)."""
    now = datetime.now()
    stats = _new_stats()
    targets = [t for t in await plan_competition_targets(state)
               if t.event_id == event_id]
    if targets:
        target = targets[0]
        try:
            from utils.wiseoldman import _REDIS_COMPETITION_PREFIX
            from utils.redis import redis_client

            redis_client.client.delete(
                f"{_REDIS_COMPETITION_PREFIX}{target.competition_id}")
        except Exception:
            pass
        await _poll_target(redis_conn, target, now=now, force=True, final=True,
                           stats=stats)
    try:
        redis_conn.set(_womcompfinal_key(event_id), int(time.time()),
                       ex=7 * 86400)
    except Exception:
        pass
    return stats if targets else None
