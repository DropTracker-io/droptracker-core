"""Converge DB content on the restored discord.gg/droptracker vanity invite.

History: the vanity was lost in 2026-07, so every published link was pointed at
the site redirect https://www.droptracker.io/discord and next.config.ts sent
that to the raw invite of the day (scripts/fix_discord_invite_links.py did the
DB side; deleted together with this script's introduction). The vanity was
re-obtained on 2026-08-03: the canonical published link is
https://discord.gg/droptracker again, and the /discord site redirect now
targets it — the redirect stays alive as an alias for links already in the
wild.

Rewrites, in the text columns listed in TARGETS:

* ``http(s)://(www.)droptracker.io/discord`` -> ``https://discord.gg/droptracker``
* bare ``(www.)droptracker.io/discord``      -> ``discord.gg/droptracker``
* the old raw invite ``discord.gg/dvb7yP7JJH`` / ``discord.com/invite/dvb7yP7JJH``
  (any scheme form)                          -> ``https://discord.gg/droptracker``

A trailing-guard keeps longer paths (``/discord-foo``, ``/discord/sub``)
untouched. Group-owned rows only match when they contain one of OUR old URLs,
so other groups' own invites are never rewritten. Idempotent; safe to re-run.
Also reports (without rewriting) any other Discord invite still present in
site-owned content (docs pages, announcements) so stale links get eyeballed.

Usage:
    venv/bin/python -m scripts.set_discord_vanity_links            # dry-run (default)
    venv/bin/python -m scripts.set_discord_vanity_links --apply    # write
"""
from __future__ import annotations

import argparse
import re
import sys

from db.models import session
from db.models.group import Group
from db.models.web import Announcement, DocsPage, SiteRedirect
from db.models.embed import GroupEmbed, Field as EmbedField
from db.models.group_configuration import GroupConfiguration

NEW_URL = "https://discord.gg/droptracker"
NEW_BARE = "discord.gg/droptracker"
OLD_INVITE_CODE = "dvb7yP7JJH"

# (?![\w/-]) so /discord-foo and /discord/sub never match.
_REDIRECT_FULL = re.compile(r"https?://(?:www\.)?droptracker\.io/discord(?![\w/-])")
_REDIRECT_BARE = re.compile(r"(?<![\w/])(?:www\.)?droptracker\.io/discord(?![\w/-])")
_OLD_INVITE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord\.com/invite)/"
    + OLD_INVITE_CODE
    + r"(?![\w-])"
)
_ANY_OLD = re.compile(
    "|".join(p.pattern for p in (_REDIRECT_FULL, _REDIRECT_BARE, _OLD_INVITE))
)
# Any invite that is neither the vanity nor handled above — report only.
_FOREIGN_INVITE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord\.com/invite)/(?!droptracker\b)[A-Za-z0-9-]+"
)


def _rewrite(value: str) -> str:
    value = _REDIRECT_FULL.sub(NEW_URL, value)
    value = _REDIRECT_BARE.sub(NEW_BARE, value)
    value = _OLD_INVITE.sub(NEW_URL, value)
    return value


def _snippets(pattern: re.Pattern, value: str, width: int = 40) -> list[str]:
    out = []
    for m in pattern.finditer(value):
        s, e = m.span()
        out.append(value[max(0, s - width) : e + width].replace("\n", " "))
    return out


# (model, label column for logging, text columns to scan)
TARGETS = [
    (DocsPage, "slug", ["body_md", "description", "title"]),
    (Announcement, "title", ["body_md", "title"]),
    (Group, "group_name", ["invite_url"]),
    (GroupEmbed, "embed_id", ["title", "description", "thumbnail", "image"]),
    (EmbedField, "field_id", ["field_name", "field_value"]),
    (GroupConfiguration, "config_key", ["config_value", "long_value"]),
    (SiteRedirect, "source", ["destination"]),
]

# Site-owned content where a leftover foreign invite is worth flagging.
_REPORT_FOREIGN = {DocsPage, Announcement}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    args = parser.parse_args()

    changed = 0
    for model, label_col, columns in TARGETS:
        for row in session.query(model).all():
            for col in columns:
                value = getattr(row, col, None)
                if not isinstance(value, str) or not value:
                    continue
                new_value = _rewrite(value)
                label = getattr(row, label_col, "?")
                if new_value != value:
                    print(f"{model.__tablename__}.{col} [{label_col}={label}]:")
                    for snip in _snippets(_ANY_OLD, value):
                        print(f"    …{snip}…")
                    changed += 1
                    if args.apply:
                        setattr(row, col, new_value)
                if model in _REPORT_FOREIGN:
                    for snip in _snippets(_FOREIGN_INVITE, new_value):
                        print(
                            f"[report-only] other invite in {model.__tablename__}.{col} "
                            f"[{label_col}={label}]: …{snip}…"
                        )

    if not args.apply:
        session.rollback()
        print(f"[dry-run] {changed} column value(s) would be rewritten; re-run with --apply.")
    elif changed:
        session.commit()
        print(f"Rewrote {changed} column value(s) -> {NEW_URL}")
    else:
        print("No occurrences found; nothing to do.")
    sys.exit(0)


if __name__ == "__main__":
    main()
