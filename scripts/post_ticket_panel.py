"""Post (or re-post) the Discord "open a ticket" panel.

The live panel was posted by hand and its buttons existed nowhere in this
repo, so adding a ticket type could never add a button. This renders the
panel from ``services.ticket_system.TICKET_TYPE_META`` — the same registry the
website picker and the ticket welcome card read — and enqueues it through the
Discord outbox, because the web/script side must never open a gateway
connection itself.

    ./venv/bin/python -m scripts.post_ticket_panel --channel 123456789
    ./venv/bin/python -m scripts.post_ticket_panel --channel 123456789 --apply

Dry run by default, like every other script here. The old panel is not
deleted: post the new one, check it, then remove the old message by hand.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, help="Discord channel id")
    parser.add_argument("--apply", action="store_true", help="enqueue it (default: dry run)")
    args = parser.parse_args()

    sys.path.insert(0, ".")
    from services.ticket_system import TICKET_TYPE_META

    print(f"Panel would offer {len(TICKET_TYPE_META)} buttons, in this order:")
    for key, meta in TICKET_TYPE_META.items():
        print(f"  {meta['emoji']}  {meta['label']:<24} custom_id=create_ticket_{key}")

    if not args.apply:
        print("\nDry run — re-run with --apply to enqueue it for the bot to send.")
        return 0

    from services.discord_outbox import enqueue

    content = (
        "## Need a hand?\n"
        "Pick the option that fits best and we'll open a private ticket with you."
    )
    enqueue(channel_id=str(args.channel), kind="message", content=content,
            components=_serialise_buttons(TICKET_TYPE_META))
    print(f"\nQueued for channel {args.channel}. The core bot sends it on its next drain.")
    return 0


def _serialise_buttons(registry) -> list:
    """Buttons as the outbox stores them (plain JSON, not library objects)."""
    rows, buttons = [], []
    for key, meta in registry.items():
        buttons.append({
            "type": 2,
            "style": 2 if key == "other" else 1,
            "label": meta["label"],
            "emoji": {"name": meta["emoji"]},
            "custom_id": f"create_ticket_{key}",
        })
    for i in range(0, len(buttons), 5):
        rows.append({"type": 1, "components": buttons[i:i + 5]})
    return rows


if __name__ == "__main__":
    sys.exit(main())
