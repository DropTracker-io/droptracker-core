"""Monthly / annual recap ("Wrapped") computation.

Builds the card payload for one subject over one period and persists it to
``recap_snapshots``. Rendering, Discord delivery and the web pages all read the
stored snapshot rather than recomputing — the snapshot *is* the artifact.

Design notes that are load-bearing, all measured on production:

**Never filter by ``partition =``.** Both ``drops`` and the two hourly rollups
have an index on ``player_id`` and a separate one on ``partition``, and no
composite of the two. A ``partition = :p`` equality therefore degrades to a
``ref`` on ``player_id`` alone — the player's entire lifetime — or worse, an
index merge against the whole month across every player. Measured on ``drops``:
4.9 s for ``partition = … AND player_id = …`` versus 56 ms for the equivalent
``date_added`` range. On the rollups the estimate drops from ~48,000 rows to
~520 for the same month. Every query here ranges on the date column, and the
``drops`` reads pin ``ix_drops_player_id_date_added`` explicitly, following
``web_api/routes/profiles.py:_month_npc_boxes`` which learned this the hard way
(the 2026-07-15 read-timeout incident).

**A year is not a query.** One player-year against ``drops`` measures 6.1 s, so
``compute_year`` folds the twelve stored monthly snapshots and never touches
``drops`` at all. That is the whole reason monthly snapshots are persisted
rather than cached.

**Redis owns the headline number.** ``player:{id}:{YYYYMM}:total_loot`` and the
monthly leaderboards are what the player already sees on the site, and they
persist with no TTL back to 202410. The rollups are used for *composition* (top
items, top bosses, activity histograms) but not for the headline total, so a
recap can never disagree with the leaderboard it links to. Both numbers are kept
in the payload so any drift is visible rather than silent.

**Hidden players.** The rollups do not filter hidden drops — ``services/badges``
had to solve the same problem with ``_visible_player_ids``. Group aggregates
here exclude ``hidden_player_ids()``.

``date_hour`` is ``VARCHAR(13)`` ``'YYYY-MM-DD-HH'``, zero-padded, so a
lexicographic ``BETWEEN`` is chronological — the same property
``lootboard/timeframe.py`` relies on.
"""
from __future__ import annotations

import json
from calendar import monthrange
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import bindparam, text

from db.models.recap import (
    RECAP_SCHEMA_VERSION,
    SCOPE_GROUP,
    SCOPE_PLAYER,
    RecapSnapshot,
)

# How many entries each "top N" card carries. Generous enough that the design
# pass can choose how many to show without a recompute.
TOP_N = 10

# Subjects below this many drops in the period get no recap at all. An empty
# card is worse than no card — PlayStation's Wrap-Up gates on 10+ hours played
# for the same reason.
MIN_DROPS_FOR_RECAP = 25

# Every roster read here expands to ``player_id IN (...)``. Jagex caps a clan at
# 500 members, and the largest real group tracked is 505, so this ceiling only
# ever catches the synthetic groups (group 2 holds every tracked player, ~15.8k)
# — where the expanded IN list read-timeouts rather than returning. Skipping
# loudly beats melting a connection for a "group" nobody wants a recap for.
MAX_ROSTER_FOR_RECAP = 1500


class RosterTooLarge(RuntimeError):
    """Raised instead of issuing a query that would read-timeout. Callers skip
    the subject; they should not retry."""


# --------------------------------------------------------------------------- #
# Period handling
#
# 'YYYY-MM' for a month, 'YYYY' for a year. Both sort chronologically as
# strings, and the length tells them apart, so no separate kind column exists.
# --------------------------------------------------------------------------- #
def month_period(partition: int) -> str:
    """``202607`` -> ``'2026-07'``."""
    p = int(partition)
    return f"{p // 100:04d}-{p % 100:02d}"


def period_partition(period: str) -> int:
    """``'2026-07'`` -> ``202607``. Raises for a year period."""
    if not is_month_period(period):
        raise ValueError(f"not a month period: {period!r}")
    year, month = period.split("-")
    return int(year) * 100 + int(month)


def is_month_period(period: str) -> bool:
    return len(period) == 7 and period[4] == "-"


def is_year_period(period: str) -> bool:
    return len(period) == 4 and period.isdigit()


def year_months(year: int) -> list[str]:
    """The twelve month periods of a calendar year, in order."""
    return [f"{int(year):04d}-{m:02d}" for m in range(1, 13)]


def month_bounds(period: str) -> tuple[str, str]:
    """Half-open ``[start, end)`` datetime strings for a ``drops.date_added``
    range. Half-open rather than BETWEEN so a drop at 23:59:59.999 on the last
    day of the month lands in exactly one period."""
    partition = period_partition(period)
    year, month = partition // 100, partition % 100
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return (
        f"{year:04d}-{month:02d}-01 00:00:00",
        f"{end_year:04d}-{end_month:02d}-01 00:00:00",
    )


