"""Task 11 — recurring subscriptions (group upgrades + user supporter).

  GET  /api/v1/subscriptions/tiers                          (public, cached; ?scope=group|user)
  GET  /api/v1/groups/{id}/subscription                     (group admin)
  POST /api/v1/groups/{id}/subscription/checkout            (group admin) {tier_key}
  POST /api/v1/groups/{id}/subscription/cancel              (group admin)
  POST /api/v1/groups/{id}/subscription/resume              (group admin)
  POST /api/v1/groups/{id}/subscription/portal              (group admin)
  GET  /api/v1/users/me/subscription                        (authed)
  POST /api/v1/users/me/subscription/checkout               (authed) {tier_key}
  POST /api/v1/users/me/subscription/cancel                 (authed)
  POST /api/v1/users/me/subscription/resume                 (authed)
  POST /api/v1/users/me/subscription/portal                 (authed)
  POST /api/v1/webhooks/billing                             (provider-signed)

Provider lifecycle lives in ``web_api/billing.py`` (Stripe + manual fallback);
the provider webhook is the source of truth for status/period.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from quart import Blueprint, jsonify, request

from db import GroupSubscription, SubscriptionTier, UserSubscription
from web_api import billing
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.entitlements import resolve_group_entitlements, resolve_user_entitlements
from web_api.entitlements_registry import (
    parse_stored_entitlements,
    resolve_tier_entitlements,
)
from web_api.tier_flair import normalize_flair
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
    scope = getattr(t, "scope", None) or "group"
    stored = parse_stored_entitlements(t.entitlements, scope)
    return {
        "key": t.key,
        "name": t.name,
        "description": t.description or "",
        "scope": scope,
        "price_cents": int(t.price_cents or 0),
        "currency": t.currency or "USD",
        "interval": t.interval or "month",
        "features": features if isinstance(features, list) else [],
        "entitlements": resolve_tier_entitlements(stored, scope),
        "flair": normalize_flair(getattr(t, "flair", None)),
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
    # Default to group tiers so pre-scope clients never see user tiers as
    # group checkout options. ?scope=user for supporter tiers, ?scope=all for both.
    scope = request.args.get("scope", "group")
    if scope not in ("group", "user", "all"):
        abort_problem(422, "Invalid scope", "scope must be 'group', 'user' or 'all'.")

    def _load():
        with db_session() as s:
            q = s.query(SubscriptionTier).filter(SubscriptionTier.active == True)  # noqa: E712
            if scope != "all":
                q = q.filter(SubscriptionTier.scope == scope)
            tiers = q.order_by(SubscriptionTier.price_cents.asc()).all()
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
# User supporter subscription — same lifecycle, scoped to the signed-in user.
# --------------------------------------------------------------------------- #
def _serialize_user_sub(user_id: int, sub: UserSubscription | None, entitlements: dict | None = None) -> dict:
    if sub is None or sub.status == "none":
        base = {
            "user_id": user_id,
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
        "user_id": user_id,
        "tier_key": sub.tier_key,
        "status": sub.status,
        "provider": sub.provider,
        "amount_cents": int(sub.amount_cents) if sub.amount_cents else None,
        "current_period_end": int(sub.current_period_end.timestamp())
        if sub.current_period_end
        else None,
        "cancel_at_period_end": bool(sub.cancel_at_period_end),
    }
    if entitlements is not None:
        payload["entitlements"] = entitlements
    return payload


def _load_user_sub(s, user_id: int) -> UserSubscription | None:
    return s.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()


@subscriptions_bp.get("/users/me/subscription")
async def get_my_subscription():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                abort_problem(401, "Not authenticated", "User not found for this session.")
            sub = _load_user_sub(s, user_id)
            entitlements = resolve_user_entitlements(s, user_id, user=user)
            return _serialize_user_sub(user_id, sub, entitlements=entitlements)

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


# Sanity ceiling for pay-what-you-want amounts ($1,000/month).
_PWYW_MAX_CENTS = 100_000


@subscriptions_bp.post("/users/me/subscription/checkout")
async def user_checkout():
    user_id = current_user_id()
    body = await json_body()
    tier_key = body.get("tier_key")
    if not tier_key:
        abort_problem(422, "Missing tier_key", "'tier_key' is required.")
    amount_cents = body.get("amount_cents")
    if amount_cents is not None and (
        isinstance(amount_cents, bool) or not isinstance(amount_cents, int)
    ):
        abort_problem(422, "Invalid amount", "'amount_cents' must be an integer.")

    def _prepare():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                abort_problem(401, "Not authenticated", "User not found for this session.")
            tier = (
                s.query(SubscriptionTier)
                .filter(
                    SubscriptionTier.key == tier_key,
                    SubscriptionTier.scope == "user",
                    SubscriptionTier.active == True,  # noqa: E712
                )
                .first()
            )
            if not tier:
                abort_problem(404, "Unknown tier", f"No active user tier '{tier_key}'.")
            minimum = int(tier.price_cents or 0)
            chosen = amount_cents if amount_cents is not None else minimum
            if chosen < minimum:
                abort_problem(
                    422,
                    "Amount below minimum",
                    f"The minimum for this tier is {minimum} cents per {tier.interval}.",
                )
            if chosen > _PWYW_MAX_CENTS:
                abort_problem(422, "Amount too large", f"The maximum is {_PWYW_MAX_CENTS} cents.")
            return {
                "key": tier.key,
                "interval": tier.interval,
                "provider_price_id": tier.provider_price_id,
                "currency": tier.currency,
                "price_cents": minimum,
                "amount_cents": chosen,
            }

    tier_snapshot = await asyncio.to_thread(_prepare)

    class _T:  # minimal attr holder for billing.start_user_checkout
        key = tier_snapshot["key"]
        interval = tier_snapshot["interval"]
        provider_price_id = tier_snapshot["provider_price_id"]
        currency = tier_snapshot["currency"]
        price_cents = tier_snapshot["price_cents"]

    try:
        result = await asyncio.to_thread(
            billing.start_user_checkout, user_id, _T(), None, tier_snapshot["amount_cents"]
        )
    except Exception as e:
        abort_problem(502, "Checkout failed", str(e))

    apply = result.get("apply")
    if apply:
        def _persist():
            with db_session() as s:
                sub = _load_user_sub(s, user_id)
                if not sub:
                    sub = UserSubscription(user_id=user_id)
                    s.add(sub)
                sub.tier_key = apply["tier_key"]
                sub.status = apply["status"]
                sub.provider = apply["provider"]
                sub.current_period_end = apply["current_period_end"]
                sub.cancel_at_period_end = apply["cancel_at_period_end"]
                sub.amount_cents = apply.get("amount_cents")
                s.commit()

        await asyncio.to_thread(_persist)
        _invalidate_user_entitlements(user_id)

    return jsonify({"url": result.get("url")})


@subscriptions_bp.post("/users/me/subscription/cancel")
async def user_cancel_subscription():
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "You have no active supporter subscription.")
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
            sub = _load_user_sub(s, user_id)
            sub.cancel_at_period_end = fields["cancel_at_period_end"]
            s.commit()
            return _serialize_user_sub(user_id, sub)

    return jsonify(await asyncio.to_thread(_persist))


@subscriptions_bp.post("/users/me/subscription/resume")
async def user_resume_subscription():
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "You have no supporter subscription.")
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
            sub = _load_user_sub(s, user_id)
            sub.cancel_at_period_end = fields["cancel_at_period_end"]
            s.commit()
            return _serialize_user_sub(user_id, sub)

    return jsonify(await asyncio.to_thread(_persist))


@subscriptions_bp.post("/users/me/subscription/portal")
async def user_portal():
    user_id = current_user_id()

    def _load_sub():
        with db_session() as s:
            sub = _load_user_sub(s, user_id)
            return sub.provider_customer_id if sub else None

    customer_id = await asyncio.to_thread(_load_sub)

    class _S:
        provider_customer_id = customer_id

    url = await asyncio.to_thread(billing.user_billing_portal, _S())
    return jsonify({"url": url})


def _invalidate_user_entitlements(user_id: int | None) -> None:
    """Best-effort in-process cache bust after a subscription change."""
    try:
        from db.entitlements import invalidate_user_entitlement_cache

        invalidate_user_entitlement_cache(user_id)
    except Exception:
        pass


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

    def _extract_amount_cents() -> int | None:
        """Best-effort recurring amount from Stripe payloads (PWYW capture)."""
        try:
            if etype == "checkout.session.completed":
                total = obj.get("amount_total")
                return int(total) if total else None
            if str(etype).startswith("customer.subscription."):
                items = ((obj.get("items") or {}).get("data")) or []
                if items:
                    price = items[0].get("price") or {}
                    amt = price.get("unit_amount")
                    if amt:
                        return int(amt)
                plan = obj.get("plan") or {}
                amt = plan.get("amount")
                return int(amt) if amt else None
        except Exception:
            pass
        return None

    def _apply_fields(sub, meta: dict):
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
            if not cpe:
                # Stripe API 2025-03+ moved current_period_end off the
                # Subscription object onto its items.
                items = ((obj.get("items") or {}).get("data")) or []
                if items:
                    cpe = items[0].get("current_period_end")
            if cpe:
                sub.current_period_end = datetime.fromtimestamp(int(cpe))
        elif etype == "customer.subscription.deleted":
            sub.status = "canceled"
        elif etype == "invoice.payment_failed":
            sub.status = "past_due"

    def _apply():
        with db_session() as s:
            meta = obj.get("metadata") or {}

            # ---- User-scoped supporter subscription ----
            if meta.get("scope") == "user":
                user_id = None
                try:
                    user_id = int(meta.get("user_id"))
                except (ValueError, TypeError):
                    user_id = None
                sub = None
                if user_id is not None:
                    sub = s.query(UserSubscription).filter(UserSubscription.user_id == user_id).first()
                    if not sub:
                        sub = UserSubscription(user_id=user_id)
                        s.add(sub)
                if sub is not None:
                    _apply_fields(sub, meta)
                    amt = _extract_amount_cents()
                    if amt:
                        sub.amount_cents = amt
                    s.commit()
                    _invalidate_user_entitlements(user_id)
                return

            group_id = None
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
                # Recurring customer.subscription.* events created before
                # subscription_data.metadata existed carry no scope/ids —
                # match by provider subscription id across both tables.
                prov_id = obj.get("id") if str(etype).startswith("customer.subscription.") else None
                if not prov_id:
                    return
                sub = (
                    s.query(GroupSubscription)
                    .filter(GroupSubscription.provider_subscription_id == prov_id)
                    .first()
                )
                if sub is None:
                    usub = (
                        s.query(UserSubscription)
                        .filter(UserSubscription.provider_subscription_id == prov_id)
                        .first()
                    )
                    if usub is None:
                        return
                    _apply_fields(usub, meta)
                    amt = _extract_amount_cents()
                    if amt:
                        usub.amount_cents = amt
                    s.commit()
                    _invalidate_user_entitlements(usub.user_id)
                    return
                _apply_fields(sub, meta)
                s.commit()
                return

            sub = s.query(GroupSubscription).filter(GroupSubscription.group_id == group_id).first()
            if not sub:
                sub = GroupSubscription(group_id=group_id)
                s.add(sub)
            _apply_fields(sub, meta)
            s.commit()

    await asyncio.to_thread(_apply)
    return jsonify({"received": True})
