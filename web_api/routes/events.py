"""Task 14 — events system (+ Task 16 team membership & formation modes).

Public reads:
  GET /api/v1/events?groupId=&status=active|past   -> EventSummary[]
  GET /api/v1/events/{id}                            -> EventDetail

Player-facing (session required; Task 16, PRD D4/D10):
  POST /api/v1/events/{id}/join   { player_id, team_id?, join_code? } -> { team_id }
  POST /api/v1/events/{id}/leave  { player_id }                        -> { ok }

Admin writes (session + group admin of events.group_id, or superadmin for
global events where group_id is NULL):
  POST   /api/v1/events                    { EventInput }     -> { id }  (status: draft)
  PATCH  /api/v1/events/{id}                { partial }        -> EventDetail
  POST   /api/v1/events/{id}/activate                          -> EventDetail  (Task 21)
  POST   /api/v1/events/{id}/end                               -> EventDetail  (Task 21)
  POST   /api/v1/events/{id}/tasks          { EventTaskInput } -> { id }
  DELETE /api/v1/events/{id}/tasks/{taskId}                    -> { ok }
  GET    /api/v1/events/meta/items?q=       -> [{ id, name, tracked }]  (task-form autocomplete)
  GET    /api/v1/events/meta/npcs?q=        -> [{ id, name }]
  GET    /api/v1/events/meta/resolve?kind=item|npc&names=a|b -> [{ id, name }]
  GET    /api/v1/events/meta/item-sources?items=a|b -> [{ item_name, item_id, total, npcs:[{npc_id,name,icon_url,rarity,tracked,...}] }]
  GET    /api/v1/events/{id}/players        -> event-wide player contribution leaderboard
  GET    /api/v1/events/{id}/players/{playerId} -> one player's items/tasks/activity
  GET    /api/v1/events/{id}/teams          -> standings rollup (items/GP/contributors per team)
  POST   /api/v1/events/{id}/teams          { EventTeamInput } -> { id }
  POST   /api/v1/events/{id}/teams/{teamId}/members   { player_id } -> { ok }
  POST   /api/v1/events/{id}/teams/{teamId}/members/bulk { names } -> { added, skipped }
  DELETE /api/v1/events/{id}/teams/{teamId}/members/{playerId}      -> { ok }

Scores are computed by the submission pipeline (backend-owned), never trusted
from the client; this API only reads computed scores and exposes admin CRUD.

Task 18's verification queue / manual award / revoke / per-task PATCH routes
live in ``web_api/routes/event_admin.py`` (same auth helpers, shared engine).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime

from quart import Blueprint, jsonify, request
from sqlalchemy import func
from sqlalchemy import or_ as sa_or
from sqlalchemy.exc import IntegrityError

from db import (
    AuditLog,
    Event,
    EventBingoCell,
    EventBingoCompletion,
    EventCompletion,
    EventGroup,
    EventLeaderVote,
    EventPlayerPoints,
    EventProgress,
    EventSignup,
    EventSignupMessage,
    EventTask,
    EventTeam,
    EventTeamMember,
    EVENT_DISCORD_POLICIES,
    EVENT_FORMATION_MODES,
    EVENT_KINDS,
    EVENT_SELF_SIGNUP_MODES,
    EVENT_MODES,
    EVENT_PING_KEYS,
    EVENT_SUBMISSION_POLICIES,
    EVENT_TASK_DIFFICULTIES,
    EVENT_TASK_TYPES,
    EVENT_TEAM_ROLES,
    EVENT_TASK_VISIBILITIES,
    EVENT_VISIBILITIES,
    EventTaskLibraryItem,
    Group,
    GroupAdmin,
    Player,
    user_group_association,
)
from web_api.common import (abort_problem, db_session, hidden_player_ids, money,
                            parse_page, player_month_totals, private_no_store,
                            score_num, with_cache_headers)
from web_api.event_loot import loot_gp_by_player
from web_api.event_players import (
    count_contributions,
    norm_item_name,
    rank_players,
    task_contributions,
    top_items,
)
from web_api.task_tiles import build_tile, spec_names, tile_spec
from web_api.deps import (
    assert_event_editor,
    assert_group_admin,
    assert_group_entitlement,
    assert_superadmin,
    current_user_id,
    is_event_manager,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
    optional_user_id,
    render_token_authorized,
    resolve_group_role,
)

events_bp = Blueprint("v1_events", __name__)

# Group-config key for the standing "Open DropTracker" card's channel.
# Mirrored (not imported) so the unit-test harness, which stubs the whole
# ``services`` package, can exercise this module.
ACTIVITY_LAUNCH_CHANNEL_KEY = "activity_launch_channel"  # == services.activity_launch_core.CHANNEL_KEY


def _ts(dt) -> int | None:
    return int(dt.timestamp()) if dt else None


def _dt(unix) -> datetime | None:
    if unix is None:
        return None
    try:
        return datetime.fromtimestamp(int(unix))
    except (ValueError, TypeError, OSError):
        return None


def _parse_ping_config(raw) -> str | None:
    """Validate a ``{ping_key: [role ids]}`` object (EVENT_PING_KEYS) and
    return it as the JSON string stored in ``web_events.ping_config`` (None
    when nothing is configured). Role ids travel as strings — JS numbers lose
    precision past 2^53 (same rule as the channel snowflakes)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        abort_problem(422, "Invalid pings", "'pings' must be an object of ping key -> role id list.")
    out: dict[str, list[str]] = {}
    for key, role_ids in raw.items():
        if key not in EVENT_PING_KEYS:
            abort_problem(
                422, "Invalid ping key",
                f"'{key}' is not one of {list(EVENT_PING_KEYS)}.",
            )
        if role_ids in (None, []):
            continue  # unset this key
        if not isinstance(role_ids, list) or len(role_ids) > 10:
            abort_problem(
                422, "Invalid ping roles",
                f"'pings.{key}' must be a list of at most 10 role ids.",
            )
        clean = []
        for rid in role_ids:
            if isinstance(rid, int) and not isinstance(rid, bool):
                rid = str(rid)
            if not isinstance(rid, str) or not rid.strip().isdigit() or len(rid.strip()) > 32:
                abort_problem(
                    422, "Invalid ping roles",
                    f"'pings.{key}' entries must be Discord role snowflake id strings.",
                )
            clean.append(rid.strip())
        if clean:
            out[key] = clean
    return json.dumps(out) if out else None


def _event_pings(ev: Event) -> dict:
    """Parsed ``ping_config`` ({ping_key: [role ids]}), {} when unset/corrupt."""
    raw = getattr(ev, "ping_config", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _effective_status(ev: Event) -> str:
    """Explicit status, with one derivation: an active event past its
    scheduled end reads as 'past' even before the scheduler sweep (Task 21)
    flips the row."""
    if ev.status in ("draft", "past"):
        return ev.status
    if ev.ends_at and ev.ends_at < datetime.now():
        return "past"
    return "active"


def _assert_event_not_past(ev: Event) -> None:
    """Structural edits (tasks, teams) are allowed while draft or active but
    blocked once past — an ended event is a frozen record (web68a; mirrors
    ``_assert_roster_open`` for rosters). Mid-event edits stay first-class:
    they propagate to the live matcher via ``_bump`` and, for scoring-affecting
    task changes, carry an explicit ``retro`` choice."""
    if _effective_status(ev) == "past":
        abort_problem(409, "Event has ended",
                      "This event is over — its tasks and teams can no longer be changed.",
                      extra={"code": "event_past"})


def _summary(ev: Event) -> dict:
    return {
        "id": ev.id,
        "group_id": ev.group_id,
        "name": ev.name,
        "description": ev.description or None,
        "status": _effective_status(ev),
        "visibility": getattr(ev, "visibility", None) or "public",
        "starts_at": _ts(ev.starts_at),
        "ends_at": _ts(ev.ends_at),
        "has_bingo": bool(ev.has_bingo),
        "mode": getattr(ev, "mode", None) or "standard",
        "kind": getattr(ev, "kind", None) or "standard",
        "formation_mode": ev.formation_mode or "admin_assign",
        "requires_confirmation": bool(ev.requires_confirmation),
        "submission_policy": ev.submission_policy or "all",
        "board_size": int(ev.board_size or 5),
        "bonus_line_points": int(ev.bonus_line_points or 0),
        "bonus_blackout_points": int(ev.bonus_blackout_points or 0),
        "leadership": _leadership(ev),
        "per_group_discord": bool(getattr(ev, "per_group_discord", False)),
        "allow_live_edits": bool(getattr(ev, "allow_live_edits", False)),
        # EHE display gate (web74a) — the value itself, so the manager form can
        # render the current setting. The per-player figures are omitted
        # entirely when it's "admins" and the viewer isn't one.
        "effort_visibility": getattr(ev, "effort_visibility", None) or "public",
        # Sign-up window (web70a): the toggle, plus the derived answers the
        # join panel and the sign-up post both read — sign-ups close when the
        # event starts unless allow_late_signups is on.
        "allow_late_signups": bool(getattr(ev, "allow_late_signups", False)),
        "signups_open": _signups_open(ev),
        "signups_close_at": _ts(_signup_close_at(ev)),
        "activated_at": _ts(ev.activated_at),
        "ended_at": _ts(ev.ended_at),
    }


def _signups_open(ev: Event) -> bool:
    from services.event_signup import signups_closed

    return signups_closed(ev) is None


def _signup_close_at(ev: Event):
    from services.event_signup import signup_close_at

    return signup_close_at(ev)


def _assert_signups_open(ev: Event) -> None:
    """Self sign-up gate (web70a) — the roster may still be open to admins."""
    from services.event_signup import signups_closed

    reason = signups_closed(ev)
    if reason:
        abort_problem(409, "Sign-ups closed", reason, extra={"code": "signups_closed"})


def _leadership(ev: Event) -> dict:
    """Effective team-leadership config for one event (web48a)."""
    from web_api.event_leadership import effective_leadership

    return effective_leadership(getattr(ev, "leadership_config", None))


def participating_group_ids(s, ev) -> set[int]:
    """Groups whose members/admins the event concerns — the single pivot every
    authorization/eligibility check routes through. Standard/global: the one
    owning group (empty set for global). clan_vs_clan: the *accepted*
    participants (host is seeded accepted at create time), so standard events
    never read ``web_event_groups``."""
    if getattr(ev, "mode", "standard") == "clan_vs_clan":
        rows = (
            s.query(EventGroup.group_id)
            .filter(EventGroup.event_id == ev.id, EventGroup.status == "accepted")
            .all()
        )
        return {gid for (gid,) in rows}
    return {ev.group_id} if ev.group_id else set()


def _is_event_admin(s, viewer_id, ev: Event) -> bool:
    """Whether ``viewer_id`` administers ``ev`` (group owner/admin, or
    superadmin — the only admins of global events). clan_vs_clan: owner/admin
    of any accepted participating clan."""
    if viewer_id is None:
        return False
    user = load_user(s, viewer_id)
    if is_superadmin(user):
        return True
    if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
        mgids = manageable_guild_ids(viewer_id)
        return any(
            resolve_group_role(s, viewer_id, gid, mgids, user=user) in ("owner", "admin")
            or is_event_manager(s, viewer_id, gid)
            for gid in participating_group_ids(s, ev)
        )
    if not ev.group_id:
        return False
    role = resolve_group_role(s, viewer_id, ev.group_id, manageable_guild_ids(viewer_id), user=user)
    # web64a: group event managers administer the group's events too.
    return role in ("owner", "admin") or is_event_manager(s, viewer_id, ev.group_id)


def _member_group_ids(s, viewer_id) -> set[int]:
    """Group ids the viewer belongs to: membership rows
    (user_group_association) plus explicit web admin grants (group_admins) —
    the same union the /me groups list reports."""
    if viewer_id is None:
        return set()
    gids = {
        gid
        for (gid,) in s.query(user_group_association.c.group_id)
        .filter(user_group_association.c.user_id == viewer_id)
        .all()
        if gid is not None
    }
    gids |= {
        gid
        for (gid,) in s.query(GroupAdmin.group_id)
        .filter(GroupAdmin.user_id == viewer_id)
        .all()
    }
    return gids


def _is_restricted(ev: Event) -> bool:
    """Whether ``ev``'s CONTENT is hidden from the general public — i.e. an
    admin marked it ``private``, which limits the audience to event admins +
    participating-group members (see :func:`_can_view_restricted`).

    Being a draft is deliberately NOT restricting (web74a). A public draft is
    readable by direct link so the "the event is coming, here's the board —
    sign up!" Discord link opens for everyone, signed in or not; it is only
    kept OUT of the public listing (see :func:`_is_unlisted`). Clans that want
    their board secret before the start use ``visibility='private'``, which
    hides drafts exactly as before."""
    return (getattr(ev, "visibility", None) or "public") == "private"


def _is_unlisted(ev: Event) -> bool:
    """Whether ``ev`` is kept off the PUBLIC events list. Two reasons, one
    answer: it's private (hidden outright), or it's a draft that hasn't
    started — listing every unstarted event would bury the live ones. Unlisted
    is weaker than restricted: a public draft still serves its own page to
    anyone holding the link. Event admins and participating-group members see
    both kinds in their listing regardless."""
    return _effective_status(ev) == "draft" or _is_restricted(ev)


def _can_view_restricted(s, viewer_id, ev: Event) -> bool:
    """Audience for a restricted (private) event, and for the listing of an
    unlisted one: event admins, plus MEMBERS of any participating group.
    Members get the event page (and its sign-up panel) as soon as the event
    exists, so a private event stays visible to the clan running it. Denials
    go through :func:`_deny_restricted`: signed-in outsiders get a reasoned
    403, anonymous viewers the anonymized 404."""
    if _is_event_admin(s, viewer_id, ev):
        return True
    if viewer_id is None:
        return False
    return bool(participating_group_ids(s, ev) & _member_group_ids(s, viewer_id))


def _deny_restricted(ev: Event, viewer_id) -> None:
    """Deny a restricted (private) event to a viewer who failed
    :func:`_can_view_restricted`.

    Signed-in viewers get a 403 carrying a machine-readable ``code``
    (``event_private``) so the site can say WHY instead of a blank 404
    (web57a). This deliberately reveals that the event exists to any signed-in
    account — but nothing else about it. Anonymous viewers RETURN instead of
    aborting: the caller falls through to its usual missing-resource 404, so
    logged-out probing still can't distinguish a restricted event from a
    nonexistent one.

    Privacy is the only denial reason left since web74a made public drafts
    publicly readable — a private draft is denied for being private, which is
    the durable reason (it stays denied once it goes live).
    """
    if viewer_id is None:
        return
    abort_problem(
        403,
        "Event restricted",
        "This event is private and only visible to participants.",
        extra={"code": "event_private"},
    )


def _attach_task_tiles(s, tasks: list[dict]) -> None:
    """Attach a ``tile`` block (badge + value + resolved icon refs) to each
    serialized task dict. Names across all tasks resolve in two bulk queries
    (items, npcs); unknown names keep ``id: None``. See web_api/task_tiles.py."""
    from db import ItemList, NpcList

    specs = [tile_spec(t) for t in tasks]
    item_names: set[str] = set()
    npc_names: set[str] = set()
    for spec in specs:
        items, npcs = spec_names(spec)
        item_names |= items
        npc_names |= npcs

    item_ids: dict[str, int] = {}
    if item_names:
        # Stack/noted variants share a name — one id per name (min, like the
        # designer's autocomplete). MySQL's ci collation makes IN() match the
        # normalized lowercase keys.
        for item_id, name in (
            s.query(func.min(ItemList.item_id), ItemList.item_name)
            .filter(ItemList.item_name.in_(item_names), ItemList.noted.is_(False))
            .group_by(ItemList.item_name)
            .all()
        ):
            item_ids[" ".join(name.strip().lower().split())] = item_id
    npc_ids: dict[str, int] = {}
    if npc_names:
        for npc_id, name in (
            s.query(func.min(NpcList.npc_id), NpcList.npc_name)
            .filter(NpcList.npc_name.in_(npc_names))
            .group_by(NpcList.npc_name)
            .all()
        ):
            npc_ids[" ".join(name.strip().lower().split())] = npc_id

    for task, spec in zip(tasks, specs):
        task["tile"] = build_tile(spec, item_ids, npc_ids)


def _detail(s, ev: Event, viewer_id: int | None = None) -> dict:
    base = _summary(ev)

    tasks = [
        {
            "id": t.id,
            "type": t.type,
            "label": t.label,
            "target": t.target or None,
            "target_value": t.target_value,
            "points": int(t.points or 0),
            "requires_confirmation": bool(t.requires_confirmation),
            "visibility": t.visibility or "public",
            "difficulty": getattr(t, "difficulty", None),
            # Raw JSON config (item lists, source NPCs) so participants can
            # see exactly which items/NPCs count toward a task.
            "config": t.config or None,
        }
        for t in s.query(EventTask).filter(EventTask.event_id == ev.id).all()
    ]
    _attach_task_tiles(s, tasks)

    teams_rows = s.query(EventTeam).filter(EventTeam.event_id == ev.id).all()
    team_names = {tm.id: tm.name for tm in teams_rows}
    team_ids = [tm.id for tm in teams_rows]
    show_effort = _effort_visible(s, viewer_id, ev)
    members_by_team: dict[int, list[dict]] = {}
    member_rows = []
    if team_ids:
        member_rows = (
            s.query(EventTeamMember, Player.player_name)
            .join(Player, Player.player_id == EventTeamMember.player_id)
            .filter(EventTeamMember.team_id.in_(team_ids))
            .order_by(EventTeamMember.joined_at.asc())
            .all()
        )
        # EHE per member — the compact scalar only, not the per-boss breakdown:
        # a clan-vs-clan roster is 400+ members per team and the detail read is
        # already the page's heaviest payload. The full breakdown lives on the
        # team and player endpoints.
        from web_api.event_effort import effort_by_player

        # Hidden (effort_visibility='admins', non-admin viewer) => the figures
        # are never computed, so they cannot leak through a stray key.
        effort = (effort_by_player(s, ev.id, [m.player_id for m, _ in member_rows])
                  if show_effort else {})
        for m, player_name in member_rows:
            members_by_team.setdefault(m.team_id, []).append({
                "player_id": m.player_id,
                # Player rows can carry a null name (e.g. bulk-added accounts
                # before WOM resolves an RSN); fall back to a stable display
                # string so the contract's `player_name: string` never breaks
                # the event/admin pages on a null.
                "player_name": player_name or f"Player {m.player_id}",
                "joined_at": _ts(m.joined_at),
                "role": getattr(m, "role", None),
                **({
                    "effort_ehb": (effort.get(m.player_id) or {}).get("ehb_hours", 0.0),
                    # Portion priced with derived (non-WOM) rates — >0 means the
                    # scalar should read as an estimate ("~12h").
                    "effort_ehb_estimated": (effort.get(m.player_id) or {})
                        .get("ehb_estimated_hours", 0.0),
                } if show_effort else {}),
            })
    teams = []
    for tm in teams_rows:
        members = members_by_team.get(tm.id, [])
        teams.append({
            **({
                "ehb_hours": round(
                    sum(m.get("effort_ehb") or 0.0 for m in members), 2),
                "ehb_estimated_hours": round(
                    sum(m.get("effort_ehb_estimated") or 0.0 for m in members), 2),
            } if show_effort else {}),
            "id": tm.id,
            "name": tm.name,
            "score": score_num(tm.score),
            "group_id": getattr(tm, "group_id", None),  # clan bound (clan_vs_clan)
            "color": getattr(tm, "color", None),  # admin accent; null = palette default
            # Board game (web44a): coin wallet + game piece.
            "coins": int(getattr(tm, "coins", 0) or 0),
            "piece_item_id": getattr(tm, "piece_item_id", None),
            "member_count": len(members),
            "members": members,
        })

    # Prize pot (web52a): the headline figure + per-team paid totals, folded
    # into the detail read so the standings banner updates live on the existing
    # event-detail SSE refresh (no second fetch). The full contributor list is
    # the on-demand GET /events/{id}/pot. Function-local import mirrors
    # ``_leadership`` (the unit-test conftest stubs services; this stays in
    # web_api). Short-circuits cheaply when the pot is disabled.
    from web_api.event_prizes import pot_summary

    pot = pot_summary(s, ev, team_count=len(teams_rows))
    for t in teams:
        t["pot_total"] = money(pot["per_team"].get(t["id"], 0))
    base["prize_pot"] = {
        "enabled": pot["enabled"],
        "total": money(pot["total"]),
        "advertise": pot["advertise"],
        "distribution": pot["distribution"],
        "top_n": pot["top_n"],
    }

    # Viewer block (Task 16): which of the signed-in user's players are on
    # this event, and on which team.
    viewer = None
    if viewer_id is not None:
        my_player_ids = {
            pid for (pid,) in s.query(Player.player_id).filter(Player.user_id == viewer_id).all()
        }
        on_event = [
            (m.player_id, m.team_id) for m, _ in member_rows if m.player_id in my_player_ids
        ]
        # Sign-up-pool opt-ins that aren't (yet) on a team. Only signup_pool
        # events have signup rows, so other modes issue no extra query.
        signed_up_pids = []
        if my_player_ids and (ev.formation_mode or "") == "signup_pool":
            signed_up_pids = [
                pid for (pid,) in
                s.query(EventSignup.player_id)
                .filter(EventSignup.event_id == ev.id,
                        EventSignup.player_id.in_(my_player_ids))
                .all()
            ]
        my_roles = [
            getattr(m, "role", None) for m, _ in member_rows
            if m.player_id in my_player_ids and getattr(m, "role", None)
        ]
        viewer = {
            "player_ids_on_event": [pid for pid, _ in on_event],
            "team_id": on_event[0][1] if on_event else None,
            "signed_up_player_ids": signed_up_pids,
            # Leadership role any of the viewer's players holds on their team
            # (web48a) — the client-side gate for board roll/shop buttons.
            "team_role": my_roles[0] if my_roles else None,
        }

    bingo = None
    if ev.has_bingo:
        cells = (
            s.query(EventBingoCell)
            .filter(EventBingoCell.event_id == ev.id)
            .order_by(EventBingoCell.idx.asc())
            .all()
        )
        cell_ids = [c.id for c in cells]
        completions_by_cell: dict[int, list[str]] = {}
        detail_by_cell: dict[int, list[dict]] = {}
        if cell_ids:
            comps = (
                s.query(EventBingoCompletion)
                .filter(EventBingoCompletion.cell_id.in_(cell_ids))
                .all()
            )
            player_names = {}
            pids = [c.player_id for c in comps if c.player_id]
            if pids:
                for pid, name in (
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(pids)).all()
                ):
                    player_names[pid] = name
            # (task, team) -> completed_at from the progress rollup, so the
            # board popover can say *when* a cell was completed.
            completed_at = {
                (p.task_id, p.team_id): _ts(p.completed_at)
                for p in s.query(EventProgress)
                .filter(EventProgress.event_id == ev.id, EventProgress.completed.is_(True))
                .all()
            }
            task_by_cell = {c.id: c.task_id for c in cells}
            for comp in comps:
                label = None
                if comp.team_id:
                    label = team_names.get(comp.team_id)
                elif comp.player_id:
                    label = player_names.get(comp.player_id)
                if label:
                    completions_by_cell.setdefault(comp.cell_id, []).append(label)
                detail_by_cell.setdefault(comp.cell_id, []).append({
                    "team_id": comp.team_id,
                    "team_name": team_names.get(comp.team_id),
                    "player_id": comp.player_id,
                    "player_name": player_names.get(comp.player_id),
                    "completed_at": completed_at.get(
                        (task_by_cell.get(comp.cell_id), comp.team_id)),
                })
        size = int(round(math.sqrt(len(cells)))) if cells else 0
        bingo = {
            "size": size,
            "cells": [
                {
                    "index": c.idx,
                    "label": c.label,
                    "task_id": c.task_id,
                    "completed_by": completions_by_cell.get(c.id, []),
                    "completions": detail_by_cell.get(c.id, []),
                }
                for c in cells
            ],
        }

    # Per-team per-task rollups: lets the public page render 35/50-style
    # progress bars instead of only final team scores. Each row carries its
    # team-aware ``target`` (whole_team pb tasks scale to the roster; the
    # client's pure threshold mirror can't know that).
    progress_rows = s.query(EventProgress).filter(EventProgress.event_id == ev.id).all()
    row_targets: dict = {}
    try:
        from services.event_engine import effective_threshold

        task_by_id = {t["id"]: t for t in tasks}
        for p in progress_rows:
            t = task_by_id.get(p.task_id)
            if t is not None:
                row_targets[(p.task_id, p.team_id)] = effective_threshold(
                    s, {"type": t.get("type"), "target_value": t.get("target_value"),
                        "config": t.get("config")}, p.team_id)
    except ImportError:  # unit-test stubs
        pass
    base["progress"] = [
        {
            "task_id": p.task_id,
            "team_id": p.team_id,
            "progress": int(p.progress or 0),
            "completed": bool(p.completed),
            "completed_at": _ts(p.completed_at),
            **({"target": row_targets[(p.task_id, p.team_id)]}
               if (p.task_id, p.team_id) in row_targets else {}),
        }
        for p in progress_rows
    ]

    # Pending-review overlay (web53a): which (task, team) pairs hold pending
    # ledger rows, and whether confirming them would finish the task — the
    # board tints those tiles amber ("done, awaiting review") and marks
    # partial pends. Zero extra work when nothing is pending.
    pending_pairs = (
        s.query(EventCompletion.task_id, EventCompletion.team_id)
        .filter(EventCompletion.event_id == ev.id,
                EventCompletion.status == "pending")
        .distinct()
        .all()
    )
    if pending_pairs:
        try:
            from services.event_engine import pending_projection
        except ImportError:  # unit-test stubs
            pending_pairs = []
        task_dicts = {}
        for t in tasks:
            config = t.get("config")
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (ValueError, TypeError):
                    config = None
            task_dicts[t["id"]] = {
                "id": t["id"], "type": t.get("type"),
                "target_value": t["target_value"], "config": config,
            }
        overlay: dict[tuple[int, int], dict] = {}
        for task_id, pteam_id in pending_pairs or ():
            td = task_dicts.get(task_id)
            if td is None or pteam_id is None:
                continue
            proj = pending_projection(s, td, pteam_id)
            if proj:
                overlay[(task_id, pteam_id)] = proj

        seen_progress = set()
        for entry in base["progress"]:
            seen_progress.add((entry["task_id"], entry["team_id"]))
            proj = overlay.get((entry["task_id"], entry["team_id"]))
            if proj and not entry["completed"]:
                entry["pending"] = proj["pending_count"]
                entry["pending_complete"] = proj["pending_complete"]
        for (task_id, pteam_id), proj in overlay.items():
            if (task_id, pteam_id) in seen_progress:
                continue
            # Pending rows with no rollup row yet (nothing confirmed): still
            # surface them so a fully-pending tile can tint.
            base["progress"].append({
                "task_id": task_id,
                "team_id": pteam_id,
                "progress": proj["applied"],
                "completed": False,
                "completed_at": None,
                "pending": proj["pending_count"],
                "pending_complete": proj["pending_complete"],
            })
        if bingo:
            for cell in bingo["cells"]:
                done_teams = {
                    c.get("team_id") for c in cell.get("completions") or []
                }
                pending_teams, partial = [], []
                for (task_id, pteam_id), proj in overlay.items():
                    if task_id != cell.get("task_id") or pteam_id in done_teams:
                        continue
                    if proj["pending_complete"]:
                        pending_teams.append(pteam_id)
                    else:
                        partial.append(pteam_id)
                if pending_teams:
                    cell["pending_teams"] = sorted(pending_teams)
                if partial:
                    cell["pending_partial_teams"] = sorted(partial)

    base["tasks"] = tasks
    base["teams"] = teams
    base["bingo"] = bingo
    base["viewer"] = viewer
    # Never the code itself on public reads — only whether one is required.
    base["join_requires_code"] = bool(ev.join_code)
    # Explicit admin signal for clients (the Activity's review affordances key
    # off it) — join_code/discord_guild_id presence is not a reliable proxy.
    base["can_manage"] = _is_event_admin(s, viewer_id, ev)
    if base["can_manage"]:
        base["join_code"] = ev.join_code
        base["discord_guild_id"] = ev.discord_guild_id
    return base


