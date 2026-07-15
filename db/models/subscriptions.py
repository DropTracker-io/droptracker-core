"""Group recurring subscription models (backend Task 11, §14.1 upgrades).

A group holds at most one subscription to a tier. Billing lifecycle is owned by
the backend + a payment provider (Stripe recommended); the Web API exposes
status and kicks off provider-hosted flows. Replaces the points-based feature
store (out of scope).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy import func

from .base import Base


class SubscriptionTier(Base):
    __tablename__ = "subscription_tiers"
    __table_args__ = ({"extend_existing": True},)

    key = Column(String(40), primary_key=True)
    name = Column(String(80), nullable=False)
    description = Column(Text, nullable=True)
    # Who this tier applies to: "group" tiers grant group entitlements via
    # group_subscriptions; "user" tiers grant per-user supporter entitlements
    # via user_subscriptions. Both live here so tier CRUD/billing are shared.
    scope = Column(String(8), nullable=False, default="group")  # group|user
    price_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(8), nullable=False, default="USD")
    interval = Column(String(8), nullable=False, default="month")  # month|year
    features = Column(Text, nullable=True)  # JSON array of strings
    entitlements = Column(Text, nullable=True)  # JSON object of booleans (Task 15)
    # Cosmetic display style granted to subscribed groups on the website
    # (none|bronze|gold|amethyst|dragon). See web_api/tier_flair.py.
    flair = Column(String(16), nullable=False, default="none", server_default="none")
    recommended = Column(Boolean, nullable=False, default=False)
    provider_price_id = Column(String(120), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class GroupSubscription(Base):
    """One contribution "leg" toward a group's subscription pool.

    Subscription-pool model: a group may hold MANY legs, each an independent
    recurring payment owned by a payer (``user_id``; NULL for legacy/manual
    rows). The group's effective tier is computed from the sum of its live
    legs' monthly amounts — see ``db/entitlements.py
    effective_group_subscription``. ``tier_key`` records the tier the leg was
    purchased toward (informational; the effective tier is always computed).
    """

    __tablename__ = "group_subscriptions"
    __table_args__ = ({"extend_existing": True},)

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    # Payer. NULL for legacy PayPal agreements and comped/manual grants.
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    tier_key = Column(String(40), ForeignKey("subscription_tiers.key"), nullable=True)
    # Recurring charge in minor units per the tier's interval. NULL on legacy
    # rows (pool math falls back to the tier's price_cents).
    amount_cents = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="none")
    # none|active|trialing|past_due|canceled|expired
    # patreon|stripe|paypal|manual (comped) | nitro (Discord boost credit — not
    # revenue; see services/nitro_attribution.py + db.entitlements.NITRO_PROVIDER)
    provider = Column(String(16), nullable=True)
    provider_customer_id = Column(String(120), nullable=True)
    provider_subscription_id = Column(String(120), nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class SubscriptionPayment(Base):
    """Payment ledger — one row per settled charge/refund across providers.

    Written by the Stripe billing webhook (``invoice.paid`` /
    ``charge.refunded``), the PayPal IPN (``subscr_payment``), and the Stripe
    invoice backfill script. ``external_id`` (Stripe invoice id / PayPal
    txn_id) is unique so redeliveries are idempotent. Feeds the superadmin
    monetization dashboard (monthly income, lifetime earnings).
    """

    __tablename__ = "subscription_payments"
    __table_args__ = (
        UniqueConstraint("external_id", name="uix_subscription_payment_external"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(8), nullable=False)  # group|user
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    # Payer (group legs) or supporter (user subs); NULL when unattributable.
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    # group_subscriptions.id or user_subscriptions.id (no FK: either table).
    subscription_id = Column(Integer, nullable=True)
    tier_key = Column(String(40), nullable=True)
    provider = Column(String(16), nullable=False)  # stripe|paypal|manual
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(8), nullable=False, default="USD")
    external_id = Column(String(191), nullable=False)
    kind = Column(String(12), nullable=False, default="payment")  # payment|refund|reversal
    paid_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class UserSubscription(Base):
    """Per-user supporter subscription — same lifecycle as GroupSubscription.

    A user holds at most one subscription to a user-scoped tier. Grants
    personal perks (submission DMs, supporter flair) independent of any
    group's subscription.
    """

    __tablename__ = "user_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", name="uix_user_subscription"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    tier_key = Column(String(40), ForeignKey("subscription_tiers.key"), nullable=True)
    status = Column(String(16), nullable=False, default="none")
    # none|active|trialing|past_due|canceled|expired
    provider = Column(String(16), nullable=True)  # stripe|paypal|manual
    # Pay-what-you-want: the user's chosen monthly amount in minor units.
    # The tier's price_cents is the MINIMUM; NULL = unknown (legacy rows).
    amount_cents = Column(Integer, nullable=True)
    provider_customer_id = Column(String(120), nullable=True)
    provider_subscription_id = Column(String(120), nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
