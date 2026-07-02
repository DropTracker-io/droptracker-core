"""Seed ``group_admins`` for existing group owners (backend Task 08).

On rollout, every group's current owner(s) must keep admin access on the new
website. Owners are recorded today in each group's ``authed_users``
GroupConfiguration row (a JSON array of Discord user ids), which
``db.group_creation.create_web_group`` and the Discord ``/create-group`` command
both populate.

This script maps those Discord ids to ``users.user_id`` and inserts an
``owner``-role ``group_admins`` row for each (idempotent — skips existing rows).

MANAGE_GUILD-based ownership is resolved live at request time from the cached
Discord guilds (see ``web_api/deps.py``); it can't be seeded offline.

Run:
    venv/bin/python -m scripts.seed_group_admins            # apply
    venv/bin/python -m scripts.seed_group_admins --dry-run  # preview
"""
from __future__ import annotations

import argparse
import json
import sys

from db.models import GroupAdmin, GroupConfiguration, User, session


def _iter_owner_discord_ids(cfg_value: str):
    if not cfg_value:
        return []
    try:
        data = json.loads(cfg_value)
    except (ValueError, TypeError):
        # Legacy single-id string.
        return [str(cfg_value).strip()] if str(cfg_value).strip() else []
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    if isinstance(data, (str, int)):
        return [str(data).strip()]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed group_admins for existing owners.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = parser.parse_args()

    rows = (
        session.query(GroupConfiguration)
        .filter(GroupConfiguration.config_key == "authed_users")
        .all()
    )

    inserted = 0
    skipped = 0
    unresolved = 0

    for row in rows:
        discord_ids = _iter_owner_discord_ids(row.config_value or row.long_value)
        for discord_id in discord_ids:
            user = session.query(User).filter(User.discord_id == str(discord_id)).first()
            if not user:
                unresolved += 1
                continue
            existing = (
                session.query(GroupAdmin)
                .filter(
                    GroupAdmin.group_id == row.group_id,
                    GroupAdmin.user_id == user.user_id,
                )
                .first()
            )
            if existing:
                skipped += 1
                continue
            print(f"  + group {row.group_id} owner user {user.user_id} (discord {discord_id})")
            if not args.dry_run:
                session.add(
                    GroupAdmin(
                        group_id=row.group_id,
                        user_id=user.user_id,
                        role="owner",
                        granted_by=None,
                    )
                )
                inserted += 1

    if not args.dry_run:
        session.commit()

    print(
        f"\nDone. inserted={inserted} skipped_existing={skipped} "
        f"unresolved_discord_ids={unresolved} (dry_run={args.dry_run})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
