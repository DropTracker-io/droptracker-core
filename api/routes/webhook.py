import asyncio
import hmac
import json as _json
import os
import uuid
from datetime import datetime, timedelta

from quart import Blueprint, jsonify, request, g
from quart_rate_limiter import rate_limit

from api.core import logger, get_db_session, metrics, reset_db_connections
from data import submissions
from data.submissions.dispatch import (
    SEASONAL_WORLD_TYPE,
    SUPPORTED_TYPES,
    dispatch_submission,
    normalize_submission_type,
    normalize_world_type,
)
from db import Player, Drop
from db.models.video_upload import VideoUpload
from services.seasonal_state import is_seasonal_active
from services.submission_status import mark_submission_processed, mark_submission_rejected
from utils.download import download_image
from utils.video_storage import backend_for_video_record, get_public_video_url


webhook_bp = Blueprint("webhook", __name__)
MAIN_WORLD_TYPE = "main"

# Queue-mode: when WEBHOOK_QUEUE_MODE=true the acceptors push to Redis and
# return immediately; a separate workers/webhook_consumer.py drains the queue.
_QUEUE_MODE = os.getenv("WEBHOOK_QUEUE_MODE", "").lower() in ("true", "1", "yes")
_WEBHOOK_TEMP_DIR = os.getenv("WEBHOOK_TEMP_DIR", "/tmp/webhook_uploads")

# Past this depth a dev instance drops mirrored submissions instead of queueing
# them. The queue is an unbounded Redis list with no backpressure of its own, and
# mirrored traffic arrives at production rates -- without a ceiling it would bury
# whatever the operator is actually trying to test.
_MIRROR_SHED_DEPTH = int(os.getenv("MIRROR_SHED_DEPTH", "2000") or 2000)


def _is_mirrored_request() -> bool:
    """Whether this is a mirrored copy of production traffic.

    Set by the Cloudflare Worker (edge/intake-capture) when the admin panel has
    mirroring switched on.
    """
    return request.headers.get("X-DT-Mirror") == "1"


def _accepts_mirrored() -> bool:
    """Only a dev instance may accept mirrored traffic.

    This is the dev-side accept gate, and it is deliberately `STATE=dev` rather
    than a switch of its own: one fewer thing to set, and it cannot be left on
    by accident on a box that is not dev. It is also what stops a mistyped
    MIRROR_HOST from feeding production its own submissions back.
    """
    from utils.dev_guild_guard import is_dev_mode

    return is_dev_mode()


# Per-submission ingestion diagnostics are very high volume (several lines per
# drop); keep them off unless explicitly debugging with DROP_REQUEST_DEBUG=true.
_DROP_REQUEST_DEBUG = os.getenv("DROP_REQUEST_DEBUG", "").lower() in ("true", "1", "yes")


def _drop_request_debug(message: str):
    """Consistent logging for drop request ingestion diagnostics."""
    if _DROP_REQUEST_DEBUG:
        print(f"[DropRequestDebug] {message}")


def _mark_submission_outcome(processed_data, submission_type, response):
    """Write the per-submission status marker from the processor's verdict.

    A SubmissionResponse with success=False is a definitive rejection
    (duplicate, failed auth, unknown item/NPC) — record it as such so /check
    reports the real outcome. A missing response object means the processor
    completed without an explicit verdict; treat that as processed, matching
    the pre-status-marker behavior.
    """
    guid = processed_data.get("guid") or processed_data.get("unique_id")
    if response is not None and getattr(response, "success", True) is False:
        mark_submission_rejected(guid, submission_type, reason=getattr(response, "message", None))
    else:
        mark_submission_processed(guid, submission_type)


# Type routing lives in data/submissions/dispatch.py so this path, the queue
# consumer and the legacy Discord-webhook reader cannot drift apart again.
# These names stay as thin aliases because both other callers import them here.
_normalize_submission_type = normalize_submission_type
_normalize_world_type = normalize_world_type


async def _dispatch_seasonal_submission(submission_type, processed_data, db_session):
    """Route seasonal submissions to their processors with world_type='seasonal'."""
    return await dispatch_submission(
        submission_type, processed_data, db_session, world_type=SEASONAL_WORLD_TYPE
    )


async def _link_video_to_submission(processed_data, db_session):
    """
    If the processed webhook data contains a video_key, look up the
    corresponding VideoUpload record and link it to the submission.
    
    The video_key is injected into the webhook embed fields by the
    RuneLite plugin when video mode is active.
    """
    video_key = processed_data.get("video_key")
    if not video_key:
        return

    try:
        video_record = db_session.query(VideoUpload).filter(
            VideoUpload.video_key == video_key
        ).first()
        if video_record:
            # Link the submission type (canonicalized) + unique submission id (guid)
            sub_type_raw = (processed_data.get("type") or "").lower().strip()
            type_map = {
                "npc": "drop",
                "other": "drop",
                "drop": "drop",
                "collection_log": "collection_log",
                "personal_best": "personal_best",
                "kill_time": "personal_best",
                "npc_kill": "personal_best",
                "combat_achievement": "combat_achievement",
                "pet": "pet",
            }
            video_record.submission_type = type_map.get(sub_type_raw, sub_type_raw or None)
            video_record.submission_unique_id = processed_data.get("guid") or processed_data.get("unique_id")

            # If the video has already been processed, attach a URL (computed)
            if video_record.status == "processed" and video_record.final_key:
                processed_data["video_url"] = get_public_video_url(
                    video_record.final_key,
                    backend=backend_for_video_record(video_record),
                )
            elif video_record.status in ("pending", "uploaded"):
                # Mark as uploaded if still pending (upload confirmed by webhook arrival)
                if video_record.status == "pending":
                    video_record.status = "uploaded"
    except Exception as e:
        # Don't fail the submission if video linking fails
        print(f"[Webhook] Error linking video_key {video_key}: {e}")


