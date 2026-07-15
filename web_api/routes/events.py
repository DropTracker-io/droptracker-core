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
  GET    /api/v1/events/meta/items?q=       -> [{ id, name }]  (task-form autocomplete)
  GET    /api/v1/events/meta/npcs?q=        -> [{ id, name }]
  GET    /api/v1/events/meta/resolve?kind=item|npc&names=a|b -> [{ id, name }]
  POST   /api/v1/events/{id}/teams          { EventTeamInput } -> { id }
  POST   /api/v1/events/{id}/teams/{teamId}/members   { player_id } -> { ok }
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

from db import (
    AuditLog,
    Event,
    EventBingoCell,
    EventBingoCompletion,
    EventCompletion,
    EventGroup,
    EventProgress,
    EventSignup,
    EventTask,
    EventTeam,
    EventTeamMember,
    EVENT_DISCORD_POLICIES,
    EVENT_FORMATION_MODES,
    EVENT_SELF_SIGNUP_MODES,
    EVENT_MODES,
    EVENT_PING_KEYS,
    EVENT_SUBMISSION_POLICIES,
    EVENT_TASK_TYPES,
    EVENT_TASK_VISIBILITIES,
    EventTaskLibraryItem,
    Group,
    GroupAdmin,
    Player,
    user_group_association,
)
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.task_tiles import build_tile, spec_names, tile_spec
from web_api.deps import (
    assert_group_admin,
    assert_group_entitlement,
    assert_superadmin,
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
    optional_user_id,
    resolve_group_role,
)

events_bp = Blueprint("v1_events", __name__)


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


def _summary(ev: Event) -> dict:
    return {
        "id": ev.id,
        "group_id": ev.group_id,
        "name": ev.name,
        "description": ev.description or None,
        "status": _effective_status(ev),
        "starts_at": _ts(ev.starts_at),
        "ends_at": _ts(ev.ends_at),
        "has_bingo": bool(ev.has_bingo),
        "mode": getattr(ev, "mode", None) or "standard",
        "formation_mode": ev.formation_mode or "admin_assign",
        "requires_confirmation": bool(ev.requires_confirmation),
        "submission_policy": ev.submission_policy or "all",
        "board_size": int(ev.board_size or 5),
        "bonus_line_points": int(ev.bonus_line_points or 0),
        "bonus_blackout_points": int(ev.bonus_blackout_points or 0),
        "activated_at": _ts(ev.activated_at),
        "ended_at": _ts(ev.ended_at),
    }


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
            for gid in participating_group_ids(s, ev)
        )
    if not ev.group_id:
        return False
    role = resolve_group_role(s, viewer_id, ev.group_id, manageable_guild_ids(viewer_id), user=user)
    return role in ("owner", "admin")


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
        for m, player_name in member_rows:
            members_by_team.setdefault(m.team_id, []).append({
                "player_id": m.player_id,
                "player_name": player_name,
                "joined_at": _ts(m.joined_at),
            })
    teams = []
    for tm in teams_rows:
        members = members_by_team.get(tm.id, [])
        teams.append({
            "id": tm.id,
            "name": tm.name,
            "score": int(tm.score or 0),
            "group_id": getattr(tm, "group_id", None),  # clan bound (clan_vs_clan)
            "color": getattr(tm, "color", None),  # admin accent; null = palette default
            "member_count": len(members),
            "members": members,
        })

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
        viewer = {
            "player_ids_on_event": [pid for pid, _ in on_event],
            "team_id": on_event[0][1] if on_event else None,
            "signed_up_player_ids": signed_up_pids,
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
    # progress bars instead of only final team scores.
    base["progress"] = [
        {
            "task_id": p.task_id,
            "team_id": p.team_id,
            "progress": int(p.progress or 0),
            "completed": bool(p.completed),
            "completed_at": _ts(p.completed_at),
        }
        for p in s.query(EventProgress).filter(EventProgress.event_id == ev.id).all()
    ]

    base["tasks"] = tasks
    base["teams"] = teams
    base["bingo"] = bingo
    base["viewer"] = viewer
    # Never the code itself on public reads — only whether one is required.
    base["join_requires_code"] = bool(ev.join_code)
    if _is_event_admin(s, viewer_id, ev):
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
                            if role in ("owner", "admin"):
                                admin_groups.add(ev.group_id)

            out = []
            for ev in events:
                eff = _effective_status(ev)
                is_draft = eff == "draft"
                if is_draft and not viewer_is_superadmin and (
                        not ev.group_id or ev.group_id not in admin_groups):
                    # clan-vs-clan drafts are also visible to admins of any
                    # accepted participant (mode check first: standard events
                    # take the fast `continue` with no extra queries).
                    if not ((getattr(ev, "mode", None) or "standard") == "clan_vs_clan"
                            and viewer_id is not None
                            and _is_event_admin(s, viewer_id, ev)):
                        continue  # drafts hidden from non-admins
                if status and eff != status:
                    continue
                out.append(_summary(ev))
            return out

    events = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(events), max_age=30)


