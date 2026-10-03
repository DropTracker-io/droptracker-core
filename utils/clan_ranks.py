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


# ── account types (game modes) ──────────────────────────────────────────────
#
# The mirror line also carries the sender's account-type badge, the way the
# game draws one before an ironman's name in the chat box. Three sources, best
# first: the relaying plugin (it reads the badge off the chat line itself, so
# it covers every clanmate and every mode — plugin 6.0.17+), the sender's own
# state sync (``player_state.account_type``, plugin users only), and WOM's
# player type from the same hourly membership sync as the ranks above. WOM
# knows no group modes, so a GIM without the plugin renders no badge until a
# relayer on a new build sees them talk.

#: WOM ``PlayerType`` value → our wire string (``utils.account_types``).
#: ``regular``, ``unknown`` and ``fresh_start`` carry no badge.
WOM_ACCOUNT_TYPES = {
    "ironman": "ironman",
    "hardcore": "hardcore_ironman",
    "ultimate": "ultimate_ironman",
}

#: Group roster → account types, memoized per process. A badge is cosmetic and
#: a player's mode changes rarely (a hardcore death, leaving a group), so five
#: minutes of staleness costs nothing and saves a roster query per chat line.
_ROSTER_TYPES_TTL_SECONDS = 300
_roster_types_cache = {}


def _type_key(wom_group_id) -> str:
    return f"clantype:{int(wom_group_id)}"


def store_group_account_types(wom_group_id, types: dict) -> int:
    """Replace the cached WOM account-type map for one WOM group.

    ``types`` is ``{player display name: WOM player type}``; only modes with a
    badge are stored. Same replace-then-expire shape as the rank map."""
    if not wom_group_id or not types:
        return 0
    payload = {}
    for name, wom_type in types.items():
        key = normalize_player_display_equivalence(name)
        mapped = WOM_ACCOUNT_TYPES.get(str(wom_type or "").strip().lower())
        if key and mapped:
            payload[key] = mapped
    try:
        client = _redis()
        redis_key = _type_key(wom_group_id)
        pipe = client.pipeline()
        pipe.delete(redis_key)
        if payload:
            pipe.hset(redis_key, mapping=payload)
            pipe.expire(redis_key, RANK_TTL_SECONDS)
        pipe.execute()
        return len(payload)
    except Exception as e:
        print(f"[ClanRanks] failed to store account types for WOM group {wom_group_id}: {e}")
        return 0


def _roster_account_types(session, group_id) -> dict:
    """``{normalized name: wire string}`` for a group's plugin-synced members."""
    import time

    from utils.account_types import VALID_ACCOUNT_TYPES, account_type_from_varbit

    now = time.monotonic()
    cached = _roster_types_cache.get(int(group_id))
    if cached and now < cached[0]:
        return cached[1]
    try:
        from sqlalchemy import text as sql_text

        rows = session.execute(
            sql_text(
                "SELECT p.player_name, ps.account_type, p.account_type "
                "FROM user_group_association uga "
                "JOIN players p ON p.player_id = uga.player_id "
                "LEFT JOIN player_state ps ON ps.player_id = p.player_id "
                "WHERE uga.group_id = :gid "
                "AND (ps.account_type IS NOT NULL OR p.account_type IS NOT NULL)"
            ),
            {"gid": int(group_id)},
        ).all()
    except Exception:
        return cached[1] if cached else {}
    types = {}
    for name, varbit, stored in rows:
        key = normalize_player_display_equivalence(name or "")
        if not key:
            continue
        decoded = account_type_from_varbit(varbit)
        if decoded is None and stored in VALID_ACCOUNT_TYPES:
            decoded = stored
        if decoded:
            types[key] = decoded
    _roster_types_cache[int(group_id)] = (now + _ROSTER_TYPES_TTL_SECONDS, types)
    return types


def account_type_for_group_member(session, group_id, player_name):
    """A clan member's account type as a wire string with a badge, or None.

    State sync first (the game's own varbit, so it knows group modes), then
    the WOM cache. ``"normal"`` reads as None: no badge to draw."""
    key = normalize_player_display_equivalence(player_name)
    if not key or not group_id:
        return None
    found = _roster_account_types(session, group_id).get(key)
    if found is None:
        wom_id = _group_wom_id(session, group_id)
        if wom_id:
            try:
                value = _redis().hget(_type_key(wom_id), key)
            except Exception:
                value = None
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            found = value or None
    return None if found in (None, "", "normal") else found
