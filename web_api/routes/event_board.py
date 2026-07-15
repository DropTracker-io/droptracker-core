"""Board-game event routes (web44a, P1).

  GET   /api/v1/events/{id}/board            -> BoardDetail (tiles + config +
                                                 team positions; the game view
                                                 and the designer both read it)
  PUT   /api/v1/events/{id}/board            -> replace tiles + background ref
                                                 (the designer's autosave —
                                                 put_bingo_board's shape)
  PATCH /api/v1/events/{id}/board/settings   -> merge the §2.5 settings JSON
                                                 (editable mid-event: leaders
                                                 keep full control)
  POST  /api/v1/events/{id}/board/background -> upload the board image (B2)
  POST  /api/v1/events/{id}/board/roll       -> manual dice roll for the
                                                 caller's team ({team_id} for
                                                 admins rolling on behalf)

Auth mirrors the bingo board: writes require the event admin gate; reads are
public once the event is visible (drafts: participants/admins only via
events._can_view_draft). The board layout locks when the event starts —
settings stay live-tunable by design.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from quart import Blueprint, jsonify, request

from db import (
    EVENT_BOARD_TILE_KINDS,
    EVENT_TASK_DIFFICULTIES,
    EventBoardConfig,
    EventBoardPosition,
    EventBoardTile,
    EventProgress,
    EventTask,
    EventTaskLibraryItem,
    EventTeam,
    EventTeamMember,
    Player,
)
from db.models import AuditLog, Event
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    optional_user_id,
)
from web_api.routes.event_admin import _assert_board_editable
from web_api.routes.events import (
    _assert_event_admin,
    _bump,
    _can_view_draft,
    _effective_status,
    _is_event_admin,
)

event_board_bp = Blueprint("v1_event_board", __name__)

_BOARD_PIN_KEY = "board_pin_auto"  # library-bound pinned tasks the designer created
_MAX_TILES = 512
_BG_MAX_BYTES = 8 * 1024 * 1024
_BG_IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}

# --------------------------------------------------------------------------- #
# Settings validation (§2.5) — known keys only, everything optional.
# --------------------------------------------------------------------------- #
_MOVEMENT_MODES = ("dice", "fixed_step")
_MOVEMENT_TRIGGERS = ("auto", "manual")
_MANUAL_ROLLERS = ("team", "group_admin", "either")
_TILE_RENDER_MODES = ("rune", "invisible", "outline")
_WIN_RULES = ("finish_tile",)  # P1; threshold/time-boxed variants later


def _clean_int(value, lo, hi, name):
    if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
        abort_problem(422, "Invalid settings", f"'{name}' must be an integer {lo}–{hi}.")
    return value


def _validate_settings_patch(body: dict) -> dict:
    """Whitelist-validate a partial §2.5 settings document. Returns only the
    recognized keys — unknown sections/keys are dropped, bad values 422."""
    out: dict = {}
    if not isinstance(body, dict):
        abort_problem(422, "Invalid settings", "Body must be a JSON object.")

    movement = body.get("movement")
    if movement is not None:
        if not isinstance(movement, dict):
            abort_problem(422, "Invalid settings", "'movement' must be an object.")
        m: dict = {}
        if "mode" in movement:
            if movement["mode"] not in _MOVEMENT_MODES:
                abort_problem(422, "Invalid settings",
                              f"movement.mode must be one of {list(_MOVEMENT_MODES)}.")
            m["mode"] = movement["mode"]
        if "dice_count" in movement:
            m["dice_count"] = _clean_int(movement["dice_count"], 1, 8, "movement.dice_count")
        if "dice_sides" in movement:
            m["dice_sides"] = _clean_int(movement["dice_sides"], 2, 100, "movement.dice_sides")
        if "fixed_step" in movement:
            m["fixed_step"] = _clean_int(movement["fixed_step"], 1, 20, "movement.fixed_step")
        if "trigger" in movement:
            if movement["trigger"] not in _MOVEMENT_TRIGGERS:
                abort_problem(422, "Invalid settings",
                              f"movement.trigger must be one of {list(_MOVEMENT_TRIGGERS)}.")
            m["trigger"] = movement["trigger"]
        if "manual_roller" in movement:
            if movement["manual_roller"] not in _MANUAL_ROLLERS:
                abort_problem(422, "Invalid settings",
                              f"movement.manual_roller must be one of {list(_MANUAL_ROLLERS)}.")
            m["manual_roller"] = movement["manual_roller"]
        if m:
            out["movement"] = m

    render = body.get("tile_render")
    if render is not None:
        if not isinstance(render, dict):
            abort_problem(422, "Invalid settings", "'tile_render' must be an object.")
        t: dict = {}
        if "mode" in render:
            if render["mode"] not in _TILE_RENDER_MODES:
                abort_problem(422, "Invalid settings",
                              f"tile_render.mode must be one of {list(_TILE_RENDER_MODES)}.")
            t["mode"] = render["mode"]
        if "outline_width" in render:
            t["outline_width"] = _clean_int(render["outline_width"], 1, 12,
                                            "tile_render.outline_width")
        if "outline_color" in render:
            color = render["outline_color"]
            if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
                abort_problem(422, "Invalid settings",
                              "tile_render.outline_color must be '#rrggbb'.")
            t["outline_color"] = color
        if "show_labels" in render:
            t["show_labels"] = bool(render["show_labels"])
        if t:
            out["tile_render"] = t

    coins = body.get("coins")
    if coins is not None:
        if not isinstance(coins, dict):
            abort_problem(422, "Invalid settings", "'coins' must be an object.")
        c: dict = {}
        if "enabled" in coins:
            c["enabled"] = bool(coins["enabled"])
        if "starting" in coins:
            c["starting"] = _clean_int(coins["starting"], 0, 1_000_000, "coins.starting")
        if "default" in coins:
            c["default"] = _clean_int(coins["default"], 0, 1_000_000, "coins.default")
        if "per_difficulty" in coins:
            ladder = coins["per_difficulty"]
            if not isinstance(ladder, dict):
                abort_problem(422, "Invalid settings", "coins.per_difficulty must be an object.")
            clean = {}
            for tier, val in ladder.items():
                if tier not in EVENT_TASK_DIFFICULTIES:
                    abort_problem(422, "Invalid settings",
                                  f"Unknown difficulty '{tier}' in coins.per_difficulty.")
                clean[tier] = _clean_int(val, 0, 1_000_000, f"coins.per_difficulty.{tier}")
            c["per_difficulty"] = clean
        if c:
            out["coins"] = c

    mercy = body.get("mercy")
    if mercy is not None:
        if not isinstance(mercy, dict):
            abort_problem(422, "Invalid settings", "'mercy' must be an object.")
        me: dict = {}
        if "enabled" in mercy:
            me["enabled"] = bool(mercy["enabled"])
        if "base_hours" in mercy:
            me["base_hours"] = _clean_int(mercy["base_hours"], 1, 24 * 14, "mercy.base_hours")
        if "step_hours" in mercy:
            me["step_hours"] = _clean_int(mercy["step_hours"], 0, 24 * 7, "mercy.step_hours")
        if me:
            out["mercy"] = me

    shop = body.get("shop")
    if shop is not None:
        if not isinstance(shop, dict):
            abort_problem(422, "Invalid settings", "'shop' must be an object.")
        sh: dict = {}
        if "enabled" in shop:
            sh["enabled"] = bool(shop["enabled"])
        if sh:
            out["shop"] = sh

    win = body.get("win")
    if win is not None:
        if not isinstance(win, dict):
            abort_problem(422, "Invalid settings", "'win' must be an object.")
        w: dict = {}
        if "rule" in win:
            if win["rule"] not in _WIN_RULES:
                abort_problem(422, "Invalid settings",
                              f"win.rule must be one of {list(_WIN_RULES)}.")
            w["rule"] = win["rule"]
        if w:
            out["win"] = w

    return out


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _tile_row(t: EventBoardTile, task_labels: dict) -> dict:
    return {
        "idx": int(t.idx),
        "x": float(t.x or 0.0),
        "y": float(t.y or 0.0),
        "label": t.label or None,
        "difficulty": t.difficulty or None,
        "task_id": t.task_id,
        "task_label": task_labels.get(t.task_id) if t.task_id else None,
        "tile_kind": t.tile_kind or "normal",
    }


def _position_row(s, pos: EventBoardPosition, team: EventTeam) -> dict:
    task = None
    if pos.current_task_id:
        t = s.query(EventTask).filter(EventTask.id == pos.current_task_id).first()
        if t is not None:
            progress = (s.query(EventProgress)
                        .filter(EventProgress.task_id == t.id,
                                EventProgress.team_id == pos.team_id)
                        .first())
            from services.event_engine import completion_threshold

            task = {
                "id": t.id,
                "label": t.label,
                "type": t.type,
                "difficulty": t.difficulty,
                "progress": int(progress.progress or 0) if progress else 0,
                "target": completion_threshold({
                    "type": t.type, "target_value": t.target_value}),
            }
    last_roll = None
    if pos.last_roll:
        try:
            last_roll = json.loads(pos.last_roll)
        except (TypeError, ValueError):
            last_roll = None
    return {
        "team_id": pos.team_id,
        "team_name": team.name if team else f"Team {pos.team_id}",
        "color": team.color if team else None,
        "piece_item_id": getattr(team, "piece_item_id", None),
        "piece_icon_url": getattr(team, "piece_icon_url", None),
        "coins": int(getattr(team, "coins", 0) or 0),
        "score": int(team.score or 0) if team else 0,
        "tile_idx": int(pos.tile_idx or 0),
        "status": pos.status or "active",
        "turns_completed": int(pos.turns_completed or 0),
        "current_task": task,
        "last_roll": last_roll,
        "mercy_deadline": (int(pos.mercy_deadline.timestamp())
                           if pos.mercy_deadline else None),
    }


def _board_payload(s, ev) -> dict:
    from services.boardgame_engine import board_settings, finish_idx

    config = (s.query(EventBoardConfig)
              .filter(EventBoardConfig.event_id == ev.id).first())
    tiles = (s.query(EventBoardTile)
             .filter(EventBoardTile.event_id == ev.id)
             .order_by(EventBoardTile.idx).all())
    task_ids = [t.task_id for t in tiles if t.task_id]
    task_labels = {}
    if task_ids:
        task_labels = dict(
            s.query(EventTask.id, EventTask.label)
            .filter(EventTask.id.in_(task_ids)).all())

    teams = {t.id: t for t in s.query(EventTeam)
             .filter(EventTeam.event_id == ev.id).all()}
    positions = (s.query(EventBoardPosition)
                 .filter(EventBoardPosition.event_id == ev.id).all())

    return {
        "event_id": ev.id,
        "background_url": config.background_url if config else None,
        "bg_width": config.bg_width if config else None,
        "bg_height": config.bg_height if config else None,
        "settings": board_settings(config.settings if config else None),
        "tiles": [_tile_row(t, task_labels) for t in tiles],
        "finish_idx": finish_idx(tiles),
        "positions": [
            _position_row(s, p, teams.get(p.team_id)) for p in positions
        ],
    }


def _load_board_event(s, event_id: int, *, for_write: bool):
    ev = s.query(Event).filter(Event.id == event_id).first()
    if not ev:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if (getattr(ev, "kind", None) or "standard") != "board_game":
        abort_problem(409, "Not a board-game event",
                      "This event's format is not 'board_game'.")
    return ev


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@event_board_bp.get("/events/<int:event_id>/board")
async def get_board(event_id: int):
    viewer_id = optional_user_id()

    def _read():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=False)
            if _effective_status(ev) == "draft" and not _can_view_draft(s, viewer_id, ev):
                abort_problem(404, "Event not found", f"No event {event_id}.")
            return _board_payload(s, ev)

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


@event_board_bp.put("/events/<int:event_id>/board")
async def put_board(event_id: int):
    """Replace the whole tile layout (the designer's autosave). Body:
    { background_url?, bg_width?, bg_height?,
      tiles: [{idx, x, y, label?, difficulty?, task_id?, library_item_id?,
               tile_kind?}] }
    Exactly one of difficulty / task_id / library_item_id per tile (or none =
    rest tile). idx must cover 0..N-1 uniquely."""
    user_id = current_user_id()
    body = await json_body()
    tiles_in = body.get("tiles")
    if not isinstance(tiles_in, list):
        abort_problem(422, "Invalid body", "'tiles' must be an array.")
    if len(tiles_in) > _MAX_TILES:
        abort_problem(422, "Too many tiles", f"Boards are capped at {_MAX_TILES} tiles.")

    seen_idx = set()
    for cell in tiles_in:
        if not isinstance(cell, dict):
            abort_problem(422, "Invalid tile", "Each tile must be an object.")
        idx = cell.get("idx")
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            abort_problem(422, "Invalid tile", "'idx' must be a non-negative integer.")
        if idx in seen_idx:
            abort_problem(422, "Invalid tile", f"Duplicate tile idx {idx}.")
        seen_idx.add(idx)
        for axis in ("x", "y"):
            v = cell.get(axis)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0.0 <= float(v) <= 1.0):
                abort_problem(422, "Invalid tile",
                              f"'{axis}' must be a number between 0 and 1 (tile {idx}).")
        bindings = [k for k in ("difficulty", "task_id", "library_item_id")
                    if cell.get(k) is not None]
        if len(bindings) > 1:
            abort_problem(422, "Invalid tile",
                          f"Tile {idx} may set only one of difficulty / task_id / "
                          "library_item_id.")
        if cell.get("difficulty") is not None and \
                cell["difficulty"] not in EVENT_TASK_DIFFICULTIES:
            abort_problem(422, "Invalid tile",
                          f"difficulty must be one of {list(EVENT_TASK_DIFFICULTIES)}.")
        kind = cell.get("tile_kind") or "normal"
        if kind not in EVENT_BOARD_TILE_KINDS:
            abort_problem(422, "Invalid tile",
                          f"tile_kind must be one of {list(EVENT_BOARD_TILE_KINDS)}.")
    if seen_idx and seen_idx != set(range(len(tiles_in))):
        abort_problem(422, "Invalid tiles",
                      "Tile idx values must cover 0..N-1 exactly (a contiguous track).")

    def _apply():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            _assert_board_editable(ev)

            # Resolve bindings.
            event_task_ids = {
                tid for (tid,) in s.query(EventTask.id)
                .filter(EventTask.event_id == ev.id).all()
            }
            lib_ids = [c["library_item_id"] for c in tiles_in
                       if c.get("library_item_id") is not None]
            lib_rows = {}
            if lib_ids:
                for row in (s.query(EventTaskLibraryItem)
                            .filter(EventTaskLibraryItem.id.in_(lib_ids),
                                    EventTaskLibraryItem.active.is_(True)).all()):
                    lib_rows[row.id] = row
                missing = [i for i in lib_ids if i not in lib_rows]
                if missing:
                    abort_problem(404, "Library preset not found",
                                  f"Library item(s) {missing} not found or inactive.")
            for cell in tiles_in:
                tid = cell.get("task_id")
                if tid is not None and tid not in event_task_ids:
                    abort_problem(422, "Invalid tile",
                                  f"task_id {tid} does not belong to this event.")

            # Record designer-created pinned tasks on the OLD board for GC.
            old_tiles = (s.query(EventBoardTile)
                         .filter(EventBoardTile.event_id == ev.id).all())
            old_pin_ids = set()
            for t in old_tiles:
                if t.task_id:
                    task = s.query(EventTask).filter(EventTask.id == t.task_id).first()
                    if task is not None:
                        try:
                            if json.loads(task.config or "{}").get(_BOARD_PIN_KEY):
                                old_pin_ids.add(task.id)
                        except (TypeError, ValueError):
                            pass

            # Replace tiles wholesale (bingo pattern).
            (s.query(EventBoardTile)
             .filter(EventBoardTile.event_id == ev.id)
             .delete(synchronize_session=False))

            kept_task_ids = set()
            for cell in tiles_in:
                task_id = cell.get("task_id")
                lib_id = cell.get("library_item_id")
                if lib_id is not None:
                    preset = lib_rows[lib_id]
                    cfg = {}
                    if preset.config:
                        try:
                            parsed = json.loads(preset.config)
                            if isinstance(parsed, dict):
                                cfg = parsed
                        except (TypeError, ValueError):
                            cfg = {}
                    cfg[_BOARD_PIN_KEY] = True
                    task = EventTask(
                        event_id=ev.id, type=preset.type, label=preset.name,
                        target=preset.target, target_value=preset.target_value,
                        points=int(preset.default_points or 0),
                        requires_confirmation=False,
                        config=json.dumps(cfg), visibility="private",
                        difficulty=preset.difficulty,
                    )
                    s.add(task)
                    s.flush()
                    task_id = task.id
                if task_id is not None:
                    kept_task_ids.add(task_id)
                s.add(EventBoardTile(
                    event_id=ev.id,
                    idx=cell["idx"],
                    x=float(cell["x"]),
                    y=float(cell["y"]),
                    label=(cell.get("label") or "").strip()[:255] or None,
                    difficulty=cell.get("difficulty"),
                    task_id=task_id,
                    tile_kind=cell.get("tile_kind") or "normal",
                    config=None,
                ))

            # GC designer-created pins the new board dropped (unless the
            # engine already wrote ledger rows against them).
            from db.models import EventCompletion

            for tid in sorted(old_pin_ids - kept_task_ids):
                used = (s.query(EventCompletion.id)
                        .filter(EventCompletion.task_id == tid).first())
                if used:
                    continue
                (s.query(EventProgress)
                 .filter(EventProgress.task_id == tid)
                 .delete(synchronize_session=False))
                (s.query(EventTask)
                 .filter(EventTask.id == tid, EventTask.event_id == ev.id)
                 .delete(synchronize_session=False))

            # Upsert the config row (background ref rides along with layout).
            config = (s.query(EventBoardConfig)
                      .filter(EventBoardConfig.event_id == ev.id).first())
            if config is None:
                config = EventBoardConfig(event_id=ev.id)
                s.add(config)
            if "background_url" in body:
                bg = body.get("background_url")
                if bg is not None and (not isinstance(bg, str) or len(bg) > 255):
                    abort_problem(422, "Invalid background",
                                  "'background_url' must be a string (≤255) or null.")
                config.background_url = bg or None
            for key in ("bg_width", "bg_height"):
                if key in body:
                    v = body.get(key)
                    if v is not None and (not isinstance(v, int) or isinstance(v, bool)
                                          or not (1 <= v <= 20000)):
                        abort_problem(422, "Invalid background",
                                      f"'{key}' must be an integer 1–20000 or null.")
                    setattr(config, key, v)

            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                action="event.board.replace", target=str(ev.id),
                after=f"tiles={len(tiles_in)}",
            ))
            s.commit()
            return _board_payload(s, ev)

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


@event_board_bp.patch("/events/<int:event_id>/board/settings")
async def patch_board_settings(event_id: int):
    """Merge a partial §2.5 settings document. Live-tunable: leaders keep
    full control mid-event (dice, triggers, coins, mercy, shop, rendering)."""
    user_id = current_user_id()
    body = await json_body()
    patch = _validate_settings_patch(body)
    if not patch:
        abort_problem(422, "Empty patch", "No recognized settings keys in the body.")

    def _apply():
        from services.boardgame_engine import _deep_merge, board_settings

        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            config = (s.query(EventBoardConfig)
                      .filter(EventBoardConfig.event_id == ev.id).first())
            if config is None:
                config = EventBoardConfig(event_id=ev.id)
                s.add(config)
            stored = {}
            if config.settings:
                try:
                    parsed = json.loads(config.settings)
                    if isinstance(parsed, dict):
                        stored = parsed
                except (TypeError, ValueError):
                    stored = {}
            merged = _deep_merge(stored, patch)
            config.settings = json.dumps(merged)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                action="event.board.settings", target=str(ev.id),
                after=json.dumps(patch)[:250],
            ))
            s.commit()
            return board_settings(config.settings)

    settings = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"settings": settings}))


@event_board_bp.post("/events/<int:event_id>/board/background")
async def upload_board_background(event_id: int):
    """Board background image → B2 (the proof-upload pattern: server-side put,
    B2 bucket CORS only allows GET). Stores url + intrinsic dimensions."""
    user_id = current_user_id()
    files = await request.files
    upload = files.get("file")
    if upload is None:
        abort_problem(422, "Invalid body", "A multipart 'file' field is required.")
    raw = upload.read()
    if not raw:
        abort_problem(422, "Empty file", "The uploaded image was empty.")
    if len(raw) > _BG_MAX_BYTES:
        abort_problem(422, "File too large", "Board backgrounds are capped at 8 MB.")

    import io

    from PIL import Image, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(raw))
        fmt_name = im.format
        width, height = im.size
        im.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG, or WebP image.")
    fmt = _BG_IMAGE_FORMATS.get(fmt_name or "")
    if fmt is None:
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG, or WebP image.")
    ext, content_type = fmt

    def _check():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            _assert_board_editable(ev)
            return ev.group_id

    group_id = await asyncio.to_thread(_check)

    key = f"dt_uploads/boards/{event_id}-{uuid.uuid4().hex[:12]}.{ext}"
    try:
        from utils.b2_storage import upload_bytes
        from web_api.routes.submissions import B2_CDN_BASE_URL

        await upload_bytes(raw, key, content_type)
        public_url = f"{B2_CDN_BASE_URL.rstrip('/')}/{key}"
    except Exception as e:
        abort_problem(502, "Upload service unavailable", str(e))

    def _save():
        with db_session() as s:
            config = (s.query(EventBoardConfig)
                      .filter(EventBoardConfig.event_id == event_id).first())
            if config is None:
                config = EventBoardConfig(event_id=event_id)
                s.add(config)
            config.background_url = public_url
            config.bg_width = width
            config.bg_height = height
            s.add(AuditLog(
                actor_user_id=user_id, group_id=group_id,
                action="event.board.background", target=str(event_id),
                after=public_url[:250],
            ))
            s.commit()

    await asyncio.to_thread(_save)
    _bump(event_id)
    return private_no_store(jsonify({
        "background_url": public_url, "bg_width": width, "bg_height": height,
    }))


@event_board_bp.post("/events/<int:event_id>/board/roll")
async def roll_board(event_id: int):
    """Manual dice roll. The caller must be on the team (or an event admin
    rolling on behalf via {team_id}); movement.manual_roller decides which.
    409 unless the team is awaiting its roll."""
    user_id = current_user_id()
    body = await json_body()
    explicit_team_id = body.get("team_id")
    if explicit_team_id is not None and (
            not isinstance(explicit_team_id, int) or isinstance(explicit_team_id, bool)):
        abort_problem(422, "Invalid team_id", "'team_id' must be an integer.")

    def _apply():
        from services.boardgame_engine import (
            can_trigger_roll,
            load_board_settings,
            perform_roll,
        )
        from utils.redis import RedisClient

        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=False)
            if ev.status != "active":
                abort_problem(409, "Event not live",
                              "Rolls only happen while the event is active.")

            user = load_user(s, user_id)
            admin = _is_event_admin(s, user_id, ev) or is_superadmin(user)

            # The caller's own team: any of their players on a team here.
            my_team_ids = {
                team_id for (team_id,) in
                s.query(EventTeamMember.team_id)
                .join(Player, Player.player_id == EventTeamMember.player_id)
                .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                .filter(EventTeam.event_id == ev.id, Player.user_id == user_id)
                .all()
            }
            team_id = explicit_team_id
            if team_id is None:
                if len(my_team_ids) != 1:
                    abort_problem(422, "Which team?",
                                  "You are not on exactly one team — pass team_id.")
                team_id = next(iter(my_team_ids))
            is_member = team_id in my_team_ids
            if not is_member and not admin:
                abort_problem(403, "Forbidden", "You are not on that team.")

            settings = load_board_settings(s, ev.id)
            if (settings.get("movement") or {}).get("trigger") == "auto" and not admin:
                abort_problem(409, "Rolls are automatic",
                              "This event rolls automatically on completion.")
            if not can_trigger_roll(settings, is_team_member=is_member, is_admin=admin):
                abort_problem(403, "Forbidden",
                              "This event's settings don't let you trigger the roll.")

            pos = (s.query(EventBoardPosition)
                   .filter(EventBoardPosition.team_id == team_id).first())
            if pos is None or pos.event_id != ev.id:
                abort_problem(404, "No board position", "That team has no board state.")
            if pos.status != "awaiting_roll":
                abort_problem(409, "Not ready to roll",
                              "Complete the current task first." if pos.status == "active"
                              else f"The team is '{pos.status}'.")

            r = RedisClient().client
            summary = perform_roll(s, r, ev.id, team_id,
                                   settings=settings, acted_by_user_id=user_id)
            if summary is None:
                abort_problem(409, "Roll unavailable", "The board is not rollable.")
            won = bool(summary.get("won"))
            if won:
                from services import event_lifecycle

                event_lifecycle.end_event(s, ev)
            s.commit()
            return summary

    summary = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(summary))


# --------------------------------------------------------------------------- #
# Shop (web45a)
# --------------------------------------------------------------------------- #
def _resolve_team_for_action(s, ev, user_id: int, explicit_team_id):
    """The (team_id, is_member, is_admin) context for shop/roll actions:
    members act for their own team; event admins may act for any team by
    passing team_id."""
    user = load_user(s, user_id)
    admin = _is_event_admin(s, user_id, ev) or is_superadmin(user)
    my_team_ids = {
        team_id for (team_id,) in
        s.query(EventTeamMember.team_id)
        .join(Player, Player.player_id == EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == ev.id, Player.user_id == user_id)
        .all()
    }
    team_id = explicit_team_id
    if team_id is None:
        if len(my_team_ids) != 1:
            abort_problem(422, "Which team?",
                          "You are not on exactly one team — pass team_id.")
        team_id = next(iter(my_team_ids))
    is_member = team_id in my_team_ids
    if not is_member and not admin:
        abort_problem(403, "Forbidden", "You are not on that team.")
    return team_id, is_member, admin


@event_board_bp.get("/events/<int:event_id>/board/shop")
async def get_board_shop(event_id: int):
    """The event's purchasable catalog plus (when the caller is on a team or
    passes ?team_id= as an admin) that team's wallet/inventory/cooldowns."""
    user_id = current_user_id()
    raw_team = (request.args.get("team_id") or "").strip()
    explicit_team = int(raw_team) if raw_team.isdigit() else None

    def _read():
        from services.boardgame_shop import available_items, team_shop_state

        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=False)
            if _effective_status(ev) == "draft" and not _can_view_draft(s, user_id, ev):
                abort_problem(404, "Event not found", f"No event {event_id}.")
            payload = {"items": available_items(s, ev.id)}
            # Team context is optional — spectators still see the catalog.
            try:
                team_id, _m, _a = _resolve_team_for_action(
                    s, ev, user_id, explicit_team)
                payload["team"] = team_shop_state(s, ev.id, team_id)
            except Exception:
                payload["team"] = None
            return payload

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