# --------------------------------------------------------------------------- #
# Public reads
# --------------------------------------------------------------------------- #
@events_bp.get("/events")
async def list_events():
    group_id = request.args.get("groupId")
    status = request.args.get("status")
    try:
        group_id = int(group_id) if group_id else None
    except (ValueError, TypeError):
        group_id = None
    # Discord-guild scoping for the embedded Activity: it only knows the guild
    # it was launched in, not our group ids. Digits-only guard since snowflakes
    # arrive as strings. groupId wins when both are supplied.
    guild_id = (request.args.get("guildId") or "").strip()
    guild_id = guild_id if guild_id.isdigit() else None
    # "My events" scoping for the Activity's guild-less launches (DM launches
    # via Activity Links): events of every group the session user belongs to.
    mine = (request.args.get("mine") or "").strip().lower() in ("1", "true")

    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            q = s.query(Event)
            if group_id is None and mine:
                if viewer_id is None:
                    return []  # anonymous "mine" is empty, never the global list
                # Memberships (user_group_association) + web admin grants —
                # the same union /me reports as the user's groups.
                my_gids = {
                    gid
                    for (gid,) in s.query(user_group_association.c.group_id)
                    .filter(user_group_association.c.user_id == viewer_id)
                    .all()
                    if gid is not None
                }
                my_gids |= {
                    gid
                    for (gid,) in s.query(GroupAdmin.group_id)
                    .filter(GroupAdmin.user_id == viewer_id)
                    .all()
                }
                # Include clan-vs-clan events my groups accepted as opponents,
                # matching the per-group listing below.
                participant_event_ids = s.query(EventGroup.event_id).filter(
                    EventGroup.group_id.in_(my_gids),
                    EventGroup.status == "accepted",
                )
                q = q.filter(
                    sa_or(
                        Event.group_id.in_(my_gids),
                        Event.id.in_(participant_event_ids),
                    )
                )
            elif group_id is None and guild_id is not None:
                # Events owned by any group linked to this guild, plus events
                # explicitly pointed at it (admins can re-target discord_guild_id).
                guild_group_ids = s.query(Group.group_id).filter(Group.guild_id == guild_id)
                q = q.filter(sa_or(Event.group_id.in_(guild_group_ids),
                                   Event.discord_guild_id == guild_id))
            if group_id is not None:
                # A group's list also includes clan-vs-clan events it has
                # ACCEPTED as an opponent (its own web_event_groups rows).
                # Standard events never have participant rows, so their
                # listing is byte-for-byte unchanged.
                participant_event_ids = (
                    s.query(EventGroup.event_id)
                    .filter(EventGroup.group_id == group_id,
                            EventGroup.status == "accepted")
                )
                q = q.filter(sa_or(Event.group_id == group_id,
                                   Event.id.in_(participant_event_ids)))
            events = q.order_by(Event.id.desc()).all()

            # Determine which groups the viewer can admin (to see drafts).
            # Superadmins see every draft, including global ones (Task 21).
            admin_groups = set()
            viewer_is_superadmin = False
            if viewer_id is not None:
                user = load_user(s, viewer_id)
                viewer_is_superadmin = is_superadmin(user)
                manage_ids = manageable_guild_ids(viewer_id)
                if not viewer_is_superadmin:
                    for ev in events:
                        if ev.group_id and ev.group_id not in admin_groups:
                            role = resolve_group_role(s, viewer_id, ev.group_id, manage_ids, user=user)
                            # web64a: event managers see their group's drafts too.
                            if (role in ("owner", "admin")
                                    or is_event_manager(s, viewer_id, ev.group_id)):
                                admin_groups.add(ev.group_id)

            # Members of a participating group see its drafts too — the
            # pre-publication landing page (one cheap set for the whole list).
            member_groups = _member_group_ids(s, viewer_id)

            out = []
            for ev in events:
                eff = _effective_status(ev)
                # Unlisted = draft (not started yet) OR private: both stay out
                # of an outsider's listing, and both stay in the listing of
                # admins + participating-group members. A public draft is still
                # readable at its own URL — this only keeps the list to events
                # that are actually live (web74a).
                if _is_unlisted(ev) and not viewer_is_superadmin and (
                        not ev.group_id or ev.group_id not in admin_groups):
                    if not (member_groups & participating_group_ids(s, ev)):
                        # clan-vs-clan unlisted events are also listed for
                        # admins of any accepted participant (mode check first:
                        # standard events take the fast `continue` with no extra
                        # queries; guild-derived admins aren't in member_groups).
                        if not ((getattr(ev, "mode", None) or "standard") == "clan_vs_clan"
                                and viewer_id is not None
                                and _is_event_admin(s, viewer_id, ev)):
                            continue  # hidden from outsiders
                if status and eff != status:
                    continue
                out.append(_summary(ev))
            return out

    events = await asyncio.to_thread(_load)
    if viewer_id is not None:
        # Signed-in lists can include drafts the viewer administers or
        # belongs to — viewer-specific, never shared-cacheable.
        return private_no_store(jsonify(events))
    return with_cache_headers(jsonify(events), max_age=30)


@events_bp.get("/events/launch-intent")
async def event_launch_intent():
    """Claim (and clear) the current user's pending Activity deep-link target —
    set by the bot when they clicked an "Open in Discord" launch button on an
    event message. One-shot: returns the target once, then it's gone.
    ``{"event_id": null, "view": null}`` when nothing is pending (app opens to
    its home hub). ``view`` names an in-app screen beyond the event page —
    ``"review"`` (a "Review in app" button) opens the event's pending-
    completions queue.

    Keyed by the user's Discord id, so a session only ever claims its own
    intent."""
    user_id = current_user_id()

    def _claim():
        from services.activity_launch_core import LAUNCH_VIEWS, intent_key
        from utils.redis import redis_client

        with db_session() as s:
            user = load_user(s, user_id)
            discord_id = getattr(user, "discord_id", None) if user else None
        if not discord_id:
            return None, None
        key = intent_key(discord_id)
        raw = redis_client.get(key)
        if raw is None:
            return None, None
        redis_client.delete(key)  # one-shot claim
        value = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        # Wire format "<event_id>" or "<event_id>:<view>" (activity_launch_core).
        event_id, _, view = value.partition(":")
        if not event_id.isdigit():
            return None, None
        return int(event_id), (view if view in LAUNCH_VIEWS else None)

    event_id, view = await asyncio.to_thread(_claim)
    return private_no_store(jsonify({"event_id": event_id, "view": view}))


@events_bp.get("/events/by-channel/<channel_id>")
async def event_by_channel(channel_id: str):
    """Resolve a Discord channel to the event whose board/notifications live
    there — the Activity's anonymous deep-link fallback: a launch button opens
    the app in its channel, and ``sdk.channelId`` tells us which. Prefers the
    active event; falls back to the most recent event pointed at the channel
    (so an ended event's "Final standings" button still lands right).
    ``{"event_id": null}`` when no event maps to the channel — or when the
    channel hosts a group's standing "Open DropTracker" card
    (``activity_launch_channel``): launches from there mean "open the app",
    not "open this channel's event", so the fallback must not fire."""
    channel_id = (channel_id or "").strip()
    if not channel_id.isdigit():
        return with_cache_headers(jsonify({"event_id": None}), max_age=30)

    def _load():
        from db.models import EventChannel, GroupConfiguration

        with db_session() as s:
            is_launcher_channel = (
                s.query(GroupConfiguration.group_id)
                .filter(
                    GroupConfiguration.config_key == ACTIVITY_LAUNCH_CHANNEL_KEY,
                    GroupConfiguration.config_value == channel_id,
                )
                .first()
            )
            if is_launcher_channel:
                return None
            base = (
                s.query(EventChannel.event_id)
                .join(Event, Event.id == EventChannel.event_id)
                .filter(EventChannel.channel_id == channel_id)
            )
            row = (
                base.filter(Event.status == "active").order_by(Event.id.desc()).first()
                or base.order_by(Event.id.desc()).first()
            )
            return int(row[0]) if row else None

    event_id = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify({"event_id": event_id}), max_age=30)


@events_bp.get("/events/<int:event_id>")
async def get_event(event_id: int):
    viewer_id = optional_user_id()
    # Internal board-image render bypass: the chrome-less /board-image page reads
    # any event (incl. private/draft) with the shared token so the Discord
    # screenshot never regresses vs. the old direct-DB path.
    render_bypass = render_token_authorized()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not render_bypass:
                # Private events: event admins + members of participating
                # groups only. Signed-in outsiders get a reasoned 403,
                # anonymous viewers the anonymized 404. Public drafts are NOT
                # restricted — the direct link opens for anyone (web74a).
                if not _can_view_restricted(s, viewer_id, ev):
                    _deny_restricted(ev, viewer_id)
                    return None
            return _detail(s, ev, viewer_id=viewer_id)

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if viewer_id is not None:
        # Viewer-specific payload (viewer block, possibly join_code) — never
        # shared-cacheable.
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=30)


# Ledger statuses that actually counted toward progress/score (pending and
# rejected/revoked rows are excluded from public team activity).
_APPLIED_STATUSES = ("auto", "confirmed", "manual")

_TEAM_ITEMS_LIMIT = 60        # full item gallery on the team-detail page
_MEMBER_ITEM_PREVIEW = 12     # item strip inside one roster row's breakdown
_MEMBER_TASK_PREVIEW = 12     # per-task rows inside one roster row's breakdown
_TEAMS_ITEM_PREVIEW = 8       # per-team item strip on the Teams tab rollup
_TEAMS_TOP_CONTRIBUTORS = 3   # contributor chips per team on the Teams tab


@events_bp.get("/events/<int:event_id>/teams/<int:team_id>")
async def get_event_team(event_id: int, team_id: int):
    """Public team detail: standings context, roster with per-member
    contribution counts, per-task progress, and the recent applied-ledger
    activity feed (no admin notes or proof URLs)."""
    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None
            all_teams = (
                s.query(EventTeam)
                .filter(EventTeam.event_id == event_id)
                .order_by(EventTeam.score.desc(), EventTeam.id.asc())
                .all()
            )
            team = next((tm for tm in all_teams if tm.id == team_id), None)
            if team is None:
                return None
            rank = all_teams.index(team) + 1

            member_rows = (
                s.query(EventTeamMember, Player.player_name)
                .join(Player, Player.player_id == EventTeamMember.player_id)
                .filter(EventTeamMember.team_id == team_id)
                .order_by(EventTeamMember.joined_at.asc())
                .all()
            )

            applied = (
                s.query(EventCompletion)
                .filter(
                    EventCompletion.event_id == event_id,
                    EventCompletion.team_id == team_id,
                    EventCompletion.status.in_(_APPLIED_STATUSES),
                )
                .order_by(EventCompletion.id.desc())
                .all()
            )
            # Task rows are needed before the roster rollup so a member's
            # contribution count can respect the task's type (metric tasks
            # collapse their update spam — see web_api/event_players).
            task_rows = s.query(EventTask).filter(EventTask.event_id == event_id).all()
            task_labels = {t.id: t.label for t in task_rows}
            task_types = {t.id: t.type for t in task_rows}

            # Per-member contribution rollup from the applied ledger: rows per
            # task (folded into contributions below), total quantity, the
            # newest row, and the items they personally pulled.
            contrib: dict[int, dict] = {}
            for c in applied:
                if c.player_id is None:
                    continue
                row = contrib.setdefault(
                    c.player_id,
                    {"rows_by_task": {}, "quantity": 0, "last": None, "items": {}},
                )
                t = row["rows_by_task"].setdefault(c.task_id, [0, 0])  # [rows, quantity]
                t[0] += 1
                t[1] += int(c.quantity or 1)
                row["quantity"] += int(c.quantity or 1)
                # `applied` is newest-first, so the first row wins.
                if row["last"] is None:
                    row["last"] = c
                if c.matched_target:
                    a = row["items"].setdefault(c.matched_target, [0, 0])
                    a[0] += int(c.quantity or 1)
                    a[1] += 1

            # Everything the team pulled to earn points, aggregated by item —
            # icons via the same resolution the task tiles use. One id lookup
            # covers both the team gallery and the per-member strips.
            team_item_agg: dict[str, list] = {}   # name -> [quantity, drops]
            for c in applied:
                if not c.matched_target:
                    continue
                a = team_item_agg.setdefault(c.matched_target, [0, 0])
                a[0] += int(c.quantity or 1)
                a[1] += 1
            team_item_ids = _resolve_item_ids(s, set(team_item_agg))
            team_items = sorted(
                ({"name": mt, "item_id": team_item_ids.get(norm_item_name(mt)),
                  "quantity": q, "drops": d}
                 for mt, (q, d) in team_item_agg.items()),
                key=lambda r: (-r["quantity"], -r["drops"], r["name"].lower()),
            )[:_TEAM_ITEMS_LIMIT]

            def _last_contribution(c) -> dict | None:
                """The member's newest applied ledger row, shaped for the
                roster's "what they did last" line."""
                from services.event_engine import display_note

                if c is None:
                    return None
                return {
                    "task_id": c.task_id,
                    "task_label": task_labels.get(c.task_id),
                    "task_type": task_types.get(c.task_id),
                    "quantity": int(c.quantity or 1),
                    "source_type": c.source_type,
                    "matched_target": c.matched_target,
                    "note": display_note(c.note),
                    "created_at": _ts(c.created_at),
                }

            # Contribution points: each completed task's points split across
            # its contributors by net share (web_event_player_points, floats).
            ppoints = {
                pid: float(total or 0)
                for pid, total in (
                    s.query(EventPlayerPoints.player_id,
                            func.sum(EventPlayerPoints.points))
                    .filter(EventPlayerPoints.event_id == event_id,
                            EventPlayerPoints.team_id == team_id)
                    .group_by(EventPlayerPoints.player_id)
                    .all()
                )
            }

            # Event-window loot GP for the roster (all sources) — the team
            # figure is the roster sum; both fail open to zeros.
            roster_pids = [m.player_id for m, _ in member_rows]
            loot_gp = loot_gp_by_player(s, ev, roster_pids)
            # Bingo EHB: effort at the event's relevant bosses, credited or
            # not. Fails open to {} — members simply render without it.
            from web_api.event_effort import effort_by_player

            show_effort = _effort_visible(s, viewer_id, ev)
            effort = (effort_by_player(s, event_id, roster_pids)
                      if show_effort else {})
            members = []
            for m, player_name in member_rows:
                agg = contrib.get(m.player_id) or {}
                rows_by_task = agg.get("rows_by_task") or {}
                members.append({
                    "player_id": m.player_id,
                    "player_name": player_name or f"Player {m.player_id}",
                    "joined_at": _ts(m.joined_at),
                    "role": getattr(m, "role", None),
                    "completions": count_contributions(
                        {tid: n for tid, (n, _q) in rows_by_task.items()}, task_types
                    ),
                    "quantity": int(agg.get("quantity", 0)),
                    "tasks_contributed": len(rows_by_task),
                    "points": round(ppoints.get(m.player_id, 0.0), 2),
                    "loot_gp": money(loot_gp.get(m.player_id, 0)),
                    "effort": effort.get(m.player_id),
                    "last_contribution": _last_contribution(agg.get("last")),
                    "items": top_items(
                        [{"name": mt, "quantity": q, "drops": d}
                         for mt, (q, d) in (agg.get("items") or {}).items()],
                        team_item_ids,
                        _MEMBER_ITEM_PREVIEW,
                    ),
                    # Per-task split of what they did, richest first — backs the
                    # roster's expandable contribution breakdown.
                    "tasks": sorted(
                        ({"task_id": tid,
                          "task_label": task_labels.get(tid),
                          "task_type": task_types.get(tid),
                          "contributions": task_contributions(task_types.get(tid), n),
                          "quantity": qty}
                         for tid, (n, qty) in rows_by_task.items()),
                        key=lambda r: (-r["contributions"], -r["quantity"],
                                       (r["task_label"] or "").lower()),
                    )[:_MEMBER_TASK_PREVIEW],
                })

            # Leadership context for the roster UI: the viewer's own player on
            # THIS team (if any), their role, and their live election vote.
            viewer_block = None
            if viewer_id is not None:
                my_pids = [
                    pid for (pid,) in
                    s.query(Player.player_id).filter(Player.user_id == viewer_id).all()
                ]
                mine = next((m for m, _ in member_rows if m.player_id in my_pids), None)
                if mine is not None:
                    my_vote = (
                        s.query(EventLeaderVote.candidate_player_id)
                        .filter(EventLeaderVote.event_id == event_id,
                                EventLeaderVote.voter_player_id == mine.player_id)
                        .first()
                    )
                    viewer_block = {
                        "player_id": mine.player_id,
                        "role": getattr(mine, "role", None),
                        "vote": my_vote[0] if my_vote else None,
                        "is_admin": _is_event_admin(s, viewer_id, ev),
                    }
                else:
                    viewer_block = {
                        "player_id": None, "role": None, "vote": None,
                        "is_admin": _is_event_admin(s, viewer_id, ev),
                    }

            prog_by_task = {
                p.task_id: p
                for p in s.query(EventProgress).filter(
                    EventProgress.event_id == event_id,
                    EventProgress.team_id == team_id,
                ).all()
            }
            tasks = []
            for t in task_rows:
                p = prog_by_task.get(t.id)
                tasks.append({
                    "id": t.id,
                    "type": t.type,
                    "label": t.label,
                    "target": t.target or None,
                    "target_value": t.target_value,
                    "points": int(t.points or 0),
                    "requires_confirmation": bool(t.requires_confirmation),
                    "config": t.config or None,
                    "progress": int(p.progress or 0) if p else 0,
                    "completed": bool(p.completed) if p else False,
                    "completed_at": _ts(p.completed_at) if p else None,
                })
            _attach_task_tiles(s, tasks)

            player_names = {m.player_id: name for (m, name) in member_rows}
            missing_pids = {
                c.player_id for c in applied[:50]
                if c.player_id and c.player_id not in player_names
            }
            if missing_pids:  # contributors who since left the roster
                for pid, name in (
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(missing_pids)).all()
                ):
                    player_names[pid] = name
            from services.event_engine import display_note as _dnote

            activity = [
                {
                    "id": c.id,
                    "task_id": c.task_id,
                    "task_label": task_labels.get(c.task_id),
                    "player_id": c.player_id,
                    "player_name": player_names.get(c.player_id),
                    "quantity": int(c.quantity or 1),
                    "source_type": c.source_type,
                    "matched_target": c.matched_target,
                    "note": _dnote(c.note),
                    "created_at": _ts(c.created_at),
                }
                for c in applied[:50]
            ]

            return {
                "event": _summary(ev),
                "team": {
                    "id": team.id,
                    "name": team.name,
                    "score": score_num(team.score),
                    "group_id": getattr(team, "group_id", None),
                    "color": getattr(team, "color", None),
                    "rank": rank,
                    "team_count": len(all_teams),
                    "member_count": len(members),
                    "coins": int(getattr(team, "coins", 0) or 0),
                    "loot_gp": money(sum(loot_gp.values())),
                    **({
                        "ehb_hours": round(
                            sum(float((e or {}).get("ehb_hours") or 0.0)
                                for e in effort.values()), 2),
                        "ehb_estimated_hours": round(
                            sum(float((e or {}).get("ehb_estimated_hours") or 0.0)
                                for e in effort.values()), 2),
                    } if show_effort else {}),
                },
                "members": members,
                "items": team_items,
                "tasks": tasks,
                "activity": activity,
                "viewer": viewer_block,
            }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Team not found", f"No team {team_id} in event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


# --- Teams rollup (Teams tab) ------------------------------------------------


