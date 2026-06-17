import asyncio
import json
import logging
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis as _redis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)
log = logging.getLogger("webhook_consumer")

QUEUE_KEY = "webhook:queue"
BLPOP_TIMEOUT = 5
TEMP_DIR = os.getenv("WEBHOOK_TEMP_DIR", "/tmp/webhook_uploads")
_REDIS_PW = os.getenv("DB_PASS")


def _get_redis():
    return _redis.Redis(host="127.0.0.1", port=6379, db=0, password=_REDIS_PW)


class _TempFileUpload:
    def __init__(self, file, filename, content_type):
        self.file = file
        self.filename = filename
        self.content_type = content_type


async def _process_entry(entry_bytes: bytes) -> None:
    from api.core import get_db_session, reset_db_connections
    from api.routes.webhook import (
        _dispatch_seasonal_submission,
        _normalize_submission_type,
        _normalize_world_type,
    )
    from data import submissions
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
                if world_type == "seasonal":
                    await _dispatch_seasonal_submission(norm_type, processed_data, db_session)
                    db_session.commit()
                    continue
                elif world_type != "main":
                    continue

                match norm_type:
                    case "drop":
                        await submissions.drop_processor(processed_data, external_session=db_session)
                    case "collection_log":
                        await submissions.clog_processor(processed_data, external_session=db_session)
                    case "personal_best":
                        await submissions.pb_processor(processed_data, external_session=db_session)
                    case "combat_achievement":
                        await submissions.ca_processor(processed_data, external_session=db_session)
                    case "experience":
                        await submissions.experience_processor(processed_data, external_session=db_session)
                    case "quest":
                        await submissions.quest_processor(processed_data, external_session=db_session)
                    case "pet":
                        await submissions.pet_processor(processed_data, external_session=db_session)
                    case "adventure_log":
                        await submissions.adventure_log_processor(processed_data, external_session=db_session)
                    case _:
                        log.warning("Unknown submission type %r; skipping", norm_type)
                        continue

                db_session.commit()
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
        reset_db_connections()
        if image_tmp_path:
            try:
                os.remove(image_tmp_path)
            except FileNotFoundError:
                pass


async def run_consumer() -> None:
    log.info("Webhook consumer starting (queue=%s)", QUEUE_KEY)
    r = await asyncio.to_thread(_get_redis)

    while True:
        try:
            result = await asyncio.to_thread(r.blpop, QUEUE_KEY, BLPOP_TIMEOUT)
            if result is None:
                continue
            _, entry_bytes = result
            await _process_entry(entry_bytes)
        except Exception:
            log.error("Error in consumer loop:\n%s", traceback.format_exc())
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run_consumer())
