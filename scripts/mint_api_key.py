"""Mint a data-API (v2) key from the shell.

The plaintext token prints exactly once and is not recoverable — only its
SHA-256 lands in the table. Self-serve minting (supporter-gated user keys,
group-admin group keys) arrives with the web_api routes; this script is the
admin/dev path and the only mint path until then.

    ./venv/bin/python -m scripts.mint_api_key --group 2 --label "smoke test"
    ./venv/bin/python -m scripts.mint_api_key --user 1 --label "joel dev" --tier elevated
    ./venv/bin/python -m scripts.mint_api_key --revoke 3
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    owner = parser.add_mutually_exclusive_group()
    owner.add_argument("--user", type=int, help="owner user_id (user key)")
    owner.add_argument("--group", type=int, help="owner group_id (group key)")
    parser.add_argument("--label", default="", help="what this key is for")
    parser.add_argument("--tier", default="standard",
                        help="tier_key (default: standard — the floor, per policy)")
    parser.add_argument("--revoke", type=int, metavar="KEY_ID",
                        help="revoke this key id instead of minting")
    parser.add_argument("--list", action="store_true", help="list keys and exit")
    args = parser.parse_args()

    from db.models import ApiKey, ApiKeyTier, Session
    from db import api_keys as keys

    session = Session()
    try:
        if args.list:
            for row in session.query(ApiKey).order_by(ApiKey.id).all():
                state = ("revoked" if row.revoked_at else
                         "expired" if row.expires_at and row.expires_at <= datetime.utcnow()
                         else "active")
                owner_desc = (f"user {row.owner_user_id}" if row.owner_user_id is not None
                              else f"group {row.group_id}")
                print(f"#{row.id:<4} {state:<8} {row.tier_key:<10} {owner_desc:<12} "
                      f"dtk_{row.id}_{row.token_prefix}…  {row.label}")
            return 0

        if args.revoke is not None:
            row = session.query(ApiKey).filter(ApiKey.id == args.revoke).first()
            if row is None:
                print(f"No key #{args.revoke}")
                return 1
            if row.revoked_at is not None:
                print(f"Key #{row.id} was already revoked at {row.revoked_at}")
                return 0
            row.revoked_at = datetime.utcnow()
            session.commit()
            print(f"Revoked key #{row.id} ({row.label!r})")
            return 0

        if (args.user is None) == (args.group is None):
            parser.error("exactly one of --user / --group is required to mint")

        tier = session.query(ApiKeyTier).filter(
            ApiKeyTier.tier_key == args.tier, ApiKeyTier.enabled == True  # noqa: E712
        ).first()
        if tier is None:
            known = [t.tier_key for t in session.query(ApiKeyTier).all()]
            print(f"Unknown/disabled tier {args.tier!r}. Known: {', '.join(known)}")
            return 1

        row, token = keys.create_key(
            session,
            owner_user_id=args.user,
            group_id=args.group,
            label=args.label,
            tier_key=args.tier,
        )
        session.commit()
        print(f"Key #{row.id} minted (tier {row.tier_key}, {args.label!r}).")
        print("Token — shown once, store it now:")
        print(f"  {token}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
