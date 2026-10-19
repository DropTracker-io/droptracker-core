"""The Clan Log catalog, as a selection over the game's own collection log.

The first catalog was seeded from a Loot Sweep competition template. That gave
one clan's competition picks rather than the game's content: Corp, Yama and
every clue reward were missing, and the Moons had their weapons but no armour
(suggestion #207). The game already answers "which uniques does this boss
have": ``scripts/collection_log_structure.json`` is read from the cache every
week (``scripts/cache/extract-collection-log.mjs``). This module chooses which
of those pages and slots a clan board shows, and how they are grouped.

Pure: no database. ``seed_catalog`` resolves each slot to the id drops carry.

Selection rules, as agreed with the owner on 2026-10-19:

* Bosses and raids show rares only. ``EXCLUDE`` drops teleports, resources,
  quest tablets, and rewards that are bought or awarded rather than dropped.
* Clues show the mega rares plus the elite and master cosmetics, one section
  per in-game page.
* Slots that only a collection-log sync can prove are kept when notable
  (``CLOG_ONLY``), and are never treated as evidence that a slot is missing.

An item can sit on more than one page (godsword shards, 3rd age across clue
tiers). ``services.clan_log.scope_shared_items`` gives each section its own
slot and credits a drop by the dropping NPC, which is what ``npc_keys`` here
are for. A shared item needs ``npc_keys`` on every section it sits in, or that
section can never be filled; :func:`shared_items_without_npc_keys` checks it.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from scripts.clan_log.catalog_source import slugify
from utils.npc_names import npc_match_key

STRUCTURE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "collection_log_structure.json"
)

SOURCE_DROP = "drop"
SOURCE_CLOG = "clog"
SOURCE_PET = "pet"

# Not rares: teleports, resources, near-guaranteed quest tablets, and rewards
# that are bought or awarded rather than dropped.
EXCLUDE = frozenset({
    # teleports and scrolls
    "Spider cave teleport", "Key master teleport", "Zul-andra teleport",
    "Chasm teleport scroll", "Ardeaglais teleport", "Guthixian temple teleport",
    "Revenant cave teleport",
    # resources and other high-volume drops
    "Araxyte venom sac", "Frozen tear", "Bolt rack", "Granite dust",
    "Gryphon feather", "Ancient essence", "Frozen cache", "Nihil shard",
    "Atlatl dart", "Sunfire splinters", "Mokhaiotl waystone", "Demon tear",
    "Oathplate shards", "Forgotten lockbox", "Barrel of demonic tallow (full)",
    "Zulrah's scales", "Huasca seed", "Soiled page", "Desiccated page",
    "Burnt page", "Soaked page", "Spirit flakes", "Ancient shard",
    "Giant egg sac(full)", "Pristine spider silk", "Mole skin", "Mole claw",
    "Iasor seed", "Kronos seed", "Attas seed", "Awakener's orb",
    "Torn prayer scroll", "Dark relic", "Vial of blood", "Cache of runes",
    "Masori crafting kit", "Onyx", "Revenant ether",
    "Bracelet of ethereum (uncharged)", "Abyssal whip", "Coagulated venom",
    "Charged ice", "Zalcano shard",
    # quest tablets, near-guaranteed on early kills
    "Frozen tablet", "Scarred tablet", "Strangled tablet", "Sirenic tablet",
    "Slepey tablet",
    # bought, awarded or guaranteed: never a drop
    "Dizana's quiver (uncharged)", "Gauntlet cape", "Dossier",
    "Spirit angler headband", "Spirit angler top", "Spirit angler waders",
    "Spirit angler boots", "Champion's cape",
    "Thread of Elidinis", "Breach of the Scarab", "Eye of the Corruptor",
    "Jewel of the Sun", "Jewel of Amascut",
    "Xeric's guard", "Xeric's warrior", "Xeric's sentinel", "Xeric's general",
    "Xeric's champion",
    "Sinhaza shroud tier 1", "Sinhaza shroud tier 2", "Sinhaza shroud tier 3",
    "Sinhaza shroud tier 4", "Sinhaza shroud tier 5",
    "Icthlarin's shroud (tier 1)", "Icthlarin's shroud (tier 2)",
    "Icthlarin's shroud (tier 3)", "Icthlarin's shroud (tier 4)",
    "Icthlarin's shroud (tier 5)",
})

# Notable slots that never arrive as a drop. Shown, credited from the
# collection-log sync, never counted as proof a slot is missing.
CLOG_ONLY = frozenset({
    "Fire cape", "Infernal cape", "Evil chicken head", "Evil chicken wings",
    "Evil chicken legs", "Evil chicken feet", "Helmet of the moon",
})

VIRTUS = frozenset({"Virtus mask", "Virtus robe top", "Virtus robe bottom"})

# The game's "Slayer" page is 90 slots, most of them common. These are the rares.
SLAYER_RARES = frozenset({
    "Imbued heart", "Eternal gem", "Dust battlestaff", "Mist battlestaff",
    "Brine sabre", "Leaf-bladed battleaxe", "Black mask (10)", "Granite longsword",
    "Granite boots", "Wyvern visage", "Granite legs", "Granite helm", "Dragon boots",
    "Dark bow", "Dragon sword", "Broken dragon hasta", "Drake's tooth", "Drake's claw",
    "Basilisk jaw", "Aquanite tendon", "Blood shard", "Warped sceptre (uncharged)",
    "Sulphur blades", "Aranea boots", "Earthbound tecpatl", "Antler guard",
    "Horn of Plenty (empty)",
    "Mystic hat (dusk)", "Mystic robe top (dusk)", "Mystic robe bottom (dusk)",
    "Mystic gloves (dusk)", "Mystic boots (dusk)",
    "Dagon'hai hat", "Dagon'hai robe top", "Dagon'hai robe bottom",
    "Ancient ceremonial mask", "Ancient ceremonial top", "Ancient ceremonial legs",
    "Ancient ceremonial gloves", "Ancient ceremonial boots",
})

MISC_RARES = frozenset({
    "Herbi", "Chompy chick", "Dragon warhammer", "Long bone", "Curved bone",
    "Dragon full helm", "Shield left half", "Dragon metal slice", "Dragon metal lump",
    "Dragon limbs", "Amulet of eternal glory", "Shaman mask", "Evil chicken head",
    "Evil chicken wings", "Evil chicken legs", "Evil chicken feet", "Mask of Ranul",
    "Elven signet", "Crystal grail", "Enhanced crystal teleport seed",
    "Dragonstone full helm", "Dragonstone platebody", "Dragonstone platelegs",
    "Dragonstone gauntlets", "Dragonstone boots", "Broken zombie axe",
    "Broken zombie helmet", "Helmet of the moon",
})

# Demonic gorilla parts are not collection-log slots at all; kept from the
# first catalog by id.
DEMONIC_GORILLA_PARTS = [
    (19529, "Zenyte shard"), (19592, "Ballista limbs"), (19601, "Ballista spring"),
    (19586, "Light frame"), (19589, "Heavy frame"), (19610, "Monkey tail"),
]

# Extra NPCs a section's drops come from, beyond the page's own name. Only
# matters for shared items (see the module docstring). A general's bodyguards
# drop about half of all godsword shards.
NPC_EXTRAS = {
    "Kree'arra": ["Flockleader Geerin", "Wingman Skree", "Flight Kilisa"],
    "General Graardor": ["Sergeant Strongstack", "Sergeant Steelwill",
                         "Sergeant Grimspike"],
    "Commander Zilyana": ["Starlight", "Growler", "Bree"],
    "K'ril Tsutsaroth": ["Balfrug Kreeyath", "Zakl'n Gritch", "Tstanon Karlak"],
    "Vardorvis": ["Vardorvis (Awakened)"],
    "Duke Sucellus": ["Duke Sucellus (Awakened)"],
    "The Whisperer": ["The Whisperer (Awakened)"],
    "The Leviathan": ["The Leviathan (Awakened)"],
    "Venenatis and Spindel": ["Venenatis", "Spindel"],
    "Callisto and Artio": ["Callisto", "Artio"],
    "Vet'ion and Calvar'ion": ["Vet'ion", "Calvar'ion"],
    "Dagannoth Kings (shared)": ["Dagannoth Rex", "Dagannoth Supreme",
                                 "Dagannoth Prime"],
    "Wintertodt": ["Reward cart (Wintertodt)", "Supply crate"],
    "Hard (rare)": ["Clue Scroll (Hard)"],
    "Elite (rare)": ["Clue Scroll (Elite)"],
    "Elite": ["Clue Scroll (Elite)"],
    "Master (rare)": ["Clue Scroll (Master)"],
    "Master": ["Clue Scroll (Master)"],
}
# Section labels that are not themselves an NPC name.
NO_SELF_KEY = frozenset({
    "Venenatis and Spindel", "Callisto and Artio", "Vet'ion and Calvar'ion",
    "Dagannoth Kings (shared)", "Hard (rare)", "Elite (rare)", "Elite",
    "Master (rare)", "Master", "Virtus set", "Slayer (non-boss)",
    "Demonic gorillas", "Champion scrolls", "Obsidian armour", "Miscellaneous",
    "Revenants", "Elder Chaos Druids",
})

CATEGORY_ORDER = [
    "raids", "gwd", "desert_treasure", "bosses", "slayer", "wilderness",
    "group_bosses", "skilling_bosses", "clues", "misc",
]

CATEGORY_LABELS = {
    "raids": "Raids",
    "gwd": "God Wars Dungeon",
    "desert_treasure": "Desert Treasure II",
    "bosses": "Bosses",
    "slayer": "Slayer",
    "wilderness": "Wilderness",
    "group_bosses": "Multi-boss sets",
    "skilling_bosses": "Skilling bosses",
    "clues": "Treasure Trails",
    "misc": "Miscellaneous",
}

BOSS_PAGES = [
    "Zulrah", "Vorkath", "Corporeal Beast", "Yama", "Doom of Mokhaiotl",
    "Maggot King", "The Nightmare", "Phantom Muspah", "Kalphite Queen",
    "The Hueycoatl", "Amoxliatl", "Royal Titans", "Shellbane Gryphon",
    "The Mad Angel", "Sarachnis", "Fortis Colosseum", "The Gauntlet",
    "The Fight Caves", "The Inferno", "Tormented Demons", "Scurrius",
    "Giant Mole", "Hespori", "Brutus", "Obor", "Bryophyta",
    "Deranged Archaeologist",
]
SLAYER_BOSS_PAGES = [
    "Abyssal Sire", "Kraken", "Cerberus", "Thermonuclear Smoke Devil",
    "Alchemical Hydra", "Grotesque Guardians", "Araxxor", "Skotizo",
]
WILDERNESS_PAGES = [
    "Chaos Elemental", "Chaos Fanatic", "Crazy Archaeologist", "Scorpia",
    "Venenatis and Spindel", "Callisto and Artio", "Vet'ion and Calvar'ion",
    "King Black Dragon", "Revenants", "Elder Chaos Druids",
]
BARROWS_BROTHERS = ["Ahrim", "Dharok", "Guthan", "Karil", "Torag", "Verac"]
DAGANNOTH_KINGS = {
    "Dagannoth Supreme": ["Pet Dagannoth Supreme", "Archers ring", "Seercull"],
    "Dagannoth Rex": ["Pet Dagannoth Rex", "Berserker ring", "Warrior ring"],
    "Dagannoth Prime": ["Pet Dagannoth Prime", "Seers ring", "Mud battlestaff"],
}
MOONS = [("Eclipse Moon", "Eclipse atlatl"), ("Blood Moon", "Dual macuahuitl"),
         ("Blue Moon", "Blue moon spear")]
CLUE_PAGES = [
    ("Hard (rare)", "Hard Treasure Trails (Rare)"),
    ("Elite (rare)", "Elite Treasure Trails (Rare)"),
    ("Elite", "Elite Treasure Trails"),
    ("Master (rare)", "Master Treasure Trails (Rare)"),
    ("Master", "Master Treasure Trails"),
]


def load_structure(path: Optional[str] = None) -> dict:
    with open(path or STRUCTURE_PATH) as fh:
        return json.load(fh)


def build_sections(structure: dict) -> list[dict]:
    """The board's sections, in display order.

    Each is ``{slug, label, category, npc_keys, items}`` and each item is
    ``{item_id, item_name, source_hint, attributable}``. ``item_id`` is the id
    the collection log draws; ``seed_catalog`` maps it to the id drops carry.
    """
    pages: dict[str, list[tuple[int, str]]] = {}
    pets: set[str] = set()
    for tab in structure["tabs"]:
        for page in tab["pages"]:
            pages[page["name"]] = list(zip(page["items"], page["names"]))
            if page["name"] in ("All Pets", "Skilling Pets"):
                pets.update(page["names"])

    def page(name: str, only=None, drop=frozenset()) -> list[tuple[int, str]]:
        if name not in pages:
            raise KeyError(f"collection log has no page {name!r}")
        return [(i, n) for i, n in pages[name]
                if n not in EXCLUDE and n not in drop and (only is None or n in only)]

    out: list[dict] = []

    def add(category: str, label: str, items: list[tuple[int, str]],
            source_page: Optional[str] = None) -> None:
        if not items:
            return
        names = [] if label in NO_SELF_KEY else [source_page or label]
        names += NPC_EXTRAS.get(label, [])
        keys = sorted({npc_match_key(n) for n in names if n})
        rows = []
        for item_id, name in items:
            pet = name in pets
            rows.append({
                "item_id": int(item_id),
                "item_name": name,
                "source_hint": SOURCE_PET if pet else (
                    SOURCE_CLOG if name in CLOG_ONLY else SOURCE_DROP),
                "attributable": not pet and name not in CLOG_ONLY,
            })
        out.append({
            "slug": slugify(f"{category} {label}"),
            "label": label,
            "category": category,
            "npc_keys": keys,
            "items": rows,
        })

    for raid in ("Chambers of Xeric", "Theatre of Blood", "Tombs of Amascut"):
        add("raids", raid, page(raid))

    for general in ("Kree'arra", "General Graardor", "Commander Zilyana",
                    "K'ril Tsutsaroth", "Nex"):
        add("gwd", general, page(general))

    for boss in ("Vardorvis", "Duke Sucellus", "The Whisperer", "The Leviathan"):
        add("desert_treasure", boss, page(boss, drop=VIRTUS))
    add("desert_treasure", "Virtus set", page("Duke Sucellus", only=VIRTUS))

    for boss in BOSS_PAGES:
        add("bosses", boss, page(boss))

    for boss in SLAYER_BOSS_PAGES:
        add("slayer", boss, page(boss))
    add("slayer", "Slayer (non-boss)", page("Slayer", only=SLAYER_RARES))
    add("slayer", "Demonic gorillas", DEMONIC_GORILLA_PARTS)

    for boss in WILDERNESS_PAGES:
        # The Fanatic's page repeats the Chaos Elemental pet.
        drop = frozenset({"Pet Chaos Elemental"}) if boss == "Chaos Fanatic" else frozenset()
        add("wilderness", boss, page(boss, drop=drop))

    for brother in BARROWS_BROTHERS:
        add("group_bosses", brother,
            [(i, n) for i, n in page("Barrows Chests") if n.startswith(brother)],
            source_page="Barrows")
    for king, names in DAGANNOTH_KINGS.items():
        add("group_bosses", king, page("Dagannoth Kings", only=frozenset(names)))
    add("group_bosses", "Dagannoth Kings (shared)",
        page("Dagannoth Kings", only=frozenset({"Dragon axe"})))
    for moon, weapon in MOONS:
        prefix = moon.split()[0].lower() + " moon "
        add("group_bosses", moon,
            [(i, n) for i, n in page("Moons of Peril")
             if n == weapon or n.lower().startswith(prefix)],
            source_page="Lunar Chest")

    for boss in ("Wintertodt", "Tempoross", "Zalcano"):
        add("skilling_bosses", boss, page(boss))

    for label, source in CLUE_PAGES:
        add("clues", label, page(source))

    add("misc", "Champion scrolls", page("Champion's Challenge"))
    add("misc", "Obsidian armour", page("TzHaar", only=frozenset({
        "Obsidian cape", "Toktz-ket-xil", "Obsidian helmet", "Obsidian platebody",
        "Obsidian platelegs"})))
    add("misc", "Miscellaneous", page("Miscellaneous", only=MISC_RARES))
    return out


def shared_items_without_npc_keys(sections: list[dict]) -> list[str]:
    """Sections holding a shared item they could never be credited for."""
    seen: dict[int, int] = {}
    for section in sections:
        for item in section["items"]:
            seen[item["item_id"]] = seen.get(item["item_id"], 0) + 1
    problems = []
    for section in sections:
        if section["npc_keys"]:
            continue
        for item in section["items"]:
            if seen[item["item_id"]] > 1 and item["source_hint"] != SOURCE_PET:
                problems.append(f"{section['label']}: {item['item_name']}")
    return problems
