"""EHB harvesting for recap cards.

A recap says how much loot a subject gained in a month and how that compares to
the month before. This module answers the same two questions for **EHB**
(efficient hours bossed), which unlike loot is not ours to compute — it lives at
Wise Old Man, and WOM has asked us to keep our call volume down.

So the harvest is built around one fact: ``GET /groups/:id/bulk-gained`` returns
*every* metric for *every* member of a clan in a single request. One call per
WOM-linked clan therefore answers for its whole roster, which is why this module
goes out of its way to reach players through their groups and treats the
per-player endpoint as a last resort behind a hard cap.

Three properties make the monthly cost close to nothing:

* **A closed month's gains are immutable.** They are harvested once and stored
  in ``recap_wom_gains`` forever, so the delivery sweep — which fires every 15
  minutes for three days — pays for the first tick only.
* **This month's harvest is next month's baseline.** The "vs last month" figure
  on an August card was written by the August 1st run for July. Only the very
  first run after deploy fetches a second window.
* **Attempts are remembered, not just results.** A clan whose fetch legitimately
  matched nobody would otherwise be re-fetched on every tick of the window.

Nothing here is allowed to fail a recap. Every path degrades to "no EHB for this
subject", the card omits the stat, and the next run tries again. Absence of a
row means *unknown* and is deliberately distinct from a stored ``0.0``, which
means the player was measured and bossed nothing.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import bindparam, text

logger = logging.getLogger("services.recap_ehb")

# The metric key inside a bulk-gained row's flat `data` list.
EHB_METRIC = "ehb"

SOURCE_BULK = "bulk"
SOURCE_PLAYER = "player"

# Neither cap is a budget we expect to spend. They exist so that a month with an
# unusual audience cannot turn into an unbounded run against a shared, rate
# limited API — and both log what they dropped, because a silent cap reads as
# "we covered everyone" when we didn't.
DEFAULT_GROUP_CAP = int(os.getenv("RECAP_EHB_GROUP_CAP", "400"))
DEFAULT_PLAYER_CAP = int(os.getenv("RECAP_EHB_PLAYER_CAP", "150"))
# Wall clock for one harvest. The delivery sweep re-runs every 15 minutes for
# three days, so stopping early costs coverage on this tick and nothing overall,
# whereas overrunning delays the messages the run exists to send.
DEFAULT_TIME_BUDGET = float(os.getenv("RECAP_EHB_TIME_BUDGET", "600"))
# The lazy web path generates a card inside an HTTP request, so it gets a much
# tighter one — a slow clan fetch must not become a slow page.
LAZY_TIME_BUDGET = float(os.getenv("RECAP_EHB_LAZY_BUDGET", "10"))

# "We already asked WOM about this subject for this period." Kept in Redis
# rather than the table because it records an *attempt*, and the useful lifetime
# of that fact is the delivery window, not forever. Losing it costs re-fetches,
# never correctness.
_ATTEMPT_PREFIX = "recap:ehbharvest:"
_ATTEMPT_TTL = int(os.getenv("RECAP_EHB_ATTEMPT_TTL", str(30 * 24 * 3600)))

# Group 1 is the config template and group 2 holds every tracked player; neither
# is a clan, and a bulk call for the latter would be absurd.
_NON_CLAN_GROUPS = (1, 2)


def _new_stats() -> dict:
    return {
        "bulk_calls": 0,
        "player_calls": 0,
        "rows_written": 0,
        "groups_done": 0,
        "groups_skipped": 0,
        "groups_dropped": 0,
        "players_dropped": 0,
        "fetch_failures": 0,
        "out_of_budget": False,
    }


# --------------------------------------------------------------------------- #
# Pure helpers (the testable core)
# --------------------------------------------------------------------------- #
def _extract_ehb(row) -> Optional[float]:
    """The EHB gained out of one ``bulk-gained`` row, or ``None``.

    A row's ``data`` is a flat list of ``{"metric": ..., "gained": ...}`` across
    all 112 metrics; only one of them is ours. Negative gains are clamped to
    zero: EHB only ever accumulates in play, so a negative is a hiscore rollback
    or a reused name, and "gained −2.3 hours" is not a thing a card can say.
    """
    if not isinstance(row, dict):
        return None
    for entry in row.get("data") or ():
        if isinstance(entry, dict) and entry.get("metric") == EHB_METRIC:
            try:
                return max(0.0, float(entry.get("gained")))
            except (TypeError, ValueError):
                return None
    return None


def _norm_name(name) -> str:
    from utils.format import normalize_player_display_equivalence

    return normalize_player_display_equivalence(name)


def match_rows(rows, by_wom: dict, by_name: dict) -> dict:
    """``{player_id: ehb_gained}`` for the rows we can attribute.

    WOM id first, normalised display name second — the same order (and the same
    reason) as ``event_wom_reconciler._match_participant``: ids are stable
    across name changes, but plenty of our rows predate ever learning one.

    Rows that match nobody are dropped rather than guessed at. A WOM roster
    routinely holds accounts DropTracker doesn't track, and a clan total that
    counted them would not match the membership every other number on the card
    is scoped to.
    """
    out: dict[int, float] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        player = row.get("player")
        if not isinstance(player, dict):
            continue
        pid = None
        wom_id = player.get("id")
        if wom_id is not None:
            try:
                pid = by_wom.get(int(wom_id))
            except (TypeError, ValueError):
                pid = None
        if pid is None:
            pid = by_name.get(_norm_name(player.get("displayName")))
        if pid is None:
            continue
        ehb = _extract_ehb(row)
        if ehb is None:
            continue
        # A player in two harvested clans arrives twice with the same figure;
        # last write wins and they agree, so no reconciliation is needed.
        out[int(pid)] = ehb
    return out


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _redis():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None


def _attempted(conn, kind: str, subject_id: int, period: str) -> bool:
    if conn is None:
        return False
    try:
        return bool(conn.get(f"{_ATTEMPT_PREFIX}{kind}:{subject_id}:{period}"))
    except Exception:
        return False


def _mark_attempted(conn, kind: str, subject_id: int, period: str) -> None:
    if conn is None:
        return
    try:
        conn.setex(f"{_ATTEMPT_PREFIX}{kind}:{subject_id}:{period}", _ATTEMPT_TTL, "1")
    except Exception:
        pass


def stored_gains(session, player_ids: Iterable[int], period: str) -> dict:
    """``{player_id: ehb_gained}`` already harvested for the period."""
    ids = [int(p) for p in player_ids]
    if not ids:
        return {}
    rows = session.execute(
        text(
            "SELECT player_id, ehb_gained FROM recap_wom_gains "
            "WHERE period = :period AND player_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"period": period, "ids": ids},
    ).fetchall()
    return {int(r[0]): float(r[1] or 0.0) for r in rows}


def _already_bulk_harvested(session, player_ids, period: str) -> bool:
    """Whether a clan's roster already carries rows from a group fetch.

    The durable half of the skip guard. The Redis marker answers faster but
    expires, and a month's gains never do — without this, a marker ageing out
    between the delivery window and the next month's baseline pass would buy a
    second copy of data we already hold.
    """
    ids = sorted(int(p) for p in player_ids)
    if not ids:
        return False
    row = session.execute(
        text(
            "SELECT 1 FROM recap_wom_gains "
            "WHERE period = :period AND source = :source AND player_id IN :ids "
            "LIMIT 1"
        ).bindparams(bindparam("ids", expanding=True)),
        {"period": period, "source": SOURCE_BULK, "ids": ids},
    ).first()
    return row is not None


def _write_gains(session, gains: dict, period: str, source: str) -> int:
    if not gains:
        return 0
    session.execute(
        text(
            "INSERT INTO recap_wom_gains "
            "  (player_id, period, ehb_gained, source, fetched_at) "
            "VALUES (:player_id, :period, :ehb, :source, NOW()) "
            "ON DUPLICATE KEY UPDATE "
            "  ehb_gained = VALUES(ehb_gained), source = VALUES(source), "
            "  fetched_at = NOW()"
        ),
        [
            {"player_id": int(pid), "period": period, "ehb": float(ehb),
             "source": source}
            for pid, ehb in gains.items()
        ],
    )
    session.commit()
    return len(gains)


# --------------------------------------------------------------------------- #
# Subject selection
# --------------------------------------------------------------------------- #
def _month_window(period: str):
    """The ``(start, end)`` datetimes to ask WOM about for a month period."""
    from services.recap import month_bounds

    start, end = month_bounds(period)
    fmt = "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(start, fmt), datetime.strptime(end, fmt)


def _candidate_groups(session, explicit: set, player_ids: set) -> list:
    """``[(group_id, wom_id, coverage)]``, best value first.

    Coverage is how many of the players we actually need this call would answer
    for. Explicit groups (a clan whose own card is being built) sort first
    regardless — their card needs the number whether or not anyone in the DM
    audience happens to be a member.
    """
    coverage: dict[int, int] = {}
    if player_ids:
        rows = session.execute(
            text(
                "SELECT group_id, COUNT(*) FROM user_group_association "
                "WHERE player_id IN :pids AND group_id NOT IN :skip "
                "GROUP BY group_id"
            ).bindparams(
                bindparam("pids", expanding=True), bindparam("skip", expanding=True)
            ),
            {"pids": sorted(player_ids), "skip": list(_NON_CLAN_GROUPS)},
        ).fetchall()
        coverage = {int(r[0]): int(r[1]) for r in rows}

    wanted = set(coverage) | {g for g in explicit if g not in _NON_CLAN_GROUPS}
    if not wanted:
        return []

    rows = session.execute(
        text(
            "SELECT group_id, wom_id FROM groups "
            "WHERE group_id IN :gids AND wom_id IS NOT NULL AND wom_id > 0"
        ).bindparams(bindparam("gids", expanding=True)),
        {"gids": sorted(wanted)},
    ).fetchall()

    out = []
    for group_id, wom_id in rows:
        group_id = int(group_id)
        # A clan whose own card is being built outranks any amount of incidental
        # DM coverage; +1 keeps a zero-coverage explicit group ahead of nothing.
        rank = coverage.get(group_id, 0) + (1_000_000 if group_id in explicit else 0)
        out.append((group_id, int(wom_id), rank))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def _roster_maps(session, group_id: int) -> tuple:
    """``({wom_id: pid}, {normalised name: pid})`` for one group's members.

    Hidden players are included: the visibility rule belongs to the card, which
    sums over its own visible roster, not to the cache behind it.
    """
    rows = session.execute(
        text(
            "SELECT p.player_id, p.wom_id, p.player_name "
            "FROM user_group_association uga "
            "JOIN players p ON p.player_id = uga.player_id "
            "WHERE uga.group_id = :gid"
        ),
        {"gid": int(group_id)},
    ).fetchall()
    by_wom: dict[int, int] = {}
    by_name: dict[str, int] = {}
    for player_id, wom_id, player_name in rows:
        if wom_id:
            by_wom[int(wom_id)] = int(player_id)
        norm = _norm_name(player_name)
        if norm:
            by_name[norm] = int(player_id)
    return by_wom, by_name


# --------------------------------------------------------------------------- #
# The harvest
# --------------------------------------------------------------------------- #
async def harvest_month_ehb(
    session,
    period: str,
    *,
    group_ids: Iterable[int] = (),
    player_ids: Iterable[int] = (),
    fetch_prev: bool = True,
    group_cap: Optional[int] = None,
    player_cap: Optional[int] = None,
    time_budget: Optional[float] = None,
    log=None,
) -> dict:
    """Fill ``recap_wom_gains`` for ``period`` (and the month before it).

    ``group_ids`` are subjects whose own card needs a clan total; ``player_ids``
    are subjects whose card needs their personal figure — those are reached
    through whichever clans they belong to before any per-player call is
    considered.

    Returns a stats dict for the run's log. Never raises: a harvest that fails
    leaves the cards without an EHB stat, which is a card, not an outage.
    """
    stats = _new_stats()
    emit = log or (lambda message: logger.info("%s", message))
    deadline = time.monotonic() + (
        DEFAULT_TIME_BUDGET if time_budget is None else time_budget
    )
    group_cap = DEFAULT_GROUP_CAP if group_cap is None else group_cap
    player_cap = DEFAULT_PLAYER_CAP if player_cap is None else player_cap

    try:
        from services.recap import is_month_period, previous_month_period

        if not is_month_period(period):
            return stats
        periods = [period]
        if fetch_prev:
            periods.append(previous_month_period(period))

        explicit = {int(g) for g in group_ids}
        wanted = {int(p) for p in player_ids}
        conn = _redis()
        candidates = _candidate_groups(session, explicit, wanted)

        for target_period in periods:
            if time.monotonic() >= deadline:
                stats["out_of_budget"] = True
                break
            await _harvest_period(
                session, target_period, candidates, wanted, conn, stats,
                deadline=deadline, group_cap=group_cap, player_cap=player_cap,
            )
    except Exception as e:
        logger.warning("recap EHB harvest failed for %s: %s", period, e,
                       exc_info=True)
        try:
            session.rollback()
        except Exception:
            pass

    if stats["groups_dropped"] or stats["players_dropped"] or stats["out_of_budget"]:
        emit(
            f"  EHB harvest incomplete: {stats['groups_dropped']} group(s) and "
            f"{stats['players_dropped']} player(s) not fetched"
            + (" (time budget)" if stats["out_of_budget"] else " (cap)")
            + " — cards for them omit EHB until the next run"
        )
    emit(
        f"  EHB harvest: {stats['rows_written']} row(s) from "
        f"{stats['bulk_calls']} group + {stats['player_calls']} player call(s), "
        f"{stats['groups_skipped']} group(s) already done"
    )
    return stats


async def _harvest_period(
    session, period: str, candidates: list, wanted: set, conn, stats: dict,
    *, deadline: float, group_cap: int, player_cap: int,
) -> None:
    """One month window: clans first, then whoever they didn't cover."""
    from utils.wiseoldman import get_group_bulk_gained

    start_dt, end_dt = _month_window(period)
    calls = 0

    for group_id, wom_id, _rank in candidates:
        if _attempted(conn, "g", group_id, period):
            stats["groups_skipped"] += 1
            continue

        by_wom, by_name = _roster_maps(session, group_id)
        if not by_wom and not by_name:
            _mark_attempted(conn, "g", group_id, period)
            continue
        roster = set(by_wom.values()) | set(by_name.values())
        if _already_bulk_harvested(session, roster, period):
            stats["groups_skipped"] += 1
            _mark_attempted(conn, "g", group_id, period)
            continue

        # Checked here rather than at the top of the loop so the caps bound
        # *fetches*, not skips: a run that finds everything already harvested
        # should never report that it dropped anyone.
        if calls >= group_cap:
            stats["groups_dropped"] += 1
            continue
        if time.monotonic() >= deadline:
            stats["out_of_budget"] = True
            stats["groups_dropped"] += 1
            continue

        calls += 1
        stats["bulk_calls"] += 1
        rows = await get_group_bulk_gained(wom_id, start_dt, end_dt)
        if rows is None:
            # Rate-limit backlog or a WOM fault. Deliberately NOT marked as
            # attempted, so the next tick retries it.
            stats["fetch_failures"] += 1
            continue

        stats["rows_written"] += _write_gains(
            session, match_rows(rows, by_wom, by_name), period, SOURCE_BULK
        )
        stats["groups_done"] += 1
        _mark_attempted(conn, "g", group_id, period)

    if wanted:
        await _harvest_stragglers(
            session, period, wanted, conn, stats,
            start_dt=start_dt, end_dt=end_dt,
            deadline=deadline, player_cap=player_cap,
        )


