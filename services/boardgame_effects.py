"""Shop-item effect behaviors (web49a): the per-effect metadata registry.

One table (:data:`EFFECT_REGISTRY`) describes every board-game shop effect —
its cooldown family, whether it binds to a tile, and its tunable behavior
knobs — so game mechanics resolve behavior uniformly instead of each handler
or the movement core hardcoding per-item rules. A future item ("bear trap",
"portal", "toll gate") registers a spec here and, when tile-bound, rides the
same movement resolver (:func:`apply_tile_effects_on_path`) that roadblocks
use — ``_move_piece`` never learns item names.

Behavior layering (:func:`resolve_effect_behavior`), lowest to highest:

  1. ``EFFECT_REGISTRY[key].default_behavior`` — code-level defaults.
  2. ``BoardgameShopItem.effect_config``       — the superadmin catalog
     default (global, /admin/boardgame-shop).
  3. ``settings.items.behaviors[key]``         — the event leader's override
     (PATCH /events/{id}/board/settings). Pass the RAW stored overrides
     (:func:`load_event_behavior_overrides`) — the defaults-merged settings
     document would shadow layer 2 with layer-1 values.

The RESOLVED behavior is snapshotted into ``EventBoardEffect.effect_config``
at placement time, so a live trap keeps the rules it was placed under even
if the leader re-tunes the settings mid-event.

Tile-bound semantics (roadblock and friends):

  - Passing OVER the tile always stops the piece on it (the blocker blocks).
  - ``break_on`` governs consumption only: ``"pass"`` (default) spends the
    effect when it intercepts a passer; ``"land"`` keeps it alive through
    interceptions and spends it when a team lands exactly on the tile;
    ``"both"`` spends it on either.
  - ``stall_turns`` (0–N): a team STOPPED by the effect also loses that many
    turns (position status ``"blocked"`` + ``blocked_until_turn``; served in
    boardgame_engine._serve_blocked_turn). Landing exactly on the tile is
    not an interception and never stalls.
  - The placer is NOT immune — every team interacts with the effect.

Pure/testable: the registry, behavior resolution, and trigger decisions are
side-effect free; only :func:`apply_tile_effects_on_path` and
:func:`load_event_behavior_overrides` touch the session (lazy db imports,
repo convention). Caller owns the transaction; this module only mutates
loaded rows and never flushes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# Consumption modes for tile-bound effects (settings validator mirrors this).
BREAK_MODES = ("pass", "land", "both")

# Engine-side ceiling on turn stalls — the settings PATCH validator clamps
# leader input tighter (0–3); this bound just keeps corrupt data sane.
_MAX_STALL_TURNS = 10


@dataclass(frozen=True)
class EffectSpec:
    """One shop effect's code-level metadata.

    - ``item_type``       — cooldown family (BOARDGAME_ITEM_TYPES).
    - ``is_tile_bound``   — sits on a tile and interacts with movement
                            (resolved by apply_tile_effects_on_path).
    - ``implemented``     — a ``_use_<key>`` handler exists in
                            services/boardgame_shop.py; unimplemented effects
                            surface greyed-out ("usable_now": false).
    - ``default_behavior``— the layer-1 behavior knobs (see module doc).
    """

    key: str
    item_type: str
    is_tile_bound: bool = False
    implemented: bool = True
    default_behavior: dict = field(default_factory=dict)


EFFECT_REGISTRY: dict[str, EffectSpec] = {spec.key: spec for spec in (
    # -- P2: self-targeted -------------------------------------------------
    EffectSpec("skip_task", "utility"),
    EffectSpec("reroll_task", "utility"),
    EffectSpec("boost_coins", "economy", default_behavior={"multiplier": 2}),
    # -- P3: movement + interference ---------------------------------------
    EffectSpec("advance", "movement", default_behavior={"dice_sides": 6}),
    EffectSpec("roadblock", "defensive", is_tile_bound=True,
               default_behavior={"break_on": "pass", "stall_turns": 1,
                                 "visible_to_all": True}),
    EffectSpec("freeze_opponent", "offensive", default_behavior={"turns": 2}),
    EffectSpec("shield", "defensive"),
)}


def live_effects() -> frozenset:
    """Effect keys with a live handler — the shop's ``usable_now`` gate."""
    return frozenset(k for k, s in EFFECT_REGISTRY.items() if s.implemented)


def tile_bound_effects() -> tuple:
    """Effect keys that sit on a tile and interact with movement."""
    return tuple(k for k, s in EFFECT_REGISTRY.items() if s.is_tile_bound)


def parse_config(raw) -> dict:
    """A JSON/dict effect_config as a dict; anything corrupt → {}."""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def sanitize_behavior(effect_key: str, behavior: Optional[dict]) -> dict:
    """Clamp/normalize the known knobs of one behavior dict (unknown keys
    ride through untouched — forward-compat with future items). Tile-bound
    effects always come back with a complete break_on/stall_turns/
    visible_to_all trio so a partial or legacy config never breaks a turn."""
    spec = EFFECT_REGISTRY.get(effect_key)
    defaults = dict(spec.default_behavior) if spec else {}
    out = dict(behavior or {})
    if spec is not None and spec.is_tile_bound:
        if out.get("break_on") not in BREAK_MODES:
            out["break_on"] = defaults.get("break_on", "pass")
        try:
            out["stall_turns"] = max(
                0, min(_MAX_STALL_TURNS, int(out.get("stall_turns"))))
        except (TypeError, ValueError):
            out["stall_turns"] = int(defaults.get("stall_turns", 0) or 0)
        out["visible_to_all"] = bool(
            out.get("visible_to_all", defaults.get("visible_to_all", True)))
    return out


