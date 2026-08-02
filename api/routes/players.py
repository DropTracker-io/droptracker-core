from datetime import datetime
import asyncio
import heapq
from typing import List, Tuple
import time
import threading
import copy

from quart import Blueprint, jsonify, request
from quart_rate_limiter import rate_limit
from datetime import timedelta
from sqlalchemy import bindparam, or_, text

from api.core import get_db_session, redis_client, redis_tracker
from api.routes.helpers import assemble_submission_data
from utils.format import format_number
from utils import value_overrides
from services.redis_updates import get_player_current_month_total
from data.TOP_NPCS import TOP_NPCS
from db import Player, NotifiedSubmission, NpcList, Group, GroupConfiguration, get_current_partition


players_bp = Blueprint("players", __name__)

# Small in-process cache for hot leaderboard endpoints.
_endpoint_cache = {}
_endpoint_cache_lock = threading.Lock()
TOP_PLAYERS_CACHE_TTL_SECONDS = 20


def _cache_get(cache_key: str, ttl_seconds: int):
    now = time.time()
    with _endpoint_cache_lock:
        entry = _endpoint_cache.get(cache_key)
        if not entry:
            return None
        if (now - entry["ts"]) > ttl_seconds:
            _endpoint_cache.pop(cache_key, None)
            return None
        return copy.deepcopy(entry["value"])


def _cache_set(cache_key: str, value):
    with _endpoint_cache_lock:
        _endpoint_cache[cache_key] = {"ts": time.time(), "value": copy.deepcopy(value)}


def _fetch_top_from_sorted_set(redis_conn, partition: int, limit: int) -> List[Tuple[int, int]]:
    """
    Fetch players directly from the leaderboard sorted set. Returns a list of (player_id, total_loot).
    """
    leaderboard_key = f"leaderboard:{partition}"
    results: List[Tuple[int, int]] = []

    try:
        raw_entries = redis_conn.zrevrange(leaderboard_key, 0, limit - 1, withscores=True)
        for member_raw, score in raw_entries:
            try:
                member_str = member_raw.decode("utf-8") if isinstance(member_raw, (bytes, bytearray)) else str(member_raw)
                player_id = int(member_str)
                total_loot = int(float(score))
            except (ValueError, TypeError, UnicodeDecodeError) as decode_error:
                print(f"Skipping leaderboard entry due to decode error: {decode_error} (value: {member_raw})")
                continue
            if total_loot > 0:
                results.append((player_id, total_loot))
    except Exception as redis_error:
        print(f"Error fetching leaderboard sorted set: {redis_error}")

    return results


def _collect_top_players_from_totals(redis_conn, partition: int, limit: int = 5, scan_count: int = 200,
                                     max_scan_loops: int = 20, max_keys: int = 5000) -> List[Tuple[int, int]]:
    """
    Fallback helper that scans per-player Redis totals for the given partition and returns the top entries.
    Uses a bounded min-heap to keep memory and processing manageable even with large datasets.
    """
    pattern = f"player:*:{partition}:total_loot"
    cursor = 0
    loops = 0
    keys_seen = 0
    heap: List[Tuple[int, int]] = []  # (total_loot, player_id)

    while True:
        loops += 1
        if loops > max_scan_loops:
            print(f"Top players fallback: max scan loops ({max_scan_loops}) reached, stopping early")
            break

        cursor, keys = redis_conn.scan(cursor=cursor, match=pattern, count=scan_count)
        if keys:
            values = redis_conn.mget(keys)
            for key_bytes, value_bytes in zip(keys, values):
                keys_seen += 1
                if keys_seen > max_keys:
                    print(f"Top players fallback: max key budget ({max_keys}) reached, stopping early")
                    cursor = 0
                    break
                if not value_bytes:
                    continue
                try:
                    total_loot = int(float(value_bytes))
                except (TypeError, ValueError):
                    continue
                if total_loot <= 0:
                    continue

                try:
                    key_str = key_bytes.decode("utf-8") if isinstance(key_bytes, (bytes, bytearray)) else str(key_bytes)
                    player_id_str = key_str.split(":")[1]
                    player_id = int(player_id_str)
                except (IndexError, ValueError, UnicodeDecodeError):
                    continue

                heapq.heappush(heap, (total_loot, player_id))
                if len(heap) > limit:
                    heapq.heappop(heap)

        if cursor == 0:
            break

    # Convert heap to sorted list in descending order
    top_entries_desc = sorted(heap, key=lambda item: item[0], reverse=True)
    return [(player_id, total_loot) for total_loot, player_id in top_entries_desc]