async def _harvest_stragglers(
    session, period: str, wanted: set, conn, stats: dict,
    *, start_dt, end_dt, deadline: float, player_cap: int,
) -> None:
    """Per-player calls for subjects no clan fetch reached.

    One call buys one player here, against a shared budget the whole box draws
    on, so this is capped and gives up entirely the moment the limiter starts
    refusing — a backlog is a signal to stop asking, not to queue.
    """
    from utils.wiseoldman import get_player_gained_ehb

    missing = sorted(wanted - set(stored_gains(session, wanted, period)))
    missing = [p for p in missing if not _attempted(conn, "p", p, period)]
    if not missing:
        return

    if len(missing) > player_cap:
        stats["players_dropped"] += len(missing) - player_cap
        missing = missing[:player_cap]

    rows = session.execute(
        text(
            "SELECT player_id, player_name FROM players WHERE player_id IN :pids"
        ).bindparams(bindparam("pids", expanding=True)),
        {"pids": missing},
    ).fetchall()

    for player_id, player_name in rows:
        if not player_name:
            continue
        if time.monotonic() >= deadline:
            stats["out_of_budget"] = True
            stats["players_dropped"] += 1
            continue
        stats["player_calls"] += 1
        ehb = await get_player_gained_ehb(player_name, start_dt, end_dt)
        if ehb is None:
            # Could be this player (no snapshot, renamed) or the limiter turning
            # everyone away. Cheaper to stop than to find out: the next run picks
            # up where this one left off.
            stats["fetch_failures"] += 1
            _mark_attempted(conn, "p", int(player_id), period)
            continue
        stats["rows_written"] += _write_gains(
            session, {int(player_id): ehb}, period, SOURCE_PLAYER
        )
        _mark_attempted(conn, "p", int(player_id), period)


async def ensure_player_ehb(player_id: int, period: str) -> dict:
    """Harvest one player's month on demand, for the lazily-generated card.

    Runs inside an HTTP request, so it is bounded hard by
    :data:`LAZY_TIME_BUDGET` and reaches the player through their clan when it
    can — a group call costs the same as a personal one and answers for
    everyone else in the clan at the same time.
    """
    from db import Session

    session = Session()
    try:
        return await harvest_month_ehb(
            session,
            period,
            player_ids=[int(player_id)],
            group_cap=2,
            player_cap=1,
            time_budget=LAZY_TIME_BUDGET,
            log=lambda message: logger.debug("%s", message),
        )
    finally:
        try:
            session.close()
        except Exception:
            pass
