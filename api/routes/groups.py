from datetime import datetime, timedelta
from typing import List
import asyncio
import hmac
import time
import threading
import copy

from quart import Blueprint, jsonify, request
from quart_cors import route_cors
from quart_rate_limiter import rate_limit
from sqlalchemy import func, or_, text

from api.core import get_db_session, redis_client
from api.routes.group_create import require_service_key
from api.routes.helpers import assemble_submission_data
from lootboard import generator
from services.redis_updates import get_player_list_loot_sum
from utils.discord_urls import public_discord_url
from utils.format import format_number
from db import Player, Group, GroupConfiguration, NotifiedSubmission, NpcList, get_current_partition
from db.ops import sync_group_from_wom_with_stats


groups_bp = Blueprint("groups", __name__)

# Small in-process cache for hot group leaderboard endpoint.
_groups_endpoint_cache = {}
_groups_endpoint_cache_lock = threading.Lock()
TOP_GROUPS_CACHE_TTL_SECONDS = 20


def _groups_cache_get(cache_key: str, ttl_seconds: int):
    now = time.time()
    with _groups_endpoint_cache_lock:
        entry = _groups_endpoint_cache.get(cache_key)
        if not entry:
            return None
        if (now - entry["ts"]) > ttl_seconds:
            _groups_endpoint_cache.pop(cache_key, None)
            return None
        return copy.deepcopy(entry["value"])


def _groups_cache_set(cache_key: str, value):
    with _groups_endpoint_cache_lock:
        _groups_endpoint_cache[cache_key] = {"ts": time.time(), "value": copy.deepcopy(value)}


def _decode_redis_int(raw):
    try:
        return int(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else int(raw)
    except (TypeError, ValueError):
        return None


def _compute_group_totals_slow(db_session):
    """Legacy O(groups × members) recompute, kept only as a fallback for the
    brief window after a month rollover before the lootboard generator has
    repopulated `gleaderboard:{partition}`."""
    group_totals = {}
    groups = db_session.query(Group).all()
    for group_object in groups:
        group_id = group_object.group_id
        if group_id in (0, 2):
            continue
        players_in_group = db_session.query(Player.player_id).join(Player.groups).filter(Group.group_id == group_id).all()
        try:
            group_totals[group_id] = get_player_list_loot_sum([player.player_id for player in players_in_group])
        except Exception as e:
            print(f"Error getting group total for group {group_id}: {e}")
            group_totals[group_id] = 0
    return sorted(group_totals.items(), key=lambda x: x[1], reverse=True)


def _build_top_groups_payload(partition):
    """Build the /top_groups payload from the precomputed group-total sorted
    set (`gleaderboard:{partition}`, maintained every ~2 min by the lootboard
    generator) plus three batched DB queries and one pipelined Redis pass —
    instead of the old per-group/per-player recompute."""
    totals = []
    try:
        raw = redis_client.client.zrevrange(f"gleaderboard:{partition}", 0, -1, withscores=True)
        for member_raw, score in raw:
            gid = _decode_redis_int(member_raw)
            if gid is None or gid in (0, 2):
                continue
            totals.append((gid, int(float(score))))
    except Exception as e:
        print(f"top_groups: failed reading gleaderboard:{partition}: {e}")

    db_session = get_db_session()
    try:
        if not totals:
            totals = _compute_group_totals_slow(db_session)
        if not totals:
            return {"groups": []}

        gids = [gid for gid, _ in totals]

        name_map = dict(
            db_session.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(gids))
            .all()
        )
        member_counts = dict(
            db_session.query(Group.group_id, func.count(Player.player_id))
            .select_from(Player)
            .join(Player.groups)
            .filter(Group.group_id.in_(gids))
            .group_by(Group.group_id)
            .all()
        )

        # One pipelined round-trip for every group's #1 player this month.
        top_pid_by_gid = {}
        try:
            pipe = redis_client.client.pipeline(transaction=False)
            for gid in gids:
                pipe.zrevrange(f"leaderboard:{partition}:group:{gid}", 0, 0)
            for gid, result in zip(gids, pipe.execute()):
                if result:
                    pid = _decode_redis_int(result[0])
                    if pid is not None:
                        top_pid_by_gid[gid] = pid
        except Exception as e:
            print(f"top_groups: failed reading per-group top players: {e}")

        player_name_map = {}
        if top_pid_by_gid:
            player_name_map = dict(
                db_session.query(Player.player_id, Player.player_name)
                .filter(Player.player_id.in_(set(top_pid_by_gid.values())))
                .all()
            )

        final_groups = []
        rank = 0
        for gid, group_total in totals:
            group_name = name_map.get(gid)
            if group_name is None:
                # Deleted group still lingering in the sorted set — skip it.
                continue
            rank += 1
            top_player_display = player_name_map.get(top_pid_by_gid.get(gid))
            final_groups.append({
                "group_name": group_name,
                "total_loot": format_number(group_total),
                "rank": rank,
                "group_id": gid,
                "member_count": int(member_counts.get(gid, 0)),
                "top_player": top_player_display,
                # The plugin's TopGroupResult deserializes "top_member".
                "top_member": top_player_display,
            })
        return {"groups": final_groups}
    finally:
        db_session.close()


