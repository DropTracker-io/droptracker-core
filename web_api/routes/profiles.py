"""Task 04 — player and group profiles.

GET /api/v1/players/{id}   -> PlayerProfile
GET /api/v1/groups/{id}    -> GroupProfile
GET /api/v1/groups/{id}/members?page=
"""
from __future__ import annotations

import asyncio
import json
import zlib
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import or_, text
from sqlalchemy.exc import OperationalError
from quart import Blueprint, jsonify, request

from db import (
    Player,
    Group,
    NotifiedSubmission,
    Drop,
    ItemList,
    CollectionLogEntry,
    PersonalBestEntry,
    NpcList,
)
from db.models import PlayerState
from utils.account_types import account_type_from_varbit
from web_api.common import (
    cache_get,
    cache_set,
    canonical_slug_for,
    db_session,
    decode_member,
    hidden_player_ids,
    leaderboard_key,
    money,
    parse_page,
    period_to_partition,
    player_global_rank,
    player_list_loot_sum,
    player_month_total,
    problem,
    with_cache_headers,
    _rc,
)
from web_api.routes.leaderboards import _compute_group_totals
from web_api.flair import group_flair, group_flairs

profiles_bp = Blueprint("v1_profiles", __name__)

IMG_BASE = "https://www.droptracker.io/img"


def _player_points(s, player_id: int):
    """Lifetime points earned (best-effort; omitted on error)."""
    try:
        from services.points import get_player_lifetime_points_earned

        return int(get_player_lifetime_points_earned(player_id=player_id, session=s))
    except Exception:
        return None


def _player_top_npc(s, player: Player, partition: int):
    """The player's highest-loot NPC name this month over the tracked NPC set
    (best-effort; omitted on error). Reads the hourly analytics rollup — the
    old per-NPC Redis leaderboards this used to read are no longer written."""
    try:
        from sqlalchemy import bindparam

        from data.TOP_NPCS import TOP_NPCS

        sql = text(
            "SELECT n.npc_name FROM player_npc_hourly_totals t "
            "JOIN npc_list n ON n.npc_id = t.npc_id "
            "WHERE t.player_id = :pid AND t.`partition` = :partition "
            "  AND t.npc_id IN :npc_ids "
            "GROUP BY t.npc_id, n.npc_name "
            "ORDER BY SUM(t.total_value) DESC LIMIT 1"
        ).bindparams(bindparam("npc_ids", expanding=True))
        row = s.execute(
            sql,
            {"pid": player.player_id, "partition": int(partition), "npc_ids": list(TOP_NPCS)},
        ).first()
        return row[0] if row else None
    except Exception:
        return None


def _convert_from_ms(ms: int) -> str:
    try:
        ms = int(ms)
    except Exception:
        return ""
    hours = ms // 3_600_000
    ms %= 3_600_000
    minutes = ms // 60_000
    ms %= 60_000
    seconds = ms // 1000
    ms %= 1000
    tenths = ms // 100
    if hours > 0:
        return f"{hours}:{minutes:02}:{seconds:02}.{tenths}"
    return f"{minutes}:{seconds:02}.{tenths}"


