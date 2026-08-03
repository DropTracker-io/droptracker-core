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
from utils.submission_messages import friendly_rejection

submissions_bp = Blueprint("v1_submissions", __name__)

INTAKE_API_URL = os.getenv("INTAKE_API_URL", "http://127.0.0.1:31323")
B2_CDN_BASE_URL = os.getenv("B2_CDN_BASE_URL", "https://videos.droptracker.io")

# Shared secret the intake API requires on /manual-submit (the endpoint
# substitutes the player's real account_hash, so it must never be publicly
# callable). Read at request time (matches the intake-side gate; also lets
# tests monkeypatch it).
MANUAL_SUBMIT_KEY_HEADER = "X-DT-Manual-Key"


def _manual_submit_headers() -> dict:
    key = (os.getenv("MANUAL_SUBMIT_KEY") or "").strip()
    return {MANUAL_SUBMIT_KEY_HEADER: key} if key else {}

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

# Mirrors data/submissions/drop.py::MAX_SPLIT_SIZE (CoX's 100-player cap).
MAX_SPLIT_SIZE = 100


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


def _parse_split(body, receiver_name: str):
    """Validate the submit form's split fields -> (other_players, split_size).

    Both are optional and independent: naming people is enough for the common
    all-tracked case, and a bare size covers "I split with people who aren't on
    DropTracker". Rejects only what is self-contradictory, since the processor
    already ignores a size that would shrink the divisor below the names given.

    The receiver is filtered out of the name list (the form pre-fills the
    submitting account and the pipeline counts the receiver separately, so
    leaving it in would double-count them and shrink everyone's share).
    """
    raw = body.get("split_players")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        abort_problem(422, "Invalid split", "'split_players' must be a list of player names.")

    receiver_key = receiver_name.strip().lower().replace("_", " ")
    others, seen = [], set()
    for entry in raw:
        name = str(entry or "").strip()
        if not name:
            continue
        if len(name) > 12:  # OSRS display names cap at 12 characters
            abort_problem(422, "Invalid split", f"“{name}” isn't a valid RuneScape name.")
        key = name.lower().replace("_", " ")
        if key == receiver_key or key in seen:
            continue
        seen.add(key)
        others.append(name)

    if len(others) + 1 > MAX_SPLIT_SIZE:
        abort_problem(422, "Invalid split",
                      f"A split can involve at most {MAX_SPLIT_SIZE} players.")

    raw_size = body.get("split_size")
    if raw_size in (None, ""):
        # Everyone named and nobody else: the party is the people listed.
        return others, (len(others) + 1 if others else None)
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        abort_problem(422, "Invalid split", "'split_size' must be a whole number.")
    if size < 2 or size > MAX_SPLIT_SIZE:
        abort_problem(422, "Invalid split",
                      f"A split has to be between 2 and {MAX_SPLIT_SIZE} players.")
    if size < len(others) + 1:
        abort_problem(
            422, "Invalid split",
            f"You listed {len(others)} other player(s), so the split is at least "
            f"{len(others) + 1} ways — but you entered {size}.",
        )
    return others, size


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

    # Friendly per-type validation (the intake API would 400 anyway, but its
    # messages are payload-key jargon; reject with field-level detail here).
    item_name = (body.get("item_name") or "").strip() or None
    npc_name = (body.get("npc_name") or "").strip() or None
    if sub_type == "drop" and not (item_name and npc_name):
        abort_problem(422, "Missing fields", "A drop needs both an item and the NPC it came from.")
    if sub_type in ("clog", "pet") and not item_name:
        noun = "collection log item" if sub_type == "clog" else "pet"
        abort_problem(422, "Missing fields", f"Pick the {noun} you received.")
    if sub_type == "pb" and not npc_name:
        abort_problem(422, "Missing fields", "Pick the boss the personal best is for.")
    if sub_type == "pb" and not body.get("time_ms"):
        abort_problem(422, "Missing fields", "A personal best needs the kill time.")
    if sub_type == "ca" and not ((body.get("task") or "").strip() and (body.get("tier") or "").strip()):
        abort_problem(422, "Missing fields", "A combat achievement needs the task name and its tier.")

    # Build the intake `/manual-submit` payload (reuse the pipeline).
    payload = {
        "submission_type": _TYPE_MAP[sub_type],
        "player_name": player_name,
        "world_type": "main",
    }
    if item_name:
        payload["item_name"] = item_name
    if npc_name:
        payload["npc_name"] = npc_name
        payload["boss_name"] = npc_name  # pb uses boss_name
    if isinstance(body.get("item_id"), int):
        payload["item_id"] = body["item_id"]
    if body.get("value") is not None:
        payload["value"] = int(body["value"])
    if body.get("quantity") is not None:
        payload["quantity"] = int(body["quantity"])
    if body.get("notes"):
        payload["notes"] = body["notes"]
    if sub_type == "drop":
        # Split tracking. `split_players` are the OTHER people the drop was split
        # with; `split_size` is how many people it was split between in total,
        # receiver included — which is NOT len(split_players) + 1 whenever a
        # share went to someone who isn't tracked here. Groups with
        # split_gp_tracking on divide by split_size, so an untracked or unnamed
        # party member still shrinks everyone's cut instead of silently
        # inflating it. See data/submissions/drop.py::_award_split_gp_credits.
        split_players, split_size = _parse_split(body, player_name)
        if split_players:
            payload["players_included"] = split_players
        if split_size:
            payload["split_size"] = split_size

    if sub_type == "pb":
        payload["time_ms"] = int(body["time_ms"])
        if body.get("team_size"):
            payload["team_size"] = int(body["team_size"])
    elif sub_type == "ca":
        payload["task"] = str(body["task"]).strip()
        payload["tier"] = str(body["tier"]).strip().lower()
    elif sub_type == "pet":
        # The pet processor keys off pet_name (pets live in the item list);
        # the boss/source and killcount ride along for the notification embed.
        payload["pet_name"] = item_name
        if npc_name:
            payload["source"] = npc_name
        if body.get("kc") is not None:
            payload["killcount"] = int(body["kc"])
    elif sub_type == "clog":
        if body.get("kc") is not None:
            payload["kc"] = int(body["kc"])
        if npc_name:
            payload["source"] = npc_name
    # Proof: a presigned B2 key becomes a public image URL the pipeline stores.
    key = body.get("proof_upload_key")
    if key:
        payload["image_url"] = f"{B2_CDN_BASE_URL.rstrip('/')}/{key.lstrip('/')}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{INTAKE_API_URL}/manual-submit",
                json=payload,
                headers=_manual_submit_headers(),
            )
    except Exception as e:
        abort_problem(502, "Submission pipeline unavailable", str(e))

    try:
        data = resp.json()
    except Exception:
        data = {}

    # A *processor* rejection comes back as HTTP 200 with
    # {"success": false, "message": ...} — only the endpoint's own validation
    # and parse failures set "error". Reading "error" alone therefore missed
    # every real rejection and fell through to the status-code fallback, which
    # showed players the literal string "Intake returned 200."; friendly_rejection
    # reads message/notice too and puts the reason in player-facing words.
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
        detail = friendly_rejection(data)
        # 5xx, and the shared-secret gate's 401/503, are our problem rather
        # than a bad submission — don't blame the player's input for them.
        if resp.status_code >= 500 or resp.status_code in (401, 503):
            abort_problem(502, "Submission pipeline unavailable", detail)
        abort_problem(422, "Submission rejected", detail)

    # The intake pipeline doesn't surface a row id; return a confirmation id.
    new_id = None
    if isinstance(data, dict):
        new_id = data.get("id") or data.get("drop_id")
    if not isinstance(new_id, int):
        new_id = int(time.time() * 1000) % 1_000_000_000
    return jsonify({"id": new_id})


