"""Task 21 — explicit event lifecycle: activate / end / scheduler sweep.

One code path for both callers (spec requirement):

- web_api routes ``POST /events/{id}/activate`` / ``POST /events/{id}/end``
  call :func:`activate_event` / :func:`end_event` and map
  :class:`LifecycleError` onto RFC-7807 responses;
- the event consumer worker's ~60s tick calls :func:`run_lifecycle_sweep`,
  which activates scheduled drafts whose ``starts_at`` has passed (same
  validations — a failure enqueues a one-time admin notification instead of
  silently skipping) and ends active events whose ``ends_at`` has passed.

Lifecycle is one-way: ``draft -> active -> past`` (no reactivation in v1).

Tier concurrency (PRD D9): activation of a *group* event counts the group's
already-active events against the ``events_max_active`` entitlement of its
subscription tier (superadmin actors bypass via the entitlement resolver;
global events — ``group_id`` NULL, superadmin-run — are uncapped).

Module-level imports are stdlib-only on purpose (same convention as
``services/event_engine.py``): the unit tests load this file directly, so the
conftest ``db``/``services`` stubs never interfere. Anything DB/Redis-shaped
is lazy-imported inside functions.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

# One-time "scheduled activation failed" notification guard (per event). The
# key is deleted when the event finally activates so a later re-schedule can
# alert again.
ACTIVATION_FAILED_KEY = "events:sweep:activation-failed:{event_id}"
_ACTIVATION_FAILED_TTL = 7 * 24 * 3600


class LifecycleError(Exception):
    """A transition is not allowed. ``status`` is the HTTP status the route
    maps to (409 conflict / 422 validation / 403 entitlement)."""

    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = int(status)
        self.title = title
        self.detail = detail


# ══════════════════════════════════════════════════════════════════════════════
# Pure sweep decision (no I/O — unit-tested in isolation)
# ══════════════════════════════════════════════════════════════════════════════

def sweep_due(events, now: datetime) -> dict:
    """Which events one scheduler tick should transition.

    ``events`` is an iterable of dicts ``{id, status, starts_at, ends_at}``
    (datetimes or None). Draft events whose scheduled start has passed are due
    for activation; active events whose scheduled end has passed are due to
    end. Returns ``{"activate": [ids], "end": [ids]}``. Pure; no I/O.
    """
    activate, end = [], []
    for ev in events or []:
        status = ev.get("status")
        if status == "draft":
            starts_at = ev.get("starts_at")
            if starts_at is not None and starts_at <= now:
                activate.append(ev["id"])
        elif status == "active":
            ends_at = ev.get("ends_at")
            if ends_at is not None and ends_at <= now:
                end.append(ev["id"])
    return {"activate": activate, "end": end}


# ══════════════════════════════════════════════════════════════════════════════
# Validation / capacity
# ══════════════════════════════════════════════════════════════════════════════

def activation_blockers(session, event, now: Optional[datetime] = None) -> list:
    """Human-readable reasons ``event`` cannot activate right now (empty when
    it can): ``ends_at`` (if set) must be in the future; a bingo event needs a
    complete ``board_size²`` board whose bound cells reference this event's
    tasks. Standard/global events need ≥1 team. clan_vs_clan needs ≥2 accepted
    clans; teams are optional there — with none it runs whole-clan vs
    whole-clan (auto-seeded at activation), but once any team exists every
    accepted clan must have one."""
    from db.models import EventBingoCell, EventTask, EventTeam

    now = now or datetime.now()
    blockers = []

    is_cvc = (getattr(event, "mode", None) or "standard") == "clan_vs_clan"
    team_count = session.query(EventTeam).filter(EventTeam.event_id == event.id).count()

    if not is_cvc:
        if team_count < 1:
            blockers.append("The event needs at least one team.")
    else:
        from db.models import EventGroup

        accepted = [
            gid for (gid,) in
            session.query(EventGroup.group_id)
            .filter(EventGroup.event_id == event.id, EventGroup.status == "accepted")
            .all()
        ]
        if len(accepted) < 2:
            blockers.append(
                "A clan-vs-clan event needs at least two accepted clans "
                "(the host plus an opponent that accepted its invitation)."
            )
        # Teams are optional: with none, activation seeds one whole-clan team
        # per clan (anyone-vs-anyone). But a half-built roster is ambiguous, so
        # once any team exists every accepted clan must have at least one.
        if team_count:
            team_gids = {
                gid for (gid,) in
                session.query(EventTeam.group_id)
                .filter(EventTeam.event_id == event.id)
                .all()
            }
            clans_without_teams = [g for g in accepted if g not in team_gids]
            if accepted and clans_without_teams:
                blockers.append(
                    f"Every accepted clan needs at least one team, or remove all "
                    f"teams to run whole-clan vs whole-clan — "
                    f"{len(clans_without_teams)} clan(s) have none yet."
                )

    if event.ends_at is not None and event.ends_at <= now:
        blockers.append("The end date is in the past — move it into the future first.")

    if event.has_bingo:
        cells = (
            session.query(EventBingoCell)
            .filter(EventBingoCell.event_id == event.id)
            .all()
        )
        size = int(event.board_size or 0)
        if not cells:
            blockers.append("The bingo board has no cells — lay out the board first.")
        elif size * size != len(cells):
            blockers.append(
                f"The bingo board is incomplete: a {size}×{size} board needs "
                f"{size * size} cells, found {len(cells)}."
            )
        if cells:
            task_ids = {
                tid for (tid,) in session.query(EventTask.id)
                .filter(EventTask.event_id == event.id).all()
            }
            unbound = sorted(
                c.idx for c in cells
                if c.task_id is not None and c.task_id not in task_ids
            )
            if unbound:
                blockers.append(
                    f"Bingo cell(s) {unbound} are bound to tasks that do not "
                    "belong to this event — rebind or free them in the designer."
                )
    return blockers


def assert_activation_capacity(session, event, user=None) -> None:
    """Tier concurrency check (PRD D9), enforced at activation only.

    Group events: the group's ``status='active'`` event count must be below
    the ``events_max_active`` entitlement of its tier (superadmin ``user``
    resolves to effectively-unlimited via the entitlement resolver; the sweep
    passes no user, so real tier limits apply). Global events skip entirely.
    Raises :class:`LifecycleError` (403/409).
    """
    if not event.group_id:
        return  # global events (superadmin-run) are uncapped (PRD §7.1)

    from web_api.entitlements import resolve_group_entitlements

    entitlements = resolve_group_entitlements(session, event.group_id, user=user)
    if not entitlements.get("events"):
        raise LifecycleError(
            403, "Subscription required",
            "This group's subscription tier does not include events.",
        )
    limit = int(entitlements.get("events_max_active") or 0)

    from db.models import Event

    active = (
        session.query(Event)
        .filter(Event.group_id == event.group_id, Event.status == "active")
        .count()
    )
    if active >= limit:
        raise LifecycleError(
            409, "Active event limit reached",
            f"This group already has {active} active event(s) and its "
            f"subscription tier allows {limit} at a time. End an active event "
            "or upgrade the subscription, then activate this one.",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _ts(dt) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def final_standings(session, event_id: int, limit: int = 5) -> list:
    """[{team_id, name, score}] best-first."""
    from db.models import EventTeam

    rows = (
        session.query(EventTeam)
        .filter(EventTeam.event_id == event_id)
        .order_by(EventTeam.score.desc(), EventTeam.id.asc())
        .limit(limit)
        .all()
    )
    return [{"team_id": t.id, "name": t.name, "score": int(t.score or 0)} for t in rows]


def _representative_player_id(session, event_id: int) -> Optional[int]:
    """A player id to hang lifecycle notification_queue rows on
    (``notification_queue.player_id`` is NOT NULL; the event sender never uses
    it). Prefers the event's earliest roster member, falls back to any player.
    """
    from db.models import EventTeam, EventTeamMember, Player

    row = (
        session.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == event_id)
        .order_by(EventTeamMember.joined_at.asc())
        .first()
    )
    if row:
        return row[0]
    row = session.query(Player.player_id).order_by(Player.player_id.asc()).first()
    return row[0] if row else None


def _mark_active_in_redis(event_id: int, active: bool) -> None:
    """Immediate ``events:active`` gate update (the worker's periodic matcher
    refresh reconciles the set from the DB anyway). Best-effort."""
    try:
        from services.event_engine import ACTIVE_EVENTS_KEY
        from utils.redis import redis_client

        conn = getattr(redis_client, "client", None)
        if conn is None:
            return
        if active:
            conn.sadd(ACTIVE_EVENTS_KEY, int(event_id))
        else:
            conn.srem(ACTIVE_EVENTS_KEY, int(event_id))
    except Exception:
        pass


def _publish(event_id: int, data: dict) -> None:
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, data)
    except Exception:
        pass


def _audit(session, actor_user_id, event, action: str, before: dict, after: dict) -> None:
    from db.models import AuditLog

    session.add(AuditLog(
        actor_user_id=actor_user_id,
        group_id=event.group_id,
        action=action,
        target=f"web_events.{event.id}",
        before=json.dumps(before, default=str),
        after=json.dumps(after, default=str),
    ))


# ══════════════════════════════════════════════════════════════════════════════
# Transitions (routes + sweep share these — caller owns the commit)
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_whole_clan_teams(session, event) -> int:
    """clan_vs_clan fallback: when an event activates with no teams, seed one
    ``auto_clan`` team per accepted clan (named after the clan). Each represents
    the whole clan — the matcher credits every current member of ``group_id`` to
    it (see :func:`services.event_engine.load_matcher_state`), so the event runs
    as "anyone in clan A vs anyone in clan B". No-op for other modes or when
    teams already exist. Returns the number of teams created."""
    if (getattr(event, "mode", None) or "standard") != "clan_vs_clan":
        return 0
    from db.models import EventGroup, EventTeam, Group

    if session.query(EventTeam).filter(EventTeam.event_id == event.id).count():
        return 0
    accepted = (
        session.query(EventGroup.group_id)
        .filter(EventGroup.event_id == event.id, EventGroup.status == "accepted")
        .all()
    )
    created = 0
    for (gid,) in accepted:
        group = session.query(Group).filter(Group.group_id == gid).first()
        name = ((getattr(group, "group_name", None) or "").strip() or f"Clan {gid}")[:80]
        session.add(EventTeam(
            event_id=event.id, name=name, score=0, group_id=gid, auto_clan=True,
        ))
        created += 1
    if created:
        session.flush()
    return created


def activate_event(session, event, *, actor_user_id=None, user=None,
                   now: Optional[datetime] = None) -> None:
    """draft -> active. Validates readiness and tier capacity, stamps
    status/activated_at (and ``starts_at`` when unscheduled), grants bingo
    free cells to every team, enqueues the ``event_started`` announcement,
    updates the Redis gate and publishes an SSE ``{kind: "started"}`` frame.
    Audit-logged as ``event.activate``. Raises :class:`LifecycleError`;
    the caller owns the commit (and the post-commit admin bump).
    """
    from services import event_engine

    now = now or datetime.now()
    if event.status == "past":
        raise LifecycleError(409, "Event is over",
                             "A past event cannot be reactivated.")
    if event.status == "active":
        raise LifecycleError(409, "Already active", "This event is already active.")

    blockers = activation_blockers(session, event, now=now)
    if blockers:
        raise LifecycleError(422, "Event is not ready to start", " ".join(blockers))
    assert_activation_capacity(session, event, user=user)

    before = {"status": event.status, "starts_at": _ts(event.starts_at)}
    event.status = "active"
    event.activated_at = now
    if event.starts_at is None:
        event.starts_at = now
    session.flush()

    # The Discord scheduled-event mirror goes live now: with the default
    # ``discord_event_policy='on_activate'`` a draft desired no guilds, so
    # this re-sync is what seeds the ``web_event_guilds`` rows the bot
    # reconciler turns into real Discord events. (No-op for ``immediate``
    # events — their rows already exist and stay synced.)
    from services.event_scheduled_events import sync_event_guilds

    sync_event_guilds(session, event)

    # clan_vs_clan with no teams set up: seed a whole-clan team per accepted
    # clan so it runs as "anyone in clan A vs anyone in clan B". Must precede
    # grant_free_cells so the freshly-created teams receive their free cells.
    _ensure_whole_clan_teams(session, event)

    # Free cells complete for every team "from activation" (Task 20/21).
    event_engine.grant_free_cells(session, event)

    from db.models import EventTeam

    team_count = (
        session.query(EventTeam).filter(EventTeam.event_id == event.id).count()
    )
    ev_dict = event_engine._event_to_dict(event)
    event_engine._enqueue_notification(
        session, "event_started", ev_dict,
        _representative_player_id(session, event.id),
        {
            "description": event.description or None,
            "starts_at": _ts(event.starts_at),
            "ends_at": _ts(event.ends_at),
            "team_count": team_count,
        },
    )
    _audit(session, actor_user_id, event, "event.activate", before, {
        "status": "active",
        "activated_at": _ts(event.activated_at),
        "starts_at": _ts(event.starts_at),
    })
    session.flush()

    _mark_active_in_redis(event.id, True)
    _publish(event.id, {
        "kind": "started", "event_id": event.id, "name": event.name,
        "starts_at": _ts(event.starts_at), "ends_at": _ts(event.ends_at),
    })


def end_event(session, event, *, actor_user_id=None,
              now: Optional[datetime] = None) -> list:
    """active -> past. Stamps status/ended_at, enqueues ``event_ended`` with
    the final standings (top 5), updates the Redis gate and publishes an SSE
    ``{kind: "ended"}`` frame. Audit-logged as ``event.end``. Returns the
    standings. Raises :class:`LifecycleError`; the caller owns the commit.
    """
    from services import event_engine

    now = now or datetime.now()
    if event.status == "draft":
        raise LifecycleError(409, "Not active",
                             "A draft event cannot be ended — it never started.")
    if event.status == "past":
        raise LifecycleError(409, "Already ended", "This event has already ended.")

    before = {"status": event.status, "ended_at": _ts(event.ended_at)}
    event.status = "past"
    event.ended_at = now
    session.flush()

    # Retire the mirrored Discord scheduled event(s): the core bot's
    # reconciler deletes them and drops the rows
    # (services/event_scheduled_events.py).
    from db.models import EventGuild

    session.query(EventGuild).filter(EventGuild.event_id == event.id).update(
        {EventGuild.sync_status: "delete_pending"}, synchronize_session=False,
    )

    standings = final_standings(session, event.id, limit=5)
    ev_dict = event_engine._event_to_dict(event)
    event_engine._enqueue_notification(
        session, "event_ended", ev_dict,
        _representative_player_id(session, event.id),
        {"standings": standings, "ended_at": _ts(event.ended_at)},
    )
    _audit(session, actor_user_id, event, "event.end", before, {
        "status": "past",
        "ended_at": _ts(event.ended_at),
        "standings": standings,
    })
    session.flush()

    _mark_active_in_redis(event.id, False)
    _publish(event.id, {
        "kind": "ended", "event_id": event.id, "name": event.name,
        "standings": standings,
    })
    return standings


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler sweep (worker tick)
# ══════════════════════════════════════════════════════════════════════════════

def _notify_activation_failure(session, redis_conn, event, detail: str) -> None:
    """Enqueue an admin-channel ``event_activation_failed`` notification —
    once per event, guarded by a Redis NX key (no retry spam: the sweep hits a
    broken scheduled draft every tick until an admin fixes or unschedules it).
    """
    if redis_conn is not None:
        try:
            fresh = redis_conn.set(
                ACTIVATION_FAILED_KEY.format(event_id=event.id),
                detail, nx=True, ex=_ACTIVATION_FAILED_TTL,
            )
            if not fresh:
                return  # already notified for this event
        except Exception:
            pass

    from services import event_engine

    event_engine._enqueue_notification(
        session, "event_activation_failed", event_engine._event_to_dict(event),
        _representative_player_id(session, event.id),
        {"reason": detail, "starts_at": _ts(event.starts_at)},
    )


def _clear_activation_failure(redis_conn, event_id: int) -> None:
    if redis_conn is None:
        return
    try:
        redis_conn.delete(ACTIVATION_FAILED_KEY.format(event_id=event_id))
    except Exception:
        pass


def run_lifecycle_sweep(session, redis_conn=None, now: Optional[datetime] = None) -> dict:
    """One scheduler tick: activate due drafts / end due actives through the
    exact same transition functions the routes use. Commits per transition
    (a failed activation rolls back and notifies the admin channel once).
    Returns ``{"activated": [...], "ended": [...], "failed": [{id, detail}]}``.
    """
    from db.models import Event

    now = now or datetime.now()
    rows = (
        session.query(Event)
        .filter(Event.status.in_(("draft", "active")))
        .all()
    )
    due = sweep_due(
        [{"id": e.id, "status": e.status, "starts_at": e.starts_at,
          "ends_at": e.ends_at} for e in rows],
        now,
    )
    by_id = {e.id: e for e in rows}
    summary = {"activated": [], "ended": [], "failed": []}

    for event_id in due["activate"]:
        event = by_id[event_id]
        try:
            activate_event(session, event, now=now)
            session.commit()
            summary["activated"].append(event_id)
            _clear_activation_failure(redis_conn, event_id)
        except LifecycleError as exc:
            session.rollback()
            try:
                _notify_activation_failure(session, redis_conn, event, exc.detail)
                session.commit()
            except Exception:
                session.rollback()
            summary["failed"].append({"id": event_id, "detail": exc.detail})

    for event_id in due["end"]:
        event = by_id[event_id]
        try:
            end_event(session, event, now=now)
            session.commit()
            summary["ended"].append(event_id)
        except LifecycleError as exc:
            session.rollback()
            summary["failed"].append({"id": event_id, "detail": exc.detail})

    return summary
