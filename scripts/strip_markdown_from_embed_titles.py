"""Flatten Discord markdown out of stored group_embeds.title values.

Discord renders an embed *title* as plain text — `**bold**`, `` `code` `` and
`[masked](links)` all show their markers. utils.format.strip_title_markdown now
flattens titles at send time, so this script is about the *stored* templates:
leaving markdown in the DB means the embed editor shows a template that does
not match what Discord will post, which is how the confusion started.

Includes the shipped defaults on the template group (group 1) and the global
server (group 2), whose `level_up` title is `**Levels achieved:** {skills_text}`
— every group without a custom template inherits those literal asterisks.

`{player_name}` in a title is reported but NOT rewritten: it resolves to a
markdown profile link only at send time, where the strip now handles it. Swap
it for `{player_name_plain}` in the editor if you want the intent explicit.

Dry-run by default. Idempotent — safe to re-run.

Run:
    venv/bin/python -m scripts.strip_markdown_from_embed_titles
    venv/bin/python -m scripts.strip_markdown_from_embed_titles --apply
"""
from __future__ import annotations

import argparse

from db import Session
from db.models import GroupEmbed
from utils.format import strip_title_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the changes (default: dry run)"
    )
    parser.add_argument(
        "--group-id", type=int, default=None, help="limit to one group"
    )
    args = parser.parse_args()

    with Session() as session:
        query = session.query(GroupEmbed).order_by(GroupEmbed.group_id, GroupEmbed.embed_type)
        if args.group_id is not None:
            query = query.filter(GroupEmbed.group_id == args.group_id)
        rows = query.all()

        changed = []
        player_name_titles = []
        for row in rows:
            title = row.title or ""
            stripped = strip_title_markdown(title)
            if stripped != title:
                changed.append((row, title, stripped))
            if "{player_name}" in title:
                player_name_titles.append(row)

        print(f"Scanned {len(rows)} group_embeds rows.")
        print(f"{len(changed)} title(s) carry markdown Discord will not render:\n")
        for row, before, after in changed:
            print(f"  group {row.group_id:>5}  {row.embed_type:<9}  {before!r}")
            print(f"  {'':>5}  {'':>9}  -> {after!r}")

        if player_name_titles:
            print(
                f"\n{len(player_name_titles)} title(s) use {{player_name}}, which resolves to a "
                "markdown profile link (flattened at send time; consider {player_name_plain}):"
            )
            for row in player_name_titles:
                print(f"  group {row.group_id:>5}  {row.embed_type:<9}  {row.title!r}")

        if not changed:
            print("\nNothing to do.")
            return 0

        if not args.apply:
            print("\nDry run — re-run with --apply to write these changes.")
            return 0

        for row, _before, after in changed:
            row.title = after
        session.commit()
        print(f"\nApplied {len(changed)} title update(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
