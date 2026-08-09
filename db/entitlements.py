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
    {
        "key": "video_submissions",
        "label": "Video submissions",
        "category": "features",
        "help": "Members can capture and upload short video clips of drops, personal bests and other achievements instead of screenshots.",
        "default": False,
    },
    {
        "key": "custom_points",
        "label": "Custom points system",
        "category": "features",
        "help": "Configure a custom point system: award rules per submission type, per-item/NPC overrides, timed boosts, and points leaderboards.",
        "default": False,
    },
    {
        "key": "custom_site",
        "label": "Custom clan website",
        "category": "features",
        "help": "A multi-page mini-site for the group on its own subdomain of the sites domain, built from blocks with live DropTracker data.",
        "default": False,
    },
]

# User-scoped ("supporter") entitlements — granted by a user_subscriptions row
# to a tier with scope="user". Personal perks, independent of group tiers.
USER_ENTITLEMENT_FIELDS: List[Dict[str, Any]] = [
    {
        "key": "dm_submissions",
        "label": "Submission DMs",
        "category": "supporter",
        "help": "Receive Discord DMs for your own drops, personal bests, collection log slots and other achievements, filtered by your own settings.",
        "default": False,
    },
    {
        "key": "supporter_flair",
        "label": "Supporter flair",
        "category": "supporter",
        "help": "A distinct supporter display style on your public profile and site listings.",
        "default": False,
    },
    {
        "key": "video_submissions",
        "label": "Personal video submissions",
        "category": "supporter",
        "help": "Capture and upload short video clips of your own submissions, independent of whether any of your groups has video submissions enabled.",
        "default": False,
    },
]

_BY_KEY = {f["key"]: f for f in ENTITLEMENT_FIELDS}
_USER_BY_KEY = {f["key"]: f for f in USER_ENTITLEMENT_FIELDS}

TIER_SCOPES = ("group", "user")

# Group-config keys that require the hall_of_fame entitlement to edit. This is
# the whole Hall of Fame surface — the master on/off switch (create_pb_embeds)
# and everything that configures it. notify_pbs is deliberately NOT here: plain
# PB Discord notifications stay available to every group, premium or not.
HALL_OF_FAME_CONFIG_KEYS = frozenset({
    "create_pb_embeds",
    "personal_best_embed_boss_list",
    "number_of_pbs_to_display",
    "channel_id_to_send_pb_embeds",
    "hof_individual_boss_messages",
})


def _registry(scope: str) -> tuple:
    if scope == "user":
        return USER_ENTITLEMENT_FIELDS, _USER_BY_KEY
    return ENTITLEMENT_FIELDS, _BY_KEY


def all_entitlement_keys(scope: str = "group") -> List[str]:
    fields, _ = _registry(scope)
    return [f["key"] for f in fields]


def get_entitlement_field(key: str, scope: str = "group") -> Optional[Dict[str, Any]]:
    _, by_key = _registry(scope)
    return by_key.get(key)


class EntitlementValidationError(ValueError):
    def __init__(self, key: str, detail: str):
        super().__init__(detail)
        self.key = key
        self.detail = detail


def _validate_value(key: str, value: Any, scope: str = "group") -> Any:
    """Coerce/validate one entitlement value against its field kind."""
    _, by_key = _registry(scope)
    kind = by_key[key].get("kind", "bool")
    if kind == "int":
        # bool is an int subclass in Python — reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EntitlementValidationError(key, f"'{key}' must be a non-negative integer.")
        return value
    if not isinstance(value, bool):
        raise EntitlementValidationError(key, f"'{key}' must be a boolean.")
    return value


