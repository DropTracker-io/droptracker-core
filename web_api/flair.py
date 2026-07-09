"""Resolve per-group subscription flair for public listings.

Flair is the cosmetic tier style shown wherever a group name appears on the
website (see ``web_api/tier_flair.py``). Only groups with an active/trialing
subscription to a tier whose flair is not "none" get a descriptor; everything
else renders like a free group. Kept to a single query — the leaderboard is hot.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from db import GroupSubscription, SubscriptionTier
from web_api.tier_flair import normalize_flair

# Subscription statuses that confer flair — parity with isSubscriptionActive()
# in the frontend (apps/web/lib/entitlements.ts).
_ACTIVE_STATUSES = ("active", "trialing")


def group_flairs(session, group_ids: Iterable[int]) -> Dict[int, dict]:
    """``{group_id: {"tier_key","tier_name","style"}}`` for the flaired subset.

    One indexed query joining ``group_subscriptions`` -> ``subscription_tiers``.
    Groups without an active, flaired subscription are simply absent from the map.
    """
    ids: List[int] = [int(g) for g in group_ids if g is not None]
    if not ids:
        return {}
    rows = (
        session.query(
            GroupSubscription.group_id,
            SubscriptionTier.key,
            SubscriptionTier.name,
            SubscriptionTier.flair,
        )
        .join(SubscriptionTier, SubscriptionTier.key == GroupSubscription.tier_key)
        .filter(
            GroupSubscription.group_id.in_(ids),
            GroupSubscription.status.in_(_ACTIVE_STATUSES),
        )
        .all()
    )
    out: Dict[int, dict] = {}
    for gid, key, name, flair in rows:
        style = normalize_flair(flair)
        if style == "none":
            continue
        out[int(gid)] = {"tier_key": key, "tier_name": name, "style": style}
    return out


def group_flair(session, group_id: int) -> Optional[dict]:
    """Single-group convenience wrapper around :func:`group_flairs`."""
    return group_flairs(session, [group_id]).get(int(group_id))
