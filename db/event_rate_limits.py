"""Per-tier event frequency caps — resolution + enforcement (web65a).

Companion to ``db/entitlements.py``: superadmins configure
``web_event_rate_limits`` rows on /admin/event-limits ("tier X may run at most
N events of kind K per rolling D days"; ``type_key`` "*" caps the total across
every kind), and this module is the read side shared by the Web API and the
lifecycle sweep.

Two roles, both resolved from the same rules:

1. **Cap** — at activation (drafts are unlimited, like ``events_max_active``),
   :func:`check_activation_rate_limit` counts the group's events activated
   inside each applicable rule's rolling window and reports the first
   violated rule. Callers turn that into a 409 / readiness blocker.
2. **Grant** — an enabled rule with ``max_events`` > 0 gives a tier
   *rate-limited* event access even when its ``events`` entitlement is off
   (:func:`group_has_rate_limited_events`) — how the free tier can get "an
   event every so often" without unlocking unlimited events.

With no rows configured (the launch baseline) both roles are inert: events
remain gated purely by the ``events`` entitlement, so nothing changes until a
superadmin writes rules.

It lives in ``db/`` (not ``web_api/``) for the same reason as entitlements:
bot/worker processes (the lifecycle sweep) must resolve rules without
importing the Quart app package.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Literal (not imported from db.models.events.EVENT_RATE_LIMIT_ALL_TYPES, which
# must stay in sync) so this module works standalone under the test bootstrap's
# ``db`` stub — the db.entitlements load-by-path pattern.
ALL_TYPES = "*"

# Bounds accepted by the admin CRUD (web_api/routes/admin.py mirrors these in
# its validation errors; the UI mirrors them as input hints).
MAX_EVENTS_CEILING = 1000
WINDOW_DAYS_CEILING = 365

_CACHE_TTL_SECONDS = 60.0
_cache: dict = {"rules": None, "ts": 0.0}


def invalidate_cache() -> None:
    _cache["rules"] = None
    _cache["ts"] = 0.0


def _load_rules(s) -> Dict[str, Dict[str, dict]]:
    """{tier_key: {type_key: {"max_events", "window_days"}}} — enabled rows only."""
    from db.models import EventRateLimit

    rules: Dict[str, Dict[str, dict]] = {}
    rows = (
        s.query(EventRateLimit)
        .filter(EventRateLimit.enabled == True)  # noqa: E712
        .all()
    )
    for row in rows:
        rules.setdefault(row.tier_key, {})[row.type_key] = {
            "max_events": int(row.max_events),
            "window_days": int(row.window_days),
        }
    return rules


def all_rules(s, *, fresh: bool = False) -> Dict[str, Dict[str, dict]]:
    """Every enabled rule, TTL-cached in-process (the event_types pattern)."""
    now = time.monotonic()
    if not fresh and _cache["rules"] is not None and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["rules"]
    rules = _load_rules(s)
    _cache["rules"] = rules
    _cache["ts"] = now
    return rules


def rules_for_tier(s, tier_key: Optional[str]) -> Dict[str, dict]:
    if not tier_key:
        return {}
    return all_rules(s).get(tier_key, {})


def effective_tier_key(s, group_id: int) -> Optional[str]:
    """The tier key a group's rules resolve against: its subscription pool's
    effective tier, else the implicit free/basic fallback, else None."""
    from db.entitlements import _load_fallback_tier, effective_group_subscription

    tier = effective_group_subscription(s, group_id)["tier"] or _load_fallback_tier(s)
    return tier.key if tier is not None else None


def group_has_rate_limited_events(s, group_id: Optional[int]) -> bool:
    """True when the group's tier carries at least one enabled rule granting
    more than zero events — the rate-limited access path for tiers whose
    ``events`` entitlement is off. Callers check the entitlement first."""
    if not group_id:
        return False
    try:
        rules = rules_for_tier(s, effective_tier_key(s, group_id))
        return any(r["max_events"] > 0 for r in rules.values())
    except Exception:
        # Fail closed: a resolution error must not unlock event access.
        return False


# --------------------------------------------------------------------------- #
# Pure evaluation (unit-tested without a session)
# --------------------------------------------------------------------------- #
def window_usage(
    stamps: List[datetime], max_events: int, window_days: int, now: datetime
) -> tuple:
    """(used, retry_at) for one rule against a group's activation timestamps.

    ``used`` counts stamps inside the rolling window. ``retry_at`` is when the
    next activation becomes possible: None when a slot is open right now, and
    also None when ``max_events`` is 0 (blocked outright — no amount of
    waiting frees a slot)."""
    window = timedelta(days=window_days)
    in_window = sorted(t for t in stamps if t is not None and t > now - window)
    used = len(in_window)
    if used < max_events:
        return used, None
    if max_events <= 0:
        return used, None
    # A slot opens once enough of the oldest activations age out that the
    # window holds max_events - 1: that happens when in_window[used - max]
    # crosses the window edge.
    return used, in_window[used - max_events] + window


def evaluate_rules(
    tier_rules: Dict[str, dict],
    kind: str,
    stamps_by_scope: Dict[str, List[datetime]],
    now: datetime,
) -> Optional[Dict[str, Any]]:
    """First violated rule for activating one more event of ``kind``, or None.

    ``stamps_by_scope`` maps each rule scope (the kind itself and/or "*") to
    the group's activation timestamps for that scope. The specific-kind rule
    is checked before the all-kinds rule so the report names the tighter
    scope when both are exhausted."""
    for scope in (kind, ALL_TYPES):
        rule = tier_rules.get(scope)
        if not rule:
            continue
        used, retry_at = window_usage(
            stamps_by_scope.get(scope, []),
            rule["max_events"],
            rule["window_days"],
            now,
        )
        if used >= rule["max_events"]:
            return {
                "scope": scope,
                "max_events": rule["max_events"],
                "window_days": rule["window_days"],
                "used": used,
                "retry_at": retry_at,
            }
    return None


# --------------------------------------------------------------------------- #
# DB-backed activation check
# --------------------------------------------------------------------------- #
def _activation_stamps(s, group_id: int, kind: Optional[str], since: datetime,
                       exclude_event_id: Optional[int]) -> List[datetime]:
    from db.models import Event

    q = (
        s.query(Event.activated_at)
        .filter(
            Event.group_id == group_id,
            Event.activated_at.isnot(None),
            Event.activated_at >= since,
        )
    )
    if kind is not None:
        q = q.filter(Event.kind == kind)
    if exclude_event_id is not None:
        q = q.filter(Event.id != exclude_event_id)
    return [row[0] for row in q.all()]


def check_activation_rate_limit(s, event, now: Optional[datetime] = None) -> Optional[dict]:
    """Would activating ``event`` right now break its group's tier rules?

    Returns the :func:`evaluate_rules` violation dict (or None). Global
    events (no group) are never limited. Counts only events that actually ran
    (``activated_at`` set) — deleted events fall out of the count, which is
    acceptable: deleting an ended event destroys its whole history just to
    free a slot."""
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return None
    # Fail OPEN: a resolution error must not block activations — the cap is a
    # business rule, not an access gate. (Access stays fail-closed via
    # group_has_rate_limited_events / the events entitlement.)
    try:
        rules = rules_for_tier(s, effective_tier_key(s, group_id))
        if not rules:
            return None
        now = now or datetime.now()
        kind = getattr(event, "kind", None) or "standard"

        stamps_by_scope: Dict[str, List[datetime]] = {}
        exclude = getattr(event, "id", None)
        for scope, rule in rules.items():
            if scope not in (kind, ALL_TYPES):
                continue
            since = now - timedelta(days=rule["window_days"])
            stamps_by_scope[scope] = _activation_stamps(
                s, group_id, None if scope == ALL_TYPES else scope, since, exclude
            )
        return evaluate_rules(rules, kind, stamps_by_scope, now)
    except Exception:
        return None


def describe_violation(violation: dict, kind_labels: Optional[Dict[str, str]] = None) -> str:
    """Human sentence for a violation dict, shared by the 409 and the
    readiness blocker."""
    scope = violation["scope"]
    if scope == ALL_TYPES:
        what = "event(s)"
    else:
        label = (kind_labels or {}).get(scope, scope.replace("_", " "))
        what = f"{label} event(s)"
    msg = (
        f"This group's subscription tier allows {violation['max_events']} "
        f"{what} per {violation['window_days']} days and it has already run "
        f"{violation['used']} in that window."
    )
    retry_at = violation.get("retry_at")
    if retry_at is not None:
        msg += f" The next slot opens {retry_at:%b %d, %Y at %H:%M} UTC."
    elif violation["max_events"] <= 0:
        msg = (
            f"This group's subscription tier does not currently allow {what} — "
            "upgrade the subscription to run one."
        )
    return msg