def _build_submissions(rows, s, partition, player_names: dict | None = None):
    """Map NotifiedSubmission rows to the contract SubmissionSchema list.

    ``player_names`` (player_id -> name) is supplied by the caller so group-scope
    listings can show who received each submission without an extra round trip
    per row; player-scope callers pass a single-entry map for their own id.
    """
    player_names = player_names or {}
    out = []
    seen = set()
    for sub in rows:
        try:
            ts = int(sub.date_added.timestamp()) if sub.date_added else 0
        except Exception:
            ts = 0
        player_id = getattr(sub, "player_id", None)
        player_name = player_names.get(player_id) if player_id else None

        if getattr(sub, "drop_id", None):
            key = ("drop", sub.drop_id)
            if key in seen:
                continue
            seen.add(key)
            drop = s.query(Drop).filter(Drop.drop_id == sub.drop_id).first()
            if not drop:
                continue
            item = s.query(ItemList).filter(ItemList.item_id == drop.item_id).first()
            label = item.item_name if item else f"Item {drop.item_id}"
            npc = s.query(NpcList).filter(NpcList.npc_id == drop.npc_id).first() if drop.npc_id else None
            out.append({
                "id": int(sub.drop_id),
                "type": "drop",
                "label": label,
                "value": money((drop.value or 0) * (drop.quantity or 1)),
                "quantity": int(drop.quantity or 1),
                "image_url": f"{IMG_BASE}/itemdb/{drop.item_id}.png",
                "item_id": int(drop.item_id) if drop.item_id else None,
                "npc_id": int(drop.npc_id) if drop.npc_id else None,
                "npc_name": npc.npc_name if npc else None,
                "player_id": player_id,
                "player_name": player_name,
                "ts": ts,
            })
        elif getattr(sub, "clog_id", None):
            key = ("clog", sub.clog_id)
            if key in seen:
                continue
            seen.add(key)
            clog = s.query(CollectionLogEntry).filter(CollectionLogEntry.log_id == sub.clog_id).first()
            if not clog:
                continue
            item = s.query(ItemList).filter(ItemList.item_id == clog.item_id).first()
            label = item.item_name if item else f"Item {clog.item_id}"
            npc = s.query(NpcList).filter(NpcList.npc_id == clog.npc_id).first() if clog.npc_id else None
            out.append({
                "id": int(sub.clog_id),
                "type": "clog",
                "label": label,
                "image_url": f"{IMG_BASE}/itemdb/{clog.item_id}.png",
                "item_id": int(clog.item_id) if clog.item_id else None,
                "npc_id": int(clog.npc_id) if clog.npc_id else None,
                "npc_name": npc.npc_name if npc else None,
                "player_id": player_id,
                "player_name": player_name,
                "ts": ts,
            })
        elif getattr(sub, "pb_id", None):
            key = ("pb", sub.pb_id)
            if key in seen:
                continue
            seen.add(key)
            pb = s.query(PersonalBestEntry).filter(PersonalBestEntry.id == sub.pb_id).first()
            if not pb:
                continue
            npc = s.query(NpcList).filter(NpcList.npc_id == pb.npc_id).first()
            npc_name = npc.npc_name if npc else "Unknown"
            raw_time = 0
            if pb.personal_best and 0 < pb.personal_best < (pb.kill_time or float("inf")):
                raw_time = pb.personal_best
            elif pb.kill_time and pb.kill_time > 0:
                raw_time = pb.kill_time
            label = f"{npc_name} PB: {_convert_from_ms(raw_time)}" if raw_time else f"{npc_name} PB"
            out.append({
                "id": int(sub.pb_id),
                "type": "pb",
                "label": label,
                "image_url": f"{IMG_BASE}/npcdb/{pb.npc_id}.png",
                "npc_id": int(pb.npc_id) if pb.npc_id else None,
                "npc_name": npc_name,
                "player_id": player_id,
                "player_name": player_name,
                "ts": ts,
            })
    return out


# --------------------------------------------------------------------------- #
# Profile stat blocks (group/player page enrichment).
#
# Everything here is best-effort: each block is wrapped so a failure omits the
# optional field instead of breaking the profile. Aggregates are cached
# in-process on top of the 30s HTTP cache because the boss/record SQL touches
# large tables.
# --------------------------------------------------------------------------- #
_STATS_TTL = 120.0

# Loot tracker, all-time mode (`?partition=all`): the fold of every month the
# account has, so it gets a much longer in-process TTL than a single month, and
# a server-side execution cap below the engine's 30s `read_timeout` (with the
# error codes MariaDB reports when it wins that race).
_ALL_TIME = "all"
_ALL_TIME_TTL = 900.0
_STATEMENT_TIMEOUT_SECONDS = 25
_TIMEOUT_ERR_CODES = {1969, 3024, 2013}
# Per-month NPC boxes, cached in Redis (shared by both workers, survives a
# restart) because an all-time fold reads every month of an account. A settled
# month is immutable, so it is held for days; only the live month is re-read.
# The floor matches `_parse_loot_partition`'s, so a bad `drops` row can never
# turn one request into years of empty month queries.
_MONTH_CACHE_PREFIX = "pstats:lootbox:"
_SETTLED_MONTH_CACHE_TTL = 7 * 24 * 3600
_CURRENT_MONTH_CACHE_TTL = 120
_EARLIEST_SUPPORTED_PARTITION = 202001


def _previous_partition(partition: int) -> int:
    year, month = divmod(int(partition), 100)
    if month <= 1:
        return (year - 1) * 100 + 12
    return year * 100 + (month - 1)


def _group_top_players(s, conn, group_id: int, partition, hidden: set, limit: int = 5):
    """Top members from the per-group monthly board, hidden accounts skipped."""
    if conn is None:
        return []
    raw = conn.zrevrange(
        leaderboard_key(partition, group_id=group_id), 0, limit + 14, withscores=True
    )
    ids = []
    scores = {}
    for member, score in raw:
        pid = decode_member(member)
        if pid is None or pid in hidden:
            continue
        ids.append(pid)
        scores[pid] = int(float(score))
    if not ids:
        return []
    names = {
        p.player_id: p.player_name
        for p in s.query(Player).filter(Player.player_id.in_(ids)).all()
    }
    out = []
    for pid in ids:
        name = names.get(pid)
        if not name:
            continue
        out.append({
            "rank": len(out) + 1,
            "id": pid,
            "name": name,
            "loot": money(scores[pid]),
        })
        if len(out) >= limit:
            break
    return out


