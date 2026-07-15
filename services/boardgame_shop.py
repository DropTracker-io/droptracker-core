"""Board-game shop (web45a): catalog resolution, atomic buys, item use.

The coin-spend half of the dice-board mode, shaped after the premium-points
purchase flow (services/points.py::activate_feature_for_group): one
transaction locks the team row, checks the balance and the item-type
cooldown, debits the wallet (ledger row), and flips the inventory row.

Availability layers, outermost first:
  1. settings.shop.enabled          — the whole economy kill switch
  2. catalog ``active``             — superadmin-curated
  3. event rotation rows            — per-event stocking/pricing/stock
     (no rows at all = sell the whole active catalog at list price)
  4. settings.items.enabled_item_ids / disabled_effects — per-event kill
     switches a leader can flip mid-event

Type cooldowns compare against ``EventBoardPosition.turns_completed``:
an item of type T is usable iff  turns_completed - last_used_turn(T)
>= type_cooldown_turns.  Cooldown state lives in web_event_team_cooldowns.

Live handlers — self-targeted (P2): skip_task / reroll_task / boost_coins;
interference (P3): advance (teleport forward, roadblock-aware, no turn
consumed), roadblock (tile trap: the next OTHER team to pass stops on it),
freeze_opponent (target's next N rolls move 0 tiles; blocked by an armed
shield), shield (absorbs the next offensive effect).

Caller owns the transaction (routes commit); helpers only flush.
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from typing import Optional

from services.boardgame_engine import (
    assign_tile_task,
    award_coins,
    load_board_settings,
    load_tiles,
    perform_roll,
)


class ShopError(Exception):
    """User-facing shop failure: (status, title, detail)."""

    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


_LIVE_EFFECTS = (
    "skip_task", "reroll_task", "boost_coins",          # P2 self-targeted
    "advance", "roadblock", "freeze_opponent", "shield",  # P3 interference
)


def _cfg(raw) -> dict:
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _item_kill_switches(settings: dict) -> tuple[Optional[set], set]:
    items = settings.get("items") or {}
    enabled_ids = items.get("enabled_item_ids")
    enabled = (
        {int(i) for i in enabled_ids} if isinstance(enabled_ids, list) else None
    )
    disabled_effects = {
        str(e) for e in (items.get("disabled_effects") or []) if e
    }
    return enabled, disabled_effects


def available_items(session, event_id: int, settings: Optional[dict] = None) -> list[dict]:
    """The event's purchasable catalog, availability resolved per layer.
    Rows include live stock and the effective price."""
    from db.models import BoardgameShopItem, EventShopRotation

    settings = settings or load_board_settings(session, event_id)
    if not (settings.get("shop") or {}).get("enabled", True):
        return []
    enabled_ids, disabled_effects = _item_kill_switches(settings)

    rotation = {
        r.shop_item_id: r
        for r in session.query(EventShopRotation)
        .filter(EventShopRotation.event_id == event_id).all()
    }
    rows = (
        session.query(BoardgameShopItem)
        .filter(BoardgameShopItem.active.is_(True))
        .order_by(BoardgameShopItem.sort, BoardgameShopItem.id)
        .all()
    )
    out = []
    for item in rows:
        if rotation and item.id not in rotation:
            continue  # the event stocked an explicit subset
        if enabled_ids is not None and item.id not in enabled_ids:
            continue
        if item.effect in disabled_effects:
            continue
        rot = rotation.get(item.id)
        if rot is not None and rot.stock is not None and rot.stock <= 0:
            continue
        out.append({
            "id": item.id,
            "key": item.key,
            "name": item.name,
            "description": item.description or None,
            "icon_item_id": item.icon_item_id,
            "item_type": item.item_type,
            "effect": item.effect,
            "cost_coins": int(
                rot.price_override if rot is not None and rot.price_override is not None
                else item.cost_coins
            ),
            "type_cooldown_turns": int(item.type_cooldown_turns or 0),
            "stock": rot.stock if rot is not None else None,
            # P3 effects surface greyed-out once their rows activate.
            "usable_now": item.effect in _LIVE_EFFECTS,
        })
    return out


def team_shop_state(session, event_id: int, team_id: int) -> dict:
    """Wallet + inventory + per-type cooldown readiness for one team."""
    from db.models import (
        BoardgameShopItem,
        EventBoardPosition,
        EventTeam,
        EventTeamCooldown,
        EventTeamInventory,
    )

    team = session.query(EventTeam).filter(EventTeam.id == team_id).first()
    pos = (session.query(EventBoardPosition)
           .filter(EventBoardPosition.team_id == team_id).first())
    turns = int(pos.turns_completed or 0) if pos else 0
    cooldowns = {
        c.item_type: int(c.last_used_turn or 0)
        for c in session.query(EventTeamCooldown)
        .filter(EventTeamCooldown.team_id == team_id).all()
    }
    inv_rows = (
        session.query(EventTeamInventory, BoardgameShopItem)
        .join(BoardgameShopItem,
              BoardgameShopItem.id == EventTeamInventory.shop_item_id)
        .filter(EventTeamInventory.team_id == team_id,
                EventTeamInventory.event_id == event_id)
        .order_by(EventTeamInventory.created_at)
        .all()
    )
    inventory = []
    for inv, item in inv_rows:
        cd = int(item.type_cooldown_turns or 0)
        last = cooldowns.get(item.item_type)
        ready_turn = (last + cd) if last is not None else None
        inventory.append({
            "inventory_id": inv.id,
            "shop_item_id": item.id,
            "key": item.key,
            "name": item.name,
            "icon_item_id": item.icon_item_id,
            "item_type": item.item_type,
            "effect": item.effect,
            "status": inv.status,
            "acquired_turn": int(inv.acquired_turn or 0),
            "used_turn": inv.used_turn,
            # Ready when no prior use of this type, or the cooldown elapsed.
            "cooldown_ready": inv.status == "owned" and (
                last is None or turns - last >= cd
            ),
            "cooldown_ready_turn": ready_turn,
            "usable_now": item.effect in _LIVE_EFFECTS,
        })
    return {
        "team_id": team_id,
        "coins": int(getattr(team, "coins", 0) or 0) if team else 0,
        "turns_completed": turns,
        "inventory": inventory,
    }


def buy_item(session, event_id: int, team_id: int, shop_item_id: int,
             user_id: Optional[int]) -> dict:
    """Atomic purchase: lock team → availability + balance → debit + ledger →
    inventory row (+ stock decrement). Raises ShopError on any rule breach."""
    from db.models import (
        BoardgameShopItem,
        EventBoardPosition,
        EventShopRotation,
        EventTeam,
        EventTeamInventory,
    )

    settings = load_board_settings(session, event_id)
    catalog = {i["id"]: i for i in available_items(session, event_id, settings)}
    offer = catalog.get(int(shop_item_id))
    if offer is None:
        raise ShopError(404, "Not for sale",
                        "That item is not available in this event's shop.")

    team = (session.query(EventTeam)
            .filter(EventTeam.id == team_id)
            .with_for_update()
            .first())
    if team is None or team.event_id != event_id:
        raise ShopError(404, "Team not found", "No such team in this event.")

    price = int(offer["cost_coins"])
    balance = int(team.coins or 0)
    if balance < price:
        raise ShopError(409, "Not enough coins",
                        f"That costs {price} coins; the team has {balance}.")

    # Stock (rotation-managed events only), decremented under the same lock.
    rot = (session.query(EventShopRotation)
           .filter(EventShopRotation.event_id == event_id,
                   EventShopRotation.shop_item_id == shop_item_id)
           .with_for_update()
           .first())
    if rot is not None and rot.stock is not None:
        if rot.stock <= 0:
            raise ShopError(409, "Sold out", "That item is out of stock.")
        rot.stock -= 1

    pos = (session.query(EventBoardPosition)
           .filter(EventBoardPosition.team_id == team_id).first())
    inv = EventTeamInventory(
        event_id=event_id, team_id=team_id, shop_item_id=shop_item_id,
        price_paid=price, acquired_turn=int(pos.turns_completed or 0) if pos else 0,
        status="owned",
    )
    session.add(inv)
    session.flush()
    new_balance = award_coins(
        session, event_id, team, -price, "purchase",
        ref_type="purchase", ref_id=inv.id, acted_by_user_id=user_id,
        note=offer["key"],
    )
    return {"inventory_id": inv.id, "coins": new_balance, "price_paid": price}


def use_item(session, redis_conn, event_id: int, team_id: int,
             inventory_id: int, user_id: Optional[int],
             target: Optional[dict] = None,
             rng: Optional[random.Random] = None) -> dict:
    """Consume an owned item: ownership + kill switches + TYPE cooldown, then
    the effect handler. Marks the row used and stamps the cooldown."""
    from db.models import (
        BoardgameShopItem,
        EventBoardPosition,
        EventTeamCooldown,
        EventTeamInventory,
    )

    inv = (session.query(EventTeamInventory)
           .filter(EventTeamInventory.id == inventory_id)
           .with_for_update()
           .first())
    if inv is None or inv.event_id != event_id or inv.team_id != team_id:
        raise ShopError(404, "Item not found", "That item is not in the team's bag.")
    if inv.status != "owned":
        raise ShopError(409, "Already used", "That item has already been consumed.")

    item = (session.query(BoardgameShopItem)
            .filter(BoardgameShopItem.id == inv.shop_item_id).first())
    if item is None:
        raise ShopError(404, "Item not found", "Unknown shop item.")

    settings = load_board_settings(session, event_id)
    if not (settings.get("shop") or {}).get("enabled", True):
        raise ShopError(409, "Shop disabled", "This event's shop is switched off.")
    enabled_ids, disabled_effects = _item_kill_switches(settings)
    if (enabled_ids is not None and item.id not in enabled_ids) or \
            item.effect in disabled_effects:
        raise ShopError(409, "Item disabled",
                        "The event's settings have disabled this item.")
    if item.effect not in _LIVE_EFFECTS:
        raise ShopError(409, "Not yet usable",
                        "This power-up's effect arrives in a later update.")

    pos = (session.query(EventBoardPosition)
           .filter(EventBoardPosition.team_id == team_id)
           .with_for_update()
           .first())
    if pos is None:
        raise ShopError(409, "No board state", "The team has no board position.")
    turns = int(pos.turns_completed or 0)

    cd_rows = {
        c.item_type: c
        for c in session.query(EventTeamCooldown)
        .filter(EventTeamCooldown.team_id == team_id).all()
    }
    cd = cd_rows.get(item.item_type)
    cooldown = int(item.type_cooldown_turns or 0)
    if cd is not None and turns - int(cd.last_used_turn or 0) < cooldown:
        ready = int(cd.last_used_turn or 0) + cooldown
        raise ShopError(
            409, "On cooldown",
            f"A {item.item_type} item was used on turn {cd.last_used_turn}; "
            f"the next is usable from turn {ready} (you are on {turns}).",
        )

    handler = {
        "skip_task": _use_skip_task,
        "reroll_task": _use_reroll_task,
        "boost_coins": _use_boost_coins,
        "advance": _use_advance,
        "roadblock": _use_roadblock,
        "freeze_opponent": _use_freeze_opponent,
        "shield": _use_shield,
    }[item.effect]
    result = handler(session, redis_conn, event_id, team_id, pos, item,
                     settings, rng=rng, target=target)

    inv.status = "used"
    inv.used_turn = turns
    inv.used_at = datetime.now()
    inv.used_by_user_id = user_id
    inv.used_on = json.dumps(target) if target else None
    if cd is None:
        session.add(EventTeamCooldown(
            event_id=event_id, team_id=team_id,
            item_type=item.item_type, last_used_turn=turns,
        ))
    else:
        cd.last_used_turn = turns
    session.flush()

    # Live frame so open board views refresh.
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_item_used", "event_id": event_id,
            "team_id": team_id, "item": item.key, "effect": item.effect,
        })
    except Exception:
        pass
    return {"effect": item.effect, **(result or {})}


# --------------------------------------------------------------------------- #
# P2 effect handlers (self-targeted)
# --------------------------------------------------------------------------- #
def _use_skip_task(session, redis_conn, event_id, team_id, pos, item,
                   settings, rng=None, target=None) -> dict:
    """Complete the current task without submissions (no coin reward) —
    the paid version of the mercy rule."""
    from db.models import EventProgress

    if pos.status != "active" or not pos.current_task_id:
        raise ShopError(409, "Nothing to skip", "The team has no live task.")
    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == pos.current_task_id,
                        EventProgress.team_id == team_id)
                .first())
    if progress is None:
        progress = EventProgress(
            event_id=event_id, task_id=pos.current_task_id,
            team_id=team_id, progress=0)
        session.add(progress)
    progress.completed = True
    progress.completed_at = datetime.now()
    pos.status = "awaiting_roll"
    pos.current_task_id = None
    pos.mercy_deadline = None
    session.flush()
    out: dict = {"skipped": True}
    if ((settings.get("movement") or {}).get("trigger") or "manual") == "auto":
        roll = perform_roll(session, redis_conn, event_id, team_id,
                            settings=settings, rng=rng)
        if roll:
            out["roll"] = roll
    return out


def _use_reroll_task(session, redis_conn, event_id, team_id, pos, item,
                     settings, rng=None, target=None) -> dict:
    """Redraw the current tile's task (unobtainable-RNG escape hatch). The
    old instance is dropped (GC'd when progress-free); the new draw avoids
    repeating the same source task when the pool allows."""
    from db.models import EventBoardTile, EventCompletion, EventProgress, EventTask

    if pos.status != "active" or not pos.current_task_id:
        raise ShopError(409, "Nothing to reroll", "The team has no live task.")
    tile = (session.query(EventBoardTile)
            .filter(EventBoardTile.event_id == event_id,
                    EventBoardTile.idx == int(pos.tile_idx or 0))
            .first())
    if tile is None or (tile.task_id is None and not tile.difficulty):
        raise ShopError(409, "Cannot reroll",
                        "This tile has no task pool to redraw from.")
    if tile.task_id is not None:
        raise ShopError(409, "Cannot reroll",
                        "This tile pins one specific task — rerolls don't apply.")

    old_task_id = pos.current_task_id
    old_task = session.query(EventTask).filter(EventTask.id == old_task_id).first()
    old_source = None
    if old_task is not None:
        try:
            old_source = json.loads(old_task.config or "{}").get("source_task_id")
        except (TypeError, ValueError):
            old_source = None

    # Draw the replacement, biased away from the current source task.
    from services.boardgame_engine import _task_pool

    pool = _task_pool(session, event_id, tile.difficulty)
    if old_source is not None and len(pool) > 1:
        pool = [t for t in pool if t.id != old_source]
    if not pool:
        raise ShopError(409, "Cannot reroll", "No other tasks in this tile's pool.")
    choice = (rng or random).choice(pool)

    from services.boardgame_engine import _materialize_instance, _mercy_deadline

    instance = _materialize_instance(session, event_id, team_id, choice,
                                     int(pos.turns_completed or 0))
    pos.current_task_id = instance.id
    pos.task_assigned_at = datetime.now()
    pos.mercy_deadline = _mercy_deadline(settings, pos.mercy_count)
    session.flush()

    # GC the abandoned instance if the engine never credited it.
    used = (session.query(EventCompletion.id)
            .filter(EventCompletion.task_id == old_task_id).first())
    if not used:
        (session.query(EventProgress)
         .filter(EventProgress.task_id == old_task_id)
         .delete(synchronize_session=False))
        (session.query(EventTask)
         .filter(EventTask.id == old_task_id, EventTask.event_id == event_id)
         .delete(synchronize_session=False))
    session.flush()

    # New instance = new matcher target.
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(redis_conn)
    except Exception:
        pass
    return {"task_id": instance.id, "task_label": instance.label,
            "task_difficulty": instance.difficulty}


def _use_boost_coins(session, redis_conn, event_id, team_id, pos, item,
                     settings, rng=None, target=None) -> dict:
    """Arm a coin multiplier for the team's next completed task."""
    from db.models import EventBoardEffect

    existing = (session.query(EventBoardEffect)
                .filter(EventBoardEffect.event_id == event_id,
                        EventBoardEffect.target_team_id == team_id,
                        EventBoardEffect.effect_type == "boost_coins",
                        EventBoardEffect.status == "active")
                .first())
    if existing is not None:
        raise ShopError(409, "Already boosted",
                        "A coin boost is already armed for this team.")
    multiplier = 2
    try:
        multiplier = max(2, int(_cfg(item.effect_config).get("multiplier", 2)))
    except (TypeError, ValueError):
        pass
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id, target_team_id=team_id,
        effect_type="boost_coins",
        effect_config=json.dumps({"multiplier": multiplier}),
        status="active",
    ))
    session.flush()
    return {"boost_multiplier": multiplier}


