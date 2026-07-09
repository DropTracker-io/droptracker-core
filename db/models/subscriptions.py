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
    __tablename__ = "group_subscriptions"
    __table_args__ = (
        UniqueConstraint("group_id", name="uix_group_subscription"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    tier_key = Column(String(40), ForeignKey("subscription_tiers.key"), nullable=True)
    status = Column(String(16), nullable=False, default="none")
    # none|active|trialing|past_due|canceled|expired
    provider = Column(String(16), nullable=True)  # patreon|stripe|manual
    provider_customer_id = Column(String(120), nullable=True)
    provider_subscription_id = Column(String(120), nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


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