def _top_bosses_sql(s, partition: int, group_id: int = None, player_id: int = None, limit: int = 5):
    """Most-farmed NPCs this month by loot (drop counts included), from the
    hourly analytics rollup. Scope to a group's members or a single player."""
    if group_id is not None:
        sql = text(
            "SELECT t.npc_id, n.npc_name, SUM(t.total_value) AS loot, SUM(t.drop_count) AS drops "
            "FROM player_npc_hourly_totals t "
            "JOIN user_group_association uga ON uga.player_id = t.player_id AND uga.group_id = :scope_id "
            "JOIN npc_list n ON n.npc_id = t.npc_id "
            "WHERE t.`partition` = :partition "
            "GROUP BY t.npc_id, n.npc_name ORDER BY loot DESC LIMIT :lim"
        )
        params = {"scope_id": group_id, "partition": int(partition), "lim": limit}
    else:
        sql = text(
            "SELECT t.npc_id, n.npc_name, SUM(t.total_value) AS loot, SUM(t.drop_count) AS drops "
            "FROM player_npc_hourly_totals t "
            "JOIN npc_list n ON n.npc_id = t.npc_id "
            "WHERE t.`partition` = :partition AND t.player_id = :scope_id "
            "GROUP BY t.npc_id, n.npc_name ORDER BY loot DESC LIMIT :lim"
        )
        params = {"scope_id": player_id, "partition": int(partition), "lim": limit}
    rows = s.execute(sql, params).fetchall()
    return [
        {
            "npc_id": int(npc_id),
            "name": npc_name,
            "loot": money(loot),
            "drops": int(drops or 0),
        }
        for (npc_id, npc_name, loot, drops) in rows
    ]


def _group_records(s, group_id: int, hidden: set, limit: int = 8):
    """The group's fastest kill time per boss and who holds it.

    One row per NPC (the member-held minimum personal best), most recently
    set first, so fresh records surface at the top of the showcase.
    """
    sql = text(
        "SELECT pb.npc_id, n.npc_name, pb.player_id, p.player_name, "
        "       pb.personal_best, pb.team_size, pb.date_added "
        "FROM personal_best pb "
        "JOIN ( "
        "  SELECT pb2.npc_id, MIN(pb2.personal_best) AS best "
        "  FROM personal_best pb2 "
        "  JOIN user_group_association uga2 "
        "    ON uga2.player_id = pb2.player_id AND uga2.group_id = :gid "
        "  WHERE pb2.personal_best > 0 "
        "  GROUP BY pb2.npc_id "
        ") m ON m.npc_id = pb.npc_id AND m.best = pb.personal_best "
        "JOIN user_group_association uga ON uga.player_id = pb.player_id AND uga.group_id = :gid "
        "JOIN npc_list n ON n.npc_id = pb.npc_id "
        "JOIN players p ON p.player_id = pb.player_id "
        "WHERE pb.personal_best > 0 "
        "ORDER BY pb.date_added DESC"
    )
    rows = s.execute(sql, {"gid": group_id}).fetchall()
    out = []
    seen_npcs = set()
    for npc_id, npc_name, pid, pname, best_ms, team_size, date_added in rows:
        if npc_id in seen_npcs or pid in hidden:
            continue
        seen_npcs.add(npc_id)
        try:
            date_ts = int(date_added.timestamp()) if date_added else 0
        except Exception:
            date_ts = 0
        out.append({
            "npc_id": int(npc_id),
            "boss": npc_name,
            "time_ms": int(best_ms),
            "time_display": _convert_from_ms(best_ms),
            "team_size": team_size or "Solo",
            "holder": {"id": int(pid), "name": pname},
            "date_ts": date_ts,
        })
        if len(out) >= limit:
            break
    return out


def _player_personal_bests(s, player_id: int):
    """The player's best time per boss, fastest-improved-most-recently first.

    Returns every boss (the site collapses past the first dozen client-side);
    the per-npc dedupe bounds this at one entry per tracked PB boss (~85)."""
    rows = (
        s.query(PersonalBestEntry)
        .filter(
            PersonalBestEntry.player_id == player_id,
            PersonalBestEntry.personal_best > 0,
        )
        .all()
    )
    best_by_npc = {}
    for pb in rows:
        cur = best_by_npc.get(pb.npc_id)
        if cur is None or pb.personal_best < cur.personal_best:
            best_by_npc[pb.npc_id] = pb
    if not best_by_npc:
        return []
    names = {
        n.npc_id: n.npc_name
        for n in s.query(NpcList).filter(NpcList.npc_id.in_(best_by_npc.keys())).all()
    }
    entries = sorted(
        best_by_npc.values(),
        key=lambda pb: pb.date_added or datetime.min,
        reverse=True,
    )
    out = []
    for pb in entries:
        try:
            date_ts = int(pb.date_added.timestamp()) if pb.date_added else 0
        except Exception:
            date_ts = 0
        out.append({
            # Needed to fetch the loadout this time was set with; the npc/team
            # pair is not unique enough to look a row up by.
            "pb_id": int(pb.id),
            "npc_id": int(pb.npc_id),
            "boss": names.get(pb.npc_id, f"NPC {pb.npc_id}"),
            "time_ms": int(pb.personal_best),
            "time_display": _convert_from_ms(pb.personal_best),
            "team_size": pb.team_size or "Solo",
            "date_ts": date_ts,
        })
    return out