# --------------------------------------------------------------------------- #
# P3 effect handlers (movement + interference)
# --------------------------------------------------------------------------- #
def _use_advance(session, redis_conn, event_id, team_id, pos, item,
                 settings, rng=None, target=None) -> dict:
    """Teleport forward without completing a task: rolls the item's own die
    (effect_config.dice_sides, default 6) and moves — roadblock-aware, does
    NOT consume a turn, and replaces any live task with the landed tile's."""
    from services.boardgame_engine import _move_piece, load_tiles

    if pos.status not in ("active", "awaiting_roll"):
        raise ShopError(409, "Cannot teleport",
                        f"The team is '{pos.status}' and cannot move.")
    tiles = load_tiles(session, event_id)
    if not tiles:
        raise ShopError(409, "No board", "The event has no tiles.")
    sides = 6
    try:
        sides = max(1, min(20, int(_cfg(item.effect_config).get("dice_sides", 6))))
    except (TypeError, ValueError):
        pass
    steps = (rng or random).randint(1, sides)
    start = int(pos.tile_idx or 0)
    summary = _move_piece(session, event_id, team_id, pos, tiles, start, steps,
                          settings, rng=rng)
    session.flush()
    # New instance task (or a finish) = matcher change.
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(redis_conn)
    except Exception:
        pass
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_roll", "event_id": event_id, "team_id": team_id,
            "dice": [steps], "from": start, "to": summary["to"],
            "won": summary["won"], "task_label": summary.get("task_label"),
            "teleport": True,
        })
    except Exception:
        pass
    return {"teleport": True, **summary}


