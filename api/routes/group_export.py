"""
Group data-export API.

Key-authenticated endpoints that let a group pull its own tracked data
programmatically (spreadsheets, clan sites, competition tooling, etc.).

AUTHENTICATION
    Every endpoint requires the group's export API key
    (GroupConfiguration key = "export_api_key"), passed as any of:
      • Authorization header:  Authorization: Bearer <key>
      • X-API-Key header:      X-API-Key: <key>
      • Query parameter:       ?api_key=<key>

ENDPOINTS
    GET /groups/<group_id>/export/top-players
        Ranked loot leaderboard for the group's members over a time window,
        optionally restricted to one or more NPCs, with per-player item
        breakdowns.
    GET /groups/<group_id>/export/drops
        Raw drop records for the group over a time window (paginated).
    GET /groups/<group_id>/export/members
        Current member list.

TIME WINDOWS
    start_time / end_time accept ISO-8601 (2026-07-01T00:00:00Z) or unix
    epoch (seconds or milliseconds). All stored timestamps are UTC.
    Defaults: end_time = now, start_time = end_time - 30 days.
    Maximum window: 366 days.
"""

import hmac
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from quart import Blueprint, jsonify, request
from quart_cors import route_cors
from quart_rate_limiter import rate_limit
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import OperationalError

from api.core import get_db_session, logger
from db import (
    Drop,
    Group,
    GroupConfiguration,
    ItemList,
    NpcList,
    Player,
    PlayerItemHourlyTotals,
    user_group_association,
)

