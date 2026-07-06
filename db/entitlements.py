"""Tier entitlement registry + resolution, shared by the Web API and the bots.

This is the single validation authority for machine-readable tier capabilities
stored on ``subscription_tiers.entitlements`` (TS parity:
``packages/api-types/src/entitlements.ts`` in the web repo). Marketing
``features[]`` is display-only; this registry drives runtime access control.

It lives in ``db/`` (not ``web_api/``) so bot processes can resolve
entitlements without importing the Quart app package.
``web_api/entitlements_registry.py`` re-exports everything here.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

ENTITLEMENT_FIELDS: List[Dict[str, Any]] = [
    {
        "key": "events",
        "label": "Events",
        "category": "features",
        "help": "Create and manage group events (tasks, teams, scoreboards).",
        "default": False,
    },
    {
        # Concurrency limit enforced at event activation (events-prd.md D9).
        "key": "events_max_active",
        "label": "Max active events",
        "category": "features",
        "help": "How many events a group may have active at the same time (enforced at activation; drafts are unlimited).",
        "kind": "int",
        "default": 1,
    },
    {
        "key": "hall_of_fame",
        "label": "Hall of Fame",
        "category": "features",
        "help": "Hall of Fame personal-best embeds and boss leaderboards in Discord.",
        "default": False,
    },
    {
        "key": "custom_embeds",
        "label": "Custom Discord embeds",
        "category": "features",
        "help": "Customize the Discord embeds the bot posts for drops, collection logs, personal bests, combat achievements, pets and the lootboard.",
        "default": False,
    },
]

_BY_KEY = {f["key"]: f for f in ENTITLEMENT_FIELDS}

HALL_OF_FAME_CONFIG_KEYS = frozenset({
    "personal_best_embed_boss_list",
    "hof_individual_boss_messages",
})


def all_entitlement_keys() -> List[str]:
    return [f["key"] for f in ENTITLEMENT_FIELDS]


def get_entitlement_field(key: str) -> Optional[Dict[str, Any]]:
    return _BY_KEY.get(key)


class EntitlementValidationError(ValueError):
    def __init__(self, key: str, detail: str):
        super().__init__(detail)
        self.key = key
        self.detail = detail


def _validate_value(key: str, value: Any) -> Any:
    """Coerce/validate one entitlement value against its field kind."""
    kind = _BY_KEY[key].get("kind", "bool")
    if kind == "int":
        # bool is an int subclass in Python — reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EntitlementValidationError(key, f"'{key}' must be a non-negative integer.")
        return value
    if not isinstance(value, bool):
        raise EntitlementValidationError(key, f"'{key}' must be a boolean.")
    return value


def parse_stored_entitlements(raw: Optional[str]) -> Dict[str, Any]:
    """Parse the JSON column; returns {} when unset/invalid."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in _BY_KEY:
            continue
        out[key] = _validate_value(key, value)
    return out


def validate_entitlements_input(body: dict) -> Dict[str, Any]:
    """Validate a partial entitlement map from tier CRUD. Unknown keys rejected."""
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise EntitlementValidationError("", "Entitlements must be an object.")
    out: Dict[str, Any] = {}
    for key, value in body.items():
        if get_entitlement_field(key) is None:
            raise EntitlementValidationError(key, f"Unknown entitlement '{key}'.")
        out[key] = _validate_value(key, value)
    return out


def entitlements_to_storage(data: Dict[str, bool]) -> str:
    return json.dumps(data)


def default_entitlements() -> Dict[str, Any]:
    """Restrictive baseline when no tier applies (unsubscribed / unknown tier)."""
    return {f["key"]: f["default"] for f in ENTITLEMENT_FIELDS}