async def get_top_groups_payload():
    """Cached /top_groups payload; also reused by the /panel_data aggregate."""
    partition = get_current_partition()
    cache_key = f"top_groups:{partition}"
    cached_payload = _groups_cache_get(cache_key, TOP_GROUPS_CACHE_TTL_SECONDS)
    if cached_payload is not None:
        return cached_payload

    payload = await asyncio.to_thread(_build_top_groups_payload, partition)
    _groups_cache_set(cache_key, payload)
    return payload


@groups_bp.get("/top_groups")
async def top_groups():
    resp = jsonify(await get_top_groups_payload())
    resp.headers["Cache-Control"] = "public, max-age=15"
    return resp, 200


@groups_bp.get("/group_search")
@rate_limit(limit=10, period=timedelta(seconds=60))
async def group_search():
    group_name = request.args.get("name", None)
    if not group_name:
        return jsonify({"error": "Group name is required"}), 400

    partition = get_current_partition()
    db_session = get_db_session()
    try:
        group: Group = db_session.query(Group).filter(Group.group_name == group_name).first()
        if not group:
            group = db_session.query(Group).filter(Group.group_name.ilike(f"%{group_name}%")).first()
        if not group or group.group_id in (0, 2):
            return jsonify({"error": "Group " + group_name + " not found"}), 404

        # Monthly total + rank come from the precomputed group leaderboard
        # (maintained by the lootboard generator). This used to trigger a live
        # WOM member fetch plus a per-member Redis loot sum on every search.
        group_total = 0
        group_rank = None
        total_groups = 0
        try:
            raw = redis_client.client.zrevrange(f"gleaderboard:{partition}", 0, -1, withscores=True)
            standings = []
            for member_raw, score in raw:
                gid = _decode_redis_int(member_raw)
                if gid is None or gid in (0, 2):
                    continue
                standings.append((gid, int(float(score))))
            total_groups = len(standings)
            for idx, (gid, score) in enumerate(standings, start=1):
                if gid == group.group_id:
                    group_rank = idx
                    group_total = score
                    break
        except Exception as e:
            print(f"group_search: failed reading gleaderboard:{partition}: {e}")
        if group_rank is None:
            # Not on the board yet (new/empty group): rank it last.
            total_groups += 1
            group_rank = total_groups

        top_player_data = redis_client.client.zrevrange(
            f"leaderboard:{partition}:group:{group.group_id}",
            0,
            0,
            withscores=True
        )

        top_player_display = None
        if top_player_data:
            player_id_raw, player_score = top_player_data[0]
            player_id_int = _decode_redis_int(player_id_raw)
            if player_id_int is not None:
                top_player = db_session.query(Player).filter(Player.player_id == player_id_int).first()
                if top_player:
                    top_player_display = f"{top_player.player_name}"

        player_count = group.get_player_count(session_to_use=db_session)
        group_recent_submissions = db_session.query(NotifiedSubmission).filter(or_(NotifiedSubmission.pb_id != None, NotifiedSubmission.drop_id != None, NotifiedSubmission.clog_id != None)).filter(NotifiedSubmission.group_id == group.group_id).order_by(NotifiedSubmission.date_added.desc()).limit(10).all()
        final_submission_data = await assemble_submission_data(group_recent_submissions, db_session)
        # The plugin can only load images from our own host, so a Discord-hosted
        # icon has to be mirrored locally before we can hand out a path for it.
        # Scheduled, not awaited: this endpoint serves every plugin version and
        # must not wait on a download. See utils/group_icon.py.
        from utils.group_icon import (
            discord_invite_code,
            icon_relative_path,
            schedule_group_icon_mirror,
        )
        schedule_group_icon_mirror(group.group_id, group.icon_url)
        return jsonify({
            "group_name": group.group_name,
            "group_description": group.description,
            "group_image_url": group.icon_url,
            "group_image_path": icon_relative_path(group.group_id, group.icon_url),
            "public_discord_link": public_discord_url(group.invite_url),
            "discord_invite_code": discord_invite_code(group.invite_url),
            "group_droptracker_id": group.group_id,
            "group_members": player_count,
            "group_rank": f"{group_rank}/{total_groups}",
            "group_top_player": top_player_display,
            "group_recent_submissions": final_submission_data,
            "group_stats": {
                "total_members": player_count,
                "global_rank": f"{group_rank}/{total_groups}",
                "monthly_loot": format_number(group_total),
            },
        })
    finally:
        db_session.close()