group_export_bp = Blueprint("group_export", __name__)

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
TOP_PLAYERS_DEFAULT_LIMIT = 25
TOP_PLAYERS_MAX_LIMIT = 100
ITEMS_PER_PLAYER_DEFAULT = 10
ITEMS_PER_PLAYER_MAX = 25
DROPS_DEFAULT_LIMIT = 100
DROPS_MAX_LIMIT = 500
DROPS_MAX_OFFSET = 100_000
# Guard against the global catch-all pseudo-groups (10k+ members) whose
# window scans are far too heavy for synchronous requests. The largest real
# clan is well under this. Such callers should use the public global
# endpoints (/top_players etc.) instead.
MAX_EXPORT_MEMBERS = 2500


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _extract_api_key() -> str:
    """Pull the export API key from the request (header or query param)."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    header_key = request.headers.get("X-API-Key", "")
    if header_key.strip():
        return header_key.strip()
    return (request.args.get("api_key") or "").strip()


def _authenticate_group(db_session, group_id: int):
    """Resolve the group and validate the caller's export API key.

    Returns (group, None) on success or (None, (response, status)) on failure.
    """
    group = db_session.query(Group).filter(Group.group_id == group_id).first()
    if not group:
        return None, (jsonify({"success": False, "error": f"Group {group_id} not found"}), 404)

    api_key = _extract_api_key()
    if not api_key:
        return None, (jsonify({"success": False, "error": "API key required"}), 401)

    # A group can have more than one export_api_key row (historical dupes);
    # the dashboard may surface either, so accept a match against any of them.
    key_rows = db_session.query(GroupConfiguration).filter(
        GroupConfiguration.group_id == group_id,
        GroupConfiguration.config_key == "export_api_key",
    ).all()

    for key_cfg in key_rows:
        stored = str(key_cfg.config_value).strip() if key_cfg.config_value else ""
        # "0" is the pre-generation placeholder value, never a valid key.
        if stored and stored != "0" and hmac.compare_digest(stored, api_key):
            return group, None

    return None, (jsonify({"success": False, "error": "Invalid API key"}), 403)


def parse_time(value: str):
    """Parse an ISO-8601 or unix-epoch timestamp into a naive UTC datetime.

    Returns None if the value cannot be parsed.
    """
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None

    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        try:
            epoch = int(value)
        except ValueError:
            return None
        if abs(epoch) >= 10 ** 12:  # milliseconds
            epoch = epoch / 1000
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def resolve_window():
    """Read start_time/end_time from the query string.

    Returns (start, end, None) or (None, None, (response, status)) on bad input.
    """
    raw_start = request.args.get("start_time")
    raw_end = request.args.get("end_time")

    end = parse_time(raw_end) if raw_end else datetime.utcnow()
    if raw_end and end is None:
        return None, None, (jsonify({
            "success": False,
            "error": "Invalid end_time; use ISO-8601 or unix epoch",
        }), 400)

    start = parse_time(raw_start) if raw_start else (end - timedelta(days=DEFAULT_WINDOW_DAYS))
    if raw_start and start is None:
        return None, None, (jsonify({
            "success": False,
            "error": "Invalid start_time; use ISO-8601 or unix epoch",
        }), 400)

    if start >= end:
        return None, None, (jsonify({
            "success": False,
            "error": "start_time must be before end_time",
        }), 400)

    if (end - start) > timedelta(days=MAX_WINDOW_DAYS):
        return None, None, (jsonify({
            "success": False,
            "error": f"Time window too large (max {MAX_WINDOW_DAYS} days)",
        }), 400)

    return start, end, None


def _parse_int_arg(name: str, default: int, minimum: int, maximum: int):
    """Read an integer query param, clamped to [minimum, maximum]."""
    raw = request.args.get(name)
    if raw is None or not str(raw).strip():
        return default, None
    try:
        value = int(raw)
    except ValueError:
        return None, (jsonify({"success": False, "error": f"Invalid {name}"}), 400)
    return max(minimum, min(maximum, value)), None


def _resolve_npcs(db_session):
    """Resolve npc_id (comma-separated ids) and/or npc_name query params.

    Returns (npc_rows, None) — empty list means no NPC filter — or
    (None, (response, status)) when a filter was given but nothing matched.
    """
    raw_ids = request.args.get("npc_id", "")
    raw_name = request.args.get("npc_name", "")
    if not raw_ids.strip() and not raw_name.strip():
        return [], None

    npc_ids = set()
    for token in raw_ids.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            npc_ids.add(int(token))
        except ValueError:
            return None, (jsonify({"success": False, "error": f"Invalid npc_id value: {token!r}"}), 400)

    filters = []
    if npc_ids:
        filters.append(NpcList.npc_id.in_(npc_ids))
    if raw_name.strip():
        # A boss name can map to several npc_ids (variants); include them all.
        filters.append(NpcList.npc_name == raw_name.strip())

    npcs = db_session.query(NpcList).filter(or_(*filters)).all()
    if not npcs:
        return None, (jsonify({"success": False, "error": "No matching NPCs found"}), 404)
    return npcs, None


def _member_player_ids(db_session, group_id: int):
    """Player IDs of the group's current members (hidden players excluded)."""
    rows = db_session.execute(
        select(user_group_association.c.player_id)
        .where(
            user_group_association.c.group_id == group_id,
            user_group_association.c.player_id.isnot(None),
        )
        .distinct()
    ).fetchall()
    player_ids = [row[0] for row in rows]
    if not player_ids:
        return []
    hidden_rows = db_session.query(Player.player_id).filter(
        Player.player_id.in_(player_ids),
        Player.hidden == True,  # noqa: E712
    ).all()
    hidden_ids = {row[0] for row in hidden_rows}
    return [pid for pid in player_ids if pid not in hidden_ids]


def _too_large_response(group_id: int):
    return jsonify({
        "success": False,
        "error": (
            f"Group {group_id} has too many members for the export API "
            f"(limit {MAX_EXPORT_MEMBERS}); use the public global endpoints instead"
        ),
    }), 413


def _iso_utc(dt) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


# ──────────────────────────────────────────────────────────────────────────────
# Query safety net
# ──────────────────────────────────────────────────────────────────────────────
# The shared engine sets a 30s client read_timeout (db/models/base.py). A drops
# aggregate that runs past it dies as pymysql errno 2013 "Lost connection …
# (timed out)" — an unhandled 500 — and, worse, keeps scanning server-side after
# the client has already given up. We cap execution *server-side* below that
# client timeout so MariaDB aborts the query itself (errno 1969), which we turn
# into a clean 503. Kept a few seconds under 30 so the server wins the race.
STATEMENT_TIMEOUT_SECONDS = 25
# MariaDB timeout / lost-connection error codes worth surfacing as "too heavy".
_TIMEOUT_ERR_CODES = {1969, 3024, 2013}


