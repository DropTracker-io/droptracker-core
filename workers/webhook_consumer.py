import asyncio
import json
import logging
import os
import signal
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis as _redis
from dotenv import load_dotenv

load_dotenv()

from utils.sentry import init_sentry

init_sentry("droptracker-webhook-consumer")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)
log = logging.getLogger("webhook_consumer")

QUEUE_KEY = "webhook:queue"
# Entries that raise out of _process_entry land here instead of vanishing —
# BLPOP has already removed them from the queue, so this is the only copy.
DEAD_KEY = "webhook:dead"
DEAD_MAX = 10_000
BLPOP_TIMEOUT = 5
# Concurrency model: N worker coroutines on one event loop, each running the
# same blpop -> _process_entry loop. The worker count IS the concurrency
# bound; every in-flight entry has its own SQLAlchemy session (Session() is a
# plain sessionmaker) exactly like bots/webhook_bot.py's concurrent message
# handling. The serial consumer's ~40k drops/hour ceiling put the queue hours
# behind at evening peak (2026-07-31: 107 min).
NUM_WORKERS = max(1, min(32, int(os.getenv("WEBHOOK_CONSUMER_WORKERS", "6"))))
TEMP_DIR = os.getenv("WEBHOOK_TEMP_DIR", "/tmp/webhook_uploads")
_REDIS_PW = os.getenv("DB_PASS")

# Entries currently being processed across all workers. Only mutated between
# awaits on the single event loop, so no lock is needed.
_in_flight = 0


def _get_redis():
    return _redis.Redis(host="127.0.0.1", port=6379, db=0, password=_REDIS_PW)


class _TempFileUpload:
    def __init__(self, file, filename, content_type):
        self.file = file
        self.filename = filename
        self.content_type = content_type


def _push_plugin_notice(db_session, processed_data, response) -> None:
    """Deliver the processor's `notice` to the player's plugin inbox.

    Queue mode acknowledges /webhook before processing, so the response
    `notice` field the plugin used to render as in-game chat never reaches it.
    This restores that channel via the notification inbox
    (services/plugin_notifications, type "submission_notice"). Best-effort.
    """
    try:
        notice = getattr(response, "notice", None) if response else None
        if not notice:
            return
        acc_hash = processed_data.get("acc_hash")
        if not acc_hash:
            return
        from db.models import Player
        from services.plugin_notifications import push_submission_notice

        player = (
            db_session.query(Player)
            .filter(Player.account_hash == str(acc_hash))
            .first()
        )
        if player:
            push_submission_notice(player.player_id, notice)
    except Exception as e:
        # A failed read leaves the shared session with a pending rollback;
        # clear it so the next entry in the batch isn't poisoned.
        try:
            db_session.rollback()
        except Exception:
            pass
        log.debug("plugin notice push failed: %s", e)


