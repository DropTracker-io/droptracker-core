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
public once the event is visible (restricted events — drafts or private:
participants/admins only via events._can_view_restricted). The board layout
locks when the event starts — settings stay live-tunable by design.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from quart import Blueprint, Response, jsonify, request

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
    render_token_authorized,
)
from web_api.routes.event_admin import _assert_board_editable
from web_api.routes.events import (
    _assert_event_admin,
    _bump,
    _can_view_restricted,
    _deny_restricted,
    _effective_status,
    _is_event_admin,
    _is_restricted,
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
# Ordered tiebreak metrics for a finish_tile race (event_lifecycle.final_standings).
_WIN_TIEBREAKS = ("score", "coins")
# Tile-bound effect consumption modes (services/boardgame_effects.BREAK_MODES).
_EFFECT_BREAK_MODES = ("pass", "land", "both")
# Per-event shop stock refresh cadence (DEFAULT_BOARD_SETTINGS.shop). "days"
# added web61a for the "restock every X days" ask; "turns"/"hours" predate it.
_SHOP_REFRESH_MODES = ("none", "turns", "hours", "days")


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
        if "icon_size" in render:
            t["icon_size"] = _clean_int(render["icon_size"], 8, 64,
                                        "tile_render.icon_size")
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
        if "refresh_mode" in shop:
            if shop["refresh_mode"] not in _SHOP_REFRESH_MODES:
                abort_problem(422, "Invalid settings",
                              f"shop.refresh_mode must be one of {list(_SHOP_REFRESH_MODES)}.")
            sh["refresh_mode"] = shop["refresh_mode"]
        if "refresh_interval" in shop:
            sh["refresh_interval"] = _clean_int(
                shop["refresh_interval"], 0, 100000, "shop.refresh_interval")
        if "refresh_random" in shop:
            sh["refresh_random"] = bool(shop["refresh_random"])
        if sh:
            out["shop"] = sh

    items = body.get("items")
    if items is not None:
        if not isinstance(items, dict):
            abort_problem(422, "Invalid settings", "'items' must be an object.")
        it: dict = {}
        if "enabled_item_ids" in items:
            ids = items["enabled_item_ids"]
            if ids is None:
                it["enabled_item_ids"] = None  # null = the whole active catalog
            else:
                if not isinstance(ids, list) or len(ids) > 200 or not all(
                        isinstance(i, int) and not isinstance(i, bool) and i > 0
                        for i in ids):
                    abort_problem(422, "Invalid settings",
                                  "items.enabled_item_ids must be null or a "
                                  "list of shop item ids (max 200).")
                it["enabled_item_ids"] = ids
        if "disabled_effects" in items:
            fx = items["disabled_effects"]
            if not isinstance(fx, list) or len(fx) > 50 or not all(
                    isinstance(e, str) and 0 < len(e) <= 32 for e in fx):
                abort_problem(422, "Invalid settings",
                              "items.disabled_effects must be a list of "
                              "effect keys (max 50, each ≤32 chars).")
            it["disabled_effects"] = fx
        if "behaviors" in items:
            behaviors = items["behaviors"]
            if not isinstance(behaviors, dict) or len(behaviors) > 50:
                abort_problem(422, "Invalid settings",
                              "items.behaviors must be an object keyed by "
                              "effect name (max 50 keys).")
            clean_behaviors: dict = {}
            for effect_key, b in behaviors.items():
                # Unknown effect keys ride through (forward-compat with
                # future registry entries) after the same field checks.
                if not isinstance(effect_key, str) or not (0 < len(effect_key) <= 32):
                    abort_problem(422, "Invalid settings",
                                  "items.behaviors keys must be effect names "
                                  "(≤32 chars).")
                if not isinstance(b, dict) or len(b) > 20:
                    abort_problem(422, "Invalid settings",
                                  f"items.behaviors.{effect_key} must be an object.")
                cb: dict = {}
                for knob, v in b.items():
                    if not isinstance(knob, str) or not (0 < len(knob) <= 32):
                        abort_problem(422, "Invalid settings",
                                      f"items.behaviors.{effect_key} has an "
                                      "invalid field name.")
                    if knob == "break_on":
                        if v not in _EFFECT_BREAK_MODES:
                            abort_problem(
                                422, "Invalid settings",
                                f"items.behaviors.{effect_key}.break_on must "
                                f"be one of {list(_EFFECT_BREAK_MODES)}.")
                        cb[knob] = v
                    elif knob == "stall_turns":
                        cb[knob] = _clean_int(
                            v, 0, 3, f"items.behaviors.{effect_key}.stall_turns")
                    elif knob == "visible_to_all":
                        cb[knob] = bool(v)
                    elif knob == "expire_on_placer_move":
                        cb[knob] = bool(v)
                    else:
                        # Forward-compat: unknown knobs pass through when scalar.
                        if v is not None and not isinstance(v, (str, int, float, bool)):
                            abort_problem(
                                422, "Invalid settings",
                                f"items.behaviors.{effect_key}.{knob} must be "
                                "a scalar value.")
                        if isinstance(v, str) and len(v) > 64:
                            abort_problem(
                                422, "Invalid settings",
                                f"items.behaviors.{effect_key}.{knob} is too long.")
                        cb[knob] = v
                clean_behaviors[effect_key] = cb
            it["behaviors"] = clean_behaviors
        if it:
            out["items"] = it

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
        if "tiebreak" in win:
            tb = win["tiebreak"]
            if (not isinstance(tb, list) or not tb or len(tb) > len(_WIN_TIEBREAKS)
                    or any(t not in _WIN_TIEBREAKS for t in tb)
                    or len(set(tb)) != len(tb)):
                abort_problem(422, "Invalid settings",
                              "win.tiebreak must be a non-empty ordered list of "
                              f"distinct tokens from {list(_WIN_TIEBREAKS)}.")
            w["tiebreak"] = tb
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


# Team-bound live effects (target_team_id set) → how the player view labels
# them. Tile-bound roadblocks are handled by _active_tile_effects; these are the
# buffs/debuffs that sit on a TEAM (U2 — so a player can see they're frozen,
# shielded, boosted, …). icon is a plain emoji; kind drives the badge tone.
_TEAM_EFFECT_META = {
    "freeze_opponent": ("Frozen", "debuff", "❄️"),
    "shield": ("Shield armed", "buff", "\U0001F6E1️"),
    "ward": ("Ward armed", "buff", "\U0001F9FF"),
    "boost_coins": ("Coin boost", "buff", "✨"),
    "extra_dice": ("Extra dice", "buff", "\U0001F3B2"),
    "choose_roll": ("Chosen roll", "buff", "\U0001F3AF"),
    "coin_toll": ("Coin toll armed", "buff", "\U0001F4B0"),
}


def _team_effects_by_team(s, event_id: int) -> dict:
    """All active team-bound effects for the event, grouped by target team, so
    _position_row can attach ``active_effects`` without an N+1 query."""
    from db.models import EventBoardEffect

    rows = (s.query(EventBoardEffect)
            .filter(EventBoardEffect.event_id == event_id,
                    EventBoardEffect.status == "active",
                    EventBoardEffect.target_team_id.isnot(None),
                    EventBoardEffect.effect_type.in_(list(_TEAM_EFFECT_META)))
            .all())
    grouped: dict = {}
    for r in rows:
        grouped.setdefault(int(r.target_team_id), []).append(r)
    return grouped


def _active_effects_for(effect_rows: list) -> list:
    """Render team-bound effect rows into the compact ``active_effects`` list
    the board view badges (frozen ❄️ / shield 🛡 / boost ✨ …)."""
    out = []
    for e in effect_rows or []:
        meta = _TEAM_EFFECT_META.get(e.effect_type)
        if not meta:
            continue
        label, kind, icon = meta
        detail = None
        if e.effect_type == "freeze_opponent":
            try:
                cfg = json.loads(e.effect_config or "{}")
                remaining = int(cfg.get("remaining", cfg.get("turns", 1)))
                detail = f"{remaining} roll{'s' if remaining != 1 else ''} left"
            except (TypeError, ValueError):
                detail = None
        out.append({"effect_type": e.effect_type, "label": label,
                    "kind": kind, "icon": icon, "detail": detail})
    return out


def _position_row(s, pos: EventBoardPosition, team: EventTeam,
                  effects_by_team: dict = None) -> dict:
    task = None
    if pos.current_task_id:
        t = s.query(EventTask).filter(EventTask.id == pos.current_task_id).first()
        if t is not None:
            progress = (s.query(EventProgress)
                        .filter(EventProgress.task_id == t.id,
                                EventProgress.team_id == pos.team_id)
                        .first())
            from services.event_engine import effective_threshold

            task = {
                "id": t.id,
                "label": t.label,
                "type": t.type,
                "difficulty": t.difficulty,
                "progress": int(progress.progress or 0) if progress else 0,
                # Config included so counted goals (pb times/unique modes)
                # show their real target; team-aware for whole_team.
                "target": effective_threshold(s, {
                    "type": t.type, "target_value": t.target_value,
                    "config": t.config}, pos.team_id),
            }
    last_roll = None
    if pos.last_roll:
        try:
            last_roll = json.loads(pos.last_roll)
        except (TypeError, ValueError):
            last_roll = None
    pending_choice = None
    if getattr(pos, "pending_choice", None):
        try:
            pending_choice = json.loads(pos.pending_choice)
        except (TypeError, ValueError):
            pending_choice = None
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
        "pending_choice": pending_choice,
        "active_effects": _active_effects_for(
            (effects_by_team or {}).get(pos.team_id, [])),
        "mercy_deadline": (int(pos.mercy_deadline.timestamp())
                           if pos.mercy_deadline else None),
    }