@contextmanager
def _statement_timeout(db_session, seconds: int = STATEMENT_TIMEOUT_SECONDS):
    """Bound server-side execution time for queries run inside the block.

    Sessions come from the shared pool, so the cap is reset on exit to avoid it
    riding a checked-in connection into the next caller. A query killed by the
    timeout leaves the connection usable, so the reset still runs.
    """
    db_session.execute(text(f"SET SESSION max_statement_time = {int(seconds)}"))
    try:
        yield
    finally:
        try:
            db_session.execute(text("SET SESSION max_statement_time = 0"))
        except Exception:
            pass


def _is_timeout_error(err: OperationalError) -> bool:
    try:
        return err.orig.args[0] in _TIMEOUT_ERR_CODES
    except (AttributeError, IndexError, TypeError):
        return False


def _query_too_heavy_response():
    return jsonify({
        "success": False,
        "error": (
            "Query timed out. Narrow the request — use a smaller time window "
            "(start_time/end_time) or add an npc_id/npc_name filter — and retry."
        ),
    }), 503


# ──────────────────────────────────────────────────────────────────────────────
# Hybrid aggregation (top-players, no NPC filter)
# ──────────────────────────────────────────────────────────────────────────────
# A full-history drops aggregate over a wide window is what tripped the timeout.
# `player_item_hourly_totals` is an hourly rollup (SUM(value*quantity),
# drop_count, MAX(date_added) as last_drop_time) that reconciles exactly with
# `drops` for settled months but lags/has gaps for the in-progress month. So we
# read settled months from the rollup (cheap) and the current calendar month
# straight from `drops` (source of truth, bounded to ≤1 month), then merge.
#
# Windowing note: the rollup is bucketed by hour, so for the pre-current-month
# portion start_time/end_time are effectively floored to the hour, and a drop's
# first_drop from that portion is hour-granular. The current-month portion (from
# drops) stays exact to the second. Day/month-aligned windows are exact either
# way. NPC-filtered requests never take this path (the item rollup has no NPC
# dimension) — they aggregate from `drops` directly, under the same timeout cap.

# For a large member IN-list the optimizer mis-picks the single-column
# `ix_drops_player_id` and reads each member's ENTIRE lifetime of drops before
# filtering by date (EXPLAIN: key=ix_drops_player_id, "Using where"). Forcing
# the composite index makes it seek per-player *within the date range* — the
# difference between a >30s timeout and ~11s for the current month of a
# 496-member clan. Kept in sync with the index name in the drops schema.
_DROPS_WINDOW_HINT = "FORCE INDEX (ix_drops_player_id_date_added)"


def _current_month_start() -> datetime:
    """First instant of the current calendar month (naive UTC)."""
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


def _hour_str(dt: datetime) -> str:
    """Rollup date_hour key ('YYYY-MM-DD-HH') for a datetime."""
    return dt.strftime("%Y-%m-%d-%H")


def _parse_hour(hour_key: str):
    """Inverse of _hour_str → datetime at the top of that hour (or None)."""
    try:
        return datetime.strptime(hour_key, "%Y-%m-%d-%H")
    except (ValueError, TypeError):
        return None


def _fold_player(agg, pid, value, count, first_dt, last_dt):
    entry = agg.get(pid)
    if entry is None:
        agg[pid] = [value, count, first_dt, last_dt]
        return
    entry[0] += value
    entry[1] += count
    if first_dt and (entry[2] is None or first_dt < entry[2]):
        entry[2] = first_dt
    if last_dt and (entry[3] is None or last_dt > entry[3]):
        entry[3] = last_dt