def _use_roadblock(session, redis_conn, event_id, team_id, pos, item,
                   settings, rng=None, target=None) -> dict:
    """Place a trap on a tile: the next OTHER team whose movement crosses it
    stops there (the block is consumed when it procs)."""
    from db.models import EventBoardEffect
    from services.boardgame_engine import finish_idx, load_tiles

    tile_idx = (target or {}).get("target_tile_idx")
    if tile_idx is None:
        raise ShopError(422, "Pick a tile",
                        "Pass target_tile_idx — the tile to block.")
    tiles = load_tiles(session, event_id)
    fin = finish_idx(tiles) or 0
    valid = {int(t.idx) for t in tiles}
    if int(tile_idx) not in valid or not (0 < int(tile_idx) < fin):
        raise ShopError(422, "Bad tile",
                        "Roadblocks go on a mid-track tile (not start/finish).")
    existing = (session.query(EventBoardEffect)
                .filter(EventBoardEffect.event_id == event_id,
                        EventBoardEffect.effect_type == "roadblock",
                        EventBoardEffect.status == "active",
                        EventBoardEffect.target_tile_idx == int(tile_idx))
                .first())
    if existing is not None:
        raise ShopError(409, "Tile occupied", "That tile is already blocked.")
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id,
        target_tile_idx=int(tile_idx), effect_type="roadblock",
        effect_config=item.effect_config, status="active",
    ))
    session.flush()
    return {"roadblock_tile_idx": int(tile_idx)}


