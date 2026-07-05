"""Backfill ``data.group_subscriptions`` from the XenForo group-upgrade addon.

Why this exists
---------------
Group premium tiers were sold through the XenForo ``DropTracker/Upgrades``
addon (``xf_dt_group_upgrade_active``), with recurring billing handled by
PayPal subscription agreements that notify XenForo's ``payment_callback.php``.
The new stack reads entitlements from ``data.group_subscriptions`` (Task 11),
so live XF subscriptions must be mirrored there or paying groups lose their
benefits on the Next.js front-end.

Renewals after cutover are handled by the Web API's PayPal IPN endpoint
(``web_api/routes/paypal_ipn.py``), which matches incoming IPNs against the
``provider_subscription_id`` / ``provider_customer_id`` values written here.

What it does
------------
For each group in ``xf_dt_group_upgrade_active`` (group_ids are identical
across the ``xenforo`` and ``data`` schemas):

  * keeps only the record with the furthest ``end_date`` (groups can carry
    superseded rows, e.g. a cancelled tier next to its replacement),
  * maps XF tier ids to new tier keys: 1→basic, 2→t2, 8→t3,
  * resolves the PayPal subscription id (``I-…``) from
    ``xf_payment_provider_log.subscriber_id``,
  * upserts one ``group_subscriptions`` row:
      - provider:                 'paypal' (has a purchase request) or 'manual'
      - provider_subscription_id: PayPal subscr_id  (IPN match key)
      - provider_customer_id:     XF purchase_request_key (IPN ``custom`` field,
                                  fallback match key — see paypal_ipn.py)
      - status:                   'active', or 'expired' if end_date has passed
      - current_period_end:       XF end_date
      - cancel_at_period_end:     XF is_cancelled

Usage
-----
    # dry run (default) — prints the plan, writes nothing
    venv/bin/python scripts/backfill_group_subscriptions.py

    # write to data.group_subscriptions
    venv/bin/python scripts/backfill_group_subscriptions.py --apply

Idempotent: re-running converges rows to XF truth. Re-run right before the
domain cutover so renewals XenForo processed in the meantime are captured.
Rows for providers other than paypal/manual (e.g. stripe) are never touched.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text

from db.models import Session, XenforoSession, GroupSubscription

TIER_MAP = {1: "basic", 2: "t2", 8: "t3"}


def load_xf_records() -> list[dict]:
    """One winning upgrade record per group, with the PayPal subscriber id."""
    with XenforoSession() as xf:
        rows = xf.execute(text("""
            SELECT a.group_upgrade_record_id, a.group_id, a.group_upgrade_id,
                   a.purchase_request_key, a.start_date, a.end_date, a.is_cancelled,
                   (
                       SELECT l.subscriber_id
                       FROM xf_payment_provider_log l
                       WHERE l.purchase_request_key = a.purchase_request_key
                         AND l.subscriber_id != ''
                       ORDER BY l.log_date DESC LIMIT 1
                   ) AS subscriber_id
            FROM xf_dt_group_upgrade_active a
        """)).mappings().all()

    by_group: dict[int, dict] = {}
    for row in rows:
        r = dict(row)
        cur = by_group.get(r["group_id"])
        # Furthest end_date wins; prefer non-cancelled paid records on ties.
        if (
            cur is None
            or r["end_date"] > cur["end_date"]
            or (
                r["end_date"] == cur["end_date"]
                and (not r["is_cancelled"], bool(r["purchase_request_key"]))
                > (not cur["is_cancelled"], bool(cur["purchase_request_key"]))
            )
        ):
            by_group[r["group_id"]] = r
    return sorted(by_group.values(), key=lambda r: r["group_id"])


def plan_changes(records: list[dict]) -> list[tuple[str, dict, GroupSubscription | None]]:
    """Return (action, desired_fields, existing_row) per group."""
    now = datetime.now()
    plan = []
    with Session() as s:
        for r in records:
            tier_key = TIER_MAP.get(r["group_upgrade_id"])
            if tier_key is None:
                plan.append(("SKIP unknown tier", {"group_id": r["group_id"], "xf_tier": r["group_upgrade_id"]}, None))
                continue

            end = datetime.fromtimestamp(r["end_date"]) if r["end_date"] else None
            desired = {
                "group_id": r["group_id"],
                "tier_key": tier_key,
                "status": "active" if (end is None or end > now) else "expired",
                "provider": "paypal" if r["purchase_request_key"] else "manual",
                "provider_subscription_id": r["subscriber_id"],
                "provider_customer_id": r["purchase_request_key"],
                "current_period_end": end,
                "cancel_at_period_end": bool(r["is_cancelled"]),
            }

            existing = (
                s.query(GroupSubscription)
                .filter(GroupSubscription.group_id == r["group_id"])
                .first()
            )
            if existing and existing.provider not in (None, "paypal", "manual"):
                plan.append(("SKIP non-migratable provider", desired, existing))
                continue

            if existing is None:
                plan.append(("INSERT", desired, None))
            else:
                diffs = {
                    k: (getattr(existing, k), v)
                    for k, v in desired.items()
                    if k != "group_id" and getattr(existing, k) != v
                }
                plan.append(("UPDATE" if diffs else "OK (no change)", desired, existing))
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    records = load_xf_records()
    print(f"XF live group upgrade records (deduped): {len(records)}\n")

    plan = plan_changes(records)
    for action, desired, existing in plan:
        gid = desired["group_id"]
        if action.startswith("SKIP"):
            print(f"  group {gid:>4}  {action}: {desired}")
            continue
        end = desired["current_period_end"]
        print(
            f"  group {gid:>4}  {action:<15} tier={desired['tier_key']:<5} "
            f"status={desired['status']:<7} provider={desired['provider']:<6} "
            f"subscr_id={desired['provider_subscription_id'] or '-':<16} "
            f"period_end={end.strftime('%Y-%m-%d %H:%M') if end else '-'} cancel_at_end={desired['cancel_at_period_end']}"
        )
        if existing is not None and action == "UPDATE":
            for k in ("tier_key", "status", "provider", "provider_subscription_id",
                      "provider_customer_id", "current_period_end", "cancel_at_period_end"):
                old, new = getattr(existing, k), desired[k]
                if old != new:
                    print(f"               {k}: {old!r} -> {new!r}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to persist.")
        return

    written = 0
    with Session() as s:
        for action, desired, _ in plan:
            if action not in ("INSERT", "UPDATE"):
                continue
            sub = (
                s.query(GroupSubscription)
                .filter(GroupSubscription.group_id == desired["group_id"])
                .first()
            )
            if sub is None:
                sub = GroupSubscription(group_id=desired["group_id"])
                s.add(sub)
            for k, v in desired.items():
                if k != "group_id":
                    setattr(sub, k, v)
            written += 1
        s.commit()
    print(f"\nApplied: {written} row(s) written to data.group_subscriptions.")


if __name__ == "__main__":
    main()