async def _process_entry(entry_bytes: bytes) -> None:
    from api.core import get_db_session
    from api.routes.webhook import (
        _dispatch_seasonal_submission,
        _link_video_to_submission,
        _mark_submission_outcome,
        _normalize_submission_type,
        _normalize_world_type,
        _try_attach_video_url_to_drop,
    )
    from data import submissions
    from data.submissions.common import SubmissionResponse
    from data.submissions.raid_dedupe import (
        RELOOT_FLAG,
        RELOOT_REJECT_MESSAGE,
        flag_raid_reloot_duplicates,
    )
    from db.models import Player
    from utils.download import download_image

    entry = json.loads(entry_bytes)
    payload = entry["payload"]
    image_tmp_path = entry.get("image_tmp_path")
    image_filename = entry.get("image_filename")
    image_content_type = entry.get("image_content_type")

    from api.routes.webhook import process_webhook_data
    processed_items = await process_webhook_data(payload)
    if not processed_items:
        log.warning("process_webhook_data returned nothing; skipping entry")
        return

    # Re-looted raid chest defense (see data/submissions/raid_dedupe.py):
    # fingerprint the payload's raid drop bundle before dispatch so a second
    # opening of the same reward chest (bank collection chest, older plugin
    # builds) is rejected instead of double-counted.
    reflagged = flag_raid_reloot_duplicates(processed_items)
    if reflagged:
        log.info("Flagged %d raid re-loot duplicate embed(s) in payload", reflagged)

    db_session = get_db_session()
    tmp_fh = None
    try:
        for processed_data in processed_items:
            submission_type = processed_data.get("type")
            world_type = _normalize_world_type(processed_data.get("world_type"))
            processed_data["world_type"] = world_type
            processed_data["downloaded"] = False
            processed_data["used_api"] = True

            if image_tmp_path and os.path.exists(image_tmp_path):
                processed_data["has_image"] = True
                player_name = processed_data.get("player") or processed_data.get("player_name")
                player = db_session.query(Player).filter(Player.player_name == player_name).first()
                player_wom_id = player.wom_id if player else None
                if player:
                    tmp_fh = open(image_tmp_path, "rb")
                    file_upload = _TempFileUpload(
                        file=tmp_fh,
                        filename=image_filename or "screenshot.jpg",
                        content_type=image_content_type or "image/jpeg",
                    )
                    file_path = await download_image(
                        sub_type=submission_type,
                        player=player,
                        player_wom_id=player_wom_id,
                        file_data=file_upload,
                        processed_data=processed_data,
                    )
                    if tmp_fh:
                        tmp_fh.close()
                        tmp_fh = None
                    if file_path:
                        if processed_data.get("image_path"):
                            file_path = processed_data["image_path"]
                        processed_data["image_url"] = file_path
                        processed_data["downloaded"] = True

            norm_type = _normalize_submission_type(submission_type)

            try:
                # Flagged re-looted raid bundle: reject instead of processing —
                # a second Drop row would double GP and credit a phantom KC.
                if norm_type == "drop" and processed_data.get(RELOOT_FLAG):
                    _mark_submission_outcome(
                        processed_data,
                        norm_type,
                        SubmissionResponse(False, RELOOT_REJECT_MESSAGE),
                    )
                    continue

                if world_type == "seasonal":
                    from services.seasonal_state import is_seasonal_active
                    if not is_seasonal_active():
                        # Global kill switch (admin panel): skip seasonal
                        # processing entirely between seasons.
                        _mark_submission_outcome(
                            processed_data,
                            norm_type,
                            SubmissionResponse(False, "Seasonal processing is currently disabled."),
                        )
                        continue
                    response = await _dispatch_seasonal_submission(norm_type, processed_data, db_session)
                    db_session.commit()
                    _mark_submission_outcome(processed_data, norm_type, response)
                    _push_plugin_notice(db_session, processed_data, response)
                    continue
                elif world_type != "main":
                    continue

                # Mirror the synchronous intake path: link any uploaded video to
                # this submission before dispatch, then (for drops) attach the
                # serving URL to the created Drop row afterwards. Without these
                # the queue path silently drops all video linkage.
                await _link_video_to_submission(processed_data, db_session)

                match norm_type:
                    case "drop":
                        response = await submissions.drop_processor(processed_data, external_session=db_session)
                        await _try_attach_video_url_to_drop(processed_data, db_session)
                    case "collection_log":
                        response = await submissions.clog_processor(processed_data, external_session=db_session)
                    case "personal_best":
                        response = await submissions.pb_processor(processed_data, external_session=db_session)
                    case "combat_achievement":
                        response = await submissions.ca_processor(processed_data, external_session=db_session)
                    case "experience":
                        response = await submissions.experience_processor(processed_data, external_session=db_session)
                    case "quest":
                        response = await submissions.quest_processor(processed_data, external_session=db_session)
                    case "death":
                        response = await submissions.death_processor(processed_data, external_session=db_session)
                    case "diary":
                        response = await submissions.diary_processor(processed_data, external_session=db_session)
                    case "pet":
                        response = await submissions.pet_processor(processed_data, external_session=db_session)
                    case "adventure_log":
                        response = await submissions.adventure_log_processor(processed_data, external_session=db_session)
                    case _:
                        log.warning("Unknown submission type %r; skipping", norm_type)
                        _mark_submission_outcome(
                            processed_data,
                            norm_type,
                            SubmissionResponse(False, f"Unsupported submission type: {norm_type}"),
                        )
                        continue

                db_session.commit()
                _mark_submission_outcome(processed_data, norm_type, response)
                _push_plugin_notice(db_session, processed_data, response)
            except Exception:
                db_session.rollback()
                raise
    finally:
        if tmp_fh:
            try:
                tmp_fh.close()
            except Exception:
                pass
        db_session.close()
        # NOTE: the serial consumer called api.core.reset_db_connections()
        # (scoped-session .remove()) here. With concurrent workers that would
        # tear down the shared scoped session under any sibling that touched
        # it, so cleanup now happens in _maintenance() only while idle.
        if image_tmp_path:
            try:
                os.remove(image_tmp_path)
            except FileNotFoundError:
                pass


