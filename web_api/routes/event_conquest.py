"""Conquest event routes (web120a).

  GET    /api/v1/events/{id}/conquest                     -> the map + live state
  GET    /api/v1/events/{id}/conquest/battles             -> one page of the battle log
  GET    /api/v1/events/{id}/conquest/presets             -> preset options (admins)
  PUT    /api/v1/events/{id}/conquest/map                 -> the designer's save
  POST   /api/v1/events/{id}/conquest/preset              -> build the map from a preset
  PATCH  /api/v1/events/{id}/conquest/settings            -> merge the settings JSON
  POST   /api/v1/events/{id}/conquest/background          -> upload map art (B2)
  DELETE /api/v1/events/{id}/conquest/background          -> back to the drawn map
  POST   /api/v1/events/{id}/conquest/tiles/{tid}/adjust  -> set owner/defense by hand

Auth mirrors the board game: reads are public once the event is visible
(private events: participants and admins only), writes need the event admin
gate. The map layout (regions, tiles, rules, art) locks when the event starts;
settings stay live-tunable. Game rules live in services/conquest.py, the DB
side in services/conquest_engine.py.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from quart import Blueprint, jsonify, request

from db import EventTask, EventTeam, EventTeamMember
from db.models import AuditLog, Event
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    current_user_id,
    json_body,
    optional_user_id,
    render_token_authorized,
)
from web_api.routes.events import (
    _assert_event_admin,
    _bump,
    _can_view_restricted,
    _deny_restricted,
    _is_restricted,
    _tasks_visible,
)

event_conquest_bp = Blueprint("v1_event_conquest", __name__)

# Marks tasks the designer (or a preset) created for a tile, so a later save
# can garbage-collect the ones no tile uses any more. Preserved through task
# validation (event_task_validation._PASSTHROUGH_KEYS).
CONQUEST_AUTO_KEY = "conquest_auto"
_BG_MAX_BYTES = 8 * 1024 * 1024
_BG_IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}


def _cq():
    from services import conquest

    return conquest


def _load_conquest_event(s, event_id: int):
    ev = s.query(Event).filter(Event.id == event_id).first()
    if not ev:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if (getattr(ev, "kind", None) or "standard") != "conquest":
        abort_problem(409, "Not a Conquest event",
                      "This event's format is not 'conquest'.")
    return ev


def _assert_map_editable(ev) -> None:
    """409 once the event has started (the board game's rule: a draft, or
    never activated with a start still in the future). Settings stay
    editable; the map itself would strand live ownership and holds."""
    if ev.status == "draft":
        return
    if ev.activated_at is None and (ev.starts_at is None or ev.starts_at > datetime.now()):
        return
    abort_problem(409, "Event has started",
                  "The Conquest map is locked once the event starts. Settings can "
                  "still be changed.")


def _read_gate(s, ev, viewer_id, render_bypass: bool) -> bool:
    """Apply the visibility rules; returns ``conceal`` (tile rules hidden
    from this viewer, web112a)."""
    if (_is_restricted(ev) and not render_bypass
            and not _can_view_restricted(s, viewer_id, ev)):
        _deny_restricted(ev, viewer_id)
        abort_problem(404, "Event not found", f"No event {ev.id}.")
    return not render_bypass and not _tasks_visible(s, viewer_id, ev)


def _auto_config(config) -> str:
    """The validated task config (JSON string or None) with the designer
    marker added."""
    parsed = {}
    if isinstance(config, str) and config:
        try:
            parsed = json.loads(config)
        except ValueError:
            parsed = {}
    elif isinstance(config, dict):
        parsed = dict(config)
    if not isinstance(parsed, dict):
        parsed = {}
    parsed[CONQUEST_AUTO_KEY] = True
    return json.dumps(parsed)


def _is_auto(task) -> bool:
    try:
        cfg = json.loads(task.config) if task.config else {}
    except ValueError:
        return False
    return isinstance(cfg, dict) and bool(cfg.get(CONQUEST_AUTO_KEY))


def _config_dict(config) -> dict:
    if isinstance(config, dict):
        return config
    if isinstance(config, str) and config:
        try:
            parsed = json.loads(config)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# --------------------------------------------------------------------------- #
# The map write (designer save + presets)
# --------------------------------------------------------------------------- #
def _write_map(s, ev, user_id: int, clean: dict, *, preset=None,
               keep_preset: bool = True) -> dict:
    """Replace the whole map with ``clean`` (a services.conquest.validate_map
    result). Rules either reuse an existing event task (``task_id``) or create
    one (``new_task``, validated exactly like the task builder's). Tasks the
    designer created earlier and no tile uses any more are deleted. The
    caller owns the commit."""
    from db.models import (
        ConquestEdge,
        ConquestRegion,
        ConquestRule,
        ConquestTile,
        EventCompletion,
        EventProgress,
    )
    from services.conquest_engine import ensure_map
    from web_api.routes.event_task_validation import validate_task_payload

    cq = _cq()
    tiles_in = clean["tiles"]
    regions_in = clean["regions"]

    # Existing tasks: must be this event's and able to drive a tile.
    wanted = [r["task_id"] for t in tiles_in for r in t["rules"] if r["task_id"]]
    if len(wanted) != len(set(wanted)):
        abort_problem(422, "Task used twice",
                      "A task can drive only one tile rule. Duplicate it for the "
                      "other tile instead.")
    existing = {}
    if wanted:
        existing = {t.id: t for t in s.query(EventTask)
                    .filter(EventTask.event_id == ev.id,
                            EventTask.id.in_(wanted)).all()}
    missing = sorted(set(wanted) - set(existing))
    if missing:
        abort_problem(422, "Unknown task",
                      f"Task(s) {missing} don't belong to this event.")
    for task in existing.values():
        problem = cq.rule_task_problem(task.type, _config_dict(task.config))
        if problem:
            abort_problem(422, "Task can't drive a tile", f"{task.label}: {problem}")

    # New tasks: validate them all before touching any rows.
    prepared = {}
    for t in tiles_in:
        for j, rule in enumerate(t["rules"]):
            nt = rule.get("new_task")
            if nt is None:
                continue
            problem = cq.rule_task_problem(nt.get("type"), _config_dict(nt.get("config")))
            if problem:
                abort_problem(422, "Task can't drive a tile", f"{t['label']}: {problem}")
            label = str(nt.get("label") or "").strip()
            if not label:
                abort_problem(422, "Invalid new_task",
                              f"{t['label']}: every new task needs a label.")
            prepared[(t["key"], j)] = (label[:255], validate_task_payload(s, nt), nt)

    # Replace. Nothing holds state yet (the map is draft-only), so the old
    # rows simply go; rules first (they reference tiles and tasks).
    old_rule_task_ids = {tid for (tid,) in s.query(ConquestRule.task_id)
                         .filter(ConquestRule.event_id == ev.id).all()}
    for model in (ConquestRule, ConquestEdge, ConquestTile, ConquestRegion):
        (s.query(model).filter(model.event_id == ev.id)
         .delete(synchronize_session=False))
    s.flush()

    region_ids = {}
    for i, r in enumerate(regions_in):
        row = ConquestRegion(event_id=ev.id, name=r["name"], color=r["color"],
                             bonus=r["bonus"], sort=i,
                             label_x=r["label_x"], label_y=r["label_y"])
        s.add(row)
        s.flush()
        region_ids[r["key"]] = row.id

    used_task_ids = set()
    for i, t in enumerate(tiles_in):
        tile = ConquestTile(
            event_id=ev.id, region_id=region_ids.get(t["region_key"]), idx=i,
            label=t["label"], x=t["x"], y=t["y"], kind=t["kind"], value=t["value"],
            icon_npc_id=t["icon_npc_id"], icon_item_id=t["icon_item_id"],
            owner_team_id=None, defense=0, captures=0,
        )
        s.add(tile)
        s.flush()
        for j, rule in enumerate(t["rules"]):
            if rule["task_id"]:
                task_id = rule["task_id"]
            else:
                label, normalized, nt = prepared[(t["key"], j)]
                task = EventTask(
                    event_id=ev.id, type=nt["type"], label=label,
                    target=normalized["target"],
                    target_value=normalized["target_value"],
                    points=0,
                    requires_confirmation=bool(nt.get("requires_confirmation")),
                    # Designer tasks stay out of the public task library.
                    visibility="private",
                    config=_auto_config(normalized["config"]),
                )
                s.add(task)
                s.flush()
                task_id = task.id
            used_task_ids.add(task_id)
            s.add(ConquestRule(event_id=ev.id, tile_id=tile.id, task_id=task_id,
                               troops=rule["troops"], sort=j))
    s.flush()

    # Garbage-collect designer tasks no tile uses any more (never ones that
    # already carry ledger rows — a draft shouldn't, but be safe).
    dropped = old_rule_task_ids - used_task_ids
    removed = 0
    if dropped:
        for task in s.query(EventTask).filter(EventTask.id.in_(dropped)).all():
            if not _is_auto(task):
                continue
            if (s.query(EventCompletion.id)
                    .filter(EventCompletion.task_id == task.id).first()):
                continue
            (s.query(EventProgress).filter(EventProgress.task_id == task.id)
             .delete(synchronize_session=False))
            s.delete(task)
            removed += 1

    map_row = ensure_map(s, ev.id)
    map_row.revision = int(map_row.revision or 0) + 1
    if preset is not None or not keep_preset:
        map_row.preset = preset
    s.add(AuditLog(
        actor_user_id=user_id, group_id=ev.group_id, event_id=ev.id,
        action="event.conquest.map", target=str(ev.id),
        after=json.dumps({"regions": len(regions_in), "tiles": len(tiles_in),
                          "preset": preset, "tasks_removed": removed})[:250],
    ))
    s.flush()
    return {"regions": len(regions_in), "tiles": len(tiles_in),
            "tasks_removed": removed}


def _suggested_troop_hours(s, ev, tile_count: int) -> float:
    """The preset dialog's default troop cost for this event's size."""
    from services.conquest_presets import suggest_troop_hours

    team_ids = [tid for (tid,) in s.query(EventTeam.id)
                .filter(EventTeam.event_id == ev.id).all()]
    team_count = len(team_ids)
    members = 0
    if team_ids:
        members = (s.query(EventTeamMember)
                   .filter(EventTeamMember.team_id.in_(team_ids)).count())
    if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
        from db.models import EventGroup

        clans = (s.query(EventGroup)
                 .filter(EventGroup.event_id == ev.id,
                         EventGroup.status == "accepted").count())
        team_count = max(team_count, clans)
    team_size = round(members / team_count) if team_count and members else 10
    days = 7.0
    if ev.starts_at and ev.ends_at and ev.ends_at > ev.starts_at:
        days = (ev.ends_at - ev.starts_at).total_seconds() / 86400.0
    return suggest_troop_hours(max(team_count, 2), team_size, days, tile_count)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@event_conquest_bp.get("/events/<int:event_id>/conquest")
async def get_conquest(event_id: int):
    viewer_id = optional_user_id()
    render_bypass = render_token_authorized()

    def _read():
        from services.conquest_engine import conquest_payload

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            conceal = _read_gate(s, ev, viewer_id, render_bypass)
            return conquest_payload(s, ev, conceal=conceal)

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


@event_conquest_bp.get("/events/<int:event_id>/conquest/battles")
async def get_conquest_battles(event_id: int):
    viewer_id = optional_user_id()

    def _int_arg(name):
        raw = (request.args.get(name) or "").strip()
        return int(raw) if raw.isdigit() else None

    before_id, tile_id, team_id = _int_arg("before"), _int_arg("tile_id"), _int_arg("team_id")
    limit = _int_arg("limit") or 50

    def _read():
        from services.conquest_engine import battles_page

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _read_gate(s, ev, viewer_id, False)
            return battles_page(s, ev.id, before_id=before_id, limit=limit,
                                tile_id=tile_id, team_id=team_id)

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


@event_conquest_bp.get("/events/<int:event_id>/conquest/presets")
async def get_conquest_presets(event_id: int):
    """What the designer's "start from a preset" panel offers, with a troop
    cost suggested for this event's size (teams × roster × days)."""
    user_id = current_user_id()

    def _read():
        from services.conquest_presets import (
            DEFAULT_TROOP_HOURS,
            DEFAULT_UNIQUE_TROOPS,
            GIELINOR_REGIONS,
            PRESETS,
            TROOP_HOURS_CHOICES,
        )

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            tiles = sum(len(r["tiles"]) for r in GIELINOR_REGIONS)
            return {
                "presets": [{"key": k, "label": v} for k, v in PRESETS.items()],
                "troop_hours_choices": list(TROOP_HOURS_CHOICES),
                "default_troop_hours": DEFAULT_TROOP_HOURS,
                "suggested_troop_hours": _suggested_troop_hours(s, ev, tiles),
                "default_unique_troops": DEFAULT_UNIQUE_TROOPS,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
@event_conquest_bp.put("/events/<int:event_id>/conquest/map")
async def put_conquest_map(event_id: int):
    """Replace the whole map (the designer's save). Body:
    ``{revision?, regions: [{key, name, color?, bonus?, label_x?, label_y?}],
    tiles: [{key, label, x, y, kind?, value?, region_key?, icon_npc_id?,
    icon_item_id?, rules: [{task_id | new_task, troops?}]}]}``.
    ``revision`` (the payload's last-read value) guards against two open
    editors overwriting each other: a stale one gets a 409."""
    user_id = current_user_id()
    body = await json_body()
    clean, errors = _cq().validate_map(body)
    if errors:
        abort_problem(422, "Invalid map", " ".join(errors[:5]),
                      extra={"errors": errors[:20]})
    revision = body.get("revision")

    def _apply():
        from services.conquest_engine import conquest_payload, load_map

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_map_editable(ev)
            map_row = load_map(s, ev.id)
            current = int(map_row.revision or 0) if map_row is not None else 0
            if isinstance(revision, int) and not isinstance(revision, bool) \
                    and revision != current:
                abort_problem(409, "Map changed",
                              "Someone else saved this map since you opened it. "
                              "Reload to see their changes.",
                              extra={"revision": current})
            _write_map(s, ev, user_id, clean)
            s.commit()
            return conquest_payload(s, ev)

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


@event_conquest_bp.post("/events/<int:event_id>/conquest/preset")
async def apply_conquest_preset(event_id: int):
    """Build the map from a preset, replacing the current one. Body:
    ``{preset: "gielinor", troop_hours?: number, unique_troops?: int}``.
    Every tile gets a kill rule sized to ``troop_hours`` of efficient kills
    and an any-unique rule worth ``unique_troops``."""
    user_id = current_user_id()
    body = await json_body()
    from services.conquest_presets import (
        DEFAULT_UNIQUE_TROOPS,
        PRESETS,
        TROOP_HOURS_CHOICES,
    )

    preset = body.get("preset") or "gielinor"
    if preset not in PRESETS:
        abort_problem(422, "Unknown preset", f"preset must be one of {list(PRESETS)}.")
    troop_hours = body.get("troop_hours")
    if troop_hours is not None:
        try:
            troop_hours = float(troop_hours)
        except (TypeError, ValueError):
            troop_hours = -1
        if troop_hours not in TROOP_HOURS_CHOICES:
            abort_problem(422, "Invalid troop cost",
                          f"troop_hours must be one of {list(TROOP_HOURS_CHOICES)}.")
    unique_troops = body.get("unique_troops", DEFAULT_UNIQUE_TROOPS)
    if (isinstance(unique_troops, bool) or not isinstance(unique_troops, int)
            or not 0 <= unique_troops <= _cq().MAX_TROOPS_PER_RULE):
        abort_problem(422, "Invalid unique troops",
                      f"unique_troops must be 0 to {_cq().MAX_TROOPS_PER_RULE}.")

    def _apply():
        from services.conquest_engine import conquest_payload
        from services.conquest_presets import GIELINOR_REGIONS, build_preset_map
        from web_api.task_generator_catalog import load_catalog

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_map_editable(ev)
            hours = troop_hours
            if hours is None:
                tiles = sum(len(r["tiles"]) for r in GIELINOR_REGIONS)
                hours = _suggested_troop_hours(s, ev, tiles)
            body_map, skipped = build_preset_map(
                preset, load_catalog(s), troop_hours=hours,
                unique_troops=max(unique_troops, 1))
            if unique_troops == 0:
                for tile in body_map["tiles"]:
                    tile["rules"] = tile["rules"][:1]
            clean, errors = _cq().validate_map(body_map)
            if errors:  # a catalog row the validator rejects: say so, don't half-build
                abort_problem(500, "Preset failed validation", " ".join(errors[:5]))
            summary = _write_map(s, ev, user_id, clean, preset=preset)
            s.commit()
            payload = conquest_payload(s, ev)
            payload["preset_summary"] = dict(summary, skipped=skipped,
                                             troop_hours=hours)
            return payload

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


@event_conquest_bp.patch("/events/<int:event_id>/conquest/settings")
async def patch_conquest_settings(event_id: int):
    """Merge a partial settings document. Live-tunable: dice, defense caps
    and the summary cadence apply from the next troop / sweep. The start
    options only matter at activation."""
    user_id = current_user_id()
    body = await json_body()
    patch, errors = _cq().clean_settings_patch(body)
    if errors:
        abort_problem(422, "Invalid settings", " ".join(errors),
                      extra={"errors": errors})
    if not patch:
        abort_problem(422, "Empty patch", "No recognized settings keys in the body.")

    def _apply():
        from services.conquest_engine import ensure_map

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            map_row = ensure_map(s, ev.id)
            stored = _config_dict(map_row.settings)
            stored.update(patch)
            settings = _cq().conquest_settings(stored)
            map_row.settings = json.dumps(settings)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id, event_id=ev.id,
                action="event.conquest.settings", target=str(ev.id),
                after=json.dumps(patch)[:250],
            ))
            s.commit()
            return settings

    settings = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"settings": settings}))


