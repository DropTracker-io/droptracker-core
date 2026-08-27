"""Who a key is allowed to read, and who is allowed to be seen at all.

Two independent gates, and a player must clear both:

1. **Visibility** — a hidden player, or one whose owning user is hidden, is
   invisible here exactly as it is on droptracker.io. The external API must
   never reveal more than the website would, so "hidden" and "does not exist"
   are the same 404. No separate code path decides this per endpoint.

2. **Scope** — a *group* key may read only its own members. A *user* key may
   read the players that user has claimed. Neither may enumerate the site.

Group 2 is the global pseudo-group containing every player, so a key scoped to
it would be a full-database export. It is refused explicitly rather than
allowed to fall out of the roster query.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import text

#: The pseudo-group that holds every player on the site.
GLOBAL_GROUP_ID = 2


def visible_player_ids(session, player_ids: List[int]) -> List[int]:
    """``player_ids`` minus anyone hidden, in the order given."""
    if not player_ids:
        return []
    rows = session.execute(text("""
        SELECT p.player_id
        FROM players p
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE p.player_id IN :ids
          AND COALESCE(p.hidden, 0) = 0
          AND COALESCE(u.hidden, 0) = 0
    """).bindparams(ids=tuple(player_ids)))
    allowed = {int(r[0]) for r in rows}
    return [pid for pid in player_ids if pid in allowed]


def key_may_read(session, key: dict, player_id: int) -> bool:
    """Whether ``key`` is in scope for one player."""
    if key["owner_type"] == "group":
        group_id = key["group_id"]
        if group_id == GLOBAL_GROUP_ID:
            return False
        row = session.execute(text("""
            SELECT 1 FROM user_group_association
            WHERE group_id = :gid AND player_id = :pid LIMIT 1
        """).bindparams(gid=group_id, pid=player_id)).first()
        return row is not None

    # A user key reads the accounts that user has claimed.
    row = session.execute(text("""
        SELECT 1 FROM players WHERE player_id = :pid AND user_id = :uid LIMIT 1
    """).bindparams(pid=player_id, uid=key["owner_user_id"])).first()
    return row is not None


def group_roster_page(session, group_id: int, after_id: int,
                      limit: int) -> List[int]:
    """One page of a group's visible members, ordered by player_id.

    Cursor pagination on the primary key, never OFFSET: a deep OFFSET makes the
    database walk and discard every skipped row, so page 500 costs 500 pages of
    work. ``after_id`` is the last id of the previous page.
    """
    rows = session.execute(text("""
        SELECT DISTINCT a.player_id
        FROM user_group_association a
        JOIN players p ON p.player_id = a.player_id
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE a.group_id = :gid
          AND a.player_id IS NOT NULL
          AND a.player_id > :after
          AND COALESCE(p.hidden, 0) = 0
          AND COALESCE(u.hidden, 0) = 0
        ORDER BY a.player_id
        LIMIT :lim
    """).bindparams(gid=group_id, after=after_id, lim=limit))
    return [int(r[0]) for r in rows]


def resolve_player_ref(session, ref: str) -> Optional[int]:
    """A path segment that is either a player id or an exact name."""
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.isdigit():
        return int(ref)
    row = session.execute(text("""
        SELECT player_id FROM players WHERE player_name = :name LIMIT 1
    """).bindparams(name=ref)).first()
    return int(row[0]) if row else None
