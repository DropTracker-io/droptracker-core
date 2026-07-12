"""Resolve item_value_overrides names from ItemList and attach icon URLs.

Seed scripts may store placeholder names like ``Item 28279`` when the GE
mapping was offline at insert time. API reads enrich from the local ``items``
catalog so admin and public surfaces always show real names.
"""
from __future__ import annotations

import re

IMG_BASE = "https://www.droptracker.io/img"

_PLACEHOLDER = re.compile(r"^Item \d+$", re.I)


def item_icon_url(item_id: int | None) -> str | None:
    if item_id is None:
        return None
    return f"{IMG_BASE}/itemdb/{int(item_id)}.png"


def _collect_item_ids(items: list[dict]) -> set[int]:
    ids: set[int] = set()
    for it in items:
        iid = it.get("item_id")
        if iid is not None:
            ids.add(int(iid))
        for c in it.get("components") or []:
            cid = c.get("item_id")
            if cid is not None:
                ids.add(int(cid))
    return ids


def _resolve_name(stored: str | None, item_id: int | None, name_by_id: dict[int, str]) -> str:
    if item_id is not None and item_id in name_by_id:
        stored = (stored or "").strip()
        if not stored or _PLACEHOLDER.match(stored):
            return name_by_id[item_id]
    return (stored or "").strip() or (f"Item {item_id}" if item_id is not None else "Unknown item")


def enrich_overrides(s, items: list[dict]) -> None:
    """Mutate override dicts in place: canonical names + ``icon_url`` fields."""
    ids = _collect_item_ids(items)
    if not ids:
        return

    from db import ItemList

    name_by_id = dict(
        s.query(ItemList.item_id, ItemList.item_name)
        .filter(ItemList.item_id.in_(ids))
        .all()
    )

    for it in items:
        iid = it.get("item_id")
        if iid is not None:
            iid = int(iid)
            it["item_name"] = _resolve_name(it.get("item_name"), iid, name_by_id)
            it["icon_url"] = item_icon_url(iid)
        else:
            it["icon_url"] = None

        enriched_components = []
        for c in it.get("components") or []:
            cid = c.get("item_id")
            cid_int = int(cid) if cid is not None else None
            enriched_components.append({
                **c,
                "item_name": _resolve_name(c.get("item_name"), cid_int, name_by_id),
                "icon_url": item_icon_url(cid_int),
            })
        it["components"] = enriched_components
