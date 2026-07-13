"""Redis-backed per-submission processed markers.

The intake paths (api/routes/webhook.py sync path and workers/webhook_consumer.py
queue path) call mark_submission_processed() once a submission's DB transaction
commits. The plugin-facing /check endpoint reads these markers so the side panel
shows real processing status instead of the old always-processed stub.

Markers are best-effort: a Redis hiccup must never fail intake, and /check has a
poll-count fallback for submissions that never get a marker (rejected duplicates,
unauthorized players, pre-deploy submissions).
"""

import json
import time

from utils.redis import RedisClient

STATUS_KEY_PREFIX = "submission:status:"
STATUS_TTL_SECONDS = 24 * 3600

_redis = RedisClient()


def _key(guid) -> str:
    return f"{STATUS_KEY_PREFIX}{guid}"


def mark_submission_processed(guid, submission_type=None):
    """Record that the submission with this guid finished processing."""
    if not guid:
        return
    try:
        payload = json.dumps({
            "status": "processed",
            "type": submission_type,
            "ts": int(time.time()),
        })
        _redis.client.setex(_key(guid), STATUS_TTL_SECONDS, payload)
    except Exception:
        pass


def mark_submission_rejected(guid, submission_type=None, reason=None):
    """Record that the submission with this guid was definitively rejected
    (duplicate, failed auth, unknown item/NPC, unsupported type).

    /check reports these with processed=true so legacy plugin builds stop
    polling exactly as before; newer builds can read status=="rejected" and
    the reason string to surface the real outcome.
    """
    if not guid:
        return
    try:
        payload = json.dumps({
            "status": "rejected",
            "type": submission_type,
            "reason": (str(reason)[:300] if reason else None),
            "ts": int(time.time()),
        })
        _redis.client.setex(_key(guid), STATUS_TTL_SECONDS, payload)
    except Exception:
        pass


def get_submission_statuses(guids):
    """Return {guid: status_dict_or_None} for a list of guids in one round-trip."""
    guids = list(guids)
    result = {g: None for g in guids}
    if not guids:
        return result
    try:
        pipe = _redis.client.pipeline(transaction=False)
        for g in guids:
            pipe.get(_key(g))
        for g, raw in zip(guids, pipe.execute()):
            if not raw:
                continue
            try:
                result[g] = json.loads(raw)
            except Exception:
                result[g] = {"status": "processed"}
    except Exception:
        pass
    return result
