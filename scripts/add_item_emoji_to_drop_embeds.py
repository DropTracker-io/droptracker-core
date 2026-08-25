#!/usr/bin/env python3
"""Prefix drop embed titles with the item's own icon.

``{item_emoji}`` resolves to the item's application emoji (``utils/game_emojis``)
for the ~1000 items in the set, and to nothing for the rest — so
``{item_emoji} {item_name}`` renders as ":twisted_bow: Twisted bow" where there
is a glyph and as "Bronze dagger" where there is not.
``utils.format.tidy_title`` removes the space the empty case leaves behind.

Defaults to the **template group (1)** only. That is the row every group without
a custom drop embed inherits, so one edit changes what almost everyone sees;
`--group-id N` or `--all` extend it to the 15 groups that own a drop template,
which is the "wire it into group embeds later" step.

Only ever touches a title that mentions ``{item_name}`` and does not already
mention ``{item_emoji}`` — a group that renamed its title to something else is
left alone rather than having a token bolted onto prose it does not fit.

Dry-run by default. Idempotent — safe to re-run.

Run:
    venv/bin/python -m scripts.add_item_emoji_to_drop_embeds
    venv/bin/python -m scripts.add_item_emoji_to_drop_embeds --apply
    venv/bin/python -m scripts.add_item_emoji_to_drop_embeds --all --apply
    venv/bin/python -m scripts.add_item_emoji_to_drop_embeds --revert --apply
"""
from __future__ import annotations

import argparse
import sys

from db import Session
from db.models import GroupEmbed

TOKEN = "{item_emoji}"
TEMPLATE_GROUP_ID = 1

#: db/models/embed.py caps the column; Discord caps a title at 256.
MAX_TITLE = 255


def rewritten(title: str, revert: bool) -> str | None:
    """The new title, or None when this row should be left alone."""
    title = title or ""
    if revert:
        if TOKEN not in title:
            return None
        return title.replace(f"{TOKEN} ", "").replace(TOKEN, "").strip()
    if TOKEN in title or "{item_name}" not in title:
        return None
    new = f"{TOKEN} {title}"
    return new if len(new) <= MAX_TITLE else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default: dry run)")
    parser.add_argument("--group-id", type=int, action="append", default=None,
                        help="a group to include; repeatable (default: only group 1)")
    parser.add_argument("--all", action="store_true",
                        help="every group that owns a drop embed, not just the template")
    parser.add_argument("--revert", action="store_true",
                        help="strip the token back out again")
    args = parser.parse_args()

    with Session() as session:
        query = session.query(GroupEmbed).filter(GroupEmbed.embed_type == "drop")
        if not args.all:
            query = query.filter(
                GroupEmbed.group_id.in_(args.group_id or [TEMPLATE_GROUP_ID]))
        rows = query.order_by(GroupEmbed.group_id).all()

        changed, skipped = [], []
        for row in rows:
            new = rewritten(row.title, args.revert)
            (skipped if new is None else changed).append(
                row if new is None else (row, row.title, new))

        verb = "strip" if args.revert else "add"
        print(f"Scanned {len(rows)} drop embed(s).")
        print(f"{len(changed)} to {verb}:\n")
        for row, before, after in changed:
            print(f"  group {row.group_id:>4} (embed {row.embed_id})")
            print(f"    before: {before!r}")
            print(f"    after : {after!r}")
        if skipped:
            print(f"\n{len(skipped)} left alone "
                  f"(already {verb}ed, or the title does not mention {{item_name}}):")
            for row in skipped:
                print(f"  group {row.group_id:>4}: {row.title!r}")

        if not changed:
            print("\nNothing to do.")
            return 0
        if not args.apply:
            print("\nDry run — pass --apply to write.")
            return 0

        for row, _before, after in changed:
            row.title = after
        session.commit()
        print(f"\nWrote {len(changed)} title(s).")
        print("The notification service reads group_embeds per send, so this is "
              "live immediately — no restart needed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
