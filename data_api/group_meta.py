"""Cheap contextual stats: where a group or player sits on the boards.

Everything here is a Redis read against the boards the site already
maintains, so the numbers match droptracker.io exactly rather than being a
second, subtly different calculation.

Batching is the whole point. The obvious implementations are one round trip
per group or per player, which turns a 400-member roster into 400 round trips;
``web_api.common.player_list_loot_sum`` is exactly that loop and is why it is
not used here. Every function below issues a fixed, small number of round
trips regardless of how many ids it is given.

What is deliberately NOT offered: an **all-time group rank**. Monthly group
standings are maintained as a sorted set (``gleaderboard:{partition}``) so a
rank is one ``ZREVRANK``, but there is no all-time equivalent — deriving one
means summing every group's members across all history, which is the O(all
groups) computation the website caches for its leaderboard page. A number that
expensive does not belong in a section advertised as free.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from sqlalchemy import text


def _redis():
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


def _group_board_key(partition) -> str:
    # Same key the lootboard generator writes and the site's group
    # leaderboard reads.
    return f"gleaderboard:{partition}"


def group_stats(session, group_ids: List[int], partition: int,
                with_all_time: bool = False) -> Dict[int, dict]:
    """``{group_id: stats}`` — members, monthly GP, monthly rank, optionally all-time.

    ``with_all_time`` sums each group's members' lifetime totals, which costs
    one indexed query plus one pipelined Redis fetch *per group*. Fine for a
    single group; refused for a listing, where it would be that per row.
    """
    if not group_ids:
        return {}

    out: Dict[int, dict] = {
        gid: {"members": 0, "month_gp": 0, "month_rank": None,
              "ranked_groups": None, "partition": partition}
        for gid in group_ids
    }

    # Visible member counts, in one grouped query — hidden players and players
    # with hidden owners are excluded so this matches what the API will serve.
    rows = session.execute(text("""
        SELECT a.group_id, COUNT(DISTINCT a.player_id)
        FROM user_group_association a
        JOIN players p ON p.player_id = a.player_id AND COALESCE(p.hidden, 0) = 0
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE a.group_id IN :ids AND a.player_id IS NOT NULL
          AND COALESCE(u.hidden, 0) = 0
        GROUP BY a.group_id
    """).bindparams(ids=tuple(group_ids)))
    for gid, members in rows:
        out[int(gid)]["members"] = int(members)

    conn = _redis()
    if conn is None:
        return out

    key = _group_board_key(partition)
    try:
        pipe = conn.pipeline()
        pipe.zcard(key)
        for gid in group_ids:
            pipe.zscore(key, gid)
        for gid in group_ids:
            pipe.zrevrank(key, gid)
        results = pipe.execute()
    except Exception:
        return out

    total_ranked = int(results[0] or 0)
    scores = results[1:1 + len(group_ids)]
    ranks = results[1 + len(group_ids):]
    for gid, score, rank in zip(group_ids, scores, ranks):
        entry = out[gid]
        entry["month_gp"] = int(score) if score is not None else 0
        entry["month_rank"] = int(rank) + 1 if rank is not None else None
        entry["ranked_groups"] = total_ranked or None

    if with_all_time:
        for gid in group_ids:
            out[gid]["all_time_gp"] = _group_all_time(session, gid)
    return out


def _group_all_time(session, group_id: int) -> Optional[int]:
    """Lifetime GP for one group: its members' all-time totals, summed.

    There is no maintained all-time group board, so this is derived — but from
    the same per-player keys the site's own group leaderboard sums, so the two
    agree. One indexed query plus one pipelined Redis fetch.
    """
    rows = session.execute(text("""
        SELECT DISTINCT a.player_id
        FROM user_group_association a
        JOIN players p ON p.player_id = a.player_id AND COALESCE(p.hidden, 0) = 0
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE a.group_id = :gid AND a.player_id IS NOT NULL
          AND COALESCE(u.hidden, 0) = 0
    """).bindparams(gid=group_id))
    member_ids = [int(r[0]) for r in rows]
    if not member_ids:
        return 0
    try:
        from web_api.common import player_month_totals

        # 'all' is a partition token, not a month: it resolves to the
        # player:{id}:all:total_loot keys. player_month_totals pipelines.
        return int(sum(player_month_totals(member_ids, "all").values()))
    except Exception:
        return None


def player_ranks(player_ids: Iterable[int], partition: int) -> Dict[int, dict]:
    """``{player_id: {month_rank, all_time_rank, ...}}`` in two round trips.

    Ranks come from the same sorted sets the leaderboards render, so a player's
    rank here and on their profile page cannot disagree.
    """
    ids = [int(p) for p in player_ids]
    if not ids:
        return {}
    blank = {"month_rank": None, "ranked_players": None,
             "all_time_rank": None, "ranked_players_all_time": None}
    out = {pid: dict(blank) for pid in ids}

    conn = _redis()
    if conn is None:
        return out

    month_key, all_key = f"leaderboard:{partition}", "leaderboard:all"
    try:
        pipe = conn.pipeline()
        pipe.zcard(month_key)
        pipe.zcard(all_key)
        for pid in ids:
            pipe.zrevrank(month_key, pid)
        for pid in ids:
            pipe.zrevrank(all_key, pid)
        results = pipe.execute()
    except Exception:
        return out

    ranked_month, ranked_all = int(results[0] or 0), int(results[1] or 0)
    month_ranks = results[2:2 + len(ids)]
    all_ranks = results[2 + len(ids):]
    for pid, m, a in zip(ids, month_ranks, all_ranks):
        out[pid] = {
            "month_rank": int(m) + 1 if m is not None else None,
            "ranked_players": ranked_month or None,
            "all_time_rank": int(a) + 1 if a is not None else None,
            "ranked_players_all_time": ranked_all or None,
        }
    return out
