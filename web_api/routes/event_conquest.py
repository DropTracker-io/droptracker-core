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
import time
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

    # Home tiles must name this event's teams.
    homes = {t["home_team_id"] for t in tiles_in if t.get("home_team_id") is not None}
    if homes:
        known_teams = {tid for (tid,) in s.query(EventTeam.id)
                       .filter(EventTeam.event_id == ev.id,
                               EventTeam.id.in_(homes)).all()}
        if homes - known_teams:
            abort_problem(422, "Unknown team",
                          "A home tile names a team that isn't in this event.")

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
                             label_x=r["label_x"], label_y=r["label_y"],
                             shape=r.get("shape"))
        s.add(row)
        s.flush()
        region_ids[r["key"]] = row.id

    used_task_ids = set()
    tile_ids = {}
    for i, t in enumerate(tiles_in):
        tile = ConquestTile(
            event_id=ev.id, region_id=region_ids.get(t["region_key"]), idx=i,
            label=t["label"], x=t["x"], y=t["y"], kind=t["kind"], value=t["value"],
            icon_npc_id=t["icon_npc_id"], icon_item_id=t["icon_item_id"],
            shape=t.get("shape"), max_defense=t.get("max_defense"),
            garrison=t.get("garrison"), home_team_id=t.get("home_team_id"),
            owner_team_id=None, defense=0, captures=0,
        )
        s.add(tile)
        s.flush()
        tile_ids[t["key"]] = tile.id
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
                               troops=rule["troops"], once=1 if rule.get("once") else 0,
                               sort=j))
    for a_key, b_key in clean.get("edges") or []:
        a, b = sorted((tile_ids[a_key], tile_ids[b_key]))
        s.add(ConquestEdge(event_id=ev.id, tile_a_id=a, tile_b_id=b))
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
                          "edges": len(clean.get("edges") or []),
                          "preset": preset, "tasks_removed": removed})[:250],
    ))
    s.flush()
    return {"regions": len(regions_in), "tiles": len(tiles_in),
            "edges": len(clean.get("edges") or []), "tasks_removed": removed}


def _apply_preset_art(s, ev, art) -> None:
    """A drawn preset brings its backdrop and the coordinate space of its
    territory outlines; a schematic one clears the outline space (it drew
    none) and leaves any uploaded art alone."""
    from services.conquest_engine import ensure_map

    map_row = ensure_map(s, ev.id)
    if not art:
        map_row.shape_width = map_row.shape_height = None
        return
    map_row.background_url = art["background_url"]
    map_row.bg_width = map_row.shape_width = int(art["width"])
    map_row.bg_height = map_row.shape_height = int(art["height"])


# The preset's own tiles (services.conquest_presets.EXTRA_TILES) resolved
# against this database; like the task generator's catalog, it changes weekly
# at most, so it is cached in-process.
_EXTRA_TILES_TTL_SECONDS = 900.0
_extra_tiles_cache: dict = {"ts": 0.0, "rows": None}


def _resolve_extra_tiles(s) -> list:
    """Look up everything EXTRA_TILES names (NPCs, WOM rates, collection-log
    pages, Clan Log sections, item spellings and ids) and resolve the rows."""
    from sqlalchemy import bindparam, text

    from services.conquest_presets import EXTRA_TILES, resolve_extra_tiles
    from web_api.routes.player_state import _collection_log_structure
    from web_api.task_generator_catalog import _wom_rates

    def expanding(sql, name):
        return text(sql).bindparams(bindparam(name, expanding=True))

    npc_names = sorted({n for t in EXTRA_TILES for n in t.get("kc_npcs") or []})
    npc_ids: dict = {}
    if npc_names:
        for nid, name in s.execute(
                expanding("SELECT npc_id, npc_name FROM npc_list WHERE npc_name IN :n", "n"),
                {"n": npc_names}):
            npc_ids.setdefault(str(name), []).append(int(nid))

    wanted_pages = {p for t in EXTRA_TILES for p in t.get("clog_pages") or []}
    clog_pages: dict = {}
    for tab in _collection_log_structure(s) or []:
        for page in (tab or {}).get("pages") or []:
            if page.get("name") in wanted_pages:
                clog_pages.setdefault(page["name"], []).extend(page.get("names") or [])

    slugs = sorted({slug for t in EXTRA_TILES for slug in t.get("sections") or []})
    sections: dict = {}
    if slugs:
        for slug, name in s.execute(
                expanding("SELECT c.slug, i.item_name FROM clan_log_items i "
                          "JOIN clan_log_sections c ON c.id = i.section_id "
                          "WHERE c.slug IN :s AND c.enabled = 1 AND i.enabled = 1 "
                          "ORDER BY c.sort_order, i.sort_order, i.id", "s"),
                {"s": slugs}):
            sections.setdefault(str(slug), []).append(str(name))

    names = {n for v in clog_pages.values() for n in v}
    names |= {n for v in sections.values() for n in v}
    names |= {n for t in EXTRA_TILES for n in t.get("uniques") or []}
    names |= {t["icon_item"] for t in EXTRA_TILES if t.get("icon_item")}
    items: dict = {}
    if names:
        for name, item_id in s.execute(
                expanding("SELECT item_name, MIN(item_id) FROM items "
                          "WHERE item_name IN :n GROUP BY item_name", "n"),
                {"n": sorted(names)}):
            items[str(name).lower()] = (str(name), int(item_id))

    return resolve_extra_tiles(npc_ids=npc_ids, wom_rates=_wom_rates(),
                               clog_pages=clog_pages, sections=sections, items=items)


