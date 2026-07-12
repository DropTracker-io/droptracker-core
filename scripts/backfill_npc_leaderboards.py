"""Backfill the per-NPC loot leaderboards from the ``drops`` table.

Why this exists
---------------
The Hall of Fame "Most Loot / Total loot tracked" section and the website's
per-NPC leaderboards read these sorted sets:

    leaderboard:npc:{npc_id}[:{partition}]                (global)
    leaderboard:group:{gid}:npc:{npc_id}[:{partition}]    (per group)

Until now **nothing wrote them** — the reader was live but the writer never
existed, so the section silently rendered nothing. ``services/redis_updates.py``
now maintains them incrementally (ZINCRBY) on every drop, but that only covers
drops processed *after* the writer shipped. This script rebuilds the historical
baseline from the authoritative source: the ``drops`` table.

Scope
-----
Only the NPCs that a Hall-of-Fame-enabled group actually tracks
(``personal_best_embed_boss_list``) are backfilled. That is exactly the set the
HOF renders, it keeps the aggregation on the indexed ``npc_id`` column (instead
of scanning the whole ~160M-row table), and it bounds memory. Every other NPC's
website board simply fills in going forward from the live ZINCRBY path.

Definition of a player's per-NPC score (matches the intake path):

    SUM(value * quantity) over every non-hidden drop from that NPC.

Per-group boards are the same per-player sums restricted to each group's
*current* membership (the rule the intake path applies). Seasonal boards are
out of scope (separate tables/namespace; the HOF reads main-world only).

Usage
-----
    # dry run (default) — reports what would change, writes nothing
    python scripts/backfill_npc_leaderboards.py

    # actually write to Redis
    python scripts/backfill_npc_leaderboards.py --commit

Idempotent: scores are written with absolute ZADD (converges to DB truth), so
re-running is safe. The only race is a drop landing between aggregation and the
ZADD, whose increment is overwritten — bounded by run time and reconciled by
any re-run.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from services.redis_updates import RedisLootTracker  # noqa: E402
from utils.hof import parse_boss_list  # noqa: E402
from utils.redis import redis_client  # noqa: E402

BATCH = 500
NPC_MONTH_TTL = RedisLootTracker._NPC_MONTH_TTL

_DB_USER = os.getenv("DB_USER")
_DB_PASS = os.getenv("DB_PASS")
# Dedicated engine with a long read timeout for the aggregation queries (the
# shared app engine caps read_timeout at 30s).
_maint_engine = create_engine(
    f"mysql+pymysql://{_DB_USER}:{_DB_PASS}@localhost:3306/data",
    pool_size=1,
    max_overflow=1,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "read_timeout": 1800, "write_timeout": 60,
                  "charset": "utf8mb4", "autocommit": True},
)
MaintSession = sessionmaker(bind=_maint_engine)


def _fmt(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(n)


def _int_or_none(raw):
    try:
        return int(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


def _hof_groups(session) -> list[int]:
    """Groups with the Hall of Fame enabled (create_pb_embeds=1)."""
    rows = session.execute(
        text("SELECT group_id FROM group_configurations "
             "WHERE config_key='create_pb_embeds' AND config_value='1'")
    ).fetchall()
    return [int(r[0]) for r in rows]


def _target_npc_ids(session, group_ids: list[int]) -> set[int]:
    """Every npc_id any HOF group tracks, resolved via its configured boss list."""
    if not group_ids:
        return set()
    rows = session.execute(
        text("SELECT config_value, long_value FROM group_configurations "
             "WHERE config_key='personal_best_embed_boss_list' "
             "  AND group_id IN :gids").bindparams(
                 bindparam("gids", expanding=True)),
        {"gids": group_ids},
    ).fetchall()
    names: set[str] = set()
    for config_value, long_value in rows:
        for name in parse_boss_list(config_value, long_value):
            names.add(name)
    if not names:
        return set()
    npc_rows = session.execute(
        text("SELECT npc_id FROM npc_list WHERE npc_name IN :names").bindparams(
            bindparam("names", expanding=True)),
        {"names": list(names)},
    ).fetchall()
    return {int(r[0]) for r in npc_rows}


def _group_membership(session, group_ids: list[int]) -> dict[int, set[int]]:
    """group_id -> {player_id, ...} for the given groups (current membership)."""
    if not group_ids:
        return {}
    rows = session.execute(
        text("SELECT group_id, player_id FROM user_group_association "
             "WHERE player_id IS NOT NULL AND group_id IN :gids").bindparams(
                 bindparam("gids", expanding=True)),
        {"gids": group_ids},
    ).fetchall()
    members: dict[int, set[int]] = defaultdict(set)
    for gid, pid in rows:
        members[int(gid)].add(int(pid))
    return members


def _aggregate(session, npc_ids: set[int]):
    """Return (all_time, monthly):

        all_time[npc_id][player_id] = SUM(value*quantity)
        monthly[(npc_id, partition)][player_id] = SUM(value*quantity)
    """
    all_time: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    monthly: dict[tuple[int, int], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    rows = session.execute(
        text(
            "SELECT npc_id, player_id, YEAR(date_added)*100 + MONTH(date_added) AS part, "
            "       SUM(value * quantity) "
            "FROM drops "
            "WHERE npc_id IN :npcs AND hidden != 1 "
            "GROUP BY npc_id, player_id, part"
        ).bindparams(bindparam("npcs", expanding=True)),
        {"npcs": list(npc_ids)},
    ).fetchall()
    for npc_id, pid, part, total in rows:
        if pid is None or total is None or int(total) <= 0:
            continue
        npc_id, pid, part, total = int(npc_id), int(pid), int(part), int(total)
        all_time[npc_id][pid] += total
        monthly[(npc_id, part)][pid] += total
    return all_time, monthly


def _write_board(conn, key: str, totals: dict[int, int], ttl: int | None,
                 commit: bool) -> tuple[int, int]:
    """Converge one sorted set to ``totals``; returns (written, removed)."""
    totals = {pid: t for pid, t in totals.items() if t > 0}
    existing = {
        pid: int(s)
        for m, s in conn.zrange(key, 0, -1, withscores=True)
        if (pid := _int_or_none(m)) is not None
    }
    stale = [pid for pid in existing if pid not in totals]
    changed = {pid: t for pid, t in totals.items() if existing.get(pid) != t}
    if commit and (changed or stale):
        items = list(changed.items())
        for i in range(0, len(items), BATCH):
            chunk = dict(items[i:i + BATCH])
            if chunk:
                conn.zadd(key, {str(p): v for p, v in chunk.items()})
        if stale:
            conn.zrem(key, *[str(p) for p in stale])
        if ttl:
            conn.expire(key, ttl)
    return len(changed), len(stale)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="write to Redis (default: dry run)")
    args = ap.parse_args()

    conn = redis_client.client
    session = MaintSession()
    try:
        groups = _hof_groups(session)
        npc_ids = _target_npc_ids(session, groups)
        print(f"HOF groups: {len(groups)} | tracked NPCs: {len(npc_ids)}")
        if not npc_ids:
            print("Nothing to backfill (no HOF-tracked NPCs).")
            return
        membership = _group_membership(session, groups)
        print("Aggregating drops (indexed on npc_id)...", flush=True)
        all_time, monthly = _aggregate(session, npc_ids)

        keys = wrote = removed = 0

        # Global boards.
        for npc_id, totals in all_time.items():
            w, r = _write_board(conn, f"leaderboard:npc:{npc_id}", totals, None, args.commit)
            keys += 1; wrote += w; removed += r
        for (npc_id, part), totals in monthly.items():
            w, r = _write_board(conn, f"leaderboard:npc:{npc_id}:{part}", totals,
                                NPC_MONTH_TTL, args.commit)
            keys += 1; wrote += w; removed += r

        # Per-group boards: same sums restricted to each group's membership.
        for gid in groups:
            members = membership.get(gid, set())
            if not members:
                continue
            for npc_id, totals in all_time.items():
                sub = {p: t for p, t in totals.items() if p in members}
                if sub:
                    w, r = _write_board(conn, f"leaderboard:group:{gid}:npc:{npc_id}",
                                        sub, None, args.commit)
                    keys += 1; wrote += w; removed += r
            for (npc_id, part), totals in monthly.items():
                sub = {p: t for p, t in totals.items() if p in members}
                if sub:
                    w, r = _write_board(conn, f"leaderboard:group:{gid}:npc:{npc_id}:{part}",
                                        sub, NPC_MONTH_TTL, args.commit)
                    keys += 1; wrote += w; removed += r

        mode = "WROTE" if args.commit else "DRY RUN — would write"
        print(f"{mode}: {keys} board(s), {_fmt(wrote)} entries set, {removed} stale removed")
        if not args.commit:
            print("Re-run with --commit to apply.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