@players_bp.get("/player_search")
async def player_search():
    player_name = request.args.get("name", None)
    if not player_name:
        return jsonify({"error": "Player name is required"}), 400

    db_session = get_db_session()
    try:
        player = db_session.query(Player).filter(Player.player_name == player_name).first()
        if not player:
            player = db_session.query(Player).filter(Player.player_name.ilike(f"%{player_name}%")).first()

        if not player:
            return jsonify({"error": f"Player '{player_name}' not found"}), 404

        player_recent_submissions = db_session.query(NotifiedSubmission).filter(or_(NotifiedSubmission.pb_id != None, NotifiedSubmission.drop_id != None, NotifiedSubmission.clog_id != None)).filter(NotifiedSubmission.player_id == player.player_id).order_by(NotifiedSubmission.date_added.desc()).limit(10).all()
        final_submission_data = await assemble_submission_data(player_recent_submissions, db_session)
        player_loot = get_player_current_month_total(player.player_id)
        player_rank = redis_tracker.get_player_rank(player.player_id)
        if player_rank is not None:
            player_rank, total = player_rank[0], player_rank[1]
            player_rank += 1
        player_npc_ranks = {}
        target_npcs = db_session.query(NpcList).filter(NpcList.npc_id.in_(TOP_NPCS)).all()
        for npc in target_npcs:
            player_npc_rank, player_npc_score = player.get_score_at_npc(npc.npc_id)
            player_npc_ranks[npc.npc_name] = {"rank": player_npc_rank, "loot": format_number(player_npc_score)}
        if len(player_npc_ranks) == 0:
            top_npc_name = "Unknown"
            top_npc_data = {"rank": 0, "loot": 0}
            top_npc_data["name"] = top_npc_name
        else:
            top_npc_name = max(player_npc_ranks, key=lambda x: player_npc_ranks[x]["loot"]) 
            top_npc_data = player_npc_ranks[top_npc_name].copy()
            top_npc_data["name"] = top_npc_name
        player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
        player_group_ids_result = db_session.execute(text(player_group_id_query), {"player_id": player.player_id}).fetchall()
        player_group_ids = [g[0] for g in player_group_ids_result if g[0] > 2]
        player_groups = []
        if player_group_ids:
            # Batched: group names in one query, member counts in one grouped
            # query, monthly totals from the precomputed group leaderboard in
            # one pipelined pass. This used to recompute each group's total
            # from every member's per-player Redis key.
            name_map = dict(
                db_session.query(Group.group_id, Group.group_name)
                .filter(Group.group_id.in_(player_group_ids))
                .all()
            )
            member_counts = dict(
                db_session.execute(
                    text("SELECT group_id, COUNT(player_id) FROM user_group_association "
                         "WHERE group_id IN :gids GROUP BY group_id")
                    .bindparams(bindparam("gids", expanding=True)),
                    {"gids": player_group_ids},
                ).fetchall()
            )
            partition = get_current_partition()
            group_totals = {}
            try:
                pipe = redis_client.client.pipeline(transaction=False)
                for gid in player_group_ids:
                    pipe.zscore(f"gleaderboard:{partition}", gid)
                for gid, score in zip(player_group_ids, pipe.execute()):
                    group_totals[gid] = int(float(score)) if score is not None else 0
            except Exception as e:
                print(f"player_search: failed reading group totals: {e}")
            for gid in player_group_ids:
                if gid not in name_map:
                    continue
                player_groups.append({
                    "name": name_map[gid],
                    "id": gid,
                    "_loot_raw": group_totals.get(gid, 0),
                    "loot": format_number(group_totals.get(gid, 0)),
                    "members": int(member_counts.get(gid, 0)),
                })
            # Sort numerically (sorting the formatted strings put "999M" above "1.2B").
            player_groups.sort(key=lambda x: x["_loot_raw"], reverse=True)
            for pg in player_groups:
                pg.pop("_loot_raw", None)
        from services.points import get_player_lifetime_points_earned
        player_lifetime_points = get_player_lifetime_points_earned(player_id=player.player_id,session=db_session)

        response_data = {
            "player_name": player.player_name,
            "droptracker_player_id": player.player_id,
            "registered": player.user_id is not None,
            "total_loot": format_number(player_loot),
            "global_rank": player_rank,
            "top_npc": top_npc_data,
            "points": player_lifetime_points,
            "groups": player_groups,
            "recent_submissions": final_submission_data,
        }

        return jsonify(response_data), 200
    finally:
        db_session.close()


