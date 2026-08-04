"""Collapse each group's ``group_admins`` rows down to exactly one ``owner``.

Background (web86a)
-------------------
Until now ``owner`` and ``admin`` grants were interchangeable — every gate went
through ``assert_group_admin``, which accepts both — so nothing stopped a group
accumulating several owners. ``scripts/seed_group_admins.py`` did exactly that
on rollout: it inserted an ``owner`` row for *every* Discord id in a group's
``authed_users`` list, in one batch. 46 groups came out of that with more than
one owner (max 9), all stamped within the same second, so neither ``created_at``
nor ``granted_by`` can tell them apart.

The reliable creator signal is the FIRST entry of ``authed_users``: group
creation writes the creator as its sole entry (``db/group_creation.py``) and
later additions append. Spot-checked against prod, it names one of the existing
owner grants on 41 of the 46 multi-owner groups.

Resolution, per group
---------------------
* exactly one owner grant      -> leave alone
* several owner grants         -> keep the ``authed_users[0]`` user if they hold
                                  any grant here, demote the rest to ``admin``;
                                  if that user can't be identified, demote them
                                  ALL and leave the group ownerless
* no owner grant               -> promote the ``authed_users[0]`` user (existing
                                  row upgraded, or a fresh row inserted)
* nothing resolvable           -> leave ownerless

Nobody loses access: a demoted owner keeps full ``admin`` rights, and an
ownerless group is claimable in-app by any of its admins ("Claim ownership" on
the group's Roles & access tab), which writes an audit row and announces itself
to the group's Discord.

Run BEFORE the web86a migration adds the unique index — that index rejects the
duplicate owners this script clears.

    venv/bin/python -m scripts.migrate_group_single_owner            # dry run
    venv/bin/python -m scripts.migrate_group_single_owner --apply    # write
    venv/bin/python -m scripts.migrate_group_single_owner --group 14 # one group

Idempotent: a second run finds every group already at one-or-zero owners and
makes no changes.
"""

import argparse
import json
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from db.models import Group, GroupAdmin, GroupConfiguration, session

_AUTHED_USERS_KEY = "authed_users"


def _authed_ids(row) -> list[str]:
    """Discord ids from an ``authed_users`` config row, order preserved.

    Long lists spill from ``config_value`` (VARCHAR(255)) into ``long_value``;
    read whichever holds the payload, same as the web editor and the bot.
    """
    if row is None:
        return []
    raw = row.config_value or getattr(row, "long_value", None)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        # Legacy single-id string.
        text = str(raw).strip()
        return [text] if text else []
    if isinstance(data, list):
        return [str(v).strip() for v in data if str(v).strip()]
    if isinstance(data, (str, int)):
        return [str(data).strip()]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collapse each group to a single group_admins owner."
    )
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    parser.add_argument("--group", type=int, default=None, help="Limit to one group_id.")
    parser.add_argument("--verbose", action="store_true", help="Print unchanged groups too.")
    args = parser.parse_args()

    grants: dict[int, list[GroupAdmin]] = defaultdict(list)
    q = session.query(GroupAdmin)
    if args.group is not None:
        q = q.filter(GroupAdmin.group_id == args.group)
    for row in q.all():
        grants[row.group_id].append(row)

    cfg_q = session.query(GroupConfiguration).filter(
        GroupConfiguration.config_key == _AUTHED_USERS_KEY
    )
    if args.group is not None:
        cfg_q = cfg_q.filter(GroupConfiguration.group_id == args.group)
    authed = {row.group_id: _authed_ids(row) for row in cfg_q.all()}

    # discord_id -> user_id, for the whole run (one query beats one per group).
    from db.models import User

    by_discord = {
        str(d): int(u)
        for u, d in session.query(User.user_id, User.discord_id)
        .filter(User.discord_id.isnot(None))
        .all()
    }

    def creator_user_id(group_id: int):
        ids = authed.get(group_id) or []
        return by_discord.get(ids[0]) if ids else None

    stats = defaultdict(int)
    ownerless: list[int] = []
    demoted = promoted = inserted = 0

    # Drive off `groups`, not off the grant/config tables: a group with neither
    # a group_admins row nor an authed_users config is exactly the case that
    # most needs reporting, and it would never appear in a union of those two.
    group_q = session.query(Group.group_id)
    if args.group is not None:
        group_q = group_q.filter(Group.group_id == args.group)
    all_group_ids = sorted(gid for (gid,) in group_q.all())

    for group_id in all_group_ids:
        rows = grants.get(group_id, [])
        owners = [r for r in rows if r.role == "owner"]
        # `is not None` throughout: user_id 0 is a real account.
        candidate = creator_user_id(group_id)

        if len(owners) == 1:
            stats["kept_single_owner"] += 1
            if args.verbose:
                print(f"  = group {group_id}: owner user {owners[0].user_id} (unchanged)")
            continue

        if len(owners) > 1:
            keep = None
            if candidate is not None:
                keep = next((r for r in rows if r.user_id == candidate), None)
            if keep is not None:
                for r in owners:
                    if r.user_id != keep.user_id:
                        print(
                            f"  - group {group_id}: demote user {r.user_id} owner -> admin"
                        )
                        if args.apply:
                            r.role = "admin"
                        demoted += 1
                if keep.role != "owner":
                    print(f"  + group {group_id}: promote user {keep.user_id} -> owner")
                    if args.apply:
                        keep.role = "owner"
                    promoted += 1
                print(f"  * group {group_id}: owner = user {keep.user_id}")
                stats["multi_resolved"] += 1
            else:
                for r in owners:
                    print(
                        f"  - group {group_id}: demote user {r.user_id} owner -> admin "
                        "(creator unidentifiable)"
                    )
                    if args.apply:
                        r.role = "admin"
                    demoted += 1
                print(f"  ! group {group_id}: left OWNERLESS ({len(owners)} equal claims)")
                stats["multi_unresolved_ownerless"] += 1
                ownerless.append(group_id)
            continue

        # No owner grant at all.
        if candidate is None:
            print(f"  ! group {group_id}: left OWNERLESS (no resolvable creator)")
            stats["no_owner_ownerless"] += 1
            ownerless.append(group_id)
            continue

        existing = next((r for r in rows if r.user_id == candidate), None)
        if existing is not None:
            print(f"  + group {group_id}: promote user {candidate} admin -> owner")
            if args.apply:
                existing.role = "owner"
            promoted += 1
            stats["no_owner_promoted"] += 1
        else:
            print(f"  + group {group_id}: insert owner grant for user {candidate}")
            if args.apply:
                session.add(
                    GroupAdmin(group_id=group_id, user_id=candidate, role="owner")
                )
            inserted += 1
            stats["no_owner_inserted"] += 1

    if args.apply:
        session.commit()
    else:
        session.rollback()

    print("\n--- summary ---")
    for key in (
        "kept_single_owner",
        "multi_resolved",
        "no_owner_promoted",
        "no_owner_inserted",
        "multi_unresolved_ownerless",
        "no_owner_ownerless",
    ):
        print(f"  {key:30s} {stats[key]}")
    print(f"\n  rows demoted={demoted} promoted={promoted} inserted={inserted}")
    print(f"  groups left ownerless: {len(ownerless)}")
    if ownerless:
        print(f"    {sorted(ownerless)}")
        print("    (each is claimable in-app by any of its admins)")
    print(f"\n  {'APPLIED' if args.apply else 'DRY RUN — nothing written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