async def _try_attach_video_url_to_drop(processed_data, db_session):
    """
    If this is a drop submission with a video_key, try to:
    - find the created Drop row via Drop.unique_id == guid
    - link VideoUpload.drop_id to that Drop
    - if the video is already processed, write drops.video_url
      (otherwise the client can poll /video/status by key)
    """
    try:
        video_key = processed_data.get("video_key")
        guid = processed_data.get("guid") or processed_data.get("unique_id")
        if not video_key or not guid:
            return

        # Find video upload record
        video_record = db_session.query(VideoUpload).filter(VideoUpload.video_key == video_key).first()
        if not video_record:
            return

        # Find the Drop created for this submission (drop_processor uses unique_id=guid)
        drop = db_session.query(Drop).filter(Drop.unique_id == str(guid)).order_by(Drop.drop_id.desc()).first()
        if not drop:
            return

        video_record.drop_id = drop.drop_id

        # If processed, persist the serving URL on the drop row
        if video_record.status == "processed" and video_record.final_key:
            drop.video_url = get_public_video_url(
                video_record.final_key,
                backend=backend_for_video_record(video_record),
            )
    except Exception as e:
        print(f"[Webhook] Error attaching video URL to drop: {e}")


async def _save_upload_to_temp(image_file) -> tuple:
    """Save a Quart FileStorage object to a temp path. Returns (path, filename, content_type)."""
    import inspect
    import aiofiles

    os.makedirs(_WEBHOOK_TEMP_DIR, exist_ok=True)

    filename = getattr(image_file, "filename", None) or "upload.jpg"
    content_type = (
        getattr(image_file, "content_type", None)
        or getattr(image_file, "mimetype", None)
        or "image/jpeg"
    )

    ext = "jpg"
    if filename and "." in filename:
        cand = filename.rsplit(".", 1)[1].lower()
        if cand in {"jpg", "jpeg", "png", "gif", "webp"}:
            ext = "jpg" if cand == "jpeg" else cand
    elif "png" in content_type:
        ext = "png"
    elif "gif" in content_type:
        ext = "gif"
    elif "webp" in content_type:
        ext = "webp"

    tmp_path = os.path.join(_WEBHOOK_TEMP_DIR, f"{uuid.uuid4().hex}.{ext}")

    try:
        if hasattr(image_file, "save"):
            save_fn = image_file.save
            if inspect.iscoroutinefunction(save_fn):
                await save_fn(tmp_path)
            else:
                await asyncio.to_thread(save_fn, tmp_path)
        elif hasattr(image_file, "read") and inspect.iscoroutinefunction(
            getattr(image_file, "read", None)
        ):
            async with aiofiles.open(tmp_path, "wb") as f:
                data = await image_file.read()
                await f.write(data)
        elif hasattr(image_file, "file"):
            def _sync_copy():
                import shutil
                try:
                    image_file.file.seek(0)
                except Exception:
                    pass
                with open(tmp_path, "wb") as out:
                    shutil.copyfileobj(image_file.file, out)
            await asyncio.to_thread(_sync_copy)
        return tmp_path, filename, content_type
    except Exception as e:
        logger.log_sync("error", f"[QueueAcceptor] Failed to save upload to temp: {e}")
        return None, filename, content_type


async def _queue_webhook_request():
    """Fast-path acceptor: validate, save image to temp, push to Redis queue, return 200."""
    import json
    from utils import webhook_spool
    from utils.redis import RedisClient

    try:
        content_type = request.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return jsonify({"error": "Expected multipart/form-data"}), 400

        form = await request.form
        payload_json = form.get("payload_json")
        if not payload_json:
            return jsonify({"error": "No payload_json found in form data"}), 400

        try:
            webhook_payload = json.loads(payload_json)
        except Exception:
            return jsonify({"error": "Invalid JSON in payload_json"}), 400

        if not webhook_payload:
            return jsonify({"error": "Empty payload"}), 400

        files = await request.files
        image_file = files.get("file") if files else None

        mirrored = _is_mirrored_request()

        image_tmp_path = image_filename = image_content_type = None
        # Mirrored submissions keep no screenshot. utils/download.py bakes the
        # production URL into image_url, so a copy stored on dev would render as
        # a broken link anyway -- all it would buy is dev's disk filling at
        # production rates.
        if image_file and not mirrored:
            image_tmp_path, image_filename, image_content_type = await _save_upload_to_temp(image_file)
            if image_tmp_path is None:
                # The stash failed (almost always a full disk) but the payload
                # itself is fine. Keeping the submission and dropping the
                # screenshot is the right trade — losing the drop to save its
                # picture would be worse — but it must not be silent, because
                # the result looks identical to a client that never sent one.
                logger.log_sync(
                    "warning",
                    "[QueueAcceptor] Accepted a submission WITHOUT its screenshot: "
                    f"the temp stash failed for {image_filename!r}",
                )

        entry = {
            "payload": webhook_payload,
            "image_tmp_path": image_tmp_path,
            "image_filename": image_filename,
            "image_content_type": image_content_type,
            "enqueued_at": datetime.utcnow().isoformat(),
        }
        if mirrored:
            entry["mirrored"] = True

        # A 200 from here means "we have durably taken responsibility for this
        # submission", and the plugin stops retrying on the strength of it. So
        # the enqueue failing must never reach the client as success: spool it
        # to disk instead, and if even that fails, answer 503 so the client
        # keeps its copy and retries. (2026-08-18: this path silently swallowed
        # the Redis error and answered 200 ~40,800 times.)
        rc = RedisClient()
        if mirrored:
            # Shed rather than buffer. The production copy of this submission is
            # already durably handled, so a dropped mirror costs nothing, while a
            # mirror-driven backlog would delay everything behind it.
            try:
                if rc.client.llen("webhook:queue") > _MIRROR_SHED_DEPTH:
                    return jsonify({"message": "Shed"}), 200
            except Exception:
                pass  # If we cannot measure the queue, take the submission.
        try:
            rc.rpush("webhook:queue", json.dumps(entry))
        except Exception as redis_error:
            if webhook_spool.write(entry):
                logger.log_sync(
                    "warning",
                    f"[QueueAcceptor] Redis rejected the enqueue; spooled to disk: {redis_error}",
                )
                return jsonify({"message": "Queued"}), 200
            logger.log_sync(
                "error",
                f"[QueueAcceptor] Redis rejected the enqueue AND the spool write failed "
                f"({redis_error}) — asking the client to retry",
            )
            # The client will resend, screenshot included, so this copy is
            # already garbage. Leaving it behind is how 926 orphans from
            # 2026-08-18 are still sitting in WEBHOOK_TEMP_DIR: nothing else
            # ever unlinks a temp file whose entry never reached the queue.
            if image_tmp_path:
                try:
                    os.unlink(image_tmp_path)
                except OSError:
                    pass
            return jsonify({"error": "Queue temporarily unavailable"}), 503
        return jsonify({"message": "Queued"}), 200

    except Exception as e:
        logger.log_sync("error", f"[QueueAcceptor] Error: {e}")
        return jsonify({"error": str(e)}), 500


