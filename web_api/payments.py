"""Payment ledger writes (``subscription_payments``).

One row per settled charge/refund, written from the Stripe billing webhook,
the PayPal IPN handler, and the Stripe invoice backfill script. Inserts are
idempotent via the unique ``external_id`` (Stripe invoice id / PayPal txn_id):
provider redeliveries simply no-op. Each insert uses its own session so a
duplicate can never poison the caller's transaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError

from db import SubscriptionPayment
from web_api.common import db_session


def record_payment(
    *,
    scope: str,
    provider: str,
    amount_cents: int,
    external_id: str,
    group_id: Optional[int] = None,
    user_id: Optional[int] = None,
    subscription_id: Optional[int] = None,
    tier_key: Optional[str] = None,
    currency: str = "USD",
    kind: str = "payment",
    paid_at: Optional[datetime] = None,
) -> bool:
    """Insert a ledger row. Returns False (without raising) on duplicates."""
    with db_session() as s:
        row = SubscriptionPayment(
            scope=scope,
            group_id=group_id,
            user_id=user_id,
            subscription_id=subscription_id,
            tier_key=tier_key,
            provider=provider,
            amount_cents=int(amount_cents),
            currency=(currency or "USD").upper(),
            external_id=external_id,
            kind=kind,
            paid_at=paid_at or datetime.now(),
        )
        s.add(row)
        try:
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
            return False
