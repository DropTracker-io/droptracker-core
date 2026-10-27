"""Hardcore Worlds intake: store every submission, touch nothing main-game.

``data/submissions/dispatch.py`` routes every ``world_type="hardcore"``
submission here (the plugin sends ``HARDCORE_MAIN``; dispatch folds it). See
``db/models/hardcore.py`` for why these worlds get their own tables.

This is storage only. Nothing here writes ``players``, ``drops`` or any other
main-game table, creates item/NPC catalog rows, prices anything (drops keep
the client's value), touches Redis, awards points, queues events or sends a
Discord message. Those can be built on the stored rows later.

Like every processor this does not commit: the caller owns the transaction.
"""
import json
import logging

from sqlalchemy.exc import IntegrityError

from db.models.hardcore import HardcoreDrop, HardcorePlayer, HardcoreSubmission
from utils.valuation_guard import exceeds_value_sanity_limit, sanitize_quantity

from .common import SubmissionResponse, received_at

log = logging.getLogger(__name__)

# Never stored. The auth key is a credential; the rest is intake plumbing.
_PAYLOAD_DROP_KEYS = frozenset({"auth_key", "downloaded", "has_image"})


def _clean_payload(data: dict) -> dict:
    """The submission as stored: JSON-safe, credential stripped."""
    kept = {k: v for k, v in data.items()
            if k not in _PAYLOAD_DROP_KEYS and not str(k).startswith("_")}
    # Round-trip so anything exotic (datetimes, bytes) is a string, not a
    # crash in the JSON column at flush time.
    return json.loads(json.dumps(kept, default=str))


def _int_or_none(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _partition(when) -> int:
    return when.year * 100 + when.month


def _upsert_player(session, account_hash: str, player_name: str, data: dict, when):
    """The character for this account hash, created on first sight.

    A concurrent first submission from the same character (the queue runs
    several workers) can win the insert; the SAVEPOINT keeps that from
    poisoning the caller's transaction and the loser re-reads the winner's row.
    """
    player = session.query(HardcorePlayer).filter(
        HardcorePlayer.account_hash == account_hash).first()
    if player is None:
        try:
            with session.begin_nested():
                player = HardcorePlayer(
                    account_hash=account_hash, player_name=player_name,
                    first_seen=when, last_seen=when,
                )
                session.add(player)
        except IntegrityError:
            player = session.query(HardcorePlayer).filter(
                HardcorePlayer.account_hash == account_hash).first()
            if player is None:
                raise
    if player.player_name != player_name:
        player.player_name = player_name
    if when > player.last_seen:
        player.last_seen = when
    account_type = data.get("account_type")
    if account_type not in (None, ""):
        player.account_type = str(account_type).strip().lower()[:32]
    plugin_version = data.get("p_v")
    if plugin_version not in (None, ""):
        player.plugin_version = str(plugin_version)[:20]
    return player


def _drop_row(data: dict):
    """(fields for a HardcoreDrop, None) or (None, rejection message)."""
    item_name = data.get("item_name", data.get("item"))
    if not item_name:
        return None, "Hardcore drop has no item name"
    quantity = sanitize_quantity(data.get("quantity"))
    if quantity is None:
        return None, "Invalid drop quantity"
    # The client's price, deliberately: ours are main-game GE prices.
    unit_value = _int_or_none(data.get("value"))
    if unit_value is None or unit_value < 0:
        unit_value = 0
    if exceeds_value_sanity_limit(unit_value, quantity):
        return None, "Drop value exceeds the plausible maximum and was rejected"
    # The plugin sends "killcount" and uses 0 for unknown.
    kill_count = _int_or_none(data.get("kill_count", data.get("killcount")))
    if kill_count is not None and kill_count <= 0:
        kill_count = None
    npc_name = data.get("source", data.get("npc_name"))
    return {
        "item_id": _int_or_none(data.get("item_id", data.get("id"))),
        "item_name": str(item_name).strip()[:255],
        "npc_name": str(npc_name).strip()[:255] if npc_name else None,
        "quantity": quantity,
        "unit_value": unit_value,
        "total_value": unit_value * quantity,
        "kill_count": kill_count,
    }, None


async def store_hardcore_submission(submission_type: str, data: dict, session):
    """Store one hardcore-world submission of canonical ``submission_type``."""
    player_name = str(data.get("player_name", data.get("player")) or "").strip()
    account_hash = str(data.get("acc_hash") or "").strip()
    if not player_name or not account_hash:
        return SubmissionResponse(False, "Hardcore submission is missing its player or account hash")

    drop_fields = None
    if submission_type == "drop":
        drop_fields, rejection = _drop_row(data)
        if rejection:
            return SubmissionResponse(False, rejection)

    guid = data.get("guid") or data.get("unique_id")
    guid = str(guid)[:255] if guid else None
    if guid and session.query(HardcoreSubmission.id).filter(
            HardcoreSubmission.unique_id == guid).first():
        return SubmissionResponse(False, "Duplicate hardcore submission")

    when = received_at(data)
    partition = _partition(when)
    player = _upsert_player(session, account_hash, player_name, data, when)
    image_url = data.get("image_url")
    image_url = str(image_url)[:300] if image_url else None

    try:
        with session.begin_nested():
            submission = HardcoreSubmission(
                player_id=player.id,
                submission_type=submission_type[:32],
                unique_id=guid,
                payload=_clean_payload(data),
                image_url=image_url,
                plugin_version=str(data["p_v"])[:20] if data.get("p_v") else None,
                used_api=bool(data.get("used_api")),
                date_added=when,
                partition=partition,
            )
            session.add(submission)
            session.flush()
            if drop_fields is not None:
                session.add(HardcoreDrop(
                    submission_id=submission.id,
                    player_id=player.id,
                    image_url=image_url,
                    date_added=when,
                    partition=partition,
                    **drop_fields,
                ))
                session.flush()
    except IntegrityError:
        # The same GUID landed from a second worker between our check and now.
        return SubmissionResponse(False, "Duplicate hardcore submission")

    log.info("[Hardcore] stored %s for %s (hardcore player %s)",
             submission_type, player_name, player.id)
    return SubmissionResponse(True, f"Stored hardcore world {submission_type}")
