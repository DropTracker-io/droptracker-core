"""Create Stripe Products/Prices for tiers that still have no provider_price_id.

Why this exists
---------------
``web_api/routes/admin.py::create_tier`` calls ``billing.ensure_provider_price``
so *new* tiers get a Stripe Price automatically — but ``update_tier`` does not,
and the three tiers seeded before ``STRIPE_SECRET_KEY`` was configured
(``basic``, ``t2``, ``t3``) were created while Stripe was unconfigured, so they
still have ``provider_price_id = NULL``. Checkout for them
(``web_api/billing.py::start_checkout``) will fail with a Stripe error until
this is backfilled.

Idempotent: ``ensure_provider_price`` itself no-ops for tiers that already
have a price id, so re-running is always safe.

Usage
-----
    # dry run (default) — reports which tiers are missing a price, creates nothing
    venv/bin/python scripts/backfill_stripe_prices.py

    # create Products/Prices in Stripe and persist the ids
    venv/bin/python scripts/backfill_stripe_prices.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from db import Session, SubscriptionTier
from web_api import billing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="create prices in Stripe (default: dry run)")
    args = parser.parse_args()

    if billing.provider_name() != "stripe":
        print("STRIPE_SECRET_KEY is not configured (or the stripe package is missing) — nothing to do.")
        return

    with Session() as s:
        tiers = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.active == True, SubscriptionTier.provider_price_id.is_(None))  # noqa: E712
            .all()
        )
        if not tiers:
            print("Every active tier already has a provider_price_id. Nothing to do.")
            return

        for tier in tiers:
            print(f"  {tier.key:<8} {tier.name:<12} {tier.price_cents / 100:.2f} {tier.currency}/{tier.interval}")

        if not args.apply:
            print(f"\nDry run — {len(tiers)} tier(s) would get a new Stripe Product+Price. Re-run with --apply.")
            return

        for tier in tiers:
            price_id = billing.ensure_provider_price(tier)
            if not price_id:
                print(f"  ! {tier.key}: ensure_provider_price returned nothing (check Stripe API logs)")
                continue
            tier.provider_price_id = price_id
            print(f"  + {tier.key}: {price_id}")
        s.commit()
        print("\nApplied.")


if __name__ == "__main__":
    main()
