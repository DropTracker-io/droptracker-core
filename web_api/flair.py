"""Resolve per-group subscription flair for public listings.

Flair is the cosmetic tier style shown wherever a group name appears on the
website (see ``web_api/tier_flair.py``). Subscription-pool model: a group's
effective tier comes from the sum of its live contribution legs
(``db/entitlements.py effective_group_tiers``), so flair reflects exactly the
tier the group's live payments cover — including the period-end grace that
plain status checks miss. Groups whose pool covers no flaired tier are simply
absent from the map.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from db.entitlements import effective_group_tiers
from web_api.tier_flair import normalize_flair


def group_flairs(session, group_ids: Iterable[int]) -> Dict[int, dict]:
    """``{group_id: {"tier_key","tier_name","style"}}`` for the flaired subset."""
    out: Dict[int, dict] = {}
    for gid, (tier, _total) in effective_group_tiers(session, group_ids).items():
        style = normalize_flair(getattr(tier, "flair", None))
        if style == "none":
            continue
        out[gid] = {"tier_key": tier.key, "tier_name": tier.name, "style": style}
    return out


def group_flair(session, group_id: int) -> Optional[dict]:
    """Single-group convenience wrapper around :func:`group_flairs`."""
    return group_flairs(session, [group_id]).get(int(group_id))