def month_hour_bounds(period: str) -> tuple[str, str]:
    """Inclusive ``date_hour`` bounds for the rollups. Inclusive (not half-open)
    because ``date_hour`` is an hour bucket, not an instant — the last bucket of
    the month is ``…-23`` and must be included."""
    partition = period_partition(period)
    year, month = partition // 100, partition % 100
    last_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01-00", f"{year:04d}-{month:02d}-{last_day:02d}-23"


def previous_month_period(period: str) -> str:
    partition = period_partition(period)
    year, month = partition // 100, partition % 100
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def period_closed(period: str, now: Optional[datetime] = None) -> bool:
    """Whether the period has finished. Mirrors ``services/badges._period_closed``:
    a pure token comparison, so it needs no clock arithmetic. Publishing a
    partial month is the one mistake a recap cannot walk back."""
    now = now or datetime.now()
    if is_year_period(period):
        return int(period) < now.year
    return period < month_period(now.year * 100 + now.month)


# --------------------------------------------------------------------------- #
# Redis reads (headline totals + ranks)
# --------------------------------------------------------------------------- #
def _redis_totals(player_ids: list[int], partition: int) -> dict[int, int]:
    """``{player_id: gp}`` for the month, batched.

    ``web_api.common`` is imported lazily and only ever as a leaf helper — it
    pulls Quart, and this module runs inside the bot. Same pattern as
    ``services/event_board.py``'s ``web_api.event_prizes`` import.
    """
    if not player_ids:
        return {}
    try:
        from web_api.common import player_month_totals

        return player_month_totals(player_ids, partition)
    except Exception:
        return {}


def _player_rank(player_id: int, partition: int) -> tuple[Optional[int], Optional[int]]:
    """``(rank, ranked_players)`` on the global monthly board, 1-based."""
    try:
        from web_api.common import _rc, leaderboard_key, player_global_rank

        conn = _rc()
        total = int(conn.zcard(leaderboard_key(partition))) if conn else None
        return player_global_rank(player_id, partition), total
    except Exception:
        return None, None


