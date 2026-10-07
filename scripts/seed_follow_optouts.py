#!/usr/bin/env python3
"""
Recover "Follows Updates" opt-outs made before the choice was stored
====================================================================

Before 2026-10-07 the Follow button toggled the role but stored nothing, so a
clan leader who unfollowed back then would be handed the role again by the
leader sweep (services/news_optin.py) once LEADER_UPDATES_LIVE is on. Discord's
audit log still has those presses for the last 45 days: the core bot's own
role changes, with reason "Opted out of updates" / "Opted in to updates". This
takes each person's LAST press and, where it was an unfollow, stores
``follows_updates = 0`` so the sweep leaves them alone.

Uses the core bot's token (BOT_TOKEN; needs View Audit Log in HQ). Dry run
unless ``--apply``. Presses older than the audit log's 45 days can't be seen.

Usage:
    python scripts/seed_follow_optouts.py            # dry run
    python scripts/seed_follow_optouts.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from interactions.api.http.http_client import HTTPClient  # noqa: E402

from services import leader_updates as lu  # noqa: E402

MEMBER_ROLE_UPDATE = 25
OPT_OUT_REASON = "Opted out of updates"
OPT_IN_REASON = "Opted in to updates"
FOLLOW_ROLE_NAME = "Follows Updates"


async def _last_presses() -> dict[str, str]:
    """discord_id -> 'out' | 'in', from the bot's latest button press each."""
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("error: BOT_TOKEN not set")
    http = HTTPClient()
    me = await http.login(token)
    try:
        roles = await http.get_roles(lu.HQ_GUILD_ID)
        role = next((r for r in roles if r["name"] == FOLLOW_ROLE_NAME), None)
        if role is None:
            raise SystemExit(f"error: role '{FOLLOW_ROLE_NAME}' not found")
        role_id = str(role["id"])
        entries: list[dict] = []
        before = None
        while True:
            page = await http.get_audit_log(lu.HQ_GUILD_ID, user_id=me["id"],
                                            action_type=MEMBER_ROLE_UPDATE, before=before, limit=100)
            batch = page.get("audit_log_entries") or []
            entries.extend(batch)
            if len(batch) < 100:
                break
            before = min(int(e["id"]) for e in batch)
    finally:
        await http.close()

    last: dict[str, tuple[int, str]] = {}
    for e in entries:
        reason = e.get("reason") or ""
        if reason not in (OPT_OUT_REASON, OPT_IN_REASON):
            continue
        touches_role = any(
            str(r.get("id")) == role_id
            for c in e.get("changes") or [] if c.get("key") in ("$add", "$remove")
            for r in c.get("new_value") or []
        )
        if not touches_role:
            continue
        uid, eid = str(e.get("target_id")), int(e["id"])
        if uid not in last or eid > last[uid][0]:
            last[uid] = (eid, "out" if reason == OPT_OUT_REASON else "in")
    print(f"audit entries read: {len(entries)}; button presses on the role: {len(last)} people")
    return {uid: state for uid, (_eid, state) in last.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    presses = asyncio.run(_last_presses())
    out_ids = sorted(uid for uid, state in presses.items() if state == "out")
    print(f"last press was an unfollow: {len(out_ids)}")

    from db.models.base import Session

    session = Session()
    try:
        leaders = lu.leader_discord_ids(session)
        already = lu.opted_out_ids(session, out_ids)
        for uid in out_ids:
            tag = "leader" if uid in leaders else "not a leader"
            if uid in already:
                print(f"  {uid} ({tag}): already stored as off")
                continue
            if args.apply:
                stored = lu.set_follow_pref(session, uid, False)
                print(f"  {uid} ({tag}): {'stored as off' if stored else 'no DropTracker account, skipped'}")
            else:
                print(f"  {uid} ({tag}): would store as off")
    finally:
        session.close()
    if not args.apply:
        print("[DRY RUN] nothing changed. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