async def get_top_players_payload():
    """Cached /top_players payload; also reused by the /panel_data aggregate."""
    try:
        limit = 5
        partition = get_current_partition()
        cache_key = f"top_players:{partition}:{limit}"
        cached_payload = _cache_get(cache_key, TOP_PLAYERS_CACHE_TTL_SECONDS)
        if cached_payload is not None:
            return cached_payload
        redis_conn = getattr(redis_client, "client", None)
        top_entries: List[Tuple[int, int]] = []

        if redis_conn is not None:
            top_entries = await asyncio.to_thread(_fetch_top_from_sorted_set, redis_conn, partition, limit)

        if not top_entries:
            print("Top players: sorted-set empty, falling back to per-player totals scan")
            if redis_conn is None:
                print("Top players fallback: Redis connection unavailable")
                top_entries = []
            else:
                top_entries = await asyncio.to_thread(
                    _collect_top_players_from_totals,
                    redis_conn,
                    partition,
                    limit,
                )

        if not top_entries:
            return {"players": []}

        db_session = get_db_session()
        try:
            top_player_ids = [player_id for player_id, _ in top_entries]
            players = (
                db_session.query(Player.player_id, Player.player_name)
                .filter(Player.player_id.in_(top_player_ids))
                .all()
            )
        finally:
            db_session.close()

        player_name_map = {player_id: player_name for player_id, player_name in players}

        top_players_data = []
        for index, (player_id, total_loot) in enumerate(top_entries, start=1):
            player_name = player_name_map.get(player_id, f"Player {player_id}")
            top_players_data.append({
                "rank": index,
                "player_name": player_name,
                "total_loot": format_number(total_loot)
            })
        payload = {"players": top_players_data}
        _cache_set(cache_key, payload)
        return payload
    except Exception as e:
        print(f"Exception in top_players: {e}")
        return {"players": []}


@players_bp.get("/top_players")
async def top_players():
    resp = jsonify(await get_top_players_payload())
    resp.headers["Cache-Control"] = "public, max-age=15"
    return resp, 200


@players_bp.get("/player")
@rate_limit(limit=5, period=timedelta(seconds=10))
async def get_player():
    player_name = request.args.get("player_name") or request.args.get("name")
    player_id = request.args.get("id")
    if not player_name and not player_id:
        return jsonify({"error": "Player name or id is required"}), 400

    db_session = get_db_session()
    try:
        query = db_session.query(Player)
        if player_id:
            try:
                query = query.filter(Player.player_id == int(player_id))
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid player id"}), 400
        else:
            query = query.filter(Player.player_name == player_name)
        player = query.first()
        # Hidden players are excluded from public lookups (privacy setting).
        if not player or player.hidden:
            return jsonify({"error": "Player not found"}), 404
        return jsonify({"player": {
            "player_id": player.player_id,
            "player_name": player.player_name,
            "wom_id": player.wom_id,
            "total_level": player.total_level,
            "log_slots": player.log_slots,
            "date_added": player.date_added.isoformat() if player.date_added else None,
            "date_updated": player.date_updated.isoformat() if player.date_updated else None,
        }})
    finally:
        db_session.close()


@players_bp.get("/value_mods")
async def item_value_modifications():
    """Item ids the plugin should ask the server to re-value after submission.

    Derived from the item_value_overrides table so it never drifts from the
    valuation rules themselves. Falls back to the legacy hard-coded list only
    during the deploy window before the table is seeded
    (see scripts/seed_item_value_overrides.py)."""
    # Pre-table id set — kept solely as a fallback until the overrides are seeded.
    legacy = [
        "22968", "22970", "22972", "22974",
        "13274", "13275", "13276", "18633", "18634", "18635",
        "28279", "28281", "28283", "28285",
        "29790", "29792", "29794", "29799", "31109",
    ]
    try:
        overrides = await asyncio.to_thread(value_overrides.all_active)
        ids = sorted({int(o["item_id"]) for o in overrides if o.get("item_id") is not None})
        if ids:
            return [str(i) for i in ids]
    except Exception:
        pass
    return legacy

