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

import json
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


def _guard_manual_grant():
    """Refuse the manual (free, immediate) grant in production.

    The manual provider exists so dev/test environments work end-to-end
    without Stripe. In production a missing/broken Stripe configuration must
    fail the checkout loudly instead of silently giving the tier away.
    """
    state = (os.getenv("STATE") or "").strip().strip('"').lower()
    if state in ("live", "prod", "production"):
        raise RuntimeError(
            "Billing is not configured (Stripe unavailable); refusing manual grant in production."
        )


def start_checkout(group_id: int, tier, subscription) -> dict:
    """Begin (or switch to) a paid tier. Returns {'url': str|None, 'apply': dict|None}.

    - Stripe: returns a hosted Checkout URL; state is confirmed by webhook.
    - Manual: no hosted flow — returns an ``apply`` dict describing the
      immediate in-DB activation the caller should persist, and ``url: None``.
    """
    stripe = _stripe()
    if stripe is None:
        # Manual grant: activate for one interval; the caller persists this.
        _guard_manual_grant()
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


def start_user_checkout(user_id: int, tier, subscription, amount_cents: int | None = None) -> dict:
    """Begin (or switch to) a user-scoped supporter tier.

    Pay-what-you-want: ``amount_cents`` is the user's chosen recurring amount
    (validated ≥ ``tier.price_cents`` by the route); ``None`` falls back to
    the tier minimum. Same contract as ``start_checkout``: Stripe returns a
    hosted URL, manual returns an ``apply`` dict for immediate activation.
    """
    amount = int(amount_cents or tier.price_cents or 0)
    stripe = _stripe()
    if stripe is None:
        _guard_manual_grant()
        period = timedelta(days=365 if tier.interval == "year" else 30)
        return {
            "url": None,
            "apply": {
                "tier_key": tier.key,
                "status": "active",
                "provider": "manual",
                "current_period_end": datetime.now() + period,
                "cancel_at_period_end": False,
                "amount_cents": amount,
            },
        }

    try:
        meta = {"scope": "user", "user_id": str(user_id), "tier_key": tier.key}
        # Custom amounts need an ad-hoc recurring price. Reuse the tier's
        # Stripe product when one exists so the dashboard stays tidy;
        # otherwise let Stripe create a product inline.
        price_data = {
            "currency": (tier.currency or "USD").lower(),
            "unit_amount": amount,
            "recurring": {"interval": tier.interval or "month"},
        }
        product_id = None
        if tier.provider_price_id:
            try:
                product_id = stripe.Price.retrieve(tier.provider_price_id).product
            except Exception:
                product_id = None
        if product_id:
            price_data["product"] = product_id
        else:
            price_data["product_data"] = {"name": f"DropTracker {tier.key} (supporter)"}

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price_data": price_data, "quantity": 1}],
            success_url=f"{SITE_URL}/premium?checkout=success",
            cancel_url=f"{SITE_URL}/premium?checkout=cancel",
            metadata=meta,
            subscription_data={"metadata": meta},
        )
        return {"url": session.url, "apply": None}
    except Exception as e:
        raise RuntimeError(f"Stripe checkout failed: {e}")


