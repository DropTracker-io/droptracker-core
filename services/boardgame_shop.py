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
consumed), roadblock (tile trap: ANY team whose movement crosses it — the
placer included — stops on it; break_on/stall_turns behavior comes from the
effect registry, see services/boardgame_effects.py), freeze_opponent
(target's next N rolls move 0 tiles; blocked by an armed shield), shield
(absorbs the next offensive effect).

Effect metadata/behavior lives in services.boardgame_effects.EFFECT_REGISTRY;
handlers here follow the ``_use_<effect>`` naming convention the dispatch
relies on.

Caller owns the transaction (routes commit); helpers only flush.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import Optional

from services.boardgame_effects import EFFECT_REGISTRY, live_effects
from services.boardgame_engine import (
    assign_tile_task,
    award_coins,
    load_board_settings,
    load_tiles,
    perform_roll,
)

# Difficulty tiers, easiest → hardest (services/boardgame_engine +
# db.models.EVENT_TASK_DIFFICULTIES). reroll_task's difficulty_shift and
# choose_task's distinct-tier draw walk this ordering.
_DIFFICULTY_ORDER = ("air", "water", "earth", "fire")

# Effect types that count as a NEGATIVE effect on the TARGET team — cleared by
# the cleanse power-up. (coin_toll is armed on the MOVER, never targets a team,
# so it never matches here; listed for intent/forward-compat.)
_NEGATIVE_EFFECTS = ("freeze_opponent", "coin_toll")


class ShopError(Exception):
    """User-facing shop failure: (status, title, detail)."""

    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


# Effects with a live handler — sourced from the behavior registry so the
# catalog flags, inventory flags, and the dispatch can never drift apart.
_LIVE_EFFECTS = live_effects()


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


# --------------------------------------------------------------------------- #
# Shared defense resolution (web50a) — every offensive handler funnels through
# this before applying, so shield + ward (and any future defense) apply
# uniformly. Returns a summary dict when a defense absorbs the incoming effect
# (already consumed), else None.
# --------------------------------------------------------------------------- #
def _absorb_defense(session, event_id: int, target_team_id: int,
                    incoming_effect_key: str) -> Optional[dict]:
    """Consume the first armed defense on ``target_team_id`` that stops
    ``incoming_effect_key`` and report it; None when nothing absorbs it.

    - ``shield``: a one-shot that absorbs ANY offensive effect (web45a).
    - ``ward``:   absorbs only the effect keys its config ``blocks`` lists;
      the token ``"offensive"`` matches every offensive effect.

    Shield is checked first (broadest catch-all, preserving the pre-web50a
    freeze behavior); wards second."""
    from db.models import EventBoardEffect

    shield = (session.query(EventBoardEffect)
              .filter(EventBoardEffect.event_id == event_id,
                      EventBoardEffect.target_team_id == target_team_id,
                      EventBoardEffect.effect_type == "shield",
                      EventBoardEffect.status == "active")
              .first())
    if shield is not None:
        shield.status = "consumed"
        session.flush()
        return {"absorbed": True, "absorbed_by": "shield",
                "blocked_by_shield": True, "target_team_id": target_team_id}

    wards = (session.query(EventBoardEffect)
             .filter(EventBoardEffect.event_id == event_id,
                     EventBoardEffect.target_team_id == target_team_id,
                     EventBoardEffect.effect_type == "ward",
                     EventBoardEffect.status == "active")
             .all())
    for ward in wards:
        blocks = _cfg(ward.effect_config).get("blocks")
        if not isinstance(blocks, list):
            blocks = []
        if "offensive" in blocks or incoming_effect_key in blocks:
            ward.status = "consumed"
            session.flush()
            return {"absorbed": True, "absorbed_by": "ward",
                    "ward_id": ward.id, "target_team_id": target_team_id}
    return None


def _shifted_difficulty(difficulty: Optional[str], shift: int) -> Optional[str]:
    """A difficulty shifted ``shift`` tiers along air<water<earth<fire and
    clamped to the ends. Unknown/None difficulty rides through unchanged."""
    if difficulty not in _DIFFICULTY_ORDER or not shift:
        return difficulty
    idx = _DIFFICULTY_ORDER.index(difficulty)
    idx = max(0, min(len(_DIFFICULTY_ORDER) - 1, idx + int(shift)))
    return _DIFFICULTY_ORDER[idx]


# --------------------------------------------------------------------------- #
# Per-event shop refresh (web50a): stock restock cadence.
# --------------------------------------------------------------------------- #
def shop_refresh_due(mode: str, interval: int, refreshed_at, refreshed_turn,
                     now: datetime, current_turn: int) -> bool:
    """Pure due-check: has enough elapsed since the last refresh? A NULL marker
    means the clock hasn't started (baseline set separately) → not due."""
    try:
        interval = int(interval or 0)
    except (TypeError, ValueError):
        interval = 0
    if interval <= 0:
        return False
    if mode == "hours":
        if refreshed_at is None:
            return False
        return (now - refreshed_at) >= timedelta(hours=interval)
    if mode == "turns":
        if refreshed_turn is None:
            return False
        return (int(current_turn) - int(refreshed_turn)) >= interval
    return False


def _max_turn(session, event_id: int) -> int:
    from db.models import EventBoardPosition

    rows = (session.query(EventBoardPosition)
            .filter(EventBoardPosition.event_id == event_id).all())
    return max((int(p.turns_completed or 0) for p in rows), default=0)


def maybe_refresh_shop(session, event_id: int,
                       settings: Optional[dict] = None) -> bool:
    """Restock every rotation row's ``stock`` to its ``stock_per_refresh`` when
    a refresh is due (per ``settings.shop.refresh_mode``/``refresh_interval``).
    Idempotent + lazy: safe to call from every shop read/buy. Commits its own
    restock (the read paths that call it — GET shop — do not otherwise commit),
    so the fresh stock persists. No-op (no commit) when not due. Returns True
    iff it restocked."""
    from db.models import EventBoardConfig, EventShopRotation

    settings = settings or load_board_settings(session, event_id)
    shop = settings.get("shop") or {}
    mode = shop.get("refresh_mode") or "none"
    try:
        interval = int(shop.get("refresh_interval") or 0)
    except (TypeError, ValueError):
        interval = 0
    if mode not in ("turns", "hours") or interval <= 0:
        return False

    config = (session.query(EventBoardConfig)
              .filter(EventBoardConfig.event_id == event_id).first())
    if config is None:
        return False
    now = datetime.now()
    current_turn = _max_turn(session, event_id) if mode == "turns" else 0

    # Baseline: the first observation after a refresh cadence is enabled just
    # starts the clock (no restock) so the interval counts forward from now.
    if (mode == "hours" and config.shop_refreshed_at is None) or \
            (mode == "turns" and config.shop_refreshed_turn is None):
        locked = (session.query(EventBoardConfig)
                  .filter(EventBoardConfig.event_id == event_id)
                  .with_for_update().first())
        if locked is not None and (
                (mode == "hours" and locked.shop_refreshed_at is None) or
                (mode == "turns" and locked.shop_refreshed_turn is None)):
            locked.shop_refreshed_at = now
            locked.shop_refreshed_turn = current_turn
            session.commit()
        return False

    if not shop_refresh_due(mode, interval, config.shop_refreshed_at,
                            config.shop_refreshed_turn, now, current_turn):
        return False

    # Due: re-check under a row lock (another reader may have beaten us), then
    # restock every capped rotation row.
    locked = (session.query(EventBoardConfig)
              .filter(EventBoardConfig.event_id == event_id)
              .with_for_update().first())
    if locked is None:
        return False
    current_turn = _max_turn(session, event_id) if mode == "turns" else 0
    if not shop_refresh_due(mode, interval, locked.shop_refreshed_at,
                            locked.shop_refreshed_turn, now, current_turn):
        return False
    rows = (session.query(EventShopRotation)
            .filter(EventShopRotation.event_id == event_id)
            .with_for_update().all())
    for r in rows:
        if r.stock_per_refresh is not None:
            r.stock = r.stock_per_refresh
    locked.shop_refreshed_at = now
    locked.shop_refreshed_turn = current_turn
    session.commit()
    return True


def available_items(session, event_id: int, settings: Optional[dict] = None,
                    team_id: Optional[int] = None) -> list[dict]:
    """The event's purchasable catalog, availability resolved per layer.

    web50a semantics: a ``EventShopRotation`` row is an OVERRIDE, not an
    allow-list — an item with NO row is sold at catalog defaults (enabled,
    list price, unlimited stock, uncapped). A row may disable the item,
    re-price it, cap its stock, and cap per-team purchases. The
    ``settings.items`` kill switches (enabled_item_ids / disabled_effects)
    still apply on top. When ``team_id`` is given, each row also carries
    ``bought_by_team`` so the UI can grey out cap-reached items."""
    from db.models import BoardgameShopItem, EventShopRotation, EventTeamInventory

    settings = settings or load_board_settings(session, event_id)
    if not (settings.get("shop") or {}).get("enabled", True):
        return []
    maybe_refresh_shop(session, event_id, settings)
    enabled_ids, disabled_effects = _item_kill_switches(settings)

    rotation = {
        r.shop_item_id: r
        for r in session.query(EventShopRotation)
        .filter(EventShopRotation.event_id == event_id).all()
    }
    bought_counts: dict = {}
    if team_id is not None:
        for (sid,) in (session.query(EventTeamInventory.shop_item_id)
                       .filter(EventTeamInventory.event_id == event_id,
                               EventTeamInventory.team_id == team_id).all()):
            bought_counts[sid] = bought_counts.get(sid, 0) + 1

    rows = (
        session.query(BoardgameShopItem)
        .filter(BoardgameShopItem.active.is_(True))
        .order_by(BoardgameShopItem.sort, BoardgameShopItem.id)
        .all()
    )
    out = []
    for item in rows:
        if enabled_ids is not None and item.id not in enabled_ids:
            continue
        if item.effect in disabled_effects:
            continue
        rot = rotation.get(item.id)
        if rot is not None and rot.enabled is False:
            continue  # per-event override disabled this item
        stock = rot.stock if rot is not None else None
        if stock is not None and stock <= 0:
            continue  # sold out (capped stock exhausted)
        per_team_cap = rot.per_team_cap if rot is not None else None
        bought_by_team = (bought_counts.get(item.id, 0)
                          if team_id is not None else None)
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
            "stock": stock,
            "stock_per_refresh": rot.stock_per_refresh if rot is not None else None,
            "per_team_cap": per_team_cap,
            "bought_by_team": bought_by_team,
            # A cap-reached item stays listed but flips sold-out for this team.
            "cap_reached": (per_team_cap is not None and bought_by_team is not None
                            and bought_by_team >= per_team_cap),
            # Effects with no live handler surface greyed-out.
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

    # Override row (stock + per-team cap), locked under the same team lock.
    rot = (session.query(EventShopRotation)
           .filter(EventShopRotation.event_id == event_id,
                   EventShopRotation.shop_item_id == shop_item_id)
           .with_for_update()
           .first())
    if rot is not None and rot.stock is not None:
        if rot.stock <= 0:
            raise ShopError(409, "Sold out", "That item is out of stock.")
        rot.stock -= 1
    # Per-team cap (web50a): the max lifetime purchases of this item per team.
    if rot is not None and rot.per_team_cap is not None:
        bought = (session.query(EventTeamInventory)
                  .filter(EventTeamInventory.event_id == event_id,
                          EventTeamInventory.team_id == team_id,
                          EventTeamInventory.shop_item_id == shop_item_id)
                  .count())
        if bought >= int(rot.per_team_cap):
            raise ShopError(
                409, "Purchase limit reached",
                f"Your team already bought the max of this item "
                f"({int(rot.per_team_cap)}).")

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
    # Registry-driven dispatch: an implemented effect has a _use_<key>
    # handler in this module (uniform signature).
    spec = EFFECT_REGISTRY.get(item.effect)
    handler = (globals().get(f"_use_{item.effect}")
               if spec is not None and spec.implemented else None)
    if handler is None:
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


def _discard_task_instance(session, event_id, task_id) -> None:
    """GC an abandoned per-landing task instance (progress + row) unless the
    engine already credited a completion against it. Mirrors reroll_task's GC."""
    if not task_id:
        return
    from db.models import EventCompletion, EventProgress, EventTask

    used = (session.query(EventCompletion.id)
            .filter(EventCompletion.task_id == task_id).first())
    if used:
        return
    (session.query(EventProgress)
     .filter(EventProgress.task_id == task_id)
     .delete(synchronize_session=False))
    (session.query(EventTask)
     .filter(EventTask.id == task_id, EventTask.event_id == event_id)
     .delete(synchronize_session=False))


def _reroll_current_task(session, redis_conn, event_id, team_id, pos, settings,
                         rng=None, difficulty_shift=0):
    """Redraw the position's current tile task and return the new instance.

    Shared by the self reroll (reroll_task) and the offensive one
    (reroll_opponent_task). ``difficulty_shift`` (reroll_task only) picks the
    replacement pool from a shifted tier (−1 = one tier easier). The old
    instance is GC'd when progress-free; the draw is biased away from the same
    source task. Raises ShopError on an un-rerollable tile."""
    from db.models import EventBoardTile, EventTask
    from services.boardgame_engine import (
        _materialize_instance,
        _mercy_deadline,
        _task_pool,
    )

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

    difficulty = _shifted_difficulty(tile.difficulty, int(difficulty_shift or 0))
    pool = _task_pool(session, event_id, difficulty)
    if old_source is not None and len(pool) > 1:
        pool = [t for t in pool if t.id != old_source]
    if not pool:
        raise ShopError(409, "Cannot reroll", "No other tasks in this tile's pool.")
    choice = (rng or random).choice(pool)

    instance = _materialize_instance(session, event_id, team_id, choice,
                                     int(pos.turns_completed or 0))
    pos.current_task_id = instance.id
    pos.task_assigned_at = datetime.now()
    pos.mercy_deadline = _mercy_deadline(settings, pos.mercy_count)
    session.flush()

    _discard_task_instance(session, event_id, old_task_id)
    session.flush()

    # New instance = new matcher target.
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(redis_conn)
    except Exception:
        pass
    return instance


def _use_reroll_task(session, redis_conn, event_id, team_id, pos, item,
                     settings, rng=None, target=None) -> dict:
    """Redraw the current tile's task (unobtainable-RNG escape hatch). The old
    instance is dropped (GC'd when progress-free). ``effect_config.
    difficulty_shift`` (int, default 0) redraws from a shifted difficulty tier
    (−1 = one tier easier); 0/absent keeps the existing behavior."""
    try:
        shift = int(_cfg(item.effect_config).get("difficulty_shift", 0) or 0)
    except (TypeError, ValueError):
        shift = 0
    instance = _reroll_current_task(session, redis_conn, event_id, team_id, pos,
                                    settings, rng=rng, difficulty_shift=shift)
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
    """Place a bulwark on a tile: ANY team whose movement crosses it — the
    placer included — stops on it. Consumption (break_on pass/land/both) and
    the turn stall (stall_turns) come from the resolved behavior — registry
    defaults ← item effect_config ← the event's leader override — snapshotted
    into the placed effect so mid-event re-tuning never mutates live traps."""
    from db.models import EventBoardEffect
    from services.boardgame_effects import (
        load_event_behavior_overrides,
        resolve_effect_behavior,
    )
    from services.boardgame_engine import finish_idx, load_tiles

    tile_idx = (target or {}).get("target_tile_idx")
    if tile_idx is None:
        # web50a: default to the PLACER'S current tile (an explicit
        # target_tile_idx still wins).
        tile_idx = int(pos.tile_idx or 0)
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
    behavior = resolve_effect_behavior(
        item.effect, item=item,
        overrides=load_event_behavior_overrides(session, event_id))
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id,
        target_tile_idx=int(tile_idx), effect_type="roadblock",
        effect_config=json.dumps(behavior), status="active",
    ))
    session.flush()
    return {"roadblock_tile_idx": int(tile_idx), "behavior": behavior}


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

    # Defense check: an armed shield or covering ward eats the freeze.
    absorbed = _absorb_defense(session, event_id, target_team, item.effect)
    if absorbed is not None:
        return absorbed

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


