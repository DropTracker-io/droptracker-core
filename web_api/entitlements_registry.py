"""Tier entitlement registry (Task 15) — Python parity with api-types entitlements.ts.

Single validation authority for machine-readable tier capabilities stored on
``subscription_tiers.entitlements``. Marketing ``features[]`` is display-only;
this registry drives runtime access control.
"""
from __future__ import annotations

import json
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
