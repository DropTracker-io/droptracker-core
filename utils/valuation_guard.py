"""Sanity guards for client-supplied drop quantities and totals (stdlib only)."""
from __future__ import annotations

from typing import Optional

# Ceiling for one drop's total value (unit price x quantity). This used to be the
# 32-bit GE cap (2,147,483,647), but prices are 64-bit since RuneLite 1.13 and
# the Obligator (raid 4) trades at 3.3-5b, so every one was rejected as
# implausible (2026-10-21/22). 25b leaves ~5x headroom over the priciest item
# while still catching spoofed quantities (1.2b x 1000 was the real exploit).
# The value columns are BIGINT since web142a, so totals this large store as-is.
MAX_SINGLE_DROP_TOTAL = 25_000_000_000


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
