"""Sanity guards for client-supplied drop quantities and totals (stdlib only)."""
from __future__ import annotations

from typing import Optional

# OSRS Grand Exchange integer cap: no legitimate single drop's total value
# exceeds this. Valuable uniques drop at quantity 1; large stacks are low-value.
MAX_SINGLE_DROP_TOTAL = 2_147_483_647


def sanitize_quantity(raw) -> Optional[int]:
    """Coerce a submitted quantity to a positive int, or None if invalid."""
    try:
        quantity = int(raw)
    except (TypeError, ValueError):
        return None
    return quantity if quantity > 0 else None


def exceeds_value_sanity_limit(unit_value, quantity) -> bool:
    """True when unit_value * quantity exceeds the limit (fail closed on junk)."""
    try:
        return int(unit_value) * int(quantity) > MAX_SINGLE_DROP_TOTAL
    except (TypeError, ValueError):
        return True