def _account_type_for(s, player_id: int):
    """Wire-string game mode from the player's latest state sync, if any.

    Never raises: a missing state row, an unreadable table or a game mode
    newer than this build all mean "no badge", not a failed profile load.
    """
    try:
        row = (
            s.query(PlayerState.account_type)
            .filter(PlayerState.player_id == player_id)
            .first()
        )
        return account_type_from_varbit(row[0]) if row else None
    except Exception:
        return None


@profiles_bp.get("/players/<int:player_id>")
async def player_profile(player_id: int):
    def _load():
        with db_session() as s:
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if not player:
                return None
            # Privacy: hidden accounts (or accounts of hidden users) are
            # indistinguishable from missing ones on the public profile.
            if bool(player.hidden) or bool(player.user and player.user.hidden):
                return None
            partition = period_to_partition("all")

            group_id_rows = s.execute(
                text("SELECT group_id FROM user_group_association WHERE player_id = :pid"),
                {"pid": player_id},
            ).fetchall()
            groups = []
            for (gid,) in group_id_rows:
                if gid is None or gid <= 2:
                    continue
                g = s.query(Group).filter(Group.group_id == gid).first()
                if g:
                    groups.append({"id": gid, "name": g.group_name})
            # Flair for the player's subscribed groups (one query for all).
            group_flair_map = group_flairs(s, [g["id"] for g in groups])
            for g in groups:
                flair = group_flair_map.get(g["id"])
                if flair:
                    g["flair"] = flair

            recent = (
                s.query(NotifiedSubmission)
                .filter(or_(NotifiedSubmission.pb_id != None,  # noqa: E711
                            NotifiedSubmission.drop_id != None,
                            NotifiedSubmission.clog_id != None))
                .filter(NotifiedSubmission.player_id == player_id)
                .order_by(NotifiedSubmission.date_added.desc())
                .limit(10)
                .all()
            )
            submissions = _build_submissions(recent, s, partition, {player_id: player.player_name})

            loot = player_month_total(player_id, partition)
            rank = player_global_rank(player_id, partition)

            payload = {
                "id": player_id,
                "name": player.player_name,
                "total_loot": money(loot),
                "groups": groups,
                "recent_submissions": submissions,
                # Pretty-URL slug this profile declares as canonical (null when
                # the name collides with another visible player → id url stays).
                "canonical_slug": canonical_slug_for(s, "player", player_id, player.player_name),
            }
            # Supporter flair: user-level premium display perk.
            try:
                if player.user_id:
                    from db.entitlements import resolve_user_entitlements

                    if resolve_user_entitlements(s, player.user_id).get("supporter_flair"):
                        payload["is_supporter"] = True
            except Exception:
                pass
            # Game mode. The plugin reports this two ways and only the
            # state-sync snapshot is actually populated today, so prefer it:
            # it carries the raw varbit and is rewritten on every login, which
            # is what makes a de-ironed account downgrade on its own. The
            # string on the player row is the Task 23 submission path, kept as
            # a fallback for players who submit but have never synced.
            account_type = _account_type_for(s, player_id) or getattr(
                player, "account_type", None
            )
            if account_type:
                payload["account_type"] = account_type
            if rank is not None:
                payload["global_rank"] = rank
            # Month-over-month movement + percentile context for the hero tiles.
            try:
                payload["previous_month_loot"] = money(
                    player_month_total(player_id, _previous_partition(partition))
                )
            except Exception:
                pass
            try:
                conn = _rc()
                if conn is not None:
                    ranked = conn.zcard(leaderboard_key(partition))
                    if ranked:
                        payload["ranked_players"] = int(ranked)
            except Exception:
                pass
            # Boss activity + PB showcase (cached: the analytics scan is the
            # heaviest query on this page).
            try:
                cache_key = f"pstats:bosses:{player_id}:{partition}"
                top_bosses = cache_get(cache_key, _STATS_TTL)
                if top_bosses is None:
                    top_bosses = _top_bosses_sql(s, partition, player_id=player_id)
                    cache_set(cache_key, top_bosses)
                if top_bosses:
                    payload["top_bosses"] = top_bosses
            except Exception:
                pass
            try:
                pbs = _player_personal_bests(s, player_id)
                if pbs:
                    payload["personal_bests"] = pbs
            except Exception:
                pass
            # Character model, when the player has uploaded one. Best-effort:
            # the profile must render for the overwhelming majority who have
            # not, so any failure here just omits the viewer.
            try:
                from db.models import PlayerState
                from services.player_model import model_exists

                state = (
                    s.query(PlayerState)
                    .filter(PlayerState.player_id == player_id)
                    .first()
                )
                if state is not None and state.model_fingerprint:
                    payload["model_fingerprint"] = state.model_fingerprint
                    payload["model_has_pet"] = model_exists(
                        player_id, state.model_fingerprint, pet=True
                    )
            except Exception:
                pass
            points = _player_points(s, player_id)
            if points is not None:
                payload["points"] = points
            top_npc = _player_top_npc(s, player, partition)
            if top_npc:
                payload["top_npc"] = top_npc
            # Best-effort: badge failures must never break profiles.
            try:
                from web_api.routes.badges import player_awards
                payload["badges"] = player_awards(s, player_id)
            except Exception:
                pass
            return payload

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Player not found", f"No player with id {player_id}")
    return with_cache_headers(jsonify(payload), max_age=30)


