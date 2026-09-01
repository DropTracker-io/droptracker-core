"""Group always-announce list — the single matching rule for forced drop posts.

The inverse of ``db.notification_blacklist``: a group leader lists an **item**
or an **NPC**, and a drop of that item — or from that NPC — is announced in the
group's Discord even when it falls below the group's ``minimum_value_to_notify``.
Built for the "notable" zero-value items the plugin already force-screenshots
(kits, dyes, untradeable pieces): the screenshot arrives, but nothing ever told
the clan about it.

This list only ever *adds* a drop announcement the value gate would have
skipped. Every other rule still applies downstream: a screenshot requirement
still withholds an imageless drop, ``create_notification`` still applies the
hidden-player and blacklist gates (so the blacklist wins when a name is on
both lists), and non-drop announcement types are untouched — clogs, pets, CAs
and PBs have their own on/off toggles already.

Name normalization is imported from ``db.notification_blacklist`` rather than
reimplemented, so the two lists can never disagree about what a name means:
``item_key`` for items, and the NPC lookup keys that fold raid mode-variants
into their base raid (listing "Chambers of Xeric" also covers Challenge Mode).

Unlike the blacklist — consulted only when a notification is actually being
enqueued — this rule runs for every drop that *failed* the value gate, which is
most drops, once per group. A small TTL cache keeps that off the database; 30s
staleness matches ``utils.group_config`` and is invisible next to the settings
page's save round-trip.
"""
from __future__ import annotations

import time
from threading import Lock

from db.notification_blacklist import _npc_lookup_keys, item_key, npc_key

#: The kinds of thing a group can always-announce.
ENTRY_TYPES = ("item", "npc")

#: How each entry type is described in a match reason.
_TYPE_LABELS = {"item": "item", "npc": "NPC"}

_TTL_SECONDS = 30.0
#: group_id -> ({"item": {keys}, "npc": {keys}}, expires_monotonic)
_cache: dict[int, tuple[dict[str, set[str]], float]] = {}
_cache_lock = Lock()


def entry_key(entry_type: str, name) -> str:
    """The ``match_key`` stored for an always-list row of this type."""
    if entry_type == "npc":
        return npc_key(name)
    return item_key(name)


def load_group_always_list(db_session, group_id) -> dict[str, set[str]]:
    """This group's list as ``{"item": {keys}, "npc": {keys}}``, TTL-cached."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(int(group_id))
        if entry is not None and now < entry[1]:
            return entry[0]

    # Imported here, not at module scope: loaded by the submission processors
    # and the web API alike, and only the query needs the ORM (it is also the
    # form the unit-test bootstrap can stub — see tests/conftest.py).
    from db.models import GroupNotificationAlwaysList

    rows = (
        db_session.query(
            GroupNotificationAlwaysList.entry_type,
            GroupNotificationAlwaysList.match_key,
        )
        .filter(GroupNotificationAlwaysList.group_id == group_id)
        .all()
    )
    out: dict[str, set[str]] = {entry_type: set() for entry_type in ENTRY_TYPES}
    for entry_type, key in rows:
        if key and entry_type in out:
            out[entry_type].add(key)
    with _cache_lock:
        _cache[int(group_id)] = (out, now + _TTL_SECONDS)
    return out


def invalidate_cache(group_id=None) -> None:
    """Evict the cache (one group, or everything). Same-process only — the web
    API writes from another process, where the TTL is the freshness bound."""
    with _cache_lock:
        if group_id is None:
            _cache.clear()
        else:
            _cache.pop(int(group_id), None)


def always_announce_reason(db_session, group_id, *, item_name=None, npc_name=None) -> str | None:
    """Why this drop must be announced despite failing the value gate, or ``None``.

    A short human-readable reason ("always-announce item 'twisted bow'") so the
    forced post is greppable in the logs and legible on its queue row.

    Fails **closed**: no group, an empty list or a DB fault all return ``None``
    — this gate only ever adds announcements, so on any doubt the drop simply
    behaves as it does today.
    """
    if not group_id:
        return None
    try:
        entries = load_group_always_list(db_session, group_id)
    except Exception:
        return None
    if not any(entries.values()):
        return None

    key = item_key(item_name) if item_name else ""
    if key and key in entries["item"]:
        return f"always-announce {_TYPE_LABELS['item']} '{key}'"
    npc_keys = _npc_lookup_keys(npc_name) if npc_name else set()
    hit = npc_keys & entries["npc"]
    if hit:
        return f"always-announce {_TYPE_LABELS['npc']} '{sorted(hit)[0]}'"
    return None
