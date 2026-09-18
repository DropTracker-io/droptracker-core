"""Group points standings -- the one place that decides who stands where.

Every surface that shows a clan's custom points reads through this module: the
website leaderboard (``web_api/routes/points.py``), the Discord commands
(``commands/points.py``) and the "current total" a notification prints
(``data/submissions/point_awards.py``, ``services/entry_modifier.py``). They
used to each run their own ``SUM(amount) GROUP BY player_id``, which is how the
site and the bot could disagree about a total, and why neither knew about the
two rules below.

**Only current members stand.** ``player_points`` is a ledger and is never
edited when somebody leaves: the row that goes away is their
``user_group_association`` membership (the WOM sync's ``Player.remove_group``).
Standings therefore count a player's points only while that membership row
exists. Nothing is deleted -- a player who rejoins gets their history back, and
a WOM roster that briefly comes back short cannot destroy anybody's points. The
admin history view deliberately does NOT apply this rule: it is the audit trail.

**Accounts can be combined per Discord user.** With the group's
``points_combine_accounts`` behavior on, every RSN a Discord user has claimed
*that is currently in the group* is summed into one standing. The two rules
compose on purpose: an alt that left the clan stops counting while the main
that stayed keeps its place. Unclaimed players always stand alone, and off (the
default) is the per-RSN board clans have always had.

Deliberately free of ORM-model imports: the SQL is ``text()`` so the unit suite,
which stubs the ``db`` package, can run the real queries against SQLite instead
of asserting on a mock that agrees with whatever it is told.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from sqlalchemy import bindparam, text

from utils.rsn import rsn_contains

#: Group behavior key (``group_configurations``), written by the points settings
#: route as "1"/"0". Absent means off.
COMBINE_CONFIG_KEY = "points_combine_accounts"

#: Expanding IN lists are sent in slices: a big clan's roster is thousands of
#: ids, and one statement carrying all of them is a needless packet.
_IN_CHUNK = 500


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MemberTotal:
    """One current member's points inside the window being ranked."""

    player_id: int
    name: str
    user_id: Optional[int]
    hidden: bool
    points: int


@dataclass(frozen=True)
class StandingAccount:
    player_id: int
    name: str
    points: int
    hidden: bool = False


@dataclass(frozen=True)
class Standing:
    """One row of a board: a single RSN, or a Discord user's combined RSNs."""

    rank: int
    points: int
    #: Set only when the entry was grouped under a Discord user (combine on and
    #: the player is claimed). ``0`` and negative ids are real users -- test
    #: with ``is not None``.
    user_id: Optional[int]
    #: Most points first. Hidden accounts stay in here (they count towards the
    #: total) but are never named -- read ``visible_accounts`` for display.
    accounts: tuple

    @property
    def visible_accounts(self) -> tuple:
        return tuple(a for a in self.accounts if not a.hidden)

    @property
    def primary(self) -> Optional[StandingAccount]:
        """The account the row is shown under: the top-scoring visible one."""
        visible = self.visible_accounts
        return visible[0] if visible else None

    @property
    def hidden(self) -> bool:
        """No account that may be named -- the row is omitted from public
        boards, keeping its rank (a gap, the same as the loot boards)."""
        return not self.visible_accounts

    @property
    def combined(self) -> bool:
        return len(self.accounts) > 1

    def has_player(self, player_id: int) -> bool:
        return any(a.player_id == int(player_id) for a in self.accounts)


