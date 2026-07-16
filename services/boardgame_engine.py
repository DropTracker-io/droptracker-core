"""Board-game event mode (web44a): settings, dice, the turn loop, coins.

The dice-board kind (``web_events.kind == 'board_game'``): each team stands
on one tile and has at most ONE live task (``EventBoardPosition.
current_task_id`` — the only task the matcher evaluates for it). Completing
that task awards coins and puts the team ``awaiting_roll``; the dice roll
(auto, or an explicit web/Activity action depending on config) moves the
piece X tiles forward and the landed tile assigns the next task. That whole
cycle is one turn (``turns_completed`` — the counter item cooldowns compare
against in P2).

Tile → task resolution is difficulty-first: a tile carrying ``difficulty``
ROLLS a random task from the event's own pool of that tier (different teams
get different tasks), while ``task_id`` pins one. Either way the landing
materializes a per-team INSTANCE task (a clone flagged
``config.board_instance``) so ``EventProgress (task, team)`` and the ledger
work unchanged and a re-landed pool task starts clean — the same pattern as
the bingo designer's ``bingo_auto`` tasks.

Everything tunable lives in ``web_event_board_config.settings`` (one JSON
document, §2.5 of docs/BOARD_GAME_EVENT_PLAN.md); :func:`board_settings`
overlays it on :data:`DEFAULT_BOARD_SETTINGS` key-by-key so a partial or
corrupt document never breaks a mechanic.

Transaction ownership: every function here expects the caller's session and
only flushes — apply-path callers (event_engine.apply_ledger_row) and the web
routes commit. Dice go through an injectable ``rng`` so tests are exact.
"""
from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timedelta
from typing import Optional

DEFAULT_BOARD_SETTINGS = {
    "movement": {
        "mode": "dice",          # dice | fixed_step
        "dice_count": 1,
        "dice_sides": 6,
        "fixed_step": 1,
        "trigger": "manual",     # auto | manual
        "manual_roller": "team",  # team | group_admin | either
    },
    "tile_render": {
        "mode": "rune",          # rune | invisible | outline
        "outline_width": 2,
        "outline_color": "#ffcc33",
        "show_labels": True,
        "icon_size": 20,         # px — tile icon size on the rendered board (8–64)
    },
    "coins": {
        "enabled": True,
        "per_difficulty": {"air": 10, "water": 20, "earth": 30, "fire": 50},
        "default": 10,           # tasks with no difficulty
        "starting": 0,
    },
    # Shop refresh cadence (web50a): "none" = stock never resets; "turns" =
    # restock every ``refresh_interval`` global turns; "hours" = every
    # ``refresh_interval`` hours. Defaults keep the pre-web50a behavior (no
    # refresh, unlimited stock unless a rotation row caps it).
    "shop": {"enabled": True, "refresh_mode": "none", "refresh_interval": 0},
    "items": {
        "enabled_item_ids": None,   # None = all active catalog items
        "disabled_effects": [],
        # Per-effect behavior overrides (the leader layer). Values here MUST
        # mirror services/boardgame_effects.EFFECT_REGISTRY defaults — they
        # exist so the merged settings document the web settings UI reads
        # always carries a complete behavior shape. True layering (registry
        # ← item effect_config ← leader override) resolves against the RAW
        # stored overrides via boardgame_effects.load_event_behavior_overrides.
        "behaviors": {
            "roadblock": {"break_on": "pass", "stall_turns": 1,
                          "visible_to_all": True, "expire_on_placer_move": True},
        },
    },
    "mercy": {"enabled": True, "base_hours": 24, "step_hours": 12},
    "win": {"rule": "finish_tile", "tiebreak": ["coins", "score"]},
}

# Tiles a rolled task may come from must be auto-evaluable — a rolled custom
# task could never complete on its own. Pinned tasks may be anything.
_ROLLABLE_TYPES = (
    "item_collection", "kc_target", "xp_target", "ehp_target", "ehb_target",
    "pb_target", "skill_target", "loot_value",
)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def board_settings(raw_json) -> dict:
    """The full §2.5 settings document: stored JSON overlaid on defaults.
    Corrupt/absent JSON → pure defaults (a bad config never breaks a turn)."""
    if not raw_json:
        return copy.deepcopy(DEFAULT_BOARD_SETTINGS)
    data = raw_json
    if not isinstance(data, dict):
        try:
            data = json.loads(raw_json)
        except (TypeError, ValueError):
            return copy.deepcopy(DEFAULT_BOARD_SETTINGS)
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULT_BOARD_SETTINGS)
    return _deep_merge(DEFAULT_BOARD_SETTINGS, data)


