"""Event templates — save an event's structure for public/private re-use.

The whole-event analogue of the task library ("Saving/Rerunning Events"
suggestion): admins of an event in any lifecycle state snapshot its
*structure* (config + tasks + bingo layout + team names) as a named template,
shared ``public`` (every clan's picker) or ``private`` (owning group only),
and instantiate it later as a fresh standard draft.

  POST   /api/v1/events/{id}/save-template   { name, description?,
                                               visibility?, include_teams? }
         -> { id }                    (upserts per group by lower-cased name)
  GET    /api/v1/event-templates?query=&groupId=&page=  -> EventTemplateSummary[]
         (session + any group admin / superadmin; public ∪ own-group private;
          groupId switches to that group's own templates for management)
  GET    /api/v1/event-templates/{id}          -> EventTemplateDetail (+ preview)
  POST   /api/v1/event-templates/{id}/instantiate
         { group_id, name?, description?, starts_at?, ends_at?, include_teams? }
         -> { id, skipped_tasks }     (a fresh draft event; bumps times_used)
  PATCH  /api/v1/event-templates/{id}          { name?, description?, visibility? }
  DELETE /api/v1/event-templates/{id}          -> { ok }   (soft: active=false)

Semantics:
- Snapshots capture structure only — never dates, rosters, ledger/progress,
  scores, join codes, Discord config or clan-vs-clan participants. A
  clan_vs_clan event saves its team *names* and re-runs as a standard draft
  (clan bindings and invites are inherently per-run).
- Templates are owned by the event's host group (``ev.group_id``; NULL for
  global events = site-wide, superadmin-managed).
- Instantiation re-validates every task through ``validate_task_payload`` —
  item/NPC names can drift between save and re-run — and is lenient: tasks
  that no longer validate are skipped and reported, never a hard failure.
  Their bingo cells survive unbound (rebindable in the designer).
- The payload is versioned JSON (``EVENT_TEMPLATE_SCHEMA_VERSION``);
  ``_normalize_payload`` is the upgrade shim for older snapshots.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify, request
from sqlalchemy import func
from sqlalchemy import or_ as sa_or

from db import (
    AuditLog,
    Event,
    EventBingoCell,
    EventTask,
    EventTeam,
    EventTemplate,
    EVENT_TASK_TYPES,
    EVENT_TASK_VISIBILITIES,
    EVENT_TEMPLATE_SCHEMA_VERSION,
    Group,
)
from web_api.common import ProblemException, abort_problem, db_session, private_no_store
from web_api.deps import (
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
)
from web_api.routes.events import (
    _assert_event_admin,
    _bump,
    _dt,
    _load_event_or_404,
    _sync_event_guilds,
    _ts,
)

event_templates_bp = Blueprint("v1_event_templates", __name__)

_PAGE_SIZE = 20


# --------------------------------------------------------------------------- #
# Snapshot / instantiate core (pure session-level helpers, unit-testable)
# --------------------------------------------------------------------------- #
def _strip_bingo_auto(config):
    """Drop the bingo designer's task-instance marker from a config JSON
    string (same bookkeeping strip as ``save_task_to_library``)."""
    if not config:
        return None
    try:
        parsed = json.loads(config)
    except (TypeError, ValueError):
        return config
    if isinstance(parsed, dict) and parsed.pop("bingo_auto", None) is not None:
        return json.dumps(parsed) if parsed else None
    return config


def snapshot_event(s, ev: Event) -> dict:
    """The event's structure as a version-stamped payload dict.

    Bingo cells reference tasks by ``task_ref`` — the task's index in the
    payload's ``tasks`` list — so the snapshot carries no row ids.
    """
    tasks = (
        s.query(EventTask)
        .filter(EventTask.event_id == ev.id)
        .order_by(EventTask.id.asc())
        .all()
    )
    ref_by_task_id: dict[int, int] = {}
    tasks_out = []
    for i, t in enumerate(tasks):
        ref_by_task_id[t.id] = i
        tasks_out.append({
            "type": t.type,
            "label": t.label,
            "target": t.target or None,
            "target_value": t.target_value,
            "points": int(t.points or 0),
            "requires_confirmation": bool(t.requires_confirmation),
            "visibility": t.visibility or "public",
            "config": _strip_bingo_auto(t.config),
        })

    teams_out = [
        {"name": tm.name}
        for tm in s.query(EventTeam)
        .filter(EventTeam.event_id == ev.id)
        .order_by(EventTeam.id.asc())
        .all()
    ]

    bingo = None
    if ev.has_bingo:
        cells = (
            s.query(EventBingoCell)
            .filter(EventBingoCell.event_id == ev.id)
            .order_by(EventBingoCell.idx.asc())
            .all()
        )
        bingo = {
            "size": int(ev.board_size or 5),
            "cells": [
                {
                    "idx": c.idx,
                    "label": c.label,
                    "task_ref": ref_by_task_id.get(c.task_id),
                }
                for c in cells
            ],
        }

    return {
        "version": EVENT_TEMPLATE_SCHEMA_VERSION,
        "event": {
            "description": ev.description or None,
            "formation_mode": ev.formation_mode or "admin_assign",
            "requires_confirmation": bool(ev.requires_confirmation),
            "submission_policy": ev.submission_policy or "all",
            "has_bingo": bool(ev.has_bingo),
            "board_size": int(ev.board_size or 5),
            "bonus_line_points": int(ev.bonus_line_points or 0),
            "bonus_blackout_points": int(ev.bonus_blackout_points or 0),
            "mode": getattr(ev, "mode", None) or "standard",
            # Game format (web43a) — preserved so a loot_sweep/board_game
            # template instantiates as that kind, not a flat standard event.
            "kind": getattr(ev, "kind", None) or "standard",
            # Organizer settings (audit): these were silently dropped, so a
            # monthly re-run shipped with the pot/leadership/verbosity OFF
            # while the organizer believed the template carried them.
            "buyins_enabled": bool(getattr(ev, "buyins_enabled", False)),
            "prize_config": _parse_json_col(getattr(ev, "prize_config", None)),
            "leadership_config": _parse_json_col(
                getattr(ev, "leadership_config", None)),
            "message_config": _parse_json_col(
                getattr(ev, "message_config", None)),
            # Recurring schedule (web82a): the RULE travels, never the
            # materialized windows — instantiation recompiles it against the
            # new run's dates, which is the whole point of "run our weekend
            # event again next month".
            "schedule": _parse_json_col(getattr(ev, "schedule_config", None)),
        },
        "tasks": tasks_out,
        "teams": teams_out,
        "bingo": bingo,
    }


def _parse_json_col(raw) -> dict | None:
    """Parsed dict from a JSON text column (None for empty/corrupt/non-dict)."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _normalize_payload(raw: str) -> dict:
    """Parse a stored payload and upgrade it to the current schema version.

    v1 is the only shape so far; when the snapshot changes, bump
    ``EVENT_TEMPLATE_SCHEMA_VERSION`` and add an upgrade step here so old
    templates keep instantiating.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        abort_problem(500, "Template unreadable", "Stored template payload is corrupt.")
    version = payload.get("version")
    if version != EVENT_TEMPLATE_SCHEMA_VERSION:
        abort_problem(
            500, "Template version unsupported",
            f"Template payload version {version!r} has no upgrade path.",
        )
    return payload


def instantiate_template(
    s,
    payload: dict,
    *,
    group_id: int | None,
    name: str,
    description,
    starts_at,
    ends_at,
    include_teams: bool,
    superadmin: bool = False,
) -> tuple[Event, list[dict]]:
    """Materialize a payload as a fresh draft event (preserving its ``kind``).

    Returns ``(event, skipped_tasks)`` — tasks that no longer validate
    (renamed items/NPCs, tightened rules) are skipped and reported instead of
    failing the whole instantiation; their bingo cells survive unbound.
    """
    from db import EVENT_KINDS
    from services.event_types import is_event_type_creatable
    from web_api.routes.event_task_validation import validate_task_payload

    spec = payload.get("event") or {}
    # Preserve the templated game format, but a restricted kind (loot_sweep is
    # admin_only until launch) still respects the create gate for this viewer.
    kind = spec.get("kind") or "standard"
    if kind not in EVENT_KINDS:
        kind = "standard"
    if kind != "standard" and not is_event_type_creatable(
        s, kind, is_superadmin=superadmin, group_id=group_id
    ):
        abort_problem(
            403, "Event type unavailable",
            f"This template creates a '{kind}' event, which isn't enabled for "
            "you yet. Ask a site admin to enable it or add your clan to its "
            "test list.",
        )

    # Group events default their Discord destination to the group's linked
    # guild, exactly like POST /events.
    discord_guild_id = None
    if group_id:
        group = s.query(Group).filter(Group.group_id == group_id).first()
        if group and group.guild_id:
            discord_guild_id = str(group.guild_id)

    ev = Event(
        group_id=group_id,
        name=name,
        description=description if description is not None else (spec.get("description") or None),
        status="draft",
        starts_at=starts_at,
        ends_at=ends_at,
        has_bingo=False,  # set below once cells exist
        formation_mode=spec.get("formation_mode") or "admin_assign",
        requires_confirmation=bool(spec.get("requires_confirmation")),
        submission_policy=spec.get("submission_policy") or "all",
        join_code=None,  # per-run secret, never templated
        discord_guild_id=discord_guild_id,
        kind=kind,  # preserve the templated game format (web43a)
        mode="standard",  # clan bindings/invites are per-run (CvC resets)
        discord_event_policy="on_activate",
        ping_config=None,
        board_size=int(spec.get("board_size") or 5),
        bonus_line_points=int(spec.get("bonus_line_points") or 0),
        bonus_blackout_points=int(spec.get("bonus_blackout_points") or 0),
        # Organizer settings carried by the template (audit) — absent on old
        # (pre-carry) templates, so each falls back to the create default.
        buyins_enabled=bool(spec.get("buyins_enabled")),
    )
    for json_key in ("prize_config", "leadership_config", "message_config"):
        value = spec.get(json_key)
        if isinstance(value, dict):
            setattr(ev, json_key, json.dumps(value))
    s.add(ev)
    s.flush()

    # Recurring schedule (web82a) — recompiled against THIS run's dates. A
    # template whose schedule no longer fits (dates too short for a single
    # window, or none supplied) instantiates as a continuous event rather
    # than failing the whole run; the organizer can re-add it in the manager.
    schedule = spec.get("schedule")
    if isinstance(schedule, dict) and schedule.get("rule") and starts_at and ends_at:
        from services.event_schedule import ScheduleError, apply_schedule, validate_config

        try:
            apply_schedule(s, ev, validate_config(schedule))
            s.flush()
        except ScheduleError:
            ev.schedule_config = None

    # Tasks — lenient revalidation; local payload index -> new row id.
    skipped: list[dict] = []
    task_id_by_ref: dict[int, int] = {}
    for i, t in enumerate(payload.get("tasks") or []):
        label = (str(t.get("label") or "")).strip()
        ttype = t.get("type")
        if ttype not in EVENT_TASK_TYPES or not label:
            skipped.append({"index": i, "label": label or "(unnamed)",
                            "reason": "Unknown task type." if label else "Missing label."})
            continue
        try:
            normalized = validate_task_payload(s, {
                "type": ttype,
                "target": t.get("target"),
                "target_value": t.get("target_value"),
                "config": t.get("config"),
            })
        except ProblemException as exc:
            skipped.append({"index": i, "label": label,
                            "reason": exc.detail or exc.title})
            continue
        points = t.get("points")
        visibility = t.get("visibility")
        task = EventTask(
            event_id=ev.id,
            type=ttype,
            label=label[:255],
            target=normalized["target"],
            target_value=normalized["target_value"],
            points=points if isinstance(points, int) and not isinstance(points, bool) and points >= 0 else 0,
            requires_confirmation=bool(t.get("requires_confirmation")),
            visibility=visibility if visibility in EVENT_TASK_VISIBILITIES else "public",
            config=normalized["config"],
        )
        s.add(task)
        s.flush()
        task_id_by_ref[i] = task.id

    # Bingo board — cells whose task was skipped stay as labeled, unbound
    # cells (rebindable in the designer), NOT silently free spaces.
    bingo = payload.get("bingo")
    if spec.get("has_bingo") and isinstance(bingo, dict) and isinstance(bingo.get("cells"), list):
        for cell in bingo["cells"]:
            idx = cell.get("idx")
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                continue
            ref = cell.get("task_ref")
            s.add(EventBingoCell(
                event_id=ev.id,
                idx=idx,
                label=(str(cell.get("label") or "") or "Free space")[:255],
                task_id=task_id_by_ref.get(ref) if isinstance(ref, int) else None,
            ))
        ev.has_bingo = True
        size = bingo.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            ev.board_size = size
        # No free-cell grants here — a fresh draft is not live; activation
        # (event_lifecycle) grants free cells exactly as for hand-built events.

    if include_teams:
        for team in payload.get("teams") or []:
            tname = (str(team.get("name") or "")).strip()
            if tname:
                s.add(EventTeam(event_id=ev.id, name=tname[:80], score=0))

    return ev, skipped


# --------------------------------------------------------------------------- #
# Auth / serialization helpers
# --------------------------------------------------------------------------- #
def _viewer_admin_group_ids(s, user_id: int) -> set[int]:
    """Group ids the user administers (web grants ∪ MANAGE_GUILD on linked
    guilds) — lazy import so this module stays importable under the pytest
    conftest stubs (same reason event_admin lazy-loads the engine)."""
    from web_api.routes.event_admin import _admin_group_ids

    return _admin_group_ids(s, user_id, manageable_guild_ids(user_id))


def _can_view_template(tmpl: EventTemplate, *, superadmin: bool, admin_gids: set[int]) -> bool:
    if superadmin:
        return True
    if tmpl.visibility == "public" or tmpl.group_id is None:
        # group_id NULL rows come from global events — always shareable.
        return tmpl.visibility == "public"
    return tmpl.group_id in admin_gids


def _assert_template_owner(s, user_id: int, tmpl: EventTemplate) -> None:
    """Mutations (rename / re-scope / delete): owning group's admins, or
    superadmin for site-wide (group_id NULL) templates."""
    user = load_user(s, user_id)
    if is_superadmin(user):
        return
    if tmpl.group_id is not None and tmpl.group_id in _viewer_admin_group_ids(s, user_id):
        return
    abort_problem(403, "Forbidden", "Only the owning clan's admins can manage this template.")


def _summary(tmpl: EventTemplate) -> dict:
    return {
        "id": tmpl.id,
        "name": tmpl.name,
        "description": tmpl.description,
        "source_event_id": tmpl.source_event_id,
        "group_id": tmpl.group_id,
        "visibility": tmpl.visibility or "private",
        "mode": tmpl.mode or "standard",
        "has_bingo": bool(tmpl.has_bingo),
        "board_size": int(tmpl.board_size or 5),
        "task_count": int(tmpl.task_count or 0),
        "team_count": int(tmpl.team_count or 0),
        "times_used": int(tmpl.times_used or 0),
        "created_at": _ts(tmpl.created_at),
        "updated_at": _ts(tmpl.updated_at),
    }


def _detail(tmpl: EventTemplate) -> dict:
    base = _summary(tmpl)
    payload = _normalize_payload(tmpl.payload)
    spec = payload.get("event") or {}
    base["preview"] = {
        "description": spec.get("description"),
        "formation_mode": spec.get("formation_mode") or "admin_assign",
        "requires_confirmation": bool(spec.get("requires_confirmation")),
        "submission_policy": spec.get("submission_policy") or "all",
        "bonus_line_points": int(spec.get("bonus_line_points") or 0),
        "bonus_blackout_points": int(spec.get("bonus_blackout_points") or 0),
        "tasks": [
            {
                "type": t.get("type"),
                "label": t.get("label"),
                "target": t.get("target"),
                "target_value": t.get("target_value"),
                "points": int(t.get("points") or 0),
            }
            for t in payload.get("tasks") or []
        ],
        "teams": [t.get("name") for t in payload.get("teams") or [] if t.get("name")],
    }
    return base


def _clean_template_name(body: dict, *, required: bool) -> str | None:
    if "name" not in body and not required:
        return None
    name = (str(body.get("name") or "")).strip()
    if not (1 <= len(name) <= 120):
        abort_problem(422, "Invalid name", "Template name must be 1–120 characters.")
    return name


def _clean_template_visibility(body: dict, default: str | None) -> str | None:
    if "visibility" not in body:
        return default
    visibility = body.get("visibility")
    if visibility not in EVENT_TASK_VISIBILITIES:
        abort_problem(
            422, "Invalid visibility",
            f"visibility must be one of {list(EVENT_TASK_VISIBILITIES)}.",
        )
    return visibility


def _load_template_or_404(s, template_id: int) -> EventTemplate:
    tmpl = (
        s.query(EventTemplate)
        .filter(EventTemplate.id == template_id, EventTemplate.active.is_(True))
        .first()
    )
    if tmpl is None:
        abort_problem(404, "Template not found", f"No event template {template_id}.")
    return tmpl


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@event_templates_bp.post("/events/<int:event_id>/save-template")
async def save_event_template(event_id: int):
    """Snapshot an event as a template (any lifecycle state). Upserts per
    owning group by lower-cased name — re-saving a same-named template
    updates it instead of duplicating (task-library semantics)."""
    user_id = current_user_id()
    body = await json_body()
    name = _clean_template_name(body, required=True)
    visibility = _clean_template_visibility(body, default="private")
    include_teams = bool(body.get("include_teams", True))
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        abort_problem(422, "Invalid description", "description must be a string or null.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)

            payload = snapshot_event(s, ev)
            if not include_teams:
                payload["teams"] = []

            group_match = (
                EventTemplate.group_id == ev.group_id
                if ev.group_id is not None
                else EventTemplate.group_id.is_(None)
            )
            tmpl = (
                s.query(EventTemplate)
                .filter(group_match, func.lower(EventTemplate.name) == name.lower())
                .first()
            )
            created = tmpl is None
            if created:
                tmpl = EventTemplate(name=name, group_id=ev.group_id)
                s.add(tmpl)
            tmpl.name = name
            tmpl.description = (description or "").strip() or None
            tmpl.source_event_id = ev.id
            tmpl.created_by_user_id = user_id
            tmpl.visibility = visibility
            tmpl.mode = payload["event"]["mode"]
            tmpl.has_bingo = payload["event"]["has_bingo"]
            tmpl.board_size = payload["event"]["board_size"]
            tmpl.task_count = len(payload["tasks"])
            tmpl.team_count = len(payload["teams"])
            tmpl.schema_version = EVENT_TEMPLATE_SCHEMA_VERSION
            tmpl.payload = json.dumps(payload)
            tmpl.active = True
            s.flush()

            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                action="event.template.save",
                target=f"web_event_templates.{tmpl.id}",
                before=None,
                after=json.dumps({
                    "event_id": ev.id, "name": name, "visibility": visibility,
                    "tasks": tmpl.task_count, "teams": tmpl.team_count,
                    "created": created,
                }),
            ))
            s.commit()
            return tmpl.id

    tmpl_id = await asyncio.to_thread(_apply)
    return jsonify({"id": tmpl_id})


@event_templates_bp.get("/event-templates")
async def list_event_templates():
    """Picker/management list. Default: public rows ∪ the caller's own
    groups' private rows. ``groupId=`` narrows to that group's own templates
    (management view — requires admin of that group)."""
    user_id = current_user_id()
    query = (request.args.get("query") or "").strip()
    group_filter = request.args.get("groupId")
    try:
        page = max(int(request.args.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    if group_filter is not None:
        try:
            group_filter = int(group_filter)
        except (TypeError, ValueError):
            abort_problem(422, "Invalid groupId", "groupId must be an integer.")

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            superadmin = is_superadmin(user)
            admin_gids: set[int] = set()
            if not superadmin:
                admin_gids = _viewer_admin_group_ids(s, user_id)
                if not admin_gids:
                    abort_problem(403, "Forbidden", "Event admins only.")
            if (group_filter is not None and not superadmin
                    and group_filter not in admin_gids):
                abort_problem(403, "Forbidden", "You do not administer that group.")
            q = s.query(EventTemplate).filter(EventTemplate.active.is_(True))
            if group_filter is not None:
                q = q.filter(EventTemplate.group_id == group_filter)
            elif not superadmin:
                q = q.filter(sa_or(
                    EventTemplate.visibility == "public",
                    EventTemplate.group_id.in_(sorted(admin_gids)),
                ))
            if query:
                like = f"%{query}%"
                q = q.filter(EventTemplate.name.like(like)
                             | EventTemplate.description.like(like))
            rows = (
                q.order_by(EventTemplate.times_used.desc(), EventTemplate.name.asc())
                .offset((page - 1) * _PAGE_SIZE)
                .limit(_PAGE_SIZE)
                .all()
            )
            return [_summary(r) for r in rows]

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_templates_bp.get("/event-templates/<int:template_id>")
async def get_event_template(template_id: int):
    """Template detail + preview (task list, team names) for the picker."""
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            tmpl = _load_template_or_404(s, template_id)
            user = load_user(s, user_id)
            superadmin = is_superadmin(user)
            admin_gids = set() if superadmin else _viewer_admin_group_ids(s, user_id)
            if not _can_view_template(tmpl, superadmin=superadmin, admin_gids=admin_gids):
                abort_problem(404, "Template not found", f"No event template {template_id}.")
            return _detail(tmpl)

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_templates_bp.post("/event-templates/<int:template_id>/instantiate")
async def instantiate_event_template(template_id: int):
    """Create a fresh draft event from a template. The caller must be able to
    *see* the template and must pass the exact create-event gate (group admin
    + events entitlement; superadmin for global) on the TARGET group."""
    user_id = current_user_id()
    body = await json_body()
    group_id = body.get("group_id")
    if group_id is not None and not isinstance(group_id, int):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer or null.")
    name = _clean_template_name(body, required=False)
    include_teams = bool(body.get("include_teams", True))
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        abort_problem(422, "Invalid description", "description must be a string or null.")

    def _apply():
        with db_session() as s:
            tmpl = _load_template_or_404(s, template_id)
            user = load_user(s, user_id)
            superadmin = is_superadmin(user)
            admin_gids = set() if superadmin else _viewer_admin_group_ids(s, user_id)
            if not _can_view_template(tmpl, superadmin=superadmin, admin_gids=admin_gids):
                abort_problem(404, "Template not found", f"No event template {template_id}.")
            # Same gate as POST /events on the target group.
            _assert_event_admin(s, user_id, group_id)

            payload = _normalize_payload(tmpl.payload)
            ev, skipped = instantiate_template(
                s,
                payload,
                group_id=group_id,
                name=name or tmpl.name,
                description=description,
                starts_at=_dt(body.get("starts_at")),
                ends_at=_dt(body.get("ends_at")),
                include_teams=include_teams,
                superadmin=superadmin,
            )
            tmpl.times_used = int(tmpl.times_used or 0) + 1
            if ev.discord_guild_id:
                # Desired-state rows for the scheduled-event mirror — a no-op
                # for on_activate drafts, kept for parity with POST /events.
                _sync_event_guilds(s, ev)

            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=group_id,
                action="event.template.instantiate",
                target=f"web_events.{ev.id}",
                before=None,
                after=json.dumps({
                    "template_id": tmpl.id, "name": ev.name,
                    "skipped_tasks": len(skipped),
                }),
            ))
            s.commit()
            return ev.id, skipped

    ev_id, skipped = await asyncio.to_thread(_apply)
    _bump(ev_id)
    return jsonify({"id": ev_id, "skipped_tasks": skipped})


@event_templates_bp.patch("/event-templates/<int:template_id>")
async def update_event_template(template_id: int):
    """Rename / re-describe / re-scope (visibility) a template."""
    user_id = current_user_id()
    body = await json_body()
    name = _clean_template_name(body, required=False)
    visibility = _clean_template_visibility(body, default=None)
    description = body.get("description") if "description" in body else False  # False = absent
    if description not in (False, None) and not isinstance(description, str):
        abort_problem(422, "Invalid description", "description must be a string or null.")

    def _apply():
        with db_session() as s:
            tmpl = _load_template_or_404(s, template_id)
            _assert_template_owner(s, user_id, tmpl)
            before = {"name": tmpl.name, "visibility": tmpl.visibility,
                      "description": tmpl.description}
            if name is not None:
                tmpl.name = name
            if visibility is not None:
                tmpl.visibility = visibility
            if description is not False:
                tmpl.description = (description or "").strip() or None
            after = {"name": tmpl.name, "visibility": tmpl.visibility,
                     "description": tmpl.description}
            if after == before:
                return _summary(tmpl)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=tmpl.group_id,
                action="event.template.update",
                target=f"web_event_templates.{tmpl.id}",
                before=json.dumps(before),
                after=json.dumps(after),
            ))
            s.commit()
            return _summary(tmpl)

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))


@event_templates_bp.delete("/event-templates/<int:template_id>")
async def delete_event_template(template_id: int):
    """Soft-delete (active=false) — instantiated events are untouched."""
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            tmpl = _load_template_or_404(s, template_id)
            _assert_template_owner(s, user_id, tmpl)
            tmpl.active = False
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=tmpl.group_id,
                action="event.template.delete",
                target=f"web_event_templates.{tmpl.id}",
                before=json.dumps({"name": tmpl.name, "visibility": tmpl.visibility}),
                after=None,
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    return jsonify({"ok": True})