# --------------------------------------------------------------------------- #
# Pure: build, search, page
# --------------------------------------------------------------------------- #
def build_standings(rows: Iterable[MemberTotal], *, combine: bool) -> list:
    """Rank current members' totals into standings.

    ``rows`` must already be restricted to current members (``load_member_totals``
    does that); this only groups and orders them. Accounts and entries that net
    to zero are dropped, matching the old ``HAVING SUM(amount) != 0`` -- a
    negative total is a real standing, an empty one is not.

    Order is points descending, then the entry's lowest player id, so a tie is
    stable from one request to the next.
    """
    grouped: dict = {}
    for row in rows:
        points = int(row.points or 0)
        if points == 0:
            continue
        owner = row.user_id if (combine and row.user_id is not None) else None
        key = ("u", int(owner)) if owner is not None else ("p", int(row.player_id))
        grouped.setdefault(key, []).append(
            StandingAccount(
                player_id=int(row.player_id),
                name=row.name or f"Player {row.player_id}",
                points=points,
                hidden=bool(row.hidden),
            )
        )

    entries = []
    for (kind, ident), accounts in grouped.items():
        total = sum(a.points for a in accounts)
        if total == 0:
            continue
        accounts.sort(key=lambda a: (-a.points, a.player_id))
        entries.append((
            total,
            min(a.player_id for a in accounts),
            ident if kind == "u" else None,
            tuple(accounts),
        ))
    entries.sort(key=lambda e: (-e[0], e[1]))

    return [
        Standing(rank=pos, points=total, user_id=user_id, accounts=accounts)
        for pos, (total, _tiebreak, user_id, accounts) in enumerate(entries, start=1)
    ]


def visible_standings(standings: Sequence[Standing]) -> list:
    """Standings that may be shown publicly. Ranks are untouched, so a hidden
    player leaves a gap rather than promoting everybody below them."""
    return [s for s in standings if not s.hidden]


def search_standings(standings: Sequence[Standing], query: Optional[str]) -> list:
    """Standings with a visible account whose name contains ``query`` under
    OSRS name equivalence ("tzuk-kal" finds "tzuk kal lag"). Each keeps the rank
    it holds on the full board -- a search narrows the list, it does not
    re-rank it. A hidden account's name never matches: that would confirm the
    name to anybody who guessed it."""
    needle = (query or "").strip()
    if not needle:
        return list(standings)
    return [
        s for s in standings
        if any(rsn_contains(a.name, needle) for a in s.visible_accounts)
    ]


