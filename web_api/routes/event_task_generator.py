"""Event task generator routes — "Fill for me" (docs/TASK_GENERATOR_PLAN.md).

  GET  /api/v1/events/{id}/tasks/generator
       -> { defaults, categories, kinds, encounters[], activity_available }
  POST /api/v1/events/{id}/tasks/generate   { criteria }
       -> { seed, capacity_hours, bounds, total_hours, shortfall, tasks[] }
       Preview only — writes nothing. Every returned task has already passed
       validate_task_payload, so saving it cannot 422.
  POST /api/v1/events/{id}/tasks/bulk       { tasks: [EventTaskInput] }
       -> { created: [EventTask], skipped: [str] }
       Creates many tasks in one transaction (the flat-list / board-game-pool
       save; the bingo designer writes generated tasks as `new_task` cells
       through PUT /events/{id}/bingo instead).

Event-admin gated like every other task write. The generator itself is pure
(services/task_generator.py); the catalog it sizes is assembled and cached in
web_api/task_generator_catalog.py.
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime

from quart import Blueprint, jsonify

from db import AuditLog, Event, EventTask, EventTeam, EventTeamMember
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import current_user_id, json_body

event_task_generator_bp = Blueprint("v1_event_task_generator", __name__)

_DIFFICULTIES = ("air", "water", "earth", "fire")
_MAX_BULK = 100
_MAX_VALIDATION_ROUNDS = 3


def _tg():
    from services import task_generator
    return task_generator


def _events():
    # Lazy — keeps this module importable under the conftest stubs and avoids
    # a cycle with the (large) events route module.
    from web_api.routes import events
    return events


def _event_defaults(s, ev: Event) -> dict:
    """Sensible starting criteria from the event itself."""
    days = 7.0
    if ev.starts_at and ev.ends_at and ev.ends_at > ev.starts_at:
        days = max(0.5, (ev.ends_at - ev.starts_at).total_seconds() / 86400.0)
    team_ids = [tid for (tid,) in s.query(EventTeam.id).filter(EventTeam.event_id == ev.id)]
    team_size = 5
    if team_ids:
        members = s.query(EventTeamMember).filter(
            EventTeamMember.event_id == ev.id).count()
        if members:
            team_size = max(1, round(members / len(team_ids)))
    kind = getattr(ev, "kind", None) or "standard"
    if kind == "bingo" or ev.has_bingo:
        size = int(ev.board_size or 5)
        count = size * size
    elif kind == "board_game":
        count = 40
    else:
        count = 12
    return {
        "count": count,
        "days": round(days, 1),
        "team_size": team_size,
        "activity": "normal",
        # Bingo reads best with a fat easy/medium middle and a few elites.
        "mix": {"air": 3, "water": 3, "earth": 2, "fire": 1},
        "clan_focus": "off",
    }


def _clean_criteria(body: dict) -> dict:
    """Validate the generate body → keyword args for task_generator.generate
    (minus catalog/activity, which the route supplies)."""
    tg = _tg()

    def _int(key, lo, hi, default):
        v = body.get(key, default)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= v <= hi):
            abort_problem(422, f"Invalid {key}", f"'{key}' must be a number {lo}–{hi}.")
        return v

    count = int(_int("count", 1, tg.MAX_GENERATE, 12))
    days = float(_int("days", 0.5, 120, 7))
    team_size = int(_int("team_size", 1, 500, 5))
    activity = body.get("activity") or "normal"
    if activity not in tg.ACTIVITY_HOURS_PER_DAY:
        abort_problem(422, "Invalid activity",
                      f"activity must be one of {list(tg.ACTIVITY_HOURS_PER_DAY)}.")
    raw_mix = body.get("mix") or {}
    if not isinstance(raw_mix, dict):
        abort_problem(422, "Invalid mix", "'mix' must map difficulty → weight.")
    mix = {}
    for t in _DIFFICULTIES:
        w = raw_mix.get(t, 0)
        if not isinstance(w, (int, float)) or isinstance(w, bool) or not (0 <= w <= 100):
            abort_problem(422, "Invalid mix", "Mix weights must be numbers 0–100.")
        mix[t] = float(w)

    def _str_list(key, allowed=None, cap=200):
        raw = body.get(key)
        if raw is None:
            return None
        if not isinstance(raw, list) or len(raw) > cap or not all(isinstance(x, str) for x in raw):
            abort_problem(422, f"Invalid {key}", f"'{key}' must be a list of strings.")
        if allowed is not None:
            bad = sorted({x for x in raw if x not in allowed})
            if bad:
                abort_problem(422, f"Invalid {key}", f"Unknown {key}: {', '.join(bad[:5])}.")
        return raw

    categories = _str_list("categories", set(tg.CONTENT_CATEGORIES))
    kinds = _str_list("kinds", set(tg.TASK_KINDS))
    enc_keys = set(tg.ENCOUNTER_BY_KEY)
    must = _str_list("must_include", enc_keys, cap=25) or []
    exclude_enc = _str_list("exclude_encounters", enc_keys) or []
    exclude_keys = _str_list("exclude_keys", cap=500) or []
    taken_keys = _str_list("taken_keys", cap=500) or []
    focus = body.get("clan_focus") or "off"
    if focus not in tg.CLAN_FOCUS_MODES:
        abort_problem(422, "Invalid clan_focus",
                      f"clan_focus must be one of {list(tg.CLAN_FOCUS_MODES)}.")
    only_tier = body.get("only_tier")
    if only_tier is not None and only_tier not in _DIFFICULTIES:
        abort_problem(422, "Invalid only_tier",
                      f"only_tier must be one of {list(_DIFFICULTIES)} or null.")
    seed = body.get("seed")
    if seed is None:
        seed = random.randrange(1, 2**31)
    elif not isinstance(seed, int) or isinstance(seed, bool) or not (0 <= seed < 2**31):
        abort_problem(422, "Invalid seed", "'seed' must be a non-negative integer.")
    return {
        "count": count,
        "capacity": tg.capacity_hours(team_size, days, activity),
        "mix": mix,
        "seed": seed,
        "categories": set(categories) if categories is not None else None,
        "kinds": set(kinds) if kinds is not None else None,
        "must_include": must,
        "exclude_encounters": exclude_enc,
        "exclude_keys": exclude_keys,
        "taken_keys": taken_keys,
        "clan_focus": focus,
        "only_tier": only_tier,
    }


@event_task_generator_bp.get("/events/<int:event_id>/tasks/generator")
async def generator_options(event_id: int):
    """What the fill form can offer for this event: defaults from the event,
    the category/kind vocabularies and every boss (with how many members of
    the owning clan were active there lately, when that's known)."""
    user_id = current_user_id()

    def _load():
        from web_api.task_generator_catalog import clan_activity, load_catalog

        ev_mod = _events()
        tg = _tg()
        with db_session() as s:
            ev = ev_mod._load_event_or_404(s, event_id)
            ev_mod._assert_event_admin(s, user_id, ev)
            catalog = load_catalog(s)
            activity, members = clan_activity(s, ev.group_id, catalog)
            return {
                "defaults": _event_defaults(s, ev),
                "categories": [{"key": k, "label": v} for k, v in tg.CONTENT_CATEGORIES.items()],
                "kinds": [{"key": k, "label": v} for k, v in tg.TASK_KINDS.items()],
                "difficulties": [{"key": k, "label": tg.DIFFICULTY_LABELS[k],
                                  "points": tg.DEFAULT_POINTS[k]} for k in _DIFFICULTIES],
                "encounters": [
                    {"key": row["key"], "label": row["label"], "category": row["category"],
                     "clan_players": int(activity.get(row["key"], 0))}
                    for row in catalog
                ],
                "activity_available": bool(activity),
                "clan_members": members,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@event_task_generator_bp.post("/events/<int:event_id>/tasks/generate")
async def generate_tasks(event_id: int):
    """Preview a generated task set. Writes nothing."""
    user_id = current_user_id()
    body = await json_body()
    criteria = _clean_criteria(body)

    def _run():
        from web_api.common import ProblemException
        from web_api.routes.event_task_validation import validate_task_payload
        from web_api.task_generator_catalog import clan_activity, load_catalog

        ev_mod = _events()
        tg = _tg()
        with db_session() as s:
            ev = ev_mod._load_event_or_404(s, event_id)
            ev_mod._assert_event_admin(s, user_id, ev)
            catalog = load_catalog(s)
            activity, members = ({}, 0)
            if criteria["clan_focus"] != "off":
                activity, members = clan_activity(s, ev.group_id, catalog)
            existing = [label for (label,) in s.query(EventTask.label)
                        .filter(EventTask.event_id == event_id) if label]

            want = criteria["count"]
            exclude = list(criteria["exclude_keys"])
            taken = list(criteria["taken_keys"])
            kept: list[dict] = []
            result = None
            for round_no in range(_MAX_VALIDATION_ROUNDS):
                result = tg.generate(
                    catalog,
                    count=want - len(kept),
                    capacity=criteria["capacity"],
                    mix=criteria["mix"],
                    seed=criteria["seed"] + round_no,
                    categories=criteria["categories"],
                    kinds=criteria["kinds"],
                    must_include=criteria["must_include"] if round_no == 0 else (),
                    exclude_keys=exclude,
                    exclude_encounters=criteria["exclude_encounters"],
                    exclude_labels=existing + [r["task"]["label"] for r in kept],
                    taken_keys=taken + [r["key"] for r in kept],
                    clan_focus=criteria["clan_focus"],
                    activity=activity,
                    roster=members,
                    only_tier=criteria["only_tier"],
                )
                failed = []
                for row in result["tasks"]:
                    try:
                        # A copy: the validator normalizes config in place.
                        validate_task_payload(s, json.loads(json.dumps(row["task"])))
                        kept.append(row)
                    except ProblemException:
                        # A catalog item the validator no longer knows (a
                        # renamed item, a retired NPC): drop it and refill.
                        failed.append(row["key"])
                if not failed:
                    break
                exclude += failed
            s.rollback()
            order = {t: i for i, t in enumerate(_DIFFICULTIES)}
            kept.sort(key=lambda r: (order[r["difficulty"]], r["hours"]))
            result = dict(result or {})
            result["seed"] = criteria["seed"]
            result["tasks"] = kept
            result["total_hours"] = round(sum(r["hours"] for r in kept), 1)
            result["requested"] = want
            return result

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


@event_task_generator_bp.post("/events/<int:event_id>/tasks/bulk")
async def bulk_create_tasks(event_id: int):
    """Create many tasks at once (a generated set). Each payload is validated
    exactly like POST /events/{id}/tasks; one that fails is skipped (and
    named in ``skipped``) instead of sinking the batch. Labels already in the
    event are skipped too, so a double-submit can't duplicate the set."""
    user_id = current_user_id()
    body = await json_body()
    raw = body.get("tasks")
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_BULK:
        abort_problem(422, "Invalid tasks",
                      f"'tasks' must be a list of 1–{_MAX_BULK} task objects.")

    def _apply():
        from web_api.common import ProblemException
        from web_api.routes.event_task_validation import MAX_TASK_POINTS, validate_task_payload

        ev_mod = _events()
        from db import EVENT_TASK_TYPES

        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            ev_mod._assert_event_admin(s, user_id, ev)
            ev_mod._assert_event_not_past(ev)
            existing = {label.strip().lower() for (label,) in s.query(EventTask.label)
                        .filter(EventTask.event_id == event_id) if label}
            created: list[dict] = []
            skipped: list[str] = []
            for item in raw:
                if not isinstance(item, dict):
                    skipped.append("(invalid)")
                    continue
                label = str(item.get("label") or "").strip()[:255]
                ttype = item.get("type")
                if not label or ttype not in EVENT_TASK_TYPES or ttype == "competition":
                    skipped.append(label or "(unnamed)")
                    continue
                if label.lower() in existing:
                    skipped.append(label)
                    continue
                points = item.get("points") or 0
                if (not isinstance(points, int) or isinstance(points, bool)
                        or not (0 <= points <= MAX_TASK_POINTS)):
                    points = 0
                difficulty = item.get("difficulty")
                if difficulty not in _DIFFICULTIES:
                    difficulty = None
                try:
                    normalized = validate_task_payload(s, json.loads(json.dumps(item)))
                except ProblemException:
                    skipped.append(label)
                    continue
                task = EventTask(
                    event_id=event_id,
                    type=ttype,
                    label=label,
                    points=points,
                    requires_confirmation=bool(item.get("requires_confirmation")),
                    # Generated tasks are the event's own; they never publish
                    # into the shared library.
                    visibility="private",
                    difficulty=difficulty,
                    **normalized,
                )
                s.add(task)
                s.flush()
                existing.add(label.lower())
                created.append({
                    "id": task.id,
                    "type": task.type,
                    "label": task.label,
                    "target": task.target or None,
                    "target_value": task.target_value,
                    "points": int(task.points or 0),
                    "requires_confirmation": bool(task.requires_confirmation),
                    "visibility": "private",
                    "difficulty": task.difficulty,
                    "config": task.config or None,
                })
            if created:
                s.add(AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
                    event_id=event_id,
                    action="event.task.bulk_create",
                    target=f"web_events.{event_id}",
                    after=json.dumps({
                        "source": "generator",
                        "created": len(created),
                        "skipped": len(skipped),
                        "task_ids": [t["id"] for t in created],
                        "at": datetime.utcnow().isoformat(timespec="seconds"),
                    }),
                ))
            s.commit()
            ev_mod._attach_task_tiles(s, created)
            return {"created": created, "skipped": skipped}

    result = await asyncio.to_thread(_apply)
    if result["created"]:
        _events()._bump(event_id)
    return jsonify(result)