# --------------------------------------------------------------------------- #
# web50a effect handlers
# --------------------------------------------------------------------------- #
def _resolve_offensive_target(session, event_id, team_id, target):
    """Validate an offensive item's target_team_id (mirrors freeze_opponent):
    422 missing/self, 404 not-on-board, 409 already finished. Returns
    (target_team_id, target_position)."""
    from db.models import EventBoardPosition

    target_team = (target or {}).get("target_team_id")
    if target_team is None:
        raise ShopError(422, "Pick a target", "Pass target_team_id.")
    target_team = int(target_team)
    if target_team == team_id:
        raise ShopError(422, "Bad target", "You cannot target your own team.")
    tpos = (session.query(EventBoardPosition)
            .filter(EventBoardPosition.team_id == target_team).first())
    if tpos is None or tpos.event_id != event_id:
        raise ShopError(404, "Bad target", "That team is not on this board.")
    if tpos.status == "finished":
        raise ShopError(409, "Bad target", "That team has already finished.")
    return target_team, tpos


def _use_extra_dice(session, redis_conn, event_id, team_id, pos, item,
                    settings, rng=None, target=None) -> dict:
    """Arm extra dice for the team's NEXT roll (drained in
    boardgame_engine.perform_roll via _consume_extra_dice)."""
    from db.models import EventBoardEffect

    existing = (session.query(EventBoardEffect)
                .filter(EventBoardEffect.event_id == event_id,
                        EventBoardEffect.target_team_id == team_id,
                        EventBoardEffect.effect_type == "extra_dice",
                        EventBoardEffect.status == "active")
                .first())
    if existing is not None:
        raise ShopError(409, "Already armed", "Extra dice are already armed.")
    n = 1
    try:
        n = max(1, min(8, int(_cfg(item.effect_config).get("extra_dice", 1))))
    except (TypeError, ValueError):
        pass
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id, target_team_id=team_id,
        effect_type="extra_dice",
        effect_config=json.dumps({"extra_dice": n}), status="active",
    ))
    session.flush()
    return {"extra_dice": n}


