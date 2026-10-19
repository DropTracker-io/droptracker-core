#!/usr/bin/env python3
"""
Give website admin grants to admins who only exist on the bot's list
====================================================================

An admin added by Discord ID before they had a DropTracker account was written
to the group's ``authed_users`` config only, so the website never let them in
(ticket 479). Sign-in now promotes those entries (``web_api/admin_grants.py``);
this does the same for everyone stranded before that, without waiting for them
to sign in again.

Only people who already have a ``users`` row can be granted anything. Entries
for Discord IDs with no account are listed and left for the sign-in hook.

Usage:
    python scripts/backfill_pending_group_admins.py                 # dry run
    python scripts/backfill_pending_group_admins.py --group 339     # one group
    python scripts/backfill_pending_group_admins.py --apply         # write grants
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from db import Group, GroupAdmin, GroupConfiguration, User  # noqa: E402
from db.models.base import Session  # noqa: E402
from web_api.admin_grants import (  # noqa: E402
    AUTHED_USERS_KEY,
    grant_pending,
    row_authed_ids,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--group", type=int, action="append", help="limit to these group ids")
    ap.add_argument("--apply", action="store_true", help="write the grants")
    args = ap.parse_args()

    s = Session()
    try:
        q = s.query(GroupConfiguration).filter(GroupConfiguration.config_key == AUTHED_USERS_KEY)
        if args.group:
            q = q.filter(GroupConfiguration.group_id.in_(args.group))
        rows = q.all()

        live = {gid: name for gid, name in s.query(Group.group_id, Group.group_name).all()}
        wanted: dict[str, set[int]] = defaultdict(set)  # discord_id -> group ids
        for row in rows:
            if row.group_id not in live:
                continue
            for did in row_authed_ids(row):
                if did.isdigit():
                    wanted[did].add(row.group_id)

        ids = list(wanted)
        users: dict[str, User] = {}
        for i in range(0, len(ids), 500):
            for u in s.query(User).filter(User.discord_id.in_(ids[i:i + 500])).all():
                users[str(u.discord_id)] = u

        granted = defaultdict(set)  # user_id -> group ids with a grant already
        uids = [u.user_id for u in users.values()]
        for i in range(0, len(uids), 500):
            for gid, uid in (
                s.query(GroupAdmin.group_id, GroupAdmin.user_id)
                .filter(GroupAdmin.user_id.in_(uids[i:i + 500]))
                .all()
            ):
                granted[uid].add(gid)

        plan: list[tuple[User, list[int]]] = []
        no_account = 0
        for did, gids in sorted(wanted.items()):
            u = users.get(did)
            if u is None:
                no_account += len(gids)
                continue
            missing = sorted(gids - granted[u.user_id])
            if missing:
                plan.append((u, missing))

        total = sum(len(g) for _, g in plan)
        print(f"authed_users rows scanned: {len(rows)}")
        print(f"entries with no DropTracker account (left for sign-in): {no_account}")
        print(f"grants to add: {total} across {len(plan)} users")
        for u, gids in plan:
            names = ", ".join(f"{g}:{live.get(g)}" for g in gids)
            print(f"  user {u.user_id} {u.username!r} discord={u.discord_id} -> {names}")

        if not args.apply:
            print("\ndry run; re-run with --apply to write")
            s.rollback()
            return 0

        for u, gids in plan:
            grant_pending(s, u, gids)
        s.commit()
        print(f"\napplied {total} grants")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
