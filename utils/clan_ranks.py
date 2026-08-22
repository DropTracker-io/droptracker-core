"""Per-clan rank cache: which in-game clan rank each member currently holds.

The plugin's chat relay sends ``{clan_name, sender, message}`` and no rank —
RuneLite exposes the rank title through ``ClanSettings``, but adding it needs a
plugin release, so the rank a mirror line renders with comes from **WOM**
instead. WOM's group roles ARE the OSRS clan rank list (269 of them against the
wiki's 270 icons), and every hourly ``fetch_group_members`` call already
receives one per membership and threw it away.

So this is written as a side effect of that existing sync — the same
already-paid-for call that refreshes EHB — and read when a chat line is staged.
Cosmetic data: every failure path here returns None and the line renders
without a glyph rather than not rendering at all.

Names are keyed through ``normalize_player_display_equivalence`` because the
plugin sends the client's underscore spelling (``Beast_Owned``) while WOM and
the DB carry the display spelling (``Beast Owned``).
"""

from utils.format import normalize_player_display_equivalence

#: Roles refresh hourly with the membership sync; hold them long enough that a
#: WOM outage dims the icons slowly instead of blanking a whole clan at once.
RANK_TTL_SECONDS = 6 * 60 * 60

#: WOM's default role for an unranked member. It has no wiki icon and no
#: in-game equivalent, so it is stored as "no rank" rather than as a rank.
DEFAULT_WOM_ROLE = "member"


def _key(wom_group_id) -> str:
    return f"clanrank:{int(wom_group_id)}"


def _redis():
    from utils.redis import RedisClient

    return RedisClient().client


def store_group_ranks(wom_group_id, ranks: dict) -> int:
    """Replace the cached rank map for one WOM group. Returns members stored.

    ``ranks`` is ``{player display name: role}``. Written with a fresh key so a
    member who lost their rank stops rendering one, then expired so a group
    that stops syncing eventually falls back to plain lines."""
    if not wom_group_id or not ranks:
        return 0
    payload = {}
    for name, role in ranks.items():
        key = normalize_player_display_equivalence(name)
        if not key or not role:
            continue
        role = str(role).strip().lower()
        if not role or role == DEFAULT_WOM_ROLE:
            continue
        payload[key] = role
    if not payload:
        return 0
    try:
        client = _redis()
        redis_key = _key(wom_group_id)
        pipe = client.pipeline()
        pipe.delete(redis_key)
        pipe.hset(redis_key, mapping=payload)
        pipe.expire(redis_key, RANK_TTL_SECONDS)
        pipe.execute()
        return len(payload)
    except Exception as e:
        print(f"[ClanRanks] failed to store ranks for WOM group {wom_group_id}: {e}")
        return 0


#: group_id → wom_id, memoized so a busy channel doesn't query per chat line.
_wom_id_cache = {}
_WOM_ID_TTL_SECONDS = 300


def _group_wom_id(session, group_id):
    import time

    now = time.monotonic()
    cached = _wom_id_cache.get(int(group_id))
    if cached and now < cached[0]:
        return cached[1]
    try:
        from sqlalchemy import text as sql_text

        row = session.execute(
            sql_text("SELECT wom_id FROM groups WHERE group_id = :gid"),
            {"gid": int(group_id)},
        ).first()
        wom_id = int(row[0]) if row and row[0] else None
    except Exception:
        return cached[1] if cached else None
    _wom_id_cache[int(group_id)] = (now + _WOM_ID_TTL_SECONDS, wom_id)
    return wom_id


def rank_for_group_member(session, group_id, player_name) -> str:
    """Cached WOM role for a player in one DropTracker group, or None."""
    wom_id = _group_wom_id(session, group_id)
    if not wom_id:
        return None
    return rank_for_player(wom_id, player_name)


def rank_for_player(wom_group_id, player_name) -> str:
    """Cached WOM role for one member of one group, or None."""
    key = normalize_player_display_equivalence(player_name)
    if not wom_group_id or not key:
        return None
    try:
        value = _redis().hget(_key(wom_group_id), key)
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value or None