def _partition_bounds(partition: int) -> tuple[str, str]:
    """[month-start, next-month-start) datetime bounds for a YYYYMM partition.

    Lets a `partition = :p` filter be expressed as a ``date_added`` range so the
    ``(player_id, date_added)`` composite index (`ix_drops_player_id_date_added`)
    does a bounded seek instead of index-merge-intersecting the whole month's
    partition index. `partition` is written as ``YYYYMM(date_added)``, so the
    two are equivalent (verified: 0 mismatches across a 5M-row sample)."""
    year, month = divmod(int(partition), 100)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01 00:00:00", f"{ny:04d}-{nm:02d}-01 00:00:00"


def _parse_loot_partition(raw: str, current: int):
    """Resolve the loot tracker's ``partition`` query param.

    Returns ``(partition, all_time, error)``. ``all`` selects the whole account;
    its ``partition`` is still the current month, which is the newest month the
    payload covers (and what the client falls back to when leaving all-time).
    ``error`` is a human-readable message when the param is unusable.
    """
    if (raw or "").strip().lower() == _ALL_TIME:
        return current, True, None
    try:
        partition = int(raw) if raw else current
    except ValueError:
        return None, False, "partition must be YYYYMM or 'all'"
    year, month = divmod(partition, 100)
    if not (2020 <= year <= 2100 and 1 <= month <= 12) or partition > current:
        return None, False, "partition must be a valid YYYYMM month, not in the future"
    return partition, False, None


def _month_cache_get(key: str):
    """A cached month's NPC boxes, or None. Redis is a nicety here — every miss
    (including a dead connection) just re-reads the month from `drops`."""
    conn = _rc()
    if conn is None:
        return None
    try:
        raw = conn.get(key)
        return json.loads(zlib.decompress(raw)) if raw else None
    except Exception:
        return None


def _month_cache_set(key: str, npc_list, ttl: int) -> None:
    """Store a month's NPC boxes. Compressed: a busy account's month is a few
    hundred KB of very repetitive JSON, and every month of every account that
    opens the all-time view lands here."""
    conn = _rc()
    if conn is None:
        return
    try:
        blob = zlib.compress(json.dumps(npc_list, separators=(",", ":")).encode("utf-8"))
        conn.setex(key, int(ttl), blob)
    except Exception:
        pass


def _is_timeout_error(err: OperationalError) -> bool:
    """MariaDB statement-timeout / lost-connection codes (see `_statement_timeout`)."""
    try:
        return err.orig.args[0] in _TIMEOUT_ERR_CODES
    except (AttributeError, IndexError, TypeError):
        return False


@contextmanager
def _statement_timeout(s, *, enabled: bool, seconds: int = _STATEMENT_TIMEOUT_SECONDS):
    """Bound server-side execution of the statements in the block (all-time only).

    The shared engine sets ``connect_args.read_timeout=30`` (db/models/base.py):
    a query that outruns it dies as an unhandled 500 *and* keeps scanning
    server-side after the client gave up. Capping a few seconds below that makes
    MariaDB abort it first (errno 1969), which the route turns into a clean 503.
    Folding months keeps each statement far under the cap; this is the backstop
    for an account whose single month is pathological. Sessions come from a
    shared pool, so the cap is always reset on exit — a query killed by the
    timeout leaves the connection usable.
    """
    if not enabled:
        yield
        return
    s.execute(text(f"SET SESSION max_statement_time = {int(seconds)}"))
    try:
        yield
    finally:
        try:
            s.execute(text("SET SESSION max_statement_time = 0"))
        except Exception:
            pass


