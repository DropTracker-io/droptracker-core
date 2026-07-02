"""Grant or revoke site-staff (superadmin) on a user (backend Task 12).

Superadmins unlock the ``/admin`` dashboard (services, subscriptions, comped
grants, data viewer, lookup, tier CRUD). Two ways to designate one:

  1. Env bootstrap (recommended for owners): set ``WEB_SUPERADMIN_DISCORD_IDS``
     (comma-separated Discord ids) in the backend ``.env``; those users are
     granted on their next login.
  2. This script (manual, immediate):

        venv/bin/python -m scripts.set_superadmin --discord 207621486842871809
        venv/bin/python -m scripts.set_superadmin --user 987
        venv/bin/python -m scripts.set_superadmin --discord 207621486842871809 --revoke
        venv/bin/python -m scripts.set_superadmin --list
"""
from __future__ import annotations

import argparse
import sys

from db.models import User, session


def main() -> int:
    ap = argparse.ArgumentParser(description="Grant/revoke superadmin.")
    ap.add_argument("--discord", help="Target Discord user id.")
    ap.add_argument("--user", type=int, help="Target DropTracker user_id.")
    ap.add_argument("--revoke", action="store_true", help="Revoke instead of grant.")
    ap.add_argument("--list", action="store_true", help="List current superadmins.")
    args = ap.parse_args()

    if args.list:
        admins = session.query(User).filter(User.is_superadmin == True).all()  # noqa: E712
        if not admins:
            print("No superadmins set.")
        for u in admins:
            print(f"  user_id={u.user_id} discord_id={u.discord_id} username={u.username}")
        return 0

    if not args.discord and not args.user:
        ap.error("Provide --discord, --user, or --list.")

    q = session.query(User)
    user = (
        q.filter(User.discord_id == str(args.discord)).first()
        if args.discord
        else q.filter(User.user_id == args.user).first()
    )
    if not user:
        print(f"User not found (discord={args.discord} user={args.user}).")
        return 1

    user.is_superadmin = not args.revoke
    session.commit()
    verb = "revoked from" if args.revoke else "granted to"
    print(f"Superadmin {verb} user_id={user.user_id} (discord {user.discord_id}, {user.username}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
