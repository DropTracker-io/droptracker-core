"""Give every group explicit rows for the notify settings an absent row used to
misreport (ticket 495).

Until this fix, a group with no row for ``notify_pbs`` / ``notify_clogs`` /
``notify_cas`` / ``notify_pets`` / ``min_ca_tier_to_notify`` saw the registry
default on the website (on, and Easy), while the processors and the plugin
treated the same absent row as off. The readers now use the registry default
(``utils.group_config.get_effective``). This script writes the rows first so
the switch is decided per group, not by accident:

* a group whose admins have edited its settings on the site (any
  ``config.update`` audit row), and the template group 1 that new groups are
  cloned from, get the value the site showed them: the registry default;
* every other group gets the value it has actually been running with, off,
  so its notifications do not change. The CA tier is the exception: it only
  matters while CA notifications are on, and a group that explicitly turned
  them on gets Easy, the tier the site showed.

Run it BEFORE restarting services on the new readers.

    venv/bin/python -m scripts.backfill_config_absent_defaults            # dry run
    venv/bin/python -m scripts.backfill_config_absent_defaults --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import group_config as gc  # noqa: E402

TEMPLATE_GROUP_ID = 1
TOGGLES = (gc.NOTIFY_PBS, gc.NOTIFY_CLOGS, gc.NOTIFY_CAS, gc.NOTIFY_PETS)
TIER = gc.MIN_CA_TIER_TO_NOTIFY
BASE_KEYS = TOGGLES + (TIER,)
KEYS = BASE_KEYS + tuple(gc.SEASONAL_PREFIX + k for k in BASE_KEYS)


def plan_rows(group_ids, stored, visited):
    """Rows to insert: ``[(group_id, key, value, reason)]``.

    ``stored`` maps ``(group_id, key)`` to the value of an existing row;
    ``visited`` is the set of groups edited on the site. Pure, for tests.
    """
    out = []
    for gid in group_ids:
        on_site = gid in visited or gid == TEMPLATE_GROUP_ID
        for key in KEYS:
            if (gid, key) in stored:
                continue
            default = gc.absent_default(key)
            base = key[len(gc.SEASONAL_PREFIX):] if key.startswith(gc.SEASONAL_PREFIX) else key
            if on_site:
                out.append((gid, key, default, "site"))
            elif base == TIER:
                cas_key = key[: -len(TIER)] + gc.NOTIFY_CAS
                cas_on = gc.is_truthy(stored.get((gid, cas_key)))
                out.append((gid, key, default, "cas-on" if cas_on else "kept"))
            else:
                out.append((gid, key, "false", "kept"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", type=int, action="append", default=[],
                    help="print the planned rows for this group id (repeatable)")
    args = ap.parse_args()

    from sqlalchemy import text
    from db.models import GroupConfiguration, session

    try:
        group_ids = [r[0] for r in session.execute(text("SELECT group_id FROM `groups`")).all()]
        stored = {
            (r.group_id, r.config_key): r.config_value
            for r in session.query(GroupConfiguration.group_id, GroupConfiguration.config_key,
                                   GroupConfiguration.config_value)
            .filter(GroupConfiguration.config_key.in_(KEYS)).all()
        }
        visited = {r[0] for r in session.execute(text(
            "SELECT DISTINCT group_id FROM audit_log "
            "WHERE action = 'config.update' AND group_id IS NOT NULL")).all()}

        rows = plan_rows(group_ids, stored, visited)
        print(f"groups {len(group_ids)}, edited on the site {len(visited & set(group_ids))}, "
              f"rows to add {len(rows)}")
        by = Counter((key, value, reason) for _, key, value, reason in rows)
        for (key, value, reason), n in sorted(by.items()):
            print(f"  {key:<34} {value:<6} {reason:<7} {n}")
        turned_on = sorted({gid for gid, key, value, reason in rows
                            if reason in ("site", "cas-on") and gc.is_truthy(value)
                            and not key.startswith(gc.SEASONAL_PREFIX)})
        print(f"groups that start getting a notification type they were not: {len(turned_on)}")
        for gid in args.show:
            for r in rows:
                if r[0] == gid:
                    print("   ", r)
        if not args.apply:
            print("(dry run)")
            return 0
        for gid, key, value, _ in rows:
            session.add(GroupConfiguration(group_id=gid, config_key=key, config_value=value))
        session.commit()
        print(f"inserted {len(rows)} rows")
        return 0
    finally:
        session.rollback()
        session.remove()


if __name__ == "__main__":
    sys.exit(main())
