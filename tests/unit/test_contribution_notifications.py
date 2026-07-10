"""Monetary-contribution notification queueing (services/contribution_notifications.py).

Pure-logic tests: money formatting, first-payment detection (only a
subscription's first settled payment is announced), and the queue payload the
bot-side NotificationService consumes. The session-dependent queue writer is
exercised against the real DB in integration.

Loaded directly from the file path (like test_event_notifications.py) because
conftest stubs the ``services`` package; the db.models imports resolve against
the conftest MagicMock stubs, which the pure functions never touch.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "contribution_notifications.py",
)
_spec = importlib.util.spec_from_file_location("_contribution_notifications_under_test", _MODULE_PATH)
cn = importlib.util.module_from_spec(_spec)
sys.modules["_contribution_notifications_under_test"] = cn
_spec.loader.exec_module(cn)

build_contribution_payload = cn.build_contribution_payload
format_money = cn.format_money
should_announce = cn.should_announce


class TestFormatMoney:
    def test_usd_symbol(self):
        assert format_money(500, "USD") == "$5.00"

    def test_thousands_separator(self):
        assert format_money(123456, "USD") == "$1,234.56"

    def test_known_symbols(self):
        assert format_money(500, "EUR") == "€5.00"
        assert format_money(500, "GBP") == "£5.00"

    def test_unknown_currency_uses_code_suffix(self):
        assert format_money(500, "SEK") == "5.00 SEK"

    def test_lowercase_currency_normalized(self):
        # Stripe sends lowercase currency codes.
        assert format_money(500, "usd") == "$5.00"

    def test_none_amount_is_zero(self):
        assert format_money(None) == "$0.00"

    def test_none_currency_defaults_usd(self):
        assert format_money(500, None) == "$5.00"


class TestShouldAnnounce:
    def test_first_payment_of_fresh_subscription(self):
        assert should_announce(0, datetime.now() - timedelta(minutes=5)) is True

    def test_renewal_with_prior_ledger_rows(self):
        assert should_announce(3, datetime.now() - timedelta(minutes=5)) is False

    def test_old_subscription_without_ledger_history_stays_quiet(self):
        # Legacy PayPal agreements: first ledger row can arrive years into
        # the subscription's life — that's a renewal, not a new supporter.
        assert should_announce(0, datetime.now() - timedelta(days=400)) is False

    def test_unknown_created_at_announces(self):
        assert should_announce(0, None) is True

    def test_explicit_paid_at_used_for_age(self):
        created = datetime(2026, 1, 1)
        assert should_announce(0, created, paid_at=datetime(2026, 1, 2)) is True
        assert should_announce(0, created, paid_at=datetime(2026, 3, 1)) is False


class TestBuildContributionPayload:
    def test_group_scope_keeps_group_id(self):
        payload = build_contribution_payload(
            scope="group", user_id=7, group_id=42, tier_key="premium",
            amount_cents=500, currency="usd", provider="stripe", external_id="in_1",
        )
        assert payload["scope"] == "group"
        assert payload["group_id"] == 42
        assert payload["user_id"] == 7
        assert payload["amount_cents"] == 500
        assert payload["currency"] == "USD"

    def test_user_scope_drops_group_id(self):
        payload = build_contribution_payload(
            scope="user", user_id=7, group_id=42, tier_key="supporter",
            amount_cents=300, currency="USD", provider="paypal", external_id="txn_9",
        )
        assert payload["scope"] == "user"
        assert payload["group_id"] is None

    def test_unknown_scope_treated_as_user(self):
        payload = build_contribution_payload(
            scope="weird", user_id=7, group_id=None, tier_key=None,
            amount_cents=100, currency="USD", provider="stripe", external_id="x",
        )
        assert payload["scope"] == "user"
