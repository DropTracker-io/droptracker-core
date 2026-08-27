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
    CollectionLogEntry,
    PersonalBestLoadout,
    PlayerCollectionLogItem,
    PlayerCombatAchievementVarps,
    PlayerDiaryTier,
    PlayerQuestState,
    PlayerState,
)
from web_api.common import db_session, problem, proof_url, with_cache_headers

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
    """Tabs -> pages -> item ids, from the manifest the structure sync populates.

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


def _name_key(name):
    """The form the two name sources can be compared in, or ``None``.

    Our items table and the structure's names disagree on spacing and case often
    enough that comparing the raw strings loses matches that are plainly the
    same item ("Trident of the seas" against "Trident of the Seas").
    """
    if not isinstance(name, str):
        return None
    return " ".join(name.split()).lower() or None


def _slot_by_name(slot_ids, item_names, structure_names):
    """name key -> the one slot it identifies. Ambiguous names map to ``None``.

    Both name sources feed the index because neither covers every slot: a dozen
    slots have no ``items`` row at all, which is exactly why the structure
    carries the game's own name for each slot alongside the ids.

    A name that several slots share identifies none of them, and the index says
    so rather than picking one. "Graceful hood" is two slots, "Ancient page" is
    twenty-six, "Chompy bird hat" eighteen — guessing would put one player's
    screenshot on a slot they never filled.
    """
    by_name = {}
    for slot_id in slot_ids:
        for name in (item_names.get(slot_id), structure_names.get(slot_id)):
            key = _name_key(name)
            if key is None:
                continue
            if by_name.setdefault(key, slot_id) != slot_id:
                by_name[key] = None
    return by_name


def _fold_clog_rows(rows):
    """One ``{"ts", "image_url"}`` from every row recorded against a slot.

    A player can have several rows for one item — clog unlocks are not deduped
    (the ``notified`` table only guards drops), so a re-sent notification just
    writes another row. The date comes from the earliest row, because that is
    when they actually got it; the screenshot comes from the earliest row that
    *has* one, because the later rows re-announce the same unlock and a
    screenshot we hold beats a slot showing nothing. Roughly 100 of the ~1,400
    duplicated player/item pairs in production only have their screenshot on a
    later row.

    Returns ``None`` when the rows say nothing worth sending.
    """
    entries = sorted(
        ((_epoch(date_added), proof_url(image_url)) for _, date_added, image_url in rows),
        # Undated rows sort last: they cannot answer "when", and a dated row can.
        key=lambda entry: (entry[0] is None, entry[0] or 0),
    )
    ts = next((t for t, _ in entries if t is not None), None)
    image = next((img for _, img in entries if img), None)
    if ts is None and image is None:
        return None
    return {"ts": ts, "image_url": image}


def _epoch(value):
    try:
        return int(value.timestamp())
    except (AttributeError, OSError, OverflowError, ValueError):
        return None


def collection_details(rows, slot_ids, item_names, structure_names):
    """slot id -> what a submission can add to that slot, for the few that have one.

    ``rows`` is every ``collection`` row for one player as
    ``(item_id, date_added, image_url)``. The result is sparse by design: a row
    exists only for an unlock the plugin announced while the player was running
    it, and everything older was backfilled by the interface scrape with no date
    and no screenshot. Most slots therefore have nothing, which is the common
    case rather than the edge case.

    The mismatch this has to survive: ``collection.item_id`` is not the log's
    slot id. The plugin resolves the item from the chat line's *name* via
    ``ItemIDSearch``, which answers with the earliest cache id sharing that
    name — 764 where the log's "Coal bag" slot is 25627, 4133 where "Crawling
    hand" is 7975, 766 where "Gem bag" is 25628. Joining on the id alone loses
    those silently, so a row whose id is not a slot falls back to matching on
    the item's name, and only when that name identifies exactly one slot.

    An ambiguous name is dropped rather than resolved to its first candidate,
    which is the whole safety margin here: a screenshot on the wrong slot is far
    worse than no screenshot at all. Once a name does identify a single slot,
    its rows simply join that slot's — folding is order-independent, so it makes
    no difference whether they arrived by id or by name.

    Measured over production's 37,420 screenshot-carrying rows: 36,582 match by
    id, 752 of the remaining 838 recover uniquely by name, and 86 stay missing.
    """
    by_slot = {}
    unmatched = {}
    for row in rows:
        target = by_slot if row[0] in slot_ids else unmatched
        target.setdefault(row[0], []).append(row)

    if unmatched:
        by_name = _slot_by_name(slot_ids, item_names, structure_names)
        for item_id, item_rows in unmatched.items():
            slot_id = by_name.get(_name_key(item_names.get(item_id)))
            if slot_id is not None:
                by_slot.setdefault(slot_id, []).extend(item_rows)

    details = {}
    for slot_id, slot_rows in by_slot.items():
        detail = _fold_clog_rows(slot_rows)
        if detail is not None:
            details[slot_id] = detail
    return details


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
            # ignored and so names can be fetched in one query. The game's name
            # for each slot is collected in the same pass: it is both the
            # display fallback for ids our items table has no row for and one
            # half of the name index the detail matcher needs.
            defined_ids = set()
            structure_names = {}
            for tab in structure:
                for page in tab.get("pages", []):
                    page_names = page.get("names") or []
                    for index, item_id in enumerate(page.get("items", [])):
                        defined_ids.add(item_id)
                        if index < len(page_names):
                            structure_names.setdefault(item_id, page_names[index])

            # The whole player's submission history in one read — ~150-900 rows
            # against ~1,900 slots, so indexing it in memory is far cheaper than
            # asking per slot. Only the three columns the detail needs, since
            # nothing here wants a hydrated ORM object.
            clog_rows = (
                s.query(
                    CollectionLogEntry.item_id,
                    CollectionLogEntry.date_added,
                    CollectionLogEntry.image_url,
                )
                .filter(CollectionLogEntry.player_id == player_id)
                .all()
            )

            # Submitted ids join the lookup rather than forming a second query:
            # the name fallback needs a name for the id the plugin recorded,
            # which is by definition an id the structure does not define.
            names = _item_names(s, defined_ids | {row[0] for row in clog_rows})
            details = collection_details(clog_rows, defined_ids, names, structure_names)

            tabs = []
            total_slots = 0
            total_obtained = 0
            for tab in structure:
                pages = []
                for page in tab.get("pages", []):
                    items = []
                    page_obtained = 0
                    # The structure carries the game's name for each slot,
                    # which covers the ids our items table has no row for yet —
                    # a slot reading "Item 30805" is worse than one reading
                    # "Dossier".
                    page_names = page.get("names") or []
                    for index, item_id in enumerate(page.get("items", [])):
                        quantity = obtained.get(item_id, 0)
                        if quantity > 0:
                            page_obtained += 1
                        fallback = page_names[index] if index < len(page_names) else None
                        items.append({
                            "item_id": item_id,
                            "name": names.get(item_id) or fallback or f"Item {item_id}",
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

            # Distinct items, which is what the game's own counter counts. The
            # page totals above count slot *instances*, and plenty of items sit
            # on several pages (a dragon pickaxe is on six), so ``obtained`` is
            # always the larger number and comparing it against the game's
            # figure would say a complete log was still missing things.
            obtained_unique = len([i for i in obtained if i in defined_ids])

            return {
                "player_id": player_id,
                # What the game itself reports, which stays right even when our
                # structure or our item rows lag behind.
                "slots": state.clog_slots if state else None,
                "slots_total": state.clog_slots_total if state else None,
                # What we can actually account for against the known structure.
                # ``obtained``/``total`` count slot instances (what the pages
                # add up to); ``obtained_unique`` counts items, which is the
                # only figure comparable with ``slots``.
                "obtained": total_obtained,
                "obtained_unique": obtained_unique,
                "total": total_slots,
                "unknown_recorded": unknown,
                "has_structure": bool(structure),
                "tabs": tabs,
                # Kept beside the tabs rather than on each slot: only a couple
                # of hundred slots have a submission behind them, so two extra
                # (mostly null) fields on all ~1,900 would have added ~15% to a
                # 145 KB payload to say "nothing" 1,700 times.
                "details": {str(slot_id): d for slot_id, d in details.items()},
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