def _use_freeze_opponent(session, redis_conn, event_id, team_id, pos, item,
                         settings, rng=None, target=None) -> dict:
    """Freeze another team: their next N rolls move 0 tiles. An armed shield
    on the target absorbs the freeze instead."""
    from db.models import EventBoardEffect, EventBoardPosition

    target_team = (target or {}).get("target_team_id")
    if target_team is None:
        raise ShopError(422, "Pick a target", "Pass target_team_id.")
    target_team = int(target_team)
    if target_team == team_id:
        raise ShopError(422, "Bad target", "You cannot freeze your own team.")
    tpos = (session.query(EventBoardPosition)
            .filter(EventBoardPosition.team_id == target_team).first())
    if tpos is None or tpos.event_id != event_id:
        raise ShopError(404, "Bad target", "That team is not on this board.")
    if tpos.status == "finished":
        raise ShopError(409, "Bad target", "That team has already finished.")

    # Shield check: an armed shield eats the freeze.
    shield = (session.query(EventBoardEffect)
              .filter(EventBoardEffect.event_id == event_id,
                      EventBoardEffect.target_team_id == target_team,
                      EventBoardEffect.effect_type == "shield",
                      EventBoardEffect.status == "active")
              .first())
    if shield is not None:
        shield.status = "consumed"
        session.flush()
        return {"blocked_by_shield": True, "target_team_id": target_team}

    existing = (session.query(EventBoardEffect)
                .filter(EventBoardEffect.event_id == event_id,
                        EventBoardEffect.target_team_id == target_team,
                        EventBoardEffect.effect_type == "freeze_opponent",
                        EventBoardEffect.status == "active")
                .first())
    if existing is not None:
        raise ShopError(409, "Already frozen", "That team is already frozen.")
    turns = 2
    try:
        turns = max(1, min(5, int(_cfg(item.effect_config).get("turns", 2))))
    except (TypeError, ValueError):
        pass
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id, target_team_id=target_team,
        effect_type="freeze_opponent",
        effect_config=json.dumps({"turns": turns, "remaining": turns}),
        status="active",
    ))
    session.flush()
    return {"target_team_id": target_team, "frozen_rolls": turns}