def _active_tile_effects(s, event_id: int) -> list:
    """Active tile-bound effects (web49a) — roadblocks/bulwarks placed on tiles,
    surfaced in the board payload so they render on the board for *everyone*
    (per-item ``visible_to_all`` behavior). Icon + name are resolved from the
    shop catalog by effect type so a future tile-bound item renders for free."""
    from db.models import BoardgameShopItem, EventBoardEffect
    from services.boardgame_effects import tile_bound_effects

    kinds = list(tile_bound_effects())
    if not kinds:
        return []
    rows = (s.query(EventBoardEffect)
            .filter(EventBoardEffect.event_id == event_id,
                    EventBoardEffect.status == "active",
                    EventBoardEffect.effect_type.in_(kinds),
                    EventBoardEffect.target_tile_idx.isnot(None))
            .all())
    if not rows:
        return []
    meta = {}
    for it in (s.query(BoardgameShopItem)
               .filter(BoardgameShopItem.effect.in_(
                   list({r.effect_type for r in rows})))
               .order_by(BoardgameShopItem.active.desc(),
                         BoardgameShopItem.sort)
               .all()):
        meta.setdefault(it.effect,
                        {"icon_item_id": it.icon_item_id, "name": it.name})
    out = []
    for r in rows:
        try:
            behavior = json.loads(r.effect_config or "{}")
        except (ValueError, TypeError):
            behavior = {}
        m = meta.get(r.effect_type, {})
        out.append({
            "id": int(r.id),
            "effect_type": r.effect_type,
            "target_tile_idx": int(r.target_tile_idx),
            "placed_by_team_id": r.source_team_id,
            "icon_item_id": m.get("icon_item_id"),
            "name": m.get("name"),
            "visible_to_all": bool(behavior.get("visible_to_all", True)),
        })
    return out


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
    effects_by_team = _team_effects_by_team(s, ev.id)

    return {
        "event_id": ev.id,
        "background_url": config.background_url if config else None,
        "bg_width": config.bg_width if config else None,
        "bg_height": config.bg_height if config else None,
        "settings": board_settings(config.settings if config else None),
        "tiles": [_tile_row(t, task_labels) for t in tiles],
        "finish_idx": finish_idx(tiles),
        "positions": [
            _position_row(s, p, teams.get(p.team_id), effects_by_team)
            for p in positions
        ],
        "effects": _active_tile_effects(s, ev.id),
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
# Tile-write core (shared by the designer autosave PUT and the procedural
# generator POST — both replace the whole track wholesale).
# --------------------------------------------------------------------------- #
def _validate_tiles_payload(tiles_in) -> None:
    """Whitelist-validate a tiles array (idx contiguous 0..N-1, x/y in [0,1],
    at most one binding per tile, known difficulty/kind). Raises 422."""
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


def _write_board(s, ev, user_id: int, tiles_in: list, body: dict,
                 *, audit_action: str = "event.board.replace",
                 audit_after: str | None = None) -> None:
    """Replace the event's tile track wholesale + upsert the background ref.

    The heart of both the designer's autosave and the procedural generator:
    resolves task/library bindings, swaps the tiles, GCs orphaned designer
    pins, and rides the background_url/dimensions in via ``body``. Caller
    owns the commit + payload build."""
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

    # GC designer-created pins the new board dropped (unless the engine already
    # wrote ledger rows against them).
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
        actor_user_id=user_id, group_id=ev.group_id, event_id=ev.id,
        action=audit_action, target=str(ev.id),
        after=audit_after if audit_after is not None else f"tiles={len(tiles_in)}",
    ))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@event_board_bp.get("/events/<int:event_id>/board")
