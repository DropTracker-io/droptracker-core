"""Turn the curated loot-sweep sheet into Clan Log catalog rows.

Pure-ish helpers, kept apart from :mod:`scripts.clan_log.seed_catalog` so the
name-repair rules can be unit-tested without a database or a CLI.

The sheet was written for humans, so its item column carries shorthand that is
correct in context and wrong out of it: under the "Ahrim" header, "Staff" means
*Ahrim's staff*, but ``items`` also contains a plain item literally called
"Staff" (id 1379), and a naive name lookup takes it. :func:`repair_item_id` is
the fix — it checks the resolved id against the boss's own wiki drop table and,
when it isn't there, re-resolves the shorthand against the names that are.
"""
from __future__ import annotations

import re

# Board grouping. The sheet has no notion of category; these are the rows a
# clan actually thinks in, and they drive both the page's section order and the
# per-category bars on the Discord summary card.
CATEGORY_BY_TASK = {
    "Barrows Brothers": "group_bosses",
    "Dagannoth Kings": "group_bosses",
    "Moons of Peril": "group_bosses",
    "Kree'arra": "gwd",
    "General Graardor": "gwd",
    "Commander Zilyana": "gwd",
    "K'ril Tsutsaroth": "gwd",
    "Nex": "gwd",
    "Chambers of Xeric": "raids",
    "Theater of Blood": "raids",
    "Tombs of Amascut": "raids",
    "Vardorvis": "desert_treasure",
    "Duke Sucellus": "desert_treasure",
    "The Whisperer": "desert_treasure",
    "The Leviathan": "desert_treasure",
    "Virtus set": "desert_treasure",
    "Chaos Elemental": "wilderness",
    "Venenatis / Spindel": "wilderness",
    "Callisto / Artio": "wilderness",
    "Vet'ion / Calvar'ion": "wilderness",
    "King Black Dragon": "wilderness",
    "Revenant Caves (pvm)": "wilderness",
    "Wilderness wards": "wilderness",
    "Elder Chaos Druid": "wilderness",
    "Abyssal Sire": "slayer",
    "Kraken (boss)": "slayer",
    "Cerberus": "slayer",
    "Araxxor": "slayer",
    "Thermonuclear Devil": "slayer",
    "Alchemical Hydra": "slayer",
    "Grotesque Guardians": "slayer",
    "Demonic Gorillas": "slayer",
    "Sarachnis": "slayer",
    "Slayer non-Boss": "slayer",
    "Obsidian armour set": "misc",
    "Champion Scrolls": "misc",
    "Miscellaneous I": "misc",
    "Miscellaneous II": "misc",
}
DEFAULT_CATEGORY = "bosses"

# Display order of the categories on the board.
CATEGORY_ORDER = [
    "raids",
    "gwd",
    "desert_treasure",
    "bosses",
    "slayer",
    "wilderness",
    "group_bosses",
    "misc",
]

CATEGORY_LABELS = {
    "raids": "Raids",
    "gwd": "God Wars Dungeon",
    "desert_treasure": "Desert Treasure II",
    "bosses": "Bosses",
    "slayer": "Slayer",
    "wilderness": "Wilderness",
    "group_bosses": "Multi-boss sets",
    "misc": "Miscellaneous",
}

# Items whose id differs by charge/damage state but which are one board slot.
# Keyed by canonical (the state a drop actually arrives in).
VARIANT_NAME_PATTERNS = (
    " (uncharged)", " (charged)", " (damaged)", " (broken)", " (deadman)",
)

# Sections whose wiki drop table lives on the collective encounter rather than
# the NPC the sheet names. Every Barrows brother has a one-line junk table of
# its own while the gear sits on `Barrows`; the Moons' table is on
# `Lunar Chest`. Without this the name check reads the junk table, finds
# nothing, and reports the whole section as suspect.
WIKI_SOURCE_OVERRIDE = {
    "ahrim": "Barrows", "dharok": "Barrows", "guthan": "Barrows",
    "karil": "Barrows", "torag": "Barrows", "verac": "Barrows",
    "eclipse moon": "Lunar Chest", "blood moon": "Lunar Chest",
    "blue moon": "Lunar Chest",
}

# A table this small is a stub, not coverage — treat it as absent so the name
# check stays silent rather than flagging every item in the section.
MIN_USEFUL_TABLE_ROWS = 5

# Sheet rows naming an item that is *assembled*, never dropped. The board has
# to track what a clan can actually obtain, or the slot reads "missing"
# forever. Keyed by (section label, sheet name) -> the real drops.
ASSEMBLED_ITEM_PARTS = {
    ("araxxor", "noxious halberd"): ["Noxious point", "Noxious blade", "Noxious pommel"],
    ("alchemical hydra", "brimstone ring"): ["Hydra's eye", "Hydra's fang", "Hydra's heart"],
}


