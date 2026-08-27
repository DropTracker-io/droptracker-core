"""Telling a group's Discord *invite* apart from a Discord *credential*.

A group's Discord link is a free-text field, and legitimately holds all sorts
of things: bare ``discord.gg/x`` with no scheme, mixed case, and custom
redirects on the clan's own domain. An allowlist of "real" invite shapes would
reject a tenth of the groups that have one, so this is deliberately a narrow
blocklist instead.

What the field must never hold is a **credential**. A Discord webhook URL
carries its own token — anyone who can read it can post into that clan's
server as the clan, with no further authentication. One group had one stored
here, and it was being published on the group's public profile, handed out by
the intake API, and mirrored into the forum database.

Lives in ``utils`` because all three of those callers are in different
packages (``web_api``, ``api``, ``db``) and none of them should import
another's.
"""
from __future__ import annotations

import re
from typing import Optional

#: Any Discord *API* URL, and any webhook URL on any host. Both carry tokens;
#: neither is ever a thing to show a visitor.
_CREDENTIAL_URL = re.compile(
    r"/api/webhooks/\d+/|discord(?:app)?\.com/api/", re.IGNORECASE
)


def is_discord_credential_url(url) -> bool:
    """Whether ``url`` is a Discord API/webhook URL rather than an invite."""
    return isinstance(url, str) and bool(_CREDENTIAL_URL.search(url))


def public_discord_url(url) -> Optional[str]:
    """``url`` if it is safe to publish, else ``None``.

    Applied on the way *out* as well as on the way in. Values predating the
    write-side validation are already in the database, and this read-side
    guard is what stops one being published while it waits to be cleaned up.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    return None if is_discord_credential_url(url) else url.strip()
