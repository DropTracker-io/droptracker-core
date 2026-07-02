"""Task 14 — events system.

Public reads:
  GET /api/v1/events?groupId=&status=active|past   -> EventSummary[]
  GET /api/v1/events/{id}                            -> EventDetail

Admin writes (session + group admin of events.group_id):
  POST   /api/v1/events                    { EventInput }     -> { id }
  PATCH  /api/v1/events/{id}                { partial }        -> EventDetail
  POST   /api/v1/events/{id}/tasks          { EventTaskInput } -> { id }
  DELETE /api/v1/events/{id}/tasks/{taskId}                    -> { ok }
  POST   /api/v1/events/{id}/teams          { EventTeamInput } -> { id }

Scores are computed by the submission pipeline (backend-owned), never trusted
from the client; this API only reads computed scores and exposes admin CRUD.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime

from quart import Blueprint, jsonify, request

from db import (
    Event,
    EventBingoCell,
    EventBingoCompletion,
    EventTask,
    EventTeam,
    EventTeamMember,
    EVENT_TASK_TYPES,
    Player,
)
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import (
    assert_group_admin,
    current_user_id,
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
    }


def _detail(s, ev: Event) -> dict:
    base = _summary(ev)

    tasks = [
        {
            "id": t.id,
            "type": t.type,
            "label": t.label,
            "target": t.target or None,
            "target_value": t.target_value,
            "points": int(t.points or 0),
        }
        for t in s.query(EventTask).filter(EventTask.event_id == ev.id).all()
    ]

    teams_rows = s.query(EventTeam).filter(EventTeam.event_id == ev.id).all()
    team_names = {tm.id: tm.name for tm in teams_rows}
    teams = []
    for tm in teams_rows:
        member_count = (
            s.query(EventTeamMember).filter(EventTeamMember.team_id == tm.id).count()
        )
        teams.append({
            "id": tm.id,
            "name": tm.name,
            "score": int(tm.score or 0),
            "member_count": int(member_count),
        })

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
            for comp in comps:
                label = None
                if comp.team_id:
                    label = team_names.get(comp.team_id)
                elif comp.player_id:
                    label = player_names.get(comp.player_id)
                if label:
                    completions_by_cell.setdefault(comp.cell_id, []).append(label)
        size = int(round(math.sqrt(len(cells)))) if cells else 0
        bingo = {
            "size": size,
            "cells": [
                {
                    "index": c.idx,
                    "label": c.label,
                    "task_id": c.task_id,
                    "completed_by": completions_by_cell.get(c.id, []),
                }
                for c in cells
            ],
        }

    base["tasks"] = tasks
    base["teams"] = teams
    base["bingo"] = bingo
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
                # Drafts only visible to group admins.
                allowed = False
                if viewer_id is not None and ev.group_id:
                    user = load_user(s, viewer_id)
                    role = resolve_group_role(
                        s, viewer_id, ev.group_id, manageable_guild_ids(viewer_id), user=user
                    )
                    allowed = role in ("owner", "admin")
                if not allowed:
                    return None
            return _detail(s, ev)

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    return with_cache_headers(jsonify(payload), max_age=30)


# --------------------------------------------------------------------------- #
# Admin writes
# --------------------------------------------------------------------------- #
def _assert_event_admin(s, user_id, group_id):
    if not group_id:
        abort_problem(422, "Missing group", "Events must belong to a group.")
    user = load_user(s, user_id)
    assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)


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

    def _apply():
        with db_session() as s:
            _assert_event_admin(s, user_id, group_id)
            ev = Event(
                group_id=group_id,
                name=name,
                description=(body.get("description") or None),
                status="active",
                starts_at=_dt(body.get("starts_at")),
                ends_at=_dt(body.get("ends_at")),
                has_bingo=False,
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
            s.commit()
            return _detail(s, ev)

    payload = await asyncio.to_thread(_apply)
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
            )
            s.add(task)
            s.commit()
            return task.id

    task_id = await asyncio.to_thread(_apply)
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
    return jsonify({"id": team_id})