def _agg_players_hybrid(db_session, member_ids, start, end):
    """Per-player totals over [start, end): rollup for settled months + drops
    for the current month. Returns {pid: [total_value, drop_count, first_dt,
    last_dt]}."""
    boundary = _current_month_start()
    agg = {}

    rollup_hi = min(end, boundary)
    if start < rollup_hi:
        for pid, val, cnt, min_hour, max_last in db_session.query(
            PlayerItemHourlyTotals.player_id,
            func.sum(PlayerItemHourlyTotals.total_value),
            func.sum(PlayerItemHourlyTotals.drop_count),
            func.min(PlayerItemHourlyTotals.date_hour),
            func.max(PlayerItemHourlyTotals.last_drop_time),
        ).filter(
            PlayerItemHourlyTotals.player_id.in_(member_ids),
            PlayerItemHourlyTotals.date_hour >= _hour_str(start),
            PlayerItemHourlyTotals.date_hour < _hour_str(rollup_hi),
        ).group_by(PlayerItemHourlyTotals.player_id).all():
            _fold_player(agg, pid, int(val or 0), int(cnt or 0), _parse_hour(min_hour), max_last)

    drops_lo = max(start, boundary)
    if drops_lo < end:
        for pid, val, cnt, mn, mx in db_session.query(
            Drop.player_id,
            func.sum(Drop.value * Drop.quantity),
            func.count(Drop.drop_id),
            func.min(Drop.date_added),
            func.max(Drop.date_added),
        ).with_hint(Drop, _DROPS_WINDOW_HINT, "mysql").filter(
            Drop.player_id.in_(member_ids),
            Drop.date_added >= drops_lo,
            Drop.date_added < end,
            func.coalesce(Drop.hidden, False) == False,  # noqa: E712
        ).group_by(Drop.player_id).all():
            _fold_player(agg, pid, int(val or 0), int(cnt or 0), mn, mx)

    return agg


def _agg_items_hybrid(db_session, player_ids, start, end):
    """Per-(player, item) totals over [start, end): rollup for settled months +
    drops for the current month. Returns {pid: {item_id: [qty, value, count]}}."""
    boundary = _current_month_start()
    items = {}

    def _fold(pid, item_id, qty, value, count):
        by_item = items.setdefault(pid, {})
        entry = by_item.get(item_id)
        if entry is None:
            by_item[item_id] = [qty, value, count]
        else:
            entry[0] += qty
            entry[1] += value
            entry[2] += count

    rollup_hi = min(end, boundary)
    if start < rollup_hi:
        for pid, item_id, qty, val, cnt in db_session.query(
            PlayerItemHourlyTotals.player_id,
            PlayerItemHourlyTotals.item_id,
            func.sum(PlayerItemHourlyTotals.quantity),
            func.sum(PlayerItemHourlyTotals.total_value),
            func.sum(PlayerItemHourlyTotals.drop_count),
        ).filter(
            PlayerItemHourlyTotals.player_id.in_(player_ids),
            PlayerItemHourlyTotals.date_hour >= _hour_str(start),
            PlayerItemHourlyTotals.date_hour < _hour_str(rollup_hi),
        ).group_by(
            PlayerItemHourlyTotals.player_id, PlayerItemHourlyTotals.item_id
        ).all():
            _fold(pid, item_id, int(qty or 0), int(val or 0), int(cnt or 0))

    drops_lo = max(start, boundary)
    if drops_lo < end:
        for pid, item_id, qty, val, cnt in db_session.query(
            Drop.player_id,
            Drop.item_id,
            func.sum(Drop.quantity),
            func.sum(Drop.value * Drop.quantity),
            func.count(Drop.drop_id),
        ).with_hint(Drop, _DROPS_WINDOW_HINT, "mysql").filter(
            Drop.player_id.in_(player_ids),
            Drop.date_added >= drops_lo,
            Drop.date_added < end,
            func.coalesce(Drop.hidden, False) == False,  # noqa: E712
        ).group_by(Drop.player_id, Drop.item_id).all():
            _fold(pid, item_id, int(qty or 0), int(val or 0), int(cnt or 0))

    return items


# ──────────────────────────────────────────────────────────────────────────────
# GET /groups/<group_id>/export/top-players
# ──────────────────────────────────────────────────────────────────────────────
# Query params:
#   npc_id           optional — one id or comma-separated list
#   npc_name         optional — exact NPC name (matches all id variants)
#   start_time       optional — ISO-8601 or epoch (default: end_time - 30d)
#   end_time         optional — ISO-8601 or epoch (default: now)
#   limit            optional — players returned (default 25, max 100)
#   include_items    optional — per-player item breakdown (default true)
#   items_per_player optional — items per player (default 10, max 25)
# ──────────────────────────────────────────────────────────────────────────────