@groups_bp.get("/groups/custom_board/<int:group_id>")
@route_cors(allow_origin="https://www.droptracker.io")
async def group_custom_board(group_id: int):
    db_session = get_db_session()
    try:
        group = db_session.query(Group).filter(Group.group_id == group_id).first()
        if not group:
            return jsonify({"error": "Group not found"}), 404
        custom_board = await generator.generate_custom_board(group_id=group_id)
        return jsonify({"image_path": custom_board}), 200
    finally:
        db_session.close()

@groups_bp.get("/groups/board_update/<int:group_id>")
@groups_bp.post("/groups/board_update/<int:group_id>")
@route_cors(allow_origin="https://www.droptracker.io")
@rate_limit(limit=5, period=timedelta(seconds=60))
async def group_board_update(group_id: int):
    try:
        force_raw = request.args.get("force", "false").lower()
        force = force_raw in ("1", "true", "yes", "y", "on")

        import sys
        from asyncio.subprocess import PIPE

        cmd = [sys.executable, "/store/droptracker/disc/board_cli.py", str(group_id)]
        if force:
            cmd.append("--force")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=PIPE,
            stderr=PIPE
        )
        stdout, stderr = await proc.communicate()
        status_code = 200 if proc.returncode == 0 else 500

        return jsonify({
            "message": "Board update completed" if proc.returncode == 0 else "Board update failed",
            "group_id": group_id,
            "force": force,
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="ignore"),
            "stderr": stderr.decode(errors="ignore"),
        }), status_code
    except Exception as e:
        return jsonify({"error": f"Failed to run board update: {e}"}), 500


@groups_bp.get("/groups/admin_diagnostics/<int:group_id>")
@route_cors(allow_origin="https://www.droptracker.io")
@require_service_key
async def group_admin_diagnostics(group_id: int):
    db_session = get_db_session()
    try:
        group = db_session.query(Group).filter(Group.group_id == group_id).first()
        if not group:
            return jsonify({"success": False, "error": "Group not found"}), 404

        # Pipeline heartbeat checks mirror existing website assumptions.
        last_webhook_drop = db_session.execute(
            text("SELECT MAX(date_added) FROM drops WHERE used_api = 0")
        ).scalar()
        last_api_drop = db_session.execute(
            text("SELECT MAX(date_added) FROM drops WHERE used_api = 1")
        ).scalar()
        last_group_submission = db_session.query(NotifiedSubmission.date_added).filter(
            NotifiedSubmission.group_id == group_id
        ).order_by(NotifiedSubmission.date_added.desc()).first()

        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        recent_submission_count = db_session.query(NotifiedSubmission).filter(
            NotifiedSubmission.group_id == group_id,
            NotifiedSubmission.date_added >= seven_days_ago
        ).count()

        return jsonify({
            "success": True,
            "group_id": group_id,
            "pipeline": {
                "webhook_bot": {
                    "online": bool(last_webhook_drop),
                    "last_seen": str(last_webhook_drop) if last_webhook_drop else None,
                },
                "api_pipeline": {
                    "online": bool(last_api_drop),
                    "last_seen": str(last_api_drop) if last_api_drop else None,
                },
            },
            "group_activity": {
                "recent_submission_count_7d": int(recent_submission_count),
                "last_submission": str(last_group_submission[0]) if last_group_submission else None,
            }
        }), 200
    finally:
        db_session.close()


