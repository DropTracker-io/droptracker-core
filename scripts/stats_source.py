#!/usr/bin/env python3
"""Show or set where each stats consumer reads from: wom, shadow or ours.

    venv/bin/python -m scripts.stats_source                      # show
    venv/bin/python -m scripts.stats_source set events shadow
    venv/bin/python -m scripts.stats_source set ehb_rates ours --owner-approved "name, date"

``shadow`` changes nothing a user can see: WOM is still read and used, and
our own numbers are recorded beside it (scripts/stats_shadow_report.py).
``ours`` switches the consumer to our numbers. It needs --owner-approved,
because each cutover is the owner's decision, one consumer at a time
(dev tracker t427). Services pick a change up within half a minute.

A switch pinned in the environment (STATS_SOURCE_<CONSUMER>) wins over this.
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    setter = sub.add_parser("set")
    setter.add_argument("consumer")
    setter.add_argument("mode", choices=("wom", "shadow", "ours"))
    setter.add_argument("--owner-approved", metavar="WHO_AND_WHEN",
                        help="required for 'ours': who approved this cutover, and when")
    args = parser.parse_args()

    from utils import stats_shadow
    from utils.redis import redis_client

    conn = redis_client.client
    if args.command == "set":
        if args.consumer not in stats_shadow.CONSUMERS:
            print(f"unknown consumer {args.consumer!r}; one of {', '.join(stats_shadow.CONSUMERS)}",
                  file=sys.stderr)
            return 2
        if args.mode == stats_shadow.OURS and not args.owner_approved:
            print("'ours' is a cutover and needs --owner-approved \"who, when\"", file=sys.stderr)
            return 2
        key = stats_shadow.SWITCH_KEY.format(consumer=args.consumer)
        if args.mode == stats_shadow.WOM:
            conn.delete(key)
        else:
            conn.set(key, args.mode)
        if args.owner_approved:
            conn.set(key + ":approved", args.owner_approved)
        print(f"{args.consumer} -> {args.mode}")

    for consumer in stats_shadow.CONSUMERS:
        stored = stats_shadow._clean(conn.get(stats_shadow.SWITCH_KEY.format(consumer=consumer))) or "wom"
        pinned = stats_shadow._clean(os.getenv(f"STATS_SOURCE_{consumer.upper()}"))
        note = f"  (this shell's environment pins {pinned})" if pinned else ""
        print(f"  {consumer:<11} {stored}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