def _earliest_loot_partition(s, player_id: int):
    """First YYYYMM with tracked drops for this player (for the month picker).

    Ordered by ``date_added`` and pinned to ``ix_drops_player_id_date_added``:
    the composite seeks straight to the player and reads the single earliest
    row — O(1). The earlier ``ORDER BY drop_id`` form let the optimiser choose
    the PRIMARY key once a second (player_id, …) index existed, walking drop_id
    order and filtering player_id — tens of millions of rows for any account
    whose drops sit high in the id range (30s+ → the read-timeout 500s in this
    endpoint). `partition` tracks ``YYYYMM(date_added)`` so the earliest-dated
    row carries the earliest partition."""
    row = s.execute(
        text(
            "SELECT `partition` FROM drops FORCE INDEX (ix_drops_player_id_date_added) "
            "WHERE player_id = :pid ORDER BY date_added ASC LIMIT 1"
        ),
        {"pid": player_id},
    ).first()
    return int(row[0]) if row and row[0] else None


def _month_npc_boxes(s, player_id: int, partition: int, current: int):
    """One month of a player's drops as NPC boxes — the unit BOTH loot-tracker
    views are built from (all-time is the fold of every month, see
    `_fold_npc_boxes`).

    Cached in Redis so the two hypercorn workers share the work and it survives
    a restart: a settled month can never change, so it is held for days, while
    the live month gets the same short TTL as the in-process payload cache.
    """
    cache_key = f"{_MONTH_CACHE_PREFIX}{player_id}:{partition}"
    cached = _month_cache_get(cache_key)
    if cached is not None:
        return cached

    # A `partition = :p` equality made the optimiser index-merge-intersect
    # ix_drops_player_id with ix_drops_partition, and the partition side spans
    # the whole month across every player (tens of millions of rows) — 3-4s
    # here, 30s+ (read-timeout 500s) for large accounts. The equivalent
    # ``date_added`` range pins the (player_id, date_added) composite for a
    # single bounded seek.
    start_dt, end_dt = _partition_bounds(partition)
    bounds = {"pid": player_id, "start": start_dt, "end": end_dt}
    item_rows = s.execute(text(
        "SELECT d.npc_id, n.npc_name, d.item_id, i.item_name, "
        "       SUM(d.quantity) AS qty, SUM(d.value * d.quantity) AS loot, "
        "       COUNT(*) AS drop_count, "
        "       MIN(d.date_added) AS first_at, MAX(d.date_added) AS last_at "
        "FROM drops d FORCE INDEX (ix_drops_player_id_date_added) "
        "JOIN npc_list n ON n.npc_id = d.npc_id "
        "JOIN items i ON i.item_id = d.item_id "
        "WHERE d.player_id = :pid AND d.date_added >= :start AND d.date_added < :end "
        "GROUP BY d.npc_id, n.npc_name, d.item_id, i.item_name"
    ), bounds).fetchall()

    # "Kills" the way the old XenForo widget counted them: distinct drop
    # timestamps per NPC (multi-item kills share one timestamp).
    kill_rows = s.execute(text(
        "SELECT d.npc_id, COUNT(DISTINCT d.date_added) "
        "FROM drops d FORCE INDEX (ix_drops_player_id_date_added) "
        "WHERE d.player_id = :pid AND d.date_added >= :start AND d.date_added < :end "
        "  AND d.npc_id IS NOT NULL "
        "GROUP BY d.npc_id"
    ), bounds).fetchall()
    kills = {int(npc_id): int(cnt) for npc_id, cnt in kill_rows}

    def _ts(dt) -> int | None:
        """DB datetime -> unix seconds (None-safe)."""
        try:
            return int(dt.timestamp()) if dt is not None else None
        except Exception:
            return None

    npcs = {}
    for npc_id, npc_name, item_id, item_name, qty, loot, drop_count, first_at, last_at in item_rows:
        npc = npcs.setdefault(int(npc_id), {
            "npc_id": int(npc_id),
            "name": npc_name,
            "kills": kills.get(int(npc_id), 0),
            "total_value": 0,
            "items": [],
        })
        loot = int(loot or 0)
        npc["total_value"] += loot
        item = {
            "item_id": int(item_id),
            "name": item_name,
            "quantity": int(qty or 0),
            "loot": money(loot),
            "drops": int(drop_count or 0),
        }
        # Optional detail for the web item tooltip; omitted when NULL so
        # the payload stays backwards compatible.
        first_ts, last_ts = _ts(first_at), _ts(last_at)
        if first_ts is not None:
            item["first_ts"] = first_ts
        if last_ts is not None:
            item["last_ts"] = last_ts
        npc["items"].append(item)

    npc_list = sorted(npcs.values(), key=lambda x: x["total_value"], reverse=True)
    for npc in npc_list:
        npc["items"].sort(key=lambda x: x["loot"]["value"], reverse=True)
        npc["loot"] = money(npc.pop("total_value"))

    _month_cache_set(
        cache_key,
        npc_list,
        _CURRENT_MONTH_CACHE_TTL if partition >= current else _SETTLED_MONTH_CACHE_TTL,
    )
    return npc_list