def _use_choose_roll(session, redis_conn, event_id, team_id, pos, item,
                     settings, rng=None, target=None) -> dict:
    """Pick the team's next roll VALUE (forced in perform_roll via
    _consume_choose_roll). ``target['value']`` must be achievable for the
    event's dice config."""
    from db.models import EventBoardEffect

    value = (target or {}).get("value")
    if value is None or not isinstance(value, int) or isinstance(value, bool):
        raise ShopError(422, "Pick a value", "Pass target.value — the roll total.")
    movement = settings.get("movement") or {}
    if movement.get("mode") == "fixed_step":
        try:
            step = max(1, int(movement.get("fixed_step") or 1))
        except (TypeError, ValueError):
            step = 1
        lo = hi = step
    else:
        try:
            count = max(1, min(8, int(movement.get("dice_count") or 1)))
            sides = max(2, min(100, int(movement.get("dice_sides") or 6)))
        except (TypeError, ValueError):
            count, sides = 1, 6
        lo, hi = count, count * sides
    if not (lo <= value <= hi):
        raise ShopError(422, "Unreachable roll",
                        f"Choose a value between {lo} and {hi} for this event's dice.")
    existing = (session.query(EventBoardEffect)
                .filter(EventBoardEffect.event_id == event_id,
                        EventBoardEffect.target_team_id == team_id,
                        EventBoardEffect.effect_type == "choose_roll",
                        EventBoardEffect.status == "active")
                .first())
    if existing is not None:
        raise ShopError(409, "Already armed", "A chosen roll is already armed.")
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id, target_team_id=team_id,
        effect_type="choose_roll",
        effect_config=json.dumps({"value": int(value)}), status="active",
    ))
    session.flush()
    return {"chosen_roll": int(value)}