@group_export_bp.get("/groups/<int:group_id>/export/top-players")
@route_cors(allow_origin="*")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def group_export_top_players(group_id: int):
    db_session = get_db_session()
    try:
        group, err = _authenticate_group(db_session, group_id)
        if err:
            return err

        start, end, err = resolve_window()
        if err:
            return err

        npcs, err = _resolve_npcs(db_session)
        if err:
            return err
        npc_ids = [npc.npc_id for npc in npcs]

        limit, err = _parse_int_arg("limit", TOP_PLAYERS_DEFAULT_LIMIT, 1, TOP_PLAYERS_MAX_LIMIT)
        if err:
            return err
        items_per_player, err = _parse_int_arg(
            "items_per_player", ITEMS_PER_PLAYER_DEFAULT, 1, ITEMS_PER_PLAYER_MAX
        )
        if err:
            return err
        include_items = request.args.get("include_items", "true").strip().lower() not in ("false", "0", "no")

        member_ids = _member_player_ids(db_session, group_id)
        if len(member_ids) > MAX_EXPORT_MEMBERS:
            return _too_large_response(group_id)

        players_payload = []
        totals = {"total_value": 0, "drop_count": 0, "players_with_drops": 0}

        if member_ids:
            try:
                with _statement_timeout(db_session):
                    # Per-player aggregation. Without an NPC filter, settled
                    # months come from the hourly rollup and only the current
                    # month from `drops` (a wide all-drops scan is what timed
                    # out). NPC-filtered requests are naturally narrow, so they
                    # aggregate straight from `drops`.
                    if npc_ids:
                        npc_filters = [
                            Drop.player_id.in_(member_ids),
                            Drop.date_added >= start,
                            Drop.date_added < end,
                            func.coalesce(Drop.hidden, False) == False,  # noqa: E712
                            Drop.npc_id.in_(npc_ids),
                        ]
                        per_player = db_session.query(
                            Drop.player_id,
                            func.sum(Drop.value * Drop.quantity),
                            func.count(Drop.drop_id),
                            func.min(Drop.date_added),
                            func.max(Drop.date_added),
                        ).filter(*npc_filters).group_by(Drop.player_id).all()
                    else:
                        agg = _agg_players_hybrid(db_session, member_ids, start, end)
                        per_player = [(pid, v[0], v[1], v[2], v[3]) for pid, v in agg.items()]

                    totals = {
                        "total_value": sum(int(row[1] or 0) for row in per_player),
                        "drop_count": sum(int(row[2] or 0) for row in per_player),
                        "players_with_drops": len(per_player),
                    }
                    ranked = sorted(per_player, key=lambda row: int(row[1] or 0), reverse=True)[:limit]
                    top_ids = [row[0] for row in ranked]

                    name_by_id = {}
                    if top_ids:
                        for pid, pname, wom in db_session.query(
                            Player.player_id, Player.player_name, Player.wom_id
                        ).filter(Player.player_id.in_(top_ids)).all():
                            name_by_id[pid] = (pname, wom)

                    items_by_player = {}
                    if include_items and top_ids:
                        # {pid: {item_id: [qty, value, count]}}
                        if npc_ids:
                            items_raw = {}
                            for pid, item_id, qty, value, count in db_session.query(
                                Drop.player_id,
                                Drop.item_id,
                                func.sum(Drop.quantity),
                                func.sum(Drop.value * Drop.quantity),
                                func.count(Drop.drop_id),
                            ).filter(
                                Drop.player_id.in_(top_ids),
                                Drop.date_added >= start,
                                Drop.date_added < end,
                                func.coalesce(Drop.hidden, False) == False,  # noqa: E712
                                Drop.npc_id.in_(npc_ids),
                            ).group_by(Drop.player_id, Drop.item_id).all():
                                items_raw.setdefault(pid, {})[item_id] = [
                                    int(qty or 0), int(value or 0), int(count or 0)
                                ]
                        else:
                            items_raw = _agg_items_hybrid(db_session, top_ids, start, end)

                        wanted_item_ids = {iid for by_item in items_raw.values() for iid in by_item}
                        name_by_item = {}
                        if wanted_item_ids:
                            for iid, iname in db_session.query(
                                ItemList.item_id, ItemList.item_name
                            ).filter(ItemList.item_id.in_(wanted_item_ids)).all():
                                name_by_item[iid] = iname

                        for pid, by_item in items_raw.items():
                            entries = [{
                                "item_id": iid,
                                "item_name": name_by_item.get(iid),
                                "quantity": vals[0],
                                "total_value": vals[1],
                                "drop_count": vals[2],
                            } for iid, vals in by_item.items()]
                            entries.sort(key=lambda e: e["total_value"], reverse=True)
                            items_by_player[pid] = entries[:items_per_player]
            except OperationalError as exc:
                if _is_timeout_error(exc):
                    logger.log_sync(
                        "error",
                        f"group_export top-players query too heavy (group {group_id})",
                        {"group_id": group_id, "members": len(member_ids),
                         "start": str(start), "end": str(end), "npc_ids": npc_ids},
                    )
                    return _query_too_heavy_response()
                raise

            for rank, (pid, total_value, drop_count, first_drop, last_drop) in enumerate(ranked, start=1):
                pname, wom = name_by_id.get(pid, (None, None))
                entry = {
                    "rank": rank,
                    "player_id": pid,
                    "player_name": pname,
                    "wom_id": wom,
                    "total_value": int(total_value or 0),
                    "drop_count": int(drop_count or 0),
                    "first_drop": _iso_utc(first_drop),
                    "last_drop": _iso_utc(last_drop),
                }
                if include_items:
                    entry["items"] = items_by_player.get(pid, [])
                players_payload.append(entry)

        return jsonify({
            "success": True,
            "group": {"group_id": group.group_id, "group_name": group.group_name},
            "npcs": [{"npc_id": npc.npc_id, "npc_name": npc.npc_name} for npc in npcs] or None,
            "start_time": _iso_utc(start),
            "end_time": _iso_utc(end),
            "totals": totals,
            "players": players_payload,
        }), 200
    finally:
        db_session.close()


