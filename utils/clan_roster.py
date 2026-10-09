"""Who is in a clan, by name: the "is this player a clanmate?" lookup.

A group-content BotW (services/competition.py, ``party``) only counts a kill
with clanmates in it, and the evidence is the party list the plugin sends: a
list of in-game NAMES. A clanmate is anyone in the hosting clan's WiseOldMan
group or its DropTracker group, so this answers from both:

- the DropTracker side is ``user_group_association`` joined to ``players``
  (every member with a Player row, plugin users or WOM-provisioned stubs);
- the WOM side is the full member-name list the hourly membership sync
  already fetches (``utils.wiseoldman.fetch_group_members``), stored here as a
  Redis set, so a clanmate who has never been seen by DropTracker still counts.

Names compare through :func:`roster_name_key`, which must stay identical to
``services.competition.party_name_key`` (a unit test pins them together).
Module-level imports are stdlib only; Redis and the DB are reached lazily.
"""

import time

#: Refreshed hourly with the membership sync. Held long enough that a WOM
#: outage doesn't empty a clan for the races running on it.
ROSTER_TTL_SECONDS = 6 * 60 * 60

#: Per-process memo of both sides. A raid sends a party list per item, so the
#: events worker asks about the same clan many times a minute.
_CACHE_TTL_SECONDS = 120
_wom_cache = {}
_member_cache = {}


def roster_name_key(name) -> str:
    """Case, ``_``/``-`` and the game's non-breaking spaces fold to one space."""
    if name is None:
        return ""
    text = (str(name).replace(" ", " ").replace("_", " ")
            .replace("-", " "))
    return " ".join(text.split()).lower()


def _key(wom_group_id) -> str:
    return f"clanroster:{int(wom_group_id)}"


def _redis():
    from utils.redis import RedisClient

    return RedisClient().client


def store_group_roster(wom_group_id, names) -> int:
    """Replace the cached member-name set for one WOM group. Returns how many
    names were stored. Never raises: the membership sync must not fail over a
    cache write."""
    if not wom_group_id:
        return 0
    keys = {roster_name_key(n) for n in (names or ())}
    keys.discard("")
    if not keys:
        return 0
    try:
        client = _redis()
        redis_key = _key(wom_group_id)
        pipe = client.pipeline()
        pipe.delete(redis_key)
        pipe.sadd(redis_key, *sorted(keys))
        pipe.expire(redis_key, ROSTER_TTL_SECONDS)
        pipe.execute()
        _wom_cache.pop(int(wom_group_id), None)
        return len(keys)
    except Exception as e:
        print(f"[ClanRoster] failed to store roster for WOM group {wom_group_id}: {e}")
        return 0


def wom_roster_keys(wom_group_id) -> set:
    """Name keys of a WOM group's members (empty when unknown)."""
    if not wom_group_id:
        return set()
    now = time.monotonic()
    cached = _wom_cache.get(int(wom_group_id))
    if cached and now < cached[0]:
        return cached[1]
    try:
        raw = _redis().smembers(_key(wom_group_id)) or ()
    except Exception:
        return cached[1] if cached else set()
    keys = {m.decode("utf-8", errors="replace") if isinstance(m, bytes) else str(m)
            for m in raw}
    _wom_cache[int(wom_group_id)] = (now + _CACHE_TTL_SECONDS, keys)
    return keys


def group_member_keys(session, group_id) -> dict:
    """``{name key: player_id}`` for a DropTracker group's members."""
    now = time.monotonic()
    cached = _member_cache.get(int(group_id))
    if cached and now < cached[0]:
        return cached[1]
    try:
        from sqlalchemy import text as sql_text

        rows = session.execute(
            sql_text(
                "SELECT p.player_id, p.player_name "
                "FROM user_group_association uga "
                "JOIN players p ON p.player_id = uga.player_id "
                "WHERE uga.group_id = :gid"
            ),
            {"gid": int(group_id)},
        ).all()
    except Exception:
        return cached[1] if cached else {}
    members = {}
    for player_id, player_name in rows:
        key = roster_name_key(player_name)
        if key and player_id is not None:
            members[key] = int(player_id)
    _member_cache[int(group_id)] = (now + _CACHE_TTL_SECONDS, members)
    return members


def clanmate_keys(session, group_id, keys) -> set:
    """The subset of ``keys`` (name keys) that are in the clan: its
    DropTracker group or its WiseOldMan group."""
    keys = [k for k in (keys or ()) if k]
    if not keys or group_id is None:
        return set()
    members = group_member_keys(session, group_id)
    found = {k for k in keys if k in members}
    missing = [k for k in keys if k not in found]
    if missing:
        from utils.clan_ranks import _group_wom_id

        wom = wom_roster_keys(_group_wom_id(session, group_id))
        found.update(k for k in missing if k in wom)
    return found
