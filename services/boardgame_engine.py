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
    # Board style (2026-09): a preset the designer and the player copy key off.
    # "race" = the original dice track; "chutes_ladders" = the numbered grid
    # with tile links. The engine reads tiles, not the style — a race board
    # with a link on it slides just the same.
    "style": "race",
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
    # Shop restock cadence: "none" = stock never resets; "turns" = restock every
    # ``refresh_interval`` global turns; "hours"/"days" = every that many hours
    # or days of wall-clock. A restock re-opens every capped item's stock (the
    # ones that could sell out) to its stock_per_refresh, so bought-out power-ups
    # return. ``refresh_random`` (time modes only) jitters each interval to a
    # random moment in 50–150% of the base so players can't time the restock.
    # Defaults keep the original behavior (no refresh, unlimited stock unless a
    # rotation row caps it).
    "shop": {"enabled": True, "refresh_mode": "none", "refresh_interval": 0,
             "refresh_random": False},
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
    # Win = first team to reach the finish tile. ``tiebreak`` breaks ties among
    # teams that have BOTH finished (or, at a manual end, ranks unfinished teams
    # after the leader): the ordered token list is applied left→right by
    # event_lifecycle.final_standings — "score" = task points, "coins" = wallet.
    # ``exact_finish`` (2026-09): what a roll that would carry the piece PAST
    # the finish does — "off" lands on the finish anyway (the original clamp),
    # "stay" loses the move (the team rolls again from where it stood),
    # "bounce" walks the excess back from the finish.
    "win": {"rule": "finish_tile", "tiebreak": ["score"], "exact_finish": "off"},
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


def _normalize_movement(settings: dict) -> dict:
    """Collapse a 1-sided die into the ``fixed_step`` shape it is identical to.

    ``Nd1`` always rolls exactly N, which is precisely ``fixed_step: N`` — the
    same movement reached through the control leaders find more intuitive. The
    designer lets you say it either way; this makes both store and read as one
    canonical deterministic shape, so the engine, the shop's choose_roll range
    check and every embed stop having two look-alike modes to reason about.

    Mutates and returns ``settings`` (callers own a fresh dict from
    ``_deep_merge``). Only an exact ``dice_sides == 1`` normalizes — genuine
    garbage stays with the clamps in roll_dice.
    """
    movement = settings.get("movement")
    if not isinstance(movement, dict) or movement.get("mode") == "fixed_step":
        return settings
    try:
        sides = int(movement.get("dice_sides") or 6)
    except (TypeError, ValueError):
        return settings
    if sides != 1:
        return settings
    try:
        count = max(1, min(8, int(movement.get("dice_count") or 1)))
    except (TypeError, ValueError):
        count = 1
    movement["mode"] = "fixed_step"
    movement["fixed_step"] = count
    # Put the die back to the default size. Leaving a 1 behind would re-collapse
    # the document the moment the leader picked "Dice roll" again, trapping them
    # in fixed_step with no way back through the UI.
    movement["dice_sides"] = DEFAULT_BOARD_SETTINGS["movement"]["dice_sides"]
    return settings


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
    return _normalize_movement(_deep_merge(DEFAULT_BOARD_SETTINGS, data))


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
        sides = max(1, min(100, int(movement.get("dice_sides") or 6)))
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


# Tile kinds + per-tile config (EventBoardTile.config, JSON).
# Mirrors db.models.events (kept literal so this module stays importable in
# the unit tests, which stub the db package).
_TILE_KIND_ALIASES = {"special": "required"}
_JUMP_TRIGGERS = ("land", "complete")


def tile_kind(tile) -> str:
    """The tile's role with the legacy ``special`` rows read as ``required``
    (the rename shipped with the checkpoint semantics; web115a rewrites the
    stored rows, this keeps an unmigrated board playable meanwhile)."""
    raw = (getattr(tile, "tile_kind", None) or "normal") if tile is not None else "normal"
    return _TILE_KIND_ALIASES.get(raw, raw)


