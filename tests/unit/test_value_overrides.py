"""Unit tests for utils/value_overrides.py pure valuation math.

Asserts the table-driven linear formula reproduces the arithmetic of the old
hard-coded rules in utils/ge_value.py exactly, for all six seeded families.
"""
import utils.value_overrides as vo


def _c(name, qty, item_id=None):
    return {"item_id": item_id, "item_name": name, "quantity": qty}


def _ov(components, divisor=1, flat_bonus=0, fallback_value=0):
    return {
        "components": components,
        "divisor": divisor,
        "flat_bonus": flat_bonus,
        "fallback_value": fallback_value,
    }


def _price_map(override, name_prices):
    """Build a {component_price_key: price} map from an {item_name: price} dict."""
    return {vo.component_price_key(c): name_prices.get(c["item_name"]) for c in override["components"]}


def test_bludgeon_piece_is_third_of_bludgeon():
    ov = _ov([_c("Abyssal bludgeon", 1)], divisor=3)
    pm = _price_map(ov, {"Abyssal bludgeon": 10_000_000})
    # old: int(bludgeon_value / 3)
    assert vo.compute_override_from_prices(ov, pm) == int(10_000_000 / 3)


def test_bludgeon_uses_integer_floor():
    ov = _ov([_c("Abyssal bludgeon", 1)], divisor=3)
    pm = _price_map(ov, {"Abyssal bludgeon": 10})
    assert vo.compute_override_from_prices(ov, pm) == 3  # floor(10 / 3)


def test_vestige_is_ring_minus_three_ingots():
    ov = _ov([_c("Ultor ring", 1), _c("Chromium ingot", -3)])
    pm = _price_map(ov, {"Ultor ring": 100_000_000, "Chromium ingot": 30_000_000})
    # old: ring_price - (ingot_price * 3)
    assert vo.compute_override_from_prices(ov, pm) == 100_000_000 - 3 * 30_000_000


def test_hydra_piece_is_third_of_brimstone():
    ov = _ov([_c("Brimstone ring", 1)], divisor=3)
    pm = _price_map(ov, {"Brimstone ring": 4_000_000})
    assert vo.compute_override_from_prices(ov, pm) == int(4_000_000 / 3)


def test_noxious_piece_is_third_of_halberd():
    ov = _ov([_c("Noxious halberd", 1)], divisor=3)
    pm = _price_map(ov, {"Noxious halberd": 500_000_000})
    assert vo.compute_override_from_prices(ov, pm) == int(500_000_000 / 3)


def test_araxyte_fang_is_rancour_minus_torture():
    ov = _ov([_c("Amulet of rancour", 1), _c("Amulet of torture", -1)])
    pm = _price_map(ov, {"Amulet of rancour": 250_000_000, "Amulet of torture": 8_000_000})
    assert vo.compute_override_from_prices(ov, pm) == 250_000_000 - 8_000_000


def test_mokhaiotl_cloth_formula():
    ov = _ov(
        [_c("Confliction gauntlets", 1), _c("Tormented bracelet", -1), _c("Demon tear", -10000)],
        fallback_value=5_000_000,
    )
    pm = _price_map(ov, {
        "Confliction gauntlets": 100_000_000,
        "Tormented bracelet": 8_000_000,
        "Demon tear": 2_000,
    })
    # old: gauntlets - bracelet - (tear * 10000)
    assert vo.compute_override_from_prices(ov, pm) == 100_000_000 - 8_000_000 - 2_000 * 10_000


def test_missing_component_price_signals_fallback():
    # Mokhaiotl with the bracelet price unavailable → None, so the caller applies
    # the 5M fallback (matching the old `else: return 5000000`).
    ov = _ov([_c("Confliction gauntlets", 1), _c("Tormented bracelet", -1)], fallback_value=5_000_000)
    pm = _price_map(ov, {"Confliction gauntlets": 100_000_000})  # bracelet missing
    assert vo.compute_override_from_prices(ov, pm) is None


def test_zero_price_treated_as_unpriced():
    # Old code: `int(x / 3) if x else provided_value` — a 0 price falls back.
    ov = _ov([_c("Abyssal bludgeon", 1)], divisor=3)
    pm = _price_map(ov, {"Abyssal bludgeon": 0})
    assert vo.compute_override_from_prices(ov, pm) is None


def test_component_price_key_prefers_id_then_lower_name():
    assert vo.component_price_key(_c("Abyssal bludgeon", 1, item_id=13280)) == ("id", 13280)
    assert vo.component_price_key(_c("Abyssal Bludgeon", 1)) == ("name", "abyssal bludgeon")


def test_flat_bonus_added_to_numerator():
    ov = _ov([_c("Abyssal bludgeon", 1)], divisor=1, flat_bonus=500)
    pm = _price_map(ov, {"Abyssal bludgeon": 1_000})
    assert vo.compute_override_from_prices(ov, pm) == 1_500
