"""One-off: replace the dead discord.gg/droptracker vanity invite in DB content.

The vanity URL no longer exists. Canonical link is now the site redirect
https://www.droptracker.io/discord (which 307s to the real invite from
next.config.ts), so DB content never has to change when the invite rotates.

Usage:
    venv/bin/python -m scripts.fix_discord_invite_links --dry-run  # preview
    venv/bin/python -m scripts.fix_discord_invite_links            # apply
"""
from __future__ import annotations

import argparse
import sys

from db.models import session
from db.models.group import Group
from db.models.web import Announcement, DocsPage
from db.models.embed import GroupEmbed, Field as EmbedField
from db.models.group_configuration import GroupConfiguration

OLD = "discord.gg/droptracker"
NEW_URL = "https://www.droptracker.io/discord"


def _rewrite(value: str) -> str:
    # Collapse any scheme-prefixed form of the dead invite to the redirect.
    return (
        value.replace("https://" + OLD, NEW_URL)
        .replace("http://" + OLD, NEW_URL)
        .replace(OLD, NEW_URL)
    )


# (model, label column for logging, text columns to scan)
TARGETS = [
    (DocsPage, "slug", ["body_md", "description", "title"]),
    (Announcement, "title", ["body_md", "title"]),
    (Group, "group_name", ["invite_url"]),
    (GroupEmbed, "embed_id", ["title", "description", "thumbnail", "image"]),
    (EmbedField, "field_id", ["field_name", "field_value"]),
    (GroupConfiguration, "config_key", ["config_value", "long_value"]),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    changed = 0
    for model, label_col, columns in TARGETS:
        rows = session.query(model).all()
        for row in rows:
            for col in columns:
                value = getattr(row, col, None)
                if not isinstance(value, str) or OLD not in value:
                    continue
                label = getattr(row, label_col, "?")
                print(f"{model.__tablename__}.{col} [{label_col}={label}]: contains {OLD!r}")
                if not args.dry_run:
                    setattr(row, col, _rewrite(value))
                changed += 1

    if args.dry_run:
        print(f"[dry-run] {changed} column value(s) would be rewritten.")
        session.rollback()
    elif changed:
        session.commit()
        print(f"Rewrote {changed} column value(s) -> {NEW_URL}")
    else:
        print("No occurrences found; nothing to do.")
    sys.exit(0)


if __name__ == "__main__":
    main()