def _group_rank(
    group_id: int, partition: int
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """``(rank, ranked_groups, board_loot)`` from ``gleaderboard:{partition}``.

    Two caveats, both of which the payload surfaces rather than hides.

    *Coverage.* ``(None, None, None)`` before 202511 — that board is only
    written for the *current* partition, so no historical rows exist. It is also
    missing any group the lootboard hasn't rendered, so ~11 of 161 eligible
    groups legitimately have no rank. The loot total is unaffected: it is summed
    from member keys, which do persist back to 202410.

    *Scope.* The board is written by ``lootboard/generator.py``, which resolves
    a group's roster from **WOM** (``fetch_group_members``), whereas this
    module and the website's ``/groups/{id}`` page both use
    ``user_group_association``. Those populations diverge — historical members
    keep their association row after leaving the clan — so the board score and
    our summed total disagree, measured at ~13% for one 269-member clan. The
    rank is still the right rank (every group is measured the same way), but it
    is not derived from the same number we display. ``board_loot`` is returned
    so the card can show its provenance instead of implying one produced the
    other.
    """
    try:
        from web_api.common import _rc, group_totals_key

        conn = _rc()
        if conn is None:
            return None, None, None
        key = group_totals_key(partition)
        rank = conn.zrevrank(key, group_id)
        if rank is None:
            return None, None, None
        score = conn.zscore(key, group_id)
        return (
            int(rank) + 1,
            int(conn.zcard(key)),
            int(score) if score is not None else None,
        )
    except Exception:
        return None, None, None


# --------------------------------------------------------------------------- #
# Rollup reads — composition (top items, top bosses, activity)
# --------------------------------------------------------------------------- #
def _item_rollup(session, player_ids: list[int], period: str) -> dict:
    """Totals, top items and the activity histograms, in two ranged reads.

    The hour-of-day and day-of-week histograms come out of the same index range
    as the totals, so they are effectively free: ``date_hour`` carries both
    (``RIGHT(...,2)`` is the hour, the date prefix gives the weekday).

    The item read groups by ``(item_id, player_id)`` rather than ``item_id``
    alone so each item can name **who received it** — the rollup already carries
    ``player_id``, so attribution costs one extra grouping column on a read that
    was happening anyway (measured 0.3s for a 269-member clan month). Folding to
    per-item totals then happens in Python, which also yields the receiver count
    for free.
    """
    if not player_ids:
        return {
            "loot": 0, "drops": 0, "unique_items": 0,
            "top_items": [], "by_hour": [0] * 24, "by_weekday": [0] * 7,
        }
    lo, hi = month_hour_bounds(period)
    params = {"pids": player_ids, "lo": lo, "hi": hi}

    rows = session.execute(
        text(
            "SELECT r.item_id, i.item_name, r.player_id, "
            "       SUM(r.quantity) AS qty, SUM(r.total_value) AS loot, "
            "       SUM(r.drop_count) AS drops "
            "FROM player_item_hourly_totals r "
            "LEFT JOIN items i ON i.item_id = r.item_id "
            "WHERE r.player_id IN :pids AND r.date_hour BETWEEN :lo AND :hi "
            "GROUP BY r.item_id, i.item_name, r.player_id"
        ).bindparams(bindparam("pids", expanding=True)),
        params,
    ).fetchall()

    hist = session.execute(
        text(
            # DAYOFWEEK is 1=Sunday..7=Saturday; normalised to 0=Monday below so
            # the payload matches how the card will read left-to-right.
            "SELECT RIGHT(date_hour, 2) AS hh, "
            "       DAYOFWEEK(STR_TO_DATE(LEFT(date_hour, 10), '%Y-%m-%d')) AS dw, "
            "       SUM(total_value) AS loot "
            "FROM player_item_hourly_totals "
            "WHERE player_id IN :pids AND date_hour BETWEEN :lo AND :hi "
            "GROUP BY hh, dw"
        ).bindparams(bindparam("pids", expanding=True)),
        params,
    ).fetchall()

    by_hour = [0] * 24
    by_weekday = [0] * 7
    for hh, dw, loot in hist:
        loot = int(loot or 0)
        try:
            by_hour[int(hh)] += loot
            by_weekday[(int(dw) + 5) % 7] += loot  # 1=Sun -> index 6, 2=Mon -> 0
        except (TypeError, ValueError, IndexError):
            continue

    # Fold the per-(item, player) rows into per-item totals, tracking who
    # received the most of each by value.
    merged: dict[int, dict] = {}
    for item_id, name, player_id, qty, loot, drops in rows:
        if item_id is None:
            continue
        entry = merged.setdefault(
            int(item_id),
            {
                "item_id": int(item_id),
                "name": name or f"Item {item_id}",
                "quantity": 0,
                "loot": 0,
                "drops": 0,
                "receivers": 0,
                "_top_pid": None,
                "_top_loot": -1,
            },
        )
        loot = int(loot or 0)
        entry["quantity"] += int(qty or 0)
        entry["loot"] += loot
        entry["drops"] += int(drops or 0)
        entry["receivers"] += 1
        if player_id is not None and loot > entry["_top_loot"]:
            entry["_top_loot"] = loot
            entry["_top_pid"] = int(player_id)

    items = sorted(merged.values(), key=lambda x: x["loot"], reverse=True)
    top = items[:TOP_N]

    # Names only for what's actually displayed — resolving every receiver of
    # every item in the month would be a much larger lookup for no benefit.
    names = _player_names(session, [i["_top_pid"] for i in top if i["_top_pid"]])
    for entry in items:
        pid = entry.pop("_top_pid")
        entry.pop("_top_loot")
        if pid and pid in names:
            entry["receiver"] = {"player_id": pid, "name": names[pid]}

    return {
        "loot": sum(i["loot"] for i in items),
        "drops": sum(i["drops"] for i in items),
        "unique_items": len(items),
        "top_items": top,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
    }


def _npc_coverage(session, period: str) -> bool:
    """Whether ``player_npc_hourly_totals`` has been built for this month.

    It has not been, for most of history: the tailer only landed 2026-07-08 and
    the backfill covered 202410-202508 before stopping, leaving **202509 through
    202606 with no rows at all**. A recap for one of those months would
    otherwise report "0 bosses" as though the player killed nothing, which is
    the precise failure the whole feature has to avoid — a number nobody can
    trust poisons every other number on the card.

    So coverage is probed once per period and the NPC cards are omitted rather
    than zeroed. Running ``python -m scripts.backfill_npc_hourly_totals
    --from 202509`` closes the gap and the cards light up with no code change.
    """
    row = session.execute(
        text(
            "SELECT 1 FROM player_npc_hourly_totals "
            "WHERE `partition` = :p LIMIT 1"
        ),
        {"p": period_partition(period)},
    ).first()
    return row is not None


def _npc_rollup(session, player_ids: list[int], period: str) -> dict:
    if not player_ids:
        return {"unique_npcs": 0, "top_npcs": [], "available": True}
    if not _npc_coverage(session, period):
        return {"unique_npcs": None, "top_npcs": [], "available": False}
    lo, hi = month_hour_bounds(period)
    rows = session.execute(
        text(
            "SELECT r.npc_id, n.npc_name, SUM(r.total_value) AS loot, "
            "       SUM(r.drop_count) AS drops "
            "FROM player_npc_hourly_totals r "
            "LEFT JOIN npc_list n ON n.npc_id = r.npc_id "
            "WHERE r.player_id IN :pids AND r.date_hour BETWEEN :lo AND :hi "
            "GROUP BY r.npc_id, n.npc_name"
        ).bindparams(bindparam("pids", expanding=True)),
        {"pids": player_ids, "lo": lo, "hi": hi},
    ).fetchall()
    npcs = [
        {
            "npc_id": int(npc_id),
            "name": name or f"NPC {npc_id}",
            "loot": int(loot or 0),
            "drops": int(drops or 0),
        }
        for npc_id, name, loot, drops in rows
        if npc_id is not None
    ]
    npcs.sort(key=lambda x: x["loot"], reverse=True)
    return {"unique_npcs": len(npcs), "top_npcs": npcs[:TOP_N], "available": True}


# --------------------------------------------------------------------------- #
# `drops` reads — the one source that carries a single row's identity
# --------------------------------------------------------------------------- #
# Where submission screenshots live on disk, and where nginx serves them from.
# Both halves are copied from ``utils/download.py``, which owns the convention.
_UPLOAD_DIR_MARKER = "/static/assets/img/"
_UPLOAD_URL_BASE = "https://www.droptracker.io/img/"


def _public_image_url(raw: Optional[str]) -> Optional[str]:
    """A screenshot URL a browser can actually fetch, or None.

    ``drops.image_url`` is not consistently a URL: some rows carry the on-disk
    path the file was written to (``/store/droptracker/disc/static/assets/img/
    user-upload/...``) instead of the address it's served at. Rendered as-is
    that's an ``<img>`` pointing at a filesystem path, which draws a broken-image
    box on the poster — strictly worse than the blank the layout already handles,
    because a card that shows its own plumbing failing discredits the numbers
    beside it.

    Absolute http(s) values pass through. A local path is mapped onto the public
    prefix. Anything else is dropped, on the same omit-never-zero principle as
    the rest of the card: no proof beats broken proof.
    """
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    marker = url.find(_UPLOAD_DIR_MARKER)
    if marker != -1:
        return _UPLOAD_URL_BASE + url[marker + len(_UPLOAD_DIR_MARKER):].lstrip("/")
    # Already site-relative ("/img/..."), which the site and the capture page
    # both resolve same-origin — but the snapshot is an archive that may be
    # rendered elsewhere, so store it absolute.
    if url.startswith("/img/"):
        return _UPLOAD_URL_BASE + url[len("/img/"):]
    return None


def _biggest_drop(session, player_ids: list[int], period: str) -> Optional[dict]:
    """The period's single largest drop, with the evidence attached.

    Only ``drops`` can answer this: no rollup carries a single-row maximum, and
    ``image_url``/``drop_id``/``kill_count`` exist nowhere else. Capturing
    ``image_url`` into the snapshot matters — ``droptracker-prune-images.timer``
    deletes screenshots that are over 30 days old and worth under 1M GP, so a
    recap that resolved the image lazily would go blank over time.

    ``kill_count`` is populated from web76a onward and is NULL for everything
    before it, and for sources that don't report one.
    """
    if not player_ids:
        return None
    start, end = month_bounds(period)
    row = session.execute(
        text(
            "SELECT d.drop_id, d.player_id, p.player_name, d.item_id, i.item_name, "
            "       d.npc_id, n.npc_name, d.value, d.quantity, "
            "       d.value * d.quantity AS total_value, d.date_added, "
            "       d.image_url, d.kill_count "
            "FROM drops d FORCE INDEX (ix_drops_player_id_date_added) "
            "LEFT JOIN items i ON i.item_id = d.item_id "
            "LEFT JOIN npc_list n ON n.npc_id = d.npc_id "
            "LEFT JOIN players p ON p.player_id = d.player_id "
            "WHERE d.player_id IN :pids "
            "  AND d.date_added >= :start AND d.date_added < :end "
            "  AND d.hidden = 0 "
            "ORDER BY total_value DESC LIMIT 1"
        ).bindparams(bindparam("pids", expanding=True)),
        {"pids": player_ids, "start": start, "end": end},
    ).first()
    if not row:
        return None
    (
        drop_id, player_id, player_name, item_id, item_name, npc_id, npc_name,
        value, quantity, total_value, date_added, image_url, kill_count,
    ) = row
    return {
        "drop_id": int(drop_id),
        "player_id": int(player_id),
        "player_name": player_name,
        "item_id": int(item_id) if item_id else None,
        "item_name": item_name,
        "npc_id": int(npc_id) if npc_id else None,
        "npc_name": npc_name,
        "value": int(value or 0),
        "quantity": int(quantity or 0),
        "total_value": int(total_value or 0),
        "date": date_added.isoformat() if date_added else None,
        "image_url": _public_image_url(image_url),
        "kill_count": int(kill_count) if kill_count is not None else None,
    }


def _true_kills(session, player_ids: list[int], period: str, npc_ids: list[int]) -> dict:
    """Distinct drop timestamps per NPC — kills as the lootboard has always
    counted them (a multi-item kill shares one timestamp), matching
    ``_month_npc_boxes``. The rollups' ``drop_count`` counts item rows, not
    kills, so it would overstate every multi-drop boss.

    Restricted to the NPCs already selected for the card, so this stays a
    bounded read rather than a whole-period group-by.
    """
    if not player_ids or not npc_ids:
        return {}
    start, end = month_bounds(period)
    rows = session.execute(
        text(
            "SELECT d.npc_id, COUNT(DISTINCT d.date_added) "
            "FROM drops d FORCE INDEX (ix_drops_player_id_date_added) "
            "WHERE d.player_id IN :pids "
            "  AND d.date_added >= :start AND d.date_added < :end "
            "  AND d.npc_id IN :npcs "
            "GROUP BY d.npc_id"
        ).bindparams(
            bindparam("pids", expanding=True), bindparam("npcs", expanding=True)
        ),
        {"pids": player_ids, "npcs": npc_ids, "start": start, "end": end},
    ).fetchall()
    return {int(npc_id): int(cnt or 0) for npc_id, cnt in rows}


# --------------------------------------------------------------------------- #
# Achievement counts
# --------------------------------------------------------------------------- #
# Each entry is (payload key, table, date column). All of these have an index on
# player_id and are small enough (the largest, `collection`, is ~260k rows) that
# the player_id ref does the work and the date filter is applied on top.
#
# Several of these only start partway through the tracked history, so a 0 here
# frequently means "not captured yet" rather than "the player did none". First
# row observed per table:
#
#   player_pets        2026-01-16
#   quest_completions  2026-02-13
#   player_deaths      2026-07-07   (plugin release not yet live)
#   diary_completions  never        (plugin release not yet live)
#
# They are all computed regardless, so the first month of real data lights them
# up with no migration and no schema bump. **The renderer must omit any card
# whose count is 0** rather than printing a zero — "0 pets in 2025" reads as a
# fact about the player when it is really a fact about our pipeline, and one
# untrustworthy number on a card discredits every number next to it.
_ACHIEVEMENT_SOURCES = (
    ("pbs", "personal_best", "date_added"),
    ("clog_slots", "collection", "date_added"),
    ("cas", "combat_achievement", "date_added"),
    ("pets", "player_pets", "date_added"),
    ("quests", "quest_completions", "date_added"),
    ("diaries", "diary_completions", "date_added"),
    ("deaths", "player_deaths", "date_added"),
)


def _achievements(session, player_ids: list[int], period: str) -> dict:
    if not player_ids:
        return {key: 0 for key, _, _ in _ACHIEVEMENT_SOURCES}
    start, end = month_bounds(period)
    out: dict[str, int] = {}
    for key, table, date_col in _ACHIEVEMENT_SOURCES:
        try:
            row = session.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE player_id IN :pids "
                    f"  AND {date_col} >= :start AND {date_col} < :end"
                ).bindparams(bindparam("pids", expanding=True)),
                {"pids": player_ids, "start": start, "end": end},
            ).first()
            out[key] = int(row[0]) if row else 0
        except Exception:
            # A table that doesn't exist yet on some deployment must not take
            # the whole recap down with it.
            out[key] = 0
    return out