# ──────────────────────────────────────────────────────────────────────────────
# GET /groups/<group_id>/export/drops
# ──────────────────────────────────────────────────────────────────────────────
# Query params:
#   npc_id / npc_name  optional — same semantics as top-players
#   player_id          optional — restrict to one member (must be in the group)
#   min_value          optional — minimum per-drop total value (value × qty)
#   start_time / end_time — as above
#   limit              optional — rows returned (default 100, max 500)
#   offset             optional — pagination offset (newest first)
# ──────────────────────────────────────────────────────────────────────────────

@group_export_bp.get("/groups/<int:group_id>/export/drops")
@route_cors(allow_origin="*")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def group_export_drops(group_id: int):
    db_session = get_db_session()
    try:
        group, err = _authenticate_group(db_session, group_id)
        if err:
            return err

        start, end, err = resolve_window()
        if err:
            return err

        npcs, err = _resolve_npcs(db_session)
        if err:
            return err
        npc_ids = [npc.npc_id for npc in npcs]

        limit, err = _parse_int_arg("limit", DROPS_DEFAULT_LIMIT, 1, DROPS_MAX_LIMIT)
        if err:
            return err
        offset, err = _parse_int_arg("offset", 0, 0, DROPS_MAX_OFFSET)
        if err:
            return err
        min_value, err = _parse_int_arg("min_value", 0, 0, 2 ** 62)
        if err:
            return err

        member_ids = _member_player_ids(db_session, group_id)
        if len(member_ids) > MAX_EXPORT_MEMBERS:
            return _too_large_response(group_id)

        raw_player = (request.args.get("player_id") or "").strip()
        if raw_player:
            try:
                requested_pid = int(raw_player)
            except ValueError:
                return jsonify({"success": False, "error": "Invalid player_id"}), 400
            if requested_pid not in member_ids:
                return jsonify({"success": False, "error": "Player is not a member of this group"}), 404
            member_ids = [requested_pid]

        drops_payload = []
        if member_ids:
            filters = [
                Drop.player_id.in_(member_ids),
                Drop.date_added >= start,
                Drop.date_added < end,
                func.coalesce(Drop.hidden, False) == False,  # noqa: E712
            ]
            if npc_ids:
                filters.append(Drop.npc_id.in_(npc_ids))
            if min_value > 0:
                filters.append((Drop.value * Drop.quantity) >= min_value)

            try:
                with _statement_timeout(db_session):
                    rows = db_session.query(
                        Drop.drop_id,
                        Drop.player_id,
                        Player.player_name,
                        Drop.npc_id,
                        NpcList.npc_name,
                        Drop.item_id,
                        ItemList.item_name,
                        Drop.quantity,
                        Drop.value,
                        Drop.date_added,
                        Drop.image_url,
                    ).join(Player, Player.player_id == Drop.player_id).outerjoin(
                        NpcList, NpcList.npc_id == Drop.npc_id
                    ).outerjoin(
                        ItemList, ItemList.item_id == Drop.item_id
                    ).filter(*filters).order_by(
                        Drop.date_added.desc(), Drop.drop_id.desc()
                    ).limit(limit).offset(offset).all()
            except OperationalError as exc:
                if _is_timeout_error(exc):
                    logger.log_sync(
                        "error",
                        f"group_export drops query too heavy (group {group_id})",
                        {"group_id": group_id, "members": len(member_ids),
                         "start": str(start), "end": str(end),
                         "offset": offset, "npc_ids": npc_ids},
                    )
                    return _query_too_heavy_response()
                raise

            for row in rows:
                (drop_id, pid, pname, npc_id, npc_name, item_id,
                 item_name, quantity, value, date_added, image_url) = row
                drops_payload.append({
                    "drop_id": drop_id,
                    "player_id": pid,
                    "player_name": pname,
                    "npc_id": npc_id,
                    "npc_name": npc_name,
                    "item_id": item_id,
                    "item_name": item_name,
                    "quantity": int(quantity or 0),
                    "value_each": int(value or 0),
                    "total_value": int((value or 0) * (quantity or 0)),
                    "date_added": _iso_utc(date_added),
                    "image_url": image_url,
                })

        return jsonify({
            "success": True,
            "group": {"group_id": group.group_id, "group_name": group.group_name},
            "npcs": [{"npc_id": npc.npc_id, "npc_name": npc.npc_name} for npc in npcs] or None,
            "start_time": _iso_utc(start),
            "end_time": _iso_utc(end),
            "limit": limit,
            "offset": offset,
            "count": len(drops_payload),
            "drops": drops_payload,
        }), 200
    finally:
        db_session.close()


