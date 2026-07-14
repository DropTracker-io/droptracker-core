"""Queue Discord notifications for monetary contributions (supporter payments).

Called from the payment ledger (``web_api/payments.py``) whenever a settled
payment lands — Stripe ``invoice.paid`` or a PayPal IPN ``subscr_payment``.
Writes a ``monetary_contribution`` row to ``notification_queue``; the bot-side
``services/notification_service.py`` renders and sends the embeds. This module
must stay free of discord/interactions imports so the Web API process can use
it.

Only a subscription's FIRST settled payment is announced — monthly renewals
stay quiet so the supporter channel celebrates new contributions instead of
repeating billing cycles. "First" means: no prior ledger rows for the
subscription AND the subscription row itself is young (guards against legacy
PayPal agreements whose first ledger row arrives years into their life).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError

from db.models import (
    GroupSubscription,
    NotificationQueue,
    Player,
    SubscriptionPayment,
    UserSubscription,
)
from db.models.base import Session

logger = logging.getLogger("services.contribution_notifications")

NOTIFICATION_TYPE = "monetary_contribution"

# A subscription older than this at payment time is treated as pre-existing
# (renewal), even when the ledger has no earlier row for it.
NEW_SUBSCRIPTION_MAX_AGE = timedelta(hours=48)

_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}


def format_money(amount_cents: Optional[int], currency: str = "USD") -> str:
    """``500, "USD"`` → ``"$5.00"``; unknown currencies render ``"5.00 SEK"``."""
    cents = int(amount_cents or 0)
    code = (currency or "USD").upper()
    whole = f"{cents / 100:,.2f}"
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{symbol}{whole}" if symbol else f"{whole} {code}"


def should_announce(
    prior_payment_count: int,
    subscription_created_at: Optional[datetime],
    paid_at: Optional[datetime] = None,
) -> bool:
    """True when this payment is a subscription's first — the only one announced."""
    if prior_payment_count > 0:
        return False
    if subscription_created_at is None:
        return True
    age = (paid_at or datetime.now()) - subscription_created_at
    return age <= NEW_SUBSCRIPTION_MAX_AGE


def build_contribution_payload(
    *,
    scope: str,
    user_id: Optional[int],
    group_id: Optional[int],
    tier_key: Optional[str],
    amount_cents: int,
    currency: str,
    provider: str,
    external_id: str,
) -> dict:
    """The notification_queue ``data`` document consumed by NotificationService."""
    return {
        "scope": "group" if scope == "group" else "user",
        "user_id": user_id,
        "group_id": group_id if scope == "group" else None,
        "tier_key": tier_key,
        "amount_cents": int(amount_cents),
        "currency": (currency or "USD").upper(),
        "provider": provider,
        "external_id": external_id,
    }


def queue_contribution_notification(
    *,
    scope: str,
    provider: str,
    amount_cents: int,
    external_id: str,
    user_id: Optional[int] = None,
    group_id: Optional[int] = None,
    subscription_id: Optional[int] = None,
    tier_key: Optional[str] = None,
    currency: str = "USD",
    paid_at: Optional[datetime] = None,
) -> bool:
    """Queue a ``monetary_contribution`` notification for a settled payment.

    Best-effort: returns True when a row was queued, False when skipped
    (renewal, duplicate, or ambiguous). Never raises — a notification must
    not break payment processing.
    """
    try:
        s = Session()
        try:
            if subscription_id is None:
                # Can't tell first payment from renewal — stay quiet rather
                # than risk announcing every billing cycle.
                return False

            model = GroupSubscription if scope == "group" else UserSubscription
            sub = s.query(model).filter(model.id == subscription_id).first()

            prior = (
                s.query(SubscriptionPayment)
                .filter(
                    SubscriptionPayment.scope == scope,
                    SubscriptionPayment.subscription_id == subscription_id,
                    SubscriptionPayment.kind == "payment",
                    SubscriptionPayment.external_id != external_id,
                )
                .count()
            )
            if not should_announce(prior, sub.created_at if sub else None, paid_at):
                return False

            payload = build_contribution_payload(
                scope=scope,
                user_id=user_id,
                group_id=group_id,
                tier_key=tier_key,
                amount_cents=amount_cents,
                currency=currency,
                provider=provider,
                external_id=external_id,
            )

            # notification_queue.player_id is NOT NULL; use the contributor's
            # first tracked player, falling back to the ids the legacy XF
            # writers used (user_id / group_id) so the row always inserts.
            player_id = None
            if user_id is not None:
                player = s.query(Player).filter(Player.user_id == user_id).first()
                player_id = player.player_id if player else None
            if player_id is None:
                player_id = user_id or group_id or 0

            s.add(
                NotificationQueue(
                    notification_type=NOTIFICATION_TYPE,
                    player_id=player_id,
                    group_id=group_id if scope == "group" else None,
                    data=json.dumps(payload),
                    status="pending",
                    created_at=datetime.now(),
                )
            )
            try:
                s.commit()
            except IntegrityError:
                s.rollback()  # provider redelivery — already queued
                return False

            # Site-wide ticker (rt:feed): announce the new subscription. Only
            # reached for first settled payments (renewals returned above), so
            # this matches "a new group/player subscribed". Best-effort.
            try:
                from services.realtime import publish_feed_subscription

                if scope == "group" and group_id is not None:
                    from db.models import Group

                    group = s.query(Group).filter(Group.group_id == group_id).first()
                    if group is not None:
                        publish_feed_subscription(
                            "group", group.group_name,
                            group_id=group_id, tier_key=tier_key,
                        )
                elif scope == "user":
                    # `player` was resolved above (contributor's first tracked
                    # player); fall back to their Discord-linked username.
                    display_name = player.player_name if user_id is not None and player else None
                    display_player_id = player.player_id if user_id is not None and player else None
                    if display_name is None and user_id is not None:
                        from db.models import User

                        u = s.query(User).filter(User.user_id == user_id).first()
                        display_name = u.username if u else None
                    if display_name:
                        publish_feed_subscription(
                            "user", display_name,
                            player_id=display_player_id, tier_key=tier_key,
                        )
            except Exception:
                logger.exception("Ticker subscription publish failed")
            return True
        finally:
            s.close()
    except Exception:
        logger.exception(
            "Failed to queue contribution notification (scope=%s external_id=%s)",
            scope,
            external_id,
        )
        return False
