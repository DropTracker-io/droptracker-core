"""Send monthly recap cards to Discord (ops + rollout entry point).

Dry run by default: it selects, reports exactly who would receive what, and
sends nothing. ``--apply`` sends, and even then refuses unless
``RECAP_DELIVERY_ENABLED`` is set — two switches, because the failure mode here
is thousands of unsolicited DMs and there is no unsend.

While ``RECAP_DELIVERY_TEST_DISCORD_ID`` is set, every message goes to that one
Discord user instead of its real recipient, labelled with who it was for. Those
deliveries are recorded with ``is_test=1``, so they neither block the real send
later nor consume anyone's one free recap.

Usage
-----
    # who would get last month's card? (sends nothing, needs no env)
    python -m scripts.send_recaps

    # the same, for a specific month, ignoring whose local hour has arrived
    python -m scripts.send_recaps --period 2026-06 --ignore-due

    # rollout: send to the test recipient only
    RECAP_DELIVERY_ENABLED=true RECAP_DELIVERY_TEST_DISCORD_ID=528746710042804247 \
        python -m scripts.send_recaps --period 2026-06 --ignore-due --limit 2 --apply

    # one specific subject, for eyeballing a single card
    python -m scripts.send_recaps --only-user 123 --apply
    python -m scripts.send_recaps --only-group 14 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Session  # noqa: E402
from services.recap_delivery import (  # noqa: E402
    ENV_ENABLED,
    ENV_TEST_TARGET,
    delivery_enabled,
    last_completed_month,
    run_delivery,
    test_target,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Send monthly recap cards to Discord.")
    ap.add_argument("--period", help="'YYYY-MM' (default: last completed month)")
    ap.add_argument("--apply", action="store_true", help="actually send")
    ap.add_argument("--only-group", type=int, metavar="ID", help="one group")
    ap.add_argument("--only-user", type=int, metavar="ID", help="one user")
    ap.add_argument("--limit", type=int, help="cap the number of user DMs")
    ap.add_argument("--groups-only", action="store_true")
    ap.add_argument("--users-only", action="store_true")
    ap.add_argument(
        "--ignore-due", action="store_true",
        help="ignore each subject's local send time (for manual runs and testing)",
    )
    ap.add_argument(
        "--unattended", action="store_true",
        help="this is the timer, not a human (refuses to send test traffic)",
    )
    args = ap.parse_args()

    period = (args.period or last_completed_month()).strip()
    now = datetime.now(timezone.utc)
    mode = "APPLY" if args.apply else "DRY RUN"
    test_id = test_target()

    print(f"[{mode}] recap delivery for {period}")
    if test_id:
        print(f"  TEST MODE: every message goes to Discord user {test_id}")
    elif args.apply:
        print("  LIVE: messages go to their real recipients")
    if args.apply and not delivery_enabled():
        print(f"  refusing to send: {ENV_ENABLED} is not set")
        print(f"  (set {ENV_ENABLED}=true, and {ENV_TEST_TARGET} while testing)")
        return 2
    if args.apply and args.unattended and test_id:
        # The timer fires every 15 minutes for three days. Test sends are
        # re-addressed to one person and recorded as is_test, so an unattended
        # run in test mode would put hundreds of cards in that inbox over the
        # window while delivering nothing to anyone real. Test traffic is for
        # runs a human is watching.
        print(f"  refusing to send: {ENV_TEST_TARGET} is set and this is an unattended run")
        print(f"  (clear {ENV_TEST_TARGET} to go live; test sends need a manual run)")
        return 2

    session = Session()
    started = time.time()

    async def _deliver():
        try:
            return await run_delivery(
                session,
                period=period,
                now=now,
                apply=args.apply,
                only_group=args.only_group,
                only_user=args.only_user,
                limit=args.limit,
                ignore_due=args.ignore_due,
                include_groups=not args.users_only,
                include_users=not args.groups_only,
            )
        finally:
            # The EHB harvest talks to Wise Old Man over a shared client; this
            # is a one-shot process, so hand its HTTP session back rather than
            # letting aiohttp print a teardown error over the run's report.
            from utils.wiseoldman import close_client

            await close_client()

    try:
        outcome = asyncio.run(_deliver())
    finally:
        session.close()

    for note in outcome.notes:
        print(f"  note: {note}")
    print(
        f"[{mode}] sent={outcome.sent} planned/skipped={outcome.skipped} "
        f"failed={outcome.failed} in {time.time() - started:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