def load_board_settings(session, event_id: int) -> dict:
    from db.models import EventBoardConfig

    row = (session.query(EventBoardConfig)
           .filter(EventBoardConfig.event_id == event_id).first())
    return board_settings(row.settings if row else None)


def coin_reward(settings: dict, difficulty: Optional[str]) -> int:
    coins = settings.get("coins") or {}
    if not coins.get("enabled", True):
        return 0
    ladder = coins.get("per_difficulty") or {}
    try:
        if difficulty and difficulty in ladder:
            return max(0, int(ladder[difficulty]))
        return max(0, int(coins.get("default") or 0))
    except (TypeError, ValueError):
        return 0


def roll_dice(settings: dict, rng: Optional[random.Random] = None) -> list[int]:
    """The dice faces for one roll (``fixed_step`` mode returns one pseudo
    face of that size so movement code has a single shape)."""
    movement = settings.get("movement") or {}
    if movement.get("mode") == "fixed_step":
        try:
            return [max(1, int(movement.get("fixed_step") or 1))]
        except (TypeError, ValueError):
            return [1]
    rng = rng or random
    try:
        count = max(1, min(8, int(movement.get("dice_count") or 1)))
        sides = max(2, min(100, int(movement.get("dice_sides") or 6)))
    except (TypeError, ValueError):
        count, sides = 1, 6
    return [rng.randint(1, sides) for _ in range(count)]


def _mercy_deadline(settings: dict, mercy_count: int) -> Optional[datetime]:
    mercy = settings.get("mercy") or {}
    if not mercy.get("enabled", True):
        return None
    try:
        base = float(mercy.get("base_hours") or 24)
        step = float(mercy.get("step_hours") or 12)
    except (TypeError, ValueError):
        base, step = 24.0, 12.0
    return datetime.now() + timedelta(hours=base + step * max(0, int(mercy_count or 0)))


# --------------------------------------------------------------------------- #
# Board / tiles
# --------------------------------------------------------------------------- #
def load_tiles(session, event_id: int) -> list:
    from db.models import EventBoardTile

    return (session.query(EventBoardTile)
            .filter(EventBoardTile.event_id == event_id)
            .order_by(EventBoardTile.idx)
            .all())


def finish_idx(tiles: list) -> Optional[int]:
    """The finish tile's idx: an explicit ``tile_kind='finish'`` wins, else
    the last tile of the track."""
    if not tiles:
        return None
    for t in tiles:
        if t.tile_kind == "finish":
            return int(t.idx)
    return int(tiles[-1].idx)


def _is_board_instance(task) -> bool:
    try:
        cfg = json.loads(task.config) if task.config else {}
    except (TypeError, ValueError):
        return False
    return bool(isinstance(cfg, dict) and cfg.get("board_instance"))


def _task_pool(session, event_id: int, difficulty: Optional[str]) -> list:
    """The event's rollable pool tasks for one tier (instances excluded).
    Empty tier → any-tier fallback so a sparse pool can't strand a team."""
    from db.models import EventTask

    rows = (session.query(EventTask)
            .filter(EventTask.event_id == event_id,
                    EventTask.type.in_(_ROLLABLE_TYPES))
            .all())
    pool = [t for t in rows if not _is_board_instance(t)]
    if difficulty:
        tiered = [t for t in pool if (t.difficulty or None) == difficulty]
        if tiered:
            return tiered
    return pool


