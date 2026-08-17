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
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# One-time "scheduled activation failed" notification guard (per event). The
# key is deleted when the event finally activates so a later re-schedule can
# alert again.
ACTIVATION_FAILED_KEY = "events:sweep:activation-failed:{event_id}"
_ACTIVATION_FAILED_TTL = 7 * 24 * 3600

# Same one-time guard for a failing scheduled end: the sweep retries every
# tick until the end succeeds, and the admin channel should hear about it once.
END_FAILED_KEY = "events:sweep:end-failed:{event_id}"

# Recurring schedules (web82a): the last observed window state per event —
# "open:{window row id}" or "closed" — so the sweep announces transitions
# exactly once. Missing key (first tick after activation, Redis restart) is
# seeded silently: the started announcement already covers the first window,
# and a flush must not re-announce mid-flight.
WINDOW_STATE_KEY = "events:window-open:{event_id}"
_WINDOW_STATE_TTL = 14 * 24 * 3600


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

def activation_blocker_items(session, event, now: Optional[datetime] = None) -> list:
    """Structured reasons ``event`` cannot activate right now (empty when it
    can). Each item is ``{"code", "message", "target"}`` — ``target`` names the
    manager section a leader fixes it in (``teams`` / ``board`` / ``tasks`` /
    ``dates``) so the UI can link them straight there. ``activation_blockers``
    wraps this to the legacy list-of-strings contract.

    Rules: ``ends_at`` (if set) must be in the future; a bingo event needs a
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
            blockers.append({
                "code": "no_teams", "target": "teams",
                "message": "The event needs at least one team.",
            })
    else:
        from db.models import EventGroup

        accepted = [
            gid for (gid,) in
            session.query(EventGroup.group_id)
            .filter(EventGroup.event_id == event.id, EventGroup.status == "accepted")
            .all()
        ]
        if len(accepted) < 2:
            have = len(accepted)
            blockers.append({
                "code": "cvc_needs_two_clans", "target": "teams",
                "message": (
                    f"A clan-vs-clan event needs at least two accepted clans — "
                    f"{have} {'clan has' if have == 1 else 'clans have'} accepted so "
                    f"far. Invite more clans, or wait for invited clans to accept."
                ),
            })
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
                blockers.append({
                    "code": "cvc_clans_without_teams", "target": "teams",
                    "message": (f"Every accepted clan needs at least one team, or remove all "
                                f"teams to run whole-clan vs whole-clan — "
                                f"{len(clans_without_teams)} clan(s) have none yet."),
                })

    if event.ends_at is not None and event.ends_at <= now:
        blockers.append({
            "code": "end_in_past", "target": "dates",
            "message": "The end date is in the past — move it into the future first.",
        })

    # Recurring schedules (web82a). The write paths validate all of this too;
    # these blockers catch drift (dates moved after the schedule was set, a
    # kind change, every window already elapsed by start time).
    if getattr(event, "schedule_config", None):
        if (getattr(event, "kind", None) or "standard") == "board_game":
            blockers.append({
                "code": "schedule_board_game", "target": "dates",
                "message": ("Board-game events can't use a recurring schedule — "
                            "remove the schedule or change the event type."),
            })
        if event.starts_at is None or event.ends_at is None:
            blockers.append({
                "code": "schedule_needs_dates", "target": "dates",
                "message": ("A recurring schedule needs both a start and an end "
                            "date set."),
            })
        else:
            from db.models import EventWindow

            upcoming = (
                session.query(EventWindow)
                .filter(EventWindow.event_id == event.id,
                        EventWindow.ends_at > now)
                .count()
            )
            if upcoming == 0:
                blockers.append({
                    "code": "schedule_no_windows", "target": "dates",
                    "message": ("The recurring schedule has no scoring windows "
                                "left before the end date — adjust the dates or "
                                "the schedule."),
                })

    if event.has_bingo:
        cells = (
            session.query(EventBingoCell)
            .filter(EventBingoCell.event_id == event.id)
            .all()
        )
        size = int(event.board_size or 0)
        if not cells:
            blockers.append({
                "code": "bingo_no_cells", "target": "board",
                "message": "The bingo board has no cells — lay out the board first.",
            })
        elif size * size != len(cells):
            blockers.append({
                "code": "bingo_incomplete", "target": "board",
                "message": (f"The bingo board is incomplete: a {size}×{size} board needs "
                            f"{size * size} cells, found {len(cells)}."),
            })
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
                # 1-based row/column labels, not raw 0-indexed Python lists —
                # "[0, 5]" means nothing to an organizer staring at a grid.
                if size:
                    where = ", ".join(
                        f"row {i // size + 1} col {i % size + 1}" for i in unbound)
                else:
                    where = ", ".join(str(i + 1) for i in unbound)
                blockers.append({
                    "code": "bingo_unbound_cells", "target": "board",
                    "message": (f"Bingo cell(s) at {where} are bound to tasks that do not "
                                "belong to this event — rebind or free them in the designer."),
                })

    if (getattr(event, "kind", None) or "standard") == "board_game":
        blockers.extend(_board_game_blocker_items(session, event))
    return blockers


def activation_blockers(session, event, now: Optional[datetime] = None) -> list:
    """Legacy list-of-strings contract — the human-readable messages from
    :func:`activation_blocker_items`."""
    return [b["message"] for b in activation_blocker_items(session, event, now=now)]


def readiness_report(session, event, now: Optional[datetime] = None) -> dict:
    """Pre-flight the activation checks WITHOUT activating (the "check
    readiness" button). Returns the structured blockers plus schedule context
    so a leader can confirm the event will auto-activate when its start time is
    reached, and jump straight to whatever still needs fixing."""
    now = now or datetime.now()
    status = getattr(event, "status", None) or "draft"
    items = activation_blocker_items(session, event, now=now)
    if status == "draft" and getattr(event, "group_id", None):
        # Tier frequency caps (web65a): a draft whose group has exhausted its
        # rolling window won't activate (manually or via the scheduled sweep),
        # so say so here rather than surprising the leader at start time. No
        # target tab — the fix is waiting or upgrading, not an editor section.
        from db.event_rate_limits import check_activation_rate_limit, describe_violation

        violation = check_activation_rate_limit(session, event, now=now)
        if violation is not None:
            items.append({
                "code": "tier_rate_limit", "target": "subscription",
                "message": describe_violation(violation),
            })
    starts_at = getattr(event, "starts_at", None)
    return {
        "status": status,
        "ready": not items,
        "blockers": items,
        "starts_at": int(starts_at.timestamp()) if starts_at else None,
        # True when a future scheduled start will auto-activate this draft.
        "auto_start": bool(starts_at is not None and status == "draft"),
        "already_active": status != "draft",
    }


def _board_game_blocker_items(session, event) -> list:
    """Board-game readiness (web44a): a laid-out track, and a rollable task
    pool covering every difficulty the tiles use (a difficulty-tile with an
    empty pool would have nothing to draw on landing). Structured items —
    see :func:`activation_blocker_items`."""
    from db.models import EventBoardTile, EventTask

    blockers = []
    tiles = (
        session.query(EventBoardTile)
        .filter(EventBoardTile.event_id == event.id)
        .order_by(EventBoardTile.idx)
        .all()
    )
    if len(tiles) < 2:
        blockers.append({
            "code": "board_min_tiles", "target": "board",
            "message": ("The board needs at least two tiles (a start and a finish) — "
                        "lay out the track in the board designer first."),
        })
        return blockers

    from services.boardgame_engine import _ROLLABLE_TYPES, _is_board_instance

    all_tasks = (
        session.query(EventTask).filter(EventTask.event_id == event.id).all()
    )
    # Pins may target ANY event task (a custom manual task is fine); only the
    # roll pool is restricted to auto-evaluable types.
    task_ids = {t.id for t in all_tasks}
    pool = [
        t for t in all_tasks
        if t.type in _ROLLABLE_TYPES and not _is_board_instance(t)
    ]
    pool_difficulties = {t.difficulty for t in pool if t.difficulty}

    tile_difficulties = {t.difficulty for t in tiles if t.difficulty}
    uncovered = sorted(d for d in tile_difficulties if d not in pool_difficulties)
    if uncovered and not pool:
        blockers.append({
            "code": "board_no_tasks", "target": "tasks",
            "message": ("The event has no rollable tasks — add tasks (with difficulties) "
                        "so tiles can draw them."),
        })
    elif uncovered:
        # A missing tier falls back to the any-tier pool, so this is only a
        # blocker when there is nothing at all; otherwise it would be a
        # warning. Keep activation strict: tiers the designer used should
        # exist in the pool.
        blockers.append({
            "code": "board_uncovered_tiers", "target": "tasks",
            "message": (f"No tasks carry the difficulty tier(s) {', '.join(uncovered)} "
                        "used by the board's tiles — add tasks of those tiers (or retier "
                        "the tiles)."),
        })

    unbound = sorted(
        t.idx for t in tiles
        if t.task_id is not None and t.task_id not in task_ids
    )
    if unbound:
        blockers.append({
            "code": "board_unbound_pins", "target": "board",
            "message": ("Board tile(s) "
                        + ", ".join(f"#{i}" for i in unbound)
                        + " pin tasks that do not belong to this event — "
                        "rebind or clear them in the designer."),
        })
    return blockers


def assert_activation_capacity(session, event, user=None) -> None:
    """Tier capacity checks (PRD D9 + web65a), enforced at activation only.

    Group events must pass, in order:
    1. Access — the tier's ``events`` entitlement, OR a rate-limited grant
       (an enabled ``web_event_rate_limits`` rule with max_events > 0 — how a
       free tier gets occasional events).
    2. Concurrency — ``status='active'`` count below ``events_max_active``.
    3. Frequency — the tier's per-kind / all-kinds rolling-window caps
       (db/event_rate_limits.py). Nothing configured = unlimited.

    Superadmin ``user`` bypasses all three (the entitlement resolver grants
    everything; the frequency check is skipped explicitly). The sweep passes
    no user, so real tier limits apply to scheduled auto-starts. Global
    events skip entirely. Raises :class:`LifecycleError` (403/409).
    """
    if not event.group_id:
        return  # global events (superadmin-run) are uncapped (PRD §7.1)

    from db.event_rate_limits import (
        check_activation_rate_limit,
        describe_violation,
        group_has_rate_limited_events,
    )
    # db.entitlements, not web_api.entitlements: this runs inside the event
    # consumer worker, where importing the web_api package (all 38 blueprints)
    # is both heavyweight and fragile — a deploy between worker start and the
    # first scheduled activation left a stale in-memory ``db`` package that
    # made the lazy web_api import raise, failing every auto-start (2026-08-17).
    from db.entitlements import (
        all_entitlements_granted,
        resolve_group_entitlements,
    )

    superadmin = bool(user and getattr(user, "is_superadmin", False))
    entitlements = (
        all_entitlements_granted() if superadmin
        else resolve_group_entitlements(session, event.group_id)
    )
    if not entitlements.get("events") and not group_has_rate_limited_events(
        session, event.group_id
    ):
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

    if not superadmin:
        violation = check_activation_rate_limit(session, event)
        if violation is not None:
            raise LifecycleError(
                409, "Event limit reached", describe_violation(violation)
            )


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _ts(dt) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _fmt_score(value) -> float:
    # int-when-integral so non-loot_sweep standings stay clean
    f = round(float(value or 0), 2)
    return int(f) if f == int(f) else f


def _rank_board_teams(teams, positions_by_team, tiebreak):
    """Pure finish-race ordering (best-first, testable without a session):
    finished teams first, then further-along, then the ``tiebreak`` metric
    tokens (each descending → best first), then team id for stability.
    ``positions_by_team`` maps team_id → a position with ``.status`` /
    ``.tile_idx``; ``tiebreak`` is an ordered subset of ('score', 'coins')."""
    metrics = {
        "score": lambda t: float(getattr(t, "score", 0) or 0),
        "coins": lambda t: float(getattr(t, "coins", 0) or 0),
    }

    def _key(t):
        p = positions_by_team.get(t.id)
        finished = p is not None and getattr(p, "status", None) == "finished"
        tile = int(getattr(p, "tile_idx", 0) or 0) if p is not None else 0
        key = [0 if finished else 1, -tile]
        for tok in tiebreak:
            fn = metrics.get(tok)
            if fn is not None:
                key.append(-fn(t))
        key.append(t.id)
        return tuple(key)

    return sorted(teams, key=_key)


def _board_final_standings(session, event, limit: int) -> list:
    """Board-game standings (win.rule finish_tile): rank by who reached the
    finish, then the configured ``win.tiebreak`` (default task score). Task
    ``score`` alone — the old ordering — could crown a team that never reached
    the finish and hand it the prize pot (W1). Ties among finished teams (or the
    ordering of still-running teams at a manual end) break by the tiebreak
    tokens applied left→right; 'score' = task points, 'coins' = wallet."""
    from db.models import EventBoardPosition, EventTeam
    from services.boardgame_engine import load_board_settings

    settings = load_board_settings(session, event.id)
    tiebreak = (settings.get("win") or {}).get("tiebreak")
    if not isinstance(tiebreak, list) or not tiebreak:
        tiebreak = ["score"]

    teams = (session.query(EventTeam)
             .filter(EventTeam.event_id == event.id).all())
    positions = {
        p.team_id: p for p in session.query(EventBoardPosition)
        .filter(EventBoardPosition.event_id == event.id).all()
    }
    ordered = _rank_board_teams(teams, positions, tiebreak)[:limit]
    return [{"team_id": t.id, "name": t.name, "score": _fmt_score(t.score)}
            for t in ordered]


def final_standings(session, event_id: int, limit: int = 5) -> list:
    """[{team_id, name, score}] best-first. Board-game events rank by the
    finish-line race (see :func:`_board_final_standings`); every other event
    ranks by task score."""
    from db.models import Event, EventTeam

    event = session.query(Event).filter(Event.id == event_id).first()
    if event is not None and getattr(event, "kind", None) == "board_game":
        return _board_final_standings(session, event, limit)

    rows = (
        session.query(EventTeam)
        .filter(EventTeam.event_id == event_id)
        .order_by(EventTeam.score.desc(), EventTeam.id.asc())
        .limit(limit)
        .all()
    )
    return [{"team_id": t.id, "name": t.name, "score": _fmt_score(t.score)}
            for t in rows]


def _pot_advertise_line(session, event, team_count, *, ended=False, winner=None):
    """The one-line prize-pot advertisement for a lifecycle announcement, or
    None when the pot is off / not advertised (so the token-drop rule drops the
    line). web52a."""
    try:
        from services.event_prizes import pot_line, pot_summary
        from services.event_notifications import format_gp

        pot = pot_summary(session, event, team_count=team_count)
        if not (pot["enabled"] and pot["advertise"]):
            return None
        return pot_line(
            format_gp(pot["total"]), pot["distribution"], pot["top_n"],
            ended=ended, winner=winner,
        )
    except Exception:
        return None


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
    refresh reconciles the set from the DB anyway), plus the per-event ended
    tombstone: stamped on deactivation so the consumer's still-stale matcher
    snapshot stops scoring/notifying the event the moment it ends (a
    premature manual end can't rely on the window check — its ends_at is
    still in the future), cleared on activation. Best-effort."""
    try:
        from services import event_engine
        from utils.redis import redis_client

        conn = getattr(redis_client, "client", None)
        if conn is None:
            return
        if active:
            conn.sadd(event_engine.ACTIVE_EVENTS_KEY, int(event_id))
            # Every writer of this gate must honour the same TTL (P1-6): SADD
            # on a missing key creates it PERSISTENT, so activating from the
            # web while the consumer is down resurrects the gate forever and
            # re-opens producers into a queue nothing is draining.
            conn.expire(
                event_engine.ACTIVE_EVENTS_KEY,
                event_engine.ACTIVE_EVENTS_TTL_SECONDS,
            )
            event_engine.clear_ended_tombstone(conn, event_id)
        else:
            conn.srem(event_engine.ACTIVE_EVENTS_KEY, int(event_id))
            event_engine.set_ended_tombstone(conn, event_id)
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


def sync_auto_clan_rosters(session, event, now: Optional[datetime] = None) -> int:
    """Reconcile whole-clan rosters (clan_vs_clan) so an ``auto_clan`` team's
    materialized ``EventTeamMember`` rows always mirror its clan's *current*
    membership. Two directions, both automatic — no sign-up needed:

    - **join**: a current clan member with no row gets one (``joined_at`` is the
      event start, matching auto-clan credit semantics where the event window is
      the only cutoff), so the teams page, join panel and Activity show the full
      clan from the moment the event starts;
    - **leave**: a materialized row whose player has since left the clan (or
      switched to the other participating clan) is deleted, so they stop being
      credited to a clan they no longer belong to.

    Removal matches the player-facing ``POST /events/{id}/leave`` route: only
    the roster row is deleted — the player's existing completions/progress stay
    (history stands). ``auto_clan`` teams represent "the whole clan", so their
    roster is a pure mirror of clan membership; a player explicitly placed on a
    *non*-auto team is never touched (removal is scoped to auto_clan teams) and
    never re-added while they hold that placement.

    The matcher also credits clan members roster-lessly between ticks
    (:func:`services.event_engine.load_matcher_state` expands auto_clan teams
    from ``user_group_association`` on every reload), but only the materialized
    rows here are what every read surface renders — and only deleting a departed
    member's row stops the matcher loading it as an explicit participant.

    Idempotent and safe to run repeatedly — the sweep calls it every tick for
    active clan_vs_clan events, so joining or leaving a participating clan
    mid-event propagates within one tick. Returns the number of roster changes
    (added + removed); caller owns the commit."""
    if (getattr(event, "mode", None) or "standard") != "clan_vs_clan":
        return 0
    from db.models import EventTeam, EventTeamMember
    from db.models.associations import user_group_association

    teams = session.query(EventTeam).filter(EventTeam.event_id == event.id).all()
    auto_teams = [t for t in teams if getattr(t, "auto_clan", False) and t.group_id]
    if not auto_teams:
        return 0

    # Current membership of each auto_clan team's clan. distinct() — the known
    # NULL-user_id insert race can leave duplicate association rows, and one
    # player must map to one row.
    clan_members: dict = {}
    for team in auto_teams:
        clan_members[team.id] = {
            pid for (pid,) in
            session.query(user_group_association.c.player_id)
            .filter(
                user_group_association.c.group_id == team.group_id,
                user_group_association.c.player_id.isnot(None),
            )
            .distinct()
            .all()
        }

    # G7: players in more than one auto-clan team's clan are ambiguous — never
    # auto-enrol them (an admin adds them to a specific team explicitly). The
    # leave pass below is left untouched, so a manual placement still stands.
    # Keys are team ids here, but each auto team is one clan, so counting across
    # them counts across clans (the matcher's per-gid helper, reused).
    from services import event_engine

    multi_clan = event_engine.multi_clan_players(clan_members, clan_members.keys())

    auto_team_ids = [t.id for t in auto_teams]
    removed = 0
    # Leave first: drop rows on an auto_clan team whose player is no longer in
    # that team's clan. A clan-switcher lands on the other clan's team in the
    # add pass below (they're on no team once this deletes their old row).
    for m in (session.query(EventTeamMember)
              .filter(EventTeamMember.team_id.in_(auto_team_ids)).all()):
        if m.player_id not in clan_members.get(m.team_id, ()):
            session.delete(m)
            removed += 1
    if removed:
        session.flush()

    # Who is on ANY team in the event now (post-removal): the one-team-per-
    # player guard for the add pass (respects explicit placement + dual clans).
    on_event = {
        pid for (pid,) in
        session.query(EventTeamMember.player_id)
        .filter(EventTeamMember.team_id.in_([t.id for t in teams]))
        .all()
    }
    joined_at = event.activated_at or event.starts_at or now or datetime.now()
    added = 0
    placements: dict = {}   # player_id -> team_id, for the buy-in carry-over
    for team in auto_teams:
        for pid in clan_members[team.id]:
            if pid in on_event or pid in multi_clan:
                continue
            session.add(EventTeamMember(team_id=team.id, player_id=pid,
                                        event_id=event.id, joined_at=joined_at))
            placements[pid] = team.id
            on_event.add(pid)
            added += 1
    if added:
        session.flush()
        # web71a: a buy-in recorded before the roster materialized (whole-clan
        # teams only exist from activation) follows its payer onto the team.
        from services.event_buyins import sync_buyin_teams

        sync_buyin_teams(session, event.id, placements)
    if added or removed:
        # Keep the auto-created team roles/threads (web53a) in step with the
        # reconciled roster on the bot's next tick.
        try:
            from services.event_team_discord import mark_team_members_dirty

            mark_team_members_dirty(session, event.id)
        except ImportError:  # unit-test stubs
            pass
    return added + removed


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

    # Per-team Discord roles/channels (web53a): make sure every team has its
    # desired rows at start (config PUTs already sync eagerly; this catches
    # teams added after the config was saved).
    try:
        from services.event_team_discord import sync_event_team_discord
    except ImportError:  # unit-test stubs
        sync_event_team_discord = None
    if sync_event_team_discord is not None:
        sync_event_team_discord(session, event)

    # clan_vs_clan with no teams set up: seed a whole-clan team per accepted
    # clan so it runs as "anyone in clan A vs anyone in clan B". Must precede
    # grant_free_cells so the freshly-created teams receive their free cells.
    # Then put every current clan member ON their clan's team — no sign-up
    # needed; that's the whole point of skipping team setup.
    _ensure_whole_clan_teams(session, event)
    sync_auto_clan_rosters(session, event, now=now)
    # G7: tell admins (once) which players were left off the whole-clan teams
    # because they belong to more than one participating clan — each needs a
    # manual add to a specific team.
    _notify_multi_clan_skipped(session, event)

    # Free cells complete for every team "from activation" (Task 20/21).
    event_engine.grant_free_cells(session, event)

    # Board game (web44a): every team starts on tile 0 with its first task
    # (and starting coins when configured). Idempotent, so a re-run after a
    # failed activation is safe.
    if (getattr(event, "kind", None) or "standard") == "board_game":
        from services.boardgame_engine import seed_positions

        seed_positions(session, event)

    from db.models import EventTeam

    team_count = (
        session.query(EventTeam).filter(EventTeam.event_id == event.id).count()
    )
    ev_dict = event_engine._event_to_dict(event)
    started_extra = {
        "description": event.description or None,
        "starts_at": _ts(event.starts_at),
        "ends_at": _ts(event.ends_at),
        "team_count": team_count,
    }
    # Recurring schedules (web82a): the start announcement carries the
    # schedule in one line ("📅 Weekly: Sat 00:00 → Mon 00:00 UTC") so
    # participants know scoring is windowed, not continuous.
    if getattr(event, "schedule_config", None):
        try:
            from services.event_schedule import describe

            started_extra["schedule_summary"] = describe(event.schedule_config)
        except ImportError:  # unit-test stubs
            pass
    # Prize pot (web52a): advertise the opening pot on the start announcement.
    _pot_line = _pot_advertise_line(session, event, team_count)
    if _pot_line:
        started_extra["pot_started_line"] = _pot_line
    event_engine._enqueue_notification(
        session, "event_started", ev_dict,
        _representative_player_id(session, event.id),
        started_extra,
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
    """active -> past. The status flip + audit row commit FIRST — a failure in
    any wrap-up side effect must never leave an announced-as-over event still
    active and scoring drops — and the Redis gate drops immediately after.
    Wrap-up (Discord mirror retirement, team-channel retention, final
    standings, the ``event_ended`` announcement, SSE) then runs best-effort:
    a failed step is logged and alerted to the admin channel instead of
    raised. Returns the standings ([] if that step itself failed). Raises
    :class:`LifecycleError` only for invalid-state transitions, before any
    mutation.
    """
    from services import event_engine

    now = now or datetime.now()
    if event.status == "draft":
        raise LifecycleError(409, "Not active",
                             "A draft event cannot be ended — it never started.")
    if event.status == "past":
        raise LifecycleError(409, "Already ended", "This event has already ended.")

    before = {"status": event.status, "ended_at": _ts(event.ended_at)}

    # The status checks above read an UNLOCKED row, so two enders (the web
    # route, the lifecycle sweep, the board-win path in the consumer) can both
    # see 'active' and both run the whole wrap-up — duplicate teardown and a
    # second `event_ended` announcement, which has no dedupe key. Flip the row
    # atomically instead and let exactly one caller past.
    from db.models import Event as _Event

    changed = (
        session.query(_Event)
        .filter(_Event.id == event.id, _Event.status == event.status)
        .update({"status": "past", "ended_at": now}, synchronize_session=False)
    )
    if not changed:
        session.commit()
        raise LifecycleError(409, "Already ended", "This event has already ended.")
    session.refresh(event)

    _audit(session, actor_user_id, event, "event.end", before, {
        "status": "past",
        "ended_at": _ts(event.ended_at),
    })
    # P0-8: make the flip durable before any wrap-up step can throw, then
    # close the scoring gate. Callers' own commit becomes a no-op.
    session.commit()
    _mark_active_in_redis(event.id, False)

    failed_steps: list = []

    # Retire the mirrored Discord scheduled event(s): the core bot's
    # reconciler deletes them and drops the rows
    # (services/event_scheduled_events.py).
    try:
        from db.models import EventGuild

        session.query(EventGuild).filter(EventGuild.event_id == event.id).update(
            {EventGuild.sync_status: "delete_pending"}, synchronize_session=False,
        )
        session.commit()
    except Exception:
        session.rollback()
        failed_steps.append("Discord scheduled-event retirement")
        log.error("end_event(%s): guild mirror retirement failed",
                  event.id, exc_info=True)

    # Team roles/channels (web53a): apply each scope's retention — 'keep'
    # releases them as-is, 'delete_48h' schedules teardown after the grace
    # window so wrap-up pings still work.
    try:
        try:
            from services.event_team_discord import retire_event_team_discord
        except ImportError:  # unit-test stubs
            retire_event_team_discord = None
        if retire_event_team_discord is not None:
            retire_event_team_discord(session, event, now=now)
            session.commit()
    except Exception:
        session.rollback()
        failed_steps.append("team channel retirement")
        log.error("end_event(%s): team-discord retirement failed",
                  event.id, exc_info=True)

    standings: list = []
    try:
        standings = final_standings(session, event.id, limit=5)
    except Exception:
        session.rollback()
        failed_steps.append("final standings")
        log.error("end_event(%s): final standings failed",
                  event.id, exc_info=True)

    try:
        ev_dict = event_engine._event_to_dict(event)
        ended_extra = {"standings": standings, "ended_at": _ts(event.ended_at)}
        # Prize pot (web52a): "🏆 {winner} takes the {pot} pot" (or a split line).
        winner_name = standings[0].get("name") if standings else None
        _pot_line = _pot_advertise_line(session, event, None, ended=True,
                                        winner=winner_name)
        if _pot_line:
            ended_extra["pot_result_line"] = _pot_line
        event_engine._enqueue_notification(
            session, "event_ended", ev_dict,
            _representative_player_id(session, event.id),
            ended_extra,
        )
        session.commit()
    except Exception:
        session.rollback()
        failed_steps.append("end announcement")
        log.error("end_event(%s): ended-announcement enqueue failed",
                  event.id, exc_info=True)

    if failed_steps:
        # The event IS over (the flip committed) — tell the admin channel the
        # wrap-up was incomplete so someone can finish those steps by hand.
        # No NX guard needed: this path runs at most once per event.
        try:
            _notify_end_failure(session, None, event,
                                "the event ended, but wrap-up was incomplete: "
                                + ", ".join(failed_steps))
            session.commit()
        except Exception:
            session.rollback()
            log.error("end_event(%s): end-failure notification enqueue failed",
                      event.id, exc_info=True)

    _publish(event.id, {
        "kind": "ended", "event_id": event.id, "name": event.name,
        "standings": standings,
    })
    return standings


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler sweep (worker tick)
# ══════════════════════════════════════════════════════════════════════════════

def _notify_multi_clan_skipped(session, event) -> None:
    """One-time admin-channel notice (G7): the players NOT auto-enrolled onto a
    whole-clan team because they belong to more than one of the event's
    participating clans. An admin resolves each with an explicit team add
    (POST .../members/bulk). No-op unless the event seeded ≥2 auto-clan teams
    and at least one member is multi-clan."""
    if (getattr(event, "mode", None) or "standard") != "clan_vs_clan":
        return
    from db.models import EventTeam, Player
    from db.models.associations import user_group_association
    from services import event_engine

    auto_gids = {
        t.group_id for t in
        session.query(EventTeam)
        .filter(EventTeam.event_id == event.id, EventTeam.auto_clan.is_(True))
        .all()
        if t.group_id
    }
    if len(auto_gids) < 2:
        return  # no whole-clan contest → nothing was auto-enrolled to skip
    counts: dict = {}
    for gid in auto_gids:
        for (pid,) in (
            session.query(user_group_association.c.player_id)
            .filter(user_group_association.c.group_id == gid,
                    user_group_association.c.player_id.isnot(None))
            .distinct()
            .all()
        ):
            counts[pid] = counts.get(pid, 0) + 1
    skipped = sorted(pid for pid, n in counts.items() if n > 1)
    if not skipped:
        return
    names = [
        name for (name,) in
        session.query(Player.player_name)
        .filter(Player.player_id.in_(skipped))
        .order_by(Player.player_name.asc())
        .all()
    ]
    display = ", ".join(f"`{n}`" for n in names) if names else f"{len(skipped)} player(s)"
    event_engine._enqueue_notification(
        session, "event_multi_clan_skipped", event_engine._event_to_dict(event),
        skipped[0],
        {"skipped_players": display, "skipped_count": len(skipped)},
    )


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


def _notify_end_failure(session, redis_conn, event, detail: str) -> None:
    """Enqueue an admin-channel ``event_end_failed`` notification. With a
    ``redis_conn`` it is guarded once-per-event (NX key) — the sweep retries a
    failing end every tick and must not spam; pass ``redis_conn=None`` from
    paths that already run at most once (post-flip wrap-up failures)."""
    if redis_conn is not None:
        try:
            fresh = redis_conn.set(
                END_FAILED_KEY.format(event_id=event.id),
                detail, nx=True, ex=_ACTIVATION_FAILED_TTL,
            )
            if not fresh:
                return  # already notified for this event
        except Exception:
            pass

    from services import event_engine

    event_engine._enqueue_notification(
        session, "event_end_failed", event_engine._event_to_dict(event),
        _representative_player_id(session, event.id),
        {"reason": detail, "ends_at": _ts(event.ends_at)},
    )


def run_window_sweep(session, redis_conn, events, now: Optional[datetime] = None) -> dict:
    """Recurring schedules (web82a): detect scoring-window open/close
    transitions on active scheduled events and announce them.

    The scoring gate itself needs none of this — the matcher checks each
    submission's timestamp against the materialized windows directly. This
    sweep only drives the HUMAN surfaces: the ``event_window_opened`` /
    ``event_window_closed`` announcements and the SSE frames the live pages
    listen to. The close of the LAST window is silent — the event ends within
    the same minute and ``event_ended`` is the wrap-up message.

    Returns ``{"opened": [ids], "closed": [ids]}``.
    """
    summary = {"opened": [], "closed": []}
    if redis_conn is None:
        return summary
    scheduled = [e for e in events or []
                 if getattr(e, "schedule_config", None)]
    if not scheduled:
        return summary
    now = now or datetime.now()

    from db.models import EventWindow
    from services import event_engine

    wins_by_event: dict = {}
    for w in (
        session.query(EventWindow)
        .filter(EventWindow.event_id.in_([e.id for e in scheduled]))
        .order_by(EventWindow.event_id, EventWindow.starts_at)
        .all()
    ):
        wins_by_event.setdefault(w.event_id, []).append(w)

    for event in scheduled:
        wins = wins_by_event.get(event.id) or []
        if not wins:
            continue
        open_row = next((w for w in wins if w.starts_at <= now < w.ends_at), None)
        state = f"open:{open_row.id}" if open_row is not None else "closed"
        key = WINDOW_STATE_KEY.format(event_id=event.id)
        try:
            prev = redis_conn.get(key)
            if isinstance(prev, bytes):
                prev = prev.decode()
            redis_conn.set(key, state, ex=_WINDOW_STATE_TTL)
        except Exception:
            continue  # no state, no dedupe — better silent than spam
        if prev is None or prev == state:
            continue

        next_row = next((w for w in wins if w.starts_at > now), None)
        try:
            from services.event_schedule import describe

            schedule_summary = describe(event.schedule_config)
        except Exception:
            schedule_summary = None
        ev_dict = event_engine._event_to_dict(event)

        if open_row is not None:
            try:
                event_engine._enqueue_notification(
                    session, "event_window_opened", ev_dict,
                    _representative_player_id(session, event.id),
                    {
                        "window_starts_at": _ts(open_row.starts_at),
                        "window_ends_at": _ts(open_row.ends_at),
                        "next_window_starts_at": _ts(next_row.starts_at) if next_row else None,
                        "schedule_summary": schedule_summary,
                    },
                )
                session.commit()
                summary["opened"].append(event.id)
            except Exception:
                session.rollback()
                log.error("Window sweep: opened-announcement enqueue failed "
                          "for event %s", event.id, exc_info=True)
            _publish(event.id, {
                "kind": "window_opened", "event_id": event.id,
                "name": event.name,
                "window_ends_at": _ts(open_row.ends_at),
            })
        else:
            if next_row is None:
                continue  # last window closed → event_ended says it all
            standings: list = []
            try:
                standings = final_standings(session, event.id, limit=5)
            except Exception:
                session.rollback()
            try:
                event_engine._enqueue_notification(
                    session, "event_window_closed", ev_dict,
                    _representative_player_id(session, event.id),
                    {
                        "standings": standings,
                        "next_window_starts_at": _ts(next_row.starts_at),
                        "schedule_summary": schedule_summary,
                    },
                )
                session.commit()
                summary["closed"].append(event.id)
            except Exception:
                session.rollback()
                log.error("Window sweep: closed-announcement enqueue failed "
                          "for event %s", event.id, exc_info=True)
            _publish(event.id, {
                "kind": "window_closed", "event_id": event.id,
                "name": event.name,
                "next_window_starts_at": _ts(next_row.starts_at),
            })
    return summary


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
        except Exception as exc:
            # One poison event (bad board config, a slow query, an entitlement
            # hiccup) must not abort the whole sweep — every other due event,
            # the roster reconcile and the shop refresh still deserve their
            # tick. Record it, alert the admin channel once, move on.
            session.rollback()
            detail = f"unexpected error ({exc.__class__.__name__}: {exc})"
            log.error("Sweep: activating event %s failed: %s",
                      event_id, detail, exc_info=True)
            try:
                _notify_activation_failure(session, redis_conn, event, detail)
                session.commit()
            except Exception:
                session.rollback()
            summary["failed"].append({"id": event_id, "detail": detail})

    for event_id in due["end"]:
        event = by_id[event_id]
        try:
            end_event(session, event, now=now)
            session.commit()
            summary["ended"].append(event_id)
        except LifecycleError as exc:
            session.rollback()
            summary["failed"].append({"id": event_id, "detail": exc.detail})
        except Exception as exc:
            # Same poison-event guard as activation — an event that cannot end
            # keeps scoring until a human intervenes, so alert (once) besides
            # logging: unlike a failed activation there is no draft to fix.
            session.rollback()
            detail = f"unexpected error ({exc.__class__.__name__}: {exc})"
            log.error("Sweep: ending event %s failed: %s",
                      event_id, detail, exc_info=True)
            try:
                _notify_end_failure(session, redis_conn, event, detail)
                session.commit()
            except Exception:
                session.rollback()
            summary["failed"].append({"id": event_id, "detail": detail})

    # Whole-clan roster reconcile: players who joined a participating clan after
    # a clan_vs_clan event started get their team row on the next tick, and
    # players who left get their row removed. No-op (one cheap query) for
    # events without auto_clan teams.
    for event in rows:
        if event.status != "active" or event.id in due["end"]:
            continue
        if (getattr(event, "mode", None) or "standard") != "clan_vs_clan":
            continue
        try:
            if sync_auto_clan_rosters(session, event, now=now):
                session.commit()
        except Exception:
            session.rollback()
            log.error("Sweep: auto-clan roster reconcile failed for event %s",
                      event.id, exc_info=True)

    # Recurring schedules (web82a): announce scoring-window opens/closes on
    # active scheduled events. Freshly-activated events seed their state
    # silently this same tick (their row objects mutated to status='active'
    # above); events that just ended are excluded.
    try:
        run_window_sweep(
            session, redis_conn,
            [e for e in rows
             if e.status == "active" and e.id not in due["end"]],
            now=now,
        )
    except Exception:
        session.rollback()
        log.error("Sweep: window transition sweep failed", exc_info=True)

    # Board-game shop stock refresh (web50a): restock due events on the tick so
    # shops refresh even when nobody is actively browsing. maybe_refresh_shop is
    # a cheap no-op (no commit) for non-board / non-refresh events and commits
    # its own restock when due.
    for event in rows:
        if event.status != "active" or event.id in due["end"]:
            continue
        if (getattr(event, "kind", None) or "standard") != "board_game":
            continue
        try:
            from services.boardgame_shop import maybe_refresh_shop

            maybe_refresh_shop(session, event.id)
        except Exception:
            session.rollback()
            log.error("Sweep: shop refresh failed for event %s",
                      event.id, exc_info=True)

    return summary
