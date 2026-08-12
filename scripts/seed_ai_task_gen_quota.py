"""Seed the per-tier ``ai_task_gen_daily`` allowance.

The registry default (5/day) already covers the free tier, so this only writes
the paid tiers. Idempotent: a tier that already carries an explicit value is
left alone, so re-running after a superadmin has tuned a number on
/admin/tiers will not stomp their change.

Dry-run by default; pass --apply to write.

    ./venv/bin/python -m scripts.seed_ai_task_gen_quota
    ./venv/bin/python -m scripts.seed_ai_task_gen_quota --apply
"""
from __future__ import annotations

import argparse
import json
import sys

# tier key -> daily generations. "free" is intentionally absent: the registry
# default (5) supplies it, so an operator lowering the default doesn't get
# silently overridden by a row written here.
TARGETS = {
    "basic": 25,
    "t2": 75,
    "t3": 200,
}

KEY = "ai_task_gen_daily"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    args = ap.parse_args()

    from db.models import Session, SubscriptionTier

    changed, skipped, missing = [], [], []
    with Session() as s:
        for tier_key, value in TARGETS.items():
            tier = s.query(SubscriptionTier).filter(SubscriptionTier.key == tier_key).first()
            if tier is None:
                missing.append(tier_key)
                continue
            try:
                ents = json.loads(tier.entitlements or "{}")
            except (TypeError, ValueError):
                ents = {}
            if not isinstance(ents, dict):
                ents = {}
            if KEY in ents:
                skipped.append((tier_key, ents[KEY]))
                continue
            ents[KEY] = value
            changed.append((tier_key, value))
            if args.apply:
                tier.entitlements = json.dumps(ents)
        if args.apply and changed:
            s.commit()

    for k, v in changed:
        print(f"{'SET' if args.apply else 'would set'} {k}.{KEY} = {v}")
    for k, v in skipped:
        print(f"skip {k}: already set to {v}")
    for k in missing:
        print(f"WARN no such tier: {k}")

    if args.apply and changed:
        from db.entitlements import invalidate_entitlement_cache

        invalidate_entitlement_cache()
        print("entitlement cache invalidated")
    if not args.apply:
        print("\n(dry run — pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