# --------------------------------------------------------------------------- #
# Subject resolution
# --------------------------------------------------------------------------- #
def _visible_group_player_ids(session, group_id: int) -> list[int]:
    """Group members, minus players who have opted out of public display.

    The rollups carry hidden players' rows, so the filter has to be applied
    here — the same correction ``services/badges._visible_player_ids`` makes.
    """
    rows = session.execute(
        text("SELECT player_id FROM user_group_association WHERE group_id = :gid"),
        {"gid": group_id},
    ).fetchall()
    ids = {int(r[0]) for r in rows if r[0]}
    if not ids:
        return []
    try:
        from web_api.common import hidden_player_ids

        ids -= set(hidden_player_ids())
    except Exception:
        pass
    return sorted(ids)


# Groups 1 and 2 are infrastructure, not clans — 1 is the config template and 2
# holds every tracked player. Neither belongs in a player's clan line.
_NON_CLAN_GROUP_IDS = (1, 2)


def _player_groups(session, player_id: int, limit: int = 2) -> list[dict]:
    """The clans this player belongs to.

    On a personal card the clan name is the context the player didn't already
    know they'd get — it's what makes the card theirs rather than a stat sheet.
    Capped because a few players sit in many groups and the header has room for
    two. ``user_group_association`` carries no join date, so the order is by
    group id (stable, not chronological) and the cap takes the oldest clans.
    """
    rows = session.execute(
        text(
            "SELECT g.group_id, g.group_name FROM user_group_association a "
            "JOIN groups g ON g.group_id = a.group_id "
            "WHERE a.player_id = :pid AND a.group_id NOT IN :skip "
            "ORDER BY a.group_id"
        ).bindparams(bindparam("skip", expanding=True)),
        {"pid": player_id, "skip": list(_NON_CLAN_GROUP_IDS)},
    ).fetchall()
    return [{"id": int(gid), "name": name} for gid, name in rows[:limit] if gid and name]


