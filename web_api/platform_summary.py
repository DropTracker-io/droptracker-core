"""Platform-wide headline figures for the public homepage.

Three things about the current tracking month: how much loot has been tracked,
across how many accounts, and which bosses have paid out the most.

The homepage used to take all three from ``GET /groups/2`` — the global group,
which every tracked account belongs to. That endpoint is built for a clan page:
it loads every member id, reads each member's month total, and joins the boss
rollup against the membership table. For a clan of 200 that is nothing. For
27,856 members (of whom about 4,800 have loot in a given month) it measured
2.6-4.4s warm and 20s cold, and because its cache lives in the process, every
restart of the web API was a cold start for the next visitor.

Nothing here needs that work:

* **Month total** is one ``ZSCORE``. The global group's lootboard is redrawn
  every generator cycle (about two minutes) and publishes the figure it drew to
  ``gleaderboard:{partition}`` — the same number the Discord voice-channel
  counter reads (``services/group_loot_totals.py``). It is read live on every
  request and never cached here, so it is as fresh as the board. The homepage
  adds its own realtime deltas on top, so a seed a couple of minutes old is
  invisible.
* **Account count** and **top bosses** are the only parts that touch MariaDB,
  and neither changes quickly. They are computed together (about a second: the
  boss rollup no longer joins 27k memberships, since "members of the global
  group" is simply everybody) and kept in **Redis**, not in the process, so a
  restart or a second worker starts warm.

The snapshot is served stale-while-revalidate. A request never waits for a
rebuild unless there is nothing at all to serve — the first request of a new
month, or after a Redis flush — and even then only one request builds while the
rest get the total with the slow fields empty, which the homepage already
renders as "being tallied".
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

from sqlalchemy import text

GLOBAL_GROUP_ID = 2
TOP_BOSSES = 5

SNAPSHOT_KEY = "platform:summary:{partition}"
LOCK_KEY = "platform:summary:{partition}:building"

# Older than this and the snapshot is rebuilt — behind the response that
# noticed, never in front of it.
FRESH_FOR = 600
# How long a snapshot nobody has refreshed is still worth serving. Long on
# purpose: last week's boss order beats an empty panel, and the first visitor
# after a quiet spell triggers the rebuild anyway.
KEEP_FOR = 7 * 86400
# One builder at a time, across every worker. Also the retry backoff: a failed
# build leaves the lock to expire by itself, so a query that keeps failing is
# retried every two minutes rather than on every request.
LOCK_TTL = 120

# Aggregate first, name afterwards: joining npc_list inside the GROUP BY would
# drag a lookup through every one of the month's ~400k-1M rollup rows to label
# the five that survive.
_TOP_BOSSES_SQL = text(
    "SET STATEMENT max_statement_time=20 FOR "
    "SELECT a.npc_id, n.npc_name, a.loot, a.drops "
    "FROM ( "
    "  SELECT t.npc_id, SUM(t.total_value) AS loot, SUM(t.drop_count) AS drops "
    "  FROM player_npc_hourly_totals t "
    "  WHERE t.`partition` = :partition "
    "  GROUP BY t.npc_id ORDER BY loot DESC LIMIT :lim "
    ") a JOIN npc_list n ON n.npc_id = a.npc_id "
    "ORDER BY a.loot DESC"
)

_MEMBER_COUNT_SQL = text(
    "SELECT COUNT(*) FROM user_group_association WHERE group_id = :gid"
)


# "Use the app's connection". Distinct from None, which means there is no Redis
# to talk to — a state the callers below have to be able to be handed.
_DEFAULT = object()


def _rc():
    from web_api.common import _rc as common_rc

    return common_rc()


def _as_int(raw) -> Optional[int]:
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        return int(float(raw))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Month total (Redis only, read live)                                         #
# --------------------------------------------------------------------------- #
def _leaderboard_sum(conn, partition) -> Optional[int]:
    """Sum of the global player board: one round trip, a few thousand members."""
    try:
        rows = conn.zrange(f"leaderboard:{partition}", 0, -1, withscores=True)
    except Exception:
        return None
    if not rows:
        return None
    return int(sum(float(score) for _member, score in rows))


def month_total(conn, partition) -> Optional[int]:
    """GP tracked platform-wide this month, or None when nobody can say.

    The published board total comes first so this agrees with the global
    lootboard and the Discord counter. It is missing only until the first board
    of a new month is drawn; for those couple of minutes the global player board
    is summed instead. That is the same population — every account is a member
    of the global group, which hides nobody and excludes nothing — so unlike a
    clan's total this is a second route to the same number, not a second
    opinion about it.
    """
    if conn is None:
        return None
    from services.group_loot_totals import board_month_total

    total = board_month_total(GLOBAL_GROUP_ID, partition, redis_conn=conn)
    if total is not None:
        return total
    return _leaderboard_sum(conn, partition)


# --------------------------------------------------------------------------- #
# Snapshot (MariaDB -> Redis)                                                 #
# --------------------------------------------------------------------------- #
def build_snapshot(partition) -> dict:
    """The slow half, straight from MariaDB. About a second mid-month."""
    from web_api.common import db_session

    with db_session() as s:
        members = s.execute(_MEMBER_COUNT_SQL, {"gid": GLOBAL_GROUP_ID}).scalar()
        rows = s.execute(
            _TOP_BOSSES_SQL, {"partition": int(partition), "lim": TOP_BOSSES}
        ).fetchall()
    return {
        "at": int(time.time()),
        "member_count": int(members or 0),
        "top_bosses": [
            {
                "npc_id": int(npc_id),
                "name": name,
                "loot": int(loot or 0),
                "drops": int(drops or 0),
            }
            for (npc_id, name, loot, drops) in rows
        ],
    }


def _read(conn, partition) -> Optional[dict]:
    if conn is None:
        return None
    try:
        raw = conn.get(SNAPSHOT_KEY.format(partition=partition))
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        snapshot = json.loads(raw)
    except Exception:
        return None
    # A value this module did not write (or wrote in an older shape) is treated
    # as absent, so it gets rebuilt instead of half-rendered.
    if not isinstance(snapshot, dict) or _as_int(snapshot.get("at")) is None:
        return None
    if not isinstance(snapshot.get("top_bosses"), list):
        return None
    return snapshot


def refresh(partition, *, conn=_DEFAULT, build: Callable[[int], dict] = build_snapshot) -> Optional[dict]:
    """Rebuild the snapshot if nobody else is. Returns it, or None if it was
    not this caller's turn or the build failed.

    The lock is released on success only. After a failure it is left to expire,
    which is what spaces retries ``LOCK_TTL`` apart.

    Without Redis there is nothing to build: the result could not be kept, so
    every request would pay for the query.
    """
    conn = _rc() if conn is _DEFAULT else conn
    if conn is None:
        return None
    lock = LOCK_KEY.format(partition=partition)
    try:
        if not conn.set(lock, str(int(time.time())), nx=True, ex=LOCK_TTL):
            return None
    except Exception:
        return None
    try:
        snapshot = build(partition)
        conn.set(SNAPSHOT_KEY.format(partition=partition), json.dumps(snapshot), ex=KEEP_FOR)
    except Exception as e:
        print(f"platform_summary: rebuild for {partition} failed: {type(e).__name__}: {e}")
        return None
    try:
        conn.delete(lock)
    except Exception:
        pass
    return snapshot


# --------------------------------------------------------------------------- #
# Read path                                                                   #
# --------------------------------------------------------------------------- #
def load(*, conn=_DEFAULT, now: Optional[float] = None, partition: Optional[int] = None,
         build: Callable[[int], dict] = build_snapshot) -> tuple[dict, bool]:
    """``(payload, stale)``. When ``stale`` the caller should schedule
    :func:`refresh` behind the response; the payload is already complete."""
    from web_api.common import get_current_partition, money

    ts = int(now if now is not None else time.time())
    partition = int(partition if partition is not None else get_current_partition())
    conn = _rc() if conn is _DEFAULT else conn

    snapshot = _read(conn, partition)
    stale = False
    if snapshot is None:
        # Nothing to serve: build in front of this one response. Single-flight,
        # so a burst of cold requests costs one query, not one each.
        snapshot = refresh(partition, conn=conn, build=build)
    elif ts - int(snapshot["at"]) > FRESH_FOR:
        stale = True

    total = month_total(conn, partition)
    payload = {
        "partition": partition,
        "generated_at": ts,
        "monthly_loot": money(total) if total is not None else None,
        "member_count": None,
        "top_bosses": [],
        "snapshot_at": None,
    }
    if snapshot is not None:
        payload["member_count"] = _as_int(snapshot.get("member_count"))
        payload["snapshot_at"] = int(snapshot["at"])
        payload["top_bosses"] = [
            {
                "npc_id": int(b["npc_id"]),
                "name": b.get("name") or "",
                "loot": money(b.get("loot")),
                "drops": int(b.get("drops") or 0),
            }
            for b in snapshot["top_bosses"]
            if isinstance(b, dict) and _as_int(b.get("npc_id")) is not None
        ]
    return payload, stale
