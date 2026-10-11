"""Permanently delete a group and everything scoped to it.

For a clan that has left DropTracker (WOM group / Discord server gone) and
asked to be removed. There is no in-app path for this: every FK onto
``groups`` is ON DELETE RESTRICT, so the children have to go first.

What goes, in one DB transaction:

* the group's own events, via ``_cascade_delete_event`` (the same teardown the
  site's "Delete event" uses);
* every row in a table with an FK onto ``groups.group_id`` (discovered from
  information_schema, so new group tables are picked up automatically);
* staff-notice chat threads, and the group's party rows on any other thread;
* group-keyed tables without an FK: ``clan_log_firsts``,
  ``group_point_settings``, ``wom_group_member_ids.group_dt_id``,
  ``recap_snapshots`` / ``recap_deliveries`` (scope clan_log/group),
  ``api_key_reveals.audience_group_id``;
* the ``groups`` row itself.

Then, after the commit: the group's Redis boards
(``leaderboard:group:{gid}:*``, ``leaderboard:*:group:{gid}``), its member
entry in each ``gleaderboard:{partition}``, the uploaded icon and the
``static/assets/img/clans/{gid}/`` asset tree.

Players, their drops and their per-player Redis totals stay: those belong to
the players, not the clan. Paid clan points (``player_points``) are not
group-keyed and stay too.

Refuses to run if the group participates in another group's event (that needs
a team withdrawal, not a wipe) or if ``--expect-name`` doesn't match.

Dry-run by default; pass --apply to write.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.purge_group 283 --expect-name "Anzac Nomads" [--apply]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db.models.base import Session  # noqa: E402

DISC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(DISC, "static", "assets", "img", "group-icons")
CLAN_DIR = os.path.join(DISC, "static", "assets", "img", "clans")

# Group-keyed tables with no FK onto groups: (table, column, extra WHERE).
UNLINKED = [
    ("clan_log_firsts", "group_id", ""),
    ("group_point_settings", "group_id", ""),
    ("wom_group_member_ids", "group_dt_id", ""),
    ("recap_snapshots", "subject_id", "AND scope IN ('clan_log', 'group')"),
    ("recap_deliveries", "subject_id", "AND scope IN ('clan_log', 'group')"),
    ("api_key_reveals", "audience_group_id", ""),
]


def _fk_tables(s) -> list[tuple[str, str]]:
    rows = s.execute(text(
        "SELECT table_name, column_name FROM information_schema.key_column_usage "
        "WHERE table_schema = DATABASE() AND referenced_table_name = 'groups' "
        "AND referenced_column_name = 'group_id'"
    )).all()
    return sorted({(t, c) for t, c in rows if t != "groups"})


def _count(s, table: str, col: str, gid: int, extra: str = "") -> int:
    return s.execute(
        text(f"SELECT COUNT(*) FROM `{table}` WHERE `{col}` = :g {extra}"), {"g": gid}
    ).scalar()


def _delete(s, table: str, col: str, gid: int, extra: str = "") -> int:
    return s.execute(
        text(f"DELETE FROM `{table}` WHERE `{col}` = :g {extra}"), {"g": gid}
    ).rowcount


def purge_db(gid: int, expect_name: str | None, apply: bool) -> bool:
    from db import Event
    from web_api.routes.events import _cascade_delete_event

    s = Session()
    try:
        grp = s.execute(
            text("SELECT group_name, wom_id, guild_id FROM `groups` WHERE group_id = :g"),
            {"g": gid},
        ).first()
        if grp is None:
            print(f"group {gid} does not exist (already purged?)")
            return False
        print(f"group {gid}: {grp.group_name!r}  wom_id={grp.wom_id}  guild_id={grp.guild_id}")
        if expect_name is not None and grp.group_name != expect_name:
            print(f"ABORT: name is {grp.group_name!r}, --expect-name said {expect_name!r}")
            return False

        foreign = s.execute(text(
            "SELECT eg.event_id FROM web_event_groups eg JOIN web_events e ON e.id = eg.event_id "
            "WHERE eg.group_id = :g AND (e.group_id IS NULL OR e.group_id <> :g)"
        ), {"g": gid}).scalars().all()
        if foreign:
            print(f"ABORT: group takes part in other groups' events {foreign}; withdraw it first")
            return False

        events = s.query(Event).filter(Event.group_id == gid).all()
        notice_threads = s.execute(
            text("SELECT thread_id FROM group_notices WHERE group_id = :g AND thread_id IS NOT NULL"),
            {"g": gid},
        ).scalars().all()

        print(f"\nevents (full cascade): {[(e.id, e.name, e.status) for e in events]}")
        print(f"notice chat threads: {list(notice_threads)}")
        print("\nrows by table:")
        plan = [(t, c, "") for t, c in _fk_tables(s)] + UNLINKED
        for t, c, extra in plan:
            n = _count(s, t, c, gid, extra)
            if n:
                print(f"  {t}.{c}: {n}")
        n = _count(s, "group_admins", "owner_group_id", gid)
        if n:
            print(f"  group_admins.owner_group_id: {n}")
        n = s.execute(text(
            "SELECT COUNT(*) FROM chat_participants WHERE party_type = 'group' AND party_id = :g"
        ), {"g": gid}).scalar()
        if n:
            print(f"  chat_participants (party): {n}")

        if not apply:
            print("\n(dry run — nothing written)")
            return True

        for ev in events:
            _cascade_delete_event(s, ev)
        s.flush()

        deleted = {}
        # The owner row carries owner_group_id = its own group_id, so it goes
        # with the group_id sweep; clear any stray pointer first.
        deleted["group_admins.owner_group_id"] = _delete(s, "group_admins", "owner_group_id", gid)
        for t, c, extra in plan:
            n = _delete(s, t, c, gid, extra)
            if n:
                deleted[f"{t}.{c}"] = n
        if notice_threads:
            params = {f"t{i}": t for i, t in enumerate(notice_threads)}
            ids = ", ".join(f":{k}" for k in params)
            for t in ("chat_reads", "chat_messages", "chat_participants"):
                deleted[t + " (notice threads)"] = s.execute(
                    text(f"DELETE FROM `{t}` WHERE thread_id IN ({ids})"), params
                ).rowcount
            deleted["chat_threads"] = s.execute(
                text(f"DELETE FROM chat_threads WHERE id IN ({ids})"), params
            ).rowcount
        deleted["chat_participants (party)"] = s.execute(text(
            "DELETE FROM chat_participants WHERE party_type = 'group' AND party_id = :g"
        ), {"g": gid}).rowcount
        deleted["groups"] = _delete(s, "groups", "group_id", gid)
        s.commit()
        print("\ndeleted:")
        for k, v in deleted.items():
            if v:
                print(f"  {k}: {v}")
        return True
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def purge_redis(gid: int, apply: bool) -> None:
    from utils.redis import redis_client

    r = redis_client.client
    keys = list(r.scan_iter(match=f"leaderboard:group:{gid}:*", count=5000))
    keys += list(r.scan_iter(match=f"leaderboard:*:group:{gid}", count=5000))
    gboards = [k for k in r.scan_iter(match="gleaderboard:*", count=1000)
               if r.type(k) == b"zset" and r.zscore(k, str(gid)) is not None]
    print(f"\nredis: {len(keys)} group board keys; member of {len(gboards)} gleaderboard sets")
    if not apply:
        return
    for i in range(0, len(keys), 500):
        r.delete(*keys[i:i + 500])
    for k in gboards:
        r.zrem(k, str(gid))
    print("redis: cleared")


def purge_files(gid: int, apply: bool) -> None:
    icons = [os.path.join(ICON_DIR, n) for n in os.listdir(ICON_DIR) if n.startswith(f"{gid}-")]
    clan = os.path.join(CLAN_DIR, str(gid))
    print(f"\nfiles: icons {icons}; clan dir {clan if os.path.isdir(clan) else '(none)'}")
    if not apply:
        return
    for p in icons:
        os.unlink(p)
    if os.path.isdir(clan):
        shutil.rmtree(clan)
    print("files: removed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("group_id", type=int)
    ap.add_argument("--expect-name", help="refuse unless groups.group_name matches exactly")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.apply and args.expect_name is None:
        print("--apply requires --expect-name")
        return 2
    if not purge_db(args.group_id, args.expect_name, args.apply):
        return 1
    purge_redis(args.group_id, args.apply)
    purge_files(args.group_id, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
