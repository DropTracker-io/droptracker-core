"""Post a staff reply to a suggestion thread (site + Discord mirror).

Reuses exactly the logic of ``web_api/routes/suggestions.py::
create_suggestion_message``: inserts a ``SuggestionMessage`` row (visible on
droptracker.io/suggestions/<id>), bumps the thread counters, and enqueues the
Discord relay through ``services/discord_outbox`` — the core bot drains the
outbox and posts into the suggestion's forum thread.

Usage
-----
    python -m scripts.reply_to_suggestion 50 --user-id 666 \
        --message "This has been fixed — ..."

    # preview without writing anything
    python -m scripts.reply_to_suggestion 50 --user-id 666 --message "..." --dry-run
"""

import argparse
import sys
from datetime import datetime

sys.path.insert(0, ".")

from db import Session
from db.models import Suggestion, SuggestionMessage, User

# Same relay formatting the web route uses (subtext attribution footer).
from web_api.routes.suggestions import _discord_reply_content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suggestion_id", type=int)
    parser.add_argument("--message", required=True, help="reply body (markdown ok)")
    parser.add_argument("--user-id", type=int, required=True,
                        help="users.user_id to attribute the reply to")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = parser.parse_args()

    content = args.message.strip()
    if not (2 <= len(content) <= 4000):
        sys.exit("message must be 2-4000 characters")

    s = Session()
    try:
        sug = s.query(Suggestion).filter(Suggestion.id == args.suggestion_id).first()
        if sug is None:
            sys.exit(f"no suggestion {args.suggestion_id}")
        if not sug.is_open:
            sys.exit(f"suggestion {args.suggestion_id} is closed")
        user = s.query(User).filter(User.user_id == args.user_id).first()
        if user is None:
            sys.exit(f"no user {args.user_id}")

        discord_id = getattr(user, "discord_id", None)
        author_name = getattr(user, "username", None) or f"user {args.user_id}"
        relay = _discord_reply_content(content, discord_id, author_name)

        print(f"suggestion #{sug.id}: {sug.title!r} (thread {sug.discord_thread_id})")
        print(f"author: {author_name} (user {args.user_id}, discord {discord_id})")
        print("--- site message ---")
        print(content)
        print("--- discord relay ---")
        print(relay)
        if args.dry_run:
            print("\n[dry run] nothing written")
            return

        msg = SuggestionMessage(
            suggestion_id=sug.id,
            author_user_id=args.user_id,
            author_discord_id=str(discord_id) if discord_id else None,
            author_name=author_name,
            source="web",
            content=content,
        )
        s.add(msg)
        sug.message_count = int(sug.message_count or 0) + 1
        sug.last_activity_at = datetime.now()
        s.flush()

        if sug.discord_thread_id:
            from services.discord_outbox import enqueue

            enqueue(
                s,
                channel_id=sug.discord_thread_id,
                content=relay,
                kind="message",
                ref_type="suggestion_message",
                ref_id=msg.id,
                actor_user_id=args.user_id,
                commit=False,
            )
        s.commit()
        print(f"\nposted message id {msg.id}"
              + (" + Discord relay enqueued" if sug.discord_thread_id else ""))
    finally:
        s.close()


if __name__ == "__main__":
    main()