def parse_stored_entitlements(raw: Optional[str], scope: str = "group") -> Dict[str, Any]:
    """Parse the JSON column; returns {} when unset/invalid."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    _, by_key = _registry(scope)
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in by_key:
            continue
        out[key] = _validate_value(key, value, scope)
    return out


def validate_entitlements_input(body: dict, scope: str = "group") -> Dict[str, Any]:
    """Validate a partial entitlement map from tier CRUD. Unknown keys rejected."""
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise EntitlementValidationError("", "Entitlements must be an object.")
    out: Dict[str, Any] = {}
    for key, value in body.items():
        if get_entitlement_field(key, scope) is None:
            raise EntitlementValidationError(key, f"Unknown entitlement '{key}'.")
        out[key] = _validate_value(key, value, scope)
    return out


def entitlements_to_storage(data: Dict[str, bool]) -> str:
    return json.dumps(data)


def default_entitlements(scope: str = "group") -> Dict[str, Any]:
    """Restrictive baseline when no tier applies (unsubscribed / unknown tier)."""
    fields, _ = _registry(scope)
    return {f["key"]: f["default"] for f in fields}


def resolve_tier_entitlements(stored: Optional[Dict[str, Any]], scope: str = "group") -> Dict[str, Any]:
    """Resolve a tier's stored map to a full entitlement dict.

    An empty stored map uses registry defaults (usually ``false``). Superadmins
    configure explicit values on ``/admin/tiers``; unset capabilities keep
    their registry default.
    """
    stored = stored or {}
    fields, _ = _registry(scope)
    return {f["key"]: stored.get(f["key"], f["default"]) for f in fields}


def all_entitlements_granted(scope: str = "group") -> Dict[str, Any]:
    """Superadmin bypass: every capability on; numeric limits effectively unbounded."""
    fields, _ = _registry(scope)
    out: Dict[str, Any] = {}
    for f in fields:
        out[f["key"]] = 1_000_000 if f.get("kind") == "int" else True
    return out


# --------------------------------------------------------------------------- #
# Subscription → entitlement resolution (pure DB, no request context)
# --------------------------------------------------------------------------- #
_ACTIVE_STATUSES = frozenset({"active", "trialing"})
# Implicit free-plan tier keys, tried in order when a group has no active paid sub.
_FALLBACK_TIER_KEYS = ("free", "basic")

# Provider of a group leg synthesized from Discord Nitro boosts placed on the
# main server by a group's members (see services/nitro_attribution.py). It
# grants pool credit / entitlements but is NOT paid recurring revenue.
NITRO_PROVIDER = "nitro"
# Leg providers that grant entitlements but must be excluded from every revenue
# figure (MRR, lifetime, tier distribution): comped grants and boost credit.
NON_REVENUE_PROVIDERS = frozenset({"manual", NITRO_PROVIDER})
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


# --------------------------------------------------------------------------- #
# Subscription pool: a group holds many contribution "legs" (multi-payer);
# its effective tier is the most expensive tier covered by the sum of the
# live legs' monthly amounts.
# --------------------------------------------------------------------------- #
def paid_group_tiers_desc(s):
    """Active, paid, group-scope tiers sorted most-expensive first."""
    from db.models.subscriptions import SubscriptionTier

    return (
        s.query(SubscriptionTier)
        .filter(
            SubscriptionTier.active == True,  # noqa: E712
            SubscriptionTier.scope == "group",
            SubscriptionTier.price_cents > 0,
        )
        .order_by(SubscriptionTier.price_cents.desc())
        .all()
    )


def leg_monthly_cents(leg, tiers_by_key: Dict[str, Any]) -> int:
    """One leg's monthly-normalized contribution. Legacy rows (NULL amount)
    contribute their tier's full price; year-interval legs are divided out."""
    tier = tiers_by_key.get(leg.tier_key) if leg.tier_key else None
    amount = leg.amount_cents
    if amount is None:
        amount = int(tier.price_cents) if tier else 0
    interval = ((tier.interval if tier else None) or "month")
    return int(amount) // 12 if interval == "year" else int(amount)


def pool_tier_for_total(tiers_desc, total_cents: int):
    """Most expensive tier whose price the pool covers, or None."""
    for tier in tiers_desc:
        if int(tier.price_cents) <= total_cents:
            return tier
    return None


def effective_group_subscription(s, group_id: int) -> Dict[str, Any]:
    """Pool resolution for one group.

    Returns ``{tier, total_monthly_cents, legs, live_legs, status,
    current_period_end, cancel_at_period_end}``. ``tier`` is None when the
    live pool covers no paid tier. Effective status: active if any live leg;
    else past_due if any leg is past_due; else canceled/expired by presence;
    none without legs. ``current_period_end`` is the soonest live-leg end (the
    next moment the pool could shrink); ``cancel_at_period_end`` is True only
    when every live leg is winding down.
    """
    from db.models.subscriptions import GroupSubscription

    legs = (
        s.query(GroupSubscription)
        .filter(GroupSubscription.group_id == group_id)
        .order_by(GroupSubscription.created_at.asc())
        .all()
    )
    tiers_desc = paid_group_tiers_desc(s)
    tiers_by_key = {t.key: t for t in tiers_desc}

    live = [leg for leg in legs if subscription_is_live(leg)]
    total = sum(leg_monthly_cents(leg, tiers_by_key) for leg in live)
    tier = pool_tier_for_total(tiers_desc, total) if live else None

    if live:
        status = "active"
    elif any(leg.status == "past_due" for leg in legs):
        status = "past_due"
    elif any(leg.status == "canceled" for leg in legs):
        status = "canceled"
    elif any(leg.status == "expired" for leg in legs):
        status = "expired"
    else:
        status = "none"

    live_ends = [leg.current_period_end for leg in live if leg.current_period_end]
    return {
        "tier": tier,
        "total_monthly_cents": total,
        "legs": legs,
        "live_legs": live,
        "status": status,
        "current_period_end": min(live_ends) if live_ends else None,
        "cancel_at_period_end": bool(live) and all(leg.cancel_at_period_end for leg in live),
    }


