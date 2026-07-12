"""Subscription-pool math (db/entitlements.py).

Pure-logic tests for the contribution-pool model: a group's effective tier is
the most expensive tier covered by the sum of its live legs' monthly amounts.
Session-dependent wrappers (effective_group_subscription/_tiers) are exercised
against the real DB in integration; the arithmetic they delegate to is here.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from db.entitlements import (
    leg_monthly_cents,
    pool_tier_for_total,
    subscription_is_live,
)


def _tier(key, price_cents, interval="month"):
    return SimpleNamespace(key=key, price_cents=price_cents, interval=interval, name=key)


# Most-expensive-first, as paid_group_tiers_desc returns them.
TIERS = [_tier("premium_plus", 1500), _tier("premium", 500)]
TIERS_BY_KEY = {t.key: t for t in TIERS}


def _leg(amount_cents=None, tier_key="premium", status="active", period_end=None, **kw):
    return SimpleNamespace(
        amount_cents=amount_cents,
        tier_key=tier_key,
        status=status,
        current_period_end=period_end,
        cancel_at_period_end=False,
        **kw,
    )


class TestLegMonthlyCents:
    def test_explicit_amount_wins(self):
        assert leg_monthly_cents(_leg(amount_cents=1000), TIERS_BY_KEY) == 1000

    def test_null_amount_falls_back_to_tier_price(self):
        # Legacy PayPal rows predate amount_cents.
        assert leg_monthly_cents(_leg(amount_cents=None, tier_key="premium"), TIERS_BY_KEY) == 500

    def test_null_amount_unknown_tier_contributes_nothing(self):
        assert leg_monthly_cents(_leg(amount_cents=None, tier_key="gone"), TIERS_BY_KEY) == 0

    def test_year_interval_normalizes_to_monthly(self):
        yearly = {"gold_year": _tier("gold_year", 12000, interval="year")}
        assert leg_monthly_cents(_leg(amount_cents=12000, tier_key="gold_year"), yearly) == 1000

    def test_no_tier_key_uses_amount_as_monthly(self):
        assert leg_monthly_cents(_leg(amount_cents=750, tier_key=None), TIERS_BY_KEY) == 750


class TestPoolTierForTotal:
    def test_exact_boundary_reaches_tier(self):
        assert pool_tier_for_total(TIERS, 500).key == "premium"
        assert pool_tier_for_total(TIERS, 1500).key == "premium_plus"

    def test_between_tiers_keeps_lower(self):
        assert pool_tier_for_total(TIERS, 1499).key == "premium"

    def test_below_cheapest_is_none(self):
        assert pool_tier_for_total(TIERS, 499) is None
        assert pool_tier_for_total(TIERS, 0) is None

    def test_above_top_caps_at_top(self):
        assert pool_tier_for_total(TIERS, 99999).key == "premium_plus"

    def test_multi_payer_upgrade_and_lapse(self):
        """The scenario the feature exists for: a legacy $5 leg + a member's
        $10 difference leg reach Premium+ together; either lapsing falls back
        to whatever the remaining leg still covers."""
        base = _leg(amount_cents=None, tier_key="premium")          # legacy PayPal, $5
        upgrade = _leg(amount_cents=1000, tier_key="premium_plus")  # member's +$10

        both = sum(leg_monthly_cents(leg, TIERS_BY_KEY) for leg in (base, upgrade))
        assert pool_tier_for_total(TIERS, both).key == "premium_plus"

        only_base = leg_monthly_cents(base, TIERS_BY_KEY)
        assert pool_tier_for_total(TIERS, only_base).key == "premium"

        only_upgrade = leg_monthly_cents(upgrade, TIERS_BY_KEY)
        assert pool_tier_for_total(TIERS, only_upgrade).key == "premium"


class TestSubscriptionIsLive:
    def test_active_without_period_end(self):
        assert subscription_is_live(_leg()) is True

    def test_trialing_counts(self):
        assert subscription_is_live(_leg(status="trialing")) is True

    def test_canceled_expired_past_due_do_not(self):
        for status in ("canceled", "expired", "past_due", "none"):
            assert subscription_is_live(_leg(status=status)) is False

    def test_period_end_grace_window(self):
        just_lapsed = _leg(period_end=datetime.now() - timedelta(hours=1))
        assert subscription_is_live(just_lapsed) is True  # inside 72h grace
        long_lapsed = _leg(period_end=datetime.now() - timedelta(hours=100))
        assert subscription_is_live(long_lapsed) is False
