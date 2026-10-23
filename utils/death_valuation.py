"""What a death cost: the GE value of the items the player failed to protect.

A port of the plugin's ``DeathValuation.java`` (itself after Dink's
``DeathNotifier#splitItemsByKept``), for clients from 6.2 that send what they
carried instead of the totals. The client still prices every item (RuneLite's
GE price, else the store price), so the numbers match what it used to send.

Inventory and worn equipment only. Keep slots go to the most valuable items
first, per item not per stack; a bond always comes back and never takes a slot.
Treat the result as a good estimate, not an audit (see the Java docs).
"""
from __future__ import annotations

UNSKULLED_KEEP_COUNT = 3
ULTIMATE_IRONMAN = 2
BONDS = frozenset({13190, 13191, 13192})
MAX_ITEMS = 60


def parse_items(*fields) -> list:
    """``"id:qty:price,..."`` fields → ``[(id, qty, price)]`` in the order sent.
    Malformed or empty entries are skipped."""
    items = []
    for field in fields:
        for entry in str(field or "").split(","):
            parts = entry.strip().split(":")
            if len(parts) != 3:
                continue
            try:
                item_id, qty, price = (int(p) for p in parts)
            except ValueError:
                continue
            if item_id >= 0 and qty > 0:
                items.append((item_id, qty, max(price, 0)))
            if len(items) >= MAX_ITEMS:
                return items
    return items


def keep_count(account_type=None, skulled=False, protect_item=False) -> int:
    try:
        if int(account_type) == ULTIMATE_IRONMAN:
            return 0
    except (TypeError, ValueError):
        pass
    return (0 if skulled else UNSKULLED_KEEP_COUNT) + (1 if protect_item else 0)


def value(items, safe, keep) -> tuple:
    """``(lost, kept, lost_stacks)``. A safe death keeps everything."""
    # Most valuable unit first; stable, so equal prices keep the order sent
    # (inventory, then worn), exactly like the client's List.sort.
    ordered = sorted(items, key=lambda item: -item[2])
    if safe:
        return 0, sum(price * qty for _, qty, price in ordered), 0
    lost = kept = lost_stacks = used = 0
    for item_id, qty, price in ordered:
        if item_id in BONDS:
            kept += price * qty
            continue
        protected = min(qty, max(0, keep - used))
        used += protected
        kept += price * protected
        if qty - protected > 0:
            lost += price * (qty - protected)
            lost_stacks += 1
    return lost, kept, lost_stacks
