"""Public listing of post-submission item valuations (the /item-values page).

Some items are dropped with a 0gp in-game value but are re-valued after
submission because they're a *component* of a tradeable item (e.g. a bludgeon
axon is worth 1/3 of an Abyssal bludgeon). This endpoint explains every such
rule with live numbers, driven straight from the ``item_value_overrides`` table
so it never drifts from the valuation the backend actually applies.

Public read, cached ~120s (the underlying rules change rarely and each render
prices ~a dozen distinct GE items).
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from web_api.common import cache_get, cache_set, db_session, money, with_cache_headers
from web_api.item_value_enrich import enrich_overrides
from utils import value_overrides

item_values_bp = Blueprint("v1_item_values", __name__)

_CACHE_KEY = "public:item_values"
_CACHE_TTL = 120.0

# Unicode minus / multiplication so the formula reads naturally on the page.
_MINUS = "−"
_TIMES = "×"


def _formula_text(override: dict) -> str:
    """Human-readable formula, e.g. ``Abyssal bludgeon ÷ 3`` or
    ``Ultor ring − 3 × Chromium ingot, or 5,000,000 gp if a price is unavailable``."""
    terms = []
    for i, c in enumerate(override.get("components") or []):
        qty = c.get("quantity") or 0
        name = (c.get("item_name") or "").strip() or (
            f"Item {c.get('item_id')}" if c.get("item_id") is not None else "unknown item"
        )
        magnitude = abs(qty)
        piece = name if magnitude == 1 else f"{magnitude} {_TIMES} {name}"
        if i == 0:
            terms.append(piece if qty >= 0 else f"{_MINUS}{piece}")
        else:
            terms.append(f"{_MINUS if qty < 0 else '+'} {piece}")

    flat = override.get("flat_bonus") or 0
    expr = " ".join(terms)
    if flat:
        sign = _MINUS if flat < 0 else "+"
        expr = f"{expr} {sign} {abs(flat):,}" if expr else f"{flat:,}"
    if not expr:
        expr = "0"

    divisor = override.get("divisor") or 1
    if divisor != 1:
        needs_parens = len(terms) > 1 or bool(flat)
        expr = f"({expr}) ÷ {divisor}" if needs_parens else f"{expr} ÷ {divisor}"

    fallback = override.get("fallback_value") or 0
    if fallback:
        expr += f", or {fallback:,} gp if a price is unavailable"
    return expr


@item_values_bp.get("/item-values")
async def list_item_values():
    cached = cache_get(_CACHE_KEY, _CACHE_TTL)
    if cached is not None:
        return with_cache_headers(jsonify(cached), max_age=120)

    def _load():
        overrides = value_overrides.all_active()
        with db_session() as s:
            enrich_overrides(s, overrides)
        return overrides

    overrides = await asyncio.to_thread(_load)

    from utils.ge_value import build_component_price_map

    try:
        price_map = await build_component_price_map(overrides)
    except Exception:
        price_map = {}

    items = []
    for ov in overrides:
        computed = value_overrides.compute_override_from_prices(ov, price_map) if price_map else None
        priced = computed is not None
        if not priced:
            computed = ov.get("fallback_value") or 0
        items.append({
            "item_id": ov.get("item_id"),
            "item_name": ov.get("item_name"),
            "icon_url": ov.get("icon_url"),
            "description": ov.get("description"),
            "formula": _formula_text(ov),
            "components": ov.get("components"),
            "value": money(computed),
            "priced": priced,
        })

    items.sort(key=lambda x: (x["item_name"] or "").lower())
    cache_set(_CACHE_KEY, items)
    return with_cache_headers(jsonify(items), max_age=120)