@webhook_bp.post("/submit")
@rate_limit(limit=100, period=timedelta(seconds=1))
async def submit_data():
    return await webhook_data()


@webhook_bp.post("/webhook")
@rate_limit(limit=100, period=timedelta(seconds=1))
async def webhook_data():
    if _is_mirrored_request() and not _accepts_mirrored():
        # 200, not 4xx: the Worker fires the mirror and never reads the result,
        # so there is nobody to tell. What matters is that we do not process it.
        return jsonify({"message": "Ignored"}), 200
    if _QUEUE_MODE:
        return await _queue_webhook_request()
    import time
    req_start = time.perf_counter()
    return await _process_webhook_request(req_start)


async def _process_webhook_request(req_start):
    import time
    # Function-level import: a top-level `from data.submissions.common import
    # SubmissionResponse` is circular when this module is reached VIA
    # data.submissions.common (bots.webhook_bot -> data.submissions -> common
    # -> api.core -> api/__init__ -> this module) while common is still
    # partially initialized. By call time all modules are fully loaded.
    from data.submissions.common import SubmissionResponse
    from data.submissions.raid_dedupe import (
        duplicate_reject_message,
        flag_multipath_loot_duplicates,
        flag_raid_reloot_duplicates,
    )

    success = False
    request_type = "webhook"
    submission_type = None
    db_session = None

    def log_phase(phase_name):
        elapsed = (time.perf_counter() - req_start) * 1000
        if elapsed > 500:
            print(f"[WebhookPhase] {phase_name}: {elapsed:.0f}ms")

    try:
        # Default request subtype for timing logs.
        g.submission_type = "webhook"
        content_type = request.headers.get('Content-Type', '')
        if 'multipart/form-data' in content_type:
            try:
                form = await request.form
                log_phase("form_parsed")
                
                payload_json = form.get('payload_json')
                if not payload_json:
                    return jsonify({"error": "No payload_json found in form data"}), 400

                import json
                webhook_payload = json.loads(payload_json)
                if webhook_payload is None:
                    return jsonify({"error": "Invalid JSON in payload_json"}), 400

                files = await request.files
                log_phase("files_read")
                
                image_file = files.get('file') if files else None

                processed_items = await process_webhook_data(webhook_payload)
                if not processed_items:
                    return jsonify({"error": "Could not process webhook data"}), 400

                # Re-looted raid chest defense (data/submissions/raid_dedupe.py):
                # fingerprint the payload's raid drop bundle before dispatch so
                # a second opening of the same reward chest (bank collection
                # chest, older plugin builds) is rejected, not double-counted.
                flag_raid_reloot_duplicates(processed_items)
                # Multi-part boss defense (same module): one kill of the
                # Grotesque Guardians et al. reaches intake through two loot
                # events on pre-5.4.0 clients, under two names with two GUIDs.
                flag_multipath_loot_duplicates(processed_items)

                submission_type = processed_items[0].get("type")
                g.submission_type = submission_type or "webhook"
                processed_items[0]["downloaded"] = False

                response = None
                db_session = get_db_session()
                log_phase("session_acquired")
                try:
                    downloaded = False
                    file_path = None
                    for processed_data in processed_items:
                        submission_type = processed_data.get("type")
                        g.submission_type = submission_type or g.submission_type
                        processed_data["downloaded"] = False
                        world_type = _normalize_world_type(processed_data.get("world_type"))
                        processed_data["world_type"] = world_type

                        # Flagged duplicate bundle (re-looted raid chest, or a
                        # multi-part boss kill arriving down a second loot
                        # event): reject instead of processing — a second Drop
                        # row would double GP, credit a phantom KC and score
                        # event tasks twice.
                        dup_message = duplicate_reject_message(processed_data)
                        if (_normalize_submission_type(submission_type) == "drop"
                                and dup_message):
                            response = SubmissionResponse(False, dup_message)
                            _mark_submission_outcome(processed_data, "drop", response)
                            continue

                        if world_type == "seasonal":
                            if not is_seasonal_active():
                                # Global kill switch (admin panel): skip seasonal
                                # processing entirely between seasons.
                                response = SubmissionResponse(
                                    False, "Seasonal processing is currently disabled."
                                )
                                _mark_submission_outcome(
                                    processed_data,
                                    _normalize_submission_type(submission_type),
                                    response,
                                )
                                continue
                            response = await _dispatch_seasonal_submission(
                                _normalize_submission_type(submission_type),
                                processed_data,
                                db_session,
                            )
                            db_session.commit()
                            _mark_submission_outcome(
                                processed_data,
                                _normalize_submission_type(submission_type),
                                response,
                            )
                            continue
                        elif world_type != MAIN_WORLD_TYPE:
                            continue

                        if image_file:
                            processed_data["has_image"] = True
                            player_name = processed_data.get('player', processed_data.get('player_name', None))
                            player = db_session.query(Player).filter(Player.player_name == player_name).first()
                            log_phase("player_lookup")
                            player_wom_id = player.wom_id if player else None
                            if player:
                                file_path = await download_image(
                                    sub_type=processed_data.get('type', 'unknown'),
                                    player=player,
                                    player_wom_id=player_wom_id,
                                    file_data=image_file,
                                    processed_data=processed_data
                                )
                                log_phase("image_downloaded")
                                if file_path:
                                    if processed_data.get("image_path"):
                                        file_path = processed_data["image_path"]
                                    processed_data["image_url"] = file_path
                                    processed_data["downloaded"] = True
                                    downloaded = True
                        else:
                            if file_path:
                                if processed_data.get("image_path"):
                                    processed_data["image_url"] = processed_data["image_path"]
                                else:
                                    processed_data["image_url"] = file_path
                        processed_data['used_api'] = True

                        try:
                            # Link video upload if video_key is present in the embed data
                            await _link_video_to_submission(processed_data, db_session)
                            raw_submission_type = submission_type
                            submission_type = normalize_submission_type(submission_type)
                            if submission_type not in SUPPORTED_TYPES:
                                g.submission_type = submission_type or "webhook"
                                # Unknown type: acknowledge with 200 so no plugin
                                # build retry-loops, but record the truth instead
                                # of a fake success.
                                logger.log_sync(
                                    "warning",
                                    f"Unsupported submission type received: {raw_submission_type!r}",
                                )
                                response = SubmissionResponse(
                                    False, f"Unsupported submission type: {raw_submission_type}"
                                )
                                _mark_submission_outcome(processed_data, submission_type, response)
                                continue

                            g.submission_type = submission_type
                            if submission_type == "drop":
                                _drop_request_debug(
                                    "dispatch_to_drop_processor "
                                    f"guid={processed_data.get('guid')} "
                                    f"player_name={processed_data.get('player_name') or processed_data.get('player')} "
                                    f"players_included_type={type(processed_data.get('players_included')).__name__} "
                                    f"players_included={processed_data.get('players_included')} "
                                    f"nearby_players_type={type(processed_data.get('nearby_players')).__name__} "
                                    f"nearby_players={processed_data.get('nearby_players')}"
                                )
                            elif submission_type == "adventure_log":
                                print(f"Got adventure log data: {processed_data}")

                            response = await dispatch_submission(
                                submission_type, processed_data, db_session
                            )
                            log_phase(f"{submission_type}_processed")
                            if submission_type == "drop":
                                # After creating the drop, link the video to the Drop row
                                await _try_attach_video_url_to_drop(processed_data, db_session)
                            db_session.commit()
                            log_phase("committed")
                            _mark_submission_outcome(processed_data, submission_type, response)
                        except Exception:
                            db_session.rollback()
                            raise

                    success = True
                except Exception as processor_error:
                    logger.log_sync("error", f"Processor error inside {submission_type} processor: {processor_error}")
                    # 500 (not 200): the plugin treats any 2xx as delivered and
                    # never retries; processor failures may be transient, so let
                    # the client's retry/backoff logic engage.
                    return jsonify({"error": f"Error processing data: {str(processor_error)}"}), 500
                finally:
                    if db_session:
                        db_session.close()
                        db_session = None

                # HTTP status stays 200 for rejections: the plugin's retry logic
                # must NOT engage for definitive rejects (duplicates, failed auth).
                # The "status" field is additive — legacy builds ignore it.
                if response:
                    accepted = getattr(response, "success", True) is not False
                    return jsonify({
                        "message": response.message,
                        "notice": response.notice,
                        "status": "accepted" if accepted else "rejected",
                    }), 200
                else:
                    return jsonify({
                        "message": "Webhook data processed successfully",
                        "status": "accepted",
                    }), 200

            except Exception as e:
                logger.log_sync("error", f"Error processing multipart request: {e}")
                return jsonify({"error": f"Error processing request: {str(e)}"}), 400
        else:
            # No client ships data here as JSON — the plugin always posts
            # multipart. Previously this branch acknowledged (and discarded)
            # arbitrary JSON with a fake success; be honest instead.
            logger.log_sync("warning", "Rejected non-multipart POST to /webhook")
            return jsonify({"error": "Expected multipart/form-data with a payload_json field"}), 400
    except Exception as e:
        logger.log_sync("error", f"Webhook Exception: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if db_session:
            try:
                db_session.close()
            except Exception:
                pass
        reset_db_connections()
        if submission_type:
            metrics.record_request(submission_type, success, app="new_api")
        else:
            metrics.record_request(request_type, success, app="new_api")


async def process_webhook_data(webhook_data):
    try:
        embeds = webhook_data.get("embeds", [])
        if not embeds:
            print("No embeds found in webhook data")
            return None
        processed_items = []
        for embed in embeds:
            processed_data = {
                field["name"]: field["value"] for field in embed.get("fields", [])
            }
            if not processed_data.get("timestamp"):
                processed_data["timestamp"] = int(datetime.now().timestamp())
            # Carry the server's accept time down from the ENVELOPE onto each
            # embed's processed data. processed_data is rebuilt purely from
            # embed["fields"], so top-level payload keys are otherwise dropped
            # here — which left data.submissions.common.received_at() reading
            # None and silently falling back to worker-pickup time, making the
            # month-boundary fix inert from the day it shipped. Without this,
            # a queue backlog draining across midnight still books kills into
            # the wrong month.
            if webhook_data.get("_received_at"):
                processed_data["_received_at"] = webhook_data["_received_at"]
            processed_data["world_type"] = _normalize_world_type(processed_data.get("world_type"))
            submission_type = str(processed_data.get("type", "")).lower()
            if submission_type in ("drop", "npc", "other"):
                raw_nearby_players = processed_data.get("nearby_players")
                raw_players_included = processed_data.get("players_included")
                # Since ~5.3 (NearbyPlayerTracker, plugin commit 84356cc) the
                # RuneLite plugin sends the participant list as the
                # "nearby_players" embed field and omits it entirely when
                # nobody is nearby. "members" (comma-separated, literal "none"
                # when empty) is the legacy pre-5.3 field name, kept as a
                # compatibility alias — see services/split_observer.py for the
                # version history.
                raw_members = processed_data.get("members")
                raw_participants = raw_nearby_players
                if raw_participants is None:
                    raw_participants = raw_players_included
                if raw_participants is None:
                    raw_participants = raw_members
                normalized_players = _parse_nearby_players(raw_participants)
                processed_data["players_included"] = normalized_players
                processed_data["nearby_players"] = normalized_players
                _drop_request_debug(
                    "parsed_embed_fields "
                    f"guid={processed_data.get('guid')} "
                    f"player_name={processed_data.get('player_name') or processed_data.get('player')} "
                    f"item_name={processed_data.get('item_name') or processed_data.get('item')} "
                    f"source={processed_data.get('source') or processed_data.get('npc_name')} "
                    f"raw_players_included={raw_players_included} "
                    f"raw_nearby_players={raw_nearby_players} "
                    f"raw_members={raw_members} "
                    f"normalized_players_included={normalized_players}"
                )
            processed_items.append(processed_data)
        return processed_items
    except Exception as e:
        print(f"Error processing webhook data: {e}")
        return None


# ==============================================================================
# MANUAL SUBMISSION ENDPOINT
# ==============================================================================
# This endpoint is intended for manual submissions from the front-end web server.
# Authentication is handled by the PHP front-end before the request reaches here.
#
# SUPPORTS TWO REQUEST FORMATS:
#
# 1. JSON POST (Content-Type: application/json) - when no image is uploaded:
# {
#     "submission_type": string,  // Required. One of: "drop", "collection_log", 
#                                 //   "personal_best", "combat_achievement", "pet"
#     "player_name": string,      // Required. The player's OSRS username.
#     "image_url": string|null,   // Optional. URL to an already-uploaded image.
#     ... (type-specific fields below)
# }
#
# 2. MULTIPART FORM DATA - when an image file is uploaded:
#    - All fields sent as form fields
#    - Image sent as "image_file" field (CURLFile in PHP)
# "world_type": string|null,  // Optional. Defaults to "main" when omitted.
#
# === DROP SUBMISSION FIELDS ===
# submission_type: "drop"
# "item_name": string,        // Required. Name of the dropped item.
# "item_id": int|null,        // Optional. OSRS item ID if known.
# "npc_name": string,         // Required. Name of the NPC that dropped the item.
# "value": int,               // Required. GE value of the drop in GP.
# "quantity": int,            // Optional. Number of items dropped (default: 1).
# "kill_count": int|null,     // Optional. Kill count at time of drop.
# "nearby_players": list|string|null, // Optional. List of nearby player names for
#                             //   point splits. Accepts a JSON array, a
#                             //   comma-separated string, or a list. Same field
#                             //   name the RuneLite plugin sends since ~5.3
#                             //   (omitted entirely when empty). Aliases:
#                             //   "players_included", and legacy "members"
#                             //   (pre-5.3 plugin field; sent the literal
#                             //   string "none" when empty).
#
# === COLLECTION LOG SUBMISSION FIELDS ===
# submission_type: "collection_log"
# "item_name": string,        // Required. Name of the collection log item.
# "kc": int|null,             // Optional. Kill count when item was obtained.
# "reported_slots": int|null, // Optional. Number of collection log slots filled.
#
# === PERSONAL BEST SUBMISSION FIELDS ===
# submission_type: "personal_best"
# "boss_name": string,        // Required. Name of the boss (alias: "npc_name").
# "time_ms": int,             // Required. Kill time in milliseconds.
# "is_pb": bool,              // Optional. Whether this is a new personal best (default: true).
# "team_size": int,           // Optional. Team size for raids (default: 1).
#
# === COMBAT ACHIEVEMENT SUBMISSION FIELDS ===
# submission_type: "combat_achievement"
# "task": string,             // Required. Name of the combat achievement task.
# "tier": string,             // Required. Tier: easy/medium/hard/elite/master/grandmaster.
# "points": int,              // Required. Points awarded for this task.
# "total_points": int,        // Required. Player's total CA points after completion.
# "completed": string|null,   // Optional. Tier milestone completed (e.g., "elite").
#
# === PET SUBMISSION FIELDS ===
# submission_type: "pet"
# "pet_name": string,         // Required. Name of the pet obtained.
# "source": string|null,      // Optional. Source NPC/activity name.
# "killcount": int|null,      // Optional. Kill count when pet was obtained.
# "duplicate": bool,          // Optional. Whether this is a duplicate pet (default: false).
# "milestone": string|null,   // Optional. Milestone info (e.g., "500 KC").
#
# RESPONSE FORMAT:
# Success: { "success": true, "message": string, "notice": string|null }
# Error:   { "success": false, "error": string }
#
# HTTP Status Codes:
# 200 - Success (also used for processor errors to allow error message display)
# 400 - Bad request (missing required fields, invalid submission_type)
# 500 - Internal server error
# ==============================================================================

def _parse_nearby_players(raw):
    """
    Accept nearby_players in multiple formats and return a list of player-name
    strings (or None when empty / absent).

    Supported inputs:
      - None / empty            -> None
      - list of strings         -> returned as-is (after stripping blanks)
      - JSON-encoded array str  -> parsed then returned
      - comma-separated string  -> split and stripped

    The plugin sends the literal string "none" when no players are nearby;
    a lone "none" entry is treated as empty.
    """
    def finalize(cleaned):
        if cleaned and len(cleaned) == 1 and cleaned[0].lower() == "none":
            return None
        return cleaned or None

    if raw is None:
        return None
    if isinstance(raw, list):
        cleaned = [str(n).strip() for n in raw if n and str(n).strip()]
        return finalize(cleaned)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        if raw.startswith("["):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    cleaned = [str(n).strip() for n in parsed if n and str(n).strip()]
                    return finalize(cleaned)
            except (_json.JSONDecodeError, TypeError):
                pass
        cleaned = [p.strip() for p in raw.split(",") if p.strip()]
        return finalize(cleaned)
    return None


def _image_naming_hints(submission_type, data):
    """Keys ``download_image`` reads to name a manual submission's screenshot.

    The intake payload is only merged into ``processed_data`` inside the
    per-type ``match`` block, which runs *after* the upload is saved — so
    without these hints every manual image landed in ``.../drop/unknown/`` as
    ``unknown_unknown.png``. Only non-empty values are returned: the naming
    code defaults on key *absence*, and a present ``None`` stringifies to the
    literal "None".
    """
    if submission_type == "drop":
        npc_name = data.get("npc_name")
        hints = {"source": npc_name, "npc_name": npc_name, "item": data.get("item_name")}
    elif submission_type == "collection_log":
        hints = {
            "source": data.get("source") or data.get("npc_name"),
            "item": data.get("item_name"),
        }
    elif submission_type == "personal_best":
        boss_name = data.get("boss_name") or data.get("npc_name")
        hints = {
            "boss_name": boss_name,
            "npc_name": boss_name,
            "team_size": data.get("team_size"),
            "time": data.get("time_ms"),
        }
    elif submission_type == "combat_achievement":
        hints = {"task_name": data.get("task"), "task_tier": data.get("tier")}
    elif submission_type == "pet":
        hints = {"source": data.get("source"), "item": data.get("pet_name")}
    else:
        hints = {}
    return {k: v for k, v in hints.items() if v not in (None, "")}


MANUAL_SUBMIT_KEY_HEADER = "X-DT-Manual-Key"


def _manual_submit_auth_error():
    """Shared-secret gate for ``/manual-submit`` (server-to-server only).

    The endpoint substitutes the target player's real ``account_hash`` into the
    payload, so without a gate anyone who knows an RSN could forge submissions
    as that player. Legitimate callers (``web_api/routes/submissions.py``,
    which enforces session auth + player ownership) must send the shared
    secret in the ``X-DT-Manual-Key`` header.

    Same precedent as the ``XF_KEY`` auth in ``api/routes/group_create.py``:
    fail closed when the secret is unconfigured (503), reject a missing/wrong
    header (401). ``MANUAL_SUBMIT_KEY`` is read at request time so tests can
    monkeypatch it. Returns a ``(response, status)`` tuple to short-circuit
    with, or ``None`` when the request is authorized.
    """
    expected = (os.getenv("MANUAL_SUBMIT_KEY") or "").strip()
    if not expected:
        # Fail closed: if no secret is configured the endpoint is disabled.
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Manual submissions are not configured on the server.",
                }
            ),
            503,
        )
    provided = (request.headers.get(MANUAL_SUBMIT_KEY_HEADER) or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify({"success": False, "error": "Unauthorized."}), 401
    return None


@webhook_bp.post("/manual-submit")
@rate_limit(limit=10, period=timedelta(seconds=1))
async def manual_submit():
    """
    Handle manual submissions forwarded by the website backend.

    This endpoint processes manually-submitted data for drops, collection log entries,
    personal bests, combat achievements, and pets. Unlike the webhook endpoint:
    - No account hash is required (the caller must present the shared
      ``X-DT-Manual-Key`` secret; user auth + player ownership are enforced
      upstream by ``web_api/routes/submissions.py``)
    - A unique submission ID is generated automatically
    - The submission is marked as using the API and considered pre-authenticated
    """
    auth_error = _manual_submit_auth_error()
    if auth_error is not None:
        return auth_error
    import time
    req_start = time.perf_counter()
    return await _process_manual_submission(req_start)


async def _process_manual_submission(req_start):
    import time
    
    success = False
    submission_type = None
    db_session = None
    
    def log_phase(phase_name):
        elapsed = (time.perf_counter() - req_start) * 1000
        if elapsed > 100:
            print(f"[ManualSubmitPhase] {phase_name}: {elapsed:.0f}ms")
    
    try:
        # Determine content type and parse accordingly
        content_type = request.headers.get('Content-Type', '')
        image_file = None
        
        if 'multipart/form-data' in content_type:
            # Handle multipart form data (when image file is uploaded)
            try:
                form = await request.form
                files = await request.files
                log_phase("form_parsed")
                
                # Extract form fields into a dict, converting types as needed
                data = {}
                for key in form:
                    value = form[key]
                    # Convert string representations of booleans
                    if value.lower() == 'true':
                        data[key] = True
                    elif value.lower() == 'false':
                        data[key] = False
                    # Try to convert numeric strings
                    elif value.isdigit() or (value.startswith('-') and value[1:].isdigit()):
                        data[key] = int(value)
                    else:
                        data[key] = value if value else None
                
                # Get the uploaded image file
                image_file = files.get('image_file') if files else None
                log_phase("files_read")
                
            except Exception as e:
                logger.log_sync("error", f"Error parsing manual submission multipart form: {e}")
                return jsonify({"success": False, "error": f"Invalid form data: {str(e)}"}), 400
        else:
            # Handle JSON request body
            try:
                data = await request.get_json()
                if not data:
                    return jsonify({"success": False, "error": "No JSON data provided"}), 400
                log_phase("json_parsed")
            except Exception as e:
                logger.log_sync("error", f"Error parsing manual submission JSON: {e}")
                return jsonify({"success": False, "error": "Invalid JSON data"}), 400
        
        # Validate required fields
        submission_type = data.get("submission_type")
        player_name = data.get("player_name")
        
        if not submission_type:
            return jsonify({"success": False, "error": "Missing required field: submission_type"}), 400
        if not player_name:
            return jsonify({"success": False, "error": "Missing required field: player_name"}), 400
        
        # Normalize submission type
        submission_type = str(submission_type).lower().strip()
        valid_types = ["drop", "collection_log", "personal_best", "combat_achievement", "pet"]
        if submission_type not in valid_types:
            return jsonify({
                "success": False, 
                "error": f"Invalid submission_type: {submission_type}. Must be one of: {', '.join(valid_types)}"
            }), 400

        world_type = _normalize_world_type(data.get("world_type"))
        if world_type != MAIN_WORLD_TYPE:
            # Manual submissions are main-world only; seasonal/league data must
            # arrive via the plugin webhook path so it carries a real account
            # hash for identity verification.
            success = True
            return jsonify({
                "success": True,
                "status": "ignored",
                "message": f"Ignored {submission_type} submission for world_type '{world_type}'"
            }), 200
        
        # Generate a unique ID for this submission
        unique_id = str(uuid.uuid4())
        
        # Build processed data for the appropriate processor
        # Use a placeholder account hash that will pass the auth check
        placeholder_hash = f"manual_submission_{unique_id}"
        
        processed_data = {
            "player_name": player_name,
            "player": player_name,
            "acc_hash": placeholder_hash,
            "guid": unique_id,
            "used_api": True,
            # Intake-path marker: downstream processors use this for the
            # per-group manual-submission policy (suggestion #45) and to
            # present manual submissions as non-plugin to the events engine.
            # NOT "source" — that payload key already means the NPC/killer in
            # the drop/clog/pet/death processors.
            "intake_source": "manual",
            "downloaded": False,
            "world_type": world_type,
            "image_url": data.get("image_url"),
            "timestamp": datetime.now().isoformat(),
        }
        
        # Acquire database session
        db_session = get_db_session()
        log_phase("session_acquired")
        
        # Look up the player to set the correct account hash for auth
        player = db_session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        log_phase("player_lookup")
        
        if not player:
            return jsonify({
                "success": False, 
                "error": f"Player '{player_name}' not found in database. The player must have submitted at least one drop through the RuneLite plugin first."
            }), 400
        
        # Use the player's existing account hash so authentication passes
        processed_data["acc_hash"] = player.account_hash or placeholder_hash
        
        # Handle image file upload if present
        if image_file:
            processed_data["has_image"] = True
            player_wom_id = player.wom_id if player else None
            try:
                # download_image names the file from the payload and writes the
                # public URL back as "image_path"; hand it a copy carrying the
                # not-yet-merged per-type fields so nothing downstream sees the
                # hint keys (e.g. "source" means different things per processor).
                naming = {**processed_data, **_image_naming_hints(submission_type, data)}
                file_path = await download_image(
                    sub_type=submission_type,
                    player=player,
                    player_wom_id=player_wom_id,
                    file_data=image_file,
                    processed_data=naming
                )
                log_phase("image_downloaded")
                if file_path:
                    # Store the URL the file is served at, never the filesystem
                    # path — the site and Discord embeds render this verbatim,
                    # so a /store/... path 404s (mirrors the plugin path above).
                    processed_data["image_url"] = naming.get("image_path") or file_path
                    processed_data["downloaded"] = True
            except Exception as img_error:
                logger.log_sync("error", f"Error downloading manual submission image: {img_error}")
                # Continue without image - don't fail the submission
        
        response = None
        
        try:
            match submission_type:
                case "drop":
                    # Validate drop-specific required fields
                    item_name = data.get("item_name")
                    npc_name = data.get("npc_name")
                    # 0 = "unknown": get_true_item_value() recovers the real GE
                    # price for 0gp values, so a web submitter can omit it.
                    value = data.get("value") or 0
                    quantity = data.get("quantity") or 1

                    if not item_name:
                        return jsonify({"success": False, "error": "Drop submission requires 'item_name'"}), 400
                    if not npc_name:
                        return jsonify({"success": False, "error": "Drop submission requires 'npc_name'"}), 400
                    
                    nearby_players = _parse_nearby_players(
                        data.get("nearby_players") or data.get("players_included") or data.get("members")
                    )
                    
                    processed_data.update({
                        "type": "drop",
                        "item_name": item_name,
                        "item": item_name,
                        "item_id": data.get("item_id"),
                        "npc_name": npc_name,
                        "value": int(value),
                        "quantity": int(quantity),
                        "kill_count": data.get("kill_count", data.get("killcount")),
                        "players_included": nearby_players,
                        # Party size for split GP tracking, receiver included.
                        # Carries shares taken by people we can't credit, so the
                        # divisor matches the real split (see drop_processor).
                        "split_size": data.get("split_size"),
                    })
                    response = await submissions.drop_processor(processed_data, external_session=db_session)
                    log_phase("drop_processed")
                
                case "collection_log":
                    item_name = data.get("item_name")
                    
                    if not item_name:
                        return jsonify({"success": False, "error": "Collection log submission requires 'item_name'"}), 400
                    
                    processed_data.update({
                        "type": "collection_log",
                        "item_name": item_name,
                        "item": item_name,
                        "kc": data.get("kc"),
                        "reported_slots": data.get("reported_slots"),
                        # clog_processor reads the unlock's NPC from "source"
                        # (payload convention); accept npc_name as an alias so
                        # the web form's field isn't silently dropped.
                        "source": data.get("source") or data.get("npc_name"),
                    })
                    response = await submissions.clog_processor(processed_data, external_session=db_session)
                    log_phase("clog_processed")
                
                case "personal_best":
                    boss_name = data.get("boss_name") or data.get("npc_name")
                    time_ms = data.get("time_ms")
                    
                    if not boss_name:
                        return jsonify({"success": False, "error": "Personal best submission requires 'boss_name' or 'npc_name'"}), 400
                    if time_ms is None:
                        return jsonify({"success": False, "error": "Personal best submission requires 'time_ms'"}), 400
                    
                    # Handle is_pb - could be bool, string "true"/"false", or int 1/0
                    is_pb_raw = data.get("is_pb", True)
                    if isinstance(is_pb_raw, str):
                        is_pb = is_pb_raw.lower() in ('true', '1', 'yes')
                    elif isinstance(is_pb_raw, int):
                        is_pb = bool(is_pb_raw)
                    else:
                        is_pb = bool(is_pb_raw)
                    
                    processed_data.update({
                        "type": "personal_best",
                        "npc_name": boss_name,
                        "boss_name": boss_name,
                        "kill_time": int(time_ms),
                        "current_time_ms": int(time_ms),
                        "personal_best_ms": int(time_ms),
                        "is_pb": is_pb,
                        "is_new_pb": is_pb,
                        "team_size": int(data.get("team_size") or 1),
                    })
                    response = await submissions.pb_processor(processed_data, external_session=db_session)
                    log_phase("pb_processed")
                
                case "combat_achievement":
                    task = data.get("task")
                    tier = data.get("tier")
                    points = data.get("points")
                    total_points = data.get("total_points")

                    if not task:
                        return jsonify({"success": False, "error": "Combat achievement submission requires 'task'"}), 400
                    if not tier:
                        return jsonify({"success": False, "error": "Combat achievement submission requires 'tier'"}), 400
                    # The plugin reports the client's live point counters; a web
                    # submitter can't know them, so derive the task's points
                    # from its tier (the in-game award scale) and treat the
                    # running total as unknown.
                    _TIER_POINTS = {"easy": 1, "medium": 2, "hard": 3, "elite": 4, "master": 5, "grandmaster": 6}
                    if points is None:
                        points = _TIER_POINTS.get(str(tier).strip().lower(), 1)
                    if total_points is None:
                        total_points = 0

                    processed_data.update({
                        "type": "combat_achievement",
                        "player_name": player_name,
                        "task": task,
                        "tier": str(tier).lower(),
                        "points": int(points),
                        "total_points": int(total_points),
                        "completed": data.get("completed"),
                    })
                    response = await submissions.ca_processor(processed_data, external_session=db_session)
                    log_phase("ca_processed")
                
                case "pet":
                    pet_name = data.get("pet_name")
                    
                    if not pet_name:
                        return jsonify({"success": False, "error": "Pet submission requires 'pet_name'"}), 400
                    
                    # Handle duplicate boolean
                    duplicate_raw = data.get("duplicate", False)
                    if isinstance(duplicate_raw, str):
                        duplicate = duplicate_raw.lower() in ('true', '1', 'yes')
                    elif isinstance(duplicate_raw, int):
                        duplicate = bool(duplicate_raw)
                    else:
                        duplicate = bool(duplicate_raw)
                    
                    processed_data.update({
                        "type": "pet",
                        "pet_name": pet_name,
                        "source": data.get("source"),
                        "killcount": data.get("killcount"),
                        "duplicate": duplicate,
                        "milestone": data.get("milestone"),
                        "previously_owned": data.get("previously_owned"),
                        "game_message": data.get("game_message"),
                    })
                    response = await submissions.pet_processor(processed_data, external_session=db_session)
                    log_phase("pet_processed")
            
            db_session.commit()
            log_phase("committed")
            success = True

            if response:
                # Reflect the processor's actual verdict: a rejection (duplicate,
                # failed auth, unknown item/NPC) is not a success even though the
                # request itself completed.
                return jsonify({
                    "success": bool(response.success),
                    "status": "accepted" if response.success else "rejected",
                    "message": response.message,
                    "notice": response.notice
                }), 200
            else:
                return jsonify({
                    "success": True,
                    "status": "accepted",
                    "message": f"Manual {submission_type} submission processed successfully"
                }), 200
        
        except Exception as processor_error:
            logger.log_sync("error", f"Manual submission processor error: {processor_error}")
            if db_session:
                db_session.rollback()
            return jsonify({
                "success": False,
                "error": f"Error processing submission: {str(processor_error)}"
            }), 200
    
    except Exception as e:
        logger.log_sync("error", f"Manual submission exception: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    
    finally:
        if db_session:
            try:
                db_session.close()
            except Exception:
                pass
        reset_db_connections()
        if submission_type:
            metrics.record_request(f"manual_{submission_type}", success, app="new_api")
        else:
            metrics.record_request("manual_submit", success, app="new_api")
