"""Re-attribute late-processed month-boundary drops to the month they were earned.

Why this exists (incident 2026-08-01)
-------------------------------------
The `webhook:queue` backlog (~28k entries, ~107 min lag) crossed the
2026-07-31 -> 2026-08-01 00:00 UTC month rollover. Drops are stamped with
`partition` and `date_added` at PROCESSING time, so July-earned drops that the
consumer got to after midnight were written with partition=202608 and credited
to the 202608 Redis leaderboards, hourly rollups, etc. A drop's true event time
is the first hyphen-separated field of `drops.unique_id` (epoch seconds, set by
the plugin when the loot dropped).

What it corrects, per affected drop (event epoch < boundary, processed after):

  1. `drops.partition` -> the boundary's previous month, and
     `drops.date_added` -> the event time (FROM_UNIXTIME(guid epoch)).
     Moving date_added too is NOT optional: the hourly-rollup re-fold
     (services/item_totals.refold_day, run hourly by droptracker-player-updates,
     including the previous month for the first 3 days of a new month) rebuilds
     buckets from `date_added` windows and stamps the window's month as the
     partition. A partition-only fix would be reverted (and double-counted)
     within the hour. With date_added moved, every existing convergence tool
     (re-fold, rebuild_partition, force_update_player, recap composition,
     group exports, profile month queries) heals TOWARD this correction.
  2. `player_item_hourly_totals` / `player_npc_hourly_totals`: the touched
     hour buckets on both sides of the boundary are absolutely recomputed from
     `drops` with the same statement shape (and drop_id ceiling) the live
     re-fold uses, in the same transaction as the drops move.
  3. Redis, per affected player: `force_update_player` (authoritative rebuild —
     monthly boards global+group, player:{id}:{part}:* keys, split credits,
     moderation exclusions, day/week boards, all-time).
  4. Redis per-NPC monthly boards (leaderboard:npc:{id}:{part} and
     leaderboard:group:{gid}:npc:{id}:{part}) — not covered by
     force_update_player; corrected by exact ZINCRBY reversal of what intake
     credited to the wrong month (honouring drop_group_moderation exclusions).
  5. Redis `gleaderboard:{part}` (groups tab; written by the lootboard
     generator for the CURRENT month only, so the previous month froze wrong):
     re-derived as the member-sum of each group's monthly board — the same
     definition web_api/routes/leaderboards.py falls back to.
  6. Redis `pstats:lootbox:*:{part}` settled-month caches: purged.
  7. `recap_snapshots` for the previous month: RECOMPUTED IN PLACE from the
     corrected numbers. They must not be deleted — a snapshot is the published
     artifact behind the permanent `/groups/{id}/recap/{period}` page, which
     every recap DM already sent links to, and nothing rebuilds a deleted one
     once that period's delivery run has been recorded. A subject that no
     longer produces a card keeps its previous row (a stale card beats a dead
     link) and is reported. Frozen recap lootboard PNGs ARE removed — those
     self-heal, `ensure_group_lootboard` re-renders on first request.

Reporting: per-player and per-group GP deltas (the amounts moving from the
wrong month to the right one), plus current-vs-projected top standings for the
global board and every affected group's board, so the month-end loot-leader
badge outcomes can be re-verified. A full machine-readable plan/report JSON is
written to logs/.

Preconditions enforced before --apply writes anything:
  * `webhook:queue` length <= --max-queue (late backlog entries would keep
    landing in the wrong month after the fix);
  * both hourly-rollup tailer pointers (`item_totals:last_drop_id`,
    `npc_totals:last_drop_id`) are past the newest affected drop;
  * MySQL session timezone is UTC (FROM_UNIXTIME correctness).

Out of scope, on purpose: rows whose event epoch is older than --max-lag-hours
before the boundary (offline-replay resubmissions, not backlog victims — they
are listed, not moved); seasonal tables (verified zero affected rows);
`recap_deliveries` ledger rows (owner decides re-sends); the root-cause code
fix in services/redis_updates._get_partition.

Usage
-----
    # dry run (default): report, write plan JSON, change nothing
    python -m scripts.fix_month_boundary_late_drops

    # apply after the queue is drained and the dry run is approved
    python -m scripts.fix_month_boundary_late_drops --apply

    # re-run Redis/cleanup phases from a prior APPLY run's plan (e.g. after a
    # crash between the DB commit and the Redis phases)
    python -m scripts.fix_month_boundary_late_drops --apply --resume logs/<plan>.json

Idempotent: the affected set is `partition = <current month> AND date_added >=
boundary AND epoch < boundary`, snapshotted INSIDE the write transaction — once
moved, a re-run finds nothing (any stragglers that trickled in are picked up by
simply running again). The Redis player phase converges to DB truth; the
NPC-board reversal is replayed from the plan file on --resume and guarded by a
sentinel key (fix:month_boundary:{boundary}:npc_boards) so it cannot
double-apply.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import bindparam, create_engine, text  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "logs")

CHUNK = 5_000          # drop_ids per UPDATE statement
ZBATCH = 400           # redis pipeline commands per execute
RETRYABLE = ("1205", "Lock wait timeout", "Deadlock")

# Dedicated engine: the shared app engine caps read_timeout at 30s, too short
# for the hour-window aggregations here (same pattern as the reconcile_* and
# backfill_* scripts).
_maint_engine = create_engine(
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}@localhost:3306/data",
    pool_size=2,
    max_overflow=2,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "read_timeout": 1800, "write_timeout": 300,
                  "charset": "utf8mb4"},
)


def _fmt(n: int) -> str:
    n = int(n)
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(n)


def month_partition(dt: datetime) -> int:
    return dt.year * 100 + dt.month


def prev_partition_of(p: int) -> int:
    y, m = divmod(p, 100)
    return (y - 1) * 100 + 12 if m == 1 else p - 1


def hour_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d-%H")


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
AFFECTED_SQL = text(
    "SELECT drop_id, player_id, item_id, npc_id, value, quantity, hidden, date_added, "
    "       CAST(SUBSTRING_INDEX(unique_id, '-', 1) AS UNSIGNED) AS event_epoch "
    "FROM drops "
    "WHERE `partition` = :cur_part "
    "  AND date_added >= :boundary "
    "  AND unique_id REGEXP '^1[0-9]{9}-' "
    "  AND CAST(SUBSTRING_INDEX(unique_id, '-', 1) AS UNSIGNED) < :boundary_epoch"
)

NONCONFORMING_SQL = text(
    "SELECT COUNT(*) FROM drops "
    "WHERE `partition` = :cur_part AND date_added >= :boundary "
    "  AND (unique_id IS NULL OR unique_id NOT REGEXP '^1[0-9]{9}-')"
)


class Snapshot:
    """The affected set and every aggregate derived from it, in one pass."""

    def __init__(self, rows, boundary: datetime, boundary_epoch: int, max_lag_hours: int):
        min_epoch = boundary_epoch - max_lag_hours * 3600
        self.boundary = boundary
        self.rows = []          # dicts for rows to move
        self.outliers = []      # rows excluded by the lag horizon
        for (drop_id, player_id, item_id, npc_id, value, quantity,
             hidden, date_added, epoch) in rows:
            rec = {
                "drop_id": int(drop_id), "player_id": int(player_id),
                "item_id": int(item_id) if item_id is not None else None,
                "npc_id": int(npc_id) if npc_id is not None else None,
                "value": int(value or 0), "quantity": int(quantity or 0),
                "hidden": bool(hidden), "date_added": date_added,
                "epoch": int(epoch),
            }
            (self.rows if rec["epoch"] >= min_epoch else self.outliers).append(rec)

        # Aggregates over the moved set.
        self.gp_total = 0                    # all rows (partition truth)
        self.gp_visible = 0                  # hidden != 1 (leaderboard truth)
        self.player_gp = defaultdict(int)    # visible gp per player
        self.player_rows = defaultdict(int)
        self.npc_player_gp = defaultdict(int)   # (npc_id, player_id) -> visible gp
        self.aug_hours = set()
        self.jul_hours = set()
        self.max_drop_id = 0
        for r in self.rows:
            gp = r["value"] * r["quantity"]
            self.gp_total += gp
            self.player_rows[r["player_id"]] += 1
            self.max_drop_id = max(self.max_drop_id, r["drop_id"])
            self.aug_hours.add(hour_str(r["date_added"]))
            self.jul_hours.add(hour_str(datetime.utcfromtimestamp(r["epoch"])))
            if not r["hidden"]:
                self.gp_visible += gp
                self.player_gp[r["player_id"]] += gp
                if r["npc_id"]:
                    self.npc_player_gp[(r["npc_id"], r["player_id"])] += gp

    @property
    def players(self):
        return sorted({r["player_id"] for r in self.rows})


def load_snapshot(conn, boundary, boundary_epoch, cur_part, max_lag_hours) -> Snapshot:
    rows = conn.execute(
        AFFECTED_SQL,
        {"cur_part": cur_part, "boundary": boundary, "boundary_epoch": boundary_epoch},
    ).fetchall()
    return Snapshot(rows, boundary, boundary_epoch, max_lag_hours)


def load_group_context(conn, snap: Snapshot):
    """Current group membership for affected players + per-(drop,group)
    moderation exclusions for affected drops (both mirror the intake path)."""
    player_groups: dict[int, list[int]] = defaultdict(list)
    if snap.players:
        rows = conn.execute(
            text("SELECT player_id, group_id FROM user_group_association "
                 "WHERE player_id IN :pids").bindparams(bindparam("pids", expanding=True)),
            {"pids": snap.players},
        ).fetchall()
        for pid, gid in rows:
            player_groups[int(pid)].append(int(gid))

    excluded: set[tuple[int, int]] = set()  # (drop_id, group_id)
    drop_ids = [r["drop_id"] for r in snap.rows]
    try:
        for i in range(0, len(drop_ids), 10_000):
            rows = conn.execute(
                text("SELECT drop_id, group_id FROM drop_group_moderation "
                     "WHERE status IN ('excluded', 'pending', 'rejected') "
                     "  AND drop_id IN :ids").bindparams(bindparam("ids", expanding=True)),
                {"ids": drop_ids[i:i + 10_000]},
            ).fetchall()
            excluded.update((int(d), int(g)) for d, g in rows)
    except Exception as e:
        print(f"WARNING: couldn't load moderation exclusions ({e}); "
              "group deltas assume none.", file=sys.stderr)
    return player_groups, excluded


def group_deltas(snap: Snapshot, player_groups, excluded):
    """(group_id -> visible GP delta) and (group_id -> set of affected players),
    honouring per-(drop,group) moderation exclusions the way intake did."""
    g_gp = defaultdict(int)
    g_players = defaultdict(set)
    for r in snap.rows:
        if r["hidden"]:
            continue
        gp = r["value"] * r["quantity"]
        for gid in player_groups.get(r["player_id"], ()):
            if (r["drop_id"], gid) in excluded:
                continue
            g_gp[gid] += gp
            g_players[gid].add(r["player_id"])
    return g_gp, g_players


def npc_group_deltas(snap: Snapshot, player_groups, excluded):
    """(npc_id, player_id, group_id) -> visible GP for the group NPC boards."""
    out = defaultdict(int)
    for r in snap.rows:
        if r["hidden"] or not r["npc_id"]:
            continue
        gp = r["value"] * r["quantity"]
        for gid in player_groups.get(r["player_id"], ()):
            if (r["drop_id"], gid) in excluded:
                continue
            out[(r["npc_id"], r["player_id"], gid)] += gp
    return out


# --------------------------------------------------------------------------- #
# DB apply (one transaction)
# --------------------------------------------------------------------------- #
UPDATE_DROPS_SQL = text(
    "UPDATE drops "
    "SET `partition` = :prev_part, "
    "    date_added = FROM_UNIXTIME(CAST(SUBSTRING_INDEX(unique_id, '-', 1) AS UNSIGNED)), "
    "    date_updated = NOW() "
    "WHERE drop_id IN :ids AND `partition` = :cur_part"
)

# Same statement shape (and drop_id ceiling contract) as the live re-fold in
# services/item_totals.py / npc_totals.py, scoped to one hour bucket.
REFOLD_ITEM_HOUR_SQL = text(
    "INSERT INTO player_item_hourly_totals "
    "  (player_id, item_id, date_hour, `partition`, quantity, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), :part, "
    "       SUM(d.quantity), SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.date_added >= :h_start AND d.date_added < :h_end AND d.drop_id <= :max_id "
    "  AND d.item_id IS NOT NULL AND d.date_added IS NOT NULL "
    "GROUP BY d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') "
    "ON DUPLICATE KEY UPDATE quantity = VALUES(quantity), total_value = VALUES(total_value), "
    "  drop_count = VALUES(drop_count), last_drop_time = VALUES(last_drop_time)"
)

REFOLD_NPC_HOUR_SQL = text(
    "INSERT INTO player_npc_hourly_totals "
    "  (player_id, npc_id, date_hour, `partition`, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.npc_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), :part, "
    "       SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.date_added >= :h_start AND d.date_added < :h_end AND d.drop_id <= :max_id "
    "  AND d.npc_id IS NOT NULL AND d.date_added IS NOT NULL "
    "GROUP BY d.player_id, d.npc_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') "
    "ON DUPLICATE KEY UPDATE total_value = VALUES(total_value), "
    "  drop_count = VALUES(drop_count), last_drop_time = VALUES(last_drop_time)"
)

# Buckets in a recomputed hour whose (player, item|npc) no longer has ANY drop
# in that hour (the whole bucket was misattributed loot). The NOT-EXISTS side
# is deliberately uncapped so a bucket the tailer just created for a brand-new
# drop survives.
DELETE_EMPTY_ITEM_SQL = text(
    "DELETE t FROM player_item_hourly_totals t "
    "LEFT JOIN (SELECT DISTINCT player_id, item_id FROM drops "
    "           WHERE date_added >= :h_start AND date_added < :h_end "
    "             AND item_id IS NOT NULL) s "
    "  ON s.player_id = t.player_id AND s.item_id = t.item_id "
    "WHERE t.`partition` = :part AND t.date_hour = :dh AND s.player_id IS NULL"
)

DELETE_EMPTY_NPC_SQL = text(
    "DELETE t FROM player_npc_hourly_totals t "
    "LEFT JOIN (SELECT DISTINCT player_id, npc_id FROM drops "
    "           WHERE date_added >= :h_start AND date_added < :h_end "
    "             AND npc_id IS NOT NULL) s "
    "  ON s.player_id = t.player_id AND s.npc_id = t.npc_id "
    "WHERE t.`partition` = :part AND t.date_hour = :dh AND s.player_id IS NULL"
)


def _hour_window(dh: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(dh, "%Y-%m-%d-%H")
    return start, start + timedelta(hours=1)


def apply_db(conn, snap: Snapshot, prev_part: int, cur_part: int,
             item_ptr: int, npc_ptr: int) -> dict:
    """Everything MySQL, inside the caller's transaction. `snap` must have been
    loaded on this same connection/transaction."""
    stats = {"drops_moved": 0, "item_orphans": 0, "npc_orphans": 0,
             "hours": len(snap.jul_hours) + len(snap.aug_hours)}

    ids = [r["drop_id"] for r in snap.rows]
    for i in range(0, len(ids), CHUNK):
        res = conn.execute(
            UPDATE_DROPS_SQL.bindparams(bindparam("ids", expanding=True)),
            {"ids": ids[i:i + CHUNK], "prev_part": prev_part, "cur_part": cur_part},
        )
        stats["drops_moved"] += res.rowcount or 0
    if stats["drops_moved"] != len(ids):
        raise RuntimeError(
            f"drops UPDATE touched {stats['drops_moved']} rows, expected {len(ids)} "
            "— rolling back"
        )

    # Absolutely recompute every touched hour bucket, both sides. The moved
    # rows are visible to these statements (same transaction).
    for dh in sorted(snap.jul_hours):
        h_start, h_end = _hour_window(dh)
        conn.execute(REFOLD_ITEM_HOUR_SQL,
                     {"part": prev_part, "h_start": h_start, "h_end": h_end, "max_id": item_ptr})
        conn.execute(REFOLD_NPC_HOUR_SQL,
                     {"part": prev_part, "h_start": h_start, "h_end": h_end, "max_id": npc_ptr})
    for dh in sorted(snap.aug_hours):
        h_start, h_end = _hour_window(dh)
        conn.execute(REFOLD_ITEM_HOUR_SQL,
                     {"part": cur_part, "h_start": h_start, "h_end": h_end, "max_id": item_ptr})
        conn.execute(REFOLD_NPC_HOUR_SQL,
                     {"part": cur_part, "h_start": h_start, "h_end": h_end, "max_id": npc_ptr})
        r1 = conn.execute(DELETE_EMPTY_ITEM_SQL,
                          {"part": cur_part, "dh": dh, "h_start": h_start, "h_end": h_end})
        r2 = conn.execute(DELETE_EMPTY_NPC_SQL,
                          {"part": cur_part, "dh": dh, "h_start": h_start, "h_end": h_end})
        stats["item_orphans"] += r1.rowcount or 0
        stats["npc_orphans"] += r2.rowcount or 0

    # Invariant, still inside the transaction: nothing movable remains beyond
    # the deliberately-excluded outliers.
    remaining = conn.execute(AFFECTED_SQL, {
        "cur_part": cur_part, "boundary": snap.boundary,
        "boundary_epoch": int(snap.boundary.replace(tzinfo=timezone.utc).timestamp()),
    }).fetchall()
    outlier_ids = {r["drop_id"] for r in snap.outliers}
    stray = [int(r[0]) for r in remaining if int(r[0]) not in outlier_ids]
    if stray:
        raise RuntimeError(f"{len(stray)} affected rows still present after UPDATE "
                           f"(e.g. drop_id {stray[:5]}) — rolling back")
    return stats


# --------------------------------------------------------------------------- #
# Redis phases
# --------------------------------------------------------------------------- #
def redis_conn():
    from utils.redis import redis_client as rc
    return rc.client


def sentinel_key(boundary: datetime) -> str:
    return f"fix:month_boundary:{boundary.strftime('%Y%m%d')}:npc_boards"


def apply_npc_boards(conn_r, boundary: datetime, npc_pg: dict, npc_group: dict,
                     prev_part: int, cur_part: int, plan_id: str) -> int:
    """Move the misattributed GP between the monthly per-NPC boards.

    Exact reversal of the intake increments (visible drops only, group keys
    honouring moderation exclusions). Guarded by a sentinel so a re-run or
    --resume cannot double-apply.

    npc_pg:    {(npc_id, player_id): gp}
    npc_group: {(npc_id, player_id, group_id): gp}
    """
    from services.redis_updates import RedisLootTracker

    ttl = RedisLootTracker._NPC_MONTH_TTL
    if not conn_r.set(sentinel_key(boundary), plan_id, nx=True):
        holder = conn_r.get(sentinel_key(boundary))
        print(f"  npc boards: already applied by plan {holder!r} — skipping")
        return 0

    ops = 0
    pipe = conn_r.pipeline(transaction=False)
    for (npc_id, player_id), gp in npc_pg.items():
        for part, sign in ((cur_part, -1), (prev_part, +1)):
            key = f"leaderboard:npc:{npc_id}:{part}"
            pipe.zincrby(key, sign * gp, player_id)
            pipe.expire(key, ttl)
            ops += 1
            if ops % ZBATCH == 0:
                pipe.execute()
    for (npc_id, player_id, gid), gp in npc_group.items():
        for part, sign in ((cur_part, -1), (prev_part, +1)):
            key = f"leaderboard:group:{gid}:npc:{npc_id}:{part}"
            pipe.zincrby(key, sign * gp, player_id)
            pipe.expire(key, ttl)
            ops += 1
            if ops % ZBATCH == 0:
                pipe.execute()
    pipe.execute()

    # Drop zero/negative residue members left on the current-month keys.
    for npc_id in sorted({n for (n, _p) in npc_pg}):
        conn_r.zremrangebyscore(f"leaderboard:npc:{npc_id}:{cur_part}", "-inf", 0)
    for (npc_id, _pid, gid) in list(npc_group):
        conn_r.zremrangebyscore(f"leaderboard:group:{gid}:npc:{npc_id}:{cur_part}", "-inf", 0)
    return ops


def apply_force_updates(players: list[int]) -> tuple[int, list[int]]:
    """Authoritative per-player Redis rebuild (same primitive the
    player-updates service runs). Returns (ok, failed_ids)."""
    from db.models.base import get_fresh_session
    from services.redis_updates import force_update_player

    ok, failed = 0, []
    for i, pid in enumerate(players, 1):
        s = None
        try:
            s = get_fresh_session()
            if force_update_player(pid, s):
                ok += 1
            else:
                failed.append(pid)
        except Exception as e:
            failed.append(pid)
            print(f"  force_update {pid} failed: {e}", file=sys.stderr)
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        if i % 25 == 0 or i == len(players):
            print(f"  force_update: {i}/{len(players)} (ok={ok}, failed={len(failed)})",
                  flush=True)
        time.sleep(0.05)
    return ok, failed


def rebuild_gleaderboards(conn_r, parts: list[int], affected_groups: set[int]) -> int:
    """gleaderboard:{part} := member-sum of leaderboard:{part}:group:{gid} for
    every group already on the board plus every affected group — the definition
    the web API's fallback uses when the key is absent."""
    written = 0
    for part in parts:
        gids = set(affected_groups)
        for m in conn_r.zrange(f"gleaderboard:{part}", 0, -1):
            try:
                gids.add(int(m))
            except (TypeError, ValueError):
                continue
        for gid in sorted(gids):
            total = sum(
                s for _m, s in
                conn_r.zrange(f"leaderboard:{part}:group:{gid}", 0, -1, withscores=True)
            )
            if total > 0:
                conn_r.zadd(f"gleaderboard:{part}", {gid: int(total)})
            else:
                conn_r.zrem(f"gleaderboard:{part}", gid)
            written += 1
    return written