def resolve_effect_behavior(effect_key: str, item=None, settings=None,
                            overrides: Optional[dict] = None) -> dict:
    """The effective behavior for one effect: registry defaults ← item-level
    ``effect_config`` ← per-event leader override (see module doc).

    ``overrides`` is the raw ``settings.items.behaviors`` dict (preferred —
    :func:`load_event_behavior_overrides`); ``settings`` is accepted as a
    convenience and mined for the same path. Snapshot the result into the
    placed ``EventBoardEffect.effect_config``."""
    spec = EFFECT_REGISTRY.get(effect_key)
    behavior = dict(spec.default_behavior) if spec else {}
    if item is not None:
        behavior.update(parse_config(getattr(item, "effect_config", None)))
    if overrides is None and isinstance(settings, dict):
        overrides = (settings.get("items") or {}).get("behaviors")
    if isinstance(overrides, dict):
        event_level = overrides.get(effect_key)
        if isinstance(event_level, dict):
            behavior.update(event_level)
    return sanitize_behavior(effect_key, behavior)


def load_event_behavior_overrides(session, event_id: int) -> dict:
    """The event's RAW ``items.behaviors`` overrides — straight from the
    stored settings JSON, no defaults merge (a merged document would report
    default values as if the leader had chosen them, shadowing item-level
    effect_config in the layering)."""
    from db.models import EventBoardConfig

    row = (session.query(EventBoardConfig)
           .filter(EventBoardConfig.event_id == event_id).first())
    raw = parse_config(row.settings if row else None)
    behaviors = (raw.get("items") or {}).get("behaviors")
    return behaviors if isinstance(behaviors, dict) else {}


# --------------------------------------------------------------------------- #
# Tile-bound trigger strategy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TileTrigger:
    """What one tile-bound effect does to one movement crossing its tile.

    - ``stops``       — movement halts ON the effect's tile (pass-over only;
                        a landed-on tile is already the destination).
    - ``consumed``    — the effect is spent by this trigger.
    - ``stall_turns`` — extra turns the moving team loses (interceptions
                        only; landing exactly is not an interception).
    """

    stops: bool
    consumed: bool
    stall_turns: int


def resolve_tile_trigger(effect_key: str, behavior: Optional[dict], *,
                         landed: bool) -> TileTrigger:
    """Pure pass/land semantics for one effect against one movement.
    ``landed=True`` → the movement's natural destination IS the effect's
    tile; otherwise the piece is passing over it."""
    b = sanitize_behavior(effect_key, behavior)
    break_on = b.get("break_on", "pass")
    if landed:
        return TileTrigger(stops=False,
                           consumed=break_on in ("land", "both"),
                           stall_turns=0)
    return TileTrigger(stops=True,
                       consumed=break_on in ("pass", "both"),
                       stall_turns=int(b.get("stall_turns", 0) or 0))


def apply_tile_effects_on_path(session, event_id: int, team_id: int,
                               start: int, dest: int) -> Optional[dict]:
    """Resolve the active tile-bound effects a movement (start → dest]
    crosses, nearest first, and apply the first one that matters. Effect-
    agnostic: each row's placement-time behavior snapshot (effect_config)
    decides stop/consume/stall via :func:`resolve_tile_trigger` — roadblock
    is merely the first registered consumer.

    Mutates the triggering row's status when consumed (caller flushes/
    commits). Returns None when nothing triggers, else::

        {"stop_at": int,        # where the piece ends up (== dest when the
                                #  effect didn't intercept)
         "stopped": bool,       # movement was cut short on the effect tile
         "consumed": bool,
         "stall_turns": int,    # turns the moving team loses (0 = none)
         "effect_id", "effect_type", "placed_by_team_id", "behavior"}

    Note: NO placer immunity — the placing team hits its own traps.
    """
    from db.models import EventBoardEffect

    if dest <= start:
        return None
    path = list(range(start + 1, dest + 1))
    kinds = tile_bound_effects()
    if not kinds:
        return None
    rows = (session.query(EventBoardEffect)
            .filter(EventBoardEffect.event_id == event_id,
                    EventBoardEffect.effect_type.in_(list(kinds)),
                    EventBoardEffect.status == "active",
                    EventBoardEffect.target_tile_idx.in_(path))
            .all())
    for row in sorted(rows, key=lambda e: int(e.target_tile_idx)):
        tile_idx = int(row.target_tile_idx)
        landed = tile_idx == dest
        behavior = sanitize_behavior(row.effect_type,
                                     parse_config(row.effect_config))
        trig = resolve_tile_trigger(row.effect_type, behavior, landed=landed)
        if not trig.stops and not trig.consumed:
            # Inert for this crossing (e.g. a land-only trap being passed by
            # a non-stopping effect kind) — the piece sails on.
            continue
        if trig.consumed:
            row.status = "consumed"
        stopped = bool(trig.stops and not landed)
        return {
            "stop_at": tile_idx if stopped else dest,
            "stopped": stopped,
            "consumed": bool(trig.consumed),
            "stall_turns": int(trig.stall_turns or 0),
            "effect_id": row.id,
            "effect_type": row.effect_type,
            "placed_by_team_id": row.source_team_id,
            "behavior": behavior,
        }
    return None