async def get_board(event_id: int):
    viewer_id = optional_user_id()
    # Internal board-image render bypass (see events.get_event / deps).
    render_bypass = render_token_authorized()

    def _read():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=False)
            if (_is_restricted(ev) and not render_bypass
                    and not _can_view_restricted(s, viewer_id, ev)):
                _deny_restricted(ev, viewer_id)
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
    _validate_tiles_payload(tiles_in)

    def _apply():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            _assert_board_editable(ev)
            _write_board(s, ev, user_id, tiles_in, body)
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
                event_id=ev.id,
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
                event_id=event_id,
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
            _assert_leadership_authority(s, ev, team_id, user_id, admin,
                                         "trigger the roll")

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
            # "blocked" attempts go through: perform_roll consumes the
            # attempt as a stalled turn (roadblock stall, web49a).
            if pos.status not in ("awaiting_roll", "blocked"):
                abort_problem(409, "Not ready to roll",
                              "Complete the current task first." if pos.status == "active"
                              else f"The team is '{pos.status}'.")

            r = RedisClient().client
            summary = perform_roll(s, r, ev.id, team_id,
                                   settings=settings, acted_by_user_id=user_id)
            if summary is None:
                abort_problem(409, "Roll unavailable", "The board is not rollable.")
            # N1: manual rolls announce to Discord too (auto mode already
            # enqueues event_board_turn from the apply path). Reuses the existing
            # type + layout; a winning roll also fires its "reached the finish!"
            # embed. Best-effort — a notification hiccup must not fail the roll.
            try:
                from services import event_engine, event_lifecycle as _lc

                rep = _lc._representative_player_id(s, ev.id)
                if rep is not None:
                    team_row = (s.query(EventTeam)
                                .filter(EventTeam.id == team_id).first())
                    dice = summary.get("dice") or []
                    event_engine._enqueue_notification(
                        s, "event_board_turn", event_engine._event_to_dict(ev),
                        rep, {
                            "team_id": team_id,
                            "team_name": getattr(team_row, "name", None),
                            "dice": dice,
                            "dice_str": " + ".join(str(d) for d in dice) or "?",
                            "tile_from": summary.get("from"),
                            "tile_to": summary.get("to"),
                            "turn": summary.get("turn"),
                            "won": bool(summary.get("won")),
                            "next_task_label": summary.get("task_label"),
                        })
            except Exception:
                pass
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
# Procedural generation (web46a)
# --------------------------------------------------------------------------- #
@event_board_bp.post("/events/<int:event_id>/board/generate")
async def generate_board(event_id: int):
    """Roll a whole procedural board (art + sequential tile track) in one shot.

    Body (all optional): { seed?, regions?, tiles?, style?, title?, subtitle?,
    watermark? }. The boardgen engine draws a winding start->finish path; each
    path tile becomes a track tile (idx in order, x/y fractional, difficulty
    cycling air->water->earth->fire, ends start/finish). The rendered SVG is
    published to B2 and set as the tile background. Draft-only, event admin —
    same gate as the designer autosave. Everything stays editable afterward."""
    user_id = current_user_id()
    body = await json_body()

    from services.boardgame_generator import (
        build_board_assets,
        normalize_params,
        upload_board_svg,
    )

    try:
        params = normalize_params(
            seed=body.get("seed"),
            regions=body.get("regions"),
            tiles=body.get("tiles"),
            style=body.get("style"),
            title=body.get("title"),
            subtitle=body.get("subtitle"),
            watermark=body.get("watermark"),
        )
    except ValueError as e:
        abort_problem(422, "Invalid generation params", str(e))

    # Gate before spending any CPU / B2 on generation.
    def _precheck():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            _assert_board_editable(ev)

    await asyncio.to_thread(_precheck)

    assets = await asyncio.to_thread(build_board_assets, params)
    _validate_tiles_payload(assets["tiles"])  # defensive: honor the same contract

    try:
        background_url = await upload_board_svg(
            assets["svg"], event_id, params.seed)
    except Exception as e:
        abort_problem(502, "Board art upload failed", str(e))

    synthetic_bg = {
        "background_url": background_url,
        "bg_width": assets["width"],
        "bg_height": assets["height"],
    }

    def _apply():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            _assert_board_editable(ev)
            _write_board(
                s, ev, user_id, assets["tiles"], synthetic_bg,
                audit_action="event.board.generate",
                audit_after=(f"seed={params.seed} style={params.style} "
                             f"tiles={len(assets['tiles'])}"),
            )
            s.commit()
            return _board_payload(s, ev)

    payload = await asyncio.to_thread(_apply)
    payload["generated"] = assets["meta"]
    _bump(event_id)
    return private_no_store(jsonify(payload))


