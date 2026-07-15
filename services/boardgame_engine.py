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
    },
    "coins": {
        "enabled": True,
        "per_difficulty": {"air": 10, "water": 20, "earth": 30, "fire": 50},
        "default": 10,           # tasks with no difficulty
        "starting": 0,
    },
    "shop": {"enabled": True, "rotation": "static", "rotation_turns": 0},
    "items": {"enabled_item_ids": None, "disabled_effects": []},  # None = all
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
    """Adjust a team's wallet (running balance + audit row). Caller holds the
    team row (locked by its own query) and commits."""
    from db.models import EventCoinLedger

    balance = int(team.coins or 0) + int(delta)
    team.coins = balance
    session.add(EventCoinLedger(
        event_id=event_id, team_id=team.id, delta=int(delta), reason=reason,
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
    task. Caller guarantees the team is ``awaiting_roll`` (the web route
    validates + 409s; the auto path just set it). Returns the roll summary
    {dice, from, to, won, task_id?, task_label?} or None when the position
    is missing/ineligible. Caller commits."""
    from db.models import EventBoardPosition

    pos = (session.query(EventBoardPosition)
           .filter(EventBoardPosition.team_id == team_id).first())
    if pos is None or pos.event_id != event_id:
        return None
    if pos.status not in ("awaiting_roll",):
        return None

    settings = settings or load_board_settings(session, event_id)
    tiles = load_tiles(session, event_id)
    if not tiles:
        return None
    fin = finish_idx(tiles)
    by_idx = {int(t.idx): t for t in tiles}

    faces = roll_dice(settings, rng)
    start = int(pos.tile_idx or 0)
    # P3 hook: pass-through roadblock effects stop the piece short here.
    dest = min(start + sum(faces), fin)

    pos.tile_idx = dest
    pos.turns_completed = int(pos.turns_completed or 0) + 1
    pos.last_roll = json.dumps({
        "dice": faces, "from": start, "to": dest,
        "at": int(datetime.now().timestamp()),
    })

    summary: dict = {
        "dice": faces, "from": start, "to": dest,
        "turn": pos.turns_completed, "won": False,
    }

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
    session.flush()

    # Live board frame for the web/Activity views (SSE scope event:{id}).
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_roll", "event_id": event_id, "team_id": team_id,
            "dice": faces, "from": start, "to": dest,
            "won": summary["won"], "task_label": summary.get("task_label"),
        })
    except Exception:
        pass

    # The matcher caches (event, team) -> current task; nudge every consumer
    # to reload so the new instance starts matching immediately.
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(redis_conn)
    except Exception:
        pass
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