def purge_pstats(conn_r, parts: list[int]) -> int:
    removed = 0
    for part in parts:
        batch = []
        for key in conn_r.scan_iter(match=f"pstats:lootbox:*:{part}", count=500):
            batch.append(key)
            if len(batch) >= 200:
                removed += conn_r.delete(*batch)
                batch = []
        if batch:
            removed += conn_r.delete(*batch)
    return removed


# --------------------------------------------------------------------------- #
# Recap snapshot invalidation
# --------------------------------------------------------------------------- #
def _recompute_snapshots(snapshots: list, period: str) -> tuple:
    """Rebuild each recap card in place from the corrected numbers.

    Uses the same compute+save path the delivery run uses, so a card that a
    player already received keeps its URL and simply tells the truth. A subject
    that can no longer produce a card (dropped below the activity floor once
    the misattributed drops moved) keeps its old row rather than losing the
    page entirely — a slightly stale card beats a dead link.
    """
    from db.models.base import Session
    from services.recap import (
        compute_group_month,
        compute_player_month,
        save_snapshot,
    )

    done, failures = 0, []
    s = Session()
    try:
        for row in snapshots:
            try:
                payload = (
                    compute_group_month(s, row["subject_id"], period)
                    if row["scope"] == "group"
                    else compute_player_month(s, row["subject_id"], period)
                )
                if not payload:
                    failures.append({**row, "reason": "no longer produces a card"})
                    continue
                save_snapshot(s, row["scope"], row["subject_id"], period, payload)
                s.commit()
                done += 1
            except Exception as e:  # noqa: BLE001
                s.rollback()
                failures.append({**row, "reason": str(e)[:200]})
            if done and done % 100 == 0:
                print(f"  recomputed {done}/{len(snapshots)} recap card(s)")
    finally:
        s.close()
    return done, failures