def _use_reroll_move(session, redis_conn, event_id, team_id, pos, item,
                     settings, rng=None, target=None) -> dict:
    """Re-roll the team's PREVIOUS move: revert the piece to that move's
    origin, discard the not-yet-completed task drawn on landing, and roll
    fresh from the origin. Already-awarded coins from prior COMPLETED tasks
    are untouched. Limitation: any roadblock the reverted move consumed is
    NOT re-armed (a move never re-arms a tile effect)."""
    from services.boardgame_engine import _move_piece, load_tiles, roll_dice

    if pos.status != "active" or not pos.current_task_id:
        raise ShopError(409, "Cannot reroll move",
                        "Reroll right after landing, before finishing the new task.")
    if not pos.last_roll:
        raise ShopError(409, "No move to reroll", "The team hasn't rolled yet.")
    try:
        last = json.loads(pos.last_roll)
    except (TypeError, ValueError):
        last = None
    if not isinstance(last, dict) or last.get("from") is None:
        raise ShopError(409, "No move to reroll",
                        "The previous move can't be reconstructed.")
    origin = int(last["from"])
    tiles = load_tiles(session, event_id)
    if not tiles:
        raise ShopError(409, "No board", "The event has no tiles.")
    if origin not in {int(t.idx) for t in tiles}:
        raise ShopError(409, "Cannot reroll move",
                        "The previous origin tile no longer exists.")

    # Drop the not-yet-completed instance the prior landing assigned.
    _discard_task_instance(session, event_id, pos.current_task_id)
    pos.current_task_id = None
    pos.tile_idx = origin

    faces = roll_dice(settings, rng)
    steps = sum(faces)
    summary = _move_piece(session, event_id, team_id, pos, tiles, origin, steps,
                          settings, rng=rng)
    summary["dice"] = faces
    summary["rerolled"] = True
    pos.last_roll = json.dumps({
        "dice": faces, "from": origin, "to": summary["to"],
        "at": int(datetime.now().timestamp()), "rerolled": True,
    })
    session.flush()

    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(redis_conn)
    except Exception:
        pass
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_roll", "event_id": event_id, "team_id": team_id,
            "dice": faces, "from": origin, "to": summary["to"],
            "won": summary["won"], "task_label": summary.get("task_label"),
            "rerolled": True,
        })
    except Exception:
        pass
    return {"rerolled": True, **summary}