def tile_config(tile) -> dict:
    raw = getattr(tile, "config", None) if tile is not None else None
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def tile_jump(tile) -> Optional[tuple[int, str]]:
    """``(target_idx, trigger)`` for a linked tile (a chute or a ladder), or
    None. ``trigger`` is "land" (fires the moment a piece lands — the classic
    rule) or "complete" (a ladder the team must earn by finishing the tile's
    task; never valid on a chute, the validator refuses it)."""
    cfg = tile_config(tile)
    target = cfg.get("jump_to")
    if target is None or isinstance(target, bool):
        return None
    try:
        target = int(target)
    except (TypeError, ValueError):
        return None
    if target < 0 or target == int(getattr(tile, "idx", -1)):
        return None
    when = cfg.get("jump_when") or "land"
    if when not in _JUMP_TRIGGERS or (when == "complete" and target < int(tile.idx)):
        when = "land"
    return target, when


def tile_has_task(tile) -> bool:
    """Whether landing here draws or pins a task (vs a rest / plain finish)."""
    if tile is None:
        return False
    return bool(getattr(tile, "task_id", None) or getattr(tile, "difficulty", None))


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
                          turn_number: int, tile_idx: Optional[int] = None):
    """Clone a pool/pinned task into this team's per-landing instance so its
    progress rollup is isolated (the bingo_auto pattern). Never library-saved.
    ``tile_idx`` records WHICH tile the instance was drawn for — how the
    required-tile rule knows a team already cleared a checkpoint."""
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
    if tile_idx is not None:
        cfg["tile_idx"] = int(tile_idx)
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
        session, event_id, team_id, source, position.turns_completed,
        tile_idx=int(tile.idx) if tile is not None else None)
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
    coins_enabled = bool((settings.get("coins") or {}).get("enabled", True))
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
        # C4: the coins kill switch also suppresses the starting grant — a
        # coinless event must not seed a wallet.
        if starting and coins_enabled:
            award_coins(session, event.id, team, starting, "bonus",
                        ref_type="seed", note="starting coins")
        # Auto mode: a rest/empty START tile (the usual tile_kind='start')
        # leaves no live task and nothing to fire the first roll, so the team
        # would sit forever at tile 0. Advance it off the start (P1a). No-op in
        # manual mode and when tile 0 already assigned a task.
        auto_advance(session, None, event.id, team.id, settings)
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

    pos.mercy_deadline = None

    # Where the completion happened decides what it unlocks (2026-09):
    # an earned ladder lifts the team; the finish tile's own task IS the
    # win; anywhere else the team is simply ready to roll.
    tiles = load_tiles(session, event["id"])
    by_idx = {int(t.idx): t for t in tiles}
    fin = finish_idx(tiles)
    here = int(pos.tile_idx or 0)
    jump = tile_jump(by_idx.get(here))
    if (jump is not None and jump[1] == "complete" and jump[0] > here
            and jump[0] in by_idx):
        # A "climb when completed" ladder: the climb is the reward, so the
        # team lands at the top awaiting its roll rather than drawing a second
        # task — unless the top is the finish, which resolves like any finish
        # landing (a plain finish wins outright, a finish with a task assigns
        # it).
        target = jump[0]
        board["jump"] = {"kind": "ladder", "from": here, "to": target}
        top = by_idx.get(target)
        pos.tile_idx = target
        pos.current_task_id = None
        pos.task_assigned_at = None
        if fin is not None and target >= fin:
            if tile_has_task(top):
                instance = assign_tile_task(session, event["id"], team_id, top,
                                            pos, settings, rng=rng)
                board["finish_task"] = True
                if instance is not None:
                    board["task_id"] = instance.id
                    board["task_label"] = instance.label
                    board["task_difficulty"] = instance.difficulty
            else:
                pos.status = "finished"
                pos.mercy_deadline = None
                board["won"] = True
        else:
            pos.status = "awaiting_roll"
        session.flush()
    elif fin is not None and here >= fin:
        # The finish tile carried a task (a "required" finish): completing it
        # is how the team wins. The consumer ends the event off ``won``.
        pos.status = "finished"
        pos.current_task_id = None
        board["won"] = True
        session.flush()
        return board
    else:
        pos.status = "awaiting_roll"
        session.flush()

    movement = settings.get("movement") or {}
    if pos.status == "awaiting_roll" and (movement.get("trigger") or "manual") == "auto":
        # auto_advance rolls once and keeps rolling through rest tiles / stalls
        # so the game can't strand itself (P1a); its last summary's ``won`` flag
        # is what the consumer checks to end the event on a finish.
        roll = auto_advance(session, redis_conn, event["id"], team_id,
                            settings, rng=rng)
        if roll:
            board["roll"] = roll
    return board