# ==============================================================================
# TIMEFRAME BOARD GENERATOR ENDPOINT
# ==============================================================================
# This endpoint generates a custom loot board for a specific timeframe.
# Called from the PHP front-end's board generator page.
#
# EXPECTED REQUEST FORMAT (JSON POST body):
# {
#     "group_id": int,              // Required. DropTracker group ID.
#     "wom_group_id": int|null,     // Optional. Wise Old Man group ID.
#     "start_time": string|null,    // Optional. ISO datetime (e.g., "2024-01-15T00:00:00").
#     "end_time": string|null,      // Optional. ISO datetime (e.g., "2024-01-15T23:59:59").
#     "npc_id": int|null,           // Optional. Filter drops to a specific NPC.
# }
#
# RESPONSE FORMAT:
# Success: { "success": true, "image_url": string, "message": string }
# Error:   { "success": false, "error": string }
# ==============================================================================

@groups_bp.post("/generate-timeframe-board")
@route_cors(allow_origin="https://www.droptracker.io")
@rate_limit(limit=5, period=timedelta(seconds=60))
async def generate_timeframe_board_endpoint():
    """
    Generate a custom loot board for a specific timeframe.
    
    Accepts parameters for group, timeframe, and optional NPC filter.
    Returns the URL to the generated board image.
    
    Uses a subprocess to avoid blocking the main event loop during
    CPU-intensive image generation.
    """
    import sys
    import json
    from asyncio.subprocess import PIPE
    
    try:
        # Parse JSON request body
        try:
            data = await request.get_json()
            if not data:
                return jsonify({"success": False, "error": "No JSON data provided"}), 400
        except Exception as e:
            return jsonify({"success": False, "error": f"Invalid JSON data: {str(e)}"}), 400
        
        # Extract and validate required fields
        group_id = data.get("group_id")
        if group_id is None:
            return jsonify({"success": False, "error": "Missing required field: group_id"}), 400
        
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "group_id must be an integer"}), 400
        
        # Validate that the group exists
        db_session = get_db_session()
        try:
            group = db_session.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                return jsonify({"success": False, "error": f"Group with ID {group_id} not found"}), 404
        finally:
            db_session.close()
        
        # Extract optional fields
        wom_group_id = data.get("wom_group_id")
        if wom_group_id is not None:
            try:
                wom_group_id = int(wom_group_id)
            except (ValueError, TypeError):
                wom_group_id = 0
        else:
            wom_group_id = 0
        
        # Validate and normalize start_time
        start_time_str = data.get("start_time")
        if start_time_str:
            start_time_str = str(start_time_str).strip()
            # Remove timezone info if present
            if '+' in start_time_str:
                start_time_str = start_time_str.split('+')[0]
            if 'Z' in start_time_str:
                start_time_str = start_time_str.replace('Z', '')
            
            # Validate format
            parsed = False
            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    datetime.strptime(start_time_str, fmt)
                    parsed = True
                    break
                except ValueError:
                    continue
            
            if not parsed:
                return jsonify({
                    "success": False, 
                    "error": f"Invalid start_time format: {start_time_str}. Use ISO format (e.g., 2024-01-15T00:00:00)"
                }), 400
        
        # Validate and normalize end_time
        end_time_str = data.get("end_time")
        if end_time_str:
            end_time_str = str(end_time_str).strip()
            if '+' in end_time_str:
                end_time_str = end_time_str.split('+')[0]
            if 'Z' in end_time_str:
                end_time_str = end_time_str.replace('Z', '')
            
            parsed = False
            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    datetime.strptime(end_time_str, fmt)
                    parsed = True
                    break
                except ValueError:
                    continue
            
            if not parsed:
                return jsonify({
                    "success": False, 
                    "error": f"Invalid end_time format: {end_time_str}. Use ISO format (e.g., 2024-01-15T23:59:59)"
                }), 400
        
        # Parse npc_id if provided
        npc_id = data.get("npc_id")
        if npc_id is not None:
            try:
                npc_id = int(npc_id)
                if npc_id <= 0:
                    npc_id = None
            except (ValueError, TypeError):
                npc_id = None
        
        print(f"[TimeframeBoardAPI] Spawning subprocess for group_id={group_id}, "
              f"wom_group_id={wom_group_id}, start={start_time_str}, end={end_time_str}, npc_id={npc_id}")
        
        # Build command arguments for the subprocess
        cmd = [
            sys.executable,
            "/store/droptracker/disc/timeframe_board_cli.py",
            "--group-id", str(group_id),
        ]
        
        if wom_group_id and wom_group_id > 0:
            cmd.extend(["--wom-group-id", str(wom_group_id)])
        if start_time_str:
            cmd.extend(["--start-time", start_time_str])
        if end_time_str:
            cmd.extend(["--end-time", end_time_str])
        if npc_id:
            cmd.extend(["--npc-id", str(npc_id)])
        
        # Run the board generation in a subprocess to avoid blocking the event loop
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=PIPE,
            stderr=PIPE
        )
        
        # Wait for completion with a timeout
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return jsonify({
                "success": False,
                "error": "Board generation timed out after 120 seconds"
            }), 500
        
        stdout_str = stdout.decode(errors="ignore").strip()
        stderr_str = stderr.decode(errors="ignore").strip()
        
        if proc.returncode != 0:
            print(f"[TimeframeBoardAPI] Subprocess failed with code {proc.returncode}")
            print(f"[TimeframeBoardAPI] stderr: {stderr_str}")
            error_msg = stderr_str if stderr_str else f"Board generation failed with exit code {proc.returncode}"
            return jsonify({
                "success": False,
                "error": error_msg
            }), 500
        
        # The CLI script outputs the image path on stdout (last line)
        # There may be logging/debug print statements before it
        if not stdout_str:
            return jsonify({
                "success": False, 
                "error": "Failed to generate board - no image path returned"
            }), 500
        
        # Extract the image path - it should be the last non-empty line
        # and should start with "/" (absolute path)
        lines = [line.strip() for line in stdout_str.split('\n') if line.strip()]
        image_path = None
        
        # Look for the image path from the end (most likely to be last)
        for line in reversed(lines):
            if line.startswith('/store/') and line.endswith('.png'):
                image_path = line
                break
        
        # Fallback: try parsing as JSON
        if not image_path:
            try:
                result = json.loads(stdout_str)
                if isinstance(result, dict):
                    if result.get("success") is False:
                        return jsonify(result), 500
                    image_path = result.get("image_path")
            except json.JSONDecodeError:
                pass
        
        # Last resort: use the last line if it looks like a path
        if not image_path and lines:
            last_line = lines[-1]
            if '/' in last_line and '.png' in last_line:
                image_path = last_line
        
        if not image_path:
            print(f"[TimeframeBoardAPI] Could not extract image path from output: {stdout_str[:500]}")
            return jsonify({
                "success": False,
                "error": "Failed to extract image path from generator output"
            }), 500
        
        # Convert local path to public URL
        # /img/ on the web server maps to /store/droptracker/disc/static/assets/img/
        if image_path.startswith("/store/droptracker/disc/static/assets/img/"):
            relative_path = image_path.replace("/store/droptracker/disc/static/assets/img/", "")
            public_url = f"https://www.droptracker.io/img/{relative_path}"
        elif image_path.startswith("/store/droptracker/disc/static/"):
            relative_path = image_path.replace("/store/droptracker/disc/static/", "")
            public_url = f"https://www.droptracker.io/{relative_path}"
        elif image_path.startswith("static/assets/img/"):
            public_url = f"https://www.droptracker.io/img/{image_path[18:]}"
        elif image_path.startswith("static/"):
            public_url = f"https://www.droptracker.io/{image_path[7:]}"
        else:
            public_url = image_path
        
        print(f"[TimeframeBoardAPI] Board generated successfully: {public_url}")
        
        return jsonify({
            "success": True,
            "image_url": public_url,
            "message": "Custom board generated successfully"
        }), 200
    
    except Exception as e:
        print(f"[TimeframeBoardAPI] Unexpected error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# WOM SYNC ENDPOINT
# ==============================================================================
# Trigger an on-demand WOM membership sync for a group, identical to what the
# /sync-wom Discord command performs.  Intended for the website's "Sync" button.
#
# AUTHENTICATION
#   Pass the group's export API key (GroupConfiguration key = "export_api_key")
#   as either:
#     • JSON body field: { "api_key": "<key>", ... }
#     • Authorization header: Bearer <key>
#
# REQUEST FORMAT (POST /groups/<group_id>/wom-sync):
#   { "group_id": int }   — group_id in the URL path is sufficient; body is optional.
#
# RESPONSE FORMAT:
#   Success (200): {
#     "success": true,
#     "group_name": str,
#     "group_id": int,
#     "wom_id": int,
#     "added": [str, ...],
#     "removed": [str, ...],
#     "total_members": int,
#     "skipped_removals": bool,
#     "duration_seconds": float
#   }
#   Cooldown (429): {
#     "success": false,
#     "on_cooldown": true,
#     "cooldown_remaining_seconds": int,
#     "group_name": str
#   }
#   Auth failure (401/403): { "success": false, "error": str }
#   Not found    (404):      { "success": false, "error": str }
# ==============================================================================

@groups_bp.post("/groups/<int:group_id>/wom-sync")
@route_cors(allow_origin="https://www.droptracker.io")
@rate_limit(limit=10, period=timedelta(seconds=60))
async def group_wom_sync(group_id: int):
    """Trigger an on-demand WOM membership sync for the given group."""
    db_session = get_db_session()
    try:
        # --- Resolve group ---
        group = db_session.query(Group).filter(Group.group_id == group_id).first()
        if not group:
            return jsonify({"success": False, "error": f"Group {group_id} not found"}), 404

        if not group.wom_id:
            return jsonify({"success": False, "error": "This group has no WOM ID configured"}), 400

        # --- Authenticate ---
        # Accept the key from either the Authorization header or the JSON body.
        api_key = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            api_key = auth_header[7:].strip()

        if not api_key:
            try:
                body = await request.get_json(silent=True) or {}
                api_key = body.get("api_key", "")
            except Exception:
                api_key = ""

        if not api_key:
            return jsonify({"success": False, "error": "API key required"}), 401

        # Match against every stored export_api_key row (historical dupes) —
        # same semantics as api/routes/group_export.py.
        key_rows = db_session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == "export_api_key",
        ).all()
        stored_keys = [
            str(row.config_value).strip() for row in key_rows
            if row.config_value and str(row.config_value).strip() not in ("", "0")
        ]
        if not any(hmac.compare_digest(stored, str(api_key).strip()) for stored in stored_keys):
            return jsonify({"success": False, "error": "Invalid API key"}), 403

        # --- Perform sync ---
        try:
            result = await sync_group_from_wom_with_stats(wom_id=int(group.wom_id))
        except Exception as e:
            print(f"[WomSyncAPI] Sync error for group {group_id}: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

        if result["on_cooldown"]:
            return jsonify({
                "success": False,
                "on_cooldown": True,
                "cooldown_remaining_seconds": result["cooldown_remaining_seconds"],
                "group_name": result["group_name"],
            }), 429

        return jsonify({
            "success": True,
            "group_name": result["group_name"],
            "group_id": result["group_id"],
            "wom_id": result["wom_id"],
            "added": result["added"],
            "removed": result["removed"],
            "total_members": result["total_members"],
            "skipped_removals": result["skipped_removals"],
            "duration_seconds": result["duration_seconds"],
        }), 200

    finally:
        db_session.close()

