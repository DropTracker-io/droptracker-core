"""Public personal-best (kill time) leaderboards.

  GET /api/v1/personal-bests/bosses                (public, cached; ?group_id=)
  GET /api/v1/personal-bests/board?npc_id=<id>     (public, cached; ?group_id=&limit=)

Replaces the old XenForo "personal_bests" page. One board per (boss,
normalized team size); an entry is a player's fastest recorded time on that
board. Optional ``group_id`` scopes every board to the group's members (the
group-page "Personal bests" tab) and annotates entries with their global rank.

Data hygiene (the old page rendered the raw table and looked broken):
  * ``team_size`` strings are normalized — legacy rows hold truncated scale
    variants like ``"(4"`` / ``"4 s"`` alongside ``"4"``.
  * Implausibly fast outliers (old ms/ticks conversion bugs, e.g. 1.0s
    Cerberus "kills") are dropped per-board relative to the board's median,
    so genuinely fast bosses keep their legitimate sub-5s boards.
"""
from __future__ import annotations

import asyncio
import re
from statistics import median

from quart import Blueprint, jsonify, request
from sqlalchemy import text

from db import Group, Player
from web_api.common import (
    abort_problem,
    cache_get,
    cache_set,
    db_session,
    hidden_player_ids,
    proof_url,
    with_cache_headers,
)

personal_bests_bp = Blueprint("v1_personal_bests", __name__)

IMG_BASE = "https://www.droptracker.io/img"

# Raid encounters pinned to the top of the boss index, in this order (display
# concern only — unknown/missing ids are skipped gracefully).
_FEATURED_NPC_IDS = (
    13696,  # Chambers of Xeric
    14150,  # Chambers of Xeric Challenge Mode
    13699,  # Theatre of Blood
    13961,  # Theatre of Blood: Hard Mode
    13958,  # Theatre of Blood: Entry Mode
    13695,  # Tombs of Amascut
    13970,  # Tombs of Amascut: Expert Mode
    13959,  # Tombs of Amascut: Entry Mode
)

# A board entry faster than (board median / factor) is treated as corrupt
# legacy data and hidden. Median-relative (not absolute) so quick-kill bosses
# with legitimate 1-2s times keep their boards while a "1.0s Cerberus" drops.
# 4 was chosen empirically: real record-vs-median ratios in this data top out
# around 3x (CoX solo 9:17 vs ~25min median) while the ms/ticks garbage band
# sits 4-30x below its board median.
_IMPLAUSIBLE_FACTOR = 4

_DATASET_TTL_GLOBAL = 300.0
_DATASET_TTL_GROUP = 120.0

_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_PLUS_RE = re.compile(r"^(\d+)\s*\+$")
# Trailing remnants of truncated "(N scale)" spellings: "4 s", "11-15 s".
_SCALE_TAIL_RE = re.compile(r"\s*s\w*\)?$", re.IGNORECASE)


def _convert_from_ms(ms: int) -> str:
    """m:ss.t / h:mm:ss.t — same format the profile endpoints use."""
    try:
        ms = int(ms)
    except Exception:
        return ""
    hours = ms // 3_600_000
    ms %= 3_600_000
    minutes = ms // 60_000
    ms %= 60_000
    seconds = ms // 1000
    tenths = (ms % 1000) // 100
    if hours > 0:
        return f"{hours}:{minutes:02}:{seconds:02}.{tenths}"
    return f"{minutes}:{seconds:02}.{tenths}"


def normalize_team_size(raw) -> str | None:
    """Collapse legacy team-size spellings to one canonical token per board.

    Returns ``None`` for tokens that can't describe a kill (e.g. ``"0"``) —
    callers skip those rows entirely.
    """
    s = str(raw or "").strip()
    if not s or s.lower() in ("solo", "1"):
        return "Solo"
    s = s.lstrip("(").strip()  # truncated "(4 scale)" → "4 scale)"
    if s and s[0].isdigit():
        # "4 s" / "11-15 s" → "4" / "11-15" (digit-led only, so a textual
        # token ending in "s" is never mangled).
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


def _team_size_sort_key(ts: str):
    """Solo first, then exact sizes ascending, then ranges/open brackets."""
    if ts == "Solo":
        return (0, 0, "")
    if ts.isdigit():
        return (1, int(ts), "")
    m = _RANGE_RE.match(ts) or _PLUS_RE.match(ts)
    if m:
        return (2, int(m.group(1)), ts)
    return (3, 0, ts)