@events_bp.get("/events/<int:event_id>/teams")
async def get_event_teams(event_id: int):
    """Public standings rollup for the Teams tab: every team with rank/score,
    tasks-done, prize-pot share, event-window loot GP (roster total), the top
    items its members pulled to earn points, and its top contributors. One
    self-sufficient payload so the tab needs no side fetches; kind-agnostic
    (board-game coins/pieces ride along when present)."""
    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None

            all_teams = (
                s.query(EventTeam)
                .filter(EventTeam.event_id == event_id)
                .order_by(EventTeam.score.desc(), EventTeam.id.asc())
                .all()
            )
            task_count = (
                s.query(func.count(EventTask.id))
                .filter(EventTask.event_id == event_id).scalar() or 0
            )

            # Roster: pids per team (for GP sums + member counts).
            roster_by_team: dict[int, list[int]] = {}
            for pid, tid in (
                s.query(EventTeamMember.player_id, EventTeamMember.team_id)
                .filter(EventTeamMember.event_id == event_id)
                .all()
            ):
                roster_by_team.setdefault(tid, []).append(pid)
            all_pids = {p for pids in roster_by_team.values() for p in pids}
            loot_gp = loot_gp_by_player(s, ev, all_pids)
            # Team EHE = the roster's summed effort, same shape as loot_gp.
            from web_api.event_effort import effort_by_player

            show_effort = _effort_visible(s, viewer_id, ev)
            effort = (effort_by_player(s, event_id, all_pids)
                      if show_effort else {})
            ehb_by_player = {
                pid: float(e.get("ehb_hours") or 0.0) for pid, e in effort.items()
            }
            ehb_est_by_player = {
                pid: float(e.get("ehb_estimated_hours") or 0.0)
                for pid, e in effort.items()
            }

            # Tasks completed per team.
            done_by_team = {
                tid: int(done or 0)
                for tid, done in (
                    s.query(EventProgress.team_id, func.count(EventProgress.id))
                    .filter(EventProgress.event_id == event_id,
                            EventProgress.completed.is_(True))
                    .group_by(EventProgress.team_id)
                    .all()
                )
            }

            # Items each team pulled to earn points (applied ledger, bucketed).
            items_by_team: dict[int, list] = {}
            distinct_names: set[str] = set()
            for tid, mt, qty, drops in (
                s.query(EventCompletion.team_id, EventCompletion.matched_target,
                        func.sum(EventCompletion.quantity),
                        func.count(EventCompletion.id))
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.status.in_(_APPLIED_STATUSES),
                        EventCompletion.team_id.isnot(None),
                        EventCompletion.matched_target.isnot(None))
                .group_by(EventCompletion.team_id, EventCompletion.matched_target)
                .all()
            ):
                distinct_names.add(mt)
                items_by_team.setdefault(tid, []).append(
                    {"name": mt, "quantity": int(qty or 0), "drops": int(drops or 0)})
            item_ids = _resolve_item_ids(s, distinct_names)

            # Top contributors per team (split points), names resolved once.
            points_rows = (
                s.query(EventPlayerPoints.team_id, EventPlayerPoints.player_id,
                        func.sum(EventPlayerPoints.points))
                .filter(EventPlayerPoints.event_id == event_id,
                        EventPlayerPoints.team_id.isnot(None))
                .group_by(EventPlayerPoints.team_id, EventPlayerPoints.player_id)
                .all()
            )
            points_by_team: dict[int, list] = {}
            for tid, pid, pts in points_rows:
                points_by_team.setdefault(tid, []).append((pid, float(pts or 0)))
            contributor_pids = {pid for _, pid, _ in points_rows}
            names = {
                pid: name
                for pid, name in (
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(contributor_pids)).all()
                )
            } if contributor_pids else {}
            hidden = (set() if _is_event_admin(s, viewer_id, ev)
                      else hidden_player_ids())

            pot = None
            try:
                from web_api.event_prizes import pot_summary

                pot = pot_summary(s, ev, team_count=len(all_teams))
            except Exception:
                pot = None

            teams = []
            for rank, tm in enumerate(all_teams, start=1):
                pids = roster_by_team.get(tm.id, [])
                top_contribs = sorted(points_by_team.get(tm.id, []),
                                      key=lambda r: -r[1])[:_TEAMS_TOP_CONTRIBUTORS]
                teams.append({
                    "id": tm.id,
                    "name": tm.name,
                    "score": score_num(tm.score),
                    "rank": rank,
                    "group_id": getattr(tm, "group_id", None),
                    "color": getattr(tm, "color", None),
                    "coins": int(getattr(tm, "coins", 0) or 0),
                    "piece_item_id": getattr(tm, "piece_item_id", None),
                    "member_count": len(pids),
                    "tasks_done": done_by_team.get(tm.id, 0),
                    "loot_gp": money(sum(loot_gp.get(p, 0) for p in pids)),
                    **({
                        "ehb_hours": round(
                            sum(ehb_by_player.get(p, 0.0) for p in pids), 2),
                        "ehb_estimated_hours": round(
                            sum(ehb_est_by_player.get(p, 0.0) for p in pids), 2),
                    } if show_effort else {}),
                    "pot_total": money((pot or {}).get("per_team", {}).get(tm.id, 0)),
                    "items": top_items(items_by_team.get(tm.id, []), item_ids,
                                       _TEAMS_ITEM_PREVIEW),
                    "top_contributors": [
                        {
                            "player_id": None if pid in hidden else pid,
                            "player_name": ("Hidden player" if pid in hidden
                                            else names.get(pid) or f"Player {pid}"),
                            "points": round(pts, 2),
                        }
                        for pid, pts in top_contribs
                    ],
                })

            return {
                "event": _summary(ev),
                "teams": teams,
                "totals": {
                    "teams": len(teams),
                    "players": len(all_pids),
                    "tasks": int(task_count),
                    "loot_gp": money(sum(loot_gp.values())),
                    **({
                        "ehb_hours": round(sum(ehb_by_player.values()), 2),
                        "ehb_estimated_hours": round(
                            sum(ehb_est_by_player.values()), 2),
                    } if show_effort else {}),
                },
            }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


# --- Event-wide player contribution (Players tab) ---------------------------

_PLAYERS_LIMIT = 500          # cap the leaderboard payload (sorted, top kept)
_PLAYERS_ITEM_PREVIEW = 8     # top contributed items shown per row in the list


def _resolve_item_ids(s, names) -> dict[str, int]:
    """``{normalized item name -> min item_id}`` for a set of item names — the
    same stack/noted-collapsing resolution _attach_task_tiles uses, so a
    contributed item's icon (``/img/itemdb/{id}.png``) matches the task tiles.
    Unknown names are simply absent (the web then renders no icon)."""
    names = {n for n in names if n}
    out: dict[str, int] = {}
    if not names:
        return out
    from db import ItemList
    for item_id, name in (
        s.query(func.min(ItemList.item_id), ItemList.item_name)
        .filter(ItemList.item_name.in_(names), ItemList.noted.is_(False))
        .group_by(ItemList.item_name)
        .all()
    ):
        out[norm_item_name(name)] = int(item_id)
    return out


@events_bp.get("/events/<int:event_id>/players")
async def get_event_players(event_id: int):
    """Public event-wide player contribution leaderboard: every player with at
    least one applied completion (or split points), aggregated ACROSS teams —
    split points, completions/quantity, distinct tasks contributed, their team,
    and the items they pulled (with icon ids). Mirrors the team endpoint's
    restricted-event gating + caching."""
    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None

            task_count = (
                s.query(func.count(EventTask.id))
                .filter(EventTask.event_id == event_id).scalar() or 0
            )
            participants = (
                s.query(func.count(EventTeamMember.player_id))
                .filter(EventTeamMember.event_id == event_id).scalar() or 0
            )

            # Applied-ledger rollup per player (completions / quantity / tasks).
            # Grouped per (player, task) with the task's type so a metric task's
            # stream of progress rows folds into ONE contribution.
            contrib: dict[int, dict] = {}
            for pid, tid, ttype, comps, qty in (
                s.query(EventCompletion.player_id,
                        EventCompletion.task_id,
                        EventTask.type,
                        func.count(EventCompletion.id),
                        func.sum(EventCompletion.quantity))
                .join(EventTask, EventTask.id == EventCompletion.task_id)
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.status.in_(_APPLIED_STATUSES),
                        EventCompletion.player_id.isnot(None))
                .group_by(EventCompletion.player_id, EventCompletion.task_id,
                          EventTask.type)
                .all()
            ):
                row = contrib.setdefault(
                    pid, {"completions": 0, "quantity": 0, "tasks": 0})
                row["completions"] += task_contributions(ttype, comps or 0)
                row["quantity"] += int(qty or 0)
                row["tasks"] += 1

            # Split points across teams (each task's points split by share).
            points = {
                pid: float(total or 0)
                for pid, total in (
                    s.query(EventPlayerPoints.player_id,
                            func.sum(EventPlayerPoints.points))
                    .filter(EventPlayerPoints.event_id == event_id)
                    .group_by(EventPlayerPoints.player_id)
                    .all()
                )
            }

            # Team membership (name/color/role) for the WHOLE roster — rostered
            # players with zero contributions still get a row (their
            # event-window loot GP is meaningful even before they score).
            membership: dict[int, dict] = {}
            for pid, tid, tname, tcolor, role in (
                s.query(EventTeamMember.player_id, EventTeamMember.team_id,
                        EventTeam.name, EventTeam.color, EventTeamMember.role)
                .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                .filter(EventTeamMember.event_id == event_id)
                .all()
            ):
                membership[pid] = {"team_id": tid, "team_name": tname,
                                   "team_color": tcolor, "role": role}

            pids = set(contrib) | set(points) | set(membership)
            # Total loot GP over the event window (all sources, not just
            # task-credited drops) — decorative, fails open to zeros.
            loot_gp = loot_gp_by_player(s, ev, pids)
            totals = {
                "contributors": len(set(contrib) | set(points)),
                "participants": int(participants),
                "completions": sum(c["completions"] for c in contrib.values()),
                "points": round(sum(points.values()), 2),
                "tasks": int(task_count),
                "loot_gp": money(sum(loot_gp.values())),
            }
            if not pids:
                return {"event": _summary(ev), "players": [], "totals": totals}

            names = {
                pid: name
                for pid, name in (
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(pids)).all()
                )
            }

            # Top contributed items per player (one grouped query, bucketed).
            by_pid: dict[int, list] = {}
            distinct_names: set[str] = set()
            for pid, mt, qty, drops in (
                s.query(EventCompletion.player_id, EventCompletion.matched_target,
                        func.sum(EventCompletion.quantity),
                        func.count(EventCompletion.id))
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.status.in_(_APPLIED_STATUSES),
                        EventCompletion.player_id.isnot(None),
                        EventCompletion.matched_target.isnot(None))
                .group_by(EventCompletion.player_id, EventCompletion.matched_target)
                .all()
            ):
                distinct_names.add(mt)
                by_pid.setdefault(pid, []).append(
                    {"name": mt, "quantity": int(qty or 0), "drops": int(drops or 0)})
            item_ids = _resolve_item_ids(s, distinct_names)
            items_by_pid = {
                pid: top_items(lst, item_ids, _PLAYERS_ITEM_PREVIEW)
                for pid, lst in by_pid.items()
            }

            # Bingo EHB per player — the effort that credited nothing.
            from web_api.event_effort import effort_by_player

            show_effort = _effort_visible(s, viewer_id, ev)
            effort = (effort_by_player(s, event_id, pids, boss_limit=5)
                      if show_effort else {})

            players = rank_players(
                contrib, points, membership, names, items_by_pid,
                loot_gp=loot_gp)[:_PLAYERS_LIMIT]
            for row in players:
                row["effort"] = effort.get(row["player_id"])
            # Honor the privacy opt-out: a player who hid themselves (or whose
            # linked user did) is shown as "Hidden player" with no id/link to a
            # non-admin viewer — same masking the completion-history read uses.
            if not _is_event_admin(s, viewer_id, ev):
                hidden = hidden_player_ids()
                for row in players:
                    if row["player_id"] in hidden:
                        row["player_name"] = "Hidden player"
                        row["player_id"] = None
            for row in players:  # raw int -> Money envelope at the boundary
                row["loot_gp"] = money(row["loot_gp"])
            if show_effort:
                totals["ehb_hours"] = round(
                    sum(float(e.get("ehb_hours") or 0) for e in effort.values()), 2)
                totals["ehb_estimated_hours"] = round(
                    sum(float(e.get("ehb_estimated_hours") or 0)
                        for e in effort.values()), 2)
            return {"event": _summary(ev), "players": players, "totals": totals}

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


#: How EHE may be displayed. "public" = the team/player surfaces show it;
#: "admins" = only the event managers' effort report does.
EFFORT_VISIBILITY_VALUES = ("public", "admins")


def _effort_visibility_value(raw) -> str:
    """Coerce a submitted visibility to a known value, defaulting to public.
    Deliberately forgiving rather than a 422: an unrecognised value here should
    fall back to today's behaviour, not fail an otherwise valid event save."""
    value = str(raw or "").strip().lower()
    return value if value in EFFORT_VISIBILITY_VALUES else "public"


def _effort_visible(s, viewer_id, ev) -> bool:
    """Whether EHE may be shown on this event's PUBLIC surfaces.

    ``effort_visibility='admins'`` keeps the per-player figure inside the
    managers' effort report: a public "who did the least" column is a social
    problem some clans would rather not have. Effort is still recorded either
    way, and event admins always see it — so flipping the toggle later reveals
    the full history rather than starting from nothing.
    """
    if (getattr(ev, "effort_visibility", None) or "public") != "admins":
        return True
    return _is_event_admin(s, viewer_id, ev)


def _player_effort(s, event_id: int, player_id: int, *, boss_limit: int = 20):
    """One player's EHE breakdown, or None when they have no tracked effort."""
    from web_api.event_effort import effort_by_player

    return effort_by_player(s, event_id, [player_id],
                            boss_limit=boss_limit).get(player_id)


@events_bp.get("/events/<int:event_id>/effort")
async def get_event_effort(event_id: int):
    """Bingo EHB participation report — event managers only.

    "Five days into a weeklong bingo and someone's last activity was three days
    ago" is the question this answers, so it deliberately lists the WHOLE
    roster, quietest first, including members with no recorded effort at all.
    Admin-gated rather than public: it names people who look inactive, which is
    a leader's call to act on, not a public scoreboard.
    """
    viewer_id = current_user_id()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if not _is_event_admin(s, viewer_id, ev):
                abort_problem(403, "Not permitted",
                              "Only event managers can view the effort report.")
            roster = [
                {"player_id": pid, "player_name": pname, "team_id": tid,
                 "team_name": tname, "joined_at": joined_at}
                for pid, pname, tid, tname, joined_at in (
                    s.query(EventTeamMember.player_id, Player.player_name,
                            EventTeamMember.team_id, EventTeam.name,
                            EventTeamMember.joined_at)
                    .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                    .outerjoin(Player, Player.player_id == EventTeamMember.player_id)
                    .filter(EventTeamMember.event_id == event_id)
                    .all()
                )
            ]
            from web_api.event_effort import effort_report

            return {"event": _summary(ev), **effort_report(s, event_id, roster)}

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    return private_no_store(jsonify(payload))


@events_bp.get("/events/<int:event_id>/players/<int:player_id>")
async def get_event_player(event_id: int, player_id: int):
    """One player's full contribution to an event: every item they pulled (with
    icon ids), their per-task contribution + split points, and recent activity.
    Backs the Players tab's row drill-down."""
    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None
            # Privacy opt-out: a hidden player's drill-down isn't exposed to a
            # non-admin (the list already masks them to an unlinkable "Hidden
            # player", so this is only reachable by guessing the id).
            if not _is_event_admin(s, viewer_id, ev) and player_id in hidden_player_ids():
                return None

            name = (
                s.query(Player.player_name)
                .filter(Player.player_id == player_id).scalar()
            )
            mem = (
                s.query(EventTeamMember.team_id, EventTeam.name,
                        EventTeam.color, EventTeamMember.role)
                .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                .filter(EventTeamMember.event_id == event_id,
                        EventTeamMember.player_id == player_id)
                .first()
            )
            applied = (
                s.query(EventCompletion)
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.player_id == player_id,
                        EventCompletion.status.in_(_APPLIED_STATUSES))
                .order_by(EventCompletion.id.desc())
                .all()
            )
            if not applied and mem is None:
                return None  # not a participant and no contribution -> 404

            item_agg: dict[str, list] = {}   # name -> [quantity, drops]
            task_agg: dict[int, list] = {}    # task_id -> [ledger rows, quantity]
            for c in applied:
                q = int(c.quantity or 1)
                if c.matched_target:
                    a = item_agg.setdefault(c.matched_target, [0, 0])
                    a[0] += q
                    a[1] += 1
                t = task_agg.setdefault(c.task_id, [0, 0])
                t[0] += 1
                t[1] += q

            item_ids = _resolve_item_ids(s, set(item_agg))
            items = sorted(
                ({"name": mt, "item_id": item_ids.get(norm_item_name(mt)),
                  "quantity": q, "drops": d}
                 for mt, (q, d) in item_agg.items()),
                key=lambda r: (-r["quantity"], -r["drops"], r["name"].lower()),
            )

            tpoints = {
                tid: float(p or 0)
                for tid, p in (
                    s.query(EventPlayerPoints.task_id,
                            func.sum(EventPlayerPoints.points))
                    .filter(EventPlayerPoints.event_id == event_id,
                            EventPlayerPoints.player_id == player_id)
                    .group_by(EventPlayerPoints.task_id).all()
                )
            }
            task_meta = {
                t.id: (t.label, t.type)
                for t in s.query(EventTask.id, EventTask.label, EventTask.type)
                .filter(EventTask.event_id == event_id).all()
            }
            # A metric task's stream of progress rows is ONE contribution; item
            # tasks stay one per ledger row (web_api/event_players).
            tasks = sorted(
                ({"task_id": tid,
                  "task_label": task_meta.get(tid, (None, None))[0],
                  "task_type": task_meta.get(tid, (None, None))[1],
                  "completions": task_contributions(
                      task_meta.get(tid, (None, None))[1], rows),
                  "quantity": qty,
                  "points": round(tpoints.get(tid, 0.0), 2)}
                 for tid, (rows, qty) in task_agg.items()),
                key=lambda r: (-r["points"], -r["completions"], -r["quantity"]),
            )

            from services.event_engine import display_note as _dnote

            activity = [
                {
                    "id": c.id,
                    "task_id": c.task_id,
                    "task_label": task_meta.get(c.task_id, (None, None))[0],
                    "quantity": int(c.quantity or 1),
                    "source_type": c.source_type,
                    "matched_target": c.matched_target,
                    "note": _dnote(c.note),
                    "created_at": _ts(c.created_at),
                }
                for c in applied[:30]
            ]

            return {
                "event": _summary(ev),
                "player": {
                    "player_id": player_id,
                    "player_name": name or f"Player {player_id}",
                    "team_id": mem[0] if mem else None,
                    "team_name": mem[1] if mem else None,
                    "team_color": mem[2] if mem else None,
                    "role": mem[3] if mem else None,
                    "points": round(sum(tpoints.values()), 2),
                    "completions": sum(t["completions"] for t in tasks),
                    "quantity": sum(int(c.quantity or 1) for c in applied),
                    "tasks_contributed": len(task_agg),
                    "loot_gp": money(
                        loot_gp_by_player(s, ev, [player_id]).get(player_id, 0)
                    ),
                    # Full EHE breakdown here (unlike the list rows): the
                    # drill-down is where "what did they actually grind" is the
                    # question being asked.
                    "effort": (_player_effort(s, event_id, player_id)
                               if _effort_visible(s, viewer_id, ev) else None),
                },
                "items": items,
                "tasks": tasks,
                "activity": activity,
            }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Player not found",
                      f"No contribution for player {player_id} in event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


@events_bp.get("/events/<int:event_id>/tasks/<int:task_id>/breakdown")
async def get_task_breakdown(event_id: int, task_id: int):
    """Per-(task, team) item-level progress: which required items/requirements a
    team has obtained vs still needs, plus who contributed. ``team_id`` query
    param selects the team (defaults to the viewer's own team, else the current
    leader). Reconstructed from the applied ledger — see web_api/event_breakdown.
    """
    # Lazy: event_breakdown pulls in services.event_engine, which the unit-test
    # conftest stubs — a module-level import breaks test collection.
    from web_api.event_breakdown import build_task_breakdown

    viewer_id = optional_user_id()
    team_arg = request.args.get("team_id") or request.args.get("teamId")

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None
            task = (
                s.query(EventTask)
                .filter(EventTask.id == task_id, EventTask.event_id == event_id)
                .first()
            )
            if task is None:
                return None
            teams = (
                s.query(EventTeam)
                .filter(EventTeam.event_id == event_id)
                .order_by(EventTeam.score.desc(), EventTeam.id.asc())
                .all()
            )
            if not teams:
                return {"__no_teams__": True}

            team = None
            if team_arg:
                try:
                    tid = int(team_arg)
                except (TypeError, ValueError):
                    tid = None
                team = next((t for t in teams if t.id == tid), None)
            if team is None and viewer_id is not None:
                # Default to the viewer's own team — the thing they care about.
                my_pids = [
                    pid for (pid,) in
                    s.query(Player.player_id).filter(Player.user_id == viewer_id).all()
                ]
                if my_pids:
                    vt = (
                        s.query(EventTeamMember.team_id)
                        .filter(EventTeamMember.player_id.in_(my_pids),
                                EventTeamMember.team_id.in_([t.id for t in teams]))
                        .first()
                    )
                    if vt:
                        team = next((t for t in teams if t.id == vt[0]), None)
            if team is None:
                team = teams[0]

            task_dict = {
                "id": task.id, "type": task.type, "label": task.label,
                "target": task.target or None, "target_value": task.target_value,
                "points": int(task.points or 0), "config": task.config or None,
            }
            _attach_task_tiles(s, [task_dict])

            rows = (
                s.query(EventCompletion)
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.task_id == task_id,
                        EventCompletion.team_id == team.id,
                        EventCompletion.status.in_(_APPLIED_STATUSES))
                .order_by(EventCompletion.id.desc())
                .all()
            )
            pending_rows = (
                s.query(EventCompletion)
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.task_id == task_id,
                        EventCompletion.team_id == team.id,
                        EventCompletion.status == "pending")
                .order_by(EventCompletion.id.desc())
                .all()
            )
            progress_row = (
                s.query(EventProgress)
                .filter(EventProgress.task_id == task_id, EventProgress.team_id == team.id)
                .first()
            )
            pids = {r.player_id for r in rows if r.player_id}
            player_names: dict[int, str] = {}
            if pids:
                for pid, name in (
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(pids)).all()
                ):
                    player_names[pid] = name

            # Team-aware threshold (whole_team pb tasks scale to the roster).
            from services.event_engine import effective_threshold

            target_override = effective_threshold(
                s, {"type": task.type, "target_value": task.target_value,
                    "config": task.config}, team.id)
            return build_task_breakdown(
                task_dict, task_dict.get("tile"), rows, progress_row,
                {"id": team.id, "name": team.name}, player_names, _ts,
                pending_rows=pending_rows, target_override=target_override,
            )

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Not found", f"No task {task_id} in event {event_id}.")
    if payload.get("__no_teams__"):
        abort_problem(409, "No teams", "This event has no teams yet.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


@events_bp.get("/events/<int:event_id>/loot-sweep")
async def get_loot_sweep_board(event_id: int):
    """Loot Sweep live board: every ``loot_sweep`` "set" task with, per team,
    the per-item receipt counts, decayed points, and set-bonus status. Rebuilt
    from the applied ledger via services/loot_sweep.py — the same scoring the
    engine folds — so the board can never disagree with ``EventTeam.score``.

    ``sets[].items`` (config order) carries the item defs (id/name/points/cap);
    each ``sets[].teams[].items`` is the SAME-INDEXED per-item ``{count,
    scored, points}`` so the grid maps by position. Any event kind is served
    (a standard event could carry a loot_sweep task), keyed off the task type.
    """
    # Lazy: services is stubbed by the unit-test conftest.
    from db import NpcList
    from services.loot_sweep import (LootSweepConfig, counts_from_rows, score_counts,
                                     icon_ids_for, _norm)

    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None
            tasks = (
                s.query(EventTask)
                .filter(EventTask.event_id == event_id, EventTask.type == "loot_sweep")
                .order_by(EventTask.id.asc())
                .all()
            )
            teams = (
                s.query(EventTeam)
                .filter(EventTeam.event_id == event_id)
                .order_by(EventTeam.score.desc(), EventTeam.id.asc())
                .all()
            )
            team_meta = [
                {"id": t.id, "name": t.name, "color": t.color, "score": score_num(t.score)}
                for t in teams
            ]
            base = {"event_id": event_id, "kind": ev.kind, "teams": team_meta, "sets": []}
            # Render the set/item structure even with no teams yet (a fresh
            # draft) — the board shows the boss sets greyed-out. Only bail when
            # there are no loot_sweep tasks at all.
            if not tasks:
                return base

            task_ids = [t.id for t in tasks]
            rows_by: dict = {}
            for r in (
                s.query(EventCompletion)
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.task_id.in_(task_ids),
                        EventCompletion.status.in_(_APPLIED_STATUSES))
                .all()
            ):
                rows_by.setdefault((r.task_id, r.team_id), []).append(r)

            cfgs = [(task, LootSweepConfig(task.config)) for task in tasks]
            # Resolve group NPCs -> ids so the board can show the boss's own
            # artwork (npcdb/{id}.png) when no custom image_url is set.
            all_npcs = {n for _t, cfg in cfgs for g in cfg.groups for n in g.npcs}
            npc_id_by_name: dict = {}
            if all_npcs:
                for nid, nname in (s.query(NpcList.npc_id, NpcList.npc_name)
                                   .filter(NpcList.npc_name.in_(list(all_npcs))).all()):
                    npc_id_by_name.setdefault(nname, nid)

            # Resolve match_name -> item id so a pooled/virtual entry can show a
            # cluster of the real pieces it stands for (the label itself has no
            # icon). Item names are case-insensitive in the DB collation.
            from db import ItemList
            all_aliases = {a for _t, cfg in cfgs for g in cfg.groups
                           for it in g.items for a in it.match_names}
            item_id_by_name: dict = {}
            if all_aliases:
                for iid, iname in (s.query(ItemList.item_id, ItemList.item_name)
                                   .filter(ItemList.item_name.in_(list(all_aliases))).all()):
                    item_id_by_name.setdefault(_norm(iname), iid)

            for task, cfg in cfgs:
                # Config-derived group/item defs (order matches the per-team
                # breakdown below, so the grid maps by position).
                groups_def = [
                    {
                        "label": g.label, "npcs": g.npcs,
                        "npc_id": next((npc_id_by_name.get(n) for n in g.npcs
                                        if npc_id_by_name.get(n) is not None), None),
                        "image_url": g.image_url,
                        "bonus_points": g.bonus_points, "bonus_max": g.bonus_max,
                        "items": [
                            {"item_id": it.item_id, "item_name": it.name,
                             "points": score_num(it.points), "awards_per_tier": it.awards_per_tier,
                             "max_awards": it.max_awards,
                             "counts_for_group": it.counts_for_group, "source": it.source,
                             "required": it.required, "match_names": it.match_names,
                             "virtual": it.virtual,
                             "icon_ids": icon_ids_for(it, item_id_by_name)}
                            for it in g.items
                        ],
                    }
                    for g in cfg.groups
                ]
                teams_out = []
                for t in teams:
                    b = score_counts(
                        counts_from_rows(rows_by.get((task.id, t.id), [])), cfg)
                    teams_out.append({
                        "team_id": t.id,
                        "total": score_num(b["total"]),
                        "set_completions": b["set_completions"],
                        "set_awarded": b["set_awarded"],
                        "set_total": score_num(b["set_total"]),
                        "groups": [
                            {
                                "completions": gb["completions"], "awarded": gb["awarded"],
                                "bonus_total": score_num(gb["bonus_total"]),
                                "item_total": score_num(gb["item_total"]),
                                "items": [
                                    {"count": bi["count"], "scored": bi["scored"],
                                     "points": score_num(bi["points"])}
                                    for bi in gb["items"]
                                ],
                            }
                            for gb in b["groups"]
                        ],
                    })
                base["sets"].append({
                    "task_id": task.id,
                    "label": task.label,
                    "decay_percent": cfg.decay_percent,
                    "decay_mode": cfg.decay_mode,
                    "set_bonus_points": cfg.set_bonus_points,
                    "set_bonus_max": cfg.set_bonus_max,
                    "groups": groups_def,
                    "teams": teams_out,
                })
            return base

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


