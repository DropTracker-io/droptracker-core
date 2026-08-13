"""Account state for player profiles: collection log, achievements, loadouts.

Reads the tables the plugin's state sync populates (see
``db/models/player_state.py``). These answer "what does this player have?",
which the drop/PB event streams cannot.

Every endpoint degrades to an empty-but-valid shape when a player has never
synced, so the frontend renders an honest "nothing here yet" rather than an
error — most players will not have synced when this first ships.
"""
from __future__ import annotations

import json

from quart import Blueprint, jsonify

from db import ItemList, NpcList, PersonalBestEntry, Player
from db.models import (
    PersonalBestLoadout,
    PlayerCollectionLogItem,
    PlayerCombatAchievementVarps,
    PlayerDiaryTier,
    PlayerQuestState,
    PlayerState,
)
from web_api.common import db_session, problem, with_cache_headers

player_state_bp = Blueprint("v1_player_state", __name__)

IMG_BASE = "https://www.droptracker.io/img"

# Collection logs run to ~1,500 slots; the cap is a safety net on the response
# size, not a product decision.
MAX_ITEMS_RETURNED = 2000

# Diary tiers in the order the game shows them.
DIARY_TIER_NAMES = ["Easy", "Medium", "Hard", "Elite"]

DIARY_AREA_NAMES = {
    0: "Karamja", 1: "Ardougne", 2: "Falador", 3: "Fremennik", 4: "Kandarin",
    5: "Desert", 6: "Lumbridge & Draynor", 7: "Morytania", 8: "Varrock",
    9: "Wilderness", 10: "Western Provinces", 11: "Kourend & Kebos",
}

QUEST_STATE_NAMES = {0: "not_started", 1: "in_progress", 2: "finished"}

# The task registry is reference data that changes only when the manifest is
# rebuilt, so it is read once per process rather than per request.
_CA_REGISTRY_CACHE = None
_CLOG_STRUCTURE_CACHE = None


def _combat_achievement_registry(s):
    """Task varbit -> boss, from the manifest section the plugin also reads.

    Read from the manifest rather than duplicated here so the site and the
    client can never disagree about which task belongs to which boss.
    """
    global _CA_REGISTRY_CACHE
    if _CA_REGISTRY_CACHE is not None:
        return _CA_REGISTRY_CACHE

    from db.models import PluginManifestSection

    row = (
        s.query(PluginManifestSection)
        .filter(PluginManifestSection.key == "combat_achievement_tasks")
        .first()
    )
    registry = {}
    if row is not None:
        try:
            for entry in json.loads(row.payload):
                varbit = entry.get("varbit")
                boss = entry.get("boss")
                if isinstance(varbit, int) and boss:
                    registry[varbit] = boss
        except (TypeError, ValueError):
            registry = {}

    _CA_REGISTRY_CACHE = registry
    return registry


def _combat_achievement_bosses(s, player_id: int):
    """Completed/total combat achievements per boss, ordered as the game does.

    Bosses with no completions are still listed: "0/9" is the information a
    player is looking for, and hiding them would make the page look like it only
    knows about content they have already done.
    """
    registry = _combat_achievement_registry(s)
    if not registry:
        return []

    row = (
        s.query(PlayerCombatAchievementVarps)
        .filter(PlayerCombatAchievementVarps.player_id == player_id)
        .first()
    )
    if row is None or not row.completed_tasks:
        return []

    try:
        completed = set(json.loads(row.completed_tasks))
    except (TypeError, ValueError):
        return []

    totals = {}
    done = {}
    for varbit, boss in registry.items():
        totals[boss] = totals.get(boss, 0) + 1
        if varbit in completed:
            done[boss] = done.get(boss, 0) + 1

    out = [
        {"boss": boss, "completed": done.get(boss, 0), "total": total}
        for boss, total in totals.items()
    ]
    # Most-complete first, then alphabetical - a player scanning this wants to
    # see what they have finished and what is nearly finished.
    out.sort(key=lambda b: (-(b["completed"] / b["total"] if b["total"] else 0), b["boss"]))
    return out