@event_board_bp.post("/events/<int:event_id>/board/shop/buy")
async def buy_board_item(event_id: int):
    user_id = current_user_id()
    body = await json_body()
    shop_item_id = body.get("shop_item_id")
    if not isinstance(shop_item_id, int) or isinstance(shop_item_id, bool):
        abort_problem(422, "Invalid item", "'shop_item_id' must be an integer.")
    explicit_team = body.get("team_id")
    if explicit_team is not None and (
            not isinstance(explicit_team, int) or isinstance(explicit_team, bool)):
        abort_problem(422, "Invalid team_id", "'team_id' must be an integer.")

    def _apply():
        from services.boardgame_shop import ShopError, buy_item

        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=False)
            if ev.status != "active":
                abort_problem(409, "Event not live",
                              "The shop opens while the event is active.")
            team_id, _m, _a = _resolve_team_for_action(s, ev, user_id, explicit_team)
            try:
                result = buy_item(s, ev.id, team_id, shop_item_id, user_id)
            except ShopError as e:
                abort_problem(e.status, e.title, e.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                action="event.board.shop.buy", target=str(ev.id),
                after=f"team={team_id} item={shop_item_id}",
            ))
            s.commit()
            return {"team_id": team_id, **result}

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))


@event_board_bp.post("/events/<int:event_id>/board/items/<int:inventory_id>/use")
async def use_board_item(event_id: int, inventory_id: int):
    user_id = current_user_id()
    body = await json_body()
    explicit_team = body.get("team_id")
    if explicit_team is not None and (
            not isinstance(explicit_team, int) or isinstance(explicit_team, bool)):
        abort_problem(422, "Invalid team_id", "'team_id' must be an integer.")
    target = {}
    for key in ("target_team_id", "target_tile_idx"):
        if body.get(key) is not None:
            if not isinstance(body[key], int) or isinstance(body[key], bool):
                abort_problem(422, "Invalid target", f"'{key}' must be an integer.")
            target[key] = body[key]

    def _apply():
        from services.boardgame_shop import ShopError, use_item
        from utils.redis import RedisClient

        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=False)
            if ev.status != "active":
                abort_problem(409, "Event not live",
                              "Items can only be used while the event is active.")
            team_id, _m, _a = _resolve_team_for_action(s, ev, user_id, explicit_team)
            r = RedisClient().client
            try:
                result = use_item(s, r, ev.id, team_id, inventory_id, user_id,
                                  target=target or None)
            except ShopError as e:
                abort_problem(e.status, e.title, e.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                action="event.board.item.use", target=str(ev.id),
                after=f"team={team_id} inv={inventory_id} fx={result.get('effect')}",
            ))
            s.commit()
            return {"team_id": team_id, **result}

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))
