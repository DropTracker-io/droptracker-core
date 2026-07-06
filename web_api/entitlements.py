"""Resolve a group's effective entitlements from its subscription tier.

The pure DB resolution lives in ``db/entitlements.py`` (shared with the bot
processes); this wrapper adds the web-request superadmin bypass.
"""
from __future__ import annotations

from db.entitlements import (  # noqa: F401  (re-exported for existing importers)
    all_entitlements_granted,
    resolve_group_entitlements as _resolve_group_entitlements,
    subscription_is_live,
)
from web_api.deps import is_superadmin


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
    return _resolve_group_entitlements(s, group_id)