@events_bp.get("/events/launch-intent")
async def event_launch_intent():
    """Claim (and clear) the current user's pending Activity deep-link target —
    set by the bot when they clicked an "Open in Discord" launch button on an
    event message. One-shot: returns the event id once, then it's gone.
    ``{"event_id": null}`` when nothing is pending (app opens to its home hub).

    Keyed by the user's Discord id, so a session only ever claims its own
    intent."""
    user_id = current_user_id()

    def _claim():
        from services.activity_launch_core import intent_key
        from utils.redis import redis_client

        with db_session() as s:
            user = load_user(s, user_id)
            discord_id = getattr(user, "discord_id", None) if user else None
        if not discord_id:
            return None
        key = intent_key(discord_id)
        raw = redis_client.get(key)
        if raw is None:
            return None
        redis_client.delete(key)  # one-shot claim
        value = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        return int(value) if value.isdigit() else None

    event_id = await asyncio.to_thread(_claim)
    return private_no_store(jsonify({"event_id": event_id}))


@events_bp.get("/events/by-channel/<channel_id>")
async def event_by_channel(channel_id: str):
    """Resolve a Discord channel to the event whose board/notifications live
    there — the Activity's anonymous deep-link fallback: a launch button opens
    the app in its channel, and ``sdk.channelId`` tells us which. Prefers the
    active event; falls back to the most recent event pointed at the channel
    (so an ended event's "Final standings" button still lands right).
    ``{"event_id": null}`` when no event maps to the channel."""
    channel_id = (channel_id or "").strip()
    if not channel_id.isdigit():
        return with_cache_headers(jsonify({"event_id": None}), max_age=30)

    def _load():
        from db.models import EventChannel

        with db_session() as s:
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

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            if _effective_status(ev) == "draft":
                # Drafts only visible to event admins.
                if not _is_event_admin(s, viewer_id, ev):
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
            if _effective_status(ev) == "draft" and not _is_event_admin(s, viewer_id, ev):
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
            # Per-member contribution rollup from the applied ledger.
            contrib: dict[int, dict[str, int]] = {}
            for c in applied:
                if c.player_id is None:
                    continue
                row = contrib.setdefault(c.player_id, {"completions": 0, "quantity": 0})
                row["completions"] += 1
                row["quantity"] += int(c.quantity or 1)

            members = [
                {
                    "player_id": m.player_id,
                    "player_name": player_name,
                    "joined_at": _ts(m.joined_at),
                    "completions": contrib.get(m.player_id, {}).get("completions", 0),
                    "quantity": contrib.get(m.player_id, {}).get("quantity", 0),
                }
                for m, player_name in member_rows
            ]

            task_rows = s.query(EventTask).filter(EventTask.event_id == event_id).all()
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

            task_labels = {t.id: t.label for t in task_rows}
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
                    "created_at": _ts(c.created_at),
                }
                for c in applied[:50]
            ]

            return {
                "event": _summary(ev),
                "team": {
                    "id": team.id,
                    "name": team.name,
                    "score": int(team.score or 0),
                    "group_id": getattr(team, "group_id", None),
                    "color": getattr(team, "color", None),
                    "rank": rank,
                    "team_count": len(all_teams),
                    "member_count": len(members),
                },
                "members": members,
                "tasks": tasks,
                "activity": activity,
            }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Team not found", f"No team {team_id} in event {event_id}.")
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


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
            if resolve_group_role(s, user_id, gid, mgids, user=user) in ("owner", "admin"):
                return
        abort_problem(403, "Forbidden", "You must administer a participating clan.")
    group_id = ev.group_id if ev is not None else ev_or_group_id
    # ---- standard/global path, unchanged ----
    user = load_user(s, user_id)
    if not group_id:
        # Global events (group_id NULL) are administered by superadmins only.
        assert_superadmin(user)
        return
    assert_group_entitlement(
        s,
        user_id,
        group_id,
        "events",
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
    submission_policy = body.get("submission_policy") or "all"
    if submission_policy not in EVENT_SUBMISSION_POLICIES:
        abort_problem(
            422,
            "Invalid submission policy",
            f"submission_policy must be one of {list(EVENT_SUBMISSION_POLICIES)}.",
        )
    mode = body.get("mode") or "standard"
    if mode not in EVENT_MODES:
        abort_problem(422, "Invalid mode", f"mode must be one of {list(EVENT_MODES)}.")
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
                starts_at=_dt(body.get("starts_at")),
                ends_at=_dt(body.get("ends_at")),
                has_bingo=False,
                formation_mode=formation_mode,
                requires_confirmation=bool(body.get("requires_confirmation")),
                submission_policy=submission_policy,
                join_code=join_code or None,
                discord_guild_id=discord_guild_id,
                mode=mode,
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
            if "name" in body:
                name = (body.get("name") or "").strip()
                if not (1 <= len(name) <= 120):
                    abort_problem(422, "Invalid name", "Event name must be 1–120 characters.")
                ev.name = name
            if "description" in body:
                ev.description = body.get("description") or None
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
            if "requires_confirmation" in body:
                # Event-level force: all completions queue for review (PRD D3).
                ev.requires_confirmation = bool(body.get("requires_confirmation"))
            if "submission_policy" in body:
                policy = body.get("submission_policy") or "all"
                if policy not in EVENT_SUBMISSION_POLICIES:
                    abort_problem(
                        422,
                        "Invalid submission policy",
                        f"submission_policy must be one of {list(EVENT_SUBMISSION_POLICIES)}.",
                    )
                ev.submission_policy = policy
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
            s.commit()
            return _detail(s, ev, viewer_id=user_id)

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


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


def clean_task_visibility(body: dict, default: str | None = "public") -> str | None:
    """Validated EVENT_TASK_VISIBILITIES value from a request body.

    ``default`` is returned when the key is absent ("public" on create —
    matching the sitewide default — and None on PATCH, where an absent key
    means "leave the library copy alone")."""
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

    def _apply():
        from web_api.routes.event_task_validation import validate_task_payload

        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev)
            normalized = validate_task_payload(s, body)
            task = EventTask(
                event_id=event_id,
                type=ttype,
                label=label,
                points=int(body.get("points") or 0),
                requires_confirmation=bool(body.get("requires_confirmation")),
                visibility=visibility,
                **normalized,
            )
            s.add(task)
            task.visibility = save_task_to_library(s, ev, task, visibility)
            s.commit()
            return task.id, task.visibility

    task_id, effective_visibility = await asyncio.to_thread(_apply)
    _bump(event_id)
    # visibility echoes what was stored: a "public" save whose requirements
    # duplicate an existing public preset is demoted to "private".
    return jsonify({"id": task_id, "visibility": effective_visibility})