def _dead_letter(r, entry_bytes: bytes) -> None:
    """Preserve a failed entry — it has already been popped from the queue."""
    try:
        pipe = r.pipeline()
        pipe.lpush(DEAD_KEY, entry_bytes)
        pipe.ltrim(DEAD_KEY, 0, DEAD_MAX - 1)
        pipe.execute()
    except Exception:
        log.error("Failed to dead-letter entry:\n%s", traceback.format_exc())


async def _worker(worker_id: int, r, stop: asyncio.Event) -> None:
    global _in_flight
    while not stop.is_set():
        entry_bytes = None
        try:
            result = await asyncio.to_thread(r.blpop, QUEUE_KEY, BLPOP_TIMEOUT)
            if result is None:
                continue
            _, entry_bytes = result
            _in_flight += 1
            try:
                await _process_entry(entry_bytes)
            finally:
                _in_flight -= 1
        except Exception:
            log.error("[worker %d] Error in consumer loop:\n%s", worker_id, traceback.format_exc())
            if entry_bytes is not None:
                _dead_letter(r, entry_bytes)
            await asyncio.sleep(1)
    log.info("[worker %d] stopped", worker_id)


async def _maintenance(r, stop: asyncio.Event) -> None:
    """Once a minute: log queue health; clean the scoped session while idle."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
            return
        except asyncio.TimeoutError:
            pass
        try:
            depth, dead = await asyncio.to_thread(
                lambda: (r.llen(QUEUE_KEY), r.llen(DEAD_KEY)))
            log.info("queue depth=%d dead=%d in_flight=%d workers=%d",
                     depth, dead, _in_flight, NUM_WORKERS)
            if _in_flight == 0:
                from api.core import reset_db_connections
                reset_db_connections()
        except Exception:
            log.debug("maintenance tick failed:\n%s", traceback.format_exc())


async def run_consumer() -> None:
    log.info("Webhook consumer starting (queue=%s, workers=%d)", QUEUE_KEY, NUM_WORKERS)
    r = await asyncio.to_thread(_get_redis)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signame: str) -> None:
        # Drain, don't drop: BLPOP'd entries are the only copy, so workers
        # finish their in-flight entry before exiting (well inside the unit's
        # TimeoutStopSec=30).
        log.info("Received %s — finishing in-flight entries", signame)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_stop, sig.name)

    tasks = [asyncio.create_task(_worker(i, r, stop)) for i in range(NUM_WORKERS)]
    tasks.append(asyncio.create_task(_maintenance(r, stop)))
    await asyncio.gather(*tasks)
    log.info("Webhook consumer stopped cleanly")


if __name__ == "__main__":
    asyncio.run(run_consumer())
