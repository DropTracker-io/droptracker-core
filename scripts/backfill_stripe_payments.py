"""One-off backfill: historical Stripe invoices → subscription_payments ledger.

The ledger (web28a) only accrues from deploy time via the billing webhook;
this script imports every previously-paid Stripe invoice so "lifetime
earnings" and month charts are complete for the Stripe era. PayPal history is
not recoverable through IPN and is intentionally out of scope.

Idempotent: rows key on the unique invoice id, so re-runs and overlap with
webhook-written rows are harmless. Read-only against Stripe; writes only the
ledger table.

Usage:
    source venv/bin/activate
    python -m scripts.backfill_stripe_payments [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from db import GroupSubscription, UserSubscription  # noqa: E402
from web_api.common import db_session  # noqa: E402
from web_api.payments import record_payment  # noqa: E402


def _resolve_attribution(s, prov_sub_id: str | None, meta: dict) -> dict | None:
    """Map an invoice to a group leg or user subscription."""
    if prov_sub_id:
        leg = (
            s.query(GroupSubscription)
            .filter(GroupSubscription.provider_subscription_id == prov_sub_id)
            .first()
        )
        if leg is not None:
            return {
                "scope": "group",
                "group_id": leg.group_id,
                "user_id": leg.user_id,
                "subscription_id": leg.id,
                "tier_key": leg.tier_key,
            }
        usub = (
            s.query(UserSubscription)
            .filter(UserSubscription.provider_subscription_id == prov_sub_id)
            .first()
        )
        if usub is not None:
            return {
                "scope": "user",
                "user_id": usub.user_id,
                "subscription_id": usub.id,
                "tier_key": usub.tier_key,
            }
    # Metadata fallback (sessions created by our checkout flows).
    scope = meta.get("scope")
    if scope == "user" and meta.get("user_id"):
        return {"scope": "user", "user_id": int(meta["user_id"]), "tier_key": meta.get("tier_key")}
    if meta.get("group_id"):
        fields = {"scope": "group", "group_id": int(meta["group_id"]), "tier_key": meta.get("tier_key")}
        if meta.get("payer_user_id"):
            fields["user_id"] = int(meta["payer_user_id"])
        return fields
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = parser.parse_args()

    import stripe

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        print("STRIPE_SECRET_KEY unset — nothing to backfill.")
        return 1

    imported = skipped = unmatched = 0
    params = {"status": "paid", "limit": 100}
    while True:
        page = stripe.Invoice.list(**params)
        # stripe-python 15.x objects aren't dicts (no .get) — go through their
        # JSON form so the rest of the loop can use plain dict access.
        page_json = json.loads(str(page))
        data = page_json.get("data") or []
        if not data:
            break
        for inv in data:
            amount = inv.get("amount_paid")
            if not amount:
                continue
            prov_sub = inv.get("subscription")
            if not prov_sub:
                details = (inv.get("parent") or {}).get("subscription_details") or {}
                prov_sub = details.get("subscription")
            meta = inv.get("metadata") or {}
            if not meta and prov_sub:
                # Subscription metadata carries our scope/ids for our flows.
                try:
                    sub_json = json.loads(str(stripe.Subscription.retrieve(prov_sub)))
                    meta = sub_json.get("metadata") or {}
                except Exception:
                    meta = {}

            with db_session() as s:
                attribution = _resolve_attribution(s, prov_sub, meta)
            if attribution is None:
                unmatched += 1
                print(f"  unmatched invoice {inv.get('id')} (sub={prov_sub})")
                continue

            paid_ts = ((inv.get("status_transitions") or {}).get("paid_at")) or inv.get("created")
            if args.dry_run:
                print(f"  would import {inv.get('id')}: {amount} {inv.get('currency')} -> {attribution}")
                imported += 1
                continue
            inserted = record_payment(
                provider="stripe",
                amount_cents=int(amount),
                currency=inv.get("currency") or "USD",
                external_id=str(inv.get("id")),
                paid_at=datetime.fromtimestamp(int(paid_ts)) if paid_ts else None,
                **attribution,
            )
            if inserted:
                imported += 1
            else:
                skipped += 1

        if not page_json.get("has_more"):
            break
        params["starting_after"] = data[-1]["id"]

    print(f"done: imported={imported} duplicates={skipped} unmatched={unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
