"""Hardcore Worlds intake: archive every submission before it is processed.

``data/submissions/dispatch.py`` routes every ``world_type="hardcore"``
submission here first (the plugin sends ``HARDCORE_MAIN``; dispatch folds it),
then on to the normal processors with ``world_type="hardcore"`` for the types
that have hardcore mirror tables. See ``db/models/hardcore.py``.

The archive is the record of what arrived: a processor may refuse a
submission (an unknown item, an unresolvable NPC) and some types have no
hardcore processing yet (experience, adventure log, PvP), but nothing sent
from these worlds is lost.

Like every processor this does not commit: the caller owns the transaction.
"""
import json
import logging

from sqlalchemy.exc import IntegrityError

from db import HardcoreSubmission

from .common import SubmissionResponse, ensure_hardcore_player, received_at

log = logging.getLogger(__name__)

# Never stored. The auth key is a credential; the rest is intake plumbing.
_PAYLOAD_DROP_KEYS = frozenset({"auth_key", "downloaded", "has_image"})

DUPLICATE = "Duplicate hardcore submission"


def _clean_payload(data: dict) -> dict:
    """The submission as stored: JSON-safe, credential stripped."""
    kept = {k: v for k, v in data.items()
            if k not in _PAYLOAD_DROP_KEYS and not str(k).startswith("_")}
    # Round-trip so anything exotic (datetimes, bytes) is a string, not a
    # crash in the JSON column at flush time.
    return json.loads(json.dumps(kept, default=str))


async def archive_hardcore_submission(submission_type: str, data: dict, session):
    """Archive one hardcore-world submission of canonical ``submission_type``.

    Returns a SubmissionResponse: success when archived, a refusal when the
    character cannot be resolved or the GUID was archived before.
    """
    player_name = str(data.get("player_name", data.get("player")) or "").strip()
    player = ensure_hardcore_player(session, player_name, data.get("acc_hash"))
    if player is None:
        return SubmissionResponse(False, "Hardcore submission has no usable player or account hash")

    guid = data.get("guid") or data.get("unique_id")
    guid = str(guid)[:255] if guid else None
    if guid and session.query(HardcoreSubmission.id).filter(
            HardcoreSubmission.unique_id == guid).first():
        return SubmissionResponse(False, DUPLICATE)

    when = received_at(data)
    try:
        with session.begin_nested():
            session.add(HardcoreSubmission(
                player_id=player.player_id,
                submission_type=submission_type[:32],
                unique_id=guid,
                payload=_clean_payload(data),
                plugin_version=str(data["p_v"])[:20] if data.get("p_v") else None,
                date_added=when,
                partition=when.year * 100 + when.month,
            ))
            session.flush()
    except IntegrityError:
        # The same GUID landed from a second worker between our check and now.
        return SubmissionResponse(False, DUPLICATE)

    log.info("[Hardcore] archived %s for %s (player %s)", submission_type, player_name, player.player_id)
    return SubmissionResponse(True, f"Stored hardcore world {submission_type}")
