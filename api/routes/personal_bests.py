"""Public personal-best boss catalogue.

    GET /pb_bosses              → every boss we track a personal best for
    GET /pb_bosses?withSizes=1  → each boss annotated with its team-size boards

"Tracked" means the boss has at least one ranked time on the public
leaderboards, so this list matches the boards the website renders:

  * hidden players (and hidden users) are excluded, same predicate as
    ``utils.pb_rank.pb_board_rank``;
  * NPCs on the global PB blocklist (``utils.pb_blocklist``) are dropped —
    the game exposes no personal best for them, so any rows we hold are junk.
    ``block_only`` blocks without purging, so this filter still matters even
    when the blocked ids currently have no rows;
  * team-size spellings are collapsed to canonical board tokens via
    ``utils.pb_rank.normalize_team_size`` ("(4 scale)" / "4 s" / "4" → "4",
    "1" → "Solo"), so a boss lists each board once.

A listed boss always has at least one team size: a boss whose only recorded
spellings are unusable has no board and is omitted entirely, matching
``web_api``'s dataset builder. The website's outlier guard (entries faster
than the board median / 4 are dropped as legacy ms-vs-ticks corruption) can
never empty a board — the median itself always survives it — so a board
listed here always has at least one entry on the site.
"""

from datetime import timedelta
from threading import Lock
import asyncio
import re
import time

from quart import Blueprint, jsonify, request
from quart_rate_limiter import rate_limit
from sqlalchemy import text

from api.core import get_db_session
from utils.pb_blocklist import get_blocked_ids
from utils.pb_rank import normalize_team_size

personal_bests_bp = Blueprint("personal_bests", __name__)

IMG_BASE = "https://www.droptracker.io/img"

# Full scan of personal_best (~70k rows, ~350ms cold). The catalogue only
# changes when a player records a PB on a boss/team size nobody has touched
# before, so a long TTL costs nothing. Per-process, like the /top_players
# cache in api/routes/players.py — with 6 hypercorn workers that is at most
# 6 cold queries per window.
_CACHE_TTL_SECONDS = 300.0

_cache = None
_cache_expires = 0.0
_cache_lock = Lock()

_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_PLUS_RE = re.compile(r"^(\d+)\s*\+$")

# Hidden-player predicate and the personal_best > 0 filter are copied from
# utils/pb_rank.py so the catalogue and the boards agree on what "ranked"
# means. DISTINCT keeps this to one row per (boss, raw team-size spelling).
_CATALOGUE_SQL = text(
    "SELECT DISTINCT pb.npc_id, n.npc_name, pb.team_size "
    "FROM personal_best pb "
    "JOIN npc_list n ON n.npc_id = pb.npc_id "
    "JOIN players p ON p.player_id = pb.player_id "
    "LEFT JOIN users u ON u.user_id = p.user_id "
    "WHERE pb.personal_best > 0 "
    "  AND COALESCE(p.hidden, 0) = 0 AND COALESCE(u.hidden, 0) = 0"
)


def _team_size_sort_key(ts: str):
    """Solo first, then exact sizes ascending, then ranges/open brackets.

    Mirrors web_api/routes/personal_bests.py so both surfaces order the
    boards for a boss identically.
    """
    if ts == "Solo":
        return (0, 0, "")
    if ts.isdigit():
        return (1, int(ts), "")
    m = _RANGE_RE.match(ts) or _PLUS_RE.match(ts)
    if m:
        return (2, int(m.group(1)), ts)
    return (3, 0, ts)


def _truthy(raw) -> bool:
    """Shared query-flag idiom (see /groups/board_update's `force`)."""
    return str(raw or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _build_catalogue() -> list:
    """[{npc_id, npc_name, icon_url, team_sizes}] sorted by boss name."""
    db_session = get_db_session()
    try:
        rows = db_session.execute(_CATALOGUE_SQL).fetchall()
    finally:
        db_session.close()

    blocked = get_blocked_ids()

    bosses = {}
    for npc_id, npc_name, raw_team_size in rows:
        npc_id = int(npc_id)
        if npc_id in blocked:
            continue
        team_size = normalize_team_size(raw_team_size)
        if team_size is None:
            # Tokens that can't describe a kill, e.g. a stored "0".
            continue
        entry = bosses.get(npc_id)
        if entry is None:
            entry = bosses[npc_id] = {
                "npc_id": npc_id,
                "npc_name": npc_name,
                "icon_url": f"{IMG_BASE}/npcdb/{npc_id}.png",
                "team_sizes": set(),
            }
        entry["team_sizes"].add(team_size)

    catalogue = sorted(bosses.values(), key=lambda b: b["npc_name"])
    for boss in catalogue:
        boss["team_sizes"] = sorted(boss["team_sizes"], key=_team_size_sort_key)
    return catalogue


def _catalogue() -> list:
    global _cache, _cache_expires
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now < _cache_expires:
            return _cache
    catalogue = _build_catalogue()
    with _cache_lock:
        _cache = catalogue
        _cache_expires = time.monotonic() + _CACHE_TTL_SECONDS
    return catalogue


@personal_bests_bp.get("/pb_bosses")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def pb_bosses():
    """Bosses we track personal bests for.

    Query parameters:
        withSizes (bool): include each boss's team-size boards. Accepts
            `with_sizes` as a snake_case alias. Default off.
    """
    with_sizes = _truthy(
        request.args.get("withSizes") or request.args.get("with_sizes")
    )

    catalogue = await asyncio.to_thread(_catalogue)

    if with_sizes:
        bosses = [dict(boss) for boss in catalogue]
    else:
        bosses = [
            {k: v for k, v in boss.items() if k != "team_sizes"}
            for boss in catalogue
        ]

    resp = jsonify({"bosses": bosses, "count": len(bosses)})
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200
