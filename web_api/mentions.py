"""Discord mention/token resolution for web-facing message content.

Discord message content embeds entities as raw markup — user mentions
``<@123>`` / ``<@!123>``, role mentions ``<@&123>``, channel mentions
``<#123>``, custom emoji ``<:name:123>`` / ``<a:name:123>`` and timestamps
``<t:unix:R>``. The suggestion forum and ticket transcript views mirror this
raw content verbatim, so those tokens surface as meaningless numeric ids
("nonsense") unless something resolves them to human-readable names.

Resolving a user mention needs the ``discord_id -> username`` map from the
``users`` table, which only the backend can see. So we hand the frontend a
``{discord_id: username}`` map alongside the (still raw) content and let it
render styled mention chips — see ``resolve_user_mentions``. For plain-text
previews that get no rich rendering (list excerpts), ``clean_tokens`` collapses
the same markup down to readable text server-side.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from db.models import User

# ``<@123>`` and the nickname form ``<@!123>`` both denote a user mention.
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
_TIMESTAMP_RE = re.compile(r"<t:-?\d+(?::[tTdDfFR])?>")


def collect_user_ids(texts: Iterable[Optional[str]]) -> set[str]:
    """Unique Discord user ids referenced by ``<@id>`` / ``<@!id>`` mentions."""
    ids: set[str] = set()
    for text in texts:
        if text:
            ids.update(_USER_MENTION_RE.findall(text))
    return ids


def resolve_user_mentions(session, texts: Iterable[Optional[str]]) -> dict[str, str]:
    """Map the Discord ids mentioned across ``texts`` to their site usernames.

    A single batched query keeps this O(1) per response regardless of how many
    messages mention users. Ids with no linked/named user are omitted — the
    frontend renders those as a generic "@unknown" rather than the raw id.
    """
    ids = collect_user_ids(texts)
    if not ids:
        return {}
    rows = (
        session.query(User.discord_id, User.username)
        .filter(User.discord_id.in_(ids))
        .all()
    )
    return {str(discord_id): username for discord_id, username in rows if discord_id and username}


def clean_tokens(text: Optional[str], user_map: Mapping[str, str]) -> str:
    """Flatten Discord entity tokens into readable plain text.

    Used for excerpts/previews that are rendered as bare text (no mention
    chips). Mirrors the frontend chip labels so both surfaces read the same:
    users -> ``@name`` (or ``@unknown``), roles -> ``@role``, channels ->
    ``#channel``, custom emoji -> ``:name:``; timestamps are dropped.
    """
    if not text:
        return text or ""

    def _user(match: "re.Match[str]") -> str:
        name = user_map.get(match.group(1))
        return f"@{name}" if name else "@unknown"

    out = _USER_MENTION_RE.sub(_user, text)
    out = _ROLE_MENTION_RE.sub("@role", out)
    out = _CHANNEL_MENTION_RE.sub("#channel", out)
    out = _EMOJI_RE.sub(lambda m: f":{m.group(1)}:", out)
    out = _TIMESTAMP_RE.sub("", out)
    return out
