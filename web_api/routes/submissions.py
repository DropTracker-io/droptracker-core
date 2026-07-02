"""Task 06 — manual submission + media uploads.

  POST /api/v1/submissions/manual   (session) -> { id }
  GET  /api/v1/uploads/presign       (session) -> { upload_url, key, public_url }
  POST /api/v1/video/upload-complete  (session) -> forwards to intake

Authorization is enforced here (the session user must own the target player, or
be superadmin). The actual submission is routed into the **existing** intake
pipeline by forwarding to the RuneLite intake API's ``/manual-submit`` — the
pipeline is reused, never forked (§ Task 06). Uploads reuse the existing B2
presign helper.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
from quart import Blueprint, jsonify, request

from db import Player, User
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import current_user_id, is_superadmin, json_body, load_user
from utils.redis import redis_client

submissions_bp = Blueprint("v1_submissions", __name__)

INTAKE_API_URL = os.getenv("INTAKE_API_URL", "http://127.0.0.1:31323")
B2_CDN_BASE_URL = os.getenv("B2_CDN_BASE_URL", "https://videos.droptracker.io")

# Contract type -> intake `/manual-submit` submission_type.
_TYPE_MAP = {
    "drop": "drop",
    "clog": "collection_log",
    "pb": "personal_best",
    "ca": "combat_achievement",
    "pet": "pet",
}

_CONTENT_TYPE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
}

# Per-user manual-submission rate limit.
_RATE_LIMIT = int(os.getenv("WEB_MANUAL_SUBMIT_PER_MIN", "20"))


def _rc():
    return getattr(redis_client, "client", None)


def _rate_limited(user_id: int) -> bool:
    conn = _rc()
    if conn is None:
        return False
    try:
        key = f"web:ratelimit:manual:{user_id}"
        count = conn.incr(key)
        if count == 1:
            conn.expire(key, 60)
        return count > _RATE_LIMIT
    except Exception:
        return False


@submissions_bp.post("/submissions/manual")
async def manual_submission():
    user_id = current_user_id()
    body = await json_body()

    sub_type = str(body.get("type") or "").strip().lower()
    if sub_type not in _TYPE_MAP:
        abort_problem(422, "Invalid type", f"'type' must be one of {sorted(_TYPE_MAP)}.")

    player_id = body.get("player_id")
    if not isinstance(player_id, int):
        abort_problem(422, "Invalid player_id", "'player_id' must be an integer.")

    if _rate_limited(user_id):
        abort_problem(429, "Too many submissions", "Slow down and try again shortly.")

    def _authorize():
        with db_session() as s:
            user = load_user(s, user_id)
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if not player:
                abort_problem(404, "Player not found", f"No player with id {player_id}.")
            if player.user_id != user_id and not is_superadmin(user):
                abort_problem(403, "Forbidden", "You do not own this player.")
            return player.player_name

    player_name = await asyncio.to_thread(_authorize)

    # Build the intake `/manual-submit` payload (reuse the pipeline).
    payload = {
        "submission_type": _TYPE_MAP[sub_type],
        "player_name": player_name,
        "world_type": "main",
    }
    if body.get("item_name"):
        payload["item_name"] = body["item_name"]
    if body.get("npc_name"):
        payload["npc_name"] = body["npc_name"]
        payload["boss_name"] = body["npc_name"]  # pb uses boss_name
    if body.get("value") is not None:
        payload["value"] = int(body["value"])
    if body.get("quantity") is not None:
        payload["quantity"] = int(body["quantity"])
    if body.get("notes"):
        payload["notes"] = body["notes"]
    # Proof: a presigned B2 key becomes a public image URL the pipeline stores.
    key = body.get("proof_upload_key")
    if key:
        payload["image_url"] = f"{B2_CDN_BASE_URL.rstrip('/')}/{key.lstrip('/')}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{INTAKE_API_URL}/manual-submit", json=payload)
    except Exception as e:
        abort_problem(502, "Submission pipeline unavailable", str(e))

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
        detail = (data or {}).get("error") or f"Intake returned {resp.status_code}."
        abort_problem(422, "Submission rejected", detail)

    # The intake pipeline doesn't surface a row id; return a confirmation id.
    new_id = None
    if isinstance(data, dict):
        new_id = data.get("id") or data.get("drop_id")
    if not isinstance(new_id, int):
        new_id = int(time.time() * 1000) % 1_000_000_000
    return jsonify({"id": new_id})


@submissions_bp.get("/uploads/presign")
async def uploads_presign():
    current_user_id()  # session required

    content_type = (request.args.get("content_type") or "image/png").lower()
    kind = (request.args.get("kind") or "image").lower()
    ext = _CONTENT_TYPE_EXT.get(content_type, "png" if kind == "image" else "bin")
    key = f"uploads/{uuid.uuid4().hex}.{ext}"

    def _presign():
        from utils.b2_storage import generate_presigned_upload_url

        return generate_presigned_upload_url(
            object_key=key, content_type=None, expiry_seconds=600
        )

    try:
        upload_url = await asyncio.to_thread(_presign)
    except Exception as e:
        abort_problem(502, "Upload service unavailable", str(e))

    public_url = f"{B2_CDN_BASE_URL.rstrip('/')}/{key}"
    return private_no_store(
        jsonify({"upload_url": upload_url, "key": key, "public_url": public_url})
    )


@submissions_bp.post("/video/upload-complete")
async def video_upload_complete():
    current_user_id()  # session required
    body = await json_body(required=False)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{INTAKE_API_URL}/video/upload-complete", json=body)
        data = resp.json()
    except Exception as e:
        abort_problem(502, "Upload service unavailable", str(e))
    return jsonify(data if isinstance(data, dict) else {"ok": True})