def _materialize_instance(session, event_id: int, team_id: int, source_task,
                          turn_number: int):
    """Clone a pool/pinned task into this team's per-landing instance so its
    progress rollup is isolated (the bingo_auto pattern). Never library-saved."""
    from db.models import EventTask

    try:
        cfg = json.loads(source_task.config) if source_task.config else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except (TypeError, ValueError):
        cfg = {}
    cfg.update({
        "board_instance": True,
        "source_task_id": source_task.id,
        "team_id": team_id,
        "turn": int(turn_number),
    })
    instance = EventTask(
        event_id=event_id,
        type=source_task.type,
        label=source_task.label,
        target=source_task.target,
        target_value=source_task.target_value,
        points=int(source_task.points or 0),
        requires_confirmation=bool(source_task.requires_confirmation),
        config=json.dumps(cfg),
        visibility="private",
        difficulty=source_task.difficulty,
    )
    session.add(instance)
    session.flush()
    return instance


def assign_tile_task(session, event_id: int, team_id: int, tile, position,
                     settings: dict, rng: Optional[random.Random] = None):
    """Resolve the landed tile into the team's next live task (or None for a
    rest tile) and stamp the position. Caller flushes/commits."""
    from db.models import EventTask

    # P1-8: landing a new tile invalidates any choose_task candidates banked on
    # the previous tile — otherwise a team could draw easy candidates, roll onto
    # a hard tile, then apply the banked easy pick. The legit flow (draw → pick
    # without rolling) never passes through here, so it's unaffected.
    position.pending_choice = None

    source = None
    if tile is not None and tile.task_id:
        source = session.query(EventTask).filter(EventTask.id == tile.task_id).first()
    elif tile is not None and tile.difficulty:
        pool = _task_pool(session, event_id, tile.difficulty)
        if pool:
            source = (rng or random).choice(pool)

    if source is None:
        # Rest tile (or empty pool): no live task; the team may roll again.
        position.current_task_id = None
        position.status = "awaiting_roll"
        position.task_assigned_at = None
        position.mercy_deadline = None
        return None

    instance = _materialize_instance(
        session, event_id, team_id, source, position.turns_completed)
    position.current_task_id = instance.id
    position.status = "active"
    position.task_assigned_at = datetime.now()
    position.mercy_deadline = _mercy_deadline(settings, position.mercy_count)
    return instance


# --------------------------------------------------------------------------- #
# Coins
# --------------------------------------------------------------------------- #
def award_coins(session, event_id: int, team, delta: int, reason: str,
                ref_type: Optional[str] = None, ref_id=None,
                acted_by_user_id=None, note=None) -> int:
    """Adjust a team's wallet (running balance + audit row) and return the new
    balance.

    P0-3: lock the wallet row (SELECT … FOR UPDATE) before the read-modify-write
    so it self-serializes. The bare RMW lost updates whenever the serial
    consumer's task-reward credit (handle_board_completion) raced a webapi
    purchase (buy_item) — one team got free coins/items and the ledger's
    balance_after drifted from EventTeam.coins. buy_item already held the row
    lock, so re-locking there is a no-op; the previously-unlocked consumer/toll
    credits now serialize. Lock order is position→team everywhere that holds
    both, so no new deadlock surface."""
    from db.models import EventCoinLedger, EventTeam

    delta = int(delta)
    locked = (session.query(EventTeam)
              .filter(EventTeam.id == team.id)
              .with_for_update().first())
    row = locked if locked is not None else team
    balance = int(row.coins or 0) + delta
    row.coins = balance
    # Keep the caller's object consistent if the locked read returned a
    # different identity (it won't under the ORM identity map, but be safe).
    if row is not team:
        team.coins = balance
    session.add(EventCoinLedger(
        event_id=event_id, team_id=team.id, delta=delta, reason=reason,
        ref_type=ref_type, ref_id=ref_id, balance_after=balance,
        acted_by_user_id=acted_by_user_id, note=note,
    ))
    session.flush()
    return balance