def find_standing(
    standings: Sequence[Standing],
    *,
    player_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Optional[Standing]:
    """The standing holding ``player_id`` (or, on a combined board, belonging
    to ``user_id``). None means unranked: no points in the window, or not a
    current member."""
    if player_id is not None:
        for s in standings:
            if s.has_player(player_id):
                return s
    if user_id is not None:
        for s in standings:
            if s.user_id is not None and s.user_id == int(user_id):
                return s
    return None


def paginate(items: Sequence, page: int, limit: int) -> tuple:
    """``(page_items, page, pages)`` with ``page`` clamped into range, so a
    stale "next" button on a board that shrank lands on the last page instead
    of an empty one."""
    limit = max(1, int(limit))
    pages = max(1, -(-len(items) // limit))
    page = min(max(1, int(page)), pages)
    start = (page - 1) * limit
    return list(items[start:start + limit]), page, pages


def page_of(standings: Sequence[Standing], target: Standing, limit: int) -> int:
    """Which page of ``standings`` holds ``target`` (1 when it is not there)."""
    for index, s in enumerate(standings):
        if s == target:
            return index // max(1, int(limit)) + 1
    return 1


# --------------------------------------------------------------------------- #
# Pure: leaderboard windows
# --------------------------------------------------------------------------- #
def resolve_period(period: Optional[str], now: Optional[datetime] = None) -> tuple:
    """A period token -> ``(token, start, end)``; ``end`` is exclusive and
    ``(None, None)`` bounds mean all-time.

    Accepts the presets (``all``, ``day``, ``week``, ``month``) and the explicit
    partitions the site links to (``YYYYMMDD``, ``YYYYwWW``, ``YYYYMM``).
    Seasons are the caller's to resolve -- they need the group's rows. Raises
    ``ValueError`` with a user-presentable message on a malformed partition; an
    unrecognised word falls back to the current month, as it always has.

    Naive server-local time, the same domain ``player_points.date_added`` is
    written in (``func.now()`` on a DB that shares this box's clock).
    """
    period = (period or "month").strip().lower()
    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "all":
        return "all", None, None

    if period == "day" or (len(period) == 8 and period.isdigit()):
        if period == "day":
            day = midnight
        else:
            try:
                day = datetime.strptime(period, "%Y%m%d")
            except Exception:
                raise ValueError(f"Unrecognized day partition '{period}'.")
        return day.strftime("%Y%m%d"), day, day + timedelta(days=1)

    if period == "week" or (len(period) == 7 and period[4] == "w"):
        if period == "week":
            monday = midnight - timedelta(days=now.weekday())
        else:
            try:
                monday = datetime.fromisocalendar(int(period[:4]), int(period[5:]), 1)
            except Exception:
                raise ValueError(f"Unrecognized week partition '{period}'.")
        iso = monday.isocalendar()
        return f"{iso[0]}W{iso[1]:02d}", monday, monday + timedelta(days=7)

    # Month (default): "month" or YYYYMM.
    start = midnight.replace(day=1)
    if period != "month":
        try:
            start = datetime.strptime(period, "%Y%m")
        except Exception:
            pass
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.strftime("%Y%m"), start, end


# --------------------------------------------------------------------------- #
# DB: config, membership, totals
# --------------------------------------------------------------------------- #
def _chunks(values: Sequence[int]):
    for index in range(0, len(values), _IN_CHUNK):
        yield values[index:index + _IN_CHUNK]


def combine_enabled(session, group_id: int) -> bool:
    """Whether ``group_id`` sums a Discord user's RSNs into one standing."""
    row = session.execute(
        text(
            "SELECT config_value FROM group_configurations "
            "WHERE group_id = :gid AND config_key = :key "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"gid": int(group_id), "key": COMBINE_CONFIG_KEY},
    ).first()
    if row is None or row[0] is None:
        return False
    # Strictly "1", the way the settings route reads it back: a looser parse
    # here would let the boards combine while the admin's toggle shows off.
    return str(row[0]).strip() == "1"


def member_player_ids(session, group_id: int, player_ids: Optional[Sequence[int]] = None) -> set:
    """Player ids currently in the group -- all of them, or the subset of
    ``player_ids`` that are.

    ``user_group_association`` also carries Discord-user rows (``player_id``
    NULL), and its unique key does not stop duplicate player rows (NULL
    ``user_id`` never collides), so this returns a set and is never joined
    against: a join would double a duplicated member's points.
    """
    gid = int(group_id)
    if player_ids is None:
        rows = session.execute(
            text(
                "SELECT player_id FROM user_group_association "
                "WHERE group_id = :gid AND player_id IS NOT NULL"
            ),
            {"gid": gid},
        ).fetchall()
        return {int(r[0]) for r in rows}

    wanted = sorted({int(p) for p in player_ids})
    if not wanted:
        return set()
    stmt = text(
        "SELECT player_id FROM user_group_association "
        "WHERE group_id = :gid AND player_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    out: set = set()
    for chunk in _chunks(wanted):
        out.update(int(r[0]) for r in session.execute(stmt, {"gid": gid, "ids": chunk}).fetchall())
    return out


def _window_clause(start, end) -> tuple:
    clause, params = "", {}
    if start is not None:
        clause += " AND date_added >= :start"
        params["start"] = start
    if end is not None:
        clause += " AND date_added < :end"
        params["end"] = end
    return clause, params


def load_member_totals(session, group_id: int, *, start=None, end=None) -> list:
    """Every current member's points inside ``[start, end)``.

    Three index-backed reads rather than one join: the totals resolve from
    ``ix_player_points_group_date`` alone, the roster from the association's
    group index, and the intersection is a set operation here -- no optimizer
    to second-guess, and immune to duplicate association rows.
    """
    gid = int(group_id)
    clause, params = _window_clause(start, end)
    totals = {
        int(pid): int(points or 0)
        for pid, points in session.execute(
            text(
                "SELECT player_id, SUM(amount) FROM player_points "
                f"WHERE group_id = :gid{clause} GROUP BY player_id"
            ),
            {"gid": gid, **params},
        ).fetchall()
    }
    if not totals:
        return []

    members = member_player_ids(session, gid)
    standing_ids = sorted(pid for pid in totals if pid in members)
    if not standing_ids:
        return []

    stmt = text(
        "SELECT p.player_id, p.player_name, p.user_id, "
        "COALESCE(p.hidden, 0), COALESCE(u.hidden, 0) "
        "FROM players p LEFT JOIN users u ON u.user_id = p.user_id "
        "WHERE p.player_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    out = []
    for chunk in _chunks(standing_ids):
        for pid, name, user_id, p_hidden, u_hidden in session.execute(stmt, {"ids": chunk}).fetchall():
            out.append(MemberTotal(
                player_id=int(pid),
                name=name or f"Player {pid}",
                user_id=int(user_id) if user_id is not None else None,
                hidden=bool(p_hidden) or bool(u_hidden),
                points=totals[int(pid)],
            ))
    return out


def load_standings(session, group_id: int, *, start=None, end=None,
                   combine: Optional[bool] = None) -> list:
    """The group's ranked board for a window. ``combine=None`` reads the
    group's own setting; pass a bool to override it (the settings preview)."""
    if combine is None:
        combine = combine_enabled(session, group_id)
    return build_standings(
        load_member_totals(session, group_id, start=start, end=end),
        combine=bool(combine),
    )


def counted_player_ids(session, group_id: int, player_id: int,
                       combine: Optional[bool] = None) -> list:
    """Whose ledger rows make up the total shown for ``player_id``.

    Per-RSN board: just that player. Combined board: every account of the same
    Discord user that is currently in the group -- always including
    ``player_id`` itself, so a total printed at award time is never missing the
    award that triggered it (the receiver is a member by construction, but a
    roster read can race a join).
    """
    pid = int(player_id)
    if combine is None:
        combine = combine_enabled(session, group_id)
    if not combine:
        return [pid]
    owner = session.execute(
        text("SELECT user_id FROM players WHERE player_id = :pid"), {"pid": pid}
    ).first()
    if owner is None or owner[0] is None:
        return [pid]
    siblings = [
        int(r[0]) for r in session.execute(
            text("SELECT player_id FROM players WHERE user_id = :uid"),
            {"uid": int(owner[0])},
        ).fetchall()
    ]
    in_group = member_player_ids(session, group_id, siblings)
    in_group.add(pid)
    return sorted(in_group)


def display_total(session, group_id: int, player_id: int,
                  combine: Optional[bool] = None) -> int:
    """The all-time total to print next to ``player_id`` in this group: their
    own, or their Discord user's combined in-group total when the group
    combines accounts."""
    ids = counted_player_ids(session, group_id, player_id, combine)
    stmt = text(
        "SELECT COALESCE(SUM(amount), 0) FROM player_points "
        "WHERE group_id = :gid AND player_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    row = session.execute(stmt, {"gid": int(group_id), "ids": ids}).first()
    return int((row[0] if row else 0) or 0)


def load_seasons(session, group_id: int) -> list:
    """The group's leaderboard seasons, newest first, as plain dicts
    (``id``, ``name``, ``start_at``, ``end_at``)."""
    rows = session.execute(
        text(
            "SELECT id, name, start_at, end_at FROM group_point_seasons "
            "WHERE group_id = :gid ORDER BY start_at DESC"
        ),
        {"gid": int(group_id)},
    ).fetchall()
    return [
        {"id": int(r[0]), "name": r[1], "start_at": r[2], "end_at": r[3]}
        for r in rows
    ]


def recent_awards(session, group_id: int, player_ids: Sequence[int], limit: int = 5) -> list:
    """The newest ledger rows for these players in this group, as plain dicts
    (``player_id``, ``amount``, ``reason``, ``date_added``)."""
    ids = sorted({int(p) for p in player_ids})
    if not ids:
        return []
    stmt = text(
        "SELECT player_id, amount, reason, date_added FROM player_points "
        "WHERE group_id = :gid AND player_id IN :ids "
        "ORDER BY id DESC LIMIT :lim"
    ).bindparams(bindparam("ids", expanding=True))
    rows = session.execute(
        stmt, {"gid": int(group_id), "ids": ids, "lim": max(1, int(limit))}
    ).fetchall()
    return [
        {"player_id": int(r[0]), "amount": int(r[1] or 0),
         "reason": r[2] or "", "date_added": r[3]}
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# DB: reads behind the Discord cards (/my-points, /lookup)
# --------------------------------------------------------------------------- #
def totals_by_group(session, player_ids: Sequence[int]) -> dict:
    """``{group_id: points}`` across these accounts, counting each account only
    in the groups it is currently a member of -- the same leaver rule as the
    boards, applied per group. Groups that net to zero are left out."""
    ids = sorted({int(p) for p in player_ids})
    if not ids:
        return {}
    in_stmt = bindparam("ids", expanding=True)
    memberships = {
        (int(pid), int(gid))
        for pid, gid in session.execute(
            text(
                "SELECT player_id, group_id FROM user_group_association "
                "WHERE player_id IN :ids"
            ).bindparams(in_stmt),
            {"ids": ids},
        ).fetchall()
    }
    out: dict = {}
    for pid, gid, points in session.execute(
        text(
            "SELECT player_id, group_id, SUM(amount) FROM player_points "
            "WHERE player_id IN :ids AND group_id IS NOT NULL "
            "GROUP BY player_id, group_id"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).fetchall():
        if (int(pid), int(gid)) in memberships:
            out[int(gid)] = out.get(int(gid), 0) + int(points or 0)
    return {gid: pts for gid, pts in out.items() if pts != 0}


def group_names(session, group_ids: Sequence[int]) -> dict:
    ids = sorted({int(g) for g in group_ids})
    if not ids:
        return {}
    rows = session.execute(
        text("SELECT group_id, group_name FROM `groups` WHERE group_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).fetchall()
    return {int(gid): (name or f"Group #{gid}") for gid, name in rows}


def load_points_card(session, group_id: int, player_ids: Sequence[int], *,
                     now: Optional[datetime] = None, recent: int = 5) -> dict:
    """Everything a player card says about these accounts in this group: the
    all-time and this-month boards (so the card can quote a rank *of how
    many*), which of the accounts are current members, and the newest awards
    of the ones that are.

    Boards are returned whole rather than as a pre-picked standing because the
    shape differs by mode -- one combined standing, or one per RSN -- and the
    renderer already knows how to ask (``find_standing``).
    """
    ids = sorted({int(p) for p in player_ids})
    combine = combine_enabled(session, group_id)
    members = member_player_ids(session, group_id, ids)
    token, start, end = resolve_period("month", now)
    return {
        "combined": combine,
        "member_ids": members,
        "all_time": load_standings(session, group_id, combine=combine),
        "month": load_standings(session, group_id, start=start, end=end, combine=combine),
        "month_token": token,
        "recent": recent_awards(session, group_id, sorted(members), recent) if members else [],
    }


def search_member_names(session, group_id: Optional[int], typed: str, limit: int = 25) -> list:
    """RSN suggestions for an autocomplete box: this group's current members
    whose name contains ``typed`` (OSRS name equivalence), or -- with no group,
    e.g. in a DM -- any player whose name starts with it.

    Hidden players are never suggested. The in-group form folds separators in
    SQL over one clan's roster; the global form is a plain indexed prefix seek,
    because a ``%...%`` scan of every player on each keystroke is not something
    an autocomplete gets to cost.
    """
    folded = " ".join(str(typed or "").replace("-", " ").replace("_", " ").split()).lower()
    escaped = folded.replace("!", "!!").replace("%", "!%")
    limit = max(1, min(int(limit), 25))
    visible = (
        "COALESCE(p.hidden, 0) = 0 AND NOT EXISTS ("
        "SELECT 1 FROM users u WHERE u.user_id = p.user_id AND COALESCE(u.hidden, 0) = 1)"
    )
    if group_id is not None:
        rows = session.execute(
            text(
                "SELECT p.player_name FROM players p "
                "WHERE p.player_id IN (SELECT player_id FROM user_group_association "
                "                      WHERE group_id = :gid AND player_id IS NOT NULL) "
                "AND REPLACE(REPLACE(LOWER(p.player_name), '-', ' '), '_', ' ') "
                "    LIKE :needle ESCAPE '!' "
                f"AND {visible} "
                "ORDER BY p.player_name LIMIT :lim"
            ),
            {"gid": int(group_id), "needle": f"%{escaped}%", "lim": limit},
        ).fetchall()
    else:
        if not folded:
            return []
        rows = session.execute(
            text(
                "SELECT p.player_name FROM players p "
                "WHERE p.player_name LIKE :needle ESCAPE '!' "
                f"AND {visible} "
                "ORDER BY p.player_name LIMIT :lim"
            ),
            {"needle": f"{escaped}%", "lim": limit},
        ).fetchall()
    seen, out = set(), []
    for (name,) in rows:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out