def _line_for_jump(jump: Optional[dict]) -> Optional[str]:
    if not jump:
        return None
    if jump.get("kind") == "chute":
        return (f"\U0001F573\ufe0f Slid down a chute from tile `{jump.get('from')}` "
                f"to tile `{jump.get('to')}`!")
    return (f"\U0001FA9C Climbed a ladder from tile `{jump.get('from')}` "
            f"to tile `{jump.get('to')}`!")


def turn_notification_data(*, team_id: int, team_name=None, player_name=None,
                           roll: Optional[dict] = None,
                           board: Optional[dict] = None) -> dict:
    """The one payload every board-turn announcement is built from — the auto
    roll after a completion, a manual roll, and the roll-less turns (an earned
    ladder, a finish-task win). Both renderers (the V2 layout and the legacy
    embed) read these keys, and the pre-composed ``*_line`` tokens keep them
    saying the same thing; an absent line drops out of the layout."""
    roll = roll or {}
    board = board or {}
    dice = roll.get("dice") or []
    jump = roll.get("jump") or board.get("jump")
    required = roll.get("required_stop")
    overshoot = roll.get("overshoot")
    won = bool(roll.get("won") or board.get("won"))
    finish_task = bool(roll.get("finish_task") or board.get("finish_task"))
    tile_from = roll.get("from") if roll else (jump or {}).get("from")
    tile_to = roll.get("to") if roll else (jump or {}).get("to")
    turn = roll.get("turn") if roll.get("turn") is not None else board.get("turn")
    data = {
        "team_id": team_id,
        "team_name": team_name,
        "player_name": player_name,
        "dice": dice,
        "dice_str": " + ".join(str(d) for d in dice) or "?",
        "tile_from": tile_from,
        "tile_to": tile_to,
        "turn": turn,
        "won": won,
        # A win the dice did not deliver: the finish tile's task, or a ladder
        # straight onto a plain finish.
        "won_by_task": bool(board.get("won") and not roll.get("won") and not jump),
        "won_by_ladder": bool(won and jump and int(jump.get("to", -1)) == int(tile_to or -2)),
        "next_task_label": roll.get("task_label") or board.get("task_label") or "—",
        "coins_awarded": board.get("coins_awarded") or 0,
        "coin_balance": board.get("coin_balance") or 0,
        "jump": jump,
        "required_stop": required,
        "overshoot": overshoot,
        "finish_task": finish_task,
    }
    lines = {
        "jump_line": _line_for_jump(jump),
        "required_line": (
            f"\u26d4 Stopped at required tile `{required.get('tile_idx')}` — "
            "it must be completed before moving on."
            if required else None),
        "overshoot_line": None,
        "finish_line": ("\U0001F3C1 On the finish tile — complete its task to win!"
                        if finish_task and not won else None),
    }
    if overshoot:
        if overshoot.get("mode") == "stay":
            lines["overshoot_line"] = (
                f"\u21a9\ufe0f Overshot the finish by {overshoot.get('by')} — "
                "the move is lost, roll again.")
        else:
            lines["overshoot_line"] = (
                f"\u21a9\ufe0f Overshot the finish by {overshoot.get('by')} and "
                f"bounced back to tile `{overshoot.get('to')}`.")
    data.update({k: v for k, v in lines.items() if v})
    return data


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
        # extra_dice is meaningless in fixed_step mode — check the mode FIRST so
        # the armed effect (and its type-cooldown) is not burned for nothing
        # (E2). It is only drained when it can actually add dice.
        movement = settings.get("movement") or {}
        if movement.get("mode") != "fixed_step":
            extra = _consume_extra_dice(session, event_id, team_id)
            if extra > 0:
                try:
                    sides = max(1, min(100, int(movement.get("dice_sides") or 6)))
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
            "jump": summary.get("jump"),
            "required_stop": summary.get("required_stop"),
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