# --------------------------------------------------------------------------- #
# The turn loop
# --------------------------------------------------------------------------- #
def seed_positions(session, event) -> int:
    """Activation duty: every team gets a position on tile 0 with its first
    task assigned (and starting coins when configured). Idempotent — existing
    rows are left alone. Returns how many teams were seeded."""
    from db.models import EventBoardPosition, EventTeam

    settings = load_board_settings(session, event.id)
    tiles = load_tiles(session, event.id)
    start_tile = tiles[0] if tiles else None
    seeded = 0
    existing = {
        p.team_id
        for p in session.query(EventBoardPosition)
        .filter(EventBoardPosition.event_id == event.id).all()
    }
    starting = 0
    try:
        starting = max(0, int((settings.get("coins") or {}).get("starting") or 0))
    except (TypeError, ValueError):
        pass
    for team in session.query(EventTeam).filter(EventTeam.event_id == event.id).all():
        if team.id in existing:
            continue
        pos = EventBoardPosition(
            team_id=team.id, event_id=event.id, tile_idx=0,
            turns_completed=0, status="active",
        )
        session.add(pos)
        session.flush()
        assign_tile_task(session, event.id, team.id, start_tile, pos, settings)
        if starting:
            award_coins(session, event.id, team, starting, "bonus",
                        ref_type="seed", note="starting coins")
        seeded += 1
    session.flush()
    return seeded


def handle_board_completion(session, redis_conn, event: dict, task: dict,
                            team_id: int, player_id=None,
                            rng: Optional[random.Random] = None) -> Optional[dict]:
    """Board side-effects of a newly-completed task (called from
    event_engine.apply_ledger_row inside the caller's transaction):
    coins → ``awaiting_roll`` → auto-roll when configured. Returns a summary
    dict for the SSE frame / notification payload, or None when this
    completion isn't the team's live board task."""
    from db.models import EventBoardPosition, EventTeam

    pos = (session.query(EventBoardPosition)
           .filter(EventBoardPosition.team_id == team_id).first())
    if pos is None or pos.event_id != event["id"]:
        return None
    if pos.current_task_id != task["id"]:
        # A stale/parallel completion (old instance, pinned task done twice…)
        # — score already handled upstream; no board movement.
        return None

    settings = load_board_settings(session, event["id"])
    board: dict = {"team_id": team_id, "turn": int(pos.turns_completed or 0)}

    team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
    difficulty = task.get("difficulty")
    coins = coin_reward(settings, difficulty)
    if team is not None and coins > 0:
        # An armed coin boost (shop, web45a) multiplies this completion.
        try:
            from services.boardgame_shop import consume_coin_boost

            multiplier = consume_coin_boost(session, event["id"], team_id)
        except Exception:
            multiplier = 1
        if multiplier > 1:
            coins *= multiplier
            board["coin_multiplier"] = multiplier
        board["coins_awarded"] = coins
        board["coin_balance"] = award_coins(
            session, event["id"], team, coins, "task_reward",
            ref_type="task", ref_id=task["id"],
        )

    pos.status = "awaiting_roll"
    pos.mercy_deadline = None
    session.flush()

    movement = settings.get("movement") or {}
    if (movement.get("trigger") or "manual") == "auto":
        roll = perform_roll(session, redis_conn, event["id"], team_id,
                            settings=settings, rng=rng)
        if roll:
            board["roll"] = roll
    return board


