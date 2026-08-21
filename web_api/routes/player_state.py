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
import time

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

# Reference data out of the manifest — the combat achievement registry and the
# collection log's tab/page structure. Both change only when their generator
# script runs, so they are cached rather than re-read on every page view.
#
# Two rules make the cache safe, and both were learned the hard way:
#
#  - An empty section is NEVER cached. These rows are written by scripts that
#    run long after a worker boots, and the previous cache stored the empty
#    list permanently because `[] is not None`. A profile whose 140 collection
#    log items were sitting in the database rendered "No collection log
#    recorded" until the process happened to restart.
#  - Even a populated section expires, so re-running a sync propagates on its
#    own instead of needing `systemctl restart droptracker-webapi`.
_MANIFEST_SECTION_TTL_SECONDS = 300
_MANIFEST_SECTION_CACHE: dict = {}


def _manifest_section(s, key, extract):
    """The decoded manifest section for ``key``, or ``[]`` if it is not usable.

    ``extract`` shapes the decoded JSON into what callers want and returns
    something falsy when the payload is not the shape it expects; unparseable
    JSON and a missing row are treated the same way, because to a caller they
    mean the same thing — the generator has not run yet.
    """
    now = time.monotonic()
    cached = _MANIFEST_SECTION_CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    from db.models import PluginManifestSection

    row = (
        s.query(PluginManifestSection)
        .filter(PluginManifestSection.key == key)
        .first()
    )
    value = []
    if row is not None:
        try:
            decoded = extract(json.loads(row.payload))
        except (TypeError, ValueError):
            decoded = None
        if decoded:
            value = decoded

    if value:
        _MANIFEST_SECTION_CACHE[key] = (now + _MANIFEST_SECTION_TTL_SECONDS, value)
    return value


def _combat_achievement_registry(s):
    """The cache-derived task registry: every task with its varp and bit.

    Read from the manifest so the site and any client share one definition.
    """
    return _manifest_section(
        s,
        "combat_achievement_tasks",
        lambda loaded: loaded.get("tasks") if isinstance(loaded, dict) else None,
    )


def _combat_achievements_for(s, player_id: int):
    """Decode a player's stored varps into named, tiered, per-monster tasks.

    The varps are what the client sends; the registry says which (varp, bit)
    each task lives at. Decoding here rather than at write time means a registry
    fix applies retroactively to everything already stored.
    """
    tasks = _combat_achievement_registry(s)
    if not tasks:
        return None

    row = (
        s.query(PlayerCombatAchievementVarps)
        .filter(PlayerCombatAchievementVarps.player_id == player_id)
        .first()
    )
    if row is None:
        return None

    from services.state_sync import deserialize_varps

    varps = deserialize_varps(row.varps)

    by_monster = {}
    by_tier = {}
    completed_total = 0

    for task in tasks:
        varp = task.get("varp")
        bit = task.get("bit")
        if not isinstance(varp, int) or not isinstance(bit, int):
            continue

        # Mask to 32 bits: the client sends signed ints, so a varp with its top
        # bit set arrives negative.
        value = varps.get(varp, 0) & 0xFFFFFFFF
        done = bool(value & (1 << bit))
        if done:
            completed_total += 1

        tier = task.get("tier") or "Unknown"
        tier_entry = by_tier.setdefault(tier, {"tier": tier, "completed": 0, "total": 0})
        tier_entry["total"] += 1
        if done:
            tier_entry["completed"] += 1

        monster = task.get("monster") or "Other"
        entry = by_monster.setdefault(
            monster, {"monster": monster, "completed": 0, "total": 0, "tasks": []}
        )
        entry["total"] += 1
        if done:
            entry["completed"] += 1
        entry["tasks"].append({
            "name": task.get("name") or "",
            "description": task.get("description") or "",
            "tier": tier,
            "type": task.get("type") or "",
            "completed": done,
        })

    tier_order = {"Easy": 0, "Medium": 1, "Hard": 2, "Elite": 3, "Master": 4, "Grandmaster": 5}
    for entry in by_monster.values():
        entry["tasks"].sort(key=lambda t: (tier_order.get(t["tier"], 9), t["name"]))

    monsters = sorted(
        by_monster.values(),
        # Most-complete first, then alphabetical: a player scanning this wants
        # to see what is finished and what is nearly finished.
        key=lambda m: (-(m["completed"] / m["total"] if m["total"] else 0), m["monster"]),
    )
    tiers = sorted(by_tier.values(), key=lambda t: tier_order.get(t["tier"], 9))

    return {
        "completed": completed_total,
        "total": len(tasks),
        "tiers": tiers,
        "monsters": monsters,
    }


def _collection_log_structure(s):
    """Tabs -> pages -> item ids, from the manifest the wiki sync populates.

    Empty until ``scripts/sync_collection_log.py`` has run; the endpoint reports
    that as ``has_structure: false`` rather than inventing a hierarchy.
    """
    return _manifest_section(
        s,
        "collection_log",
        lambda loaded: loaded if isinstance(loaded, list) else None,
    )


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

            combat = _combat_achievements_for(s, player_id)

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
                    "tasks_completed": (
                        combat["completed"] if combat
                        else (ca_row.tasks_completed if ca_row else None)
                    ),
                    "total": combat["total"] if combat else None,
                    # Per-tier and per-monster, decoded from the stored varps.
                    "tiers": combat["tiers"] if combat else [],
                    "monsters": combat["monsters"] if combat else [],
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