def auto_advance(session, redis_conn, event_id: int, team_id: int,
                 settings: dict, rng: Optional[random.Random] = None,
                 max_rolls: int = 200) -> Optional[dict]:
    """Auto-trigger duty (P1a): keep rolling while the team is *rollable* but
    has no live task — a rest / empty-pool landing (``awaiting_roll`` with no
    ``current_task_id``) or a roadblock stall (``blocked``) — so an ``auto``
    game never dead-ends waiting for a manual roll that can never come (in auto
    mode the roll route forbids member rolls, and mercy only sweeps ``active``).

    Each iteration makes forward progress or serves exactly one stalled turn, so
    the loop terminates at a task tile, the finish, or ``max_rolls`` (a guard
    against an all-rest board). A no-op in ``manual`` mode (the players roll).
    Returns the LAST roll summary — its ``won`` flag drives the caller's
    end-event check (handle_board_completion → consumer, mercy_sweep → consumer).
    ``redis_conn`` may be None (perform_roll publishes through the realtime
    module's own client), so activation seeding can call this without a handle.
    """
    from db.models import EventBoardPosition

    movement = settings.get("movement") or {}
    if (movement.get("trigger") or "manual") != "auto":
        return None
    last: Optional[dict] = None
    for _ in range(max(1, int(max_rolls))):
        pos = (session.query(EventBoardPosition)
               .filter(EventBoardPosition.team_id == team_id).first())
        if pos is None or pos.event_id != event_id:
            break
        # 'active' (a task was drawn) or 'finished' → the chain is done. Only a
        # taskless awaiting_roll or a blocked stall keeps it going.
        if pos.status not in ("awaiting_roll", "blocked"):
            break
        roll = perform_roll(session, redis_conn, event_id, team_id,
                            settings=settings, rng=rng)
        if roll is None:
            break
        last = roll
        if roll.get("won"):
            break
    return last


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


def _cleared_tiles(session, event_id: int, team_id: int) -> set:
    """Tile indexes this team has completed a task ON — every board instance
    task records the tile it was drawn for (``config.tile_idx``), and a
    completed progress row on one clears that tile. The required-tile rule
    reads this: a checkpoint the team already cleared no longer stops it
    (so a knockback or a chute past it is not a second toll)."""
    from db.models import EventProgress, EventTask

    done = (session.query(EventProgress)
            .filter(EventProgress.event_id == event_id,
                    EventProgress.team_id == team_id,
                    EventProgress.completed.is_(True))
            .all())
    task_ids = [p.task_id for p in done if getattr(p, "task_id", None)]
    if not task_ids:
        return set()
    cleared: set = set()
    for task in session.query(EventTask).filter(EventTask.id.in_(task_ids)).all():
        try:
            cfg = json.loads(task.config) if task.config else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(cfg, dict) or not cfg.get("board_instance"):
            continue
        if cfg.get("tile_idx") is None:
            continue
        try:
            cleared.add(int(cfg["tile_idx"]))
        except (TypeError, ValueError):
            pass
    return cleared


def _required_stop(session, event_id: int, team_id: int, by_idx: dict,
                   start: int, dest: int) -> Optional[int]:
    """The nearest required tile strictly between ``start`` and ``dest`` this
    team has not cleared — where a move that would pass it must stop instead.
    None when the path crosses no live checkpoint. (Landing exactly on a
    required tile needs no interception: it is a normal landing.)"""
    candidates = [i for i in range(start + 1, dest)
                  if by_idx.get(i) is not None and tile_kind(by_idx[i]) == "required"]
    if not candidates:
        return None
    cleared = _cleared_tiles(session, event_id, team_id)
    for i in candidates:
        if i not in cleared:
            return i
    return None