# ──────────────────────────────────────────────────────────────────────────────
# GET /groups/<group_id>/export/members
# ──────────────────────────────────────────────────────────────────────────────

@group_export_bp.get("/groups/<int:group_id>/export/members")
@route_cors(allow_origin="*")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def group_export_members(group_id: int):
    db_session = get_db_session()
    try:
        group, err = _authenticate_group(db_session, group_id)
        if err:
            return err

        member_ids = _member_player_ids(db_session, group_id)
        if len(member_ids) > MAX_EXPORT_MEMBERS:
            return _too_large_response(group_id)
        members = []
        if member_ids:
            rows = db_session.query(
                Player.player_id,
                Player.player_name,
                Player.wom_id,
                Player.total_level,
                Player.log_slots,
                Player.date_added,
            ).filter(Player.player_id.in_(member_ids)).order_by(Player.player_name).all()
            for pid, pname, wom, total_level, log_slots, date_added in rows:
                members.append({
                    "player_id": pid,
                    "player_name": pname,
                    "wom_id": wom,
                    "total_level": total_level,
                    "log_slots": log_slots,
                    "tracked_since": _iso_utc(date_added),
                })

        return jsonify({
            "success": True,
            "group": {"group_id": group.group_id, "group_name": group.group_name},
            "member_count": len(members),
            "members": members,
        }), 200
    finally:
        db_session.close()
