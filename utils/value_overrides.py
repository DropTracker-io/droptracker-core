"""Cached accessor for the ``item_value_overrides`` table.

The valuation engine (``utils/ge_value.py``) runs inside the intake API and the
bots, while edits happen in the separate Web API process. An in-process cache
alone therefore can't see an admin's edit. So this module uses Redis as the
shared layer (single JSON blob, invalidated on write) with a short in-process
micro-cache on top to avoid a Redis round-trip on every drop.

Propagation of an edit: the writer calls :func:`invalidate` (deletes the Redis
key + clears its own micro-cache); every other process picks up the change on
its next load once its micro-cache expires (≤ ``_MICRO_TTL`` seconds) — no
service restart required. This mirrors the invalidate discipline in
``utils/group_config.py``.
"""
from __future__ import annotations

import json
import time
from threading import Lock
from typing import Any, Dict, List, Optional

from utils.redis import RedisClient

_redis = RedisClient()

# Shared Redis cache of the assembled active overrides (JSON list of dicts).
_REDIS_KEY = "item_value_overrides:all"
_REDIS_TTL = 300  # seconds

# Per-process micro-cache so a burst of drops doesn't hit Redis for every item.
_MICRO_TTL = 15.0  # seconds
_micro: Optional[Dict[str, Any]] = None
_micro_expires: float = 0.0
_lock = Lock()


def _serialize(row) -> Dict[str, Any]:
    """ORM row → plain dict (JSON-safe), parsing the components blob."""
    try:
        components = json.loads(row.components) if row.components else []
    except (ValueError, TypeError):
        components = []
    return {
        "id": row.id,
        "item_id": row.item_id,
        "item_name": row.item_name or "",
        "divisor": row.divisor or 1,
        "flat_bonus": row.flat_bonus or 0,
        "fallback_value": row.fallback_value or 0,
        "components": components,
        "description": row.description,
        "active": bool(row.active),
    }


def _load_from_db() -> List[Dict[str, Any]]:
    """Fetch all active overrides. Fails open (returns []) so a DB hiccup never
    takes down drop intake — items just fall back to their normal valuation."""
    try:
        from db import Session, ItemValueOverride  # lazy: avoids import-time DB touch

        s = Session()
        try:
            rows = s.query(ItemValueOverride).filter(ItemValueOverride.active.is_(True)).all()
            return [_serialize(r) for r in rows]
        finally:
            s.close()
    except Exception:
        return []


def _build_index(overrides: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble id- and name-keyed lookup maps for O(1) matching."""
    by_id: Dict[str, Any] = {}
    by_name: Dict[str, Any] = {}
    for ov in overrides:
        if ov.get("item_id") is not None:
            by_id[str(ov["item_id"])] = ov
        name = (ov.get("item_name") or "").strip().lower()
        if name:
            by_name.setdefault(name, ov)
    return {"list": overrides, "by_id": by_id, "by_name": by_name}


def _get_index() -> Dict[str, Any]:
    """Return the assembled override index, using micro-cache → Redis → DB."""
    global _micro, _micro_expires
    now = time.monotonic()
    with _lock:
        if _micro is not None and now < _micro_expires:
            return _micro

    # Redis shared layer.
    overrides: Optional[List[Dict[str, Any]]] = None
    cached = _redis.get(_REDIS_KEY)
    if cached is not None:
        try:
            overrides = json.loads(cached)
        except (ValueError, TypeError):
            overrides = None

    if overrides is None:
        overrides = _load_from_db()
        try:
            _redis.setex(_REDIS_KEY, _REDIS_TTL, json.dumps(overrides))
        except Exception:
            pass

    index = _build_index(overrides)
    with _lock:
        _micro = index
        _micro_expires = time.monotonic() + _MICRO_TTL
    return index


def match(item_id: Optional[int], item_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the override for a dropped item, or ``None``.

    Matched by ``item_id`` first (robust), then by lower-cased name so manual
    submissions that arrive without an id still resolve.
    """
    index = _get_index()
    if item_id is not None:
        ov = index["by_id"].get(str(item_id))
        if ov is not None:
            return ov
    if item_name:
        return index["by_name"].get(item_name.strip().lower())
    return None


def all_active() -> List[Dict[str, Any]]:
    """All active overrides (for the public listing / plugin id export)."""
    return list(_get_index()["list"])


def _item_ids_matching_names(names) -> set:
    """items-table ids whose ``item_name`` matches one of ``names``
    (case-insensitive). Fails open (empty set) so a DB hiccup degrades the
    caller to explicit-id coverage instead of an error."""
    try:
        from db import ItemList, Session  # lazy: avoids import-time DB touch

        from sqlalchemy import func

        s = Session()
        try:
            rows = (
                s.query(ItemList.item_id)
                .filter(func.lower(ItemList.item_name).in_(sorted(names)))
                .all()
            )
            return {int(r[0]) for r in rows}
        finally:
            s.close()
    except Exception:
        return set()


def active_item_ids() -> List[int]:
    """Every item id covered by an active override — the single source for the
    plugin-facing force-screenshot list (``valued_items.txt`` on GitHub Pages,
    ``GET /value_mods``, the admin dashboard export).

    Id-keyed rows contribute their id directly. Name-only rows (``item_id``
    NULL — the Soulreaper axe pieces, Elder venator fang) are resolved to every
    items-table id sharing that name: :func:`match` values such drops by name
    at intake, but the client can only check ids, so leaving them out means the
    drop gets a server-side value and no screenshot (Maggot King fang incident,
    2026-08-03).
    """
    overrides = _get_index()["list"]
    ids = {int(o["item_id"]) for o in overrides if o.get("item_id") is not None}
    names = {
        (o.get("item_name") or "").strip().lower()
        for o in overrides
        if o.get("item_id") is None
    }
    names.discard("")
    if names:
        ids |= _item_ids_matching_names(names)
    return sorted(ids)


def invalidate() -> None:
    """Evict the shared + local caches. Call after any write to the table."""
    global _micro, _micro_expires
    try:
        _redis.delete(_REDIS_KEY)
    except Exception:
        pass
    with _lock:
        _micro = None
        _micro_expires = 0.0


# --------------------------------------------------------------------------- #
# Pure valuation math (no I/O). Kept here rather than in ge_value so it is
# unit-testable without importing the aiohttp-backed pricing layer.
# --------------------------------------------------------------------------- #
def component_price_key(component: Dict[str, Any]):
    """Stable dedupe/lookup key for a component (prefer id, else lower name)."""
    if component.get("item_id"):
        return ("id", int(component["item_id"]))
    return ("name", (component.get("item_name") or "").strip().lower())


def compute_override_from_prices(override: Dict[str, Any], price_map: Dict) -> Optional[int]:
    """``int((flat_bonus + Σ quantity × price) / divisor)`` from a pre-fetched
    ``{component_price_key: price}`` map. Returns ``None`` if any component is
    unpriced (the caller then applies the rule's ``fallback_value``)."""
    numerator = override.get("flat_bonus") or 0
    for component in override.get("components") or []:
        price = price_map.get(component_price_key(component))
        if not price:
            return None
        numerator += (component.get("quantity") or 0) * price
    return int(numerator / (override.get("divisor") or 1))