def perform_roll(session, redis_conn, event_id: int, team_id: int,
                 settings: Optional[dict] = None,
                 rng: Optional[random.Random] = None,
                 acted_by_user_id=None) -> Optional[dict]:
    """One dice roll: move the piece, resolve the landing, assign the next
    task. Caller guarantees the team is ``awaiting_roll`` or ``blocked`` (the
    web route validates + 409s; the auto path just set it). Returns the roll
    summary {dice, from, to, won, task_id?, task_label?}, a stall summary
    {blocked: True, ...} while a tile effect is holding the team (the attempt
    is consumed, the piece stays put), or None when the position is missing/
    ineligible. Caller commits."""
    from db.models import EventBoardPosition

    # Lock the position row for the whole roll so two members clicking Roll
    # within the same instant serialize: the second waits, then re-reads a
    # status that is no longer awaiting_roll/blocked and no-ops (P0-2). Without
    # this both rolls double-apply — doubled turns, orphaned instance tasks,
    # roadblocks/tolls consumed twice.
    pos = (session.query(EventBoardPosition)
           .filter(EventBoardPosition.team_id == team_id)
           .with_for_update().first())
    if pos is None or pos.event_id != event_id:
        return None
    if pos.status not in ("awaiting_roll", "blocked"):
        return None

    settings = settings or load_board_settings(session, event_id)
    tiles = load_tiles(session, event_id)
    if not tiles:
        return None

    if pos.status == "blocked":
        # A tile effect (roadblock stall) holds the team: this attempt IS
        # the lost turn — no dice, no movement.
        return _serve_blocked_turn(session, redis_conn, event_id, team_id,
                                   pos, tiles, settings, rng=rng)

    faces = roll_dice(settings, rng)

    # web50a movement modifiers (armed by shop items, drained here):
    #  - choose_roll forces this roll to an exact value (wins over extra_dice).
    #  - extra_dice adds N dice to the roll (skipped in fixed_step mode).
    forced = _consume_choose_roll(session, event_id, team_id)
    if forced is not None:
        faces = [int(forced)]
    else:
        extra = _consume_extra_dice(session, event_id, team_id)
        movement = settings.get("movement") or {}
        if extra > 0 and movement.get("mode") != "fixed_step":
            try:
                sides = max(2, min(100, int(movement.get("dice_sides") or 6)))
            except (TypeError, ValueError):
                sides = 6
            faces = list(faces) + [
                (rng or random).randint(1, sides) for _ in range(extra)]

    start = int(pos.tile_idx or 0)
    steps = sum(faces)

    # Frozen (P3 freeze_opponent): the roll happens but the piece stays put —
    # one charge of the freeze is consumed per roll.
    frozen = _consume_freeze_charge(session, event_id, team_id)
    if frozen:
        steps = 0

    pos.turns_completed = int(pos.turns_completed or 0) + 1
    summary = _move_piece(session, event_id, team_id, pos, tiles, start, steps,
                          settings, rng=rng)
    summary["dice"] = faces
    if frozen:
        summary["frozen"] = True
    pos.last_roll = json.dumps({
        "dice": faces, "from": start, "to": summary["to"],
        "at": int(datetime.now().timestamp()),
        **({"frozen": True} if frozen else {}),
    })
    summary["turn"] = pos.turns_completed
    session.flush()

    # Live board frame for the web/Activity views (SSE scope event:{id}).
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_roll", "event_id": event_id, "team_id": team_id,
            "dice": faces, "from": start, "to": summary["to"],
            "won": summary["won"], "task_label": summary.get("task_label"),
            "blocked": bool(summary.get("blocked")),
        })
    except Exception:
        pass

    # The matcher caches (event, team) -> current task; nudge every consumer
    # to reload so the new instance starts matching immediately.
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(event_id)
    except Exception:
        pass
    return summary


def _consume_freeze_charge(session, event_id: int, team_id: int) -> bool:
    """One charge of an active freeze on this team (P3): True when frozen.
    Decrements config.remaining; the effect expires at zero."""
    from db.models import EventBoardEffect

    effect = (session.query(EventBoardEffect)
              .filter(EventBoardEffect.event_id == event_id,
                      EventBoardEffect.target_team_id == team_id,
                      EventBoardEffect.effect_type == "freeze_opponent",
                      EventBoardEffect.status == "active")
              .first())
    if effect is None:
        return False
    try:
        cfg = json.loads(effect.effect_config or "{}")
        if not isinstance(cfg, dict):
            cfg = {}
    except (TypeError, ValueError):
        cfg = {}
    remaining = int(cfg.get("remaining", cfg.get("turns", 1)) or 1) - 1
    cfg["remaining"] = remaining
    effect.effect_config = json.dumps(cfg)
    if remaining <= 0:
        effect.status = "consumed"
    session.flush()
    return True


def _effect_cfg(raw) -> dict:
    """Parse an EventBoardEffect.effect_config JSON blob (corrupt → {})."""
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _consume_choose_roll(session, event_id: int, team_id: int) -> Optional[int]:
    """Drain an armed choose_roll (web50a): the forced roll VALUE for this roll,
    or None when none is armed. Consumes the effect. A corrupt/missing value
    falls back to a normal roll (the effect is still spent)."""
    from db.models import EventBoardEffect

    effect = (session.query(EventBoardEffect)
              .filter(EventBoardEffect.event_id == event_id,
                      EventBoardEffect.target_team_id == team_id,
                      EventBoardEffect.effect_type == "choose_roll",
                      EventBoardEffect.status == "active")
              .first())
    if effect is None:
        return None
    effect.status = "consumed"
    session.flush()
    try:
        return int(_effect_cfg(effect.effect_config).get("value"))
    except (TypeError, ValueError):
        return None


