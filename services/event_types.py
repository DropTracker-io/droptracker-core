"""Site-wide event-type (game format) registry + creation gate.

``web_event_types`` (db/models/events.py) is the durable source of truth —
one row per ``EVENT_KINDS`` value with ``enabled`` / ``admin_only`` toggles
and a per-kind test-group allowlist (``web_event_type_test_groups``). This
module is the read side: a short in-process TTL cache (the seasonal_state
pattern) so the create path and the create-form picker stay cheap, plus the
single gate function every creation path calls.

Gate semantics (creation-time ONLY — existing events keep running):

    superadmin                          -> always allowed
    kind disabled OR admin_only         -> allowed only for groups on the
                                           kind's test-group allowlist
    enabled + not admin_only            -> allowed (normal group-admin +
                                           'events' entitlement rules apply
                                           separately via _assert_event_admin)

Global events (group_id None) are superadmin-only upstream, so they never
reach the allowlist branch.

Writers (web_api/routes/admin.py) call ``invalidate_cache()`` after a toggle;
other processes converge within the TTL (they only *read* for display).
"""
from __future__ import annotations

import time

from db import EVENT_KINDS, EventType, EventTypeTestGroup

_CACHE_TTL_SECONDS = 30.0
_cache: dict = {"rows": None, "ts": 0.0}


def invalidate_cache() -> None:
    _cache["rows"] = None
    _cache["ts"] = 0.0


def _load(s) -> dict[str, dict]:
    """{key: {label, description, enabled, admin_only, sort, test_group_ids}}"""
    rows: dict[str, dict] = {}
    for t in s.query(EventType).order_by(EventType.sort, EventType.key).all():
        rows[t.key] = {
            "key": t.key,
            "label": t.label,
            "description": t.description or None,
            "enabled": bool(t.enabled),
            "admin_only": bool(t.admin_only),
            "sort": int(t.sort or 0),
            "test_group_ids": set(),
        }
    for tg in s.query(EventTypeTestGroup).all():
        if tg.type_key in rows:
            rows[tg.type_key]["test_group_ids"].add(int(tg.group_id))
    return rows


def get_registry(s, *, fresh: bool = False) -> dict[str, dict]:
    """The full registry, TTL-cached in-process. ``fresh=True`` bypasses the
    cache (admin reads after a write)."""
    now = time.monotonic()
    if not fresh and _cache["rows"] is not None and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["rows"]
    rows = _load(s)
    _cache["rows"] = rows
    _cache["ts"] = now
    return rows


def known_kinds(s) -> tuple[str, ...]:
    """Kinds accepted by the API: the registry's keys, falling back to the
    code-level constant if the table is somehow empty (pre-migration)."""
    reg = get_registry(s)
    return tuple(reg.keys()) or EVENT_KINDS


def creation_restricted(s, kind: str, *, group_id: int | None) -> bool:
    """True when ``kind`` needs superadmin for this group — i.e. the kind is
    disabled/admin_only and the group is not on its test allowlist. The cheap
    half of the gate: with a warm registry cache it issues NO queries, so the
    common enabled-kind create path never pays a user lookup."""
    row = get_registry(s).get(kind)
    if row is None:
        # Unknown kind: restrict (validation upstream should have 422'd
        # already; this is defense in depth).
        return True
    if not row["enabled"] or row["admin_only"]:
        return not (group_id is not None and int(group_id) in row["test_group_ids"])
    return False


def is_event_type_creatable(
    s, kind: str, *, is_superadmin: bool, group_id: int | None
) -> bool:
    """May this (user, group) create an event of ``kind``? See module doc."""
    if is_superadmin:
        return True
    return not creation_restricted(s, kind, group_id=group_id)


def creatable_kinds(s, *, is_superadmin: bool, group_id: int | None) -> list[dict]:
    """Registry rows annotated with a ``creatable`` flag for the create-form
    picker: every kind is listed (so the UI can show 'staff only' states),
    with ``creatable`` resolved for this viewer/group."""
    out = []
    for row in sorted(get_registry(s).values(), key=lambda r: (r["sort"], r["key"])):
        out.append(
            {
                "key": row["key"],
                "label": row["label"],
                "description": row["description"],
                "enabled": row["enabled"],
                "admin_only": row["admin_only"],
                "creatable": is_event_type_creatable(
                    s, row["key"], is_superadmin=is_superadmin, group_id=group_id
                ),
            }
        )
    return out