def _use_ward(session, redis_conn, event_id, team_id, pos, item,
              settings, rng=None, target=None) -> dict:
    """Arm a ward: negate the next OFFENSIVE effect whose key its
    ``effect_config.blocks`` covers (or the token 'offensive' = any). Consumed
    by _absorb_defense. Wards may stack (e.g. a narrow rat-poison + a broad
    ward scroll)."""
    from db.models import EventBoardEffect

    blocks = _cfg(item.effect_config).get("blocks")
    if not isinstance(blocks, list) or not blocks:
        blocks = ["offensive"]
    blocks = [str(b) for b in blocks if b]
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id, target_team_id=team_id,
        effect_type="ward",
        effect_config=json.dumps({"blocks": blocks}), status="active",
    ))
    session.flush()
    return {"warded": True, "blocks": blocks}


def _use_cleanse(session, redis_conn, event_id, team_id, pos, item,
                 settings, rng=None, target=None) -> dict:
    """Clear the team's active NEGATIVE effects (freeze on them, any coin_toll
    debuff) and lift a roadblock stall — positive self-buffs are left alone."""
    from db.models import EventBoardEffect

    rows = (session.query(EventBoardEffect)
            .filter(EventBoardEffect.event_id == event_id,
                    EventBoardEffect.target_team_id == team_id,
                    EventBoardEffect.effect_type.in_(list(_NEGATIVE_EFFECTS)),
                    EventBoardEffect.status == "active")
            .all())
    cleared = []
    for r in rows:
        r.status = "consumed"
        cleared.append(r.effect_type)
    unblocked = False
    if pos.status == "blocked":
        pos.blocked_until_turn = None
        tiles = load_tiles(session, event_id)
        by_idx = {int(t.idx): t for t in tiles}
        assign_tile_task(session, event_id, team_id,
                         by_idx.get(int(pos.tile_idx or 0)), pos, settings, rng=rng)
        unblocked = True
    session.flush()
    if unblocked:
        try:
            from services.event_engine import publish_event_admin_bump

            publish_event_admin_bump(redis_conn)
        except Exception:
            pass
    return {"cleansed": cleared, "unblocked": unblocked}