def _collection_log_structure(s):
    """Tabs -> pages -> item ids, from the manifest the wiki sync populates.

    Cached per process: it is reference data that only changes when the sync
    runs, and it is read on every collection log page view.
    """
    global _CLOG_STRUCTURE_CACHE
    if _CLOG_STRUCTURE_CACHE is not None:
        return _CLOG_STRUCTURE_CACHE

    from db.models import PluginManifestSection

    row = (
        s.query(PluginManifestSection)
        .filter(PluginManifestSection.key == "collection_log")
        .first()
    )
    structure = []
    if row is not None:
        try:
            loaded = json.loads(row.payload)
            if isinstance(loaded, list):
                structure = loaded
        except (TypeError, ValueError):
            structure = []

    _CLOG_STRUCTURE_CACHE = structure
    return structure


def _player_or_none(s, player_id: int):
    return s.query(Player).filter(Player.player_id == player_id).first()


def _state_for(s, player_id: int):
    return s.query(PlayerState).filter(PlayerState.player_id == player_id).first()


def _item_names(s, item_ids):
    """item_id -> display name for the ids we know about.

    Unknown ids are simply absent; the frontend falls back to the id, which is
    better than hiding an item the player genuinely owns just because our item
    table has not caught up with a game update.
    """
    if not item_ids:
        return {}
    rows = s.query(ItemList).filter(ItemList.item_id.in_(list(item_ids))).all()
    return {r.item_id: r.item_name for r in rows}


@player_state_bp.get("/players/<int:player_id>/collection-log")
async def collection_log(player_id: int):
    """The player's collection log, grouped into the game's tabs and pages.

    Every slot the game defines is returned, obtained or not: an empty slot is
    the point of a collection log, and a page that only listed what a player
    already has would be useless for deciding what to go after.

    Slots the structure does not define are dropped rather than shown. Before
    the structure existed this endpoint returned whatever had been recorded,
    which included things that are not collection log slots at all.
    """

    def _load():
        with db_session() as s:
            if _player_or_none(s, player_id) is None:
                return None

            state = _state_for(s, player_id)
            structure = _collection_log_structure(s)

            obtained = {
                row.item_id: row.quantity
                for row in s.query(PlayerCollectionLogItem)
                .filter(PlayerCollectionLogItem.player_id == player_id)
                .all()
            }

            # Every id the log actually contains, so anything else can be
            # ignored and so names can be fetched in one query.
            defined_ids = {
                item_id
                for tab in structure
                for page in tab.get("pages", [])
                for item_id in page.get("items", [])
            }
            names = _item_names(s, defined_ids)

            tabs = []
            total_slots = 0
            total_obtained = 0
            for tab in structure:
                pages = []
                for page in tab.get("pages", []):
                    items = []
                    page_obtained = 0
                    for item_id in page.get("items", []):
                        quantity = obtained.get(item_id, 0)
                        if quantity > 0:
                            page_obtained += 1
                        items.append({
                            "item_id": item_id,
                            "name": names.get(item_id) or f"Item {item_id}",
                            "quantity": quantity,
                            "obtained": quantity > 0,
                        })
                    if not items:
                        continue
                    total_slots += len(items)
                    total_obtained += page_obtained
                    pages.append({
                        "name": page.get("name") or "Unknown",
                        "obtained": page_obtained,
                        "total": len(items),
                        "items": items,
                    })
                if pages:
                    tabs.append({
                        "name": tab.get("name") or "Unknown",
                        "obtained": sum(p["obtained"] for p in pages),
                        "total": sum(p["total"] for p in pages),
                        "pages": pages,
                    })

            # Recorded slots the structure does not know about. Usually means
            # the structure is out of date after a game update, so it is worth
            # surfacing rather than hiding.
            unknown = len([i for i in obtained if i not in defined_ids])

            return {
                "player_id": player_id,
                # What the game itself reports, which stays right even when our
                # structure or our item rows lag behind.
                "slots": state.clog_slots if state else None,
                "slots_total": state.clog_slots_total if state else None,
                # What we can actually account for against the known structure.
                "obtained": total_obtained,
                "total": total_slots,
                "unknown_recorded": unknown,
                "has_structure": bool(structure),
                "tabs": tabs,
                "last_synced": state.last_synced_at.isoformat() if state and state.last_synced_at else None,
                "has_synced": state is not None,
            }

    result = await _run(_load)
    if result is None:
        return problem(404, "Player not found")
    return with_cache_headers(jsonify(result), 60)