def _resolve_landing(session, event_id: int, team_id: int, pos, by_idx: dict,
                     fin: Optional[int], dest: int, settings: dict, summary: dict,
                     rng: Optional[random.Random] = None) -> dict:
    """Shared landing resolution — every way a piece comes to rest (a roll, a
    teleport, a knockback, a bounce) ends here so tile rules hold for all of
    them: follow a landing-triggered link ONCE (a chute or a ladder; the
    target's own link never chains), then either finish or draw the tile's
    task. A finish tile that carries a task is not a win on arrival — the
    team must complete it (``finish_task``); a plain finish wins outright.

    Mutates ``pos`` and ``summary`` (``to``, ``jump``, ``won``, task keys)."""
    tile = by_idx.get(dest)
    jump = tile_jump(tile)
    if jump is not None and jump[1] == "land" and jump[0] in by_idx:
        target = jump[0]
        summary["jump"] = {"kind": "ladder" if target > dest else "chute",
                           "from": dest, "to": target}
        dest = target
        tile = by_idx.get(dest)
    pos.tile_idx = dest
    pos.blocked_until_turn = None
    summary["to"] = dest
    if fin is not None and dest >= fin and not tile_has_task(tile):
        pos.status = "finished"
        pos.current_task_id = None
        pos.mercy_deadline = None
        summary["won"] = True
        return summary
    instance = assign_tile_task(session, event_id, team_id, tile, pos, settings, rng=rng)
    if instance is not None:
        summary["task_id"] = instance.id
        summary["task_label"] = instance.label
        summary["task_difficulty"] = instance.difficulty
    if fin is not None and dest >= fin:
        summary["finish_task"] = True
    return summary


def _move_piece(session, event_id: int, team_id: int, pos, tiles: list,
                start: int, steps: int, settings: dict,
                rng: Optional[random.Random] = None) -> dict:
    """Shared movement core (rolls AND the advance power-up): required-tile
    checkpoints, the exact-finish rule, tile-effect resolution (roadblocks &
    future traps), then the landing (links, finish, task). Mutates ``pos``;
    caller stamps turn counters/last_roll and flushes."""
    fin = finish_idx(tiles)
    by_idx = {int(t.idx): t for t in tiles}
    steps = max(0, int(steps or 0))
    raw = start + steps
    dest = min(raw, fin)

    summary: dict = {"from": start, "to": dest, "won": False}

    # Required checkpoints (2026-09): a move that would carry the team past a
    # required tile it has not cleared stops ON that tile instead — the
    # nearest one wins, and anything beyond it (a roadblock, the finish) is
    # simply not reached this turn.
    if dest > start:
        stop = _required_stop(session, event_id, team_id, by_idx, start, dest)
        if stop is not None:
            summary["required_stop"] = {"tile_idx": stop, "short_by": dest - stop}
            dest = stop
            summary["to"] = dest

    # Exact finish (settings.win.exact_finish): an overshoot either loses the
    # move ("stay" — nothing about the position changes, not even a live
    # task, so a fizzled teleport can't double as a free skip) or walks the
    # excess back from the finish ("bounce"). "off" keeps the original clamp.
    if "required_stop" not in summary and steps > 0 and raw > fin:
        mode = ((settings.get("win") or {}).get("exact_finish")) or "off"
        if mode == "stay":
            summary["to"] = start
            summary["overshoot"] = {"mode": "stay", "by": raw - fin}
            return summary
        if mode == "bounce":
            dest = max(0, fin - (raw - fin))
            summary["to"] = dest
            summary["overshoot"] = {"mode": "bounce", "by": raw - fin, "to": dest}

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
            if hit["stopped"] and "required_stop" in summary:
                # Cut short before the checkpoint — it is still ahead.
                summary.pop("required_stop", None)

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

    return _resolve_landing(session, event_id, team_id, pos, by_idx, fin, dest,
                            settings, summary, rng=rng)


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
        # The scan above is unlocked and the sweep runs on the consumer's own
        # session while apply lanes move pieces concurrently — so re-read the
        # row locked (as perform_roll does) and re-check the conditions that
        # selected it. Without this, a completion that already advanced the
        # team gets clobbered: current_task_id is nulled and the team is handed
        # a second roll.
        pos = (session.query(EventBoardPosition)
               .filter(EventBoardPosition.team_id == pos.team_id)
               .with_for_update()
               .first())
        if pos is None or pos.status != "active":
            continue
        if pos.mercy_deadline is None or pos.mercy_deadline > now:
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
        won = False
        movement = settings.get("movement") or {}
        if (movement.get("trigger") or "manual") == "auto":
            # A mercy auto-roll can carry a team across the finish. Surface that
            # so the caller ends the event — the old single perform_roll here
            # discarded its summary, leaving a mercy-won game active forever (W2).
            roll = auto_advance(session, redis_conn, ev.id, pos.team_id, settings)
            won = bool(roll and roll.get("won"))
        swept.append({"event_id": ev.id, "team_id": pos.team_id, "won": won})
    return swept