@events_bp.get("/events/<int:event_id>/loot-sweep/summary")
async def get_loot_sweep_summary(event_id: int):
    """Compact Loot Sweep standings — the Discord-image view (a leaderboard, not
    the full per-item matrix, which is far too big to screenshot for a 300-item
    game-wide sweep). Per team: rank, score, sets completed / total, and the top
    contributing sets (label + boss npc_id + points). Rebuilt from the applied
    ledger via services/loot_sweep.py, so it can never disagree with the full
    board or ``EventTeam.score``.

    Powers ``/board-image/{id}`` for loot_sweep events (services/event_board_image.py)
    and any lightweight standings widget."""
    from db import NpcList
    from services.loot_sweep import LootSweepConfig, counts_from_rows, score_counts

    viewer_id = optional_user_id()
    render_bypass = render_token_authorized()  # the board-image page's token
    top_n = 3  # contributing sets shown per team

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if (_is_restricted(ev) and not render_bypass
                    and not _can_view_restricted(s, viewer_id, ev)):
                _deny_restricted(ev, viewer_id)
                return None
            tasks = (s.query(EventTask)
                     .filter(EventTask.event_id == event_id, EventTask.type == "loot_sweep")
                     .order_by(EventTask.id.asc()).all())
            teams = (s.query(EventTeam)
                     .filter(EventTeam.event_id == event_id)
                     .order_by(EventTeam.score.desc(), EventTeam.id.asc()).all())
            base = {"event_id": event_id, "event_name": ev.name, "status": ev.status,
                    "sets_total": len(tasks), "teams": []}
            if not tasks:
                base["teams"] = [
                    {"team_id": t.id, "rank": i + 1, "name": t.name, "color": t.color,
                     "score": score_num(t.score), "sets_completed": 0, "top_sets": []}
                    for i, t in enumerate(teams)]
                return base

            task_ids = [t.id for t in tasks]
            rows_by: dict = {}
            for r in (s.query(EventCompletion)
                      .filter(EventCompletion.event_id == event_id,
                              EventCompletion.task_id.in_(task_ids),
                              EventCompletion.status.in_(_APPLIED_STATUSES)).all()):
                rows_by.setdefault((r.task_id, r.team_id), []).append(r)

            cfgs = [(task, LootSweepConfig(task.config)) for task in tasks]
            # First-group NPC per task → id, for the board's boss art.
            first_npc = {task.id: (cfg.groups[0].npcs[0] if cfg.groups and cfg.groups[0].npcs
                                   else None) for task, cfg in cfgs}
            npc_id_by_name: dict = {}
            wanted = {n for n in first_npc.values() if n}
            if wanted:
                for nid, nname in (s.query(NpcList.npc_id, NpcList.npc_name)
                                   .filter(NpcList.npc_name.in_(list(wanted))).all()):
                    npc_id_by_name.setdefault(nname, nid)

            for rank, t in enumerate(teams, start=1):
                completed = 0
                contribs = []  # (points, label, npc_id)
                for task, cfg in cfgs:
                    b = score_counts(counts_from_rows(rows_by.get((task.id, t.id), [])), cfg)
                    if b["set_completions"] >= 1:
                        completed += 1
                    if b["total"] > 0:
                        contribs.append((b["total"], task.label,
                                         npc_id_by_name.get(first_npc.get(task.id))))
                contribs.sort(key=lambda c: c[0], reverse=True)
                base["teams"].append({
                    "team_id": t.id, "rank": rank, "name": t.name, "color": t.color,
                    "score": score_num(t.score), "sets_completed": completed,
                    "top_sets": [{"label": lbl, "npc_id": nid, "points": score_num(pts)}
                                 for pts, lbl, nid in contribs[:top_n]],
                })
            return base

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


@events_bp.get("/events/<int:event_id>/loot-sweep/receipts")
async def get_loot_sweep_receipts(event_id: int):
    """Per-team receipt ledger for ONE loot_sweep item — powers the board's
    hover card: who pulled each receipt, when, the points it credited, and the
    screenshot proof when the ledger row carries one.

    Query: ``task_id`` (the loot_sweep set task) + ``item`` (item name,
    normalized server-side to the config key). Receipts come back in credit
    order per team; ``n`` is the 1-based ordinal of the row's FIRST receipt
    (a quantity-3 stack consumes ordinals n..n+2), and ``points`` is exactly
    what that row added under the decay schedule (0 once past the cap) — the
    same ``item_points`` fold the engine scores with, so the card can never
    disagree with the board."""
    from db import Player
    from services.loot_sweep import LootSweepConfig, _norm, icon_ids_for, item_points

    viewer_id = optional_user_id()
    try:
        task_id = int(request.args.get("task_id", ""))
    except ValueError:
        abort_problem(400, "Bad request", "task_id must be an integer.")
    item_q = (request.args.get("item") or "").strip()
    if not item_q:
        abort_problem(400, "Bad request", "item is required.")

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None
            task = (
                s.query(EventTask)
                .filter(EventTask.id == task_id,
                        EventTask.event_id == event_id,
                        EventTask.type == "loot_sweep")
                .first()
            )
            if not task:
                return None
            cfg = LootSweepConfig(task.config)
            key = _norm(item_q)
            # An entry answers for its own name or any of its match_names
            # aliases (the vestige + gold-ring case).
            item = next((i for g in cfg.groups for i in g.items if key in i.match_keys), None)
            if item is None:
                return None
            # Batch 2: this endpoint used to hydrate the task's ENTIRE ledger
            # to fold one item's receipts. The fold only ever advances on
            # rows whose normalized matched_target is this item's, so first
            # resolve which RAW target strings normalize into it (a cheap
            # DISTINCT over one indexed column), then fetch only those rows.
            distinct_targets = [
                t for (t,) in
                s.query(EventCompletion.matched_target)
                .filter(EventCompletion.event_id == event_id,
                        EventCompletion.task_id == task_id,
                        EventCompletion.status.in_(_APPLIED_STATUSES))
                .distinct().all()
            ]
            wanted_targets = [
                t for t in distinct_targets
                if t is not None and _norm(t) in item.match_keys
            ]
            if not wanted_targets:
                rows = []
            else:
                rows = (
                    s.query(EventCompletion, Player.player_name)
                    .outerjoin(Player, Player.player_id == EventCompletion.player_id)
                    .filter(EventCompletion.event_id == event_id,
                            EventCompletion.task_id == task_id,
                            EventCompletion.status.in_(_APPLIED_STATUSES),
                            EventCompletion.matched_target.in_(wanted_targets),
                            EventCompletion.team_id.isnot(None),
                            # NULL-safe: NULL source_type is NOT a bonus row.
                            sa_or(EventCompletion.source_type.is_(None),
                                  EventCompletion.source_type != "bonus"))
                    .order_by(EventCompletion.created_at.asc(),
                              EventCompletion.id.asc())
                    .all()
                )
            teams: dict[int, dict] = {}
            cum: dict[int, int] = {}
            for r, player_name in rows:
                if (r.source_type or "") == "bonus":
                    continue
                if r.team_id is None or _norm(r.matched_target) not in item.match_keys:
                    continue
                qty = max(int(r.quantity or 1), 1)
                before = cum.get(r.team_id, 0)
                after = before + qty
                cum[r.team_id] = after
                pts = (
                    item_points(item.points, after, item.max_awards,
                                cfg.decay_percent, item.awards_per_tier, cfg.decay_mode)
                    - item_points(item.points, before, item.max_awards,
                                  cfg.decay_percent, item.awards_per_tier, cfg.decay_mode)
                )
                entry = teams.setdefault(r.team_id, {"team_id": r.team_id, "receipts": []})
                entry["receipts"].append({
                    "n": before + 1,
                    "quantity": qty,
                    "player_id": r.player_id,
                    "player_name": player_name,
                    "received_at": int(r.created_at.timestamp()) if r.created_at else None,
                    # 2-decimal points (score_num keeps clean receipts as ints)
                    "points": score_num(pts),
                    # The name that actually dropped (may be an alias).
                    "matched_name": r.matched_target,
                    "proof_url": r.proof_url,
                    "source_type": r.source_type,
                })
            # Per-piece icons for a pooled/virtual entry (label first has none).
            from db import ItemList
            alias_ids: dict = {}
            if item.match_names:
                for iid, iname in (s.query(ItemList.item_id, ItemList.item_name)
                                   .filter(ItemList.item_name.in_(item.match_names)).all()):
                    alias_ids.setdefault(_norm(iname), iid)
            icon_ids = icon_ids_for(item, alias_ids)
            return {
                "event_id": event_id,
                "task_id": task_id,
                "item_name": item.name,
                "item_id": item.item_id,
                "virtual": item.virtual,
                "required": item.required,
                "match_names": item.match_names,
                "icon_ids": icon_ids,
                "teams": list(teams.values()),
            }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Not found", f"No event {event_id}, or no such set/item.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


def _history_points(tasks: dict, all_rows: list) -> dict:
    """``{completion_id: points that row credited}``.

    loot_sweep tasks use the exact incremental receipt fold; every other task
    type credits its face ``points`` to the row that pushes a team's summed
    progress across the completion threshold (exact for simple count tasks, a
    reasonable approximation for distinct/grouped goals) and 0 to partial rows.
    The task's face value travels separately as ``task_points`` so the UI never
    has to infer it."""
    from collections import defaultdict

    from services.event_engine import _task_to_dict, completion_threshold
    from services.loot_sweep import LootSweepConfig, receipt_points_by_row

    by_task: dict = defaultdict(list)
    for r in all_rows:
        by_task[r.task_id].append(r)

    out: dict = {}
    for task_id, rows in by_task.items():
        task = tasks.get(task_id)
        if task is None:
            for r in rows:
                out[r.id] = 0.0
            continue
        if task.type == "loot_sweep":
            out.update(receipt_points_by_row(rows, LootSweepConfig(task.config)))
            continue
        try:
            threshold = max(int(completion_threshold(_task_to_dict(task))), 1)
        except Exception:
            threshold = 1
        face = float(task.points or 0)
        cum: dict = defaultdict(int)
        done: set = set()
        for r in rows:  # already global created-asc; monotonic per team
            pts = 0.0
            tid = r.team_id
            if tid is not None and tid not in done:
                before = cum[tid]
                after = before + max(int(r.quantity or 1), 1)
                cum[tid] = after
                if before < threshold <= after:
                    pts = face
                    done.add(tid)
            out[r.id] = pts
    return out


@events_bp.get("/events/<int:event_id>/completions/history")
async def get_completion_history(event_id: int):
    """Public, read-only timeline of applied task completions — the centralized
    "where the points came from" view for loot_sweep and every other kind.

    Only applied rows (``auto``/``confirmed``/``manual``) for **public** tasks
    are visible to the general public; event admins additionally see hidden
    tasks and the real RSN behind a hidden player (whose identity is otherwise
    masked to "Hidden player" — the completion itself always stays visible so
    public point totals reconcile, which is what keeps an event auditable and
    fair). Filter with ``teamId``, ``taskId`` and ``player``; paginated."""
    from db import Player

    viewer_id = optional_user_id()

    def _int_or_none(name: str):
        raw = request.args.get(name)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            abort_problem(422, f"Invalid {name}", f"'{name}' must be an integer.")

    team_arg = _int_or_none("teamId")
    task_arg = _int_or_none("taskId")
    player_q = (request.args.get("player") or "").strip()
    page, limit = parse_page(request, default_limit=50, max_limit=100)

    def _load():
        from services.event_engine import display_note

        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None
            is_admin = _is_event_admin(s, viewer_id, ev)

            task_rows = s.query(EventTask).filter(EventTask.event_id == event_id).all()
            tasks = {t.id: t for t in task_rows}
            if is_admin:
                visible_task_ids = set(tasks)
            else:
                visible_task_ids = {
                    t.id for t in task_rows if (t.visibility or "public") == "public"
                }

            base = {
                "event_id": event_id,
                "kind": getattr(ev, "kind", "standard") or "standard",
                "is_admin": bool(is_admin),
            }
            if not visible_task_ids:
                return {**base, "entries": [],
                        "meta": {"page": page, "limit": limit, "total": 0}}

            # Batch 2 scale rework. The per-row points are a stateful fold
            # over the ordered ledger (cumulative threshold crossing per
            # team; loot_sweep receipt decay per (team, item)), so a page
            # can't be computed from page rows alone. Two levers instead:
            # - teamId/taskId push into SQL — exact, because both folds are
            #   keyed by (task, team) and never read across the filter; and
            # - the expensive UNFILTERED view builds its entry list once per
            #   ledger version (max completion id) and serves pages from a
            #   short Redis cache — hot events stop re-folding the whole
            #   ledger for every viewer. (A revoke doesn't mint an id, so a
            #   revoked row can linger for up to the 30s TTL.)
            entries_all = None
            cache_key = None
            if team_arg is None and task_arg is None:
                try:
                    from utils.redis import redis_client as _rc

                    version = (
                        s.query(func.max(EventCompletion.id))
                        .filter(EventCompletion.event_id == event_id)
                        .scalar() or 0
                    )
                    cache_key = (
                        f"events:{event_id}:history:"
                        f"{'admin' if is_admin else 'pub'}:{version}")
                    cached = _rc.client.get(cache_key)
                    if cached:
                        entries_all = json.loads(cached)
                except Exception:
                    cache_key = None

            if entries_all is None:
                rows_q = (
                    s.query(EventCompletion)
                    .filter(EventCompletion.event_id == event_id,
                            EventCompletion.status.in_(_APPLIED_STATUSES),
                            EventCompletion.task_id.in_(visible_task_ids))
                )
                if team_arg is not None:
                    rows_q = rows_q.filter(EventCompletion.team_id == team_arg)
                if task_arg is not None:
                    rows_q = rows_q.filter(EventCompletion.task_id == task_arg)
                all_rows = (
                    rows_q
                    .order_by(EventCompletion.created_at.asc(),
                              EventCompletion.id.asc())
                    .all()
                )
                points_by_row = _history_points(tasks, all_rows)

                team_names = dict(
                    s.query(EventTeam.id, EventTeam.name)
                    .filter(EventTeam.event_id == event_id).all()
                )
                pids = {r.player_id for r in all_rows if r.player_id}
                pnames: dict = {}
                phidden: set = set()
                if pids:
                    for pid, name, hidden in (
                        s.query(Player.player_id, Player.player_name, Player.hidden)
                        .filter(Player.player_id.in_(pids)).all()
                    ):
                        pnames[pid] = name
                        if hidden:
                            phidden.add(pid)
                hidden_global = hidden_player_ids()

                entries_all = []
                for r in reversed(all_rows):  # newest-first
                    is_hidden = bool(r.player_id and
                                     (r.player_id in phidden or r.player_id in hidden_global))
                    if is_hidden and not is_admin:
                        disp_name, disp_pid = "Hidden player", None
                    else:
                        disp_name, disp_pid = pnames.get(r.player_id), r.player_id
                    task = tasks.get(r.task_id)
                    entries_all.append({
                        "completion_id": r.id,
                        "task_id": r.task_id,
                        "task_label": task.label if task else None,
                        "task_type": task.type if task else None,
                        "task_points": int(task.points or 0) if task else 0,
                        "team_id": r.team_id,
                        "team_name": team_names.get(r.team_id),
                        "player_id": disp_pid,
                        "player_name": disp_name,
                        "hidden": is_hidden,
                        "matched_target": r.matched_target,
                        "quantity": int(r.quantity or 1),
                        "points": score_num(points_by_row.get(r.id, 0.0)),
                        "source_type": r.source_type,
                        "status": r.status,
                        "proof_url": r.proof_url,
                        "note": display_note(r.note),
                        "created_at": _ts(r.created_at),
                    })
                if cache_key is not None:
                    try:
                        from utils.redis import redis_client as _rc

                        _rc.client.set(cache_key, json.dumps(entries_all), ex=30)
                    except Exception:
                        pass

            if player_q:
                needle = player_q.lower()
                entries = [e for e in entries_all
                           if e.get("player_name")
                           and needle in e["player_name"].lower()]
            else:
                entries = entries_all

            total = len(entries)
            start = (page - 1) * limit
            return {**base, "entries": entries[start:start + limit],
                    "meta": {"page": page, "limit": limit, "total": total}}

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Not found", f"No event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


_LS_IMAGE_MAX_BYTES = 4 * 1024 * 1024
_LS_IMAGE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
                     "WEBP": ("webp", "image/webp")}


@events_bp.post("/events/<int:event_id>/loot-sweep/image")
async def upload_loot_sweep_image(event_id: int):
    """Upload a custom boss/category image for a Loot Sweep group → B2 (the
    board-background pattern: server-side put, bucket CORS allows GET only).
    Returns the URL; the caller stores it in the group's ``image_url`` config."""
    import io
    import uuid as _uuid

    user_id = current_user_id()
    files = await request.files
    upload = files.get("file")
    if upload is None:
        abort_problem(422, "Invalid body", "A multipart 'file' field is required.")
    raw = upload.read()
    if not raw:
        abort_problem(422, "Empty file", "The uploaded image was empty.")
    if len(raw) > _LS_IMAGE_MAX_BYTES:
        abort_problem(422, "File too large", "Loot Sweep images are capped at 4 MB.")

    from PIL import Image, UnidentifiedImageError
    try:
        im = Image.open(io.BytesIO(raw))
        fmt_name = im.format
        width, height = im.size
        im.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG, or WebP image.")
    fmt = _LS_IMAGE_FORMATS.get(fmt_name or "")
    if fmt is None:
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG, or WebP image.")
    ext, content_type = fmt

    def _check():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if ev is None:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev)

    await asyncio.to_thread(_check)

    key = f"dt_uploads/loot_sweep/{event_id}-{_uuid.uuid4().hex[:12]}.{ext}"
    try:
        from utils.b2_storage import upload_bytes
        from web_api.routes.submissions import B2_CDN_BASE_URL
        await upload_bytes(raw, key, content_type)
        public_url = f"{B2_CDN_BASE_URL.rstrip('/')}/{key}"
    except Exception as e:
        abort_problem(502, "Upload service unavailable", str(e))

    await asyncio.to_thread(_audit_ls_image, user_id, event_id, public_url)
    return private_no_store(jsonify({"url": public_url, "width": width, "height": height}))


def _audit_ls_image(user_id, event_id: int, url: str) -> None:
    with db_session() as s:
        ev = s.query(Event).filter(Event.id == event_id).first()
        s.add(AuditLog(actor_user_id=user_id, group_id=getattr(ev, "group_id", None),
                       event_id=event_id,
                       action="event.loot_sweep.image", target=str(event_id),
                       after=url[:250]))
        s.commit()


