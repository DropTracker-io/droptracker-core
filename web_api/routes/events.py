"""Task 14 — events system (+ Task 16 team membership & formation modes).

Public reads:
  GET /api/v1/events?groupId=&status=active|past   -> EventSummary[]
  GET /api/v1/events/{id}                            -> EventDetail

Player-facing (session required; Task 16, PRD D4/D10):
  POST /api/v1/events/{id}/join   { player_id, team_id?, join_code? } -> { team_id }
  POST /api/v1/events/{id}/leave  { player_id }                        -> { ok }

Admin writes (session + group admin of events.group_id, or superadmin for
global events where group_id is NULL):
  POST   /api/v1/events                    { EventInput }     -> { id }
  PATCH  /api/v1/events/{id}                { partial }        -> EventDetail
  POST   /api/v1/events/{id}/tasks          { EventTaskInput } -> { id }
  DELETE /api/v1/events/{id}/tasks/{taskId}                    -> { ok }
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
import math
from datetime import datetime

from quart import Blueprint, jsonify, request
from sqlalchemy import func

from db import (
    AuditLog,
    Event,
    EventBingoCell,
    EventBingoCompletion,
    EventProgress,
    EventTask,
    EventTeam,
    EventTeamMember,
    EVENT_FORMATION_MODES,
    EVENT_TASK_TYPES,
    Group,
    Player,
    user_group_association,
)
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
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


def _effective_status(ev: Event) -> str:
    if ev.status == "draft":
        return "draft"
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
        "formation_mode": ev.formation_mode or "admin_assign",
        "requires_confirmation": bool(ev.requires_confirmation),
        "board_size": int(ev.board_size or 5),
        "bonus_line_points": int(ev.bonus_line_points or 0),
        "bonus_blackout_points": int(ev.bonus_blackout_points or 0),
        "activated_at": _ts(ev.activated_at),
        "ended_at": _ts(ev.ended_at),
    }


def _is_event_admin(s, viewer_id, ev: Event) -> bool:
    """Whether ``viewer_id`` administers ``ev`` (group owner/admin, or
    superadmin — the only admins of global events)."""
    if viewer_id is None:
        return False
    user = load_user(s, viewer_id)
    if is_superadmin(user):
        return True
    if not ev.group_id:
        return False
    role = resolve_group_role(s, viewer_id, ev.group_id, manageable_guild_ids(viewer_id), user=user)
    return role in ("owner", "admin")


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
        }
        for t in s.query(EventTask).filter(EventTask.event_id == ev.id).all()
    ]

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
        viewer = {
            "player_ids_on_event": [pid for pid, _ in on_event],
            "team_id": on_event[0][1] if on_event else None,
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

    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            q = s.query(Event)
            if group_id is not None:
                q = q.filter(Event.group_id == group_id)
            events = q.order_by(Event.id.desc()).all()

            # Determine which groups the viewer can admin (to see drafts).
            admin_groups = set()
            if viewer_id is not None:
                user = load_user(s, viewer_id)
                manage_ids = manageable_guild_ids(viewer_id)
                for ev in events:
                    if ev.group_id and ev.group_id not in admin_groups:
                        role = resolve_group_role(s, viewer_id, ev.group_id, manage_ids, user=user)
                        if role in ("owner", "admin"):
                            admin_groups.add(ev.group_id)

            out = []
            for ev in events:
                eff = _effective_status(ev)
                is_draft = eff == "draft"
                if is_draft and ev.group_id not in admin_groups:
                    continue  # drafts hidden from non-admins
                if status and eff != status:
                    continue
                out.append(_summary(ev))
            return out

    events = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(events), max_age=30)


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


# --------------------------------------------------------------------------- #
# Admin writes
# --------------------------------------------------------------------------- #
def _assert_event_admin(s, user_id, group_id):
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
    if not isinstance(group_id, int):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer.")
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

    def _apply():
        with db_session() as s:
            _assert_event_admin(s, user_id, group_id)
            # Group events default their Discord destination to the group's
            # linked guild (Task 19); admins can re-point it at any guild the
            # bot is in via PUT /events/{id}/discord.
            discord_guild_id = None
            if group_id:
                group = s.query(Group).filter(Group.group_id == group_id).first()
                if group and group.guild_id:
                    discord_guild_id = str(group.guild_id)
            ev = Event(
                group_id=group_id,
                name=name,
                description=(body.get("description") or None),
                status="active",
                starts_at=_dt(body.get("starts_at")),
                ends_at=_dt(body.get("ends_at")),
                has_bingo=False,
                formation_mode=formation_mode,
                requires_confirmation=bool(body.get("requires_confirmation")),
                join_code=join_code or None,
                discord_guild_id=discord_guild_id,
            )
            s.add(ev)
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
            _assert_event_admin(s, user_id, ev.group_id)
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
            if "requires_confirmation" in body:
                # Event-level force: all completions queue for review (PRD D3).
                ev.requires_confirmation = bool(body.get("requires_confirmation"))
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
            s.commit()
            return _detail(s, ev, viewer_id=user_id)

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


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

    def _apply():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev.group_id)
            task = EventTask(
                event_id=event_id,
                type=ttype,
                label=label,
                target=(body.get("target") or None),
                target_value=body.get("target_value"),
                points=int(body.get("points") or 0),
                requires_confirmation=bool(body.get("requires_confirmation")),
                config=(body.get("config") or None),
            )
            s.add(task)
            s.commit()
            return task.id

    task_id = await asyncio.to_thread(_apply)
    _bump(event_id)
    return jsonify({"id": task_id})


@events_bp.delete("/events/<int:event_id>/tasks/<int:task_id>")
async def delete_task(event_id: int, task_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            _assert_event_admin(s, user_id, ev.group_id)
            task = (
                s.query(EventTask)
                .filter(EventTask.id == task_id, EventTask.event_id == event_id)
                .first()
            )
            if task:
                s.delete(task)
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
            _assert_event_admin(s, user_id, ev.group_id)
            team = EventTeam(event_id=event_id, name=name, score=0)
            s.add(team)
            s.commit()
            return team.id

    team_id = await asyncio.to_thread(_apply)
    _bump(event_id)
    return jsonify({"id": team_id})


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
    """Group events: the player must be a member of the event's group.
    Global events (group_id NULL): any player is eligible."""
    if not ev.group_id:
        return
    in_group = (
        s.query(user_group_association.c.id)
        .filter(
            user_group_association.c.player_id == player_id,
            user_group_association.c.group_id == ev.group_id,
        )
        .first()
    )
    if not in_group:
        abort_problem(403, "Not a group member", "That player is not a member of this event's group.")


def _event_membership(s, event_id: int, player_id: int) -> EventTeamMember | None:
    """The player's membership row anywhere on this event (one team per
    player per event)."""
    return (
        s.query(EventTeamMember)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == event_id, EventTeamMember.player_id == player_id)
        .first()
    )


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

            teams = (
                s.query(EventTeam)
                .filter(EventTeam.event_id == event_id)
                .order_by(EventTeam.id.asc())
                .all()
            )
            if not teams:
                abort_problem(404, "No teams", "This event has no teams to join yet.")

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

    team_id = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"team_id": team_id}))


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
            _assert_event_admin(s, user_id, ev.group_id)
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
            _assert_event_admin(s, user_id, ev.group_id)
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
