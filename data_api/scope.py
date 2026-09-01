"""Who a key is allowed to read, and who is allowed to be seen at all.

Two independent gates, and a player must clear both:

1. **Visibility** — a hidden player, or one whose owning user is hidden, is
   invisible here exactly as it is on droptracker.io. The external API must
   never reveal more than the website would, so "hidden" and "does not exist"
   are the same 404. No separate code path decides this per endpoint.

2. **Scope** — a *group* key may read only its own members. A *user* key may
   read the players that user has claimed. A *global* key, issued by staff to
   a third-party integration, may read every group and every player.

**Gate 1 still applies to global keys.** "Read everything" means every
*visible* thing: a player who hid themselves, or whose account owner is
hidden, stays invisible to a partner site exactly as they are to a logged-out
visitor. Scope widens which rows a key may ask for; it never overrides a
person's own choice not to be listed. Nothing in this module lets a scope skip
:func:`visible_player_ids`.

Group 2 is the global pseudo-group containing every player, so a key scoped to
it would be a full-database export through the roster endpoint. It stays
refused for every scope — a global key enumerates through ``/v2/players``,
which is paged and priced for the job.
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
    """Whether ``key`` is in scope for one player.

    Visibility is the caller's separate check — see the module docstring.
    """
    if key.get("scope") == "global":
        return True

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


def key_may_read_group(key: dict, group_id: int) -> bool:
    """Whether ``key`` may read a group's roster at all.

    Group 2 is every player on the site; it is a bookkeeping artefact rather
    than a clan, and is refused to every scope so that "list a group" never
    doubles as "dump the database".
    """
    if group_id == GLOBAL_GROUP_ID:
        return False
    if key.get("scope") == "global":
        return True
    return key["owner_type"] == "group" and key["group_id"] == group_id


def group_exists(session, group_id: int) -> bool:
    """Whether a group row exists at all.

    Checked separately from the roster because an empty roster page is a
    perfectly good answer for a real group whose members are all hidden — but
    the identical answer for a group id that never existed is not an answer,
    it is a silent 200. Callers that reach this have already cleared
    :func:`key_may_read_group`, so a 404 here tells an in-scope caller their
    id is wrong and tells an out-of-scope caller nothing (they got a 403
    before the database was touched).
    """
    row = session.execute(text(
        "SELECT 1 FROM groups WHERE group_id = :gid LIMIT 1"
    ).bindparams(gid=group_id)).first()
    return row is not None


def group_page(session, after_id: int, limit: int) -> List[dict]:
    """One cursor page of real groups, for a global key to enumerate.

    Excludes the global pseudo-group, and reports the visible member count so
    a partner site's "members" figure matches the website's.
    """
    # The count is a CASE, not COUNT(a.player_id): a LEFT JOIN with the hidden
    # filter in its ON clause does not remove the association row, it only
    # nulls the joined columns — so counting the association counted hidden
    # players too and advertised more members than the API would return.
    # LEFT JOINs are kept so a group with no visible members still lists.
    rows = session.execute(text("""
        SELECT g.group_id, g.group_name, g.date_added,
               COUNT(DISTINCT CASE
                   WHEN p.player_id IS NOT NULL AND COALESCE(u.hidden, 0) = 0
                   THEN p.player_id END) AS members
        FROM groups g
        LEFT JOIN user_group_association a
               ON a.group_id = g.group_id AND a.player_id IS NOT NULL
        LEFT JOIN players p
               ON p.player_id = a.player_id AND COALESCE(p.hidden, 0) = 0
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE g.group_id > :after AND g.group_id <> :pseudo
        GROUP BY g.group_id, g.group_name, g.date_added
        ORDER BY g.group_id
        LIMIT :lim
    """).bindparams(after=after_id, pseudo=GLOBAL_GROUP_ID, lim=limit))
    return [{"group_id": int(r[0]), "name": r[1],
             "created": r[2].isoformat() if hasattr(r[2], "isoformat") else None,
             "members": int(r[3] or 0)} for r in rows]


def all_players_page(session, after_id: int, limit: int) -> List[int]:
    """One cursor page of every *visible* player, for a global key.

    The hidden filter is in the query rather than applied afterwards so a page
    is never short by however many hidden players it happened to span.
    """
    rows = session.execute(text("""
        SELECT p.player_id
        FROM players p
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE p.player_id > :after
          AND COALESCE(p.hidden, 0) = 0
          AND COALESCE(u.hidden, 0) = 0
        ORDER BY p.player_id
        LIMIT :lim
    """).bindparams(after=after_id, lim=limit))
    return [int(r[0]) for r in rows]


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
