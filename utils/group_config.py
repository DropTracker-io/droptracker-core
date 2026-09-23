"""Centralized GroupConfiguration accessor with TTL-based in-process caching.

Usage
-----
    from utils import group_config as gc

    # Single value
    val = gc.get(session, group_id, gc.MINIMUM_VALUE_TO_NOTIFY, default=2_500_000)

    # Batch (one DB query for N groups × M keys)
    bulk = gc.get_bulk(session, group_ids, [gc.NOTIFY_PBS])
    notify_enabled = gc.is_truthy(bulk.get((group_id, gc.NOTIFY_PBS)))

    # Truthiness
    if gc.is_truthy(val):
        ...

    # Invalidate on write
    gc.invalidate(group_id)
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

# ── Config key constants ──────────────────────────────────────────────────────
# Canonical key the notification service reads for drop-channel routing.
DROP_CHANNEL_ID = "channel_id_to_post_loot"
MINIMUM_VALUE_TO_NOTIFY = "minimum_value_to_notify"
ONLY_SEND_MESSAGES_WITH_IMAGES = "only_send_messages_with_images"
SEND_STACKS_OF_ITEMS = "send_stacks_of_items"
LOOTBOARD_CHANNEL_ID = "lootboard_channel_id"
LOOTBOARD_MESSAGE_ID = "lootboard_message_id"
REPOST_LOOTBOARD = "repost_lootboard"
SPLIT_GP_TRACKING = "split_gp_tracking"
LOOT_BOARD_TYPE = "loot_board_type"
NOTIFY_PBS = "notify_pbs"
NOTIFY_CLOGS = "notify_clogs"
NOTIFY_CAS = "notify_cas"
MIN_CA_TIER_TO_NOTIFY = "min_ca_tier_to_notify"
NOTIFY_PETS = "notify_pets"
NOTIFY_QUESTS = "notify_quests"
NOTIFY_POINTS_AWARDED = "notify_points_awarded"
NOTIFY_DEATHS = "notify_deaths"
NOTIFY_DIARIES = "notify_diaries"
NOTIFY_SLAYER_TASKS = "notify_slayer_tasks"
# Comma-separated SLAYER_MASTER ids; an absent row means the registry default.
# Read through utils.slayer_masters.excluded_master_ids_from_config.
SLAYER_EXCLUDED_MASTERS = "slayer_excluded_masters"
# Points behavior toggle, written as "1"/"0" by the points settings route
# (web_api/routes/points.py BEHAVIOR_BOOL_KEYS). Read through
# points_replies_ephemeral() below, never directly.
POINTS_EPHEMERAL_MESSAGES = "points_ephemeral_messages"

# ── Cache internals ───────────────────────────────────────────────────────────
_TTL: float = 30.0  # seconds
# group_id → {config_key → (value_or_None, expires_monotonic)}
_cache: Dict[int, Dict[str, Tuple[Optional[str], float]]] = {}
_lock = Lock()
_MISS = object()  # sentinel: key not in cache


# ── Public API ────────────────────────────────────────────────────────────────

def is_truthy(value: Any) -> bool:
    """Return True when a config string represents an enabled/on state."""
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1")


def get(session, group_id: int, key: str, default: Any = None) -> Any:
    """Return one config value for a group, consulting cache first."""
    now = time.monotonic()
    with _lock:
        entry = _cache.get(group_id, {}).get(key, _MISS)
        if entry is not _MISS:
            stored_val, expires = entry
            if now < expires:
                return stored_val if stored_val is not None else default

    from db.models import GroupConfiguration  # lazy — avoids import-time DB touch
    row = (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == key,
        )
        .first()
    )
    value = row.config_value if row else None
    _store(group_id, key, value, now)
    return value if value is not None else default


def get_bulk(
    session,
    group_ids: List[int],
    keys: List[str],
) -> Dict[Tuple[int, str], str]:
    """Fetch config values for multiple groups + keys in a single DB query.

    Returns a dict mapping ``(group_id, key)`` → ``value_str``.  Absent keys
    are not present in the result (callers must supply their own defaults).
    All fetched rows are stored in the cache.
    """
    now = time.monotonic()
    result: Dict[Tuple[int, str], str] = {}

    # Separate hits from misses
    needed: Dict[int, List[str]] = {}
    with _lock:
        for gid in group_ids:
            g = _cache.get(gid, {})
            for key in keys:
                entry = g.get(key, _MISS)
                if entry is not _MISS:
                    stored_val, expires = entry
                    if now < expires:
                        if stored_val is not None:
                            result[(gid, key)] = stored_val
                        continue  # cache hit
                needed.setdefault(gid, []).append(key)

    if needed:
        from db.models import GroupConfiguration
        needed_gids = list(needed.keys())
        needed_keys = list({k for ks in needed.values() for k in ks})
        rows = (
            session.query(GroupConfiguration)
            .filter(
                GroupConfiguration.group_id.in_(needed_gids),
                GroupConfiguration.config_key.in_(needed_keys),
            )
            .all()
        )
        for row in rows:
            _store(row.group_id, row.config_key, row.config_value, now)
            if row.config_value is not None:
                result[(row.group_id, row.config_key)] = row.config_value

    return result


def points_replies_ephemeral(session, group_id: int) -> bool:
    """Whether the Discord replies that change a group's points stay private.

    Covers ``/add-group-points``, ``/remove-group-points`` and the changes made
    through the "Modify Entry" message menu. Absent or "0" -- the default --
    means they are posted where the whole channel can see what changed and who
    changed it; "1" means only the admin who acted sees the reply.

    Strictly "1", the way the settings route reads it back: a looser parse here
    would let the bot go private while the admin's toggle shows off. If the
    setting cannot be read, the reply stays private: that is how these replies
    always worked, and it never shows a channel something its group asked to
    keep to admins.
    """
    try:
        value = get(session, group_id, POINTS_EPHEMERAL_MESSAGES)
    except Exception as e:
        print(f"[group_config] {POINTS_EPHEMERAL_MESSAGES} unreadable for group {group_id}: {e}")
        return True
    return str(value or "").strip() == "1"


def invalidate(group_id: int, key: Optional[str] = None) -> None:
    """Evict cache entries for a group (all keys, or one specific key)."""
    with _lock:
        if key is None:
            _cache.pop(group_id, None)
        elif group_id in _cache:
            _cache[group_id].pop(key, None)


# ── Internal helper ───────────────────────────────────────────────────────────

def _store(group_id: int, key: str, value: Optional[str], now: float) -> None:
    with _lock:
        if group_id not in _cache:
            _cache[group_id] = {}
        _cache[group_id][key] = (value, now + _TTL)
