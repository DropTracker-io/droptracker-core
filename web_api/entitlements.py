"""Resolve a group's effective entitlements from its subscription tier."""
from __future__ import annotations

from typing import Optional

from db import GroupSubscription, SubscriptionTier
from web_api.entitlements_registry import (
    all_entitlements_granted,
    default_entitlements,
    parse_stored_entitlements,
    resolve_tier_entitlements,
)
from web_api.deps import is_superadmin

_ACTIVE_STATUSES = frozenset({"active", "trialing"})
# Implicit free-plan tier keys, tried in order when a group has no active paid sub.
_FALLBACK_TIER_KEYS = ("free", "basic")


def _load_fallback_tier(s) -> SubscriptionTier | None:
    for key in _FALLBACK_TIER_KEYS:
        tier = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.key == key, SubscriptionTier.active == True)  # noqa: E712
            .first()
        )
        if tier:
            return tier
    return None


def resolve_group_entitlements(
    s,
    group_id: int,
    *,
    user=None,
) -> dict[str, bool]:
    """Return the resolved entitlement map for ``group_id``.

    Superadmins receive all entitlements when administering any group.
    """
    if is_superadmin(user):
        return all_entitlements_granted()

    sub = (
        s.query(GroupSubscription)
        .filter(GroupSubscription.group_id == group_id)
        .first()
    )
    tier: SubscriptionTier | None = None
    if sub and sub.tier_key and sub.status in _ACTIVE_STATUSES:
        tier = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.key == sub.tier_key)
            .first()
        )
    elif sub is None or sub.status not in _ACTIVE_STATUSES:
        tier = _load_fallback_tier(s)

    if tier is None:
        return default_entitlements()

    stored = parse_stored_entitlements(tier.entitlements)
    return resolve_tier_entitlements(stored)
