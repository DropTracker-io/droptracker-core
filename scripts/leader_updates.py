#!/usr/bin/env python3
"""
Clan-leader updates: pilot status and test reset
================================================

``status`` shows whether the feature is live or in pilot, who the pilot
accounts are, how many clan leaders the role sweep would cover once live, how
many have turned it off, and who the next sweep will look up.

``forget`` makes one account look brand new to the sweep: its stored on/off
choice and the sweep's markers are cleared, so within one sweep (10 minutes)
the core bot hands it the Follows Updates role again. Meant for testing the
default grant on a pilot account. Dry run unless ``--apply``.

``draft`` is how agents and scripts propose an update. It can ONLY make a
notice waiting for review: it sits in "Waiting for your review" on
/admin/notices and the owner gets a DM. Nothing is shown or posted until the
owner approves it there, and the owner can edit it or send it back first. By
default it is a pop-up for clan leaders (owners and admins); ``--post`` also
makes a public news post, ``--discord`` sends that post to the news channel,
and ``--no-popup`` drops the pop-up. Dry run unless ``--apply``.

Usage:
    python scripts/leader_updates.py status
    python scripts/leader_updates.py forget 528746710042804247            # dry run
    python scripts/leader_updates.py forget 528746710042804247 --apply
    python scripts/leader_updates.py draft --title "..." --body-file post.md \
        [--audience leaders|owners|everyone|staff] [--no-popup] [--post] [--discord] \
        [--source "weekly roundup"] [--apply]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from db.models.base import Session  # noqa: E402
from services import leader_updates as lu  # noqa: E402
from utils.redis import redis_client  # noqa: E402


def _status() -> int:
    conn = redis_client.client
    session = Session()
    try:
        leaders = lu.leader_discord_ids(session)
        opted_out = lu.opted_out_ids(session, leaders)
        handled = conn.scard(lu.GRANTED_KEY)
        pending = lu.pending_role_grants(session, conn, cap=10_000)
    finally:
        session.rollback()
        session.close()

    print(f"mode:             {'LIVE' if lu.is_live() else 'pilot'}")
    print(f"pilot accounts:   {', '.join(sorted(lu.pilot_discord_ids())) or '(none)'}")
    print(f"clan leaders:     {len(leaders)} (owners/admins with a Discord id)")
    print(f"turned it off:    {len(opted_out)}")
    print(f"already handled:  {handled}")
    print(f"next sweep looks up {len(pending)} account(s) "
          f"(at most {lu.MAX_LOOKUPS_PER_SWEEP} per sweep):")
    for discord_id in pending[:20]:
        print(f"  {discord_id}")
    return 0


def _forget(discord_id: str, apply: bool) -> int:
    if not discord_id.isdigit():
        print(f"not a Discord id: {discord_id}")
        return 2
    if not apply:
        print(f"[dry run] would clear the stored choice and sweep markers for {discord_id}.")
        print("Re-run with --apply to do it.")
        return 0
    session = Session()
    try:
        lu.forget(session, redis_client.client, discord_id)
    finally:
        session.close()
    print(f"cleared {discord_id}; the next sweep (within 10 minutes) treats it as new.")
    if not lu.in_audience(discord_id):
        print("note: this account is not a pilot account, so the sweep will skip it until live.")
    return 0


AUDIENCES = {
    "leaders": [{"type": "group_leaders", "roles": ["owner", "admin"]}],
    "owners": [{"type": "group_leaders", "roles": ["owner"]}],
    "everyone": [{"type": "everyone"}],
    "staff": [{"type": "staff"}],
}


def _draft(title: str, body_file: str, audience: str, popup: bool, post: bool,
           discord: bool, source: str, apply: bool) -> int:
    from web_api.popup_audience import describe_audience, normalize_audience

    title = (title or "").strip()
    body = Path(body_file).read_text(encoding="utf-8").strip()
    if not (1 <= len(title) <= 120) or not body:
        print("title must be 1-120 characters and the body must not be empty")
        return 2
    if not popup and not post:
        print("nowhere to send it: keep the pop-up or add --post")
        return 2
    if discord and not post:
        print("--discord needs --post (the Discord post links to the news post)")
        return 2
    rules = normalize_audience(AUDIENCES[audience]) if popup else []
    summary = describe_audience(rules) if popup else None
    print(f"title:   {title}")
    print(f"source:  {source}")
    print("goes to: " + lu.notice_destinations(
        show_popup=popup, publish_post=post, post_to_discord=discord, audience_summary=summary,
    ) + " (only once the owner approves it)")
    print("-" * 60)
    print(body)
    print("-" * 60)
    if not apply:
        print("[dry run] re-run with --apply to put this in the owner's review queue.")
        return 0
    session = Session()
    try:
        notice = lu.create_notice_for_review(
            session, title=title, body_md=body, audience=rules, show_popup=popup,
            publish_post=post, post_to_discord=discord, source_label=source,
            audience_summary=summary,
        )
        print(f"notice {notice.id} is waiting for review at {lu.NOTICES_PATH}; the owner has been DMed.")
    finally:
        session.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    forget = sub.add_parser("forget")
    forget.add_argument("discord_id")
    forget.add_argument("--apply", action="store_true")
    draft = sub.add_parser("draft")
    draft.add_argument("--title", required=True)
    draft.add_argument("--body-file", required=True)
    draft.add_argument("--audience", choices=sorted(AUDIENCES), default="leaders",
                       help="who gets the pop-up (default: clan owners and admins)")
    draft.add_argument("--no-popup", action="store_true", help="news post only, no pop-up")
    draft.add_argument("--post", action="store_true", help="also a public news post")
    draft.add_argument("--discord", action="store_true", help="also the Discord news channel (needs --post)")
    draft.add_argument("--source", default="agent", help="shown in the review queue")
    draft.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.cmd == "status":
        return _status()
    if args.cmd == "draft":
        return _draft(args.title, args.body_file, args.audience, not args.no_popup, args.post,
                      args.discord, args.source, args.apply)
    return _forget(args.discord_id, args.apply)


if __name__ == "__main__":
    sys.exit(main())