def _player_is_hidden(player_id: int) -> bool:
    """Whether this player has opted out of public display.

    Group cards already drop hidden members from their aggregates
    (:func:`_visible_group_player_ids`); a *personal* card is the stronger case,
    because the whole card is about one person and it gets a permanent public
    URL. Fails closed: if the privacy list can't be read, treat the player as
    hidden rather than publish a card we can't confirm they allow.
    """
    try:
        from web_api.common import hidden_player_ids

        return player_id in hidden_player_ids()
    except Exception:
        return True


def _player_names(session, player_ids: list[int]) -> dict[int, str]:
    if not player_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT player_id, player_name FROM players WHERE player_id IN :pids"
        ).bindparams(bindparam("pids", expanding=True)),
        {"pids": player_ids},
    ).fetchall()
    return {int(pid): name for pid, name in rows}


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #
def _base_month_payload(session, player_ids: list[int], period: str) -> dict:
    """The parts of a card that are identical for a player and a group — one
    subject is just a player_id list of length one."""
    items = _item_rollup(session, player_ids, period)
    npcs = _npc_rollup(session, player_ids, period)
    kills = _true_kills(
        session, player_ids, period, [n["npc_id"] for n in npcs["top_npcs"]]
    )
    for npc in npcs["top_npcs"]:
        npc["kills"] = kills.get(npc["npc_id"], 0)
    return {
        "totals": {
            "loot_rollup": items["loot"],
            "drops": items["drops"],
            "unique_items": items["unique_items"],
            # None (not 0) when the NPC rollup has no rows for this month — see
            # `_npc_coverage`. Renderers must omit the card, never print a zero.
            "unique_npcs": npcs["unique_npcs"],
        },
        "top_items": items["top_items"],
        "top_npcs": npcs["top_npcs"],
        "npc_data_available": npcs["available"],
        "activity": {"by_hour": items["by_hour"], "by_weekday": items["by_weekday"]},
        "biggest_drop": _biggest_drop(session, player_ids, period),
        "achievements": _achievements(session, player_ids, period),
    }