def invalidate_recaps(conn, prev_part: int, apply: bool) -> dict:
    period = f"{prev_part // 100:04d}-{prev_part % 100:02d}"
    rows = conn.execute(
        text("SELECT id, scope, subject_id FROM recap_snapshots WHERE period = :p"),
        {"p": period},
    ).fetchall()
    out = {"period": period,
           "snapshots": [{"id": int(r[0]), "scope": r[1], "subject_id": int(r[2])} for r in rows],
           "lootboards_removed": [],
           "recomputed": 0,
           "recompute_failures": []}
    if apply and rows:
        # RECOMPUTE, never delete. A snapshot is the published artifact: the
        # permanent /groups/{id}/recap/{period} page renders from it, and every
        # recap DM already sent links to that page. Deleting the row to "force
        # a rebuild" assumed something would rebuild it — nothing does, because
        # the delivery for this period has already run and the ledger says so.
        # So the old behaviour silently 404'd every recap that had been sent.
        out["recomputed"], out["recompute_failures"] = _recompute_snapshots(
            out["snapshots"], period
        )
    for r in out["snapshots"]:
        if r["scope"] != "group":
            continue
        path = (f"{REPO_ROOT}/static/assets/img/clans/{r['subject_id']}"
                f"/recap/lootboard-{period}.png")
        if os.path.exists(path):
            out["lootboards_removed"].append(path)
            if apply:
                try:
                    os.remove(path)
                except OSError as e:
                    print(f"WARNING: couldn't remove {path}: {e}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------- #
# Standings (badge re-verification)
# --------------------------------------------------------------------------- #
def board_top(conn_r, key: str, n: int = 5):
    return [
        {"player_id": int(m), "score": int(s)}
        for m, s in conn_r.zrevrange(key, 0, n - 1, withscores=True)
    ]


def projected_board(conn_r, key: str, deltas: dict[int, int], n: int = 5):
    """Current board with per-player deltas applied (projection of the fix)."""
    scores = {int(m): int(s) for m, s in conn_r.zrange(key, 0, -1, withscores=True)}
    for pid, d in deltas.items():
        scores[pid] = scores.get(pid, 0) + d
    top = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [{"player_id": p, "score": s} for p, s in top]


def standings_report(conn_r, snap: Snapshot, g_gp, g_players, player_groups,
                     excluded, prev_part: int) -> dict:
    """Current vs projected standings for the previous month's global board and
    every affected group's board. `projected` == what the boards should show
    after the fix; comparing the two answers "does any loot-leader change?"."""
    out = {"global": {
        "key": f"leaderboard:{prev_part}",
        "current": board_top(conn_r, f"leaderboard:{prev_part}"),
        "projected": projected_board(conn_r, f"leaderboard:{prev_part}", dict(snap.player_gp)),
    }, "groups": {}}
    out["global"]["leader_changes"] = (
        bool(out["global"]["current"]) and bool(out["global"]["projected"])
        and out["global"]["current"][0]["player_id"] != out["global"]["projected"][0]["player_id"]
    )
    for gid in sorted(g_gp):
        deltas: dict[int, int] = defaultdict(int)
        for r in snap.rows:
            if r["hidden"] or gid not in player_groups.get(r["player_id"], ()):
                continue
            if (r["drop_id"], gid) in excluded:
                continue
            deltas[r["player_id"]] += r["value"] * r["quantity"]
        key = f"leaderboard:{prev_part}:group:{gid}"
        cur = board_top(conn_r, key)
        proj = projected_board(conn_r, key, deltas)
        out["groups"][gid] = {
            "key": key, "gp_delta": g_gp[gid], "players": len(g_players[gid]),
            "current": cur, "projected": proj,
            "leader_changes": bool(cur) and bool(proj)
                              and cur[0]["player_id"] != proj[0]["player_id"],
        }
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def print_deltas(snap: Snapshot, g_gp, g_players, prev_part: int):
    top_players = sorted(snap.player_gp.items(), key=lambda kv: -kv[1])[:25]
    print(f"\nTop player GP deltas (visible gp moving to {prev_part}):")
    for pid, gp in top_players:
        print(f"  player {pid}: +{_fmt(gp)} ({snap.player_rows[pid]} drops)")
    print("\nTop group GP deltas:")
    for gid, gp in sorted(g_gp.items(), key=lambda kv: -kv[1])[:25]:
        print(f"  group {gid}: +{_fmt(gp)} across {len(g_players[gid])} player(s)")


def print_flips(standings: dict, prev_part: int):
    flips = [("GLOBAL", standings["global"])] if standings["global"]["leader_changes"] else []
    flips += [(f"group {gid}", g) for gid, g in standings["groups"].items()
              if g["leader_changes"]]
    if flips:
        print(f"\n*** LOOT-LEADER #1 CHANGES on {prev_part} boards after correction: ***")
        for label, board in flips:
            print(f"  {label}: {board['current'][0]} -> {board['projected'][0]}")
    else:
        print(f"\nNo #1 changes on the global or any affected group's {prev_part} board.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--boundary", default="2026-08-01T00:00:00",
                    help="UTC month boundary the backlog crossed (default: %(default)s)")
    ap.add_argument("--max-lag-hours", type=int, default=48,
                    help="event epochs older than this before the boundary are "
                         "offline replays, not backlog victims: excluded + listed "
                         "(default: %(default)s)")
    ap.add_argument("--max-queue", type=int, default=50,
                    help="refuse --apply while LLEN webhook:queue exceeds this "
                         "(default: %(default)s)")
    ap.add_argument("--resume", metavar="PLAN.json",
                    help="re-run unfinished phases of a prior APPLY run from its "
                         "plan file (DB phase is verified, not repeated)")
    ap.add_argument("--skip-force-update", action="store_true",
                    help="skip the per-player Redis rebuild phase (run it later "
                         "with --resume)")
    args = ap.parse_args()

    boundary = datetime.fromisoformat(args.boundary)
    if boundary.tzinfo is not None:
        boundary = boundary.astimezone(timezone.utc).replace(tzinfo=None)
    boundary_epoch = int(boundary.replace(tzinfo=timezone.utc).timestamp())
    cur_part = month_partition(boundary)
    prev_part = prev_partition_of(cur_part)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] month-boundary re-attribution: partition {cur_part} -> {prev_part} "
          f"for drops earned before {boundary} UTC (epoch < {boundary_epoch})")

    conn_r = redis_conn()
    if conn_r is None:
        print("ERROR: no Redis connection", file=sys.stderr)
        return 1

    # ---- Preconditions -------------------------------------------------- #
    with _maint_engine.connect() as c:
        tz = c.execute(text("SELECT @@time_zone, @@system_time_zone")).one()
    if not (tz[0] == "+00:00" or (tz[0] == "SYSTEM" and tz[1] == "UTC")):
        print(f"ERROR: MySQL session timezone is {tz} — FROM_UNIXTIME would mis-stamp "
              "date_added. Aborting.", file=sys.stderr)
        return 1

    qlen = int(conn_r.llen("webhook:queue") or 0)
    item_ptr = int(conn_r.get("item_totals:last_drop_id") or 0)
    npc_ptr = int(conn_r.get("npc_totals:last_drop_id") or 0)
    print(f"webhook:queue={qlen}  item_tailer@{item_ptr}  npc_tailer@{npc_ptr}")

    now_part = month_partition(datetime.utcnow())
    if now_part != cur_part:
        print(f"NOTE: running in month {now_part}, not {cur_part} — the hourly "
              "re-fold's previous-month grace window has likely passed; this "
              "script still corrects the rollups directly.")

    if args.apply and qlen > args.max_queue:
        print(f"ERROR: webhook:queue={qlen} > --max-queue={args.max_queue}. Late "
              "entries would keep landing in the wrong month. Wait for the drain.",
              file=sys.stderr)
        return 1

    resumed = None
    if args.resume:
        with open(args.resume) as f:
            resumed = json.load(f)
        if not args.apply:
            print("--resume only makes sense with --apply", file=sys.stderr)
            return 1
        print(f"Resuming plan {resumed.get('plan_id')} "
              f"(phases done: {[k for k, v in resumed.get('phases', {}).items() if v]})")

    # ---- Snapshot -------------------------------------------------------- #
    # Dry run: plain connection. Apply: snapshot inside the write transaction
    # so the moved set is transactionally exact.
    phases = (resumed or {}).get("phases",
                                 {"db": False, "force_update": False, "npc_boards": False,
                                  "gleaderboard": False, "pstats": False, "recaps": False})
    plan_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    plan_path = args.resume or os.path.join(
        LOG_DIR, f"month_boundary_fix_{cur_part}_{plan_id}.json")
    os.makedirs(LOG_DIR, exist_ok=True)

    db_stats = None
    if not args.apply or phases["db"]:
        with _maint_engine.connect() as c:
            snap = load_snapshot(c, boundary, boundary_epoch, cur_part, args.max_lag_hours)
            nonconforming = c.execute(
                NONCONFORMING_SQL, {"cur_part": cur_part, "boundary": boundary}
            ).scalar() or 0
            player_groups, excluded = load_group_context(c, snap)
    else:
        # APPLY, DB phase pending: snapshot + context + all MySQL writes in ONE
        # transaction, with lock-timeout retries around the whole unit.
        for attempt in range(1, 4):
            try:
                with _maint_engine.begin() as c:
                    snap = load_snapshot(c, boundary, boundary_epoch, cur_part,
                                         args.max_lag_hours)
                    nonconforming = c.execute(
                        NONCONFORMING_SQL, {"cur_part": cur_part, "boundary": boundary}
                    ).scalar() or 0
                    player_groups, excluded = load_group_context(c, snap)
                    if snap.rows and (item_ptr < snap.max_drop_id
                                      or npc_ptr < snap.max_drop_id):
                        raise SystemExit(
                            f"ERROR: rollup tailer(s) behind the newest affected drop "
                            f"({snap.max_drop_id}; item@{item_ptr}, npc@{npc_ptr}). "
                            "Wait a minute and retry.")
                    print(f"[db] moving {len(snap.rows):,} drops + recomputing "
                          f"{len(snap.jul_hours) + len(snap.aug_hours)} hour "
                          "buckets/table...")
                    if snap.rows:
                        db_stats = apply_db(c, snap, prev_part, cur_part,
                                            item_ptr, npc_ptr)
                    else:
                        print("  nothing to move (affected set is empty)")
                break
            except SystemExit:
                raise
            except Exception as e:
                if attempt < 3 and any(t in str(e) for t in RETRYABLE):
                    print(f"  lock contention ({e}); retry {attempt} in 15s...")
                    time.sleep(15)
                    continue
                raise
        phases["db"] = True
        if db_stats:
            print(f"  moved {db_stats['drops_moved']:,} drops; orphan buckets deleted: "
                  f"item={db_stats['item_orphans']}, npc={db_stats['npc_orphans']}")

    # ---- Aggregates + report --------------------------------------------- #
    g_gp, g_players = group_deltas(snap, player_groups, excluded)
    npc_group = npc_group_deltas(snap, player_groups, excluded)
    npc_pg = dict(snap.npc_player_gp)

    # On resume after the DB phase, the fresh snapshot is empty by design —
    # fall back to the recorded plan for the Redis phases.
    players = snap.players
    affected_groups = set(g_gp)
    if resumed and not snap.rows:
        players = [int(p) for p in resumed.get("player_gp_delta", {})]
        affected_groups = {int(g) for g in resumed.get("group_gp_delta", {})}
        npc_pg = {tuple(int(x) for x in k.split(":")): v
                  for k, v in resumed.get("npc_player_gp", {}).items()}
        npc_group = {tuple(int(x) for x in k.split(":")): v
                     for k, v in resumed.get("npc_group_gp", {}).items()}
        g_gp = {int(g): v for g, v in resumed.get("group_gp_delta", {}).items()}
        print(f"(resume) {len(players)} players / {len(affected_groups)} groups / "
              f"{len(npc_pg)} npc deltas loaded from plan")

    print(f"\nAffected: {len(snap.rows):,} drops | {_fmt(snap.gp_total)} gp "
          f"({_fmt(snap.gp_visible)} visible) | {len(snap.players):,} players | "
          f"{len({r['npc_id'] for r in snap.rows if r['npc_id']}):,} NPCs | "
          f"{len(affected_groups):,} groups")
    if snap.rows:
        print(f"Hour buckets: {sorted(snap.jul_hours)} -> {prev_part} side; "
              f"{sorted(snap.aug_hours)} on the {cur_part} side")
        print(f"date_added span of affected rows: "
              f"{min(r['date_added'] for r in snap.rows)} .. "
              f"{max(r['date_added'] for r in snap.rows)} | newest drop_id "
              f"{snap.max_drop_id}")
    if snap.outliers:
        print(f"EXCLUDED {len(snap.outliers)} outlier row(s) older than the "
              f"{args.max_lag_hours}h lag horizon (offline replays?) — listed in the "
              "plan JSON; not moved.")
    if nonconforming:
        print(f"NOTE: {nonconforming} partition-{cur_part} row(s) since the boundary "
              "have no parseable guid epoch; untouched (attribution unknowable).")
    if not args.apply and snap.rows and (item_ptr < snap.max_drop_id
                                         or npc_ptr < snap.max_drop_id):
        print(f"WARNING: rollup tailer(s) behind newest affected drop "
              f"{snap.max_drop_id} (item@{item_ptr}, npc@{npc_ptr}) — --apply "
              "would refuse right now.")

    if snap.rows:
        print_deltas(snap, g_gp, g_players, prev_part)
    standings = standings_report(conn_r, snap, g_gp, g_players, player_groups,
                                 excluded, prev_part)
    if snap.rows:
        print_flips(standings, prev_part)

    with _maint_engine.connect() as c:
        recaps_preview = invalidate_recaps(c, prev_part, apply=False)
    if recaps_preview["snapshots"]:
        print(f"\nrecap_snapshots frozen for {recaps_preview['period']}: "
              f"{len(recaps_preview['snapshots'])} card(s) "
              f"({'will recompute' if args.apply else 'would recompute'} in place; "
              "published URLs are preserved)")

    plan = {
        "plan_id": (resumed or {}).get("plan_id", plan_id), "mode": mode,
        "boundary": str(boundary), "cur_part": cur_part, "prev_part": prev_part,
        "queue_len": qlen, "item_ptr": item_ptr, "npc_ptr": npc_ptr,
        "counts": {"rows": len(snap.rows), "gp": snap.gp_total,
                   "gp_visible": snap.gp_visible, "players": len(players),
                   "groups": len(affected_groups), "outliers": len(snap.outliers),
                   "nonconforming": int(nonconforming)},
        "db_stats": db_stats or (resumed or {}).get("db_stats"),
        "moved_drop_ids": [r["drop_id"] for r in snap.rows]
                          or (resumed or {}).get("moved_drop_ids", []),
        "original_date_added": {str(r["drop_id"]): r["date_added"].isoformat()
                                for r in snap.rows}
                               or (resumed or {}).get("original_date_added", {}),
        "outliers": [{k: (v.isoformat() if isinstance(v, datetime) else v)
                      for k, v in r.items()} for r in snap.outliers],
        "player_gp_delta": {str(k): v for k, v in snap.player_gp.items()}
                           or (resumed or {}).get("player_gp_delta", {}),
        "group_gp_delta": {str(k): v for k, v in g_gp.items()},
        "npc_player_gp": {f"{n}:{p}": v for (n, p), v in npc_pg.items()},
        "npc_group_gp": {f"{n}:{p}:{g}": v for (n, p, g), v in npc_group.items()},
        "standings_before": (resumed or {}).get("standings_before", standings),
        "recap_invalidations": recaps_preview,
        "phases": phases,
    }

    def save_plan():
        with open(plan_path, "w") as f:
            json.dump(plan, f, indent=2, default=str)

    if not args.apply:
        save_plan()
        print(f"\nDRY RUN — nothing written. Plan/report: {plan_path}")
        print("Re-run with --apply once webhook:queue is drained and the owner "
              "has approved the deltas above.")
        return 0

    save_plan()  # phases.db recorded before any Redis work

    # ---- Redis + cleanup phases ------------------------------------------ #
    if not phases["force_update"] and not args.skip_force_update:
        print(f"\n[redis] force_update_player for {len(players)} affected players...")
        ok, failed = apply_force_updates(players)
        print(f"  done: ok={ok}, failed={len(failed)}")
        if failed:
            plan["force_update_failed"] = failed
            print(f"  FAILED player ids (re-run via --resume): {failed[:20]}"
                  f"{'...' if len(failed) > 20 else ''}")
        phases["force_update"] = not failed
        save_plan()

    if not phases["npc_boards"]:
        print(f"\n[redis] npc monthly boards: {len(npc_pg)} global + "
              f"{len(npc_group)} group deltas...")
        ops = apply_npc_boards(conn_r, boundary, npc_pg, npc_group,
                               prev_part, cur_part, plan["plan_id"])
        print(f"  {ops} zincrby ops")
        phases["npc_boards"] = True
        save_plan()

    if not phases["gleaderboard"]:
        n = rebuild_gleaderboards(conn_r, [prev_part, cur_part], affected_groups)
        print(f"\n[redis] gleaderboard:{prev_part}/{cur_part}: {n} group totals rewritten")
        phases["gleaderboard"] = True
        save_plan()

    if not phases["pstats"]:
        n = purge_pstats(conn_r, [prev_part, cur_part])
        print(f"[redis] purged {n} pstats:lootbox cache keys")
        phases["pstats"] = True
        save_plan()

    if not phases["recaps"]:
        with _maint_engine.begin() as c:
            recaps = invalidate_recaps(c, prev_part, apply=True)
        print(f"[recaps] recomputed {recaps['recomputed']}/{len(recaps['snapshots'])} "
              f"snapshot(s) for {recaps['period']}; removed "
              f"{len(recaps['lootboards_removed'])} frozen lootboard PNG(s) "
              "(regenerated on demand)")
        if recaps["recompute_failures"]:
            print(f"[recaps] WARNING: {len(recaps['recompute_failures'])} card(s) "
                  "kept their previous numbers — see recap_invalidations in the plan")
        plan["recap_invalidations"] = recaps
        phases["recaps"] = True
        save_plan()

    # ---- Post-verification ------------------------------------------------ #
    print("\n[verify]")
    with _maint_engine.connect() as c:
        bh = hour_str(boundary)
        checks = {
            f"drops: partition={prev_part} with date_added >= boundary": c.execute(
                text("SELECT COUNT(*) FROM drops WHERE `partition`=:p AND date_added>=:b"),
                {"p": prev_part, "b": boundary}).scalar(),
            f"drops: partition={cur_part} with date_added < boundary": c.execute(
                text("SELECT COUNT(*) FROM drops WHERE `partition`=:p AND date_added<:b"),
                {"p": cur_part, "b": boundary}).scalar(),
            f"item rollup: partition={prev_part} rows at/after {bh}": c.execute(
                text("SELECT COUNT(*) FROM player_item_hourly_totals "
                     "WHERE `partition`=:p AND date_hour>=:h"),
                {"p": prev_part, "h": bh}).scalar(),
            f"npc rollup: partition={prev_part} rows at/after {bh}": c.execute(
                text("SELECT COUNT(*) FROM player_npc_hourly_totals "
                     "WHERE `partition`=:p AND date_hour>=:h"),
                {"p": prev_part, "h": bh}).scalar(),
        }
    all_ok = True
    for label, n in checks.items():
        ok = (n or 0) == 0
        all_ok &= ok
        print(f"  {'OK ' if ok else 'FAIL'} {label}: {n}")

    plan["standings_after"] = standings_report(
        conn_r, snap, g_gp, g_players, player_groups, excluded, prev_part)
    plan["verify"] = {k: int(v or 0) for k, v in checks.items()}
    save_plan()

    print(f"\n{'DONE' if all_ok else 'DONE WITH FAILURES — see above'}. "
          f"Plan/report: {plan_path}")
    print("""
Follow-ups this script deliberately leaves to the owner:
  * restart recap delivery once satisfied:  sudo systemctl start droptracker-recaps.timer
  * group 292's July card was already posted with pre-fix numbers
    (recap_deliveries.id=12). Deleting that ledger row makes the next timer
    firing repost a corrected card; leaving it keeps the original message.
  * badge player_badges.id=1903 (global_loot_leader_monthly 202607): winner
    verified unchanged, but its context.loot was computed pre-fix. Optional:
    update the stored context to the corrected total.
  * root cause: services/redis_updates.py _get_partition() ignores the drop
    timestamp — fix + restart droptracker-webhook-consumer, or this recurs at
    every backlogged month boundary.""")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