def team_size_label(ts: str) -> str:
    if ts == "Solo":
        return "Solo"
    return f"{ts} players"


def _build_dataset(group_id: int | None) -> dict:
    """{npc_id: {name, entry_count, player_count, boards{ts: [entry…]}}}.

    Board entries are per-player bests, fastest first, outliers dropped —
    FULL lists (serialization truncates) so group views can cite global rank.
    """
    hidden = hidden_player_ids()
    group_filter = ""
    params: dict = {}
    if group_id is not None:
        group_filter = (
            "AND EXISTS (SELECT 1 FROM user_group_association uga "
            "WHERE uga.player_id = pb.player_id AND uga.group_id = :gid) "
        )
        params["gid"] = group_id
    sql = text(
        "SELECT pb.npc_id, n.npc_name, pb.team_size, pb.player_id, "
        "       pb.personal_best, pb.date_added, pb.image_url "
        "FROM personal_best pb "
        "JOIN npc_list n ON n.npc_id = pb.npc_id "
        f"WHERE pb.personal_best > 0 {group_filter}"
    )
    with db_session() as s:
        rows = s.execute(sql, params).fetchall()

    npcs: dict[int, dict] = {}
    # (npc_id, ts) -> {player_id: (time_ms, date_ts, image_url)}
    best: dict[tuple, dict] = {}
    entry_counts: dict[int, int] = {}
    players_by_npc: dict[int, set] = {}
    for npc_id, npc_name, raw_ts, pid, pb_ms, date_added, image_url in rows:
        if pid is None or int(pid) in hidden:
            continue
        ts = normalize_team_size(raw_ts)
        if ts is None:
            continue
        npc_id = int(npc_id)
        npcs.setdefault(npc_id, {"name": npc_name})
        entry_counts[npc_id] = entry_counts.get(npc_id, 0) + 1
        players_by_npc.setdefault(npc_id, set()).add(int(pid))
        board = best.setdefault((npc_id, ts), {})
        cur = board.get(int(pid))
        if cur is None or int(pb_ms) < cur[0]:
            try:
                date_ts = int(date_added.timestamp()) if date_added else None
            except Exception:
                date_ts = None
            board[int(pid)] = (int(pb_ms), date_ts, proof_url(image_url))

    for (npc_id, ts), by_player in best.items():
        entries = sorted(
            (
                {"player_id": pid, "time_ms": t, "date_ts": d, "image_url": img}
                for pid, (t, d, img) in by_player.items()
            ),
            key=lambda e: (e["time_ms"], e["date_ts"] or 0),
        )
        if len(entries) >= 2:
            floor = median(e["time_ms"] for e in entries) / _IMPLAUSIBLE_FACTOR
            entries = [e for e in entries if e["time_ms"] >= floor]
        if not entries:
            continue
        npcs[npc_id].setdefault("boards", {})[ts] = entries

    out: dict[int, dict] = {}
    for npc_id, data in npcs.items():
        boards = data.get("boards")
        if not boards:
            continue
        out[npc_id] = {
            "name": data["name"],
            "entry_count": entry_counts.get(npc_id, 0),
            "player_count": len(players_by_npc.get(npc_id, ())),
            "boards": dict(
                sorted(boards.items(), key=lambda kv: _team_size_sort_key(kv[0]))
            ),
        }
    return out


def _dataset(group_id: int | None) -> dict:
    key = f"pb:data:group:{group_id}" if group_id is not None else "pb:data:global"
    ttl = _DATASET_TTL_GROUP if group_id is not None else _DATASET_TTL_GLOBAL
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    data = _build_dataset(group_id)
    cache_set(key, data)
    return data


def _player_names(ids: set) -> dict:
    if not ids:
        return {}
    with db_session() as s:
        rows = (
            s.query(Player.player_id, Player.player_name)
            .filter(Player.player_id.in_(ids))
            .all()
        )
    return {int(pid): name for pid, name in rows}


def _group_name(group_id: int) -> str | None:
    with db_session() as s:
        g = s.query(Group.group_name).filter(Group.group_id == group_id).first()
    return g[0] if g else None