def compute_player_month(session, player_id: int, period: str) -> Optional[dict]:
    """One player's recap for one month, or ``None`` if they're below the
    activity floor or have opted out of public display."""
    if _player_is_hidden(player_id):
        return None

    partition = period_partition(period)
    payload = _base_month_payload(session, [player_id], period)
    if payload["totals"]["drops"] < MIN_DROPS_FOR_RECAP:
        return None

    prev_period = previous_month_period(period)
    prev_partition = period_partition(prev_period)
    totals = _redis_totals([player_id], partition)
    prev_totals = _redis_totals([player_id], prev_partition)
    rank, ranked = _player_rank(player_id, partition)
    prev_rank, prev_ranked = _player_rank(player_id, prev_partition)
    names = _player_names(session, [player_id])

    payload["totals"]["loot"] = int(totals.get(player_id, 0))
    payload["scope"] = SCOPE_PLAYER
    payload["subject"] = {
        "id": player_id,
        "name": names.get(player_id),
        "groups": _player_groups(session, player_id),
    }
    # Per-item attribution is meaningful on a clan card and tautological on a
    # player's own: every item here was received by the subject. Strip it rather
    # than make the renderer special-case scope, and keep the stored payload from
    # repeating the subject's name once per item.
    for entry in payload.get("top_items", []):
        entry.pop("receiver", None)
        entry.pop("receivers", None)
    payload["rank"] = {
        "position": rank,
        "of": ranked,
        # Percentile is the highest-share-rate card type there is, and it costs
        # nothing here because the rank and the board size are both O(1) reads.
        "percentile": round(100.0 * rank / ranked, 1) if rank and ranked else None,
        "previous_loot": int(prev_totals.get(player_id, 0)),
        # Last month's placing, so the card can show movement. Both boards are
        # O(1) Redis reads.
        #
        # Deliberately NOT differenced into "up 40 places": the board's size
        # changes month to month, so a player who held still while 300 accounts
        # joined below them hasn't moved down, and subtracting positions would
        # claim they had. The card states both placings and lets the reader draw
        # the line. `previous_of` is here so that judgement stays auditable.
        "previous_position": prev_rank,
        "previous_of": prev_ranked,
    }
    return _finalize(payload, period)


def compute_group_month(session, group_id: int, period: str) -> Optional[dict]:
    """One group's recap for one month.

    Computed as a single batched pass over the whole roster rather than a fold
    over per-member snapshots: one ranged rollup read covering N players beats N
    reads covering one, and it means group recaps don't depend on player
    snapshots existing.
    """
    partition = period_partition(period)
    player_ids = _visible_group_player_ids(session, group_id)
    if not player_ids:
        return None
    if len(player_ids) > MAX_ROSTER_FOR_RECAP:
        raise RosterTooLarge(
            f"group {group_id} has {len(player_ids)} members "
            f"(> {MAX_ROSTER_FOR_RECAP}); this is a synthetic group, not a clan"
        )

    payload = _base_month_payload(session, player_ids, period)
    if payload["totals"]["drops"] < MIN_DROPS_FOR_RECAP:
        return None

    member_totals = _redis_totals(player_ids, partition)
    prev_partition = period_partition(previous_month_period(period))
    prev_totals = _redis_totals(player_ids, prev_partition)
    names = _player_names(session, player_ids)
    rank, ranked, board_loot = _group_rank(group_id, partition)

    ranked_members = sorted(
        (
            {
                "player_id": pid,
                "name": names.get(pid),
                "loot": int(loot),
                "previous_loot": int(prev_totals.get(pid, 0)),
            }
            for pid, loot in member_totals.items()
            if loot
        ),
        key=lambda m: m["loot"],
        reverse=True,
    )

    group_row = session.execute(
        text("SELECT group_name FROM groups WHERE group_id = :gid"), {"gid": group_id}
    ).first()

    payload["totals"]["loot"] = sum(m["loot"] for m in ranked_members)
    # Summed from member keys rather than read from `gleaderboard:{partition}`,
    # which is only written for the current partition and so has no rows before
    # 202511. Member keys persist back to 202410, so this works for any month —
    # and it matches what the website's own `/groups/{id}` page reports, which
    # is the number a reader will compare against. (Hidden players are excluded
    # here and are not on that page; a public recap should honour the opt-out.)
    payload["totals"]["members_active"] = len(ranked_members)
    payload["totals"]["members_total"] = len(player_ids)
    payload["scope"] = SCOPE_GROUP
    payload["subject"] = {"id": group_id, "name": group_row[0] if group_row else None}
    payload["rank"] = {
        "position": rank,
        "of": ranked,
        "previous_loot": sum(m["previous_loot"] for m in ranked_members),
        # The score the rank was actually computed from — WOM-roster scoped, so
        # it won't equal totals.loot. Kept so the number is auditable rather
        # than looking like an arithmetic error. See `_group_rank`.
        "board_loot": board_loot,
    }
    payload["top_members"] = ranked_members[:TOP_N]
    payload["superlatives"] = _group_superlatives(session, player_ids, period, names)
    return _finalize(payload, period)