def _preset_catalog(s) -> list:
    """The task generator's encounter rows plus the preset's own tiles."""
    from web_api.task_generator_catalog import load_catalog

    now = time.monotonic()
    extras = _extra_tiles_cache["rows"]
    if extras is None or now - _extra_tiles_cache["ts"] >= _EXTRA_TILES_TTL_SECONDS:
        extras = _resolve_extra_tiles(s)
        if extras:  # never cache an empty read (a mid-migration DB)
            _extra_tiles_cache.update(ts=now, rows=extras)
    return list(load_catalog(s)) + list(extras)


def _event_size(s, ev) -> tuple:
    """``(teams, players per team, days)`` for the troop-cost suggestion."""
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
    return max(team_count, 2), team_size, days


def _suggested_troop_hours(s, ev, tile_count: int) -> float:
    """The preset dialog's default troop cost for this event's size."""
    from services.conquest_presets import suggest_troop_hours

    return suggest_troop_hours(*_event_size(s, ev), tile_count)


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
    """What the designer's "start from a preset" panel offers: the preset's
    regions and tiles to pick from (and which this database can build), and a
    troop cost suggested for this event's size (teams × roster × days) at
    every tile count, so the dialog can follow the organiser's picks."""
    user_id = current_user_id()

    def _read():
        from services.conquest_presets import (
            DEFAULT_TROOP_HOURS,
            DEFAULT_UNIQUE_TROOPS,
            PRESETS,
            TROOP_HOURS_CHOICES,
            preset_regions,
            suggest_troop_hours,
        )

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            regions = preset_regions("gielinor", _preset_catalog(s))
            size = _event_size(s, ev)
            available = sum(t["available"] for r in regions for t in r["tiles"])
            by_tiles = [suggest_troop_hours(*size, n)
                        for n in range(sum(len(r["tiles"]) for r in regions) + 1)]
            return {
                "presets": [{"key": k, "label": v} for k, v in PRESETS.items()],
                "regions": regions,
                "troop_hours_choices": list(TROOP_HOURS_CHOICES),
                "default_troop_hours": DEFAULT_TROOP_HOURS,
                "suggested_troop_hours": by_tiles[available],
                "suggested_troop_hours_by_tiles": by_tiles,
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
    icon_item_id?, max_defense?, garrison?, home_team_id?,
    rules: [{task_id | new_task, troops?, once?}]}],
    edges: [[tile key, tile key]]}``.
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
    ``{preset: "gielinor", troop_hours?: number, unique_troops?: int,
    regions?: [key], exclude_tiles?: [key]}``. Every boss tile gets a kill
    rule sized to ``troop_hours`` of efficient kills and an any-unique rule
    worth ``unique_troops`` (see services.conquest_presets). ``regions``
    (default: all) picks the regions to build and ``exclude_tiles`` leaves
    tiles out of them.

    Anything short of the whole preset is drawn fresh for that pick
    (services.conquest_mapgen, ~20 s, cached per pick). Until it's ready
    this answers **202** ``{status: "generating" | "queued"}`` and nothing is
    written; the designer asks again every few seconds with the same body."""
    user_id = current_user_id()
    body = await json_body()
    from services.conquest_presets import (
        DEFAULT_UNIQUE_TROOPS,
        GIELINOR_REGIONS,
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
    region_keys = [r["key"] for r in GIELINOR_REGIONS]
    tile_keys = {k for r in GIELINOR_REGIONS for k in r["tiles"]}
    regions = body.get("regions")
    if regions is not None and (not isinstance(regions, list)
                                or not all(isinstance(k, str) for k in regions)
                                or set(regions) - set(region_keys)):
        abort_problem(422, "Invalid regions",
                      f"regions must be a list drawn from {region_keys}.")
    exclude = body.get("exclude_tiles") or []
    if (not isinstance(exclude, list) or not all(isinstance(k, str) for k in exclude)
            or set(exclude) - tile_keys):
        abort_problem(422, "Invalid tiles", "exclude_tiles must list the preset's tile keys.")
    picked = [k for r in GIELINOR_REGIONS if regions is None or r["key"] in regions
              for k in r["tiles"] if k not in exclude]
    if not picked:
        abort_problem(422, "Nothing to build", "Pick at least one tile.")

    def _plan():
        from services.conquest_presets import plan_selection

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_map_editable(ev)
            hours = troop_hours
            if hours is None:
                hours = _suggested_troop_hours(s, ev, len(picked))
            kept, drop, _skipped = plan_selection(
                _preset_catalog(s), troop_hours=hours, unique_troops=unique_troops,
                regions=regions, exclude=exclude)
            return hours, kept, drop

    hours, kept, drop = await asyncio.to_thread(_plan)
    if not kept:
        abort_problem(422, "Nothing to build",
                      "None of the picked tiles can be built on this server.")
    from services.conquest_presets import is_full_selection

    art = None
    if not is_full_selection(kept, drop):
        from services import conquest_mapgen

        status, art = await conquest_mapgen.ensure_art(kept, drop)
        if status == "unavailable":
            abort_problem(503, "Map drawing unavailable",
                          "This server can't redraw the map for a smaller pick right now. "
                          "Use every region and tile, or build the map by hand.")
        if status == "failed":
            key = conquest_mapgen.selection_key(kept, drop)
            abort_problem(502, "Map drawing failed",
                          "The map couldn't be drawn for this pick. Try again in a minute."
                          + (f" ({conquest_mapgen.failure_reason(key)})"
                             if conquest_mapgen.failure_reason(key) else ""))
        if art is None:
            return private_no_store(jsonify({
                "status": status,
                "message": ("Drawing your map…" if status == "generating"
                            else "Another map is being drawn. Yours is next…"),
            })), 202

    def _apply():
        from services.conquest_engine import conquest_payload
        from services.conquest_presets import build_preset_map

        with db_session() as s:
            ev = _load_conquest_event(s, event_id)
            _assert_event_admin(s, user_id, ev)
            _assert_map_editable(ev)
            body_map, skipped = build_preset_map(
                preset, _preset_catalog(s), troop_hours=hours,
                unique_troops=unique_troops, regions=regions, exclude=exclude, art=art)
            if not body_map["tiles"]:
                abort_problem(422, "Nothing to build",
                              "None of the picked tiles can be built on this server.")
            clean, errors = _cq().validate_map(body_map)
            if errors:  # a catalog row the validator rejects: say so, don't half-build
                abort_problem(500, "Preset failed validation", " ".join(errors[:5]))
            summary = _write_map(s, ev, user_id, clean, preset=preset)
            _apply_preset_art(s, ev, body_map.get("art"))
            s.commit()
            payload = conquest_payload(s, ev)
            payload["preset_summary"] = dict(summary, skipped=skipped,
                                             troop_hours=hours)
            payload["status"] = "ready"
            return payload

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


@event_conquest_bp.patch("/events/<int:event_id>/conquest/settings")
async def patch_conquest_settings(event_id: int):
    """Merge a partial settings document. Live-tunable: dice, defense caps
    and the summary cadence apply from the next troop / sweep. The start
    options only matter at activation. Switching the scoring mode re-scores
    the whole event, and is refused once the event is over."""
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
            problem = _cq().settings_change_problem(
                patch, _cq().conquest_settings(stored), ev.status)
            if problem:
                abort_problem(409, "Scoring is locked", problem)
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