def _fold_npc_boxes(months):
    """Merge per-month NPC boxes (oldest first) into one all-time list.

    Kills sum cleanly across months because a kill's distinct ``date_added``
    belongs to exactly one month, so no drop event is ever counted twice.
    """
    npcs: dict[int, dict] = {}
    for npc_list in months:
        for src in npc_list:
            npc = npcs.setdefault(int(src["npc_id"]), {
                "npc_id": int(src["npc_id"]),
                "name": src["name"],
                "kills": 0,
                "total_value": 0,
                "items": {},
            })
            npc["kills"] += int(src.get("kills") or 0)
            npc["total_value"] += int(src["loot"]["value"])
            for item in src["items"]:
                merged = npc["items"].setdefault(int(item["item_id"]), {
                    "item_id": int(item["item_id"]),
                    "name": item["name"],
                    "quantity": 0,
                    "value": 0,
                    "drops": 0,
                })
                merged["quantity"] += int(item.get("quantity") or 0)
                merged["value"] += int(item["loot"]["value"])
                merged["drops"] += int(item.get("drops") or 0)
                first_ts, last_ts = item.get("first_ts"), item.get("last_ts")
                if first_ts is not None:
                    merged["first_ts"] = min(first_ts, merged.get("first_ts", first_ts))
                if last_ts is not None:
                    merged["last_ts"] = max(last_ts, merged.get("last_ts", last_ts))

    out = []
    for npc in sorted(npcs.values(), key=lambda x: x["total_value"], reverse=True):
        items = []
        for item in sorted(npc["items"].values(), key=lambda x: x["value"], reverse=True):
            entry = {
                "item_id": item["item_id"],
                "name": item["name"],
                "quantity": item["quantity"],
                "loot": money(item.pop("value")),
                "drops": item["drops"],
            }
            for key in ("first_ts", "last_ts"):
                if key in item:
                    entry[key] = item[key]
            items.append(entry)
        out.append({
            "npc_id": npc["npc_id"],
            "name": npc["name"],
            "kills": npc["kills"],
            "loot": money(npc["total_value"]),
            "items": items,
        })
    return out


def _months_between(earliest: int, current: int):
    """Every YYYYMM from `earliest` to `current` inclusive, oldest first."""
    months = []
    partition = max(int(earliest), _EARLIEST_SUPPORTED_PARTITION)
    while partition <= current:
        months.append(partition)
        year, month = divmod(partition, 100)
        partition = (year + 1) * 100 + 1 if month >= 12 else partition + 1
    return months


@profiles_bp.get("/players/<int:player_id>/loot")
async def player_loot(player_id: int):
    """RuneLite-style loot tracker: the player's drops grouped by NPC, with item
    stacks — one month by default, or the whole account with ``?partition=all``.

    Both views are built from the same per-month read (`_month_npc_boxes`), a
    bounded ``date_added`` seek. All-time folds every month the account has
    rather than issuing one unbounded lifetime scan: the totals are identical,
    but each statement stays small (the lifetime version measured 15-24s on the
    biggest accounts, close enough to the engine's 30s read timeout to 500), and
    every month it touches lands in a shared cache that the month view and the
    next all-time build both reuse.
    """
    current = period_to_partition("all")
    partition, all_time, err = _parse_loot_partition(request.args.get("partition", ""), current)
    if err:
        return problem(400, "Bad partition", err)

    def _load():
        with db_session() as s:
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if not player:
                return None
            if bool(player.hidden) or bool(player.user and player.user.hidden):
                return None

            # v2: items also carry drop count + first/last received timestamps
            # (rich item tooltips). Key bumped so stale v1 payloads never serve.
            cache_key = f"pstats:loot2:{player_id}:{_ALL_TIME if all_time else partition}"
            cached = cache_get(cache_key, _ALL_TIME_TTL if all_time else _STATS_TTL)
            if cached is not None:
                return cached

            earliest = _earliest_loot_partition(s, player_id) or partition
            with _statement_timeout(s, enabled=all_time):
                if all_time:
                    npc_list = _fold_npc_boxes(
                        _month_npc_boxes(s, player_id, month, current)
                        for month in _months_between(earliest, current)
                    )
                else:
                    npc_list = _month_npc_boxes(s, player_id, partition, current)

            payload = {
                "player_id": player_id,
                "partition": partition,
                "earliest_partition": earliest,
                "all_time": all_time,
                "npcs": npc_list,
            }
            cache_set(cache_key, payload)
            return payload

    try:
        payload = await asyncio.to_thread(_load)
    except OperationalError as err:
        if not _is_timeout_error(err):
            raise
        return problem(
            503,
            "Loot history too large",
            "This account has too much tracked loot to summarise all at once. "
            "Browse it a month at a time instead.",
        )
    if payload is None:
        return problem(404, "Player not found", f"No player with id {player_id}")
    return with_cache_headers(jsonify(payload), max_age=300 if all_time else 60)


