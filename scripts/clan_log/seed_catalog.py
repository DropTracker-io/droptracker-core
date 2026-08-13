"""Seed / refresh the Clan Log catalog (``clan_log_sections`` + ``clan_log_items``).

The catalog is the list of *possible* slots a clan's board is scored against.
It is curated, not derived: ``xenforo.dt_npc_loot`` has the wiki drop tables but
no notion of "unique", its raid rarities are conditional on a unique roll, and
new content lands with no rows at all. So the source of truth is the balancing
sheet already curated for Loot Sweep (``scripts/loot_sweep/``), and the wiki
tables are used only to *check* it — see :func:`catalog_source.repair_item_id`,
which catches the sheet's in-context shorthand ("Staff" under the Ahrim header
is Ahrim's staff, not the plain staff a name lookup finds).

Idempotent: sections match on ``slug`` and items on ``(section, item_id)``, so
re-running refreshes labels and ordering rather than duplicating. Rows an admin
disabled in ``/admin`` stay disabled — a re-seed never re-enables something
somebody deliberately turned off.

Usage
-----
    # what would change? (writes nothing)
    python -m scripts.clan_log.seed_catalog

    # write it
    python -m scripts.clan_log.seed_catalog --apply

    # what does the wiki know about that the catalog is missing?
    python -m scripts.clan_log.seed_catalog --suggest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import bindparam, text  # noqa: E402

from db.models.base import Session  # noqa: E402
from db.models.clan_log import (  # noqa: E402
    ClanLogItem,
    ClanLogSection,
    SOURCE_DROP,
    SOURCE_PET,
)
from scripts.clan_log.catalog_source import (  # noqa: E402
    ASSEMBLED_ITEM_PARTS,
    CATEGORY_ORDER,
    MIN_USEFUL_TABLE_ROWS,
    WIKI_SOURCE_OVERRIDE,
    base_item_name,
    norm,
    repair_item_id,
    sections_from_template,
    slugify,
)
from utils.npc_names import (  # noqa: E402
    canonical_encounter_name,
    npc_family_tiers,
    npc_match_key,
    npc_slug_sql_expr,
)
from utils.osrs_pets import canonical_pet_name  # noqa: E402

TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "loot_sweep", "all_content_template.json"
)

# Boss section label -> its pet. Pets are part of a clan's "did we get
# everything" question but they never arrive as drops, so they are catalogued
# with attributable=False: creditable from a `pet` submission, never counted as
# evidence that a slot is missing.
PET_BY_SECTION = {
    "kree'arra": "Pet kree'arra", "general graardor": "Pet general graardor",
    "commander zilyana": "Pet zilyana", "k'ril tsutsaroth": "Pet k'ril tsutsaroth",
    "nex": "Nexling", "chaos elemental": "Pet chaos elemental",
    "venenatis / spindel": "Venenatis spiderling", "callisto / artio": "Callisto cub",
    "vet'ion / calvar'ion": "Vet'ion jr.", "king black dragon": "Prince black dragon",
    "sarachnis": "Sraracha", "dagannoth supreme": "Pet dagannoth supreme",
    "dagannoth rex": "Pet dagannoth rex", "dagannoth prime": "Pet dagannoth prime",
    "zulrah": "Pet snakeling", "vorkath": "Vorki", "vardorvis": "Butch",
    "duke sucellus": "Baron", "the whisperer": "Wisp", "the leviathan": "Lil'viathan",
    "phantom muspah": "Muphin", "kalphite queen": "Kalphite princess",
    "hueycoatl": "Huberte", "nightmare": "Little nightmare", "amoxliatl": "Moxi",
    "grotesque guardians": "Noon", "kraken (boss)": "Pet kraken", "cerberus": "Hellpuppy",
    "araxxor": "Nid", "thermonuclear devil": "Pet smoke devil",
    "alchemical hydra": "Ikkle hydra", "gauntlet": "Youngllef", "scurrius": "Scurry",
    "chambers of xeric": "Olmlet", "theater of blood": "Lil' zik",
    "tombs of amascut": "Tumeken's guardian", "abyssal sire": "Abyssal orphan",
    "fortis colosseum": "Smol heredit", "tormented demon": "Tiny tempor",
    "demonic gorillas": "Tiny tempor",
}
# Section labels where a pet mapping would be wrong (the pet belongs to a
# sibling section, or the set has no pet of its own).
PET_SKIP = {"demonic gorillas"}

# Untradeables are NOT special here. A jar, a champion scroll, a boss head and
# the raid dusts all arrive through the normal drop pipeline (they are in
# `valued_items.txt`, which is what makes the plugin send zero-GP items), so
# their absence is real evidence. Pets are the only slot that genuinely cannot
# be attributed from `drops` — they come in as their own submission type.


def _load_template() -> dict:
    with open(TEMPLATE_PATH) as fh:
        return json.load(fh)


def _item_lookup(session) -> tuple[dict[str, int], dict[int, str]]:
    rows = session.execute(
        text("SELECT item_id, item_name FROM items WHERE item_name IS NOT NULL")
    ).fetchall()
    by_norm: dict[str, int] = {}
    name_by_id: dict[int, str] = {}
    for iid, name in rows:
        name_by_id[int(iid)] = name
        by_norm.setdefault(norm(name), int(iid))
    return by_norm, name_by_id


def _npc_ids_for(session, npc_names: list[str]) -> list[int]:
    """``npc_list`` ids for a section's sources, including family donors.

    The wiki importer landed each family's table on one arbitrary member, so a
    section naming "Tombs of Amascut" has to reach the row that actually holds
    the table. Same fallback ladder ``web_api/routes/npcs`` uses.
    """
    if not npc_names:
        return []
    expr = npc_slug_sql_expr("npc_name")
    sql = text(
        f"SELECT npc_id FROM npc_list WHERE {expr} IN :slugs"
    ).bindparams(bindparam("slugs", expanding=True))
    out: list[int] = []
    for name in npc_names:
        tiers = npc_family_tiers(name) or [[npc_match_key(name)]]
        for tier in tiers:
            if not tier:
                continue
            ids = [int(r[0]) for r in session.execute(sql, {"slugs": list(tier)}).fetchall()]
            if ids:
                out.extend(ids)
                break
    return sorted(set(out))


def _wiki_table(session, npc_ids: list[int]) -> dict[int, str]:
    """``{item_id: item_name}`` the wiki says these NPCs drop."""
    if not npc_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT DISTINCT l.item_id, COALESCE(i.item_name, CONCAT('Item ', l.item_id)) "
            "FROM xenforo.dt_npc_loot l "
            "LEFT JOIN items i ON i.item_id = l.item_id "
            "WHERE l.npc_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": npc_ids},
    ).fetchall()
    return {int(iid): name for iid, name in rows}


def _is_attributable(name: str, source: str) -> bool:
    return source != SOURCE_PET


def build_catalog(session, notes: list[str]) -> list[dict]:
    """Resolve the template into catalog rows, repairing names as it goes."""
    by_norm, name_by_id = _item_lookup(session)
    sections = sections_from_template(_load_template())

    # Variants ("Scythe of vitur (uncharged)") fold onto the base slot so a
    # board row isn't three near-identical cells.
    for section in sections:
        # Route to the collective encounter when that is where the table lives
        # (every Barrows brother's gear sits on `Barrows`).
        override = WIKI_SOURCE_OVERRIDE.get(norm(section["label"]))
        sources = [override] if override else section["npcs"]
        table = _wiki_table(session, _npc_ids_for(session, sources))
        if len(table) < MIN_USEFUL_TABLE_ROWS:
            # The sheet names a sub-boss the table doesn't sit on ("Dusk" for
            # the Grotesque Guardians). The encounter canonicaliser is the
            # codebase's answer to exactly that.
            encounters = [canonical_encounter_name(n) for n in sources]
            encounters = [e for e in encounters if e and norm(e) not in {norm(s) for s in sources}]
            table = _wiki_table(session, _npc_ids_for(session, encounters)) if encounters else {}
            if len(table) < MIN_USEFUL_TABLE_ROWS:
                table = {}
            elif encounters:
                override = override or encounters[0]

        keys = {npc_match_key(n) for n in section["npcs"] if n}
        if override:
            keys.add(npc_match_key(override))
        section["npc_keys"] = sorted(keys)
        if section["npcs"] and not table:
            notes.append(f"[{section['label']}] no usable wiki drop table — names unchecked")

        resolved: dict[int, dict] = {}
        raw_items = []
        for raw in section["items"]:
            parts = ASSEMBLED_ITEM_PARTS.get((norm(section["label"]), norm(raw["name"])))
            if parts:
                notes.append(
                    f"[{section['label']}] '{raw['name']}' is assembled, never dropped "
                    f"— catalogued as its parts: {', '.join(parts)}"
                )
                raw_items.extend({**raw, "name": p, "item_id": None} for p in parts)
            else:
                raw_items.append(raw)

        for raw in raw_items:
            name = raw["name"]
            item_id = raw.get("item_id") or by_norm.get(norm(name))
            if raw["source"] != SOURCE_PET:
                item_id, note = repair_item_id(name, item_id, table)
                if note:
                    notes.append(f"[{section['label']}] {note}")
            if not item_id:
                notes.append(f"[{section['label']}] DROPPED '{name}' — no item id")
                continue

            canonical_name = name_by_id.get(item_id, name)
            base = base_item_name(canonical_name)
            base_id = by_norm.get(norm(base))
            variants: list[int] = []
            # Fold a charged/uncharged pair onto whichever id the sheet named.
            if base_id and base_id != item_id and norm(base) != norm(canonical_name):
                variants.append(base_id)

            entry = resolved.get(item_id)
            if entry:
                entry["variant_item_ids"].extend(v for v in variants if v not in entry["variant_item_ids"])
                continue
            resolved[item_id] = {
                "item_id": item_id,
                "item_name": canonical_name,
                "variant_item_ids": variants,
                "source_hint": raw["source"],
                "attributable": _is_attributable(canonical_name, raw["source"]),
            }

        # The section's pet, if it has one.
        pet_name = PET_BY_SECTION.get(norm(section["label"]))
        if pet_name and norm(section["label"]) not in PET_SKIP:
            canon = canonical_pet_name(pet_name) or pet_name
            pet_id = by_norm.get(norm(canon))
            if pet_id and pet_id not in resolved:
                resolved[pet_id] = {
                    "item_id": pet_id,
                    "item_name": name_by_id.get(pet_id, canon),
                    "variant_item_ids": [],
                    "source_hint": SOURCE_PET,
                    "attributable": False,
                }
            elif not pet_id:
                notes.append(f"[{section['label']}] pet '{canon}' is not in `items` — skipped")

        section["resolved"] = list(resolved.values())
    return [s for s in sections if s.get("resolved")]


def apply_catalog(session, sections: list[dict], apply: bool) -> dict:
    """Upsert sections + items. Returns a summary of what changed."""
    stats = {"sections_new": 0, "sections_updated": 0, "items_new": 0,
             "items_updated": 0, "items_disabled": 0}
    order_of = {c: i for i, c in enumerate(CATEGORY_ORDER)}

    for index, section in enumerate(sections):
        slug = section["slug"]
        row = session.query(ClanLogSection).filter(ClanLogSection.slug == slug).first()
        sort_order = order_of.get(section["category"], len(CATEGORY_ORDER)) * 1000 + index
        npc_keys = json.dumps(section["npc_keys"])
        if row is None:
            stats["sections_new"] += 1
            row = ClanLogSection(
                slug=slug, label=section["label"], category=section["category"],
                npc_keys=npc_keys, sort_order=sort_order, enabled=True,
            )
            if apply:
                session.add(row)
                session.flush()
        else:
            changed = (row.label != section["label"] or row.category != section["category"]
                       or row.npc_keys != npc_keys or row.sort_order != sort_order)
            if changed:
                stats["sections_updated"] += 1
                if apply:
                    row.label = section["label"]
                    row.category = section["category"]
                    row.npc_keys = npc_keys
                    row.sort_order = sort_order

        if not apply:
            stats["items_new"] += len(section["resolved"])
            continue

        existing = {
            int(i.item_id): i
            for i in session.query(ClanLogItem).filter(ClanLogItem.section_id == row.id).all()
        }
        seen = set()
        for position, item in enumerate(section["resolved"]):
            seen.add(item["item_id"])
            variants = json.dumps(item["variant_item_ids"]) if item["variant_item_ids"] else None
            current = existing.get(item["item_id"])
            if current is None:
                stats["items_new"] += 1
                session.add(ClanLogItem(
                    section_id=row.id, item_id=item["item_id"],
                    item_name=item["item_name"], variant_item_ids=variants,
                    attributable=item["attributable"], source_hint=item["source_hint"],
                    sort_order=position, enabled=True,
                ))
            else:
                if (current.item_name != item["item_name"]
                        or current.variant_item_ids != variants
                        or current.sort_order != position
                        or bool(current.attributable) != item["attributable"]):
                    stats["items_updated"] += 1
                    current.item_name = item["item_name"]
                    current.variant_item_ids = variants
                    current.sort_order = position
                    current.attributable = item["attributable"]
        # An item the sheet no longer lists is disabled, never deleted: a group
        # may already have progress rows pointing at it.
        for item_id, current in existing.items():
            if item_id not in seen and current.enabled:
                stats["items_disabled"] += 1
                current.enabled = False
    return stats


def suggest_gaps(session, limit: int = 40) -> list[str]:
    """Rare wiki lines from catalogued NPCs that the catalog does not carry.

    Advisory only — a flat rarity threshold is exactly what the catalog exists
    to avoid — but it is the cheapest way to notice a new boss's uniques after
    a wiki sync.
    """
    rows = session.execute(
        text(
            "SELECT n.npc_name, l.item_id, COALESCE(i.item_name, CONCAT('Item ', l.item_id)), "
            "       l.rarity * GREATEST(l.rolls, 1) AS p "
            "FROM xenforo.dt_npc_loot l "
            "JOIN npc_list n ON n.npc_id = l.npc_id "
            "LEFT JOIN items i ON i.item_id = l.item_id "
            "WHERE l.noted = 0 AND l.rarity * GREATEST(l.rolls, 1) <= 0.005 "
            "ORDER BY p ASC LIMIT 4000"
        )
    ).fetchall()
    have = {
        int(r[0])
        for r in session.execute(text("SELECT DISTINCT item_id FROM clan_log_items")).fetchall()
    }
    keys = {
        npc_match_key(r[0])
        for r in session.execute(text("SELECT DISTINCT label FROM clan_log_sections")).fetchall()
    }
    out = []
    for npc_name, item_id, item_name, p in rows:
        if int(item_id) in have or npc_match_key(npc_name) not in keys:
            continue
        out.append(f"{npc_name}: {item_name} (~1/{int(1 / p) if p else '?'})")
        if len(out) >= limit:
            break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the Clan Log catalog")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--suggest", action="store_true",
                        help="list rare wiki lines the catalog is missing")
    parser.add_argument("--show", action="store_true", help="print every section")
    args = parser.parse_args()

    session = Session()
    try:
        if args.suggest:
            for line in suggest_gaps(session):
                print(" ", line)
            return 0

        notes: list[str] = []
        sections = build_catalog(session, notes)
        total_items = sum(len(s["resolved"]) for s in sections)
        print(f"catalog: {len(sections)} sections / {total_items} items")

        if args.show:
            for s in sections:
                names = ", ".join(i["item_name"] for i in s["resolved"])
                print(f"  [{s['category']}] {s['label']} ({len(s['resolved'])}): {names}")

        if notes:
            print(f"\n{len(notes)} name checks:")
            for note in notes:
                print("  ", note)

        stats = apply_catalog(session, sections, args.apply)
        print("\n" + ("APPLIED" if args.apply else "DRY RUN") + ":", stats)
        if args.apply:
            session.commit()
        else:
            session.rollback()
            print("(nothing written — re-run with --apply)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