def _distinct_tier_order(difficulty):
    order = list(_DIFFICULTY_ORDER)
    if difficulty in order:
        return [difficulty] + [t for t in order if t != difficulty]
    return order


def _draw_choice_candidates(session, event_id, difficulty, n, distinct, same, rng):
    """Draw up to ``n`` candidate SOURCE tasks for a choose_task pick: distinct
    tiers when ``distinct`` (one per tier, current first), else all from the
    current tier. Returns [(source_task, difficulty_label)]."""
    from services.boardgame_engine import _task_pool

    rng = rng or random
    try:
        n = max(1, min(6, int(n or 3)))
    except (TypeError, ValueError):
        n = 3
    out = []
    seen = set()
    if distinct and not same:
        for tier in _distinct_tier_order(difficulty)[:n]:
            pool = [t for t in _task_pool(session, event_id, tier) if t.id not in seen]
            if not pool:
                continue
            t = rng.choice(pool)
            seen.add(t.id)
            out.append((t, tier))
    else:
        pool = list(_task_pool(session, event_id, difficulty))
        if pool:
            for t in rng.sample(pool, min(n, len(pool))):
                out.append((t, difficulty))
    return out


def _use_choose_task(session, redis_conn, event_id, team_id, pos, item,
                     settings, rng=None, target=None) -> dict:
    """Draw N candidate tasks for the CURRENT tile and stash them on
    ``pos.pending_choice`` for the team to pick via
    POST /events/{id}/board/choice. Does NOT change the live task yet."""
    from db.models import EventBoardTile

    if pos.status != "active" or not pos.current_task_id:
        raise ShopError(409, "Nothing to choose", "Use this on a live task tile.")
    tile = (session.query(EventBoardTile)
            .filter(EventBoardTile.event_id == event_id,
                    EventBoardTile.idx == int(pos.tile_idx or 0))
            .first())
    if tile is None:
        raise ShopError(409, "Cannot choose", "This tile has no task pool.")
    if tile.task_id is not None:
        raise ShopError(409, "Cannot choose",
                        "This tile pins one specific task — no choices to draw.")
    if not tile.difficulty:
        raise ShopError(409, "Cannot choose",
                        "This tile has no task pool to draw from.")
    cfg = _cfg(item.effect_config)
    cands = _draw_choice_candidates(
        session, event_id, tile.difficulty, cfg.get("candidates", 3),
        bool(cfg.get("distinct_difficulty")), bool(cfg.get("same_difficulty")), rng)
    if not cands:
        raise ShopError(409, "Cannot choose", "No tasks available to choose from.")
    pending = [{"index": i, "label": t.label, "task_id": t.id, "difficulty": diff}
               for i, (t, diff) in enumerate(cands)]
    pos.pending_choice = json.dumps(pending)
    session.flush()
    return {"pending_choice": pending, "candidates": len(pending)}


