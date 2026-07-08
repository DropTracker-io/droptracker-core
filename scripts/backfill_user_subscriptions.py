"""Backfill ``data.user_subscriptions`` from XenForo *user* upgrades.

Why this exists
---------------
XenForo sold recurring per-user upgrades (``xf_user_upgrade_active``, e.g.
"1500 points" at $7.50/mo) through PayPal subscription agreements that share
the same ``payment_callback.php`` notification URL as the group upgrades.
After the cutover those IPNs reached ``web_api/routes/paypal_ipn.py`` but
matched no row ("likely a legacy user upgrade") and were dropped — the users
kept paying with no tracked benefit.

This script mirrors those live agreements into ``data.user_subscriptions`` on
the user-scoped ``supporter`` tier so:
  * the PayPal IPN handler can match + renew them (subscr_id / custom keys),
  * the users receive supporter entitlements (submission DMs, flair).

Note the legacy product granted monthly premium *points*; the new supporter
tier grants personal perks instead. The mapped tier is intentionally the
closest current equivalent — review the plan output before ``--apply``.

XF → DT user mapping uses ``xf_user.external_id``, which stores the
DropTracker ``users.user_id`` (the same linkage ``db/xf/upgrades.py`` relies
on). Rows whose XF user has no linked DropTracker user are reported and
skipped.

Usage
-----
    # dry run (default) — prints the plan, writes nothing
    venv/bin/python scripts/backfill_user_subscriptions.py

    # write to data.user_subscriptions
    venv/bin/python scripts/backfill_user_subscriptions.py --apply

Idempotent: re-running converges rows to XF truth. Rows for providers other
than paypal/manual (e.g. stripe) are never touched.
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

from db.models import Session, XenforoSession, User, UserSubscription

# Every legacy XF user upgrade maps to the single supporter tier.
TIER_KEY = "supporter"


def _decode(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def load_xf_records() -> list[dict]:
    """One winning upgrade record per XF user, with the PayPal subscriber id."""
    with XenforoSession() as xf:
        rows = xf.execute(text("""
            SELECT a.user_upgrade_record_id, a.user_id, a.user_upgrade_id,
                   a.purchase_request_key, a.start_date, a.end_date, a.is_cancelled,
                   u.external_id AS dt_user_id,
                   (
                       SELECT l.subscriber_id
                       FROM xf_payment_provider_log l
                       WHERE l.purchase_request_key = a.purchase_request_key
                         AND l.subscriber_id != ''
                       ORDER BY l.log_date DESC LIMIT 1
                   ) AS subscriber_id
            FROM xf_user_upgrade_active a
            LEFT JOIN xf_user u ON u.user_id = a.user_id
        """)).mappings().all()

    by_user: dict[int, dict] = {}
    for row in rows:
        r = dict(row)
        r["purchase_request_key"] = _decode(r["purchase_request_key"])
        r["subscriber_id"] = _decode(r["subscriber_id"])
        cur = by_user.get(r["user_id"])
        # Furthest end_date wins; prefer non-cancelled paid records on ties.
        if (
            cur is None
            or (r["end_date"] or 0) > (cur["end_date"] or 0)
            or (
                r["end_date"] == cur["end_date"]
                and (not r["is_cancelled"], bool(r["purchase_request_key"]))
                > (not cur["is_cancelled"], bool(cur["purchase_request_key"]))
            )
        ):
            by_user[r["user_id"]] = r
    return sorted(by_user.values(), key=lambda r: r["user_id"])


def plan_changes(records: list[dict]) -> list[tuple[str, dict, UserSubscription | None]]:
    """Return (action, desired_fields, existing_row) per XF user."""
    now = datetime.now()
    plan = []
    with Session() as s:
        for r in records:
            dt_user = None
            try:
                dt_user_id = int(_decode(r.get("dt_user_id")) or 0)
            except (TypeError, ValueError):
                dt_user_id = 0
            if dt_user_id:
                dt_user = s.query(User).filter(User.user_id == dt_user_id).first()
            if dt_user is None:
                plan.append((
                    "SKIP no linked DT user",
                    {"xf_user_id": r["user_id"], "purchase_request_key": r["purchase_request_key"]},
                    None,
                ))
                continue

            # end_date=0 means a non-expiring (comped) upgrade.
            end = datetime.fromtimestamp(r["end_date"]) if r["end_date"] else None
            desired = {
                "user_id": dt_user.user_id,
                "tier_key": TIER_KEY,
                "status": "active" if (end is None or end > now) else "expired",
                "provider": "paypal" if r["purchase_request_key"] else "manual",
                "provider_subscription_id": r["subscriber_id"],
                "provider_customer_id": r["purchase_request_key"],
                "current_period_end": end,
                "cancel_at_period_end": bool(r["is_cancelled"]),
            }

            existing = (
                s.query(UserSubscription)
                .filter(UserSubscription.user_id == dt_user.user_id)
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
                    if k != "user_id" and getattr(existing, k) != v
                }
                plan.append(("UPDATE" if diffs else "OK (no change)", desired, existing))
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    records = load_xf_records()
    print(f"XF live user upgrade records (deduped): {len(records)}\n")

    plan = plan_changes(records)
    for action, desired, existing in plan:
        if action.startswith("SKIP") and "user_id" not in desired:
            print(f"  xf_user {desired['xf_user_id']:>4}  {action}: {desired}")
            continue
        uid = desired["user_id"]
        end = desired["current_period_end"]
        print(
            f"  user {uid:>5}  {action:<15} tier={desired['tier_key']:<9} "
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
                s.query(UserSubscription)
                .filter(UserSubscription.user_id == desired["user_id"])
                .first()
            )
            if sub is None:
                sub = UserSubscription(user_id=desired["user_id"])
                s.add(sub)
            for k, v in desired.items():
                if k != "user_id":
                    setattr(sub, k, v)
            written += 1
        s.commit()
    print(f"\nApplied: {written} row(s) written to data.user_subscriptions.")


if __name__ == "__main__":
    main()