def effective_group_tiers(s, group_ids) -> Dict[int, Any]:
    """Bulk pool resolution: ``{group_id: (tier, total_monthly_cents)}`` for
    the subset of ``group_ids`` whose live pool covers a paid tier. Two
    queries total — used on hot listing paths (flair)."""
    from db.models.subscriptions import GroupSubscription

    ids = [int(g) for g in group_ids if g is not None]
    if not ids:
        return {}
    legs = (
        s.query(GroupSubscription)
        .filter(
            GroupSubscription.group_id.in_(ids),
            GroupSubscription.status.in_(tuple(_ACTIVE_STATUSES)),
        )
        .all()
    )
    tiers_desc = paid_group_tiers_desc(s)
    tiers_by_key = {t.key: t for t in tiers_desc}

    totals: Dict[int, int] = {}
    for leg in legs:
        if not subscription_is_live(leg):
            continue
        totals[int(leg.group_id)] = totals.get(int(leg.group_id), 0) + leg_monthly_cents(
            leg, tiers_by_key
        )

    out: Dict[int, Any] = {}
    for gid, total in totals.items():
        tier = pool_tier_for_total(tiers_desc, total)
        if tier is not None:
            out[gid] = (tier, total)
    return out


def resolve_group_entitlements(s, group_id: int) -> Dict[str, Any]:
    """Resolved entitlement map for ``group_id`` from its subscription pool.

    No superadmin handling here — that's a web-request concern
    (``web_api/entitlements.py`` wraps this with the bypass).
    """
    resolved = effective_group_subscription(s, group_id)
    tier = resolved["tier"]
    if tier is None:
        # Live payments below the cheapest paid tier (or none) → free plan.
        tier = _load_fallback_tier(s)
    if tier is None:
        return default_entitlements()

    stored = parse_stored_entitlements(tier.entitlements)
    return resolve_tier_entitlements(stored)


# Badge-linked complimentary grant: users holding an active badge with this
# key (on any of their players) receive the benefits of the tier below WITHOUT
# a subscription row — they can still subscribe normally, and revoking the
# badge removes the benefits at the next resolution (caches expire in ≤60s).
_COMP_BADGE_KEY = os.getenv("SUPPORTER_COMP_BADGE_KEY", "bug_tester_helper").strip()
_COMP_TIER_KEY = os.getenv("SUPPORTER_COMP_TIER_KEY", "supporter").strip()


def _complimentary_badge_entitlements(s, user_id: int) -> Optional[Dict[str, Any]]:
    """Supporter-tier entitlement map when the user holds the comp badge."""
    if not _COMP_BADGE_KEY or not _COMP_TIER_KEY:
        return None
    try:
        from db.models import Player
        from db.models.badge import Badge, PlayerBadge
        from db.models.subscriptions import SubscriptionTier

        held = (
            s.query(PlayerBadge.id)
            .join(Badge, Badge.badge_id == PlayerBadge.badge_id)
            .join(Player, Player.player_id == PlayerBadge.player_id)
            .filter(
                Player.user_id == user_id,
                Badge.key == _COMP_BADGE_KEY,
                Badge.active == True,  # noqa: E712
                PlayerBadge.status == "active",
            )
            .first()
        )
        if held is None:
            return None
        tier = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.key == _COMP_TIER_KEY)
            .first()
        )
        if tier is None:
            return None
        stored = parse_stored_entitlements(tier.entitlements, "user")
        return resolve_tier_entitlements(stored, "user")
    except Exception:
        # Fail closed: a lookup error must not unlock (or crash resolution of)
        # premium behavior.
        return None


def resolve_user_entitlements(s, user_id: int) -> Dict[str, Any]:
    """Resolved supporter entitlement map for ``user_id``.

    Sources, merged permissively (a capability granted by either is granted):
      1. A live user_subscriptions row → its tier's entitlements.
      2. The complimentary badge grant (active Bug Tester badge).

    No fallback tier: users with neither get the restrictive user-scope
    defaults (everything off).
    """
    from db.models.subscriptions import SubscriptionTier, UserSubscription

    sub = (
        s.query(UserSubscription)
        .filter(UserSubscription.user_id == user_id)
        .first()
    )
    tier = None
    if sub is not None and sub.tier_key and subscription_is_live(sub):
        tier = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.key == sub.tier_key)
            .first()
        )
    if tier is None:
        base = default_entitlements("user")
    else:
        stored = parse_stored_entitlements(tier.entitlements, "user")
        base = resolve_tier_entitlements(stored, "user")

    grant = _complimentary_badge_entitlements(s, user_id)
    if grant:
        for key, value in grant.items():
            current = base.get(key)
            if isinstance(value, bool):
                base[key] = bool(current) or value
            else:
                base[key] = max(int(current or 0), int(value))
    return base


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


_user_entitlement_cache: Dict[int, tuple] = {}  # user_id -> (expires_at, entitlement map)


def invalidate_user_entitlement_cache(user_id: Optional[int] = None) -> None:
    if user_id is None:
        _user_entitlement_cache.clear()
    else:
        _user_entitlement_cache.pop(user_id, None)


def user_entitlements(user_id: int) -> Dict[str, Any]:
    """Cached supporter entitlement map for a user (bot processes)."""
    now = time.monotonic()
    cached = _user_entitlement_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]

    from db.models import Session

    with Session() as session:
        entitlements = resolve_user_entitlements(session, user_id)
    _user_entitlement_cache[user_id] = (now + _ENTITLEMENT_CACHE_TTL, entitlements)
    return entitlements


def user_has_entitlement(user_id: Optional[int], key: str) -> bool:
    if user_id is None:
        return False
    try:
        return bool(user_entitlements(user_id).get(key))
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