def _consume_extra_dice(session, event_id: int, team_id: int) -> int:
    """Drain an armed extra_dice (web50a): the number of EXTRA dice this roll
    adds (0 when none armed). Consumes the effect."""
    from db.models import EventBoardEffect

    effect = (session.query(EventBoardEffect)
              .filter(EventBoardEffect.event_id == event_id,
                      EventBoardEffect.target_team_id == team_id,
                      EventBoardEffect.effect_type == "extra_dice",
                      EventBoardEffect.status == "active")
              .first())
    if effect is None:
        return 0
    effect.status = "consumed"
    session.flush()
    try:
        return max(0, min(8, int(_effect_cfg(effect.effect_config).get("extra_dice", 1))))
    except (TypeError, ValueError):
        return 1


def _apply_coin_toll(session, event_id: int, team_id: int, start: int,
                     dest: int) -> Optional[dict]:
    """Drain an armed coin_toll (web50a) for a move over (start, dest]: steal
    ``coins_per_team`` from every OTHER team standing on a passed-over tile,
    crediting the mover (both sides get a coin-ledger row; each victim keeps a
    non-negative balance). Consumed after the move. Returns a summary or None
    when no toll is armed. Caller (movement core) flushes."""
    from db.models import EventBoardEffect, EventBoardPosition, EventTeam

    effect = (session.query(EventBoardEffect)
              .filter(EventBoardEffect.event_id == event_id,
                      EventBoardEffect.target_team_id == team_id,
                      EventBoardEffect.effect_type == "coin_toll",
                      EventBoardEffect.status == "active")
              .first())
    if effect is None:
        return None
    effect.status = "consumed"
    try:
        per_team = max(0, int(_effect_cfg(effect.effect_config).get("coins_per_team", 25)))
    except (TypeError, ValueError):
        per_team = 25
    stolen: list = []
    total = 0
    if per_team > 0 and dest > start:
        passed = set(range(start + 1, dest + 1))
        victims = [
            p for p in session.query(EventBoardPosition)
            .filter(EventBoardPosition.event_id == event_id).all()
            if p.team_id != team_id and int(p.tile_idx or 0) in passed
        ]
        mover = (session.query(EventTeam)
                 .filter(EventTeam.id == team_id).first()) if victims else None
        for vp in victims:
            vteam = (session.query(EventTeam)
                     .filter(EventTeam.id == vp.team_id).first())
            if vteam is None:
                continue
            amt = min(per_team, max(0, int(vteam.coins or 0)))
            if amt <= 0:
                continue
            award_coins(session, event_id, vteam, -amt, "toll",
                        ref_type="toll", ref_id=team_id, note="coin_toll")
            total += amt
            stolen.append({"team_id": vp.team_id, "coins": amt})
        if total > 0 and mover is not None:
            award_coins(session, event_id, mover, total, "toll",
                        ref_type="toll", ref_id=team_id, note="coin_toll")
    session.flush()
    return {"stolen": stolen, "total": total}


def _expire_placer_roadblocks_on_move(session, event_id: int, team_id: int) -> list:
    """When a team moves (web50a), expire its OWN still-active tile-bound
    effects whose resolved behavior sets ``expire_on_placer_move`` (default
    true) — so a placing team is not permanently hampered by its own bulwarks.
    Re-arming a bulwark consumed mid-move by this same move is out of scope (a
    move never re-arms). Returns the tile idxs expired."""
    from db.models import EventBoardEffect
    from services.boardgame_effects import (
        parse_config,
        sanitize_behavior,
        tile_bound_effects,
    )

    kinds = list(tile_bound_effects())
    if not kinds:
        return []
    rows = (session.query(EventBoardEffect)
            .filter(EventBoardEffect.event_id == event_id,
                    EventBoardEffect.source_team_id == team_id,
                    EventBoardEffect.effect_type.in_(kinds),
                    EventBoardEffect.status == "active")
            .all())
    expired = []
    for r in rows:
        behavior = sanitize_behavior(r.effect_type, parse_config(r.effect_config))
        if behavior.get("expire_on_placer_move", True):
            r.status = "expired"
            expired.append(int(r.target_tile_idx)
                           if r.target_tile_idx is not None else None)
    if expired:
        session.flush()
    return expired