_POLICY_NOTICE = {
    "block": "won't be counted (manual submissions are disabled)",
    "authorized_only": "won't be counted (only authorized members' manual submissions count)",
    "confirm": "will be held for a group admin to approve",
}


@submissions_bp.get("/submissions/manual/preflight")
async def manual_submission_preflight():
    """Per-group manual-policy notices for a player's groups, so the submit
    page can warn before submitting (suggestion #45, Phase 3). Only groups
    where THIS submitter's manual drop would be withheld are returned — groups
    that count it normally (allow, or an authorized submitter) are omitted."""
    user_id = current_user_id()
    player_id = request.args.get("player_id", type=int)
    if not player_id:
        abort_problem(422, "Invalid player_id", "'player_id' query param is required.")

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if not player:
                abort_problem(404, "Player not found", f"No player with id {player_id}.")
            if player.user_id != user_id and not is_superadmin(user):
                abort_problem(403, "Forbidden", "You do not own this player.")
            gid_name = {g.group_id: g.group_name for g in (player.groups or [])}
            # Lazy import (matches routes/points.py): keeps the processor
            # package out of web_api's startup path.
            from data.submissions.manual_policy import manual_moderation_for_player

            moderation = manual_moderation_for_player(s, player, list(gid_name))
            notices = [
                {
                    "group_id": gid,
                    "group_name": gid_name.get(gid, f"Group {gid}"),
                    "policy": policy,
                    "held": status,  # 'excluded' | 'pending'
                    "message": _POLICY_NOTICE.get(policy, "will be reviewed by the group"),
                }
                for gid, (status, policy) in moderation.items()
            ]
            notices.sort(key=lambda n: n["group_name"].lower())
            return {"notices": notices}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@submissions_bp.get("/uploads/presign")
