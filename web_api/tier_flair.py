"""Tier "flair" vocabulary — Python parity with api-types ``tier-flair.ts``.

Flair is the cosmetic display style a subscription tier grants to a group
wherever its name appears on the website (leaderboards, group profile, search,
memberships). Distinct from entitlements (runtime access control) — flair is
purely prestige. Superadmins pick a style per tier; the frontend maps the style
id to colors/glow/shimmer. Only web_api serialization uses this.
"""
from __future__ import annotations

# Ordered least -> most prestigious. Must stay in sync with TIER_FLAIR_IDS in
# packages/api-types/src/tier-flair.ts.
TIER_FLAIR_STYLES = ("none", "bronze", "gold", "amethyst", "dragon")
DEFAULT_TIER_FLAIR = "none"


class FlairValidationError(ValueError):
    """Raised by ``validate_flair`` for an unknown style (mirrors
    ``EntitlementValidationError`` so admin routes can surface ``.detail``)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def validate_flair(value) -> str:
    """Coerce a flair value from tier CRUD to a known style.

    ``None``/empty -> the default ("none"); unknown values are rejected.
    """
    if value is None or value == "":
        return DEFAULT_TIER_FLAIR
    if not isinstance(value, str) or value not in TIER_FLAIR_STYLES:
        raise FlairValidationError(
            "'flair' must be one of: " + ", ".join(TIER_FLAIR_STYLES) + "."
        )
    return value


def normalize_flair(value) -> str:
    """Read a stored/loaded flair value, defaulting unknowns to 'none'."""
    return value if value in TIER_FLAIR_STYLES else DEFAULT_TIER_FLAIR