def apply_task_choice(session, redis_conn, event_id: int, team_id: int,
                      choice_index: int) -> dict:
    """Resolve a pending choose_task pick: assign the chosen candidate as the
    team's live task, clear ``pending_choice``, refresh the matcher, publish a
    frame. Raises ShopError on a stale/invalid pick. Caller commits."""
    from db.models import EventBoardPosition, EventTask
    from services.boardgame_engine import _materialize_instance, _mercy_deadline

    pos = (session.query(EventBoardPosition)
           .filter(EventBoardPosition.team_id == team_id)
           .with_for_update().first())
    if pos is None or pos.event_id != event_id:
        raise ShopError(404, "No board state", "The team has no board position.")
    if not pos.pending_choice:
        raise ShopError(409, "No choice pending", "There is no task choice to make.")
    try:
        pending = json.loads(pos.pending_choice)
    except (TypeError, ValueError):
        pending = None
    if not isinstance(pending, list) or not pending:
        pos.pending_choice = None
        session.flush()
        raise ShopError(409, "No choice pending", "The pending choice was invalid.")
    match = next((c for c in pending
                  if isinstance(c, dict) and int(c.get("index", -1)) == int(choice_index)),
                 None)
    if match is None:
        raise ShopError(422, "Bad choice",
                        f"choice_index {choice_index} is not one of the candidates.")
    if pos.status != "active":
        raise ShopError(409, "Choice expired",
                        "The team is no longer on that task.")
    source = (session.query(EventTask)
              .filter(EventTask.id == match.get("task_id"),
                      EventTask.event_id == event_id).first())
    if source is None:
        raise ShopError(409, "Task unavailable", "The chosen task no longer exists.")

    settings = load_board_settings(session, event_id)
    old_task_id = pos.current_task_id
    instance = _materialize_instance(session, event_id, team_id, source,
                                     int(pos.turns_completed or 0))
    pos.current_task_id = instance.id
    pos.status = "active"
    pos.task_assigned_at = datetime.now()
    pos.mercy_deadline = _mercy_deadline(settings, pos.mercy_count)
    pos.pending_choice = None
    session.flush()
    _discard_task_instance(session, event_id, old_task_id)
    session.flush()

    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(redis_conn)
    except Exception:
        pass
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_task_chosen", "event_id": event_id, "team_id": team_id,
            "task_label": instance.label, "task_difficulty": instance.difficulty,
        })
    except Exception:
        pass
    return {"task_id": instance.id, "task_label": instance.label,
            "task_difficulty": instance.difficulty}


def _use_steal_item(session, redis_conn, event_id, team_id, pos, item,
                    settings, rng=None, target=None) -> dict:
    """Steal one random OWNED item from the target team and reassign it to the
    acting team. 409 when the target has nothing owned. An armed shield/ward
    on the target absorbs it instead."""
    from db.models import EventTeamInventory

    target_team, _tpos = _resolve_offensive_target(session, event_id, team_id, target)
    absorbed = _absorb_defense(session, event_id, target_team, item.effect)
    if absorbed is not None:
        return absorbed
    owned = (session.query(EventTeamInventory)
             .filter(EventTeamInventory.event_id == event_id,
                     EventTeamInventory.team_id == target_team,
                     EventTeamInventory.status == "owned")
             .all())
    if not owned:
        raise ShopError(409, "Nothing to steal", "That team has no items to steal.")
    victim = (rng or random).choice(owned)
    stolen_shop_item_id = victim.shop_item_id
    victim.team_id = team_id  # reassign the copy to the acting team
    session.flush()
    return {"target_team_id": target_team, "stolen_inventory_id": victim.id,
            "stolen_shop_item_id": stolen_shop_item_id}