@events_bp.get("/events/meta/items")
async def search_items():
    """Item-name autocomplete for the task form (session required)."""
    current_user_id()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])

    def _search():
        from db import ItemList

        with db_session() as s:
            # Stack/noted variants share a name — collapse to one row per name.
            rows = (
                s.query(func.min(ItemList.item_id), ItemList.item_name)
                .filter(ItemList.item_name.ilike(f"%{q}%"), ItemList.noted.is_(False))
                .group_by(ItemList.item_name)
                .order_by(func.length(ItemList.item_name), ItemList.item_name)
                .limit(15)
                .all()
            )
            return [{"id": i, "name": n} for i, n in rows]

    return jsonify(await asyncio.to_thread(_search))


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
            return [{"id": i, "name": n} for i, n in rows]

    return jsonify(await asyncio.to_thread(_search))


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
    it granted are taken back from team scores."""
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev)
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
            for team_id_, delta in deltas.items():
                team = s.query(EventTeam).filter(EventTeam.id == team_id_).first()
                if team is not None:
                    team.score = max(int(team.score or 0) - delta, 0)

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
            s.delete(task)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                action="event.task.delete",
                target=f"web_events.{event_id}.task.{task_id}",
                before=f"label:{task_label}",
                after=None,
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
    if name is None and not has_color:
        abort_problem(422, "Nothing to update", "Provide a name and/or a color.")

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
            before = {"name": team.name, "color": team.color}
            if name is not None:
                team.name = name
            if has_color:
                team.color = color
            after = {"name": team.name, "color": team.color}
            if before == after:
                return  # no-op
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
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
            s.delete(team)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
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
            s.add(EventTeamMember(team_id=team.id, player_id=player_id))
            s.commit()
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
            # Also withdraw a sign-up-pool opt-in. Only signup_pool events ever
            # write signup rows, so other modes stay byte-for-byte (no query).
            signup = None
            if (ev.formation_mode or "admin_assign") == "signup_pool":
                signup = _signup_row(s, event_id, player_id)
                if signup:
                    s.delete(signup)
            if membership or signup:
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
                s.flush()
            s.add(EventTeamMember(team_id=team_id, player_id=player_id))
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
                    action="event.member.add",
                    target=f"web_events.{event_id}.player.{player_id}",
                    before=before,
                    after=f"team:{team_id}",
                )
            )
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


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
                s.add(
                    AuditLog(
                        actor_user_id=user_id,
                        group_id=ev.group_id,
                        action="event.member.remove",
                        target=f"web_events.{event_id}.player.{player_id}",
                        before=f"team:{team_id}",
                        after=None,
                    )
                )
                s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


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
            return list_pool(s, ev)

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
                action="event.signup.assign",
                target=f"web_events.{event_id}.player.{player_id}",
                before=None, after=f"team:{team_id}",
            ))
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
                action="event.signup.randomize",
                target=f"web_events.{event_id}",
                before=None, after=f"assigned:{result['assigned']}",
            ))
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

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            if (ev.formation_mode or "admin_assign") not in EVENT_SELF_SIGNUP_MODES:
                abort_problem(422, "Sign-ups closed",
                              "Set the event to let players sign up first "
                              "(self-join, auto-assign, or sign-up pool).")
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
                action="event.signup.remove",
                target=f"web_events.{event_id}.player.{player_id}",
                before="signed_up", after=None,
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))