def _group_superlatives(session, player_ids: list[int], period: str, names: dict) -> dict:
    """Named per-member awards — the half of a clan card officers actually post.

    Each is a bounded ranged read over the roster, and each returns ``None``
    rather than a zero row when nobody qualifies, so the renderer can drop the
    card instead of printing "Most Pets: nobody, 0".
    """
    start, end = month_bounds(period)
    params = {"pids": player_ids, "start": start, "end": end}

    def _top_from(table: str, date_col: str = "date_added") -> Optional[dict]:
        try:
            row = session.execute(
                text(
                    f"SELECT player_id, COUNT(*) AS c FROM {table} "
                    f"WHERE player_id IN :pids "
                    f"  AND {date_col} >= :start AND {date_col} < :end "
                    f"GROUP BY player_id ORDER BY c DESC LIMIT 1"
                ).bindparams(bindparam("pids", expanding=True)),
                params,
            ).first()
        except Exception:
            return None
        if not row or not row[1]:
            return None
        return {"player_id": int(row[0]), "name": names.get(int(row[0])), "count": int(row[1])}

    return {
        "most_pbs": _top_from("personal_best"),
        "most_clog_slots": _top_from("collection"),
        "most_cas": _top_from("combat_achievement"),
        "most_pets": _top_from("player_pets"),
        # Populated once the plugin release carrying deaths goes live; the
        # renderer omits it while it's None. This is the roast card.
        "most_deaths": _top_from("player_deaths"),
    }


def _fold_receiver(agg: dict, entry: dict) -> None:
    """Carry per-item attribution through the annual fold, but only when it stays
    true.

    A monthly snapshot records the *top* receiver of an item for that month. Those
    can't be summed: if Chapsz got the June Masori body and Binny got the
    September one, neither is "the" receiver for the year, and picking the larger
    month would print one clanmate's name over another's drop.

    So a receiver survives the fold only while every folded month that saw the
    item reports the same sole receiver. The moment two months disagree — or any
    month had it shared — attribution is dropped and only the count remains,
    which the renderer reads as "shared" and shows no name for. Erring toward
    saying nothing is the whole point: a name a clanmate can disprove costs more
    than a blank.
    """
    receivers = int(entry.get("receivers") or 0)
    incoming = entry.get("receiver")

    # Once dropped, stay dropped — a later agreeing month must not resurrect it.
    if agg.get("_recv_conflict"):
        agg["receivers"] = max(int(agg.get("receivers") or 0), receivers)
        return

    if receivers > 1 or not incoming:
        agg["_recv_conflict"] = True
        agg.pop("receiver", None)
        agg["receivers"] = max(int(agg.get("receivers") or 0), receivers or 2)
        return

    held = agg.get("receiver")
    if held and held.get("player_id") != incoming.get("player_id"):
        agg["_recv_conflict"] = True
        agg.pop("receiver", None)
        # Distinct people across months: shared over the year, however sole it
        # was in any single month.
        agg["receivers"] = 2
        return

    agg["receiver"] = incoming
    agg["receivers"] = 1


