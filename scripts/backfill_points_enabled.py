"""Roll out free clan points without switching points on for every clan.

Points used to be gated by the ``custom_points`` entitlement alone (Sponsor and
up). It is now on every tier, so a clan's own ``points_enabled`` switch
(``db/point_standings.ENABLED_CONFIG_KEY``) says whether it uses points. Two
steps, in this order:

1. ``enable-existing`` -- BEFORE the tiers change: switch points on for every
   clan that is entitled to points right now and has used them (seeded rules
   or ledger rows), so nobody who pays for points loses them. Writes only
   ``group_configurations`` rows; inert until the new gate is running.
2. ``grant-free`` -- AFTER every process that awards points runs the new gate
   (webhook-consumer, droptracker-api, droptracker-webhooks, webapi, core):
   set ``custom_points`` on every active group tier. Run it earlier and the
   old gate (entitlement only) starts every clan on default rules.

Dry run (default):  venv/bin/python -m scripts.backfill_points_enabled enable-existing
Write:              venv/bin/python -m scripts.backfill_points_enabled enable-existing --apply
                    venv/bin/python -m scripts.backfill_points_enabled grant-free --apply
"""

from __future__ import annotations

import argparse
import json
import sys


def _used_points_groups(session) -> list[int]:
    from sqlalchemy import text

    rows = session.execute(
        text(
            "SELECT group_id FROM group_point_settings "
            "UNION SELECT DISTINCT group_id FROM player_points WHERE group_id IS NOT NULL"
        )
    ).fetchall()
    return sorted({int(r[0]) for r in rows if r[0] is not None and int(r[0]) != 2})


def enable_existing(session, apply: bool) -> int:
    from sqlalchemy import text

    from db.entitlements import resolve_group_entitlements
    from db.point_standings import ENABLED_CONFIG_KEY

    candidates = _used_points_groups(session)
    already = {
        int(gid)
        for (gid,) in session.execute(
            text("SELECT DISTINCT group_id FROM group_configurations WHERE config_key = :k"),
            {"k": ENABLED_CONFIG_KEY},
        ).fetchall()
    }
    to_enable = []
    for gid in candidates:
        if gid in already:
            continue
        try:
            if resolve_group_entitlements(session, gid).get("custom_points"):
                to_enable.append(gid)
        except Exception as exc:
            print(f"group {gid}: entitlement lookup failed ({exc}); skipped", file=sys.stderr)
    print(f"{len(candidates)} clans have used points; {len(already)} already carry the switch; "
          f"{len(to_enable)} entitled now -> switch ON: {to_enable}")
    if apply and to_enable:
        for gid in to_enable:
            session.execute(
                text("INSERT INTO group_configurations (group_id, config_key, config_value) "
                     "VALUES (:g, :k, '1')"),
                {"g": gid, "k": ENABLED_CONFIG_KEY},
            )
        session.commit()
        print(f"wrote {len(to_enable)} rows")
    return 0


def grant_free(session, apply: bool) -> int:
    from db.entitlements import parse_stored_entitlements
    from db.models.subscriptions import SubscriptionTier

    tiers = (
        session.query(SubscriptionTier)
        .filter(SubscriptionTier.scope == "group", SubscriptionTier.active == True)  # noqa: E712
        .all()
    )
    changed = []
    for tier in tiers:
        stored = parse_stored_entitlements(tier.entitlements)
        if stored.get("custom_points") is True:
            continue
        stored["custom_points"] = True
        changed.append(tier.key)
        if apply:
            tier.entitlements = json.dumps(stored)
    print(f"tiers gaining custom_points: {changed or 'none'}")
    if apply and changed:
        session.commit()
        print("committed; entitlement caches expire within 60s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("enable-existing", "grant-free"))
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = parser.parse_args()

    from db.models.base import Session

    session = Session()
    try:
        if args.step == "enable-existing":
            return enable_existing(session, args.apply)
        return grant_free(session, args.apply)
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