@event_board_bp.get("/events/<int:event_id>/board.png")
async def board_png(event_id: int):
    """The full live event board as a PNG (download / preview) — a 1:1 headless
    screenshot of the real web board (``/board-image/{id}``): the bingo grid with
    per-team completion state, or the board-game track with tiles, pieces and
    standings. 404 for standard task-list events (no visual board). ``?scale``
    (1..3) controls the device pixel ratio (default 2)."""
    viewer_id = optional_user_id()
    raw_scale = (request.args.get("scale") or "").strip()
    try:
        scale = float(raw_scale) if raw_scale else 2.0
    except ValueError:
        scale = 2.0
    scale = max(1.0, min(3.0, scale))

    from services.event_board_image import (
        _collect_render_inputs,
        screenshot_event_board,
    )

    def _gate():
        # Viewer-visibility gate + visual-board check off the event loop; the
        # screenshot itself needs only the event id (no session).
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                abort_problem(404, "Event not found", f"No event {event_id}.")
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                abort_problem(404, "Event not found", f"No event {event_id}.")
            if _collect_render_inputs(s, ev) is None:
                abort_problem(404, "No visual board",
                              "This event has no bingo or board-game board to render.")

    await asyncio.to_thread(_gate)
    png = await screenshot_event_board(event_id, scale=scale)
    if png is None:
        abort_problem(502, "Board render failed",
                      "The board image could not be generated.")
    return Response(png, content_type="image/png", headers={
        "Content-Disposition": f'inline; filename="board-{event_id}.png"',
    })


