from datetime import datetime, timedelta
from typing import List
import asyncio
import time
import threading
import copy

from quart import Blueprint, jsonify, request
from quart_cors import route_cors
from quart_rate_limiter import rate_limit
from sqlalchemy import or_, text

from api.core import get_db_session, redis_client
from api.routes.helpers import assemble_submission_data
from lootboard import generator
from services.redis_updates import get_player_list_loot_sum
from utils.format import format_number
from utils.wiseoldman import fetch_group_members
from db import Player, Group, GroupConfiguration, NotifiedSubmission, NpcList, get_current_partition
from db.ops import associate_player_ids, sync_group_from_wom_with_stats
from utils.redis import calculate_rank_amongst_groups


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


@groups_bp.get("/top_groups")
async def top_groups():
    partition = get_current_partition()
    cache_key = f"top_groups:{partition}"
    cached_payload = _groups_cache_get(cache_key, TOP_GROUPS_CACHE_TTL_SECONDS)
    if cached_payload is not None:
        return jsonify(cached_payload), 200

    db_session = get_db_session()
    try:
        groups = db_session.query(Group).all()

        group_totals = {}
        for group_object in groups:
            group_id = group_object.group_id
            if group_id == 2 or group_id == 0:
                continue
            players_in_group = db_session.query(Player.player_id).join(Player.groups).filter(Group.group_id == group_id).all()
            group_totals[group_id] = 0
            try:
                group_month_total = get_player_list_loot_sum([player.player_id for player in players_in_group])
                group_totals[group_id] = group_month_total
            except Exception as e:
                print(f"Error getting group total for group {group_id}: {e}")
                group_totals[group_id] = 0

        sorted_groups = sorted(group_totals.items(), key=lambda x: x[1], reverse=True)
        final_groups = []
        for rank, (g_id, group_total) in enumerate(sorted_groups, start=1):
            group_object = db_session.query(Group).filter(Group.group_id == g_id).first()
            if g_id != 2 and g_id != 0:
                top_player_data = redis_client.client.zrevrange(
                    f"leaderboard:{get_current_partition()}:group:{g_id}",
                    0,
                    0,
                    withscores=True
                )

                top_player_display = None
                if top_player_data:
                    player_id_raw, player_score = top_player_data[0]
                    try:
                        player_id_int = int(player_id_raw.decode("utf-8")) if isinstance(player_id_raw, (bytes, bytearray)) else int(player_id_raw)
                    except Exception:
                        player_id_int = int(player_id_raw)
                    top_player = db_session.query(Player).filter(Player.player_id == player_id_int).first()
                    if top_player:
                        top_player_display = f"{top_player.player_name}"

                final_groups.append({
                    "group_name": group_object.group_name,
                    "total_loot": format_number(group_total),
                    "rank": rank,
                    "group_id": group_object.group_id,
                    "member_count": len(players_in_group),
                    "top_player": top_player_display,
                })
        payload = {"groups": final_groups}
        _groups_cache_set(cache_key, payload)
        return jsonify(payload), 200
    finally:
        db_session.close()


@groups_bp.get("/group_search")
async def group_search():
    group_name = request.args.get("name", None)
    if not group_name:
        return jsonify({"error": "Group name is required"}), 400

    db_session = get_db_session()
    try:
        group: Group = db_session.query(Group).filter(Group.group_name == group_name).first()
        if not group:
            return jsonify({"error": "Group " + group_name + " not found"}), 404
        group_wom_id = db_session.query(Group.wom_id).filter(Group.group_id == group.group_id).first()
        wom_member_list = []
        try:
            if group_wom_id:
                group_wom_id = group_wom_id[0]
            if group_wom_id:
                wom_member_list = await fetch_group_members(wom_group_id=int(group_wom_id), session_to_use=db_session)
        except Exception as e:
            print("Couldn't get the member list", e)

        player_ids = await associate_player_ids(wom_member_list,session_to_use=db_session)
        group_rank, total_groups = calculate_rank_amongst_groups(group.group_id, player_ids, session_to_use=db_session)
        top_player_data = redis_client.client.zrevrange(
            f"leaderboard:{get_current_partition()}:group:{group.group_id}",
            0,
            0,
            withscores=True
        )

        top_player_display = None
        if top_player_data:
            player_id_raw, player_score = top_player_data[0]
            try:
                player_id_int = int(player_id_raw.decode("utf-8")) if isinstance(player_id_raw, (bytes, bytearray)) else int(player_id_raw)
            except Exception:
                player_id_int = int(player_id_raw)
            top_player = db_session.query(Player).filter(Player.player_id == player_id_int).first()
            if top_player:
                top_player_display = f"{top_player.player_name}"

        player_count = group.get_player_count(session_to_use=db_session)
        group_recent_submissions = db_session.query(NotifiedSubmission).filter(or_(NotifiedSubmission.pb_id != None, NotifiedSubmission.drop_id != None, NotifiedSubmission.clog_id != None)).filter(NotifiedSubmission.group_id == group.group_id).order_by(NotifiedSubmission.date_added.desc()).limit(10).all()
        final_submission_data = await assemble_submission_data(group_recent_submissions, db_session)
        players_in_group = db_session.query(Player.player_id).join(Player.groups).filter(Group.group_id == group.group_id).all()
        group_total = get_player_list_loot_sum([player.player_id for player in players_in_group])
        return jsonify({
            "group_name": group.group_name,
            "group_description": group.description,
            "group_image_url": group.icon_url,
            "public_discord_link": group.invite_url if group.invite_url else None,
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

        key_cfg = db_session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == "export_api_key",
        ).first()

        if not key_cfg or str(key_cfg.config_value).strip() != str(api_key).strip():
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

