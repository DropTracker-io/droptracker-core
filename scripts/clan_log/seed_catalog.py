"""Seed / refresh the Clan Log catalog (``clan_log_sections`` + ``clan_log_items``).

The catalog is the list of *possible* slots a clan's board is scored against.
Which slots exist comes from the game's collection log, read from the cache
(``scripts/collection_log_structure.json``); which of them a clan board shows,
and how they are grouped, is :mod:`scripts.clan_log.catalog_spec`. This script
resolves each slot to the ``items`` id drops actually carry
(:func:`catalog_source.pick_slot_ids`) and writes the result.

Idempotent: sections match on ``slug`` and items on ``(section, item_id)``, so
re-running refreshes labels and ordering rather than duplicating. A section or
item the spec no longer lists is disabled, never deleted: groups may have
ledger rows pointing at it. Rows an admin disabled in ``/admin`` stay disabled;
a re-seed never re-enables something somebody deliberately turned off.

After an ``--apply`` the catalog version changes, so every group's board needs
a rebuild. Run ``scripts/clan_log/backfill_group.py --all --apply`` rather than
waiting for the 20-minute round-robin to reach ~300 groups.

Usage
-----
    # what would change? (writes nothing)
    python -m scripts.clan_log.seed_catalog [--show]

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
from db.models.clan_log import ClanLogItem, ClanLogSection  # noqa: E402
from scripts.clan_log.catalog_source import (  # noqa: E402
    base_item_name,
    norm,
    pick_slot_ids,
)
from scripts.clan_log.catalog_spec import (  # noqa: E402
    CATEGORY_ORDER,
    SOURCE_PET,
    build_sections,
    load_structure,
    shared_items_without_npc_keys,
)
from utils.npc_names import npc_match_key  # noqa: E402


def _ids_by_base_name(session, names: list[str]) -> dict[str, set[int]]:
    wanted = {norm(base_item_name(n)) for n in names}
    out: dict[str, set[int]] = {}
    for iid, name in session.execute(
        text("SELECT item_id, item_name FROM items WHERE item_name IS NOT NULL")
    ).fetchall():
        key = norm(base_item_name(name))
        if key in wanted:
            out.setdefault(key, set()).add(int(iid))
    return out


def _drop_counts(session, item_ids: list[int]) -> dict[int, int]:
    """All-time ``drops`` rows per id. Index-only on ``drops.item_id``."""
    counts: dict[int, int] = {}
    sql = text(
        "SET STATEMENT max_statement_time=30 FOR "
        "SELECT item_id, COUNT(*) FROM drops WHERE item_id IN :ids GROUP BY item_id"
    ).bindparams(bindparam("ids", expanding=True))
    for start in range(0, len(item_ids), 80):
        chunk = item_ids[start:start + 80]
        for iid, count in session.execute(sql, {"ids": chunk}).fetchall():
            counts[int(iid)] = int(count)
    return counts


def build_catalog(session, notes: list[str], structure: dict | None = None) -> list[dict]:
    """The spec's sections with every slot keyed by the id drops carry."""
    structure = structure or load_structure()
    sections = build_sections(structure)
    for problem in shared_items_without_npc_keys(sections):
        notes.append(f"shared item with no npc_keys, can never be credited: {problem}")

    # The cache stores a dozen slots under one id and draws them as another;
    # drops carry the stored one (Unsired: 13273 stored, 25624 drawn).
    stored_for = {int(v): int(k) for k, v in (structure.get("replacements") or {}).items()}
    names = [i["item_name"] for s in sections for i in s["items"]]
    by_base = _ids_by_base_name(session, names)
    all_ids = sorted({i for ids in by_base.values() for i in ids}
                     | {i["item_id"] for s in sections for i in s["items"]}
                     | set(stored_for.values()))
    counts = _drop_counts(session, all_ids)

    for section in sections:
        resolved, seen = [], set()
        for item in section["items"]:
            drawn = item["item_id"]
            candidates = set(by_base.get(norm(base_item_name(item["item_name"])), set()))
            if drawn in stored_for:
                candidates.add(stored_for[drawn])
            if item["source_hint"] == SOURCE_PET:
                # player_pets stores the collection log's own id for every pet.
                primary, variants = drawn, []
            else:
                primary, variants = pick_slot_ids(drawn, candidates, counts)
            if primary in seen:
                continue
            seen.add(primary)
            if primary != drawn and counts.get(primary, 0):
                notes.append(f"[{section['label']}] {item['item_name']}: keyed by {primary} "
                             f"({counts.get(primary, 0)} drops), not drawn id {drawn}")
            if item["attributable"] and not any(counts.get(i, 0) for i in [primary, *variants]):
                notes.append(f"[{section['label']}] {item['item_name']}: no drop on record yet")
            resolved.append({**item, "item_id": primary, "variant_item_ids": variants})
        section["resolved"] = resolved
    return sections