@event_conquest_bp.post("/events/<int:event_id>/conquest/background")
async def upload_conquest_background(event_id: int):
    """Map art → B2 (server-side put, the board-game background pattern).
    Stores the URL and intrinsic size so tiles keep their fractional
    positions at any render size."""
    user_id = current_user_id()
    files = await request.files
    upload = files.get("file")
    if upload is None:
        abort_problem(422, "Invalid body", "A multipart 'file' field is required.")
    raw = upload.read()
    if not raw:
        abort_problem(422, "Empty file", "The uploaded image was empty.")
    if len(raw) > _BG_MAX_BYTES:
        abort_problem(422, "File too large", "Map images are capped at 8 MB.")

    import io

    from PIL import Image, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(raw))
        fmt_name = im.format
        width, height = im.size
        im.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG or WebP image.")
    fmt = _BG_IMAGE_FORMATS.get(fmt_name or "")
    if fmt is None:
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG or WebP image.")
    ext, content_type = fmt

    def _check():
        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_map_editable(ev)
            return ev.group_id

    group_id = await asyncio.to_thread(_check)

    key = f"dt_uploads/conquest/{event_id}-{uuid.uuid4().hex[:12]}.{ext}"
    try:
        from utils.b2_storage import upload_bytes
        from web_api.routes.submissions import B2_CDN_BASE_URL

        await upload_bytes(raw, key, content_type)
        public_url = f"{B2_CDN_BASE_URL.rstrip('/')}/{key}"
    except Exception as e:
        abort_problem(502, "Upload service unavailable", str(e))

    def _save():
        from services.conquest_engine import ensure_map

        with db_session() as s:
            map_row = ensure_map(s, event_id)
            map_row.background_url = public_url
            map_row.bg_width = width
            map_row.bg_height = height
            s.add(AuditLog(
                actor_user_id=user_id, group_id=group_id, event_id=event_id,
                action="event.conquest.background", target=str(event_id),
                after=public_url[:250],
            ))
            s.commit()

    await asyncio.to_thread(_save)
    _bump(event_id)
    return private_no_store(jsonify({
        "background_url": public_url, "bg_width": width, "bg_height": height,
    }))


