import asyncio
import json
import time
from datetime import datetime, timedelta

from quart import Blueprint, jsonify, request
from sqlalchemy import text

from api.core import logger, metrics
from api.core import get_db_session
from api.routes.group_create import require_service_key
from db import Drop, CollectionLogEntry, PersonalBestEntry, CombatAchievementEntry, Player
from services.submission_status import get_submission_statuses


utils_bp = Blueprint("utils", __name__)


@utils_bp.get("/debug_logs")
@require_service_key
async def debug_logger():
    file = "data/logs/app_logs.json"

    def _lenient_parse_json(content: str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            pass

        logs = []
        decoder = json.JSONDecoder()
        idx = 0
        length = len(content)
        while idx < length:
            while idx < length and content[idx] in " \t\r\n":
                idx += 1
            if idx >= length:
                break
            try:
                obj, end = decoder.raw_decode(content, idx)
                logs.append(obj)
                idx = end
                continue
            except json.JSONDecodeError:
                idx += 1
                continue

        if logs:
            return logs

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.endswith(","):
                line = line[:-1]
            try:
                logs.append(json.loads(line))
            except Exception:
                continue
        if logs:
            return logs

        s = content.strip()
        if s.startswith("[") and not s.endswith("]"):
            depth_array = 0
            depth_object = 0
            last_top_level_comma = -1
            for i, ch in enumerate(s):
                if ch == "[":
                    depth_array += 1
                elif ch == "]":
                    depth_array -= 1
                elif ch == "{":
                    depth_object += 1
                elif ch == "}":
                    depth_object -= 1
                elif ch == "," and depth_array == 1 and depth_object == 0:
                    last_top_level_comma = i
                if depth_array == 0 and i > 0:
                    try:
                        parsed = json.loads(s[: i + 1])
                        if isinstance(parsed, list):
                            return parsed
                    except Exception:
                        break
            if last_top_level_comma != -1:
                candidate = s[: last_top_level_comma] + "]"
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass

        return []

    try:
        content = await asyncio.to_thread(lambda: open(file, "r").read())
    except FileNotFoundError:
        return jsonify({"logs": [], "error": "log file not found"}), 200
    except Exception as e:
        return jsonify({"logs": [], "error": str(e)}), 200

    logs = _lenient_parse_json(content)
    return jsonify({"logs": logs}), 200


guid_fail_cache = {}
guid_status_cache = {}
GUID_CACHE_TTL_SECONDS = 30
GUID_LOOKUP_CONCURRENCY = 5
MAX_GUID_CACHE_SIZE = 5000
_guid_lookup_semaphore = asyncio.Semaphore(GUID_LOOKUP_CONCURRENCY)

def _trim_guid_caches():
    """Trim GUID caches to prevent unbounded growth."""
    global guid_fail_cache, guid_status_cache
    if len(guid_fail_cache) > MAX_GUID_CACHE_SIZE:
        # Remove oldest entries (approximately)
        keys_to_remove = list(guid_fail_cache.keys())[: len(guid_fail_cache) - int(MAX_GUID_CACHE_SIZE * 0.8)]
        for key in keys_to_remove:
            guid_fail_cache.pop(key, None)
    if len(guid_status_cache) > MAX_GUID_CACHE_SIZE:
        keys_to_remove = list(guid_status_cache.keys())[: len(guid_status_cache) - int(MAX_GUID_CACHE_SIZE * 0.8)]
        for key in keys_to_remove:
            guid_status_cache.pop(key, None)


# A submission that never gets a Redis marker (rejected duplicate, unauthorized
# player, or one submitted before markers were deployed) would otherwise poll
# forever; after this many misses we tell the client it's done.
MAX_STATUS_POLLS_BEFORE_GIVEUP = 10


def _check_one_guid(guid: str, status: dict | None) -> dict:
    """Build the /check result for a single guid from its Redis marker (or lack
    of one), applying the poll-count give-up fallback."""
    if status is not None:
        guid_fail_cache.pop(guid, None)
        result = {
            "processed": True,
            "status": status.get("status", "processed"),
            "type": status.get("type"),
            "uuid": guid,
        }
        # Rejected markers carry a human-readable reason for newer plugin builds.
        if status.get("reason"):
            result["reason"] = status.get("reason")
        return result

    polls = guid_fail_cache.get(guid, 0) + 1
    guid_fail_cache[guid] = polls
    if polls >= MAX_STATUS_POLLS_BEFORE_GIVEUP:
        # Give up gracefully so the plugin stops polling; the submission was
        # most likely rejected as a duplicate or predates status markers.
        return {"processed": True, "status": "processed", "uuid": guid}
    return {"processed": False, "status": "pending", "uuid": guid}


@utils_bp.post("/check")
async def check():
    """Report whether submissions have finished processing.

    Accepts either the legacy single form {"uuid": "..."} (returns a single
    object) or a batch {"uuids": ["...", ...]} (returns {"results": [...]}).
    Status markers are written by the intake paths via
    services/submission_status.py.
    """
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415
    try:
        # Periodically trim caches to prevent memory growth
        _trim_guid_caches()

        data = await request.get_json()
        incoming_guid = data.get("uuid")
        incoming_guids = data.get("uuids")

        if incoming_guids is not None:
            if not isinstance(incoming_guids, list) or not all(isinstance(g, str) for g in incoming_guids):
                return jsonify({"error": "'uuids' must be a list of strings"}), 422
            incoming_guids = incoming_guids[:100]
            statuses = await asyncio.to_thread(get_submission_statuses, incoming_guids)
            results = [_check_one_guid(g, statuses.get(g)) for g in incoming_guids]
            return jsonify({"results": results}), 200

        if not incoming_guid:
            return jsonify({"error": "Missing 'uuid'"}), 422

        statuses = await asyncio.to_thread(get_submission_statuses, [incoming_guid])
        return jsonify(_check_one_guid(incoming_guid, statuses.get(incoming_guid))), 200
    except Exception as e:
        print(f"/check error: {e}")
        return jsonify({"error": "Malformed or invalid request"}), 400


@utils_bp.get("/metrics")
@require_service_key
async def get_metrics():
    return jsonify(metrics.get_stats())


