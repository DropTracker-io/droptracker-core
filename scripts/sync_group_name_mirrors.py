"""Re-sync every copy of a group's name to the canonical ``groups.group_name``.

Why this exists
---------------
The website's group settings editor wrote its "Group name" field to
``group_configurations.group_name`` and nothing else, so saving it never
renamed anything — every surface reads the ``groups.group_name`` column. The
forum mirror (``xenforo.dt_player_group.group_name``) was likewise written once
at group creation and never updated afterwards. Both are now kept in step by
``db/group_rename.py``; this heals the rows that drifted before that existed.

Direction of the sync is deliberately **column -> mirrors**. The column is what
the site, Discord and the plugin have been displaying all along, so aligning
the mirrors to it changes nothing anyone can see. The reverse would silently
rename live clans to strings their admins typed months ago and never saw take
effect — those are re-applied by the admin saving the settings page again, not
by this script.

Usage:
    venv/bin/python -m scripts.sync_group_name_mirrors            # dry run
    venv/bin/python -m scripts.sync_group_name_mirrors --apply    # write
"""
from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import text

from db.models import session
from db.models.group import Group
from db.models.group_configuration import GroupConfiguration
from db.group_rename import GROUP_NAME_CONFIG_KEY


def _config_rows() -> dict[int, GroupConfiguration]:
    rows = (
        session.query(GroupConfiguration)
        .filter(GroupConfiguration.config_key == GROUP_NAME_CONFIG_KEY)
        .all()
    )
    return {r.group_id: r for r in rows}


def _xf_names() -> dict[int, str]:
    try:
        rows = session.execute(
            text("SELECT group_id, group_name FROM xenforo.dt_player_group")
        ).all()
    except Exception as exc:  # noqa: BLE001
        print(f"! XenForo mirror unreadable ({exc}); skipping that half.")
        return {}
    return {int(gid): name for gid, name in rows}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument(
        "--create-missing-config-rows",
        action="store_true",
        help="also seed a group_name config row for groups that have none "
        "(not needed: the settings editor now reads the column directly)",
    )
    args = ap.parse_args()

    groups = session.query(Group).order_by(Group.group_id).all()
    config_rows = _config_rows()
    xf_names = _xf_names()
    now_ts = int(datetime.now().timestamp())

    config_fixed = config_seeded = xf_fixed = 0

    for group in groups:
        name = (group.group_name or "").strip()
        if not name:
            print(f"  skip group {group.group_id}: column has no name")
            continue

        row = config_rows.get(group.group_id)
        if row is None:
            if args.create_missing_config_rows:
                print(f"  group {group.group_id}: seed config row -> {name!r}")
                if args.apply:
                    session.add(
                        GroupConfiguration(
                            group_id=group.group_id,
                            config_key=GROUP_NAME_CONFIG_KEY,
                            config_value=name,
                        )
                    )
                config_seeded += 1
        elif (row.config_value or "") != name:
            print(
                f"  group {group.group_id}: config {row.config_value!r} -> {name!r} "
                f"(never-applied edit discarded)"
            )
            if args.apply:
                row.config_value = name
            config_fixed += 1

        xf_name = xf_names.get(group.group_id)
        if xf_name is not None and xf_name != name:
            print(f"  group {group.group_id}: forum {xf_name!r} -> {name!r}")
            if args.apply:
                session.execute(
                    text(
                        "UPDATE xenforo.dt_player_group "
                        "SET group_name = :name, date_updated = :ts "
                        "WHERE group_id = :gid"
                    ),
                    {"name": name, "ts": now_ts, "gid": group.group_id},
                )
            xf_fixed += 1

    summary = (
        f"{config_fixed} config row(s) re-synced, "
        f"{config_seeded} seeded, {xf_fixed} forum row(s) re-synced"
    )
    if args.apply:
        session.commit()
        print(f"Applied: {summary}.")
    else:
        session.rollback()
        print(f"[dry-run] Would apply: {summary}. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