async def uploads_presign():
    current_user_id()  # session required

    content_type = (request.args.get("content_type") or "image/png").lower()
    kind = (request.args.get("kind") or "image").lower()
    ext = _CONTENT_TYPE_EXT.get(content_type, "png" if kind == "image" else "bin")
    # The B2 application key is namePrefix-restricted to "dt_" — keys outside
    # that namespace fail with 403 "not entitled".
    key = f"dt_uploads/{uuid.uuid4().hex}.{ext}"

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


_PROOF_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — matches the submit form's client cap.
# Pillow format -> (extension, content-type) for the image types accepted as proof.
_PROOF_IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}


@submissions_bp.post("/uploads/proof")
async def upload_proof():
    """Accept a proof screenshot and store it in B2 **server-side**.

    Replaces the old browser→B2 presigned PUT: Backblaze's bucket CORS policy
    only allows GET/HEAD, so a direct cross-origin PUT from the browser failed
    its CORS preflight and surfaced to the user as "Failed to fetch". The
    browser now POSTs the image to us (same origin, no CORS) and we stream it to
    B2 with server credentials. Returns the same ``{key, public_url}`` shape the
    old presign did, so the manual-submission route still consumes ``key`` as
    ``proof_upload_key`` unchanged.
    """
    current_user_id()  # session required

    files = await request.files
    upload = files.get("file")
    if upload is None:
        abort_problem(422, "Invalid body", "A multipart 'file' field is required.")
    raw = upload.read()
    if not raw:
        abort_problem(422, "Empty file", "The uploaded image was empty.")
    if len(raw) > _PROOF_MAX_BYTES:
        abort_problem(422, "File too large", "Proof screenshots are capped at 10 MB.")

    import io

    from PIL import Image, UnidentifiedImageError

    # Validate it's a real image and derive the extension from the actual bytes
    # (never trust the client's declared content-type). `.format` is populated
    # at open() and stays readable after verify() invalidates the pixel data.
    try:
        im = Image.open(io.BytesIO(raw))
        fmt_name = im.format
        im.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG, WebP, or GIF image.")
    fmt = _PROOF_IMAGE_FORMATS.get(fmt_name or "")
    if fmt is None:
        abort_problem(422, "Unsupported image", "Upload a PNG, JPEG, WebP, or GIF image.")
    ext, content_type = fmt

    # The B2 application key is namePrefix-restricted to "dt_" — keys outside
    # that namespace fail with 403 "not entitled".
    key = f"dt_uploads/{uuid.uuid4().hex}.{ext}"

    try:
        from utils.b2_storage import upload_bytes

        await upload_bytes(raw, key, content_type)
    except Exception as e:
        abort_problem(502, "Upload service unavailable", str(e))

    public_url = f"{B2_CDN_BASE_URL.rstrip('/')}/{key}"
    return private_no_store(jsonify({"key": key, "public_url": public_url}))


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