@event_conquest_bp.delete("/events/<int:event_id>/conquest/background")
async def clear_conquest_background(event_id: int):
    """Drop the uploaded art: the site draws the schematic map again."""
    user_id = current_user_id()

    def _apply():
        from services.conquest_engine import ensure_map

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_map_editable(ev)
            map_row = ensure_map(s, ev.id)
            map_row.background_url = None
            map_row.bg_width = None
            map_row.bg_height = None
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id, event_id=ev.id,
                action="event.conquest.background", target=str(ev.id),
                after="cleared",
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"background_url": None}))


@event_conquest_bp.post("/events/<int:event_id>/conquest/tiles/<int:tile_id>/adjust")
async def adjust_conquest_tile(event_id: int, tile_id: int):
    """Set a tile's owner and/or defense by hand while the event is live
    (fixing a mistake). Body: ``{owner_team_id: int | null, defense?: int}``.
    Logged in the battle log and the audit log; the next sweep re-scores."""
    user_id = current_user_id()
    body = await json_body()
    if "owner_team_id" not in body:
        abort_problem(422, "Invalid body", "owner_team_id is required (null = nobody).")
    owner = body.get("owner_team_id")
    if owner is not None and (isinstance(owner, bool) or not isinstance(owner, int)):
        abort_problem(422, "Invalid owner", "owner_team_id must be a team id or null.")
    defense = body.get("defense")
    if defense is not None and (isinstance(defense, bool) or not isinstance(defense, int)):
        abort_problem(422, "Invalid defense", "defense must be a whole number.")

    def _apply():
        from services.conquest_engine import AdjustError, adjust_tile

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            if ev.status != "active":
                abort_problem(409, "Event not live",
                              "Tiles can only be adjusted while the event is running.")
            try:
                result = adjust_tile(s, ev, tile_id, owner_team_id=owner,
                                     defense=defense, actor_user_id=user_id)
            except AdjustError as exc:
                abort_problem(422, "Invalid adjustment", str(exc))
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id, event_id=ev.id,
                action="event.conquest.adjust", target=str(tile_id),
                after=json.dumps({"owner_team_id": owner, "defense": defense})[:250],
            ))
            s.commit()
            return result

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))
