"""When a group's lootboard is redrawn and posted, by subscription tier.

Two processes share this module:

- ``droptracker-lootboards`` (``lootboard/_board_generator.py`` driving
  ``lootboard/board_generator.py``) decides which boards to REDRAW.
- The core bot (``bots/main.py lootboard_updates``) POSTS a board to Discord
  whenever its image is newer than the one it last posted.

So a board reaches Discord once per redraw, and the redraw schedule alone sets
each tier's cadence. Two group entitlements drive it (``db/entitlements.py``,
editable per tier on /admin/tiers):

- ``lootboard_refresh_minutes``: scheduled redraw interval.
- ``lootboard_instant``: also redraw shortly after every drop notification the
  group receives in Discord.

Instant redraws go through Redis. The notification service calls
:func:`mark_dirty` after a drop notification is posted; the generator takes a
dirty board only when it can claim the per-group cooldown key
(``SET NX EX``). The dirty flag stays set while the cooldown runs, so the LAST
drop of a burst is still drawn once the cooldown lapses (a leading-edge-only
debounce would leave it off the board until the next scheduled redraw).

The image's mtime is the "last drawn" record: it survives a Redis flush and
the generator runs in a fresh subprocess every pass.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

# Group 2 is the global board. It is not a subscriber and gets no instant
# redraws: every drop on the site would retrigger it.
GLOBAL_GROUP_ID = 2
GLOBAL_REFRESH_MINUTES = 5

# Used when the entitlement is unset, zero, or cannot be resolved. Matches the
# registry default (the free plan).
DEFAULT_REFRESH_MINUTES = 30

# At most one instant redraw per group per this many seconds.
INSTANT_COOLDOWN_SECONDS = 60

# A dirty flag nobody consumed (e.g. the group lost the entitlement) expires.
DIRTY_TTL_SECONDS = 6 * 3600

IMG_ROOT = "/store/droptracker/disc/static/assets/img/clans"

_DIRTY_PREFIX = "lootboard:dirty:"
_COOLDOWN_PREFIX = "lootboard:instant_cd:"
_POSTED_PREFIX = "lootboard:posted:"


def board_path(group_id: int) -> str:
    return f"{IMG_ROOT}/{int(group_id)}/lb/lootboard.png"


def board_mtime(group_id: int) -> Optional[float]:
    """When the group's current board image was drawn, or None if it has none."""
    try:
        return os.path.getmtime(board_path(group_id))
    except OSError:
        return None


def dirty_key(group_id: int) -> str:
    return f"{_DIRTY_PREFIX}{int(group_id)}"


def cooldown_key(group_id: int) -> str:
    return f"{_COOLDOWN_PREFIX}{int(group_id)}"


def posted_key(group_id: int) -> str:
    return f"{_POSTED_PREFIX}{int(group_id)}"


@dataclass(frozen=True)
class LootboardPolicy:
    refresh_minutes: int
    instant: bool

    @property
    def refresh_seconds(self) -> int:
        return self.refresh_minutes * 60


DEFAULT_POLICY = LootboardPolicy(DEFAULT_REFRESH_MINUTES, False)
GLOBAL_POLICY = LootboardPolicy(GLOBAL_REFRESH_MINUTES, False)


