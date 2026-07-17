"""Regression tests for the quantity / total-value sanity guards."""
from utils.valuation_guard import (
    MAX_SINGLE_DROP_TOTAL,
    sanitize_quantity,
    exceeds_value_sanity_limit,
)


def test_sanitize_quantity_accepts_positive_ints():
    assert sanitize_quantity(1) == 1
    assert sanitize_quantity("1000") == 1000
    assert sanitize_quantity(65_535) == 65_535


def test_sanitize_quantity_rejects_non_positive_and_junk():
    assert sanitize_quantity(0) is None
    assert sanitize_quantity(-5) is None
    assert sanitize_quantity("-5") is None
    assert sanitize_quantity("abc") is None
    assert sanitize_quantity(None) is None


def test_large_but_realistic_stack_is_within_limit():
    assert exceeds_value_sanity_limit(unit_value=1, quantity=1_000_000) is False
    assert exceeds_value_sanity_limit(unit_value=1_200_000_000, quantity=1) is False


def test_top_tier_single_item_within_limit():
    # 3rd age-tier item (GE integer ceiling) at quantity 1.
    assert exceeds_value_sanity_limit(unit_value=2_147_483_647, quantity=1) is False


def test_total_above_top_tier_single_item_is_flagged():
    assert exceeds_value_sanity_limit(unit_value=5_000_000_000, quantity=1) is True


def test_absurd_total_is_flagged():
    # 1.2B x 1000 = the demonstrated exploit.
    assert exceeds_value_sanity_limit(unit_value=1_200_000_000, quantity=1000) is True


def test_limit_boundary():
    assert exceeds_value_sanity_limit(MAX_SINGLE_DROP_TOTAL, 1) is False
    assert exceeds_value_sanity_limit(MAX_SINGLE_DROP_TOTAL + 1, 1) is True
