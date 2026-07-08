"""Payment-provider abstraction for group subscriptions (Task 11).

Stripe is the recommended provider. When Stripe isn't configured
(``STRIPE_SECRET_KEY`` unset) or the ``stripe`` library isn't installed, a
``manual`` provider is used so the subscription endpoints work end-to-end
before live billing exists: "checkout" grants the tier immediately and the
billing portal is unavailable (returns ``url: null``).

The source of truth for subscription state is always the provider webhook
(``POST /api/v1/webhooks/billing``); the Web API never trusts the client.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SITE_URL = os.getenv(
    "WEB_SITE_URL", os.getenv("NEXTAUTH_URL", "http://localhost:3000")
).rstrip("/")


def _stripe():
    """Return a configured stripe module, or None if unavailable/unconfigured."""
    if not STRIPE_SECRET_KEY:
        return None
    try:
        import stripe  # type: ignore

        stripe.api_key = STRIPE_SECRET_KEY
        return stripe
    except Exception:
        return None


def provider_name() -> str:
    return "stripe" if _stripe() is not None else "manual"


def start_checkout(group_id: int, tier, subscription) -> dict:
    """Begin (or switch to) a paid tier. Returns {'url': str|None, 'apply': dict|None}.

    - Stripe: returns a hosted Checkout URL; state is confirmed by webhook.
    - Manual: no hosted flow — returns an ``apply`` dict describing the
      immediate in-DB activation the caller should persist, and ``url: None``.
    """
    stripe = _stripe()
    if stripe is None:
        # Manual grant: activate for one interval; the caller persists this.
        period = timedelta(days=365 if tier.interval == "year" else 30)
        return {
            "url": None,
            "apply": {
                "tier_key": tier.key,
                "status": "active",
                "provider": "manual",
                "current_period_end": datetime.now() + period,
                "cancel_at_period_end": False,
            },
        }

    # Stripe hosted checkout.
    try:
        meta = {"group_id": str(group_id), "tier_key": tier.key}
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": tier.provider_price_id, "quantity": 1}],
            success_url=f"{SITE_URL}/groups/{group_id}/subscription?checkout=success",
            cancel_url=f"{SITE_URL}/groups/{group_id}/subscription?checkout=cancel",
            client_reference_id=str(group_id),
            metadata=meta,
            # Propagate onto the Subscription object so recurring
            # customer.subscription.* webhooks can be matched back to us.
            subscription_data={"metadata": meta},
        )
        return {"url": session.url, "apply": None}
    except Exception as e:
        raise RuntimeError(f"Stripe checkout failed: {e}")


def start_user_checkout(user_id: int, tier, subscription) -> dict:
    """Begin (or switch to) a user-scoped supporter tier.

    Same contract as ``start_checkout``: Stripe returns a hosted URL, manual
    returns an ``apply`` dict for immediate in-DB activation.
    """
    stripe = _stripe()
    if stripe is None:
        period = timedelta(days=365 if tier.interval == "year" else 30)
        return {
            "url": None,
            "apply": {
                "tier_key": tier.key,
                "status": "active",
                "provider": "manual",
                "current_period_end": datetime.now() + period,
                "cancel_at_period_end": False,
            },
        }

    try:
        meta = {"scope": "user", "user_id": str(user_id), "tier_key": tier.key}
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": tier.provider_price_id, "quantity": 1}],
            success_url=f"{SITE_URL}/premium?checkout=success",
            cancel_url=f"{SITE_URL}/premium?checkout=cancel",
            metadata=meta,
            subscription_data={"metadata": meta},
        )
        return {"url": session.url, "apply": None}
    except Exception as e:
        raise RuntimeError(f"Stripe checkout failed: {e}")


def cancel(subscription) -> dict:
    """Set cancel-at-period-end with the provider. Returns fields to persist."""
    stripe = _stripe()
    if stripe is not None and subscription.provider_subscription_id:
        try:
            stripe.Subscription.modify(
                subscription.provider_subscription_id, cancel_at_period_end=True
            )
        except Exception as e:
            raise RuntimeError(f"Stripe cancel failed: {e}")
    return {"cancel_at_period_end": True}


def resume(subscription) -> dict:
    stripe = _stripe()
    if stripe is not None and subscription.provider_subscription_id:
        try:
            stripe.Subscription.modify(
                subscription.provider_subscription_id, cancel_at_period_end=False
            )
        except Exception as e:
            raise RuntimeError(f"Stripe resume failed: {e}")
    return {"cancel_at_period_end": False}


def billing_portal(group_id: int, subscription) -> Optional[str]:
    """Return a provider billing-portal URL, or None if unavailable (manual)."""
    return _portal(subscription, f"{SITE_URL}/groups/{group_id}/subscription")


def user_billing_portal(subscription) -> Optional[str]:
    """Billing portal for a user-scoped supporter subscription."""
    return _portal(subscription, f"{SITE_URL}/premium")


def _portal(subscription, return_url: str) -> Optional[str]:
    stripe = _stripe()
    if stripe is None or not subscription.provider_customer_id:
        return None
    try:
        portal = stripe.billing_portal.Session.create(
            customer=subscription.provider_customer_id,
            return_url=return_url,
        )
        return portal.url
    except Exception:
        return None


def verify_webhook(payload: bytes, signature: str):
    """Verify + parse a Stripe webhook. Returns the event dict or None."""
    stripe = _stripe()
    if stripe is None or not STRIPE_WEBHOOK_SECRET:
        return None
    try:
        return stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return None


def ensure_provider_price(tier) -> Optional[str]:
    """On tier create/update, ensure a provider price exists. Returns the price
    id (or None for manual). Best-effort; failures leave provider_price_id
    unchanged."""
    stripe = _stripe()
    if stripe is None:
        return tier.provider_price_id
    if tier.provider_price_id:
        return tier.provider_price_id
    if tier.price_cents <= 0:
        return None
    try:
        product = stripe.Product.create(name=tier.name)
        price = stripe.Price.create(
            product=product.id,
            unit_amount=tier.price_cents,
            currency=tier.currency.lower(),
            recurring={"interval": tier.interval},
        )
        return price.id
    except Exception:
        return tier.provider_price_id
