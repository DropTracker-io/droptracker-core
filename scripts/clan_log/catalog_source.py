"""Pure helpers for seeding the Clan Log catalog.

Kept apart from :mod:`scripts.clan_log.seed_catalog` so the id rules can be
unit-tested without a database or a CLI. Which slots exist is decided in
:mod:`scripts.clan_log.catalog_spec`; this module decides which ``items`` id
each slot is keyed by.
"""
from __future__ import annotations

import re

# Charge/damage/fill qualifiers. Ids that differ only by one of these are one
# board slot ("Scythe of vitur (uncharged)" and the charged scythe).
VARIANT_NAME_PATTERNS = (
    " (uncharged)", " (charged)", " (damaged)", " (broken)", " (deadman)",
    " (full)", " (empty)", " (inert)", " (u)", " (10)",
)


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


def pick_slot_ids(drawn_id: int, candidates: set[int],
                  drop_counts: dict[int, int]) -> tuple[int, list[int]]:
    """Choose the id a slot is keyed by, and the ids that fold onto it.

    ``candidates`` is every ``items`` id sharing the slot's name (or its base
    name), plus the id the collection log draws. The primary is the one drops
    actually carry. A plain name lookup takes the first id it meets, which is
    how Vorkath's head ended up on 2425 (16 drops) rather than 21907 (6,995)
    and read "missing" for nearly every clan. Ties go to the drawn id.

    Variants are the other candidates with drop evidence, plus the drawn id,
    which is what a collection-log sync reports.
    """
    ids = set(candidates) | {int(drawn_id)}
    primary = max(ids, key=lambda i: (drop_counts.get(i, 0), i == drawn_id, -i))
    variants = sorted(
        i for i in ids
        if i != primary and (drop_counts.get(i, 0) > 0 or i == drawn_id)
    )
    return primary, variants