def compute_year(session, scope: str, subject_id: int, year: int) -> Optional[dict]:
    """Fold the twelve stored monthly snapshots into an annual card.

    Deliberately reads *only* ``recap_snapshots`` — one player-year against
    ``drops`` measures 6.1 s, which is why the monthly rows are persisted in the
    first place. A year with no stored months yields ``None`` rather than a
    silently empty card.
    """
    months = []
    for period in year_months(year):
        stored = load_snapshot(session, scope, subject_id, period)
        if stored:
            months.append((period, stored))
    if not months:
        return None

    totals = {"loot": 0, "drops": 0, "loot_rollup": 0}
    items: dict[int, dict] = {}
    npcs: dict[int, dict] = {}
    achievements: dict[str, int] = {}
    by_hour = [0] * 24
    by_weekday = [0] * 7
    by_month = []
    biggest = None
    # The NPC rollup has month-sized holes (see `_npc_coverage`), so an annual
    # boss total is only trustworthy if every folded month had coverage. Tracked
    # rather than assumed: a partial year would otherwise understate kills
    # silently, which is worse than omitting the card.
    npc_months_covered = 0

    for period, snap in months:
        if snap.get("npc_data_available"):
            npc_months_covered += 1
        t = snap.get("totals", {})
        for key in totals:
            totals[key] += int(t.get(key, 0) or 0)
        by_month.append({"period": period, "loot": int(t.get("loot", 0) or 0)})

        for src, dest, id_key in ((snap.get("top_items", []), items, "item_id"),
                                  (snap.get("top_npcs", []), npcs, "npc_id")):
            for entry in src:
                key = entry.get(id_key)
                if key is None:
                    continue
                # Every accumulating field must be zeroed in the seed: `{**entry}`
                # carries the first month's values, so anything not reset here
                # gets counted twice.
                agg = dest.setdefault(
                    int(key),
                    {**entry, "loot": 0, "drops": 0, "kills": 0, "quantity": 0},
                )
                agg["loot"] += int(entry.get("loot", 0) or 0)
                agg["drops"] += int(entry.get("drops", 0) or 0)
                agg["kills"] += int(entry.get("kills", 0) or 0)
                agg["quantity"] += int(entry.get("quantity", 0) or 0)
                _fold_receiver(agg, entry)

        for key, value in (snap.get("achievements") or {}).items():
            achievements[key] = achievements.get(key, 0) + int(value or 0)

        activity = snap.get("activity") or {}
        for i, v in enumerate((activity.get("by_hour") or [])[:24]):
            by_hour[i] += int(v or 0)
        for i, v in enumerate((activity.get("by_weekday") or [])[:7]):
            by_weekday[i] += int(v or 0)

        candidate = snap.get("biggest_drop")
        if candidate and (
            biggest is None
            or int(candidate.get("total_value", 0)) > int(biggest.get("total_value", 0))
        ):
            biggest = candidate

    # Unique counts can't be summed across months — the same item dropping in
    # March and July is one unique item, not two. The folded maps hold only the
    # per-month top N, so these are a floor, and they're labelled as such.
    top_items = sorted(items.values(), key=lambda x: x["loot"], reverse=True)[:TOP_N]
    top_npcs = sorted(npcs.values(), key=lambda x: x["loot"], reverse=True)[:TOP_N]
    # Drop the fold's bookkeeping flag before it reaches the stored payload.
    for entry in (*top_items, *top_npcs):
        entry.pop("_recv_conflict", None)

    payload = {
        "scope": scope,
        "subject": months[0][1].get("subject"),
        "totals": totals,
        "top_items": top_items,
        "top_npcs": top_npcs,
        "activity": {"by_hour": by_hour, "by_weekday": by_weekday},
        "by_month": by_month,
        "biggest_drop": biggest,
        "achievements": achievements,
        "months_covered": [p for p, _ in months],
        # True only when every folded month had NPC rollup coverage.
        "npc_data_available": npc_months_covered == len(months),
        "npc_months_covered": npc_months_covered,
        # The best month is the card people screenshot; it falls out of the fold.
        "peak_month": max(by_month, key=lambda m: m["loot"]) if by_month else None,
    }
    return _finalize(payload, str(year))


def _finalize(payload: dict, period: str) -> dict:
    payload["period"] = period
    payload["schema_version"] = RECAP_SCHEMA_VERSION
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return payload


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def save_snapshot(session, scope: str, subject_id: int, period: str, payload: dict):
    """Upsert. Re-running a cycle for a period it has already done refreshes the
    row rather than duplicating it — the unique constraint is the guard."""
    row = (
        session.query(RecapSnapshot)
        .filter(
            RecapSnapshot.scope == scope,
            RecapSnapshot.subject_id == subject_id,
            RecapSnapshot.period == period,
        )
        .first()
    )
    blob = json.dumps(payload, separators=(",", ":"), default=str)
    if row:
        row.payload = blob
        row.schema_version = RECAP_SCHEMA_VERSION
        row.generated_at = datetime.now()
    else:
        row = RecapSnapshot(
            scope=scope,
            subject_id=subject_id,
            period=period,
            payload=blob,
            schema_version=RECAP_SCHEMA_VERSION,
            generated_at=datetime.now(),
        )
        session.add(row)
    return row


def load_snapshot(session, scope: str, subject_id: int, period: str) -> Optional[dict]:
    row = (
        session.query(RecapSnapshot)
        .filter(
            RecapSnapshot.scope == scope,
            RecapSnapshot.subject_id == subject_id,
            RecapSnapshot.period == period,
        )
        .first()
    )
    if not row:
        return None
    try:
        return json.loads(row.payload)
    except (TypeError, ValueError):
        return None


def stored_periods(session, scope: str, subject_id: int) -> list[str]:
    """Every period a subject has a recap for, newest first — backs the archive
    index on the profile."""
    rows = (
        session.query(RecapSnapshot.period)
        .filter(RecapSnapshot.scope == scope, RecapSnapshot.subject_id == subject_id)
        .all()
    )
    return sorted((r[0] for r in rows), reverse=True)
