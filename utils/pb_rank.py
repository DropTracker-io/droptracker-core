"""Personal-best board rank for the site-wide ticker.

Mirrors the public PB leaderboard semantics in
``web_api/routes/personal_bests.py`` (one board per (boss, normalized team
size); an entry is a player's fastest time; implausibly fast legacy outliers
dropped relative to the board median) — duplicated here because the intake
path never imports the web_api package (same convention as
``utils/npc_names.npc_slug_sql_expr``). A small drift between this rank and
the website board is acceptable: the ticker only uses it as a "is this a
top-N time?" gate and headline number.
"""
from __future__ import annotations

import re
from statistics import median

from sqlalchemy import text

# See web_api/routes/personal_bests.py::_IMPLAUSIBLE_FACTOR — entries faster
# than (board median / 4) are corrupt legacy ms/ticks conversions, not kills.
_IMPLAUSIBLE_FACTOR = 4

_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_PLUS_RE = re.compile(r"^(\d+)\s*\+$")
_SCALE_TAIL_RE = re.compile(r"\s*s\w*\)?$", re.IGNORECASE)


def normalize_team_size(raw) -> str | None:
    """Canonical board token ("Solo", "2", "11-15", "6+") or None for junk."""
    s = str(raw or "").strip()
    if not s or s.lower() in ("solo", "1"):
        return "Solo"
    s = s.lstrip("(").strip()
    if s and s[0].isdigit():
        s = _SCALE_TAIL_RE.sub("", s).strip()
    if not s:
        return None
    if s.isdigit():
        if int(s) == 0:
            return None
        return "Solo" if s == "1" else str(int(s))
    m = _RANGE_RE.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = _PLUS_RE.match(s)
    if m:
        return f"{m.group(1)}+"
    return s


def pb_board_rank(session, npc_id: int, team_size, player_id: int) -> tuple[int, int] | None:
    """(rank, board_size) of ``player_id`` on the (npc, team-size) board, or
    None when the player isn't on the board (hidden, or their time was dropped
    by the outlier guard). One query per call — callers only invoke this on
    new-PB writes, never on the drop hot path.

    Matches the website board exactly: hidden players excluded, entries
    ordered by (time, date) with sequential ranks (ties get distinct ranks,
    like the site's row numbering).
    """
    board_token = normalize_team_size(team_size)
    if board_token is None:
        return None

    rows = session.execute(
        text(
            "SELECT pb.team_size, pb.player_id, pb.personal_best, pb.date_added "
            "FROM personal_best pb "
            "JOIN players p ON p.player_id = pb.player_id "
            "LEFT JOIN users u ON u.user_id = p.user_id "
            "WHERE pb.npc_id = :npc AND pb.personal_best > 0 "
            "  AND COALESCE(p.hidden, 0) = 0 AND COALESCE(u.hidden, 0) = 0"
        ),
        {"npc": int(npc_id)},
    ).fetchall()

    # Per-player best on this board: (time_ms, date_ts).
    best_by_player: dict[int, tuple[int, int]] = {}
    for raw_ts, pid, pb_ms, date_added in rows:
        if pid is None:
            continue
        if normalize_team_size(raw_ts) != board_token:
            continue
        pid, pb_ms = int(pid), int(pb_ms)
        try:
            date_ts = int(date_added.timestamp()) if date_added else 0
        except Exception:
            date_ts = 0
        cur = best_by_player.get(pid)
        if cur is None or pb_ms < cur[0]:
            best_by_player[pid] = (pb_ms, date_ts)

    if int(player_id) not in best_by_player:
        return None

    entries = sorted(
        ((t, d, pid) for pid, (t, d) in best_by_player.items()),
        key=lambda e: (e[0], e[1]),
    )
    if len(entries) >= 2:
        floor = median(t for t, _, _ in entries) / _IMPLAUSIBLE_FACTOR
        entries = [e for e in entries if e[0] >= floor]

    for i, (_, _, pid) in enumerate(entries):
        if pid == int(player_id):
            return i + 1, len(entries)
    return None