def _use_reroll_opponent_task(session, redis_conn, event_id, team_id, pos, item,
                              settings, rng=None, target=None) -> dict:
    """Reroll the target team's current tile task (same-tier redraw); their
    view refreshes via the matcher bump + a live frame. Absorbed by a
    shield/ward on the target."""
    target_team, tpos = _resolve_offensive_target(session, event_id, team_id, target)
    absorbed = _absorb_defense(session, event_id, target_team, item.effect)
    if absorbed is not None:
        return absorbed
    if tpos.status != "active" or not tpos.current_task_id:
        raise ShopError(409, "Nothing to reroll", "That team has no live task.")
    instance = _reroll_current_task(session, redis_conn, event_id, target_team,
                                    tpos, settings, rng=rng, difficulty_shift=0)
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_item_used", "event_id": event_id,
            "team_id": target_team, "effect": "reroll_opponent_task",
            "task_label": instance.label,
        })
    except Exception:
        pass
    return {"target_team_id": target_team, "task_id": instance.id,
            "task_label": instance.label, "task_difficulty": instance.difficulty}


def _use_knockback(session, redis_conn, event_id, team_id, pos, item,
                   settings, rng=None, target=None) -> dict:
    """Push the target team back ``effect_config.tiles`` (default 3) tiles
    (never below 0) and reassign the task for their new tile via the normal
    landing path. Absorbed by a shield/ward on the target."""
    target_team, tpos = _resolve_offensive_target(session, event_id, team_id, target)
    absorbed = _absorb_defense(session, event_id, target_team, item.effect)
    if absorbed is not None:
        return absorbed
    tiles = load_tiles(session, event_id)
    if not tiles:
        raise ShopError(409, "No board", "The event has no tiles.")
    n = 3
    try:
        n = max(0, int(_cfg(item.effect_config).get("tiles", 3)))
    except (TypeError, ValueError):
        pass
    old = int(tpos.tile_idx or 0)
    new = max(0, old - n)
    by_idx = {int(t.idx): t for t in tiles}
    tpos.tile_idx = new
    tpos.blocked_until_turn = None
    assign_tile_task(session, event_id, target_team, by_idx.get(new), tpos,
                     settings, rng=rng)
    session.flush()
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(redis_conn)
    except Exception:
        pass
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "board_roll", "event_id": event_id, "team_id": target_team,
            "dice": [], "from": old, "to": new, "won": False,
            "knockback": True, "placed_by_team_id": team_id,
        })
    except Exception:
        pass
    return {"target_team_id": target_team, "from": old, "to": new,
            "tiles": old - new}


def _use_coin_toll(session, redis_conn, event_id, team_id, pos, item,
                   settings, rng=None, target=None) -> dict:
    """Arm a coin toll: on the team's NEXT roll, every OTHER team on a
    passed-over tile is tolled ``effect_config.coins_per_team`` (default 25)
    coins into the mover. Drained by
    boardgame_engine._apply_coin_toll during the move."""
    from db.models import EventBoardEffect

    existing = (session.query(EventBoardEffect)
                .filter(EventBoardEffect.event_id == event_id,
                        EventBoardEffect.target_team_id == team_id,
                        EventBoardEffect.effect_type == "coin_toll",
                        EventBoardEffect.status == "active")
                .first())
    if existing is not None:
        raise ShopError(409, "Already armed", "A coin toll is already armed.")
    per_team = 25
    try:
        per_team = max(0, int(_cfg(item.effect_config).get("coins_per_team", 25)))
    except (TypeError, ValueError):
        pass
    session.add(EventBoardEffect(
        event_id=event_id, source_team_id=team_id, target_team_id=team_id,
        effect_type="coin_toll",
        effect_config=json.dumps({"coins_per_team": per_team}), status="active",
    ))
    session.flush()
    return {"coin_toll_armed": True, "coins_per_team": per_team}