def _group_configs_for(player_name, acc_hash, db_session):
    """Build the /load_config group-config list for a player, or None when the
    player is unknown. Shared by /load_config and /panel_data."""
    # Canonical identity lookup by account hash first.
    player = db_session.query(Player).filter(Player.account_hash == acc_hash).first()
    if not player:
        # Fallback for older rows/clients that still rely on strict name+hash matching.
        player = (
            db_session.query(Player)
            .filter(Player.player_name == player_name, Player.account_hash == acc_hash)
            .first()
        )
    if not player:
        return None
    # NO RENAME HERE. This is an unauthenticated GET whose player_name comes
    # straight off the query string, so writing it onto the row let anyone
    # holding a valid acc_hash (their own) relabel their player as any RSN they
    # liked — and it stuck, because the submission fast path
    # (ensure_player_and_auth) skips WOM precisely when the hash hits and the
    # stored name already equals the submitted one. The impostor row then shows
    # under the victim's name on leaderboards, profiles and drop embeds.
    #
    # A genuine RSN change needs no help from this endpoint: the names differ on
    # the next submission, which drops out of the fast path into the
    # WOM-authoritative lookup and renames the row to WOM's canonical spelling.
    player_gids = db_session.execute(text("SELECT group_id FROM user_group_association WHERE player_id = :player_id"), {"player_id": player.player_id}).all()

    # Events v2: advertise whether an active event with XP-based tasks
    # (xp_target / skill_target) is tracking this player, so the plugin
    # submits periodic experience snapshots instead of only level-ups.
    xp_events_active = False
    try:
        xp_event_row = db_session.execute(
            text(
                "SELECT 1 FROM web_event_team_members m "
                "JOIN web_event_teams t ON t.id = m.team_id "
                "JOIN web_events e ON e.id = t.event_id "
                "JOIN web_event_tasks k ON k.event_id = e.id "
                "WHERE m.player_id = :player_id AND e.status = 'active' "
                "AND k.type IN ('xp_target', 'skill_target') LIMIT 1"
            ),
            {"player_id": player.player_id},
        ).first()
        xp_events_active = xp_event_row is not None
    except Exception as e:
        print(f"Exception checking active xp events in load_config: {e}")

    # Whether ANY live event is tracking this player (not just XP tasks): the
    # plugin's signal to start polling GET /notifications for in-game event
    # notifications (docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md).
    event_active = False
    try:
        from services.plugin_notifications import player_has_active_event
        event_active = player_has_active_event(db_session, player.player_id)
    except Exception as e:
        print(f"Exception checking active events in load_config: {e}")

    group_configs = []
    def get_config_value(current_group_configs, key: str):
        for group_config in current_group_configs:
            if group_config.config_key == key:
                if key == "level_minimum_for_notifications":
                    return group_config.config_value
                config_val = group_config.config_value if group_config.config_value and group_config.config_value != "" else group_config.long_value
                if config_val == "true" or config_val == "1":
                    return True
                elif config_val == "false" or config_val == "0":
                    return False
                elif config_val == "":
                    return None
                return config_val
        return ""
    for group_id_row in player_gids:
        group_id = group_id_row[0]
        group = db_session.query(Group).filter(Group.group_id == group_id).first()
        current_group_configs = db_session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id).all()
        group_configs.append({"group_id": group_id,
                            "group_name": group.group_name,
                            "min_value": get_config_value(current_group_configs, "minimum_value_to_notify"),
                            "minimum_drop_value": get_config_value(current_group_configs, "minimum_value_to_notify"),
                            "only_screenshots": get_config_value(current_group_configs, "only_send_messages_with_images"),
                            "send_drops": True,
                            "send_pbs": get_config_value(current_group_configs, "notify_pbs"),
                            "send_clogs": get_config_value(current_group_configs, "notify_clogs"),
                            "send_cas": get_config_value(current_group_configs, "notify_cas"),
                            "send_pets": get_config_value(current_group_configs, "send_pets"),
                            "send_deaths": get_config_value(current_group_configs, "notify_deaths"),
                            "send_diaries": get_config_value(current_group_configs, "notify_diaries"),
                            "send_xp": get_config_value(current_group_configs, "notify_levels"),
                            "minimum_level": get_config_value(current_group_configs, "level_minimum_for_notifications"),
                            "send_stacked_items": get_config_value(current_group_configs, "send_stacks_of_items"),
                            "minimum_ca_tier": get_config_value(current_group_configs, "min_ca_tier_to_notify"),
                            "track_xp_events": xp_events_active,
                            "active_event": event_active})
    return group_configs