def _serve_blocked_turn(session, redis_conn, event_id: int, team_id: int,
                        pos, tiles: list, settings: dict,
                        rng: Optional[random.Random] = None) -> dict:
    """A roll attempt while a tile effect has the team ``blocked``: the
    attempt itself is the lost turn — ``turns_completed`` ticks, the piece
    stays put, no dice are thrown. When the counter reaches
    ``blocked_until_turn`` the block clears and the tile's task is assigned,
    resuming the normal loop. ``last_roll`` is left alone (the last real
    dice animation stays valid). Returns a {blocked: True, ...} summary the
    roll route surfaces as-is."""
    pos.turns_completed = int(pos.turns_completed or 0) + 1
    tile_idx = int(pos.tile_idx or 0)
    summary: dict = {"blocked": True, "from": tile_idx, "to": tile_idx,
                     "dice": [], "won": False, "turn": pos.turns_completed}
    until = pos.blocked_until_turn
    if until is None or pos.turns_completed >= int(until):
        # Stall served (or corrupt marker — fail open): back to normal play.
        pos.blocked_until_turn = None
        by_idx = {int(t.idx): t for t in tiles}
        instance = assign_tile_task(session, event_id, team_id,
                                    by_idx.get(tile_idx), pos, settings,
                                    rng=rng)
        summary["blocked_cleared"] = True
        if instance is not None:
            summary["task_id"] = instance.id
            summary["task_label"] = instance.label
            summary["task_difficulty"] = instance.difficulty
        # New live task = new matcher target.
        try:
            from services.event_engine import publish_event_admin_bump

            publish_event_admin_bump(event_id)
        except Exception:
            pass
    else:
        summary["stall_remaining"] = int(until) - int(pos.turns_completed)
    session.flush()
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_blocked", "event_id": event_id, "team_id": team_id,
            "tile_idx": tile_idx,
            "cleared": bool(summary.get("blocked_cleared")),
            "stall_remaining": int(summary.get("stall_remaining", 0)),
        })
    except Exception:
        pass
    return summary


def _move_piece(session, event_id: int, team_id: int, pos, tiles: list,
                start: int, steps: int, settings: dict,
                rng: Optional[random.Random] = None) -> dict:
    """Shared movement/landing core (rolls AND the advance power-up):
    tile-effect resolution (roadblocks & future traps), finish clamp, landing
    task resolution. Mutates ``pos``; caller stamps turn counters/last_roll
    and flushes."""
    fin = finish_idx(tiles)
    by_idx = {int(t.idx): t for t in tiles}
    dest = min(start + max(0, steps), fin)

    summary: dict = {"from": start, "to": dest, "won": False}

    # Tile-bound effects (roadblock is the first consumer): the nearest
    # triggering one on the path may stop the piece, be consumed, and/or
    # stall the team — all semantics come from the effect's placement-time
    # behavior snapshot via services.boardgame_effects, never hardcoded
    # here. NO placer immunity: the placing team hits its own traps.
    blocked_stall = 0
    if dest > start:
        from services.boardgame_effects import apply_tile_effects_on_path

        hit = apply_tile_effects_on_path(session, event_id, team_id, start, dest)
        if hit is not None:
            dest = int(hit["stop_at"])
            summary["to"] = dest
            summary["tile_effect"] = {
                "effect_type": hit["effect_type"],
                "tile_idx": int(hit["stop_at"]),
                "placed_by_team_id": hit["placed_by_team_id"],
                "stopped": hit["stopped"],
                "consumed": hit["consumed"],
                "stall_turns": hit["stall_turns"],
            }
            if hit["effect_type"] == "roadblock":
                summary["roadblock"] = {
                    "tile_idx": dest,
                    "placed_by_team_id": hit["placed_by_team_id"],
                    "consumed": hit["consumed"],
                }
            if hit["stopped"] and int(hit["stall_turns"] or 0) > 0:
                blocked_stall = int(hit["stall_turns"])

    # Movement-triggered economics + self-trap expiry (web50a). On any real
    # advance (rolls AND teleports), over the SAME (start, dest] passed range
    # the roadblock resolver walks — dest is already clamped to a mid-path
    # stop: coin_toll tolls every other team on a passed tile, and the mover
    # expires its own expire_on_placer_move bulwarks.
    if dest > start:
        toll = _apply_coin_toll(session, event_id, team_id, start, dest)
        if toll and toll.get("total"):
            summary["coin_toll"] = toll
        expired = _expire_placer_roadblocks_on_move(session, event_id, team_id)
        if expired:
            summary["expired_roadblocks"] = expired

    if blocked_stall > 0:
        # Lose-a-turn: park the piece with no task; perform_roll serves the
        # stall (one consumed attempt per stalled turn).
        pos.tile_idx = dest
        pos.status = "blocked"
        pos.blocked_until_turn = int(pos.turns_completed or 0) + blocked_stall
        pos.current_task_id = None
        pos.task_assigned_at = None
        pos.mercy_deadline = None
        summary["blocked"] = True
        summary["blocked_until_turn"] = pos.blocked_until_turn
        return summary

    pos.tile_idx = dest
    if dest >= fin:
        pos.status = "finished"
        pos.current_task_id = None
        pos.mercy_deadline = None
        summary["won"] = True
    else:
        instance = assign_tile_task(
            session, event_id, team_id, by_idx.get(dest), pos, settings, rng=rng)
        if instance is not None:
            summary["task_id"] = instance.id
            summary["task_label"] = instance.label
            summary["task_difficulty"] = instance.difficulty
    return summary