@player_state_bp.get("/players/<int:player_id>/achievements")
async def achievements(player_id: int):
    """Combat achievements, quests, diaries and account state in one payload.

    One request rather than three: they render as tabs of a single card, and a
    profile page should not need three round-trips to draw one card.
    """

    def _load():
        with db_session() as s:
            if _player_or_none(s, player_id) is None:
                return None

            state = _state_for(s, player_id)

            ca_bosses = _combat_achievement_bosses(s, player_id)

            ca_row = (
                s.query(PlayerCombatAchievementVarps)
                .filter(PlayerCombatAchievementVarps.player_id == player_id)
                .first()
            )

            quest_rows = (
                s.query(PlayerQuestState)
                .filter(PlayerQuestState.player_id == player_id)
                .all()
            )
            quest_counts = {"not_started": 0, "in_progress": 0, "finished": 0}
            for row in quest_rows:
                key = QUEST_STATE_NAMES.get(row.state)
                if key:
                    quest_counts[key] += 1

            diary_rows = (
                s.query(PlayerDiaryTier)
                .filter(PlayerDiaryTier.player_id == player_id)
                .order_by(PlayerDiaryTier.area_id, PlayerDiaryTier.tier)
                .all()
            )
            diaries = {}
            for row in diary_rows:
                area = diaries.setdefault(
                    row.area_id,
                    {
                        "area_id": row.area_id,
                        "name": DIARY_AREA_NAMES.get(row.area_id, f"Area {row.area_id}"),
                        "tiers": [],
                    },
                )
                area["tiers"].append({
                    "tier": row.tier,
                    "name": DIARY_TIER_NAMES[row.tier] if 0 <= row.tier < len(DIARY_TIER_NAMES) else str(row.tier),
                    "completed": row.completed_count,
                })

            return {
                "player_id": player_id,
                "has_synced": state is not None,
                "last_synced": state.last_synced_at.isoformat() if state and state.last_synced_at else None,
                "account_type": state.account_type if state else None,
                "combat_level": state.combat_level if state else None,
                "combat_achievements": {
                    "tasks_completed": ca_row.tasks_completed if ca_row else None,
                    # Per-boss progress, the way the in-game interface groups it.
                    # Empty when the client had no task registry to read.
                    "bosses": ca_bosses,
                },
                "quests": quest_counts,
                "diaries": sorted(diaries.values(), key=lambda d: d["area_id"]),
            }

    result = await _run(_load)
    if result is None:
        return problem(404, "Player not found")
    return with_cache_headers(jsonify(result), 60)


@player_state_bp.get("/personal-bests/<int:pb_id>/loadout")
async def pb_loadout(pb_id: int):
    """Gear and inventory a personal best was set with, if it was captured."""

    def _load():
        with db_session() as s:
            pb = s.query(PersonalBestEntry).filter(PersonalBestEntry.id == pb_id).first()
            if pb is None:
                return None

            row = (
                s.query(PersonalBestLoadout)
                .filter(PersonalBestLoadout.pb_id == pb_id)
                .first()
            )
            if row is None:
                return {"pb_id": pb_id, "has_loadout": False,
                        "equipment": [], "inventory": []}

            from services.loadout import loadout_from_json

            equipment = loadout_from_json(row.equipment)
            inventory = loadout_from_json(row.inventory)

            names = _item_names(
                s, {e["item_id"] for e in equipment} | {e["item_id"] for e in inventory}
            )

            def decorate(entries):
                return [
                    {
                        **entry,
                        "name": names.get(entry["item_id"]) or f"Item {entry['item_id']}",
                        "icon": f"{IMG_BASE}/itemdb/{entry['item_id']}.png",
                    }
                    for entry in entries
                ]

            npc = s.query(NpcList).filter(NpcList.npc_id == pb.npc_id).first()
            return {
                "pb_id": pb_id,
                "has_loadout": True,
                "boss": npc.npc_name if npc else None,
                "equipment": decorate(equipment),
                "inventory": decorate(inventory),
            }

    result = await _run(_load)
    if result is None:
        return problem(404, "Personal best not found")
    return with_cache_headers(jsonify(result), 300)


async def _run(fn):
    """Run a blocking DB read off the event loop."""
    import asyncio

    return await asyncio.to_thread(fn)