def start_group_leg_checkout(group_id: int, payer_user_id: int, tier, delta_cents: int) -> dict:
    """Begin a contribution "leg" toward a group's subscription pool.

    Pool model: the member pays the DIFFERENCE between the target tier's
    monthly price and the group's current live pool total, as their own
    recurring subscription. Same contract as ``start_checkout``: Stripe
    returns a hosted URL (state confirmed by webhook), manual returns an
    ``apply`` dict the caller persists as a new leg row.
    """
    amount = int(delta_cents)
    stripe = _stripe()
    if stripe is None:
        _guard_manual_grant()
        period = timedelta(days=365 if tier.interval == "year" else 30)
        return {
            "url": None,
            "apply": {
                "tier_key": tier.key,
                "status": "active",
                "provider": "manual",
                "current_period_end": datetime.now() + period,
                "cancel_at_period_end": False,
                "amount_cents": amount,
                "user_id": payer_user_id,
            },
        }

    try:
        meta = {
            "scope": "group",
            "group_id": str(group_id),
            "tier_key": tier.key,
            "payer_user_id": str(payer_user_id),
        }
        # Difference-priced legs need an ad-hoc recurring price (same pattern
        # as supporter pay-what-you-want). Reuse the tier's product if it has
        # one so the Stripe dashboard stays tidy.
        price_data = {
            "currency": (tier.currency or "USD").lower(),
            "unit_amount": amount,
            "recurring": {"interval": "month"},
        }
        product_id = None
        if tier.provider_price_id:
            try:
                product_id = stripe.Price.retrieve(tier.provider_price_id).product
            except Exception:
                product_id = None
        if product_id:
            price_data["product"] = product_id
        else:
            price_data["product_data"] = {"name": f"DropTracker {tier.key} (group contribution)"}

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price_data": price_data, "quantity": 1}],
            success_url=f"{SITE_URL}/groups/{group_id}/subscription?checkout=success",
            cancel_url=f"{SITE_URL}/groups/{group_id}/subscription?checkout=cancel",
            client_reference_id=str(group_id),
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
    """Verify + parse a Stripe webhook. Returns the event as a plain dict or None.

    ``construct_event`` is used only for signature verification. It returns a
    ``stripe.Event`` whose ``StripeObject`` base stopped subclassing dict in
    newer stripe-python (15.x has no ``.get``/``.to_dict_recursive``), which
    made the webhook route's dict-style access crash with 500s and silently
    drop every subscription event. Parse the verified raw payload instead so
    the route always sees plain dicts.
    """
    stripe = _stripe()
    if stripe is None or not STRIPE_WEBHOOK_SECRET:
        return None
    try:
        stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return None
    try:
        event = json.loads(payload)
    except Exception:
        return None
    return event if isinstance(event, dict) else None


def fetch_subscription_state(subscription_id: Optional[str]) -> Optional[dict]:
    """Read authoritative subscription state straight from Stripe (read-only).

    Used by the webhook to hydrate a freshly-created leg on
    ``checkout.session.completed`` instead of depending on the sibling
    ``customer.subscription.*`` / ``invoice.paid`` events. Stripe delivers
    those with no ordering guarantee, so they can arrive *before* the leg row
    exists — the handler no-ops them and returns 200, and Stripe never retries.
    That left new legs with a NULL ``current_period_end`` (blank renewal date)
    and no first-invoice ledger row until the next billing cycle.

    Returns a dict with ``status``, ``current_period_end`` (datetime|None),
    ``cancel_at_period_end`` and ``invoice`` (the latest invoice's ledger
    fields, or None), or None when Stripe is unavailable / the lookup fails so
    callers keep their previous behaviour.
    """
    stripe = _stripe()
    if stripe is None or not subscription_id:
        return None
    try:
        sub = json.loads(
            str(stripe.Subscription.retrieve(subscription_id, expand=["latest_invoice"]))
        )
    except Exception:
        return None
    # Stripe API 2025-03+ moved current_period_end off the Subscription object
    # onto its items; fall back accordingly.
    items = (sub.get("items") or {}).get("data") or [{}]
    cpe = sub.get("current_period_end") or items[0].get("current_period_end")
    invoice = None
    inv = sub.get("latest_invoice")
    if isinstance(inv, dict) and inv.get("amount_paid") and inv.get("status") == "paid":
        paid_ts = ((inv.get("status_transitions") or {}).get("paid_at")) or inv.get("created")
        invoice = {
            "external_id": str(inv.get("id")),
            "amount_cents": int(inv.get("amount_paid")),
            "currency": inv.get("currency") or "USD",
            "paid_at": datetime.fromtimestamp(int(paid_ts)) if paid_ts else None,
        }
    return {
        "status": sub.get("status"),
        "current_period_end": datetime.fromtimestamp(int(cpe)) if cpe else None,
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
        "invoice": invoice,
    }


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