def apply_catalog(session, sections: list[dict], apply: bool) -> dict:
    """Upsert sections + items, disabling what the spec dropped.

    With ``apply=False`` nothing is written; the stats say what would change.
    """
    stats = {"sections_new": 0, "sections_updated": 0, "sections_disabled": 0,
             "items_new": 0, "items_updated": 0, "items_disabled": 0}
    order_of = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    rows_by_slug = {r.slug: r for r in session.query(ClanLogSection).all()}
    wanted_slugs = {s["slug"] for s in sections}

    for index, section in enumerate(sections):
        row = rows_by_slug.get(section["slug"])
        sort_order = order_of.get(section["category"], len(CATEGORY_ORDER)) * 1000 + index
        npc_keys = json.dumps(section["npc_keys"])
        if row is None:
            stats["sections_new"] += 1
            stats["items_new"] += len(section["resolved"])
            if not apply:
                continue
            row = ClanLogSection(
                slug=section["slug"], label=section["label"], category=section["category"],
                npc_keys=npc_keys, sort_order=sort_order, enabled=True,
            )
            session.add(row)
            session.flush()
        elif (row.label != section["label"] or row.category != section["category"]
              or row.npc_keys != npc_keys or row.sort_order != sort_order):
            stats["sections_updated"] += 1
            if apply:
                row.label = section["label"]
                row.category = section["category"]
                row.npc_keys = npc_keys
                row.sort_order = sort_order

        existing = {
            int(i.item_id): i
            for i in session.query(ClanLogItem).filter(ClanLogItem.section_id == row.id).all()
        } if row.id else {}
        seen = set()
        for position, item in enumerate(section["resolved"]):
            seen.add(item["item_id"])
            variants = json.dumps(item["variant_item_ids"]) if item["variant_item_ids"] else None
            current = existing.get(item["item_id"])
            if current is None:
                stats["items_new"] += 1
                if apply:
                    session.add(ClanLogItem(
                        section_id=row.id, item_id=item["item_id"],
                        item_name=item["item_name"], variant_item_ids=variants,
                        attributable=item["attributable"], source_hint=item["source_hint"],
                        sort_order=position, enabled=True,
                    ))
                continue
            if (current.item_name != item["item_name"]
                    or current.variant_item_ids != variants
                    or current.sort_order != position
                    or current.source_hint != item["source_hint"]
                    or bool(current.attributable) != item["attributable"]):
                stats["items_updated"] += 1
                if apply:
                    current.item_name = item["item_name"]
                    current.variant_item_ids = variants
                    current.sort_order = position
                    current.source_hint = item["source_hint"]
                    current.attributable = item["attributable"]
        for item_id, current in existing.items():
            if item_id not in seen and current.enabled:
                stats["items_disabled"] += 1
                if apply:
                    current.enabled = False

    for slug, row in rows_by_slug.items():
        if slug not in wanted_slugs and row.enabled:
            stats["sections_disabled"] += 1
            if apply:
                row.enabled = False
    return stats


def suggest_gaps(session, limit: int = 40) -> list[str]:
    """Rare wiki lines from catalogued NPCs that the catalog does not carry.

    Advisory only. A flat rarity threshold is exactly what the catalog exists
    to avoid, but it is the cheapest way to notice a new boss's uniques after
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
        for r in session.execute(
            text("SELECT DISTINCT item_id FROM clan_log_items WHERE enabled = 1")
        ).fetchall()
    }
    keys = {
        npc_match_key(r[0])
        for r in session.execute(
            text("SELECT DISTINCT label FROM clan_log_sections WHERE enabled = 1")
        ).fetchall()
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
            print(f"\n{len(notes)} notes:")
            for note in notes:
                print("  ", note)

        stats = apply_catalog(session, sections, args.apply)
        print("\n" + ("APPLIED" if args.apply else "DRY RUN") + ":", stats)
        if args.apply:
            session.commit()
        else:
            session.rollback()
            print("(nothing written; re-run with --apply)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