def norm(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def slugify(label: str) -> str:
    """Stable identity for a section across re-seeds."""
    slug = re.sub(r"[^a-z0-9]+", "_", norm(label)).strip("_")
    return slug[:64] or "section"


def base_item_name(name: str) -> str:
    """Drop a charge/damage qualifier so variants can be folded together."""
    out = str(name or "")
    for suffix in VARIANT_NAME_PATTERNS:
        if out.lower().endswith(suffix):
            return out[: -len(suffix)].strip()
    return out.strip()


def repair_item_id(
    shorthand: str,
    resolved_id: int | None,
    table_names_by_id: dict[int, str],
) -> tuple[int | None, str | None]:
    """Check a sheet name against the boss's wiki drop table, repairing it.

    ``table_names_by_id`` is ``{item_id: item_name}`` for everything the wiki
    says this section's NPCs drop. Returns ``(item_id, note)`` where ``note``
    describes a repair or a doubt for the review report, and is ``None`` when
    the resolution was clean.

    An empty table means the section has no wiki coverage (new content, or a
    multi-source set with no single NPC). That is not evidence of a bad name,
    so the resolved id passes through untouched — the report says so rather
    than the script silently dropping the item.
    """
    if not table_names_by_id:
        return resolved_id, None if resolved_id else "unresolved, no wiki table to check against"

    if resolved_id in table_names_by_id:
        return resolved_id, None

    target = norm(shorthand)
    # Singular/plural both appear ("Hammer" in the sheet, "Torag's hammers" in
    # the table), so match either way round.
    forms = {target, target.rstrip("s"), f"{target}s"}
    # The sheet's shorthand is the tail of the real name ("Staff" ->
    # "Ahrim's staff", "Coif" -> "Karil's coif"). Prefer a table entry whose
    # name ends with it, then any that contains it as a whole word.
    tail, contains = [], []
    for iid, name in table_names_by_id.items():
        n = norm(name)
        if n in forms:
            return iid, (f"resolved to the boss's own '{name}'" if iid != resolved_id else None)
        if any(n.endswith(f" {f}") or n.endswith(f"'s {f}") for f in forms):
            tail.append((iid, name))
        elif any(re.search(rf"\b{re.escape(f)}\b", n) for f in forms):
            contains.append((iid, name))

    # Last resort: the distinctive first word. The sheet names the finished
    # item where the boss drops a component ("Pegasian boots" against a table
    # holding "Pegasian crystal"); one match on that word is the component.
    head = target.split()[0] if target.split() else ""
    heads = []
    if len(head) > 3:
        heads = [(iid, name) for iid, name in table_names_by_id.items()
                 if norm(name).split()[:1] == [head]]

    for candidates in (tail, contains, heads):
        if len(candidates) == 1:
            iid, name = candidates[0]
            return iid, f"'{shorthand}' repaired to '{name}' (from the wiki drop table)"
        if len(candidates) > 1:
            names = ", ".join(sorted(n for _, n in candidates))
            return resolved_id, f"'{shorthand}' is ambiguous in the drop table ({names}) — left as-is"

    if resolved_id:
        return resolved_id, (
            f"'{shorthand}' resolved to item {resolved_id} which the wiki table "
            f"does not list for this source — verify"
        )
    return None, f"'{shorthand}' could not be resolved to any item"


def sections_from_template(template: dict) -> list[dict]:
    """Flatten the loot-sweep template into catalog sections.

    Every ``group`` becomes one board row: the meta tasks (Barrows, DKs, Moons)
    carry one group per brother/king/moon, which is exactly the granularity a
    board wants, while a simple boss task carries a single group.
    """
    sections: list[dict] = []
    for task in template.get("tasks") or []:
        task_label = task.get("label") or ""
        config = task.get("config") or {}
        category = CATEGORY_BY_TASK.get(task_label, DEFAULT_CATEGORY)
        for group in config.get("groups") or []:
            label = group.get("label") or task_label
            items = []
            for item in group.get("items") or []:
                name = item.get("item_name")
                if not name:
                    continue
                items.append({
                    "name": name,
                    "source": item.get("source") or "drop",
                    "item_id": item.get("item_id"),
                })
            if not items:
                continue
            sections.append({
                "slug": slugify(f"{task_label} {label}" if task_label != label else label),
                "label": label,
                "parent": task_label,
                "category": category,
                "npcs": list(group.get("npcs") or []),
                "items": items,
            })
    return sections