def _assert_leadership_authority(s, ev, team_id, user_id, is_admin: bool,
                                 action: str) -> None:
    """Executive-authority gate (web48a): when the event runs team
    leadership, turn actions (roll / shop) belong to the team's leader or
    co-leader. Event admins bypass; leadership disabled = everyone as before."""
    from web_api.event_leadership import effective_leadership, team_role_for_user

    config = effective_leadership(getattr(ev, "leadership_config", None))
    if not config["enabled"] or is_admin:
        return
    if team_role_for_user(s, team_id, user_id) is None:
        who = "leader or co-leader" if config["co_leaders"] else "leader"
        abort_problem(403, "Leaders only",
                      f"Only your team's {who} can {action} in this event.")


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
            if _is_restricted(ev) and not _can_view_restricted(s, user_id, ev):
                _deny_restricted(ev, user_id)
                abort_problem(404, "Event not found", f"No event {event_id}.")
            # Team context is optional — spectators still see the catalog. When
            # present it drives per-team cap/bought counts in the item list.
            team_id = None
            try:
                team_id, _m, _a = _resolve_team_for_action(
                    s, ev, user_id, explicit_team)
            except Exception:
                team_id = None
            payload = {"items": available_items(s, ev.id, team_id=team_id)}
            payload["team"] = (team_shop_state(s, ev.id, team_id)
                               if team_id is not None else None)
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
            team_id, _m, is_admin = _resolve_team_for_action(s, ev, user_id, explicit_team)
            _assert_leadership_authority(s, ev, team_id, user_id, is_admin,
                                         "buy shop items")
            try:
                result = buy_item(s, ev.id, team_id, shop_item_id, user_id)
            except ShopError as e:
                abort_problem(e.status, e.title, e.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.board.shop.buy", target=str(ev.id),
                after=f"team={team_id} item={shop_item_id}",
            ))
            s.commit()
            return {"team_id": team_id, **result}

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))