@profiles_bp.get("/groups/by-guild/<guild_id>")
async def group_by_guild(guild_id: str):
    """Resolve a Discord guild to its registered group — a light lookup for
    the Discord Activity, which only knows the guild it was launched in.
    Anonymous; 404 when the guild has no group."""
    guild_id = (guild_id or "").strip()
    if not guild_id.isdigit():
        return problem(404, "Group not found", "Invalid guild id")

    def _load():
        with db_session() as s:
            group = s.query(Group).filter(Group.guild_id == guild_id).first()
            if not group:
                return None
            member_count = (
                s.query(Player.player_id).join(Player.groups)
                .filter(Group.group_id == group.group_id).count()
            )
            payload = {
                "id": group.group_id,
                "name": group.group_name,
                "member_count": member_count,
            }
            if group.icon_url:
                payload["icon_url"] = group.icon_url
            return payload

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Group not found", f"No group linked to guild {guild_id}")
    return with_cache_headers(jsonify(payload), max_age=300)


@profiles_bp.get("/groups/<int:group_id>")
async def group_profile(group_id: int):
    def _load():
        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                return None
            partition = period_to_partition("all")

            player_ids = [
                pid for (pid,) in s.query(Player.player_id).join(Player.groups)
                .filter(Group.group_id == group_id).all()
            ]
            member_count = len(player_ids)
            monthly = player_list_loot_sum(player_ids, partition)

            # Global rank via the cached cross-group totals.
            rank = None
            try:
                totals = _compute_group_totals(partition)
                for idx, (gid, _n, _t, _m) in enumerate(totals, start=1):
                    if gid == group_id:
                        rank = idx
                        break
            except Exception:
                rank = None

            hidden = hidden_player_ids()

            # Top members from the per-group monthly board (hidden filtered);
            # the first row doubles as the legacy `top_player` field.
            top_players = []
            top_player = None
            conn = _rc()
            if conn is not None:
                try:
                    top_players = _group_top_players(s, conn, group_id, partition, hidden)
                    if top_players:
                        first = top_players[0]
                        top_player = {
                            "id": first["id"],
                            "name": first["name"],
                            "total_loot": first["loot"],
                        }
                except Exception:
                    top_players = []
                    top_player = None

            # Boss activity + PB records (cached: both scan large tables).
            top_bosses = []
            records = []
            try:
                cache_key = f"gstats:{group_id}:{partition}"
                cached_stats = cache_get(cache_key, _STATS_TTL)
                if cached_stats is None:
                    cached_stats = {
                        "top_bosses": _top_bosses_sql(s, partition, group_id=group_id),
                        "records": _group_records(s, group_id, hidden),
                    }
                    cache_set(cache_key, cached_stats)
                top_bosses = cached_stats["top_bosses"]
                records = cached_stats["records"]
            except Exception:
                pass

            recent = (
                s.query(NotifiedSubmission)
                .filter(or_(NotifiedSubmission.pb_id != None,  # noqa: E711
                            NotifiedSubmission.drop_id != None,
                            NotifiedSubmission.clog_id != None))
                .filter(NotifiedSubmission.group_id == group_id)
                .order_by(NotifiedSubmission.date_added.desc())
                .limit(10)
                .all()
            )
            recent_player_ids = {r.player_id for r in recent if r.player_id}
            player_names = {}
            if recent_player_ids:
                player_names = {
                    p.player_id: p.player_name
                    for p in s.query(Player).filter(Player.player_id.in_(recent_player_ids)).all()
                }
            submissions = _build_submissions(recent, s, partition, player_names)

            payload = {
                "id": group_id,
                "name": group.group_name,
                "member_count": member_count,
                "monthly_loot": money(monthly),
                "recent_submissions": submissions,
                # Pretty-URL slug this group declares as canonical (null when the
                # name collides with another group → id url stays canonical).
                "canonical_slug": canonical_slug_for(s, "group", group_id, group.group_name),
            }
            if group.description:
                payload["description"] = group.description
            if group.icon_url:
                payload["icon_url"] = group.icon_url
            if group.invite_url:
                payload["discord_url"] = group.invite_url
            if rank is not None:
                payload["global_rank"] = rank
            if top_player is not None:
                payload["top_player"] = top_player
            if top_players:
                payload["top_players"] = top_players
            if top_bosses:
                payload["top_bosses"] = top_bosses
            if records:
                payload["records"] = records
            flair = group_flair(s, group_id)
            if flair:
                payload["flair"] = flair
            return payload

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Group not found", f"No group with id {group_id}")
    return with_cache_headers(jsonify(payload), max_age=30)

# NOTE: GET /groups/{id}/members lives in web_api/routes/group_admin.py — it is
# session + group-admin gated and reflects hidden (ignored) players (Task 10).