# --------------------------------------------------------------------------- #
# Admin writes
# --------------------------------------------------------------------------- #
def _assert_event_admin(s, user_id, ev_or_group_id):
    """Event-admin gate. Accepts an ``Event`` (any mode) or a raw group id
    (the create path, which has no event yet — always standard semantics).

    Standard/global: group admin + 'events' entitlement (superadmin only for
    global). clan_vs_clan: an owner/admin of ANY *accepted* participating clan
    may co-manage — the entitlement was paid by the host at create/invite
    time, and an accepting opponent never needs a tier.
    """
    ev = ev_or_group_id if hasattr(ev_or_group_id, "group_id") else None
    if ev is not None and (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
        user = load_user(s, user_id)
        if is_superadmin(user):
            return
        mgids = manageable_guild_ids(user_id)
        for gid in participating_group_ids(s, ev):
            if (resolve_group_role(s, user_id, gid, mgids, user=user) in ("owner", "admin")
                    or is_event_manager(s, user_id, gid)):
                return
        abort_problem(403, "Forbidden", "You must administer a participating clan.")
    group_id = ev.group_id if ev is not None else ev_or_group_id
    # ---- standard/global path ----
    user = load_user(s, user_id)
    if not group_id:
        # Global events (group_id NULL) are administered by superadmins only.
        assert_superadmin(user)
        return
    # web64a: group admins OR event managers, and (for both) the events tier.
    assert_event_editor(
        s,
        user_id,
        group_id,
        manage_guild_ids=manageable_guild_ids(user_id),
        user=user,
    )


@events_bp.post("/events")
async def create_event():
    user_id = current_user_id()
    body = await json_body()
    group_id = body.get("group_id")
    name = (body.get("name") or "").strip()
    # group_id null => global event; _assert_event_admin requires superadmin.
    if group_id is not None and not isinstance(group_id, int):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer or null.")
    if not (1 <= len(name) <= 120):
        abort_problem(422, "Invalid name", "Event name must be 1–120 characters.")
    formation_mode = body.get("formation_mode") or "admin_assign"
    if formation_mode not in EVENT_FORMATION_MODES:
        abort_problem(
            422,
            "Invalid formation mode",
            f"formation_mode must be one of {list(EVENT_FORMATION_MODES)}.",
        )
    join_code = body.get("join_code")
    if join_code is not None and not isinstance(join_code, str):
        abort_problem(422, "Invalid join code", "join_code must be a string or null.")
    join_code = (join_code or "").strip()
    if len(join_code) > 32:
        abort_problem(422, "Invalid join code", "join_code must be at most 32 characters.")
    # Default: non-plugin (manual) submissions need admin review (2026-07-17).
    submission_policy = body.get("submission_policy") or "confirm_non_api"
    if submission_policy not in EVENT_SUBMISSION_POLICIES:
        abort_problem(
            422,
            "Invalid submission policy",
            f"submission_policy must be one of {list(EVENT_SUBMISSION_POLICIES)}.",
        )
    mode = body.get("mode") or "standard"
    if mode not in EVENT_MODES:
        abort_problem(422, "Invalid mode", f"mode must be one of {list(EVENT_MODES)}.")
    visibility = body.get("visibility") or "public"
    if visibility not in EVENT_VISIBILITIES:
        abort_problem(
            422, "Invalid visibility",
            f"visibility must be one of {list(EVENT_VISIBILITIES)}.",
        )
    # Game format (web43a) — orthogonal to mode. Which kinds THIS user may
    # create is gated inside _apply() (needs a session + superadmin check).
    kind = body.get("kind") or "standard"
    if kind not in EVENT_KINDS:
        abort_problem(422, "Invalid kind", f"kind must be one of {list(EVENT_KINDS)}.")
    # When the Discord scheduled-event mirror goes live: the creation form's
    # "create the Discord event now / when the event goes live" prompt.
    discord_event_policy = body.get("discord_event_policy") or "on_activate"
    if discord_event_policy not in EVENT_DISCORD_POLICIES:
        abort_problem(
            422, "Invalid Discord event policy",
            f"discord_event_policy must be one of {list(EVENT_DISCORD_POLICIES)}.",
        )
    ping_config = _parse_ping_config(body.get("pings"))
    if mode == "clan_vs_clan" and not group_id:
        abort_problem(
            422,
            "Host group required",
            "A clan-vs-clan event needs a host group_id (global clan-vs-clan "
            "events are not a thing).",
        )

    def _apply():
        with db_session() as s:
            # Entitlement/admin on the HOST group — the host "pays" exactly
            # as for a standard event; invited opponents never need a tier.
            _assert_event_admin(s, user_id, group_id)
            # Site-wide event-type gate (web43a): a disabled/admin_only kind
            # is creatable only by superadmins and the kind's test groups.
            # Creation-only — existing events of a toggled-off kind still run.
            # creation_restricted is query-free on the common enabled path
            # (warm registry cache); the user lookup only happens for
            # restricted kinds.
            from services.event_types import creation_restricted

            if creation_restricted(s, kind, group_id=group_id) and not is_superadmin(
                load_user(s, user_id)
            ):
                abort_problem(
                    403, "Event type unavailable",
                    f"The '{kind}' event type is not currently available to "
                    "your group.",
                )
            # Group events default their Discord destination to the group's
            # linked guild (Task 19); admins can re-point it at any guild the
            # bot is in via PUT /events/{id}/discord.
            discord_guild_id = None
            if group_id:
                group = s.query(Group).filter(Group.group_id == group_id).first()
                if group and group.guild_id:
                    discord_guild_id = str(group.guild_id)
            # Explicit lifecycle (Task 21): events are born as drafts and go
            # live only through POST /events/{id}/activate (or the scheduler
            # sweep once starts_at passes). Drafts are unlimited; the tier
            # concurrency limit binds at activation.
            ev = Event(
                group_id=group_id,
                name=name,
                description=(body.get("description") or None),
                status="draft",
                visibility=visibility,
                starts_at=_dt(body.get("starts_at")),
                ends_at=_dt(body.get("ends_at")),
                has_bingo=False,
                formation_mode=formation_mode,
                requires_confirmation=bool(body.get("requires_confirmation")),
                allow_live_edits=bool(body.get("allow_live_edits")),
                effort_visibility=_effort_visibility_value(
                    body.get("effort_visibility")),
                allow_late_signups=bool(body.get("allow_late_signups")),
                submission_policy=submission_policy,
                join_code=join_code or None,
                discord_guild_id=discord_guild_id,
                mode=mode,
                kind=kind,
                discord_event_policy=discord_event_policy,
                ping_config=ping_config,
            )
            s.add(ev)
            s.commit()
            if mode == "clan_vs_clan":
                # Seed the host as an accepted participant; opponents are
                # invited via POST /events/{id}/participants.
                s.add(EventGroup(
                    event_id=ev.id,
                    group_id=group_id,
                    role="host",
                    status="accepted",
                    invited_by_user_id=user_id,
                    responded_at=func.now(),
                ))
                s.commit()
            if ev.discord_guild_id:
                # Desired-state rows for the Discord scheduled event mirror.
                # Default policy ('on_activate') makes this a no-op for the
                # newborn draft — the rows are seeded by activate_event()
                # instead. 'immediate' seeds them now (and the bot reconciler
                # still holds off until starts_at is set and in the future).
                _sync_event_guilds(s, ev)
                s.commit()
            return ev.id

    ev_id = await asyncio.to_thread(_apply)
    return jsonify({"id": ev_id})


def _event_settings_snapshot(ev) -> dict:
    """Audit snapshot of the scoring/config fields whose changes a manager
    needs to trace (web57a) — points multipliers, review policy, schedule and
    identity — so `event.settings.update` can record a precise before/after."""
    starts_at = getattr(ev, "starts_at", None)
    ends_at = getattr(ev, "ends_at", None)
    return {
        "name": getattr(ev, "name", None),
        "visibility": getattr(ev, "visibility", None),
        "mode": getattr(ev, "mode", None),
        "kind": getattr(ev, "kind", None),
        "formation_mode": getattr(ev, "formation_mode", None),
        "requires_confirmation": bool(getattr(ev, "requires_confirmation", False)),
        "submission_policy": getattr(ev, "submission_policy", None),
        "bonus_line_points": int(getattr(ev, "bonus_line_points", 0) or 0),
        "bonus_blackout_points": int(getattr(ev, "bonus_blackout_points", 0) or 0),
        "buyins_enabled": bool(getattr(ev, "buyins_enabled", False)),
        "allow_live_edits": bool(getattr(ev, "allow_live_edits", False)),
        "effort_visibility": getattr(ev, "effort_visibility", None) or "public",
        "allow_late_signups": bool(getattr(ev, "allow_late_signups", False)),
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
    }


@events_bp.patch("/events/<int:event_id>")
async def update_event(event_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev)
            _settings_before = _event_settings_snapshot(ev)
            if "name" in body:
                name = (body.get("name") or "").strip()
                if not (1 <= len(name) <= 120):
                    abort_problem(422, "Invalid name", "Event name must be 1–120 characters.")
                ev.name = name
            if "description" in body:
                ev.description = body.get("description") or None
            if "visibility" in body:
                vis = body.get("visibility") or "public"
                if vis not in EVENT_VISIBILITIES:
                    abort_problem(
                        422, "Invalid visibility",
                        f"visibility must be one of {list(EVENT_VISIBILITIES)}.",
                    )
                ev.visibility = vis
            if "starts_at" in body:
                ev.starts_at = _dt(body.get("starts_at"))
            if "ends_at" in body:
                ev.ends_at = _dt(body.get("ends_at"))
            if "formation_mode" in body:
                mode = body.get("formation_mode")
                if mode not in EVENT_FORMATION_MODES:
                    abort_problem(
                        422,
                        "Invalid formation mode",
                        f"formation_mode must be one of {list(EVENT_FORMATION_MODES)}.",
                    )
                ev.formation_mode = mode
            if "join_code" in body:
                code = body.get("join_code")
                if code is not None and not isinstance(code, str):
                    abort_problem(422, "Invalid join code", "join_code must be a string or null.")
                code = (code or "").strip()
                if len(code) > 32:
                    abort_problem(422, "Invalid join code", "join_code must be at most 32 characters.")
                ev.join_code = code or None
            if "mode" in body:
                new_mode = body.get("mode") or "standard"
                if new_mode not in EVENT_MODES:
                    abort_problem(422, "Invalid mode", f"mode must be one of {list(EVENT_MODES)}.")
                if new_mode != (getattr(ev, "mode", None) or "standard"):
                    # Mode is a structural choice: only a team-less draft may
                    # convert (participant rows and clan-bound teams would
                    # otherwise be stranded).
                    if ev.status != "draft":
                        abort_problem(409, "Event already started",
                                      "The event mode can only change while it is a draft.")
                    if s.query(EventTeam.id).filter(EventTeam.event_id == ev.id).first():
                        abort_problem(409, "Teams exist",
                                      "Remove the event's teams before changing its mode.")
                    if new_mode == "clan_vs_clan":
                        if not ev.group_id:
                            abort_problem(422, "Host group required",
                                          "A global event cannot become clan-vs-clan.")
                        s.add(EventGroup(
                            event_id=ev.id, group_id=ev.group_id, role="host",
                            status="accepted", invited_by_user_id=user_id,
                            responded_at=func.now(),
                        ))
                    else:
                        s.query(EventGroup).filter(
                            EventGroup.event_id == ev.id
                        ).delete(synchronize_session=False)
                    ev.mode = new_mode
            if "kind" in body:
                new_kind = body.get("kind") or "standard"
                if new_kind not in EVENT_KINDS:
                    abort_problem(422, "Invalid kind",
                                  f"kind must be one of {list(EVENT_KINDS)}.")
                if new_kind != (getattr(ev, "kind", None) or "standard"):
                    # Kind is structural (board/bingo state hangs off it):
                    # drafts only, and the site-wide type gate re-binds.
                    if ev.status != "draft":
                        abort_problem(409, "Event already started",
                                      "The event kind can only change while it is a draft.")
                    from services.event_types import is_event_type_creatable

                    if not is_event_type_creatable(
                        s, new_kind,
                        is_superadmin=is_superadmin(load_user(s, user_id)),
                        group_id=ev.group_id,
                    ):
                        abort_problem(
                            403, "Event type unavailable",
                            f"The '{new_kind}' event type is not currently "
                            "available to your group.",
                        )
                    ev.kind = new_kind
            if "requires_confirmation" in body:
                # Event-level force: all completions queue for review (PRD D3).
                ev.requires_confirmation = bool(body.get("requires_confirmation"))
            if "allow_live_edits" in body:
                # web68a: opt-in mid-event editability (unlocks the bingo board
                # while active). Flippable at any status — the settings-snapshot
                # diff below audits every change.
                ev.allow_live_edits = bool(body.get("allow_live_edits"))
            if "effort_visibility" in body:
                # web74a: whether EHE shows on the public team/player surfaces
                # or only in the managers' effort report. Recording is
                # unaffected, so flipping it ON later reveals the full history.
                ev.effort_visibility = _effort_visibility_value(
                    body.get("effort_visibility"))
            if "allow_late_signups" in body:
                # web70a: keep self sign-ups open after the event begins. Off by
                # default — the sign-up window normally ends at the start, and
                # the posted Discord prompt retires its button then. Flipping it
                # ON mid-event reopens sign-ups; the retired prompt is not
                # re-posted (post a fresh one from Event → Discord).
                ev.allow_late_signups = bool(body.get("allow_late_signups"))
            if "submission_policy" in body:
                # null = reset to the default (manual submissions need review).
                policy = body.get("submission_policy") or "confirm_non_api"
                if policy not in EVENT_SUBMISSION_POLICIES:
                    abort_problem(
                        422,
                        "Invalid submission policy",
                        f"submission_policy must be one of {list(EVENT_SUBMISSION_POLICIES)}.",
                    )
                ev.submission_policy = policy
            if "leadership" in body:
                from web_api.event_leadership import (
                    effective_leadership,
                    normalize_leadership_input,
                )

                norm = normalize_leadership_input(body.get("leadership") or {})
                if norm is None:
                    abort_problem(
                        422, "Invalid leadership config",
                        "leadership must be {enabled?: bool, co_leaders?: bool, "
                        "selection?: 'admin'|'election'}.",
                    )
                merged = effective_leadership(getattr(ev, "leadership_config", None))
                merged.update(norm)
                ev.leadership_config = json.dumps(merged)
                if not merged["co_leaders"]:
                    # Co-leaders switched off: demote existing ones so the
                    # authority checks and rosters don't keep a dead role.
                    team_ids = [
                        tid for (tid,) in s.query(EventTeam.id)
                        .filter(EventTeam.event_id == ev.id).all()
                    ]
                    if team_ids:
                        (s.query(EventTeamMember)
                         .filter(EventTeamMember.team_id.in_(team_ids),
                                 EventTeamMember.role == "co_leader")
                         .update({EventTeamMember.role: None},
                                 synchronize_session=False))
            if "buyins_enabled" in body or "prize_config" in body:
                # Prize pot (web52a): master toggle + JSON knobs, merged like
                # the leadership config above.
                from db import EventBuyin
                from web_api.event_prizes import (
                    effective_prize_config,
                    normalize_prize_input,
                )

                if "buyins_enabled" in body:
                    enabled = bool(body.get("buyins_enabled"))
                    if bool(getattr(ev, "buyins_enabled", False)) and not enabled:
                        # Confirm-on-disable: disabling hides the pot but keeps
                        # the records (re-enabling restores it), so any recorded
                        # buy-in/donation gates the toggle behind an explicit
                        # confirm_disable_buyins flag — the DELETE confirm_name
                        # idiom applied to the pot toggle. The 409 carries
                        # {count, total} so the client can render the confirm.
                        stats = (
                            s.query(
                                func.count(EventBuyin.id),
                                func.coalesce(func.sum(EventBuyin.amount), 0),
                            )
                            .filter(EventBuyin.event_id == ev.id,
                                    EventBuyin.status != "void")
                            .first()
                        )
                        count = int(stats[0] or 0)
                        total = int(stats[1] or 0)
                        if count > 0 and not bool(body.get("confirm_disable_buyins")):
                            abort_problem(
                                409, "Buy-ins present",
                                f"This event has {count} recorded buy-ins/donations "
                                f"totalling {total:,} GP — disabling hides the pot but "
                                "keeps the records. Re-send with confirm_disable_buyins "
                                "to continue.",
                                type_="buyins-present",
                                extra={"count": count, "total": total},
                            )
                    ev.buyins_enabled = enabled
                if "prize_config" in body:
                    norm = normalize_prize_input(body.get("prize_config") or {})
                    if norm is None:
                        abort_problem(
                            422, "Invalid prize config",
                            "prize_config must be {default_buyin?: int>=0, "
                            "distribution?: 'first_only'|'top_n'|'custom_split', "
                            "top_n?: int>=1, splits?: [positive ints summing to 100], "
                            "advertise?: bool, show_contributors?: bool, "
                            "allow_leader_mark?: bool}.",
                        )
                    merged = effective_prize_config(getattr(ev, "prize_config", None))
                    merged.update(norm)
                    ev.prize_config = json.dumps(merged)
            # Bingo bonus config (Task 20, PRD D7): 0 disables a bonus. The
            # board itself is replaced via PUT /events/{id}/bingo.
            for key in ("bonus_line_points", "bonus_blackout_points"):
                if key in body:
                    val = body.get(key)
                    if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                        abort_problem(
                            422, "Invalid bonus points",
                            f"'{key}' must be a non-negative integer.",
                        )
                    setattr(ev, key, val)
            if any(k in body for k in ("name", "description", "starts_at", "ends_at")):
                # Flip synced guild rows back to pending so the bot edits the
                # live Discord scheduled event (never re-creates: the row
                # keeps its discord_scheduled_event_id).
                _sync_event_guilds(s, ev)
            # Audit only the scoring/config fields that actually changed — a
            # no-op PATCH (or one touching non-tracked keys) writes no row.
            _settings_after = _event_settings_snapshot(ev)
            _changed = {
                k: {"from": _settings_before[k], "to": v}
                for k, v in _settings_after.items()
                if _settings_before.get(k) != v
            }
            if _changed:
                s.add(AuditLog(
                    actor_user_id=user_id, group_id=ev.group_id, event_id=event_id,
                    action="event.settings.update",
                    target=f"web_events.{ev.id}",
                    after=json.dumps(_changed),
                ))
            s.commit()
            return _detail(s, ev, viewer_id=user_id)

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


def _enqueue_orphan_scheduled_events(s, event_id: int) -> None:
    """Before dropping an event's ``web_event_guilds`` rows, hand any live
    Discord scheduled events to the bot's teardown queue (the Web API never
    talks to Discord). Best-effort: a Redis hiccup must not block the delete —
    a stray Discord scheduled event is recoverable; a half-deleted event is
    not."""
    try:
        from services.event_scheduled_events import (
            ORPHAN_SCHED_EVENTS_KEY,
            orphan_scheduled_event_payloads,
        )
        from utils.redis import redis_client

        for payload in orphan_scheduled_event_payloads(s, event_id):
            redis_client.rpush(ORPHAN_SCHED_EVENTS_KEY, json.dumps(payload))
    except Exception:
        pass


def _enqueue_orphan_team_discord(s, event_id: int, team_id: int | None = None) -> None:
    """Before FK-wiping ``web_event_team_discord`` rows (event hard delete, or
    a single team's delete), hand any live auto-created roles/channels to the
    bot's teardown queue (web53a). Best-effort, same contract as
    :func:`_enqueue_orphan_scheduled_events`."""
    try:
        from services.event_team_discord import (
            enqueue_team_discord_orphans,
            orphan_team_discord_payloads,
        )
        from utils.redis import redis_client

        enqueue_team_discord_orphans(
            redis_client, orphan_team_discord_payloads(s, event_id, team_id))
    except Exception:
        pass


def _sync_team_discord(s, ev: Event) -> None:
    """Re-materialize the desired ``web_event_team_discord`` rows (web53a).
    ImportError-guarded like every web_api -> services lazy import (the
    unit-test conftest stubs ``services``); real failures still surface."""
    try:
        from services.event_team_discord import sync_event_team_discord
    except ImportError:
        return
    sync_event_team_discord(s, ev)


def _mark_team_discord_dirty(s, event_id: int, team_id: int | None = None) -> None:
    """Roster changed: flag the team's Discord rows for a membership re-sync
    on the bot's next tick (web53a). Same ImportError guard as above."""
    try:
        from services.event_team_discord import mark_team_members_dirty
    except ImportError:
        return
    mark_team_members_dirty(s, event_id, team_id)


def _sync_buyin_team(s, event_id: int, player_id: int, team_id: int | None) -> None:
    """Roster changed: point that player's live buy-ins at their new placement
    (``None`` = back in the pool / off the roster).

    This is what lets a buy-in be recorded at sign-up, before any draft: see
    ``services/event_buyins.py`` for the invariant. Lazy import — the unit-test
    conftest stubs the ``services`` package."""
    from services.event_buyins import sync_buyin_team

    sync_buyin_team(s, event_id, player_id, team_id)


def _drop_team_discord_rows(s, event_id: int, team_id: int) -> None:
    """Team hard delete: queue live role/channel teardown for the bot, then
    drop the rows themselves (their team FK is about to go away)."""
    try:
        from db import EventTeamDiscord
        from services.event_team_discord import (
            enqueue_team_discord_orphans,
            orphan_team_discord_payloads,
        )
    except ImportError:
        return
    try:
        from utils.redis import redis_client

        enqueue_team_discord_orphans(
            redis_client, orphan_team_discord_payloads(s, event_id, team_id))
    except Exception:
        pass  # teardown loss is tolerable; the row wipe below is not
    s.query(EventTeamDiscord).filter(
        EventTeamDiscord.team_id == team_id
    ).delete(synchronize_session=False)


def _cascade_delete_event(s, ev: Event) -> None:
    """Delete ``ev`` and every row scoped to it — children first, since no ORM
    cascade is configured on these FKs (mirrors the per-team cascade in
    ``delete_team``, widened to the whole event). Order matters: board-game
    effects reference inventory rows; everything team/task/cell-scoped is
    removed before the teams/tasks/cells themselves; event templates keep only
    a nullable provenance pointer, which we null out."""
    from db import (
        EventBoardConfig,
        EventBoardEffect,
        EventBoardPosition,
        EventBoardTile,
        EventBuyin,
        EventChannel,
        EventCoinLedger,
        EventGuild,
        EventShopRotation,
        EventTeamCooldown,
        EventTeamInventory,
        EventTemplate,
    )

    event_id = ev.id
    team_ids = [
        tid for (tid,) in s.query(EventTeam.id).filter(EventTeam.event_id == event_id).all()
    ]
    cell_ids = [
        cid
        for (cid,) in s.query(EventBingoCell.id).filter(EventBingoCell.event_id == event_id).all()
    ]

    def _wipe(model, *conds) -> None:
        s.query(model).filter(*conds).delete(synchronize_session=False)

    # Board-game economy (web44a–web50a). Effects reference inventory rows, so
    # they must go first; the rest are plain event_id children.
    _wipe(EventBoardEffect, EventBoardEffect.event_id == event_id)
    _wipe(EventCoinLedger, EventCoinLedger.event_id == event_id)
    _wipe(EventTeamCooldown, EventTeamCooldown.event_id == event_id)
    _wipe(EventTeamInventory, EventTeamInventory.event_id == event_id)
    _wipe(EventShopRotation, EventShopRotation.event_id == event_id)
    _wipe(EventBoardPosition, EventBoardPosition.event_id == event_id)
    _wipe(EventBoardConfig, EventBoardConfig.event_id == event_id)
    _wipe(EventBoardTile, EventBoardTile.event_id == event_id)

    # Points / progress / completion ledger + prize-pot ledger (web52a).
    _wipe(EventPlayerPoints, EventPlayerPoints.event_id == event_id)
    _wipe(EventProgress, EventProgress.event_id == event_id)
    _wipe(EventCompletion, EventCompletion.event_id == event_id)
    _wipe(EventBuyin, EventBuyin.event_id == event_id)

    # Bingo completions hang off cells (no event_id), so scope them by cell.
    if cell_ids:
        _wipe(EventBingoCompletion, EventBingoCompletion.cell_id.in_(cell_ids))
    _wipe(EventBingoCell, EventBingoCell.event_id == event_id)

    # Rosters / votes / signups. Team members hang off teams (no event_id).
    _wipe(EventLeaderVote, EventLeaderVote.event_id == event_id)
    _wipe(EventSignup, EventSignup.event_id == event_id)
    # Posted sign-up prompts (web70a). The Discord messages themselves go with
    # the channel/event teardown the caller already queued; these rows only
    # exist so the bot could find them, and their FK would block the delete.
    _wipe(EventSignupMessage, EventSignupMessage.event_id == event_id)
    if team_ids:
        _wipe(EventTeamMember, EventTeamMember.team_id.in_(team_ids))

    # Discord destination + scheduled-event mirror rows (the real Discord
    # scheduled events were already queued for teardown by the caller), plus
    # the per-team role/channel rows (web53a — real roles/channels likewise
    # already queued on the orphan list by the caller).
    from db import EventTeamDiscord
    from db.models import EventMessageLayout

    _wipe(EventChannel, EventChannel.event_id == event_id)
    _wipe(EventGuild, EventGuild.event_id == event_id)
    _wipe(EventTeamDiscord, EventTeamDiscord.event_id == event_id)
    # Per-event message-layout overrides (web66a; event_id 0 rows are the
    # group-level layouts and are untouched — event_id here is a real id).
    _wipe(EventMessageLayout, EventMessageLayout.event_id == event_id)

    # The tasks + teams those children referenced, then the participants.
    _wipe(EventTask, EventTask.event_id == event_id)
    _wipe(EventTeam, EventTeam.event_id == event_id)
    _wipe(EventGroup, EventGroup.event_id == event_id)

    # Templates keep a nullable provenance pointer (ondelete SET NULL). Null it
    # explicitly so the delete never trips the FK even if the live constraint
    # wasn't created with SET NULL — templates outlive the events they came
    # from.
    s.query(EventTemplate).filter(EventTemplate.source_event_id == event_id).update(
        {EventTemplate.source_event_id: None}, synchronize_session=False
    )

    s.delete(ev)


@events_bp.delete("/events/<int:event_id>")
async def delete_event(event_id: int):
    """Permanently delete an event and everything scoped to it — tasks, teams,
    rosters, progress/completion ledger, bingo + board-game state, sign-ups and
    Discord config rows. Event admins (group owner/admin, or superadmin for
    global events) only; audit-logged.

    Guardrails: a *live* event must be ended first (409) so its Discord
    scheduled events, standings board and active-matching state wind down in
    order; and the caller must echo the event's exact name in ``confirm_name``
    (422 otherwise) — the explicit confirmation that keeps a misfired request
    from erasing real history. Drafts and ended events are fair game (the point
    is to keep abandoned drafts from cluttering the history)."""
    user_id = current_user_id()
    body = await json_body()
    confirm = body.get("confirm_name")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            if _effective_status(ev) == "active":
                abort_problem(
                    409,
                    "Event is live",
                    "End the event before deleting it — a running event can't "
                    "be deleted.",
                )
            # Explicit confirmation: the caller must echo the event name
            # (case/whitespace-insensitive), matching the UI's type-to-confirm.
            want = " ".join((ev.name or "").strip().lower().split())
            got = (
                " ".join(confirm.strip().lower().split())
                if isinstance(confirm, str)
                else ""
            )
            if not got or got != want:
                abort_problem(
                    422,
                    "Confirmation required",
                    "Type the event's exact name to confirm deletion.",
                )

            group_id = ev.group_id
            name = ev.name
            eff = _effective_status(ev)

            # Hand any live Discord scheduled events + auto-created team
            # roles/channels to the bot for teardown before we drop the rows
            # that describe them.
            _enqueue_orphan_scheduled_events(s, event_id)
            _enqueue_orphan_team_discord(s, event_id)
            _cascade_delete_event(s, ev)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    event_id=event_id,
                    action="event.delete",
                    target=f"web_events.{event_id}",
                    before=f"name:{name} status:{eff}",
                    after=None,
                )
            )
            s.commit()

    await asyncio.to_thread(_apply)
    # Forget it in Redis: drop from the active-matching set and nudge the
    # consumer to refresh its matcher state (both best-effort no-ops on error).
    try:
        from services.event_lifecycle import _mark_active_in_redis

        _mark_active_in_redis(event_id, False)
    except Exception:
        pass
    _bump(event_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Explicit lifecycle (Task 21): draft -> active -> past, one-way.
# The scheduler sweep in workers/event_consumer.py reuses the exact same
# services.event_lifecycle functions — one code path.
# --------------------------------------------------------------------------- #
def _run_lifecycle_transition(event_id: int, user_id: int, action: str) -> dict:
    # Lazy service import (pytest conftest stubs `services`).
    from services import event_lifecycle

    with db_session() as s:
        ev = _load_event_or_404(s, event_id)
        _assert_event_admin(s, user_id, ev)
        user = load_user(s, user_id)
        try:
            if action == "activate":
                event_lifecycle.activate_event(s, ev, actor_user_id=user_id, user=user)
            else:
                event_lifecycle.end_event(s, ev, actor_user_id=user_id)
        except event_lifecycle.LifecycleError as exc:
            abort_problem(exc.status, exc.title, exc.detail)
        s.commit()
        return _detail(s, ev, viewer_id=user_id)


@events_bp.get("/events/<int:event_id>/readiness")
async def event_readiness(event_id: int):
    """Pre-flight the activation checks WITHOUT activating (event admin). Powers
    the manager's "Check readiness" button so a leader can confirm the event
    will be ready when its start time is reached — and, when it isn't, get the
    structured list of what to fix (each tagged with the section to fix it in).
    Read-only; safe to poll."""
    user_id = current_user_id()

    def _check():
        from services import event_lifecycle

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            return event_lifecycle.readiness_report(s, ev)

    return private_no_store(jsonify(await asyncio.to_thread(_check)))


@events_bp.post("/events/<int:event_id>/activate")
async def activate_event(event_id: int):
    """Explicit activation (event admin). Validates readiness (≥1 team, a
    complete bingo board when has_bingo, a future end date) and the tier's
    ``events_max_active`` concurrency limit (409 at the limit; global events
    and superadmins bypass). Effects live in services.event_lifecycle."""
    user_id = current_user_id()
    payload = await asyncio.to_thread(_run_lifecycle_transition, event_id, user_id, "activate")
    _bump(event_id)
    return private_no_store(jsonify(payload))


@events_bp.post("/events/<int:event_id>/end")
async def end_event(event_id: int):
    """Explicit end (event admin): active -> past, final standings announced."""
    user_id = current_user_id()
    payload = await asyncio.to_thread(_run_lifecycle_transition, event_id, user_id, "end")
    _bump(event_id)
    return private_no_store(jsonify(payload))


def clean_task_visibility(body: dict, default: str | None = "private") -> str | None:
    """Validated EVENT_TASK_VISIBILITIES value from a request body.

    ``default`` is returned when the key is absent ("private" on create —
    audit: defaulting to public quietly shipped clan-specific labels into the
    shared cross-group library; sharing is now the deliberate choice — and
    None on PATCH, where an absent key means "leave the library copy
    alone")."""
    if "visibility" not in body:
        return default
    visibility = body.get("visibility")
    if visibility not in EVENT_TASK_VISIBILITIES:
        abort_problem(
            422, "Invalid visibility",
            f"visibility must be one of {list(EVENT_TASK_VISIBILITIES)}.",
        )
    return visibility


def _canonical_config(config) -> str | None:
    """Whitespace/key-order-insensitive form of a task config for equality
    checks. Falls back to the raw value on unparseable input."""
    if not config:
        return None
    try:
        parsed = json.loads(config) if isinstance(config, str) else config
    except (TypeError, ValueError):
        return str(config)
    if not parsed:
        return None
    return json.dumps(parsed, sort_keys=True)


def save_task_to_library(s, ev: Event, task: EventTask, visibility: str) -> str:
    """Upsert the task's reusable library copy (source='group').

    Public rows show in every group's picker; private rows only in the owning
    group's. Keyed per group by lower-cased name so re-saving a same-named
    task updates the preset instead of duplicating it. The bingo designer's
    ``bingo_auto`` config marker is task-instance bookkeeping and is stripped
    from the preset.

    The global library keeps at most one public preset per set of
    *requirements* (type + target + target_value + config; custom tasks are
    identified by their name too, since their requirements are free-form):

    - Copying a preset the group can already see (same name AND requirements)
      saves nothing — the picker row already exists.
    - Saving a same-requirements task under a new name as "public" is demoted
      to a private, group-only preset instead of a second public copy.
    - A "private" save never adopts a same-named PUBLIC row: library copies
      land in events as private tasks, so an edit of such a copy must stay
      independent of the shared template other clans already picked — only a
      deliberate "public" save updates the public preset.

    Returns the visibility actually stored so callers can mirror it onto the
    task row (it may differ from the requested one via the demotion rule)."""
    name = (task.label or "").strip()[:120]
    if not name:
        return visibility
    config = task.config
    if config:
        try:
            parsed = json.loads(config)
            if isinstance(parsed, dict) and parsed.pop("bingo_auto", None) is not None:
                config = json.dumps(parsed) if parsed else None
        except (TypeError, ValueError):
            pass
    target = task.target[:120] if task.target else None
    canon_config = _canonical_config(config)

    group_match = (
        EventTaskLibraryItem.group_id == ev.group_id
        if ev.group_id is not None
        else EventTaskLibraryItem.group_id.is_(None)
    )
    row = (
        s.query(EventTaskLibraryItem)
        .filter(
            EventTaskLibraryItem.source == "group",
            group_match,
            func.lower(EventTaskLibraryItem.name) == name.lower(),
        )
        .first()
    )
    if (row is not None and visibility == "private"
            and (row.visibility or "public") == "public"):
        # Editing an event task must not silently rewrite (or unshare) the
        # group's same-named PUBLIC preset — that's the shared template other
        # clans copy from. The task keeps its private visibility; the preset
        # only changes on an explicit "public" save.
        return visibility

    # Requirement duplicates among presets this group's picker already shows:
    # public rows from anywhere, plus the group's own. Target/type matching
    # rides the DB's case-insensitive collation; config equality is checked
    # canonically in Python (the table is small).
    target_filter = (
        EventTaskLibraryItem.target == target
        if target
        else sa_or(EventTaskLibraryItem.target.is_(None), EventTaskLibraryItem.target == "")
    )
    tv_filter = (
        EventTaskLibraryItem.target_value == task.target_value
        if task.target_value is not None
        else EventTaskLibraryItem.target_value.is_(None)
    )
    candidates = (
        s.query(EventTaskLibraryItem)
        .filter(
            EventTaskLibraryItem.active.is_(True),
            EventTaskLibraryItem.type == task.type,
            target_filter,
            tv_filter,
        )
        .all()
    )
    dups = [
        r for r in candidates
        if (row is None or r.id != row.id)
        and ((r.visibility or "public") == "public" or r.group_id == ev.group_id)
        and _canonical_config(r.config) == canon_config
    ]
    if task.type == "custom":
        # Free-form manual tasks: empty goal fields don't make two different
        # chores "the same task" — only an outright name collision does.
        dups = [r for r in dups if r.name.lower() == name.lower()]

    if row is None and any(r.name.lower() == name.lower() for r in dups):
        # Straight copy of a preset the group can already pick — saving a
        # duplicate row would only clutter the pickers.
        return visibility
    if visibility == "public" and any((r.visibility or "public") == "public" for r in dups):
        visibility = "private"

    if row is None:
        row = EventTaskLibraryItem(
            name=name, source="group", group_id=ev.group_id, default_points=0,
        )
        s.add(row)
    row.type = task.type
    row.target = target
    row.target_value = task.target_value
    row.default_points = int(task.points or 0)
    row.config = config or None
    row.visibility = visibility
    row.active = True
    # Board-game tier rides onto the preset ("change it if it already
    # exists" — the designer/task-form difficulty is the source of truth).
    if getattr(task, "difficulty", None) is not None:
        row.difficulty = task.difficulty
    return visibility


@events_bp.post("/events/<int:event_id>/tasks")
async def add_task(event_id: int):
    user_id = current_user_id()
    body = await json_body()
    ttype = body.get("type")
    label = (body.get("label") or "").strip()
    if ttype not in EVENT_TASK_TYPES:
        abort_problem(422, "Invalid task type", f"type must be one of {list(EVENT_TASK_TYPES)}.")
    if not label:
        abort_problem(422, "Invalid label", "Task label is required.")
    visibility = clean_task_visibility(body)
    # Board-game tier (web44a): tags the task into a difficulty-tile's roll
    # pool. Optional and harmless on non-board events.
    difficulty = body.get("difficulty")
    if difficulty is not None and difficulty not in EVENT_TASK_DIFFICULTIES:
        abort_problem(
            422, "Invalid difficulty",
            f"difficulty must be one of {list(EVENT_TASK_DIFFICULTIES)} or null.",
        )

    # Bounded here, not at the ORM: points is INT, so an out-of-range payout
    # would reach MySQL as an unhandled 1264 and 500 (see MAX_TASK_POINTS).
    # Mirrors the same check on the task PATCH route.
    from web_api.routes.event_task_validation import MAX_TASK_POINTS

    points = body.get("points") or 0
    if not isinstance(points, int) or isinstance(points, bool) or not (0 <= points <= MAX_TASK_POINTS):
        abort_problem(
            422, "Invalid points",
            f"'points' must be an integer between 0 and {MAX_TASK_POINTS:,}.",
        )

    def _apply():
        from web_api.routes.event_task_validation import validate_task_payload

        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev)
            _assert_event_not_past(ev)
            normalized = validate_task_payload(s, body)
            task = EventTask(
                event_id=event_id,
                type=ttype,
                label=label,
                points=points,
                requires_confirmation=bool(body.get("requires_confirmation")),
                visibility=visibility,
                difficulty=difficulty,
                **normalized,
            )
            s.add(task)
            task.visibility = save_task_to_library(s, ev, task, visibility)
            s.flush()  # populate task.id for the audit target
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                event_id=event_id,
                action="event.task.create",
                target=f"web_event_tasks.{task.id}",
                after=json.dumps({
                    "type": task.type, "label": task.label,
                    "points": int(task.points or 0),
                    "target": task.target, "target_value": task.target_value,
                    "visibility": task.visibility,
                }),
            ))
            s.commit()
            return task.id, task.visibility

    task_id, effective_visibility = await asyncio.to_thread(_apply)
    _bump(event_id)
    # visibility echoes what was stored: a "public" save whose requirements
    # duplicate an existing public preset is demoted to "private".
    return jsonify({"id": task_id, "visibility": effective_visibility})