def _use_shield(session, redis_conn, event_id, team_id, pos, item,
                settings, rng=None, target=None) -> dict:
    """Arm a one-shot shield: the next offensive effect against this team is
    absorbed. One armed shield at a time."""
    from db.models import EventBoardEffect

    existing = (session.query(EventBoardEffect)
                .filter(EventBoardEffect.event_id == event_id,
                        EventBoardEffect.target_team_id == team_id,
                        EventBoardEffect.effect_type == "shield",
                        EventBoardEffect.status == "active")
                .first())
    if existing is not None:
        raise ShopError(409, "Already shielded", "A shield is already armed.")
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id, target_team_id=team_id,
        effect_type="shield", effect_config=None, status="active",
    ))
    session.flush()
    return {"shielded": True}


def consume_coin_boost(session, event_id: int, team_id: int) -> int:
    """The multiplier for this completion (consuming any armed boost).
    Called from boardgame_engine.handle_board_completion; returns 1 when no
    boost is active."""
    from db.models import EventBoardEffect

    effect = (session.query(EventBoardEffect)
              .filter(EventBoardEffect.event_id == event_id,
                      EventBoardEffect.target_team_id == team_id,
                      EventBoardEffect.effect_type == "boost_coins",
                      EventBoardEffect.status == "active")
              .first())
    if effect is None:
        return 1
    effect.status = "consumed"
    session.flush()
    try:
        return max(1, int(_cfg(effect.effect_config).get("multiplier", 2)))
    except (TypeError, ValueError):
        return 2