# Offensive effects worth a Discord skirmish post (coin_toll is a self-buff
# announced on the roll, not a direct attack — omitted here).
_BOARD_ACTION_VERBS = {
    "freeze_opponent": "❄️ froze",
    "knockback": "\U0001F4A5 knocked back",
    "steal_item": "\U0001F99D stole an item from",
    "reroll_opponent_task": "\U0001F52E rerolled the task of",
}


def _maybe_enqueue_board_action(s, ev, actor_team_id: int, result: dict) -> None:
    """Announce an offensive item hit (freeze/knockback/steal/reroll-opponent),
    or a defense that absorbed one, as event_board_action so the PvP layer is
    visible on Discord (N2). Best-effort — never fails the item use."""
    effect = (result or {}).get("effect")
    target_id = (result or {}).get("target_team_id")
    if effect not in _BOARD_ACTION_VERBS or not target_id:
        return
    try:
        from services import event_engine, event_lifecycle as _lc

        rep = _lc._representative_player_id(s, ev.id)
        if rep is None:
            return
        names = {
            t.id: t.name for t in s.query(EventTeam)
            .filter(EventTeam.id.in_([actor_team_id, int(target_id)])).all()
        }
        actor = names.get(actor_team_id) or f"Team {actor_team_id}"
        victim = names.get(int(target_id)) or f"Team {target_id}"
        item_name = result.get("item_name") or "an item"
        if result.get("absorbed"):
            defense = result.get("absorbed_by") or "a defense"
            action_line = (f"**{victim}** blocked **{actor}**'s **{item_name}** "
                           f"with their {defense}! \U0001F6E1️")
        else:
            detail = ""
            if effect == "freeze_opponent" and result.get("frozen_rolls"):
                detail = f" for **{int(result['frozen_rolls'])}** rolls"
            elif effect == "knockback" and result.get("tiles"):
                detail = f" **{int(result['tiles'])}** tiles"
            action_line = (f"**{actor}** {_BOARD_ACTION_VERBS[effect]} "
                           f"**{victim}**{detail} with **{item_name}**.")
        event_engine._enqueue_notification(
            s, "event_board_action", event_engine._event_to_dict(ev), rep, {
                "team_id": actor_team_id, "team_name": actor,
                "target_team_id": int(target_id), "target_team_name": victim,
                "item_name": item_name, "effect": effect,
                "absorbed": bool(result.get("absorbed")),
                "absorbed_by": result.get("absorbed_by"),
                "action_line": action_line,
            })
    except Exception:
        pass