def _parse_group_id() -> int | None:
    raw = request.args.get("group_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer.")


@personal_bests_bp.get("/personal-bests/bosses")
async def pb_bosses():
    """Boss index: every boss with at least one ranked time, raids pinned first."""
    group_id = _parse_group_id()

    def _load():
        data = _dataset(group_id)
        # Overall fastest per boss (across team sizes) for the index cards.
        best_by_npc: dict[int, dict] = {}
        for npc_id, info in data.items():
            fastest = None
            for ts, entries in info["boards"].items():
                head = entries[0]
                if fastest is None or head["time_ms"] < fastest["time_ms"]:
                    fastest = {**head, "team_size": ts}
            best_by_npc[npc_id] = fastest
        names = _player_names({b["player_id"] for b in best_by_npc.values() if b})

        featured_rank = {nid: i for i, nid in enumerate(_FEATURED_NPC_IDS)}
        bosses = []
        for npc_id, info in data.items():
            fastest = best_by_npc.get(npc_id)
            bosses.append(
                {
                    "npc_id": npc_id,
                    "name": info["name"],
                    "entry_count": info["entry_count"],
                    "player_count": info["player_count"],
                    "featured": npc_id in featured_rank,
                    "team_sizes": list(info["boards"].keys()),
                    "best": None
                    if fastest is None
                    else {
                        "time_ms": fastest["time_ms"],
                        "time_display": _convert_from_ms(fastest["time_ms"]),
                        "team_size": fastest["team_size"],
                        "player_id": fastest["player_id"],
                        "player_name": names.get(fastest["player_id"], "Unknown"),
                    },
                }
            )
        bosses.sort(
            key=lambda b: (
                featured_rank.get(b["npc_id"], len(_FEATURED_NPC_IDS)),
                -b["player_count"],
                b["name"],
            )
        )
        payload = {"group_id": group_id, "bosses": bosses}
        if group_id is not None:
            payload["group_name"] = _group_name(group_id)
        return payload

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=120)


@personal_bests_bp.get("/personal-bests/board")
async def pb_board():
    """All team-size boards for one boss (optionally scoped to a group)."""
    try:
        npc_id = int(request.args.get("npc_id", ""))
    except (TypeError, ValueError):
        abort_problem(422, "Invalid npc_id", "'npc_id' must be an integer.")
    group_id = _parse_group_id()
    try:
        limit = int(request.args.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 50))

    def _load():
        data = _dataset(group_id)
        info = data.get(npc_id)
        if info is None:
            abort_problem(404, "No personal bests", "No ranked times for this boss.")

        # Group views cite each entry's global standing on the same board.
        global_rank: dict[tuple, int] = {}
        if group_id is not None:
            global_info = _dataset(None).get(npc_id) or {"boards": {}}
            for ts, entries in global_info["boards"].items():
                for i, e in enumerate(entries):
                    global_rank[(ts, e["player_id"])] = i + 1

        shown_ids: set = set()
        for entries in info["boards"].values():
            shown_ids.update(e["player_id"] for e in entries[:limit])
        names = _player_names(shown_ids)

        boards = []
        for ts, entries in info["boards"].items():
            out_entries = []
            for i, e in enumerate(entries[:limit]):
                row = {
                    "rank": i + 1,
                    "player_id": e["player_id"],
                    "player_name": names.get(e["player_id"], "Unknown"),
                    "time_ms": e["time_ms"],
                    "time_display": _convert_from_ms(e["time_ms"]),
                    "date_ts": e["date_ts"],
                }
                if e["image_url"]:
                    row["image_url"] = e["image_url"]
                if group_id is not None:
                    grank = global_rank.get((ts, e["player_id"]))
                    if grank is not None:
                        row["global_rank"] = grank
                out_entries.append(row)
            boards.append(
                {
                    "team_size": ts,
                    "size_label": team_size_label(ts),
                    "total_players": len(entries),
                    "entries": out_entries,
                }
            )
        payload = {
            "npc_id": npc_id,
            "name": info["name"],
            "icon_url": f"{IMG_BASE}/npcdb/{npc_id}.png",
            "entry_count": info["entry_count"],
            "player_count": info["player_count"],
            "group_id": group_id,
            "boards": boards,
        }
        if group_id is not None:
            payload["group_name"] = _group_name(group_id)
        return payload

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=120)
