"""Task 11 — group recurring subscriptions (upgrades).

  GET  /api/v1/subscriptions/tiers                          (public, cached)
  GET  /api/v1/groups/{id}/subscription                     (group admin)
  POST /api/v1/groups/{id}/subscription/checkout            (group admin) {tier_key}
  POST /api/v1/groups/{id}/subscription/cancel              (group admin)
  POST /api/v1/groups/{id}/subscription/resume              (group admin)
  POST /api/v1/groups/{id}/subscription/portal              (group admin)
  POST /api/v1/webhooks/billing                             (provider-signed)

Provider lifecycle lives in ``web_api/billing.py`` (Stripe + manual fallback);
the provider webhook is the source of truth for status/period.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from quart import Blueprint, jsonify, request

from db import GroupSubscription, SubscriptionTier
from web_api import billing
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.entitlements import resolve_group_entitlements
from web_api.entitlements_registry import (
    parse_stored_entitlements,
    resolve_tier_entitlements,
)
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

subscriptions_bp = Blueprint("v1_subscriptions", __name__)


def _serialize_tier(t: SubscriptionTier) -> dict:
    try:
        features = json.loads(t.features) if t.features else []
    except Exception:
        features = []
    stored = parse_stored_entitlements(t.entitlements)
    return {
        "key": t.key,
        "name": t.name,
        "description": t.description or "",
        "price_cents": int(t.price_cents or 0),
        "currency": t.currency or "USD",
        "interval": t.interval or "month",
        "features": features if isinstance(features, list) else [],
        "entitlements": resolve_tier_entitlements(stored),
        "recommended": bool(t.recommended),
    }


def _serialize_sub(group_id: int, sub: GroupSubscription | None, entitlements: dict | None = None) -> dict:
    if sub is None or sub.status == "none":
        base = {
            "group_id": group_id,
            "tier_key": None,
            "status": "none",
            "provider": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
        }
        if entitlements is not None:
            base["entitlements"] = entitlements
        return base
    payload = {
        "group_id": group_id,
        "tier_key": sub.tier_key,
        "status": sub.status,
        "provider": sub.provider,
        "current_period_end": int(sub.current_period_end.timestamp())
        if sub.current_period_end
        else None,
        "cancel_at_period_end": bool(sub.cancel_at_period_end),
    }
    if entitlements is not None:
        payload["entitlements"] = entitlements
    return payload


@subscriptions_bp.get("/subscriptions/tiers")
async def list_tiers():
    def _load():
        with db_session() as s:
            tiers = (
                s.query(SubscriptionTier)
                .filter(SubscriptionTier.active == True)  # noqa: E712
                .order_by(SubscriptionTier.price_cents.asc())
                .all()
            )
            return [_serialize_tier(t) for t in tiers]

    tiers = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(tiers), max_age=300)


def _require_admin_and_sub(user_id, group_id):
    with db_session() as s:
        user = load_user(s, user_id)
        assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
        sub = (
            s.query(GroupSubscription)
            .filter(GroupSubscription.group_id == group_id)
            .first()
        )
        entitlements = resolve_group_entitlements(s, group_id, user=user)
        return _serialize_sub(group_id, sub, entitlements=entitlements)


@subscriptions_bp.get("/groups/<int:group_id>/subscription")
async def get_subscription(group_id: int):
    user_id = current_user_id()
    payload = await asyncio.to_thread(_require_admin_and_sub, user_id, group_id)
    return private_no_store(jsonify(payload))


@subscriptions_bp.post("/groups/<int:group_id>/subscription/checkout")
async def checkout(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    tier_key = body.get("tier_key")
    if not tier_key:
        abort_problem(422, "Missing tier_key", "'tier_key' is required.")

    def _prepare():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            tier = (
                s.query(SubscriptionTier)
                .filter(SubscriptionTier.key == tier_key, SubscriptionTier.active == True)  # noqa: E712
                .first()
            )
            if not tier:
                abort_problem(404, "Unknown tier", f"No active tier '{tier_key}'.")
            sub = (
                s.query(GroupSubscription)
                .filter(GroupSubscription.group_id == group_id)
                .first()
            )
            # Detach a lightweight snapshot for the provider call.
            return {
                "key": tier.key,
                "interval": tier.interval,
                "provider_price_id": tier.provider_price_id,
            }, (sub is not None and sub.status == "active")

    tier_snapshot, _has_active = await asyncio.to_thread(_prepare)

    # Provider call (may be network I/O for Stripe).
    class _T:  # minimal attr holder for billing.start_checkout
        key = tier_snapshot["key"]
        interval = tier_snapshot["interval"]
        provider_price_id = tier_snapshot["provider_price_id"]

    try:
        result = await asyncio.to_thread(billing.start_checkout, group_id, _T(), None)
    except Exception as e:
        abort_problem(502, "Checkout failed", str(e))

    # Manual provider grants immediately — persist it now.
    apply = result.get("apply")
    if apply:
        def _persist():
            with db_session() as s:
                sub = (
                    s.query(GroupSubscription)
                    .filter(GroupSubscription.group_id == group_id)
                    .first()
                )
                if not sub:
                    sub = GroupSubscription(group_id=group_id)
                    s.add(sub)
                sub.tier_key = apply["tier_key"]
                sub.status = apply["status"]
                sub.provider = apply["provider"]
                sub.current_period_end = apply["current_period_end"]
                sub.cancel_at_period_end = apply["cancel_at_period_end"]
                s.commit()

        await asyncio.to_thread(_persist)

    return jsonify({"url": result.get("url")})


@subscriptions_bp.post("/groups/<int:group_id>/subscription/cancel")
async def cancel_subscription(group_id: int):
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            sub = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id).first()
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "This group has no active subscription.")
            return sub.provider_subscription_id

    prov_sub_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_subscription_id = prov_sub_id

    try:
        fields = await asyncio.to_thread(billing.cancel, _S())
    except Exception as e:
        abort_problem(502, "Cancel failed", str(e))

    def _persist():
        with db_session() as s:
            sub = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id).first()
            sub.cancel_at_period_end = fields["cancel_at_period_end"]
            s.commit()
            return _serialize_sub(group_id, sub)

    return jsonify(await asyncio.to_thread(_persist))


@subscriptions_bp.post("/groups/<int:group_id>/subscription/resume")
async def resume_subscription(group_id: int):
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            sub = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id).first()
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "This group has no subscription.")
            return sub.provider_subscription_id

    prov_sub_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_subscription_id = prov_sub_id

    try:
        fields = await asyncio.to_thread(billing.resume, _S())
    except Exception as e:
        abort_problem(502, "Resume failed", str(e))

    def _persist():
        with db_session() as s:
            sub = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id).first()
            sub.cancel_at_period_end = fields["cancel_at_period_end"]
            s.commit()
            return _serialize_sub(group_id, sub)

    return jsonify(await asyncio.to_thread(_persist))


@subscriptions_bp.post("/groups/<int:group_id>/subscription/portal")
async def portal(group_id: int):
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            sub = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id).first()
            return sub.provider_customer_id if sub else None

    customer_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_customer_id = customer_id

    url = await asyncio.to_thread(billing.billing_portal, group_id, _S())
    return jsonify({"url": url})


# --------------------------------------------------------------------------- #
# Provider webhook — source of truth for status/period (no client trust).
# --------------------------------------------------------------------------- #
@subscriptions_bp.post("/webhooks/billing")
async def billing_webhook():
    raw = await request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    event = billing.verify_webhook(raw, signature)
    if event is None:
        abort_problem(400, "Invalid webhook", "Signature verification failed or unconfigured.")

    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    def _apply():
        with db_session() as s:
            group_id = None
            meta = obj.get("metadata") or {}
            if meta.get("group_id"):
                try:
                    group_id = int(meta["group_id"])
                except (ValueError, TypeError):
                    group_id = None
            if group_id is None and obj.get("client_reference_id"):
                try:
                    group_id = int(obj["client_reference_id"])
                except (ValueError, TypeError):
                    group_id = None
            if group_id is None:
                return

            sub = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id).first()
            if not sub:
                sub = GroupSubscription(group_id=group_id)
                s.add(sub)

            if etype == "checkout.session.completed":
                sub.provider = "stripe"
                sub.status = "active"
                sub.provider_customer_id = obj.get("customer")
                sub.provider_subscription_id = obj.get("subscription")
                if meta.get("tier_key"):
                    sub.tier_key = meta["tier_key"]
            elif etype in ("customer.subscription.updated", "customer.subscription.created"):
                sub.status = obj.get("status", sub.status)
                sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
                cpe = obj.get("current_period_end")
                if cpe:
                    sub.current_period_end = datetime.fromtimestamp(int(cpe))
            elif etype == "customer.subscription.deleted":
                sub.status = "canceled"
            elif etype == "invoice.payment_failed":
                sub.status = "past_due"
            s.commit()

    await asyncio.to_thread(_apply)
    return jsonify({"received": True})