def can_trigger_roll(settings: dict, *, is_team_member: bool, is_admin: bool) -> bool:
    """Who may fire a manual roll (movement.manual_roller)."""
    roller = ((settings.get("movement") or {}).get("manual_roller")) or "team"
    if is_admin:
        # Group admins/superadmins can always unstick a team.
        return True
    if roller in ("team", "either"):
        return is_team_member
    return False


# --------------------------------------------------------------------------- #
# Mercy sweep (anti-stall)
# --------------------------------------------------------------------------- #
def mercy_sweep(session, redis_conn, now: Optional[datetime] = None) -> list:
    """Auto-complete overdue live tasks (the legacy mercy rule): zero-coin
    completion, bump mercy_count (the deadline grows next turn), then the
    normal awaiting_roll/auto-roll flow. Called from the consumer's lifecycle
    tick; returns [{event_id, team_id}] for logging."""
    from db.models import Event, EventBoardPosition, EventProgress

    now = now or datetime.now()
    swept = []
    rows = (session.query(EventBoardPosition, Event)
            .join(Event, Event.id == EventBoardPosition.event_id)
            .filter(Event.status == "active",
                    EventBoardPosition.status == "active",
                    EventBoardPosition.mercy_deadline.isnot(None),
                    EventBoardPosition.mercy_deadline <= now)
            .all())
    for pos, ev in rows:
        settings = load_board_settings(session, ev.id)
        if not (settings.get("mercy") or {}).get("enabled", True):
            pos.mercy_deadline = None
            continue
        # Mark the task's rollup complete (no score, no coins — mercy is a
        # release valve, not a reward).
        if pos.current_task_id:
            progress = (session.query(EventProgress)
                        .filter(EventProgress.task_id == pos.current_task_id,
                                EventProgress.team_id == pos.team_id)
                        .first())
            if progress is None:
                progress = EventProgress(
                    event_id=ev.id, task_id=pos.current_task_id,
                    team_id=pos.team_id, progress=0)
                session.add(progress)
            progress.completed = True
            progress.completed_at = now
        pos.mercy_count = int(pos.mercy_count or 0) + 1
        pos.status = "awaiting_roll"
        pos.current_task_id = None
        pos.mercy_deadline = None
        session.flush()
        movement = settings.get("movement") or {}
        if (movement.get("trigger") or "manual") == "auto":
            perform_roll(session, redis_conn, ev.id, pos.team_id, settings=settings)
        swept.append({"event_id": ev.id, "team_id": pos.team_id})
    return swept