def policy_from_entitlements(entitlements: Optional[dict]) -> LootboardPolicy:
    """A resolved entitlement map -> the board's schedule."""
    entitlements = entitlements or {}
    try:
        minutes = int(entitlements.get("lootboard_refresh_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        minutes = DEFAULT_REFRESH_MINUTES
    return LootboardPolicy(minutes, bool(entitlements.get("lootboard_instant")))


def load_policies(session, group_ids: Iterable[int]) -> Dict[int, LootboardPolicy]:
    """``{group_id: LootboardPolicy}`` for every id, in a handful of queries.

    Resolves each group's effective pool tier in bulk rather than calling
    ``resolve_group_entitlements`` per group (several queries each, for ~300
    groups every pass). Only the two lootboard keys are read, so the
    per-group extras that resolver folds in (event grant, sites beta) do not
    matter here. Any failure falls back to the free schedule for everyone:
    boards keep updating, just not early.
    """
    from db.entitlements import (
        _load_fallback_tier,
        effective_group_tiers,
        parse_stored_entitlements,
        resolve_tier_entitlements,
    )

    ids = [int(g) for g in group_ids]
    out: Dict[int, LootboardPolicy] = {g: DEFAULT_POLICY for g in ids}
    try:
        fallback = _load_fallback_tier(session)
        fallback_policy = policy_from_entitlements(
            resolve_tier_entitlements(parse_stored_entitlements(fallback.entitlements))
            if fallback is not None else None
        )
        by_tier: Dict[str, LootboardPolicy] = {}
        paid = effective_group_tiers(session, ids)
        for gid in ids:
            if gid in paid:
                tier = paid[gid][0]
                if tier.key not in by_tier:
                    by_tier[tier.key] = policy_from_entitlements(
                        resolve_tier_entitlements(parse_stored_entitlements(tier.entitlements))
                    )
                out[gid] = by_tier[tier.key]
            else:
                out[gid] = fallback_policy
    except Exception as e:
        print(f"[lootboard] tier policy lookup failed, using the free schedule: {e}")
    if GLOBAL_GROUP_ID in out:
        out[GLOBAL_GROUP_ID] = GLOBAL_POLICY
    return out


def scheduled_due(policy: LootboardPolicy, drawn_at: Optional[float], now: float) -> bool:
    """Whether the regular interval has passed since the board was last drawn."""
    if drawn_at is None:
        return True
    # A little slack so a board drawn a few seconds into a pass is not skipped
    # by the next pass and left for a whole extra interval.
    return now - drawn_at >= policy.refresh_seconds - 15


def next_refresh_at(policy: LootboardPolicy, drawn_at: Optional[float], now: float) -> float:
    """When the next scheduled redraw should land in Discord (for {next_refresh})."""
    base = drawn_at if drawn_at is not None else now
    # Generator pass (~1 min) plus the poster's tick, so the countdown does not
    # hit zero before the new board shows up.
    return max(now, base + policy.refresh_seconds) + 75


def plan_pass(groups, policies, dirty_ready, now, mtimes, scheduled_cap):
    """Split one generator pass into ``(instant, scheduled, deferred_count)``.

    ``dirty_ready``: groups with a pending drop whose cooldown has lapsed.
    Only groups whose tier still has instant boards count; a flag left by a
    group that since lost it is ignored (and expires on its own). Scheduled
    redraws are taken most overdue first, up to ``scheduled_cap``.
    """
    instant = [g for g in groups if g in dirty_ready and policies[g].instant]
    taken = set(instant)
    overdue = []
    for g in groups:
        if g in taken or not scheduled_due(policies[g], mtimes.get(g), now):
            continue
        drawn_at = mtimes.get(g)
        overdue.append((g, now - (drawn_at or 0) - policies[g].refresh_seconds))
    overdue.sort(key=lambda pair: pair[1], reverse=True)
    deferred = max(0, len(overdue) - scheduled_cap)
    return instant, [g for g, _ in overdue[:scheduled_cap]], deferred


# --------------------------------------------------------------------------- #
# Instant redraws (Redis)
# --------------------------------------------------------------------------- #
def _redis():
    from utils.redis import redis_client

    return redis_client.client


def mark_dirty(group_id: Optional[int]) -> bool:
    """A drop notification was posted for this group: redraw its board soon,
    if its tier has instant lootboards. Never raises (called from the
    notification send path, which must not fail over a lootboard)."""
    try:
        if group_id is None or int(group_id) == GLOBAL_GROUP_ID or int(group_id) <= 0:
            return False
        from db.entitlements import group_has_entitlement

        if not group_has_entitlement(int(group_id), "lootboard_instant"):
            return False
        _redis().set(dirty_key(group_id), str(int(time.time())), ex=DIRTY_TTL_SECONDS)
        return True
    except Exception as e:
        print(f"[lootboard] could not queue an instant redraw for group {group_id}: {e}")
        return False


def dirty_group_ids(client=None) -> set:
    """Groups with an instant redraw waiting."""
    client = client or _redis()
    out = set()
    for key in client.scan_iter(match=f"{_DIRTY_PREFIX}*", count=500):
        raw = key.decode() if isinstance(key, bytes) else key
        try:
            out.add(int(raw[len(_DIRTY_PREFIX):]))
        except ValueError:
            continue
    return out


def ready_dirty_group_ids(client=None) -> set:
    """Dirty groups whose cooldown has lapsed (what the generator can take now)."""
    client = client or _redis()
    dirty = sorted(dirty_group_ids(client))
    if not dirty:
        return set()
    pipe = client.pipeline()
    for gid in dirty:
        pipe.exists(cooldown_key(gid))
    return {gid for gid, busy in zip(dirty, pipe.execute()) if not busy}


def claim_instant(group_id: int, client=None) -> bool:
    """Take a dirty board for an instant redraw: start its cooldown and clear
    the flag. Clearing BEFORE drawing means a drop that lands mid-draw sets
    the flag again and is picked up after the cooldown."""
    client = client or _redis()
    if not client.set(cooldown_key(group_id), "1", nx=True, ex=INSTANT_COOLDOWN_SECONDS):
        return False
    client.delete(dirty_key(group_id))
    return True


def consume_for_scheduled(group_id: int, client=None) -> None:
    """A scheduled redraw already includes any pending drop: clear the flag and
    start the cooldown so it is not drawn again straight away."""
    client = client or _redis()
    if client.delete(dirty_key(group_id)):
        client.set(cooldown_key(group_id), "1", ex=INSTANT_COOLDOWN_SECONDS)


# --------------------------------------------------------------------------- #
# Posting (core bot)
# --------------------------------------------------------------------------- #
def posted_mtimes(group_ids: Iterable[int], client=None) -> Dict[int, float]:
    """The board mtime each group last had posted to Discord (0 if never)."""
    ids = [int(g) for g in group_ids]
    if not ids:
        return {}
    client = client or _redis()
    values = client.mget([posted_key(g) for g in ids])
    out = {}
    for gid, raw in zip(ids, values):
        try:
            out[gid] = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            out[gid] = 0.0
    return out


def record_posted(group_id: int, mtime: float, client=None) -> None:
    client = client or _redis()
    # Long TTL only so ids of deleted groups do not linger forever.
    client.set(posted_key(group_id), repr(float(mtime)), ex=30 * 86400)


def needs_post(drawn_at: Optional[float], posted_at: float) -> bool:
    return drawn_at is not None and drawn_at > posted_at + 0.5