def resolve_tier_entitlements(stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve a tier's stored map to a full entitlement dict.

    An empty stored map uses registry defaults (usually ``false``). Superadmins
    configure explicit values on ``/admin/tiers``; unset capabilities keep
    their registry default.
    """
    stored = stored or {}
    return {f["key"]: stored.get(f["key"], f["default"]) for f in ENTITLEMENT_FIELDS}


def all_entitlements_granted() -> Dict[str, Any]:
    """Superadmin bypass: every capability on; numeric limits effectively unbounded."""
    out: Dict[str, Any] = {}
    for f in ENTITLEMENT_FIELDS:
        out[f["key"]] = 1_000_000 if f.get("kind") == "int" else True
    return out


# --------------------------------------------------------------------------- #
# Subscription → entitlement resolution (pure DB, no request context)
# --------------------------------------------------------------------------- #
_ACTIVE_STATUSES = frozenset({"active", "trialing"})
# Implicit free-plan tier keys, tried in order when a group has no active paid sub.
_FALLBACK_TIER_KEYS = ("free", "basic")
# PayPal/manual subs have no provider webhook flipping status when a period
# lapses (Stripe does), so an "active" row past its period end must stop
# granting benefits. The grace window absorbs late IPN deliveries/retries.
_PERIOD_END_GRACE = timedelta(hours=72)


def subscription_is_live(sub) -> bool:
    """Active status AND (no period end, or within period + grace)."""
    if sub is None or sub.status not in _ACTIVE_STATUSES:
        return False
    if sub.current_period_end is None:
        return True
    return datetime.now() <= sub.current_period_end + _PERIOD_END_GRACE


def _load_fallback_tier(s):
    from db.models.subscriptions import SubscriptionTier

    for key in _FALLBACK_TIER_KEYS:
        tier = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.key == key, SubscriptionTier.active == True)  # noqa: E712
            .first()
        )
        if tier:
            return tier
    return None


def resolve_group_entitlements(s, group_id: int) -> Dict[str, Any]:
    """Resolved entitlement map for ``group_id`` from its subscription tier.

    No superadmin handling here — that's a web-request concern
    (``web_api/entitlements.py`` wraps this with the bypass).
    """
    from db.models.subscriptions import GroupSubscription, SubscriptionTier

    sub = (
        s.query(GroupSubscription)
        .filter(GroupSubscription.group_id == group_id)
        .first()
    )
    tier = None
    if sub is not None and sub.tier_key and subscription_is_live(sub):
        tier = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.key == sub.tier_key)
            .first()
        )
    elif not subscription_is_live(sub):
        tier = _load_fallback_tier(s)

    if tier is None:
        return default_entitlements()

    stored = parse_stored_entitlements(tier.entitlements)
    return resolve_tier_entitlements(stored)


# --------------------------------------------------------------------------- #
# Bot-facing cached checks (notification hot path)
# --------------------------------------------------------------------------- #
_ENTITLEMENT_CACHE_TTL = 60  # seconds
_entitlement_cache: Dict[int, tuple] = {}  # group_id -> (expires_at, entitlement map)

# Transitional: groups whose payment still lives in the legacy XenForo/Patreon
# tables (not yet backfilled into group_subscriptions) keep their custom
# embeds. Flip to "false" (env) once the subscription cutover is complete.
_LEGACY_UPGRADE_FALLBACK = (
    os.getenv("CUSTOM_EMBEDS_LEGACY_FALLBACK", "true").strip().lower() == "true"
)


def invalidate_entitlement_cache(group_id: Optional[int] = None) -> None:
    if group_id is None:
        _entitlement_cache.clear()
    else:
        _entitlement_cache.pop(group_id, None)


def group_entitlements(group_id: int) -> Dict[str, Any]:
    """Cached entitlement map for a group (bot processes)."""
    now = time.monotonic()
    cached = _entitlement_cache.get(group_id)
    if cached and cached[0] > now:
        return cached[1]

    from db.models import Session

    with Session() as session:
        entitlements = resolve_group_entitlements(session, group_id)
    _entitlement_cache[group_id] = (now + _ENTITLEMENT_CACHE_TTL, entitlements)
    return entitlements


def group_has_entitlement(group_id: Optional[int], key: str) -> bool:
    if not group_id:
        return False
    try:
        return bool(group_entitlements(group_id).get(key))
    except Exception:
        # Fail closed: a resolution error must not unlock premium behavior.
        return False


_legacy_upgrade_cache: Dict[int, tuple] = {}  # group_id -> (expires_at, bool)


def _legacy_upgrade_active(group_id: int) -> bool:
    """Cached legacy XenForo/Patreon upgrade check (hits the XF database)."""
    now = time.monotonic()
    cached = _legacy_upgrade_cache.get(group_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        from db.xf.upgrades import check_active_upgrade

        active = bool(check_active_upgrade(group_id))
    except Exception:
        active = False
    _legacy_upgrade_cache[group_id] = (now + _ENTITLEMENT_CACHE_TTL, active)
    return active


def has_custom_embeds(group_id: Optional[int]) -> bool:
    """Whether a group may use its own custom Discord embeds.

    Drop-in replacement for the old ``check_active_upgrade(group_id)`` gate in
    the notification senders: True → use the group's own ``group_embeds`` rows,
    False → use the template group (id 1) defaults.
    """
    if not group_id:
        return False
    if group_has_entitlement(group_id, "custom_embeds"):
        return True
    if _LEGACY_UPGRADE_FALLBACK:
        return _legacy_upgrade_active(group_id)
    return False