@events_bp.get("/events/meta/types")
async def list_event_kinds():
    """Event kinds for the create form (session required).

    Every registry row is returned (so the UI can show 'staff testing'
    states), each annotated with ``creatable`` resolved for the current user
    and the ``group_id`` query param (omit/empty for a global event, where
    only superadmins create anyway)."""
    user_id = current_user_id()
    raw_gid = (request.args.get("group_id") or "").strip()
    group_id = int(raw_gid) if raw_gid.isdigit() else None

    def _read():
        from services.event_types import creatable_kinds

        with db_session() as s:
            user = load_user(s, user_id)
            return creatable_kinds(
                s, is_superadmin=is_superadmin(user), group_id=group_id
            )

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


# A name only counts as "receivable" once this many drop-rollup rows exist
# across its item ids — a single misreported drop (e.g. the one historical
# charged "Scythe of vitur" row) must not resurrect an untrackable variant.
RECEIVABLE_MIN_ROWS = 3
_SEARCH_CANDIDATE_NAMES = 40


@events_bp.get("/events/meta/items")
async def search_items():
    """Item-name autocomplete for the task form (session required).

    Tasks match drops by exact item name, so offering catalog-only variants
    (charged weapons, ornamented kits, …) creates tasks no drop can ever
    complete. Results are therefore restricted to names actually seen in the
    drop history (``player_item_hourly_totals``). If nothing matching the
    query has ever dropped — e.g. a brand-new boss item — the raw catalog
    matches are returned instead, flagged ``tracked: false`` so the picker
    can warn the configurator."""
    current_user_id()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])

    def _search():
        from db import ItemList, PlayerItemHourlyTotals

        with db_session() as s:
            # Stack/noted variants share a name — collapse to one row per name,
            # keeping every id so the receivable probe covers all variants.
            candidates = (
                s.query(ItemList.item_name,
                        func.min(ItemList.item_id),
                        func.group_concat(ItemList.item_id))
                .filter(ItemList.item_name.ilike(f"%{q}%"), ItemList.noted.is_(False))
                .group_by(ItemList.item_name)
                .order_by(func.length(ItemList.item_name), ItemList.item_name)
                .limit(_SEARCH_CANDIDATE_NAMES)
                .all()
            )
            tracked, untracked = [], []
            for name, min_id, ids_csv in candidates:
                ids = [int(x) for x in str(ids_csv or min_id).split(",")]
                # Indexed probe, LIMIT'd so common items never scan rollups.
                seen = (
                    s.query(PlayerItemHourlyTotals.item_id)
                    .filter(PlayerItemHourlyTotals.item_id.in_(ids))
                    .limit(RECEIVABLE_MIN_ROWS)
                    .all()
                )
                bucket = tracked if len(seen) >= RECEIVABLE_MIN_ROWS else untracked
                bucket.append({"id": min_id, "name": name})
                if len(tracked) >= 15:
                    break
            if tracked:
                return [{**e, "tracked": True} for e in tracked[:15]]
            return [{**e, "tracked": False} for e in untracked[:15]]

    return jsonify(await asyncio.to_thread(_search))


@events_bp.get("/events/meta/pets")
async def search_pets():
    """Pet-name autocomplete for the task form (session required).

    Names come from the pet taxonomy (``utils/osrs_pets``) so anything offered
    is guaranteed to validate as a pet; ids come from the item DB (every pet
    has a same-named inventory item) purely for the itemdb icons."""
    current_user_id()
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify([])

    def _search():
        from utils.osrs_pets import PET_DISPLAY_BY_NORM
        from db import ItemList

        names = sorted(
            {n for n in PET_DISPLAY_BY_NORM.values() if q in n.lower()},
            key=lambda n: (len(n), n),
        )[:15]
        if not names:
            return []
        with db_session() as s:
            rows = (
                s.query(func.min(ItemList.item_id), ItemList.item_name)
                .filter(ItemList.item_name.in_(names), ItemList.noted.is_(False))
                .group_by(ItemList.item_name)
                .all()
            )
        id_by_name = {n: int(i) for i, n in rows}
        # Every current pet resolves to an item row; skip any future outlier
        # rather than break the picker's id contract.
        return [{"id": id_by_name[n], "name": n} for n in names if n in id_by_name]

    return jsonify(await asyncio.to_thread(_search))


@events_bp.get("/events/meta/pet-categories")
async def pet_category_catalog():
    """Full pet taxonomy for the task builder (session required).

    Every category with its member pets, so the form can seed a customizable
    pet list from a category preset (and preview what a category covers).
    Names come from ``utils/osrs_pets``; ids from the item DB purely for the
    itemdb icons (``id`` is null for a pet with no same-named item row —
    unlike the autocomplete, a preset must never silently drop a pet)."""
    current_user_id()

    def _catalog():
        from utils.osrs_pets import PET_DISPLAY_BY_NORM, pet_categories, pets_in_category
        from db import ItemList

        names_by_cat = {
            cat: sorted(PET_DISPLAY_BY_NORM[n] for n in pets_in_category(cat))
            for cat in pet_categories()
        }
        all_names = sorted({n for names in names_by_cat.values() for n in names})
        with db_session() as s:
            rows = (
                s.query(func.min(ItemList.item_id), ItemList.item_name)
                .filter(ItemList.item_name.in_(all_names), ItemList.noted.is_(False))
                .group_by(ItemList.item_name)
                .all()
            )
        id_by_name = {n: int(i) for i, n in rows}
        return [
            {"key": cat, "pets": [{"id": id_by_name.get(n), "name": n} for n in names]}
            for cat, names in names_by_cat.items()
        ]

    return jsonify(await asyncio.to_thread(_catalog))


@events_bp.get("/events/meta/resolve")
async def resolve_meta_names():
    """Resolve exact item/NPC names to their game ids (session required).

    Batch lookup for the task form's selection chips: tasks store names only,
    so editing an existing task needs a name -> id pass to render the
    itemdb/npcdb icons. ``names`` is |-separated (names never contain pipes);
    unknown names are simply absent from the response."""
    current_user_id()
    kind = (request.args.get("kind") or "item").strip()
    if kind not in ("item", "npc"):
        abort_problem(422, "Invalid kind", "kind must be 'item' or 'npc'.")
    names = [n.strip() for n in (request.args.get("names") or "").split("|") if n.strip()]
    if not names:
        return jsonify([])
    names = names[:100]

    def _resolve():
        from db import ItemList, NpcList

        with db_session() as s:
            if kind == "item":
                rows = (
                    s.query(func.min(ItemList.item_id), ItemList.item_name)
                    .filter(ItemList.item_name.in_(names), ItemList.noted.is_(False))
                    .group_by(ItemList.item_name)
                    .all()
                )
            else:
                rows = (
                    s.query(func.min(NpcList.npc_id), NpcList.npc_name)
                    .filter(NpcList.npc_name.in_(names))
                    .group_by(NpcList.npc_name)
                    .all()
                )
            return [{"id": i, "name": n} for i, n in rows]

    return jsonify(await asyncio.to_thread(_resolve))


@events_bp.get("/events/meta/npcs")
async def search_npcs():
    """NPC-name autocomplete for the task form (session required)."""
    current_user_id()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])

    def _search():
        from db import NpcList
        from web_api.routes.npc_source_aliases import alias_search_entries

        with db_session() as s:
            # Multi-form bosses repeat a name (one row per form) — dedupe.
            rows = (
                s.query(func.min(NpcList.npc_id), NpcList.npc_name)
                .filter(NpcList.npc_name.ilike(f"%{q}%"))
                .group_by(NpcList.npc_name)
                .order_by(func.length(NpcList.npc_name), NpcList.npc_name)
                .limit(15)
                .all()
            )
        # Source aliases ("Wintertodt" for its reward containers) lead the
        # list — task validators expand them back to the real recorded names.
        entries = alias_search_entries(q)
        entries.extend({"id": i, "name": n} for i, n in rows)
        return entries[:15]

    return jsonify(await asyncio.to_thread(_search))


_NPC_DROPS_LIMIT = 200
_PLACEHOLDER_ITEM = re.compile(r"^Item \d+$")


@events_bp.get("/events/meta/npc-drops")
async def npc_drop_items():
    """Item names on one NPC's drop table (session required).

    Backs the task form's "import a boss's drops" helper: pick a boss, get its
    droppable items to bulk-add to an item list. Sources mirror the public
    drop-table page — the wiki table, then the boss family's table, then items
    actually observed dropping (the last-drops registry, which covers activity
    sources with no wiki rows at all, e.g. Wintertodt's reward containers).
    Each entry carries ``tracked`` (same semantics as /events/meta/items) so
    the picker can warn on items never seen in the drop history."""
    current_user_id()
    raw = (request.args.get("npc_id") or "").strip()
    if not raw.isdigit():
        abort_problem(422, "Invalid npc_id", "npc_id must be an integer.")
    npc_id = int(raw)

    def _load():
        # Lazy imports keep events.py's conftest ``services``-stub isolation
        # and reuse the npc page's table readers (wiki + family + observed).
        from db import ItemList, NpcList, PlayerItemHourlyTotals
        from web_api.routes.npcs import (
            _family_table_rows,
            _observed_table_rows,
            _wiki_table_rows,
        )

        with db_session() as s:
            npc = s.query(NpcList).filter(NpcList.npc_id == npc_id).first()
            if not npc:
                abort_problem(404, "Unknown NPC", f"No NPC with id {npc_id}.")
            rows = _wiki_table_rows(s, npc_id)
            if not rows:
                rows = _family_table_rows(s, npc_id, npc.npc_name)
            if not rows:
                rows, _last, _status, _build = _observed_table_rows(
                    s, npc_id, npc.npc_name
                )
            picked, seen = [], set()
            # Unnoted rows first so the per-name dedupe keeps the id whose
            # itemdb icon players recognise.
            for item_id, item_name, _qty, noted, _rarity, _rolls in sorted(
                rows, key=lambda r: bool(r[3])
            ):
                name = str(item_name or "").strip()
                key = name.lower()
                if not name or key in seen or _PLACEHOLDER_ITEM.match(name):
                    continue
                seen.add(key)
                picked.append((int(item_id), name))
                if len(picked) >= _NPC_DROPS_LIMIT:
                    break

            # One name -> every variant id, in a single pass. A drop lands on
            # whichever id the client sent, so probing only the id printed on
            # the wiki row called real items untracked: ToB's table lists Vial
            # of blood as 22405 (no rollup rows) while receipts sit on 22446.
            # /events/meta/items already probes the whole variant set, so
            # without this the same item was tracked in one picker and flagged
            # "never seen in tracked drops" in the other.
            variant_ids: dict[str, list[int]] = {}
            for iid, iname in (
                s.query(ItemList.item_id, ItemList.item_name)
                .filter(ItemList.item_name.in_([n for _, n in picked]))
                .all()
            ):
                variant_ids.setdefault(iname, []).append(int(iid))

            out = []
            for item_id, name in picked:
                # Indexed LIMIT'd probe, mirroring /events/meta/items: tasks
                # match by name, so flag names never seen in the drop history.
                observed = (
                    s.query(PlayerItemHourlyTotals.item_id)
                    .filter(
                        PlayerItemHourlyTotals.item_id.in_(
                            variant_ids.get(name) or [item_id]
                        )
                    )
                    .limit(RECEIVABLE_MIN_ROWS)
                    .all()
                )
                out.append({
                    "id": item_id,
                    "name": name,
                    "tracked": len(observed) >= RECEIVABLE_MIN_ROWS,
                })
            return out

    return jsonify(await asyncio.to_thread(_load))


