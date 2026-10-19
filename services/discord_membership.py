"""Who is a member of the main DropTracker Discord server, cached in Redis.

The Web API never talks to Discord (§10.2), but the site wants to know whether
the viewer is in the server (the "join our Discord" nudge on support tickets).
The webhook bot holds the privileged GUILD_MEMBERS intent, so it keeps a Redis
set of member ids current:

* a full replace whenever the role sync lists the members (at least every
  15 minutes, see ``bots/webhook_bot.py``), and
* ``mark_joined`` / ``mark_left`` from MemberAdd / MemberRemove, so a person
  who joins shows as a member within seconds.

The full replace also refreshes a freshness marker with a TTL. When that marker
is gone (the bot is down, or role sync is switched off) the set may be stale,
and :func:`is_member` answers ``None`` ("unknown") rather than guessing.

Like ``services/discord_roles.py`` this imports neither ``interactions`` nor
``web_api``; every Redis failure degrades to "unknown".
"""
from __future__ import annotations

from typing import Iterable, Optional

from services.discord_roles import MAIN_GUILD_ID, normalize_discord_id

MEMBERS_KEY = f"discord:guild:{MAIN_GUILD_ID}:member_ids"
FRESH_KEY = f"discord:guild:{MAIN_GUILD_ID}:member_ids:fresh"
#: A full refresh runs every 15 minutes; allow a few missed passes.
FRESH_TTL_SECONDS = 2 * 3600
_CHUNK = 1000


def _conn():
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def replace_members(discord_ids: Iterable, conn=None) -> int:
    """Swap the cached set for a full member listing. Returns the count stored.

    Built under a temporary key and RENAMEd over the live one, so readers
    never see a half-written set.
    """
    conn = conn if conn is not None else _conn()
    if conn is None:
        return 0
    ids = sorted({i for i in (normalize_discord_id(x) for x in discord_ids) if i})
    if not ids:
        return 0  # an empty listing is a fetch problem, not an empty server
    tmp = MEMBERS_KEY + ":building"
    pipe = conn.pipeline()
    pipe.delete(tmp)
    for start in range(0, len(ids), _CHUNK):
        pipe.sadd(tmp, *ids[start:start + _CHUNK])
    pipe.rename(tmp, MEMBERS_KEY)
    pipe.set(FRESH_KEY, "1", ex=FRESH_TTL_SECONDS)
    pipe.execute()
    return len(ids)


def ids_from_member_objects(members: Iterable) -> list[str]:
    """Discord ids out of raw member payloads (``{"user": {"id": ...}}``)."""
    out = []
    for m in members or []:
        user = m.get("user") if isinstance(m, dict) else None
        if isinstance(user, dict) and user.get("id"):
            out.append(str(user["id"]))
    return out


def mark_joined(discord_id, conn=None) -> None:
    conn = conn if conn is not None else _conn()
    did = normalize_discord_id(discord_id)
    if conn is None or not did:
        return
    try:
        conn.sadd(MEMBERS_KEY, did)
    except Exception:
        pass


def mark_left(discord_id, conn=None) -> None:
    conn = conn if conn is not None else _conn()
    did = normalize_discord_id(discord_id)
    if conn is None or not did:
        return
    try:
        conn.srem(MEMBERS_KEY, did)
    except Exception:
        pass


def is_member(discord_id, conn=None) -> Optional[bool]:
    """True / False when the cache is fresh; None when unknown.

    A user with no linked Discord id is unknown, not a non-member: the
    nudge would be meaningless to them.
    """
    did = normalize_discord_id(discord_id)
    if not did:
        return None
    conn = conn if conn is not None else _conn()
    if conn is None:
        return None
    try:
        if not conn.exists(FRESH_KEY):
            return None
        return bool(conn.sismember(MEMBERS_KEY, did))
    except Exception:
        return None
