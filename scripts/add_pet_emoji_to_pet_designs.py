#!/usr/bin/env python3
"""Put the pet's own icon in front of its name in every pet design.

``{pet_emoji}`` resolves to the pet's application emoji (``utils/game_emojis``;
every known pet is in the set), so ``{pet_emoji} {pet_name}`` renders as
":olmlet: Olmlet". A name the set lacks renders bare, and
``utils.format.tidy_title`` removes the space the empty case leaves behind.

The drop rollout (``add_item_emoji_to_drop_embeds``) prefixed the whole title.
Here the token goes immediately before ``{pet_name}`` instead, because pet
titles are more often prose: "{player_name} is being followed by {pet_name}"
should not start with the pet's icon.

Covers both designs a pet notification can be sent with:

- ``group_embeds`` rows of type ``pet``/``pet_dupe``: the title, or the
  description when only the description names the pet.
- ``group_component_layouts`` rows of type ``pet``/``pet_dupe``: each text or
  section block that names the pet.

A design that already mentions ``{pet_emoji}`` anywhere, or never mentions
``{pet_name}``, is left alone. Every group by default (the drop rollout's
``--all``); ``--group-id N`` narrows it.

Dry-run by default. Idempotent — safe to re-run.

Run:
    venv/bin/python -m scripts.add_pet_emoji_to_pet_designs
    venv/bin/python -m scripts.add_pet_emoji_to_pet_designs --apply
    venv/bin/python -m scripts.add_pet_emoji_to_pet_designs --revert --apply
"""
from __future__ import annotations

import argparse
import json
import sys

TOKEN = "{pet_emoji}"
NAME = "{pet_name}"
PET_TYPES = ("pet", "pet_dupe")

#: db/models/embed.py caps these columns; Discord caps a title at 256.
MAX_TITLE = 255
MAX_DESCRIPTION = 1000


def with_token(text: str | None) -> str | None:
    """``text`` with the token before its first ``{pet_name}``, or None."""
    if not text or NAME not in text or TOKEN in text:
        return None
    return text.replace(NAME, f"{TOKEN} {NAME}", 1)


def without_token(text: str | None) -> str | None:
    """``text`` with the token stripped back out, or None if it has none."""
    if not text or TOKEN not in text:
        return None
    return text.replace(f"{TOKEN} ", "").replace(TOKEN, "")


def rewritten_embed(title, description, revert: bool) -> dict:
    """The embed columns to change, as ``{column: new value}``; empty to skip."""
    if revert:
        changes = {"title": without_token(title), "description": without_token(description)}
        return {k: v for k, v in changes.items() if v is not None}
    if TOKEN in (title or "") or TOKEN in (description or ""):
        return {}
    title_new = with_token(title)
    if title_new is not None:
        return {"title": title_new} if len(title_new) <= MAX_TITLE else {}
    description_new = with_token(description)
    if description_new is not None and len(description_new) <= MAX_DESCRIPTION:
        return {"description": description_new}
    return {}


def rewritten_layout(layout: dict, revert: bool) -> dict | None:
    """The layout with the token added (or stripped), or None to skip."""
    blocks = layout.get("blocks") if isinstance(layout, dict) else None
    if not isinstance(blocks, list):
        return None
    texts = [b for b in blocks if isinstance(b, dict) and b.get("type") in ("text", "section")]
    if not revert and any(TOKEN in (b.get("content") or "") for b in texts):
        return None
    edit = without_token if revert else with_token
    new_blocks, changed = [], False
    for block in blocks:
        if block in texts:
            content = edit(block.get("content"))
            if content is not None:
                block, changed = {**block, "content": content}, True
        new_blocks.append(block)
    return {**layout, "blocks": new_blocks} if changed else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default: dry run)")
    parser.add_argument("--group-id", type=int, action="append", default=None,
                        help="a group to include; repeatable (default: every group)")
    parser.add_argument("--revert", action="store_true",
                        help="strip the token back out again")
    args = parser.parse_args()

    from db import Session
    from db.models import GroupEmbed
    from db.models.component_layout import GroupComponentLayout

    with Session() as session:
        embeds = session.query(GroupEmbed).filter(GroupEmbed.embed_type.in_(PET_TYPES))
        layouts = session.query(GroupComponentLayout).filter(
            GroupComponentLayout.notification_type.in_(PET_TYPES))
        if args.group_id:
            embeds = embeds.filter(GroupEmbed.group_id.in_(args.group_id))
            layouts = layouts.filter(GroupComponentLayout.group_id.in_(args.group_id))
        embeds = embeds.order_by(GroupEmbed.group_id).all()
        layouts = layouts.order_by(GroupComponentLayout.group_id).all()

        verb = "strip" if args.revert else "add"
        embed_changes, layout_changes, skipped = [], [], []

        for row in embeds:
            changes = rewritten_embed(row.title, row.description, args.revert)
            if changes:
                embed_changes.append((row, changes))
            else:
                skipped.append(f"embed  group {row.group_id:>4} {row.embed_type}: {row.title!r}")

        for row in layouts:
            try:
                layout = json.loads(row.layout)
            except (TypeError, ValueError):
                skipped.append(f"layout group {row.group_id:>4} {row.notification_type}: unreadable JSON")
                continue
            new = rewritten_layout(layout, args.revert)
            if new is None:
                skipped.append(f"layout group {row.group_id:>4} {row.notification_type}: no change")
            else:
                layout_changes.append((row, layout, new))

        print(f"Scanned {len(embeds)} pet embed(s) and {len(layouts)} pet layout(s).")
        print(f"{len(embed_changes)} embed(s) and {len(layout_changes)} layout(s) to {verb}:\n")
        for row, changes in embed_changes:
            print(f"  embed  group {row.group_id:>4} {row.embed_type} (embed {row.embed_id})")
            for column, after in changes.items():
                print(f"    {column} before: {getattr(row, column)!r}")
                print(f"    {column} after : {after!r}")
        for row, before, after in layout_changes:
            print(f"  layout group {row.group_id:>4} {row.notification_type}"
                  f" ({'active' if row.active else 'inactive'})")
            for old, new in zip(before["blocks"], after["blocks"]):
                if old != new:
                    print(f"    before: {old.get('content')!r}")
                    print(f"    after : {new.get('content')!r}")
        if skipped:
            print(f"\n{len(skipped)} left alone (already {verb}ed, or no {NAME} to put it beside):")
            for line in skipped:
                print(f"  {line}")

        if not embed_changes and not layout_changes:
            print("\nNothing to do.")
            return 0
        if not args.apply:
            print("\nDry run — pass --apply to write.")
            return 0

        for row, changes in embed_changes:
            for column, after in changes.items():
                setattr(row, column, after)
        for row, _before, after in layout_changes:
            row.layout = json.dumps(after)
        session.commit()
        print(f"\nWrote {len(embed_changes)} embed(s) and {len(layout_changes)} layout(s).")
        print("The notification service reads designs per send, so this is "
              "live immediately — no restart needed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