@events_bp.get("/events/meta/item-sources")
async def item_sources():
    """NPC drop sources for one or more items (session required).

    Backs the task-form "restrict to specific NPC sources" picker: an item
    task can optionally require the item to drop from a chosen NPC. Sources
    come from the OSRS Wiki drop table we ingest (``xenforo.dt_npc_loot``, the
    same data the public item pages show), each flagged ``tracked`` so the
    picker can warn on NPCs we've never observed dropping. ``items`` is
    |-separated exact item names (names never contain pipes); unknown names are
    absent from the response."""
    current_user_id()
    names = [n.strip() for n in (request.args.get("items") or "").split("|") if n.strip()]
    if not names:
        return jsonify([])
    names = names[:50]

    def _load():
        # Lazy import keeps events.py's conftest ``services``-stub isolation and
        # reuses items.py's 1h in-process per-item cache for the source query.
        from web_api.routes.items import _sources
        from db import ItemList

        with db_session() as s:
            rows = (
                s.query(ItemList.item_id, ItemList.item_name, ItemList.noted)
                .filter(ItemList.item_name.in_(names))
                .all()
            )
        # Keep EVERY id a name maps to. Collapsing to one (this used to take
        # MIN(item_id) over the unnoted rows) asked the wiki/drop tables about
        # a variant id that holds no rows, so the picker offered a fraction of
        # the real sources — "Vial of blood" showed only ToB Hard Mode because
        # every receipt is recorded on 22446 while MIN picked 22405. `item_id`
        # stays the primary unnoted id (the response's identity field).
        ids_by_name: dict[str, list[int]] = {}
        primary: dict[str, int] = {}
        for iid, name, noted in rows:
            ids_by_name.setdefault(name, []).append(int(iid))
            if not noted:
                primary[name] = min(primary.get(name, int(iid)), int(iid))
        # _sources opens its own db_session, so call it after the resolve
        # session above has closed rather than nesting.
        return [
            {"item_name": n, "item_id": primary.get(n, min(ids)), **_sources(ids)}
            for n, ids in ids_by_name.items()
        ]

    return jsonify(await asyncio.to_thread(_load))


@events_bp.delete("/events/<int:event_id>/tasks/<int:task_id>")
async def delete_task(event_id: int, task_id: int):
    """Delete a task and everything that references it.

    ``web_event_bingo_cells.task_id``, ``web_event_completions.task_id`` and
    ``web_event_progress.task_id`` all FK this row with no cascade, so a bare
    delete trips an IntegrityError the moment the task is bound to a board —
    which is exactly what template instantiation produces (this used to make
    tasks on template-created events undeletable). Bound cells survive as
    labeled, unbound cells (rebindable in the designer, same as a skipped
    template task); the task's ledger/progress rows are removed and any points
    it granted are taken back from team scores.

    ``?retro=keep_scores`` (web68a, active events): the change-maker chose to
    let teams KEEP the points this task already granted — the score
    subtraction is skipped while every task-keyed child row is still removed
    (the FKs demand it, so the per-player breakdown for this task is gone
    either way). Default (``revoke``) is the full unwind."""
    user_id = current_user_id()
    retro = request.args.get("retro") or "revoke"
    if retro not in ("revoke", "keep_scores"):
        abort_problem(422, "Invalid retro",
                      "'retro' must be 'revoke' or 'keep_scores'.")

    def _apply():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev)
            _assert_event_not_past(ev)
            task = (
                s.query(EventTask)
                .filter(EventTask.id == task_id, EventTask.event_id == event_id)
                .first()
            )
            if not task:
                return
            task_label = task.label

            # Take back points the task granted: a completed (task, team)
            # rollup awarded task.points, and applied bonus rows written
            # against this task's id store their granted points in quantity.
            # keep_scores: record the would-be deltas for the audit row but
            # leave every team score standing.
            deltas: dict[int, int] = {}
            points = int(task.points or 0)
            if points:
                for p in (
                    s.query(EventProgress)
                    .filter(EventProgress.task_id == task_id,
                            EventProgress.completed.is_(True))
                    .all()
                ):
                    deltas[p.team_id] = deltas.get(p.team_id, 0) + points
            for c in (
                s.query(EventCompletion)
                .filter(EventCompletion.task_id == task_id,
                        EventCompletion.source_type == "bonus",
                        EventCompletion.status.in_(_APPLIED_STATUSES))
                .all()
            ):
                if c.team_id is not None:
                    deltas[c.team_id] = deltas.get(c.team_id, 0) + max(int(c.quantity or 0), 0)
            if retro != "keep_scores":
                for team_id_, delta in deltas.items():
                    team = s.query(EventTeam).filter(EventTeam.id == team_id_).first()
                    if team is not None:
                        team.score = max(round(float(team.score or 0) - delta, 2), 0)

            # Unbind the task's bingo cells — the labeled cell stays on the
            # board (rebindable), but its completions go with the binding.
            cell_ids = [
                cid for (cid,) in s.query(EventBingoCell.id)
                .filter(EventBingoCell.task_id == task_id)
                .all()
            ]
            if cell_ids:
                (s.query(EventBingoCompletion)
                 .filter(EventBingoCompletion.cell_id.in_(cell_ids))
                 .delete(synchronize_session=False))
                (s.query(EventBingoCell)
                 .filter(EventBingoCell.id.in_(cell_ids))
                 .update({EventBingoCell.task_id: None}, synchronize_session=False))

            (s.query(EventCompletion)
             .filter(EventCompletion.task_id == task_id)
             .delete(synchronize_session=False))
            (s.query(EventProgress)
             .filter(EventProgress.task_id == task_id)
             .delete(synchronize_session=False))

            # P0-5: task-keyed children the original cascade predates.
            # EventPlayerPoints.task_id is NOT NULL (would 500); board tile /
            # position pointers are nullable — unbind them so a pinned tile
            # falls back to a rest tile and a team's current-task pointer clears
            # (the engine reassigns on the next roll).
            from db import EventBoardPosition, EventBoardTile

            (s.query(EventPlayerPoints)
             .filter(EventPlayerPoints.task_id == task_id)
             .delete(synchronize_session=False))
            (s.query(EventBoardTile)
             .filter(EventBoardTile.task_id == task_id)
             .update({EventBoardTile.task_id: None}, synchronize_session=False))
            (s.query(EventBoardPosition)
             .filter(EventBoardPosition.current_task_id == task_id)
             .update({EventBoardPosition.current_task_id: None},
                     synchronize_session=False))
            s.delete(task)
            if retro != "keep_scores" and bool(ev.has_bingo):
                # Deleting this task's cell bindings can break lines that
                # OTHER tasks' bonuses were standing on — re-derive every
                # team's bonus set from the post-delete board (web68a; this
                # also fixes the pre-existing stale-line-bonus gap). Skipped
                # for keep_scores, which keeps granted points by definition.
                s.flush()
                from services.event_engine import reconcile_bingo_bonuses

                reconcile_bingo_bonuses(s, ev)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                event_id=ev.id,
                action="event.task.delete",
                target=f"web_events.{event_id}.task.{task_id}",
                before=f"label:{task_label}",
                after=json.dumps({"retro": retro, "score_deltas": deltas}),
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return jsonify({"ok": True})


@events_bp.post("/events/<int:event_id>/teams")
async def add_team(event_id: int):
    user_id = current_user_id()
    body = await json_body()
    name = (body.get("name") or "").strip()
    if not (1 <= len(name) <= 80):
        abort_problem(422, "Invalid name", "Team name must be 1–80 characters.")

    def _apply():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev)
            _assert_event_not_past(ev)
            # clan_vs_clan: every team belongs to an accepted participant clan.
            # Standard/global teams stay unbound (group_id NULL).
            team_group_id = None
            if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
                team_group_id = body.get("group_id")
                if not isinstance(team_group_id, int):
                    abort_problem(422, "Missing group_id",
                                  "Clan-vs-clan teams must name the clan they represent.")
                if team_group_id not in participating_group_ids(s, ev):
                    abort_problem(422, "Not an accepted participant",
                                  "That clan has not accepted this event.")
            team = EventTeam(event_id=event_id, name=name, score=0, group_id=team_group_id)
            s.add(team)
            s.flush()
            # Per-team Discord (web53a): seed the new team's desired rows when
            # the feature is configured (no-op otherwise).
            _sync_team_discord(s, ev)
            s.commit()
            return team.id

    team_id = await asyncio.to_thread(_apply)
    _bump(event_id)
    return jsonify({"id": team_id})


@events_bp.patch("/events/<int:event_id>/teams/<int:team_id>")
async def update_team(event_id: int, team_id: int):
    """Edit a team's cosmetics: rename (fix a typo) and/or set its accent
    color. Admin-only; audit-logged. The clan a clan_vs_clan team represents
    is fixed at create time. Allowed in any lifecycle state (cosmetic only).

    ``color`` is "#rrggbb", or null/"" to clear back to the palette default;
    ``name`` may be omitted to change the color alone."""
    user_id = current_user_id()
    body = await json_body()
    name = (body.get("name") or "").strip() if "name" in body else None
    if name is not None and not (1 <= len(name) <= 80):
        abort_problem(422, "Invalid name", "Team name must be 1–80 characters.")
    has_color = "color" in body
    color = body.get("color")
    if has_color and color is not None:
        color = str(color).strip().lower() or None
        if color is not None and not re.fullmatch(r"#[0-9a-f]{6}", color):
            abort_problem(422, "Invalid color", 'Team color must be "#rrggbb" hex (or null to reset).')
    # Board-game game piece (web44a): an OSRS item id rendered via
    # /img/itemdb/{id}.png. Null clears back to the color-dot default.
    has_piece = "piece_item_id" in body
    piece_item_id = body.get("piece_item_id")
    if has_piece and piece_item_id is not None and (
            not isinstance(piece_item_id, int) or isinstance(piece_item_id, bool)
            or piece_item_id <= 0):
        abort_problem(422, "Invalid piece",
                      "'piece_item_id' must be a positive item id (or null to clear).")
    if name is None and not has_color and not has_piece:
        abort_problem(422, "Nothing to update", "Provide a name, color, and/or piece.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            team = (
                s.query(EventTeam)
                .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                .first()
            )
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")
            before = {"name": team.name, "color": team.color,
                      "piece_item_id": getattr(team, "piece_item_id", None)}
            if name is not None:
                team.name = name
            if has_color:
                team.color = color
            if has_piece:
                team.piece_item_id = piece_item_id
            after = {"name": team.name, "color": team.color,
                     "piece_item_id": getattr(team, "piece_item_id", None)}
            if before == after:
                return  # no-op
            if before["name"] != after["name"] or before["color"] != after["color"]:
                # Re-pend the team's Discord rows so the bot renames/recolors
                # the auto-created role + channel (web53a; no-op when off).
                _sync_team_discord(s, ev)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
                    event_id=ev.id,
                    action="event.team.update",
                    target=f"web_events.{event_id}.team.{team_id}",
                    before=json.dumps(before),
                    after=json.dumps(after),
                )
            )
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return jsonify({"ok": True})


@events_bp.delete("/events/<int:event_id>/teams/<int:team_id>")
async def delete_team(event_id: int, team_id: int):
    """Delete a mistakenly-created team and everything scoped to it — its
    roster, progress rollups, completion-ledger rows and bingo completions —
    so no orphaned rows reference the removed team. Blocked once the event is
    over (its history is then read-only, like the rest of the roster).
    Admin-only; audit-logged."""
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            team = (
                s.query(EventTeam)
                .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                .first()
            )
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")
            team_name = team.name

            # Per-team Discord (web53a): the rows are about to be FK-wiped —
            # queue any live role/channel for bot teardown first, then drop
            # the rows with the other children below.
            _drop_team_discord_rows(s, event_id, team_id)

            # No ORM cascade is configured on these FKs, so clear the children
            # first — EventProgress.team_id is NOT NULL, so a dangling row would
            # violate the constraint. Deleting the team's ledger/progress is
            # correct: standings recompute from the remaining teams.
            s.query(EventBingoCompletion).filter(
                EventBingoCompletion.team_id == team_id
            ).delete(synchronize_session=False)
            s.query(EventCompletion).filter(
                EventCompletion.team_id == team_id
            ).delete(synchronize_session=False)
            s.query(EventProgress).filter(
                EventProgress.team_id == team_id
            ).delete(synchronize_session=False)
            s.query(EventTeamMember).filter(
                EventTeamMember.team_id == team_id
            ).delete(synchronize_session=False)

            # web71a: buy-ins are NOT wiped with the team — that GP was
            # contributed to the *event* and may already be paid. Return the
            # rows to the unassigned bucket (which also clears the FK that
            # would otherwise block this delete outright).
            from services.event_buyins import release_team_buyins

            release_team_buyins(s, event_id, team_id)

            # P0-5: points/vote + board-game children whose FKs (web45a–web48a)
            # postdate the original 4-table cascade. Several are NOT NULL
            # (board position, inventory, cooldown, coin ledger,
            # effect.source_team_id), so without these the delete raised an
            # opaque IntegrityError 500 on every pointed or board-game event —
            # board positions are seeded at activation, so their teams were
            # simply undeletable.
            from db import (
                EventBoardEffect,
                EventBoardPosition,
                EventCoinLedger,
                EventTeamCooldown,
                EventTeamInventory,
            )

            for _child in (
                EventPlayerPoints,
                EventLeaderVote,
                EventBoardPosition,
                EventTeamInventory,
                EventTeamCooldown,
                EventCoinLedger,
            ):
                s.query(_child).filter(_child.team_id == team_id).delete(
                    synchronize_session=False
                )
            # Effects reference a team from either side (source_team_id NOT NULL).
            s.query(EventBoardEffect).filter(
                sa_or(
                    EventBoardEffect.source_team_id == team_id,
                    EventBoardEffect.target_team_id == team_id,
                )
            ).delete(synchronize_session=False)
            s.delete(team)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
                    event_id=ev.id,
                    action="event.team.delete",
                    target=f"web_events.{event_id}.team.{team_id}",
                    before=f"name:{team_name}",
                    after=None,
                )
            )
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Team membership (Task 16, PRD D4/D10)
# --------------------------------------------------------------------------- #
def _bump(event_id: int | None = None) -> None:
    """Nudge the event consumer to refresh its matcher state right away
    (services.event_engine subscribes to the admin-bump channel). Safe no-op
    on any Redis problem."""
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(event_id)
    except Exception:
        pass


def _sync_event_guilds(s, ev: Event) -> None:
    """Write the desired ``web_event_guilds`` rows for ``ev`` — pure DB, the
    caller owns the commit. The core bot mirrors the rows onto real Discord
    scheduled events (services/event_scheduled_events.py); the Web API never
    talks to Discord."""
    # Lazy service import (pytest conftest stubs `services`).
    from services.event_scheduled_events import sync_event_guilds

    sync_event_guilds(s, ev)


def _load_event_or_404(s, event_id: int) -> Event:
    ev = s.query(Event).filter(Event.id == event_id).first()
    if not ev:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    return ev


def _assert_roster_open(ev: Event) -> None:
    """Roster changes are allowed while draft or active; blocked once past."""
    if _effective_status(ev) == "past":
        abort_problem(409, "Event is over", "The roster can no longer be changed.")


def _load_owned_player(s, player_id, user_id: int) -> Player:
    """The player must exist and be linked to the session user."""
    if not isinstance(player_id, int):
        abort_problem(422, "Invalid player_id", "'player_id' must be an integer.")
    player = s.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        abort_problem(404, "Player not found", f"No player {player_id}.")
    if player.user_id != user_id:
        abort_problem(403, "Forbidden", "That player is not linked to your account.")
    return player


def _assert_player_eligible(s, ev: Event, player_id: int) -> None:
    """Group events: the player must be a member of a participating group —
    for standard events that is exactly the event's one group. Global events
    (no participating groups): any player is eligible."""
    gids = participating_group_ids(s, ev)
    if not gids:
        return
    in_group = (
        s.query(user_group_association.c.id)
        .filter(
            user_group_association.c.player_id == player_id,
            user_group_association.c.group_id.in_(gids),
        )
        .first()
    )
    if not in_group:
        abort_problem(403, "Not a group member",
                      "That player is not a member of a participating clan.")


def _event_membership(s, event_id: int, player_id: int) -> EventTeamMember | None:
    """The player's membership row anywhere on this event (one team per
    player per event)."""
    return (
        s.query(EventTeamMember)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == event_id, EventTeamMember.player_id == player_id)
        .first()
    )


def _signup_row(s, event_id: int, player_id: int) -> EventSignup | None:
    return (
        s.query(EventSignup)
        .filter(EventSignup.event_id == event_id, EventSignup.player_id == player_id)
        .first()
    )


def _signup_group_for_player(s, ev, player_id: int) -> int | None:
    """Which clan a player signs up under: their participating clan for
    clan_vs_clan, else the event's group (None for global)."""
    if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
        overlap = (
            s.query(user_group_association.c.group_id)
            .filter(
                user_group_association.c.player_id == player_id,
                user_group_association.c.group_id.in_(participating_group_ids(s, ev)),
            )
            .first()
        )
        return overlap[0] if overlap else None
    return ev.group_id


def _assert_single_participating_clan(s, ev, player_id: int) -> None:
    """clan_vs_clan (G7): a player who belongs to MORE THAN ONE participating
    clan can't self-place — the system can't tell which side they're on. Block
    self sign-up (every self-service mode) and route them to a manual admin team
    add. No-op for standard/global events and single-clan players. One query
    (accepted-clan membership count) so the self-service path stays cheap."""
    if (getattr(ev, "mode", None) or "standard") != "clan_vs_clan":
        return
    n = (
        s.query(user_group_association.c.group_id)
        .join(EventGroup, EventGroup.group_id == user_group_association.c.group_id)
        .filter(
            user_group_association.c.player_id == player_id,
            EventGroup.event_id == ev.id,
            EventGroup.status == "accepted",
        )
        .distinct()
        .count()
    )
    if n > 1:
        abort_problem(
            409, "Multiple clans",
            "You're a member of more than one clan taking part in this event, so "
            "we can't add you to a team automatically — we don't know which side "
            "you're on. Ask a leader of the clan you want to play for to add you "
            "to one of their teams.",
        )


def _user_other_entry(s, ev, event_id: int, user_id: int, player_id: int) -> int | None:
    """A DIFFERENT account of ``user_id`` already signed up or placed on this
    event (one RSN per person). Returns that player_id, or None."""
    other_pids = [
        pid for (pid,) in
        s.query(Player.player_id).filter(Player.user_id == user_id).all()
        if pid != player_id
    ]
    if not other_pids:
        return None
    row = (
        s.query(EventSignup.player_id)
        .filter(EventSignup.event_id == event_id, EventSignup.player_id.in_(other_pids))
        .first()
    )
    if row:
        return row[0]
    row = (
        s.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == event_id, EventTeamMember.player_id.in_(other_pids))
        .first()
    )
    return row[0] if row else None


@events_bp.post("/events/<int:event_id>/join")
async def join_event(event_id: int):
    """Player-facing join. Behavior depends on the event's formation mode
    (PRD D4): self_join picks a team (join code enforced when set),
    auto_assign balances onto the smallest team, admin_assign refuses."""
    user_id = current_user_id()
    body = await json_body()
    player_id = body.get("player_id")
    req_team_id = body.get("team_id")
    join_code = body.get("join_code")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_roster_open(ev)
            # Self sign-ups shut when the event begins unless it allows late
            # ones (web70a); admin placement is unaffected (roster_open above).
            _assert_signups_open(ev)
            _load_owned_player(s, player_id, user_id)
            _assert_player_eligible(s, ev, player_id)

            mode = ev.formation_mode or "admin_assign"
            if mode == "admin_assign":
                abort_problem(
                    403, "Admin-assigned event", "Only event admins can place players on teams."
                )
            if mode == "self_join" and ev.join_code:
                if not isinstance(join_code, str) or join_code.strip() != ev.join_code:
                    abort_problem(403, "Join code required", "The join code is missing or wrong.")

            # G7: a player in several participating clans can't self-place.
            _assert_single_participating_clan(s, ev, player_id)

            if _event_membership(s, event_id, player_id):
                abort_problem(409, "Already joined", "That player is already on a team in this event.")

            # One RSN per person on the new self-service surfaces (clan-vs-clan
            # and the sign-up pool). Legacy standard self_join/auto_assign events
            # keep their prior behavior untouched (no extra query, no new 409).
            if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan" or mode == "signup_pool":
                other = _user_other_entry(s, ev, event_id, user_id, player_id)
                if other is not None:
                    abort_problem(
                        409, "Already signed up",
                        "You've already entered this event with another account. "
                        "Only one account per person can take part.",
                    )

            # Sign-up pool: record the opt-in with NO team; admins sort the pool
            # into teams later. Teams need not exist yet.
            if mode == "signup_pool":
                if _signup_row(s, event_id, player_id) is None:
                    s.add(EventSignup(
                        event_id=event_id, player_id=player_id,
                        group_id=_signup_group_for_player(s, ev, player_id),
                        user_id=user_id, source="web",
                    ))
                    s.commit()
                return {"team_id": None, "pooled": True}

            teams = (
                s.query(EventTeam)
                .filter(EventTeam.event_id == event_id)
                .order_by(EventTeam.id.asc())
                .all()
            )
            if not teams:
                abort_problem(404, "No teams", "This event has no teams to join yet.")

            if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
                # A member can only land on their own clan's team(s): both the
                # self_join choice and the auto_assign balancing below operate
                # on this filtered list.
                my_gids = {
                    gid for (gid,) in
                    s.query(user_group_association.c.group_id)
                    .filter(user_group_association.c.player_id == player_id)
                    .all()
                }
                teams = [t for t in teams if t.group_id and t.group_id in my_gids]
                if not teams:
                    abort_problem(404, "No teams", "Your clan has no team on this event yet.")

            if mode == "self_join":
                if req_team_id is None and len(teams) == 1:
                    team = teams[0]
                else:
                    if not isinstance(req_team_id, int):
                        abort_problem(422, "Missing team_id", "'team_id' is required to join this event.")
                    team = next((t for t in teams if t.id == req_team_id), None)
                    if not team:
                        abort_problem(404, "Team not found", f"No team {req_team_id} in this event.")
            else:  # auto_assign — server places the player; no team choice.
                if req_team_id is not None:
                    abort_problem(
                        422, "No team choice", "This event assigns teams automatically."
                    )
                counts = dict(
                    s.query(EventTeamMember.team_id, func.count(EventTeamMember.player_id))
                    .filter(EventTeamMember.team_id.in_([t.id for t in teams]))
                    .group_by(EventTeamMember.team_id)
                    .all()
                )
                # Fewest members; ties -> lowest team id (teams already id-asc).
                team = min(teams, key=lambda t: (counts.get(t.id, 0), t.id))

            # joined_at (default now()) is the credit cutoff (PRD D10).
            s.add(EventTeamMember(team_id=team.id, player_id=player_id,
                                  event_id=event_id))
            # web71a: a buy-in paid at sign-up follows the player onto the team.
            _sync_buyin_team(s, event_id, player_id, team.id)
            # Per-team Discord (web53a): let the bot pick up the roster change.
            _mark_team_discord_dirty(s, event_id, team.id)
            try:
                s.commit()
            except IntegrityError:
                # web59a backstop: a concurrent join (double click / retry)
                # won the race — that's a success from the player's view.
                s.rollback()
                abort_problem(
                    409, "Already on a team",
                    "You're already on a team for this event — refresh to "
                    "see your placement.")
            return team.id

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    if isinstance(result, dict):
        return private_no_store(jsonify(result))
    return private_no_store(jsonify({"team_id": result}))