@event_board_bp.post("/events/<int:event_id>/board/items/<int:inventory_id>/use")
async def use_board_item(event_id: int, inventory_id: int):
    user_id = current_user_id()
    body = await json_body()
    explicit_team = body.get("team_id")
    if explicit_team is not None and (
            not isinstance(explicit_team, int) or isinstance(explicit_team, bool)):
        abort_problem(422, "Invalid team_id", "'team_id' must be an integer.")
    target = {}
    # Integer targets: rival team, tile, and choose_roll's forced roll value.
    for key in ("target_team_id", "target_tile_idx", "value"):
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
            team_id, _m, is_admin = _resolve_team_for_action(s, ev, user_id, explicit_team)
            _assert_leadership_authority(s, ev, team_id, user_id, is_admin,
                                         "use items")
            r = RedisClient().client
            try:
                result = use_item(s, r, ev.id, team_id, inventory_id, user_id,
                                  target=target or None)
            except ShopError as e:
                abort_problem(e.status, e.title, e.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.board.item.use", target=str(ev.id),
                after=f"team={team_id} inv={inventory_id} fx={result.get('effect')}",
            ))
            # N2: surface offensive hits / absorbed attacks on Discord.
            _maybe_enqueue_board_action(s, ev, team_id, result)
            # A movement item (advance / reroll_move) that reaches the finish
            # tile ends the event, mirroring the roll route.
            if result.get("won"):
                from services import event_lifecycle

                event_lifecycle.end_event(s, ev)
            s.commit()
            return {"team_id": team_id, **result}

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))


@event_board_bp.post("/events/<int:event_id>/board/choice")
async def choose_board_task(event_id: int):
    """Resolve a pending choose_task pick (web50a): assign the chosen candidate
    task as the team's live task. Auth mirrors the shop use route (team
    leader/admin). Body: {choice_index, team_id?}."""
    user_id = current_user_id()
    body = await json_body()
    choice_index = body.get("choice_index")
    if not isinstance(choice_index, int) or isinstance(choice_index, bool) or choice_index < 0:
        abort_problem(422, "Invalid choice", "'choice_index' must be a non-negative integer.")
    explicit_team = body.get("team_id")
    if explicit_team is not None and (
            not isinstance(explicit_team, int) or isinstance(explicit_team, bool)):
        abort_problem(422, "Invalid team_id", "'team_id' must be an integer.")

    def _apply():
        from services.boardgame_shop import ShopError, apply_task_choice
        from utils.redis import RedisClient

        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=False)
            if ev.status != "active":
                abort_problem(409, "Event not live",
                              "Task choices happen while the event is active.")
            team_id, _m, is_admin = _resolve_team_for_action(s, ev, user_id, explicit_team)
            _assert_leadership_authority(s, ev, team_id, user_id, is_admin,
                                         "make task choices")
            r = RedisClient().client
            try:
                result = apply_task_choice(s, r, ev.id, team_id, choice_index)
            except ShopError as e:
                abort_problem(e.status, e.title, e.detail)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.board.task.choose", target=str(ev.id),
                after=f"team={team_id} choice={choice_index}",
            ))
            s.commit()
            return {"team_id": team_id, **result}

    result = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(result))


# --------------------------------------------------------------------------- #
# Per-event shop configuration (web50a): refresh cadence + per-item overrides.
# --------------------------------------------------------------------------- #
def _shop_config_payload(s, event_id: int) -> dict:
    """The leader/admin shop-config surface (web50a) — refresh cadence at the
    top level plus a row per active catalog item with its per-event overrides
    (defaulted where no rotation row exists). Shape matches the web
    `BoardShopConfigSchema`; used by both the GET and the PUT response."""
    from db.models import BoardgameShopItem, EventShopRotation
    from services.boardgame_engine import board_settings

    config = (s.query(EventBoardConfig)
              .filter(EventBoardConfig.event_id == event_id).first())
    shop = (board_settings(config.settings if config else None).get("shop") or {})
    rotation = {
        r.shop_item_id: r
        for r in s.query(EventShopRotation)
        .filter(EventShopRotation.event_id == event_id).all()
    }
    items = []
    for item in (s.query(BoardgameShopItem)
                 .filter(BoardgameShopItem.active.is_(True))
                 .order_by(BoardgameShopItem.sort, BoardgameShopItem.id).all()):
        rot = rotation.get(item.id)
        items.append({
            "shop_item_id": item.id,
            "key": item.key,
            "name": item.name,
            "effect": item.effect,
            "item_type": item.item_type,
            "icon_item_id": item.icon_item_id,
            "default_cost_coins": int(item.cost_coins or 0),
            "enabled": bool(rot.enabled) if rot is not None else True,
            "price_override": rot.price_override if rot is not None else None,
            "stock_per_refresh": rot.stock_per_refresh if rot is not None else None,
            "per_team_cap": rot.per_team_cap if rot is not None else None,
            "stock": rot.stock if rot is not None else None,
        })
    return {
        "refresh_mode": shop.get("refresh_mode") or "none",
        "refresh_interval": int(shop.get("refresh_interval") or 0),
        "refresh_random": bool(shop.get("refresh_random")),
        "items": items,
    }


