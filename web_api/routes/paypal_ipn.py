"""Legacy PayPal IPN endpoint — keeps pre-cutover group subscriptions renewing.

  POST /api/v1/webhooks/paypal-ipn

Group premium tiers sold through the XenForo era are PayPal subscription
agreements whose notification URL is baked in as
``https://www.droptracker.io/payment_callback.php?_xfProvider=paypal``.
Those agreements cannot be re-pointed or transferred to Stripe, so after the
domain cutover the Next.js server rewrites that exact path to this route
(see ``web/apps/web/next.config.ts``), and this handler takes over what
XenForo's payment callback + hourly downgrade cron used to do.

Contract (verified against XF's ``\\XF\\Payment\\PayPal`` provider):
  * ``custom``     = XF purchase_request_key  → matched to
                     ``group_subscriptions.provider_customer_id``
  * ``subscr_id``  = PayPal agreement id (I-…) → matched to
                     ``group_subscriptions.provider_subscription_id``
  * ``txn_type``   = subscr_payment | subscr_cancel | subscr_eot | subscr_failed
  * authenticity   = echo the raw body back to PayPal with
                     ``cmd=_notify-validate`` prepended; PayPal answers
                     ``VERIFIED`` or ``INVALID``.

Rows are populated by ``scripts/backfill_group_subscriptions.py`` (groups) and
``scripts/backfill_user_subscriptions.py`` (legacy XF *user* upgrades, matched
against ``user_subscriptions``). IPNs that match no row in either table are
logged loudly and acknowledged. PayPal retries non-2xx deliveries for ~4 days,
so transient failures answer 503.

Env:
  PAYPAL_RECEIVER_EMAIL      expected receiver account (recommended; skipped if unset)
  PAYPAL_IPN_ALLOW_SANDBOX   accept test_ipn messages (default off)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from urllib.parse import parse_qsl

import aiohttp
from dateutil.relativedelta import relativedelta
from quart import Blueprint, jsonify, request

from db import GroupSubscription, SubscriptionTier, UserSubscription
from web_api.common import db_session
from utils.redis import redis_client

logger = logging.getLogger("web_api.paypal_ipn")

paypal_ipn_bp = Blueprint("v1_paypal_ipn", __name__)

_VERIFY_URL = "https://ipnpb.paypal.com/cgi-bin/webscr"
_VERIFY_URL_SANDBOX = "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr"
_TXN_DEDUP_TTL = 120 * 86400  # PayPal can redeliver for days; remember 120


async def _verify_with_paypal(raw: bytes, sandbox: bool) -> str:
    """Echo the IPN back to PayPal. Returns 'VERIFIED', 'INVALID', or raises."""
    url = _VERIFY_URL_SANDBOX if sandbox else _VERIFY_URL
    body = b"cmd=_notify-validate&" + raw
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            return (await resp.text()).strip()


def _extend_subscription(kind: str, sub_id: int, fields: dict) -> dict | None:
    """Apply an IPN outcome to a subscription row. Runs in a worker thread."""
    model = UserSubscription if kind == "user" else GroupSubscription
    with db_session() as s:
        sub = s.query(model).filter(model.id == sub_id).first()
        if not sub:
            return None
        for k, v in fields.items():
            setattr(sub, k, v)
        s.commit()
        owner = {"user_id": sub.user_id} if kind == "user" else {"group_id": sub.group_id}
        result = {**owner, "tier_key": sub.tier_key, "status": sub.status,
                  "current_period_end": str(sub.current_period_end)}
    if kind == "user":
        try:
            from db.entitlements import invalidate_user_entitlement_cache

            invalidate_user_entitlement_cache(result.get("user_id"))
        except Exception:
            pass
    return result


def _find_subscription(subscr_id: str | None, custom: str | None):
    """Match an IPN to a paypal-provider row (group first, then user).

    Returns a detached snapshot dict with ``kind`` = group|user.
    """
    with db_session() as s:
        sub, kind = None, "group"
        q = s.query(GroupSubscription).filter(GroupSubscription.provider == "paypal")
        if subscr_id:
            sub = q.filter(GroupSubscription.provider_subscription_id == subscr_id).first()
        if sub is None and custom:
            sub = q.filter(GroupSubscription.provider_customer_id == custom).first()
        if sub is None:
            kind = "user"
            uq = s.query(UserSubscription).filter(UserSubscription.provider == "paypal")
            if subscr_id:
                sub = uq.filter(UserSubscription.provider_subscription_id == subscr_id).first()
            if sub is None and custom:
                sub = uq.filter(UserSubscription.provider_customer_id == custom).first()
        if sub is None:
            return None
        tier = (
            s.query(SubscriptionTier).filter(SubscriptionTier.key == sub.tier_key).first()
            if sub.tier_key
            else None
        )
        owner_id = sub.user_id if kind == "user" else sub.group_id
        return {
            "kind": kind,
            "id": sub.id,
            "owner_id": owner_id,
            "tier_key": sub.tier_key,
            "current_period_end": sub.current_period_end,
            "price_cents": int(tier.price_cents) if tier else None,
            "currency": (tier.currency or "USD").upper() if tier else None,
            "interval": (tier.interval or "month") if tier else "month",
        }


@paypal_ipn_bp.post("/webhooks/paypal-ipn")
async def paypal_ipn():
    raw = await request.get_data()
    if not raw:
        return jsonify({"ignored": "empty"}), 200

    fields = dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))
    txn_type = fields.get("txn_type", "")
    payment_status = fields.get("payment_status", "")
    subscr_id = fields.get("subscr_id") or fields.get("recurring_payment_id")
    custom = fields.get("custom")
    txn_id = fields.get("txn_id")
    is_sandbox = fields.get("test_ipn") == "1"

    if is_sandbox and os.getenv("PAYPAL_IPN_ALLOW_SANDBOX", "0") != "1":
        logger.warning("paypal_ipn: ignoring test_ipn message (sandbox not allowed)")
        return jsonify({"ignored": "test_ipn"}), 200

    # --- Authenticity: postback to PayPal. Transient failure → 503 (PayPal retries). ---
    try:
        verdict = await _verify_with_paypal(raw, sandbox=is_sandbox)
    except Exception as e:
        logger.error("paypal_ipn: verification postback failed (%s); asking for retry", e)
        return jsonify({"error": "verification unavailable"}), 503
    if verdict != "VERIFIED":
        logger.warning(
            "paypal_ipn: INVALID message dropped (txn_type=%s subscr_id=%s txn_id=%s)",
            txn_type, subscr_id, txn_id,
        )
        return jsonify({"ignored": "invalid"}), 200

    # --- Optional receiver check (guards against valid IPNs for someone else's account). ---
    expected_receiver = os.getenv("PAYPAL_RECEIVER_EMAIL", "").strip().lower()
    if expected_receiver:
        receiver = (fields.get("receiver_email") or fields.get("business") or "").strip().lower()
        if receiver and receiver != expected_receiver:
            logger.warning("paypal_ipn: receiver mismatch (%s); dropped", receiver)
            return jsonify({"ignored": "receiver mismatch"}), 200

    # --- Idempotency: PayPal redelivers; a txn_id must apply at most once. ---
    if txn_id:
        try:
            fresh = redis_client.client.set(
                f"web:paypal:ipn:{txn_id}", "1", nx=True, ex=_TXN_DEDUP_TTL
            )
            if not fresh:
                return jsonify({"ignored": "duplicate txn"}), 200
        except Exception:
            pass  # Redis down: proceed rather than drop a real payment.

    snapshot = await asyncio.to_thread(_find_subscription, subscr_id, custom)
    if snapshot is None:
        # Expected for XF *user*-upgrade agreements which share this URL.
        logger.warning(
            "paypal_ipn: VERIFIED but unmatched (txn_type=%s status=%s subscr_id=%s custom=%s "
            "gross=%s) — likely a legacy user upgrade; no group subscription touched",
            txn_type, payment_status, subscr_id, custom, fields.get("mc_gross"),
        )
        return jsonify({"ignored": "no matching subscription"}), 200

    now = datetime.now()
    updates: dict | None = None
    action = "none"
    owner = f"{snapshot['kind']} {snapshot['owner_id']}"

    if payment_status in ("Refunded", "Reversed", "Canceled_Reversal") and fields.get("parent_txn_id"):
        if payment_status == "Canceled_Reversal":
            updates = {"status": "active"}
            action = "reversal cancelled — reactivated"
        else:
            updates = {"status": "canceled"}
            action = f"{payment_status.lower()} — benefits revoked"
    elif txn_type == "subscr_payment" and payment_status == "Completed":
        gross_cents = round(float(fields.get("mc_gross", "0")) * 100)
        currency = (fields.get("mc_currency") or "").upper()
        if snapshot["price_cents"] is not None and (
            gross_cents < snapshot["price_cents"] or currency != snapshot["currency"]
        ):
            logger.warning(
                "paypal_ipn: amount mismatch for %s (%s %s < expected %s %s); not extending",
                owner, gross_cents, currency,
                snapshot["price_cents"], snapshot["currency"],
            )
            return jsonify({"ignored": "amount mismatch"}), 200
        base = snapshot["current_period_end"]
        base = base if (base and base > now) else now
        step = relativedelta(years=1) if snapshot["interval"] == "year" else relativedelta(months=1)
        updates = {
            "status": "active",
            "current_period_end": base + step,
            "cancel_at_period_end": False,
        }
        action = "payment — period extended"
    elif txn_type == "subscr_cancel":
        updates = {"cancel_at_period_end": True}
        action = "cancelled — runs to period end"
    elif txn_type == "subscr_eot":
        updates = {"cancel_at_period_end": True}
        if snapshot["current_period_end"] and snapshot["current_period_end"] < now:
            updates["status"] = "expired"
        action = "end of term"
    elif txn_type == "subscr_failed":
        logger.warning(
            "paypal_ipn: payment failed for %s (PayPal will retry); no change",
            owner,
        )
        return jsonify({"received": True}), 200
    else:
        logger.info(
            "paypal_ipn: no-op event txn_type=%s status=%s for %s",
            txn_type, payment_status, owner,
        )
        return jsonify({"received": True}), 200

    result = await asyncio.to_thread(_extend_subscription, snapshot["kind"], snapshot["id"], updates)
    logger.info("paypal_ipn: %s — %s → %s", owner, action, result)
    return jsonify({"received": True}), 200