@events_bp.post("/events/<int:event_id>/leave")
async def leave_event(event_id: int):
    """Player-facing leave: deletes the membership row. Existing ledger rows
    and progress are untouched (history stands)."""
    user_id = current_user_id()
    body = await json_body()
    player_id = body.get("player_id")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_roster_open(ev)
            _load_owned_player(s, player_id, user_id)
            membership = _event_membership(s, event_id, player_id)
            if membership:
                s.delete(membership)
                # A departed member neither votes nor stands in the election.
                (s.query(EventLeaderVote)
                 .filter(EventLeaderVote.event_id == event_id,
                         sa_or(EventLeaderVote.voter_player_id == player_id,
                               EventLeaderVote.candidate_player_id == player_id))
                 .delete(synchronize_session=False))
            # Also withdraw a sign-up-pool opt-in. Only signup_pool events ever
            # write signup rows, so other modes stay byte-for-byte (no query).
            signup = None
            if (ev.formation_mode or "admin_assign") == "signup_pool":
                signup = _signup_row(s, event_id, player_id)
                if signup:
                    s.delete(signup)
            if membership or signup:
                if membership:
                    # web71a: off the roster -> their buy-in returns to the
                    # unassigned bucket (the GP stays in the pot; it just stops
                    # crediting a team they left).
                    _sync_buyin_team(s, event_id, player_id, None)
                    _mark_team_discord_dirty(s, event_id, membership.team_id)
                s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


@events_bp.post("/events/<int:event_id>/teams/<int:team_id>/members")
async def admin_add_member(event_id: int, team_id: int):
    """Admin roster add — works in every formation mode and may move a player
    between teams (delete+insert: ``joined_at`` resets on move, so the credit
    cutoff restarts on the new team). Audit-logged."""
    user_id = current_user_id()
    body = await json_body()
    player_id = body.get("player_id")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            team = (
                s.query(EventTeam)
                .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                .first()
            )
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")
            if not isinstance(player_id, int):
                abort_problem(422, "Invalid player_id", "'player_id' must be an integer.")
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if not player:
                abort_problem(404, "Player not found", f"No player {player_id}.")
            _assert_player_eligible(s, ev, player_id)
            team_gid = getattr(team, "group_id", None)
            if team_gid:
                # Clan-bound team: membership of THAT clan specifically, not
                # just any participating clan.
                in_clan = (
                    s.query(user_group_association.c.id)
                    .filter(
                        user_group_association.c.player_id == player_id,
                        user_group_association.c.group_id == team_gid,
                    )
                    .first()
                )
                if not in_clan:
                    abort_problem(403, "Wrong clan",
                                  "That player is not a member of the clan this team represents.")

            existing = _event_membership(s, event_id, player_id)
            before = f"team:{existing.team_id}" if existing else None
            if existing:
                if existing.team_id == team_id:
                    return  # already on this team — no-op
                s.delete(existing)  # move: joined_at resets on the new row
                # Votes don't cross teams: drop the mover's vote and any
                # votes cast for them on the old team.
                (s.query(EventLeaderVote)
                 .filter(EventLeaderVote.event_id == event_id,
                         sa_or(EventLeaderVote.voter_player_id == player_id,
                               EventLeaderVote.candidate_player_id == player_id))
                 .delete(synchronize_session=False))
                s.flush()
            s.add(EventTeamMember(team_id=team_id, player_id=player_id,
                                  event_id=event_id))
            # web71a: carry a sign-up-time buy-in onto the team the draft (or
            # this move) just put them on.
            _sync_buyin_team(s, event_id, player_id, team_id)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
                    event_id=ev.id,
                    action="event.member.add",
                    target=f"web_events.{event_id}.player.{player_id}",
                    before=before,
                    after=f"team:{team_id}",
                )
            )
            _mark_team_discord_dirty(s, event_id, team_id)
            try:
                s.commit()
            except IntegrityError:
                # web59a backstop: a concurrent add/join placed them first.
                s.rollback()
                abort_problem(
                    409, "Already placed",
                    "That player was just placed on a team by another "
                    "action — refresh the roster to see where.")

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


MAX_BULK_ADD_NAMES = 200


@events_bp.post("/events/<int:event_id>/teams/<int:team_id>/members/bulk")
async def admin_add_members_bulk(event_id: int, team_id: int):
    """Admin roster add, list form ("paste your team"): resolves a list of
    RSNs case-insensitively and places every tracked, eligible player on the
    team in one call. Unlike the single-add route this never MOVES a player —
    anyone already placed on a team in this event comes back as skipped, so a
    pasted list can't silently reshuffle rosters. Per-name outcomes are
    returned so the UI can show exactly what happened. Audit-logged once."""
    user_id = current_user_id()
    body = await json_body()
    names = body.get("names")
    if not isinstance(names, list) or not names:
        abort_problem(422, "Invalid names", "'names' must be a non-empty list of player names.")
    if len(names) > MAX_BULK_ADD_NAMES:
        abort_problem(422, "Too many names",
                      f"At most {MAX_BULK_ADD_NAMES} names per request.")
    cleaned: list[str] = []
    seen_keys: set[str] = set()
    for raw in names:
        if not isinstance(raw, str):
            abort_problem(422, "Invalid names", "'names' must contain only strings.")
        name = raw.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cleaned.append(name)
    if not cleaned:
        abort_problem(422, "Invalid names", "No usable names in the list.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            team = (
                s.query(EventTeam)
                .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                .first()
            )
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")

            # Resolve the whole list in one query; keep the DB's canonical
            # capitalization for the response.
            rows = (
                s.query(Player.player_id, Player.player_name)
                .filter(func.lower(Player.player_name).in_(list(seen_keys)))
                .all()
            )
            by_key: dict[str, tuple[int, str]] = {}
            for pid, pname in rows:
                by_key.setdefault((pname or "").lower(), (pid, pname))
            resolved_ids = [pid for pid, _ in by_key.values()]

            # Eligibility mirrors admin_add_member: membership of the team's
            # own clan when the team is clan-bound, else any participating
            # clan. Global events (no participating groups) accept any
            # tracked player.
            gids = participating_group_ids(s, ev)
            team_gid = getattr(team, "group_id", None)
            check_gids = {team_gid} if team_gid else gids
            eligible_ids: set[int] = set(resolved_ids)
            if check_gids and resolved_ids:
                eligible_ids = {
                    pid for (pid,) in (
                        s.query(user_group_association.c.player_id)
                        .filter(
                            user_group_association.c.player_id.in_(resolved_ids),
                            user_group_association.c.group_id.in_(list(check_gids)),
                        )
                        .all()
                    )
                }

            # Existing placements anywhere on this event (one team per player).
            placed: dict[int, int] = {}
            if resolved_ids:
                placed = dict(
                    s.query(EventTeamMember.player_id, EventTeamMember.team_id)
                    .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                    .filter(
                        EventTeam.event_id == event_id,
                        EventTeamMember.player_id.in_(resolved_ids),
                    )
                    .all()
                )

            added: list[dict] = []
            skipped: list[dict] = []
            for name in cleaned:
                hit = by_key.get(name.lower())
                if not hit:
                    skipped.append({"name": name, "reason": "No tracked player by that name."})
                    continue
                pid, canonical = hit
                if pid not in eligible_ids:
                    skipped.append({
                        "name": canonical,
                        "reason": (
                            "Not a member of the clan this team represents."
                            if team_gid
                            else "Not a member of a participating clan."
                        ),
                    })
                    continue
                prior = placed.get(pid)
                if prior is not None:
                    skipped.append({
                        "name": canonical,
                        "reason": (
                            "Already on this team."
                            if prior == team_id
                            else "Already on another team in this event."
                        ),
                    })
                    continue
                s.add(EventTeamMember(team_id=team_id, player_id=pid,
                                      event_id=event_id))
                placed[pid] = team_id
                added.append({"id": pid, "name": canonical})

            if added:
                # web71a: one UPDATE for the whole batch's sign-up buy-ins.
                from services.event_buyins import sync_buyin_teams

                sync_buyin_teams(
                    s, event_id, {a["id"]: team_id for a in added}
                )
                s.add(
                    AuditLog(
                        actor_user_id=user_id,
                        group_id=ev.group_id,
                        event_id=ev.id,
                        action="event.member.bulk_add",
                        target=f"web_events.{event_id}.team.{team_id}",
                        before=None,
                        after=f"added:{len(added)}",
                    )
                )
                _mark_team_discord_dirty(s, event_id, team_id)
                try:
                    s.commit()
                except IntegrityError:
                    # web59a backstop: someone placed one of these players
                    # concurrently. The whole batch rolls back (atomic);
                    # re-running skips the now-placed names.
                    s.rollback()
                    abort_problem(
                        409, "Roster changed underneath you",
                        "A player in this list was just placed on a team by "
                        "another action. Re-run the paste — already-placed "
                        "names will be skipped.")
            return {"added": added, "skipped": skipped}

    result = await asyncio.to_thread(_apply)
    if result["added"]:
        _bump(event_id)
    return private_no_store(jsonify(result))


@events_bp.delete("/events/<int:event_id>/teams/<int:team_id>/members/<int:player_id>")
async def admin_remove_member(event_id: int, team_id: int, player_id: int):
    """Admin roster remove. Ledger rows and progress are untouched (history
    stands; revocation is a separate admin action). Audit-logged."""
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            team = (
                s.query(EventTeam)
                .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                .first()
            )
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")
            membership = (
                s.query(EventTeamMember)
                .filter(
                    EventTeamMember.team_id == team_id,
                    EventTeamMember.player_id == player_id,
                )
                .first()
            )
            if membership:
                s.delete(membership)
                # A departed member neither votes nor stands in the election.
                (s.query(EventLeaderVote)
                 .filter(EventLeaderVote.event_id == event_id,
                         sa_or(EventLeaderVote.voter_player_id == player_id,
                               EventLeaderVote.candidate_player_id == player_id))
                 .delete(synchronize_session=False))
                s.add(
                    AuditLog(
                        actor_user_id=user_id,
                        group_id=ev.group_id,
                        event_id=ev.id,
                        action="event.member.remove",
                        target=f"web_events.{event_id}.player.{player_id}",
                        before=f"team:{team_id}",
                        after=None,
                    )
                )
                # web71a: their buy-in returns to the unassigned bucket rather
                # than crediting a team they're no longer on.
                _sync_buyin_team(s, event_id, player_id, None)
                _mark_team_discord_dirty(s, event_id, team_id)
                s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Team leadership (web48a): leader / co-leader roles + elections
# --------------------------------------------------------------------------- #
def _load_team_or_404(s, event_id: int, team_id: int) -> EventTeam:
    team = (s.query(EventTeam)
            .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
            .first())
    if not team:
        abort_problem(404, "Team not found", f"No team {team_id} in this event.")
    return team


@events_bp.put("/events/<int:event_id>/teams/<int:team_id>/leadership")
async def set_team_leadership(event_id: int, team_id: int):
    """Assign a team's leader or co-leader. Event admins may assign either
    role; a team's LEADER may appoint their own co-leader (that's the point
    of executive authority). One holder per role — assigning demotes the
    previous holder to plain member."""
    user_id = current_user_id()
    body = await json_body()
    player_id = body.get("player_id")
    role = body.get("role")

    def _apply():
        from web_api.event_leadership import set_team_role, team_role_for_user

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            config = _leadership(ev)
            if not config["enabled"]:
                abort_problem(409, "Leadership disabled",
                              "This event does not use team leaders.")
            if role not in EVENT_TEAM_ROLES:
                abort_problem(422, "Invalid role",
                              f"role must be one of {list(EVENT_TEAM_ROLES)}.")
            if role == "co_leader" and not config["co_leaders"]:
                abort_problem(409, "Co-leaders disabled",
                              "This event does not use co-leaders.")
            if not isinstance(player_id, int):
                abort_problem(422, "Invalid player_id", "'player_id' must be an integer.")
            _load_team_or_404(s, event_id, team_id)
            if not _is_event_admin(s, user_id, ev):
                if not (role == "co_leader"
                        and team_role_for_user(s, team_id, user_id) == "leader"):
                    abort_problem(403, "Not allowed",
                                  "Only event admins (or the team's leader, for a "
                                  "co-leader) can assign leadership.")
            if not set_team_role(s, team_id, player_id, role):
                abort_problem(404, "Not a member",
                              "That player is not on this team.")
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.leadership.assign",
                target=f"web_events.{event_id}.team.{team_id}",
                before=None, after=f"{role}:{player_id}",
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


@events_bp.delete("/events/<int:event_id>/teams/<int:team_id>/leadership/<int:player_id>")
async def clear_team_leadership(event_id: int, team_id: int, player_id: int):
    """Remove a leadership role. Event admins always may; a team leader may
    demote their co-leader; and anyone may step down from a role held by a
    player they own."""
    user_id = current_user_id()

    def _apply():
        from web_api.event_leadership import team_role_for_user

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _load_team_or_404(s, event_id, team_id)
            member = (s.query(EventTeamMember)
                      .filter(EventTeamMember.team_id == team_id,
                              EventTeamMember.player_id == player_id)
                      .first())
            if member is None or not member.role:
                abort_problem(404, "No role", "That player holds no leadership role.")
            owns_player = bool(
                s.query(Player.player_id)
                .filter(Player.player_id == player_id, Player.user_id == user_id)
                .first()
            )
            allowed = (
                _is_event_admin(s, user_id, ev)
                or owns_player
                or (member.role == "co_leader"
                    and team_role_for_user(s, team_id, user_id) == "leader")
            )
            if not allowed:
                abort_problem(403, "Not allowed",
                              "Only event admins, the team leader (for a co-leader), "
                              "or the role holder can remove this role.")
            before = f"{member.role}:{player_id}"
            member.role = None
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.leadership.remove",
                target=f"web_events.{event_id}.team.{team_id}",
                before=before, after=None,
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


@events_bp.post("/events/<int:event_id>/teams/<int:team_id>/leader-vote")
async def cast_leader_vote(event_id: int, team_id: int):
    """Cast (or change) the viewer's vote for their team's leader. Election
    mode only; one live vote per voter; a strict plurality promotes the
    winner immediately."""
    user_id = current_user_id()
    body = await json_body()
    candidate_id = body.get("candidate_player_id")

    def _apply():
        from web_api.event_leadership import apply_election

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            config = _leadership(ev)
            if not config["enabled"] or config["selection"] != "election":
                abort_problem(409, "No election",
                              "This event does not elect team leaders.")
            if _effective_status(ev) == "past":
                abort_problem(409, "Event over", "The event has ended.")
            _load_team_or_404(s, event_id, team_id)
            if not isinstance(candidate_id, int):
                abort_problem(422, "Invalid candidate",
                              "'candidate_player_id' must be an integer.")
            my_pids = [
                pid for (pid,) in
                s.query(Player.player_id).filter(Player.user_id == user_id).all()
            ]
            voter = (s.query(EventTeamMember)
                     .filter(EventTeamMember.team_id == team_id,
                             EventTeamMember.player_id.in_(my_pids or [-1]))
                     .first())
            if voter is None:
                abort_problem(403, "Not on this team",
                              "Only members of the team can vote for its leader.")
            candidate = (s.query(EventTeamMember)
                         .filter(EventTeamMember.team_id == team_id,
                                 EventTeamMember.player_id == candidate_id)
                         .first())
            if candidate is None:
                abort_problem(404, "Not a member",
                              "The candidate is not on this team.")
            vote = (s.query(EventLeaderVote)
                    .filter(EventLeaderVote.event_id == event_id,
                            EventLeaderVote.voter_player_id == voter.player_id)
                    .first())
            if vote is None:
                s.add(EventLeaderVote(
                    event_id=event_id, team_id=team_id,
                    voter_player_id=voter.player_id,
                    candidate_player_id=candidate_id))
            else:
                vote.team_id = team_id
                vote.candidate_player_id = candidate_id
            s.flush()
            leader_id = apply_election(s, event_id, team_id)
            s.commit()
            return {"ok": True, "leader_player_id": leader_id}

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Sign-up pool (formation_mode == "signup_pool") — admin sort & randomize
# --------------------------------------------------------------------------- #
@events_bp.get("/events/<int:event_id>/signups")
async def list_event_signups(event_id: int):
    """The sign-up pool for an event: everyone who opted in, with each
    player's current team placement (null while unassigned). Event admins."""
    user_id = current_user_id()

    def _load():
        from services.event_signup import list_pool

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            rows = list_pool(s, ev)
        # Enrich with each player's current-month loot from Redis (batched,
        # outside the DB session so no connection is held during Redis I/O).
        # Pairs with the EHB / total-level joined by list_pool so admins can
        # gauge a signed-up player's ability at a glance. Emitted as the
        # standard `{value, value_formatted}` money envelope like every other
        # loot figure in the contract.
        totals = player_month_totals([r["player_id"] for r in rows])
        for r in rows:
            r["monthly_loot"] = money(totals.get(r["player_id"], 0))
        return rows

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@events_bp.post("/events/<int:event_id>/signups/assign")
async def assign_signup(event_id: int):
    """Place one signed-up player onto a team (admin manual sort)."""
    user_id = current_user_id()
    body = await json_body()
    player_id = body.get("player_id")
    team_id = body.get("team_id")
    if not isinstance(player_id, int) or not isinstance(team_id, int):
        abort_problem(422, "Invalid body", "'player_id' and 'team_id' must be integers.")

    def _apply():
        from services.event_signup import SignupError, assign_from_pool

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            try:
                assign_from_pool(s, ev, player_id, team_id)
            except SignupError as exc:
                abort_problem(exc.status, exc.title, exc.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.signup.assign",
                target=f"web_events.{event_id}.player.{player_id}",
                before=None, after=f"team:{team_id}",
            ))
            _mark_team_discord_dirty(s, event_id, team_id)
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


@events_bp.post("/events/<int:event_id>/signups/unassign")
async def unassign_signup(event_id: int):
    """Move a signed-up player back to the pool: drop their team placement but
    keep the sign-up (undo a mis-assignment without withdrawing them)."""
    user_id = current_user_id()
    body = await json_body()
    player_id = body.get("player_id")
    if not isinstance(player_id, int):
        abort_problem(422, "Invalid body", "'player_id' must be an integer.")

    def _apply():
        from services.event_signup import SignupError, unassign_from_pool

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            try:
                unassign_from_pool(s, ev, player_id)
            except SignupError as exc:
                abort_problem(exc.status, exc.title, exc.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.signup.unassign",
                target=f"web_events.{event_id}.player.{player_id}",
                before=None, after="team:none",
            ))
            _mark_team_discord_dirty(s, event_id)
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


@events_bp.post("/events/<int:event_id>/signups/randomize")
async def randomize_signups(event_id: int):
    """Randomly distribute the sign-up pool across teams (balanced; clan-aware).
    Repeatable — each call reshuffles everyone. Optional body ``{group_id}``
    re-rolls just one clan (clan-vs-clan)."""
    user_id = current_user_id()
    body = await json_body(required=False)
    group_id = (body or {}).get("group_id")
    if group_id is not None and not isinstance(group_id, int):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer or omitted.")

    def _apply():
        from services.event_signup import randomize_pool

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            result = randomize_pool(s, ev, group_id=group_id)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.signup.randomize",
                target=f"web_events.{event_id}",
                before=None, after=f"assigned:{result['assigned']}",
            ))
            _mark_team_discord_dirty(s, event_id)
            s.commit()
            return result

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))


@events_bp.post("/events/<int:event_id>/populate-random")
async def populate_random_members(event_id: int):
    """Admin-only scale/testing tool: bulk-fill this event's teams with random
    ACTIVE members, balanced across teams (clan-aware). Body:
    ``{ source: "group"|"global", count?: int }``. ``group`` draws from the
    event's linked group(s); ``global`` draws from every active player (only
    meaningfully different for global events — group/clan events can only place
    their own members). Returns a per-team summary. Audit-logged."""
    user_id = current_user_id()
    body = await json_body()
    source = body.get("source")
    count = body.get("count")
    if source not in ("group", "global"):
        abort_problem(422, "Invalid source", "'source' must be 'group' or 'global'.")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count <= 0):
        abort_problem(422, "Invalid count", "'count' must be a positive integer.")

    def _apply():
        from services.event_signup import populate_random, SignupError

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            try:
                result = populate_random(s, ev, source=source, count=count)
            except SignupError as e:
                abort_problem(e.status, e.title, e.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.populate_random",
                target=f"web_events.{event_id}",
                before=None, after=f"source:{source} added:{result['added']}",
            ))
            _mark_team_discord_dirty(s, event_id)
            s.commit()
            return result

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))


@events_bp.post("/events/<int:event_id>/signup-message")
async def post_signup_message(event_id: int):
    """Post an interactive "Sign up" button to the event's Discord announcements
    channel. Event admin; self-signup events only; requires the announcements
    channel to be configured (Event → Discord)."""
    user_id = current_user_id()

    def _apply():
        import json as _json
        from datetime import datetime as _dt2

        from db.models import EventChannel, NotificationQueue

        from services.event_signup import signup_close_at, signups_closed

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            if (ev.formation_mode or "admin_assign") not in EVENT_SELF_SIGNUP_MODES:
                abort_problem(422, "Sign-ups closed",
                              "Set the event to let players sign up first "
                              "(self-join, auto-assign, or sign-up pool).")
            # Don't post a button nobody can use (web70a).
            shut = signups_closed(ev)
            if shut:
                abort_problem(409, "Sign-ups closed", shut,
                              extra={"code": "signups_closed"})
            channel = (
                s.query(EventChannel)
                .filter(EventChannel.event_id == event_id,
                        EventChannel.kind == "announcements")
                .first()
            )
            if not channel:
                abort_problem(422, "No Discord channel",
                              "Configure this event's Discord announcements "
                              "channel first (Event → Discord).")
            rep = _representative_player_id(s, event_id)
            if rep is None:
                abort_problem(422, "No players", "There are no players to route the post through yet.")
            payload = {
                "event_id": event_id,
                "event_name": ev.name,
                "formation_mode": ev.formation_mode,
                "description": ev.description or None,
                "ends_at": _ts(ev.ends_at),
                # When the button stops working — the event's start, or its end
                # when late sign-ups are allowed (web70a). Drives the post's
                # "Sign-ups close …" line.
                "signup_close_at": _ts(signup_close_at(ev)),
                # Nonce so repeated posts don't collide on the notification
                # queue's unique (type, player, group, data) index.
                "posted_at": int(_dt2.now().timestamp()),
            }
            s.add(NotificationQueue(
                notification_type="event_signup_prompt",
                player_id=rep,
                group_id=ev.group_id,
                data=_json.dumps(payload),
                status="pending",
            ))
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.signup.message",
                target=f"web_events.{event_id}", before=None, after="posted",
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


def _representative_player_id(s, event_id: int) -> int | None:
    """A player id to hang the notification_queue row on (player_id is NOT
    NULL; the event sender never uses it). Prefers a roster member, else any
    player."""
    row = (
        s.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == event_id)
        .first()
    )
    if row:
        return row[0]
    row = s.query(Player.player_id).order_by(Player.player_id.asc()).first()
    return row[0] if row else None


@events_bp.delete("/events/<int:event_id>/signups/<int:player_id>")
async def remove_event_signup(event_id: int, player_id: int):
    """Admin withdraws a player from the pool (and any team placement)."""
    user_id = current_user_id()

    def _apply():
        from services.event_signup import remove_signup

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_roster_open(ev)
            remove_signup(s, ev, player_id)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.signup.remove",
                target=f"web_events.{event_id}.player.{player_id}",
                before="signed_up", after=None,
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))