@event_board_bp.get("/events/<int:event_id>/board/shop/config")
async def get_board_shop_config(event_id: int):
    """The leader/admin shop-config surface: the event's refresh cadence plus
    every active catalog item with its per-event overrides (defaulted where no
    rotation row exists)."""
    user_id = current_user_id()

    def _read():
        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            return _shop_config_payload(s, ev.id)

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


def _clean_opt_int(value, name, lo=0, hi=1_000_000):
    """A nullable non-negative-int override field (null clears it)."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
        abort_problem(422, "Invalid shop config",
                      f"'{name}' must be null or an integer {lo}–{hi}.")
    return value


@event_board_bp.put("/events/<int:event_id>/board/shop/config")
async def put_board_shop_config(event_id: int):
    """Upsert per-event shop overrides (web50a). Body:
    {items:[{shop_item_id, enabled?, price_override?, stock_per_refresh?,
             per_team_cap?}]}. A field omitted is left unchanged; a field set
    to null clears that override. Refresh cadence is set via the settings
    PATCH shop branch."""
    user_id = current_user_id()
    body = await json_body()
    items_in = body.get("items")
    if not isinstance(items_in, list) or len(items_in) > 500:
        abort_problem(422, "Invalid body", "'items' must be a list (max 500).")
    cleaned = []
    for row in items_in:
        if not isinstance(row, dict):
            abort_problem(422, "Invalid item", "Each item must be an object.")
        sid = row.get("shop_item_id")
        if not isinstance(sid, int) or isinstance(sid, bool) or sid <= 0:
            abort_problem(422, "Invalid item", "'shop_item_id' must be a positive integer.")
        entry = {"shop_item_id": sid}
        if "enabled" in row:
            if not isinstance(row["enabled"], bool):
                abort_problem(422, "Invalid item", "'enabled' must be a boolean.")
            entry["enabled"] = row["enabled"]
        if "price_override" in row:
            entry["price_override"] = _clean_opt_int(row["price_override"], "price_override")
        if "stock_per_refresh" in row:
            entry["stock_per_refresh"] = _clean_opt_int(
                row["stock_per_refresh"], "stock_per_refresh")
        if "per_team_cap" in row:
            entry["per_team_cap"] = _clean_opt_int(row["per_team_cap"], "per_team_cap")
        cleaned.append(entry)

    def _apply():
        from db.models import BoardgameShopItem, EventShopRotation

        with db_session() as s:
            ev = _load_board_event(s, event_id, for_write=True)
            _assert_event_admin(s, user_id, ev)
            valid_ids = {
                i for (i,) in s.query(BoardgameShopItem.id)
                .filter(BoardgameShopItem.active.is_(True)).all()
            }
            for entry in cleaned:
                sid = entry["shop_item_id"]
                if sid not in valid_ids:
                    abort_problem(404, "Unknown item",
                                  f"Shop item {sid} is not an active catalog item.")
                rot = (s.query(EventShopRotation)
                       .filter(EventShopRotation.event_id == ev.id,
                               EventShopRotation.shop_item_id == sid).first())
                if rot is None:
                    rot = EventShopRotation(event_id=ev.id, shop_item_id=sid)
                    s.add(rot)
                if "enabled" in entry:
                    rot.enabled = entry["enabled"]
                if "price_override" in entry:
                    rot.price_override = entry["price_override"]
                if "per_team_cap" in entry:
                    rot.per_team_cap = entry["per_team_cap"]
                if "stock_per_refresh" in entry:
                    rot.stock_per_refresh = entry["stock_per_refresh"]
                    # Initialize live stock to the cap so the item starts
                    # stocked (null = unlimited, clears the cap).
                    rot.stock = entry["stock_per_refresh"]
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.board.shop.config", target=str(ev.id),
                after=f"items={len(cleaned)}",
            ))
            s.commit()
            return _shop_config_payload(s, ev.id)

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))
