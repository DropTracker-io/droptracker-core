"""Tier entitlement registry (Task 15) — Python parity with api-types entitlements.ts.

The registry itself now lives in ``db/entitlements.py`` so bot processes can
resolve entitlements without importing the Quart app package. This module
re-exports it to keep the established ``web_api.entitlements_registry`` import
path working.
"""
from __future__ import annotations

from db.entitlements import (  # noqa: F401
    ENTITLEMENT_FIELDS,
    HALL_OF_FAME_CONFIG_KEYS,
    TIER_SCOPES,
    USER_ENTITLEMENT_FIELDS,
    EntitlementValidationError,
    all_entitlement_keys,
    all_entitlements_granted,
    default_entitlements,
    entitlements_to_storage,
    get_entitlement_field,
    parse_stored_entitlements,
    resolve_tier_entitlements,
    validate_entitlements_input,
)

__all__ = [
    "ENTITLEMENT_FIELDS",
    "HALL_OF_FAME_CONFIG_KEYS",
    "TIER_SCOPES",
    "USER_ENTITLEMENT_FIELDS",
    "EntitlementValidationError",
    "all_entitlement_keys",
    "all_entitlements_granted",
    "default_entitlements",
    "entitlements_to_storage",
    "get_entitlement_field",
    "parse_stored_entitlements",
    "resolve_tier_entitlements",
    "validate_entitlements_input",
]