@players_bp.get("/load_config")
async def load_config():
    player_name = request.args.get("player_name", None)
    acc_hash = request.args.get("acc_hash", None)
    if not player_name or not acc_hash:
        return jsonify({"error": "Player name and acc_hash are required"}), 400
    db_session = get_db_session()
    try:
        group_configs = _group_configs_for(player_name, acc_hash, db_session)
    finally:
        db_session.close()
    if group_configs is None:
        return jsonify({"error": "Player not found"}), 404
    return jsonify(group_configs), 200


# Hardcoded fallbacks when no GroupConfiguration override exists (group_id=2).
PLUGIN_LATEST_VERSION_FALLBACK = "5.4.0"
PLUGIN_MINIMUM_VERSION_FALLBACK = "5.0.0"


def _plugin_version_payload():
    """Plugin version payload for /plugin_version and /panel_data.

    Values are sourced from GroupConfiguration rows on the global group
    (group_id=2) with config keys 'plugin_latest_version' /
    'plugin_minimum_version' / 'plugin_version_message', falling back to
    hardcoded defaults when unset.
    """
    latest_version = PLUGIN_LATEST_VERSION_FALLBACK
    minimum_version = PLUGIN_MINIMUM_VERSION_FALLBACK
    message = None
    db_session = get_db_session()
    try:
        rows = (
            db_session.query(GroupConfiguration)
            .filter(
                GroupConfiguration.group_id == 2,
                GroupConfiguration.config_key.in_(
                    ["plugin_latest_version", "plugin_minimum_version", "plugin_version_message"]
                ),
            )
            .all()
        )
        for row in rows:
            value = (row.config_value or "").strip()
            if not value:
                continue
            if row.config_key == "plugin_latest_version":
                latest_version = value
            elif row.config_key == "plugin_minimum_version":
                minimum_version = value
            elif row.config_key == "plugin_version_message":
                message = value
    except Exception as e:
        print(f"Exception in plugin_version: {e}")
    finally:
        db_session.close()

    payload = {"latest_version": latest_version, "minimum_version": minimum_version}
    if message:
        payload["message"] = message
    return payload


@players_bp.get("/plugin_version")
async def plugin_version():
    """Plugin version check for the RuneLite plugin (no auth required)."""
    return jsonify(_plugin_version_payload()), 200


@players_bp.get("/panel_data")
async def panel_data():
    """One-round-trip aggregate for the plugin side panel boot.

    Combines /load_config, /top_players, /top_groups, /plugin_version and the
    welcome/news strings so the panel needs a single request instead of six.
    `player_name`/`acc_hash` are optional: without them (or for an unknown
    player) `configs` is null and everything else still loads.
    """
    from api.routes.groups import get_top_groups_payload

    player_name = request.args.get("player_name", None)
    acc_hash = request.args.get("acc_hash", None)

    configs = None
    if player_name and acc_hash:
        def _load_configs():
            db_session = get_db_session()
            try:
                return _group_configs_for(player_name, acc_hash, db_session)
            finally:
                db_session.close()
        try:
            configs = await asyncio.to_thread(_load_configs)
        except Exception as e:
            print(f"panel_data: config load failed: {e}")

    try:
        top_groups_payload = await get_top_groups_payload()
    except Exception as e:
        print(f"panel_data: top_groups failed: {e}")
        top_groups_payload = {"groups": []}

    try:
        top_players_payload = await get_top_players_payload()
    except Exception as e:
        print(f"panel_data: top_players failed: {e}")
        top_players_payload = {"players": []}

    try:
        version_payload = await asyncio.to_thread(_plugin_version_payload)
    except Exception as e:
        print(f"panel_data: version failed: {e}")
        version_payload = {
            "latest_version": PLUGIN_LATEST_VERSION_FALLBACK,
            "minimum_version": PLUGIN_MINIMUM_VERSION_FALLBACK,
        }

    from api.worker import get_latest_welcome_message, get_latest_news
    try:
        # The handlers return (body, status, headers) tuples; only the body matters here.
        welcome = (await get_latest_welcome_message())[0]
        news = (await get_latest_news())[0]
    except Exception:
        welcome, news = "Welcome to the DropTracker!", ""

    return jsonify({
        "configs": configs,
        "player_found": configs is not None,
        "top_groups": top_groups_payload,
        "top_players": top_players_payload,
        "welcome": welcome,
        "news": news,
        "version": version_payload,
    }), 200


