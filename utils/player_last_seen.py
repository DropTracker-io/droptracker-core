"""When each player's DropTracker plugin was last seen talking to us.

Two signals feed ``player_last_seen``:

* every plugin submission, on both transports (workers/webhook_consumer.py,
  bots/webhook_bot.py), after the commit — :func:`record_submission`;
* the plugin's event notifications poll (api/routes/notifications.py), which
  runs continuously while a player with API mode on is logged in, so it
  catches the player who is online but has not submitted anything —
  :func:`record_player`.

Manual submissions from the website (``intake_source == "manual"``) are not
the plugin and are not recorded.

This sits on the hot path, so it follows the same rules as
:mod:`utils.plugin_versions`:

* one Redis ``SET NX`` decides whether there is anything to write. The claim
  lasts :data:`CLAIM_TTL_SECONDS`, which is therefore the precision of the
  stored value: an account costs at most one write per window, however much
  it submits;
* the write is one statement in a session of its own, closed before return;
* every failure is swallowed, and a failed write pauses recording briefly so
  a missing table or a down database does not cost every submission a
  statement.

Stdlib only at import. Redis and the database are reached lazily.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

CLAIM_KEY = "lastseen:claim:{kind}:{ident}"
#: The precision of ``last_seen``. Five minutes keeps it meaningful for "is
#: this player active" while bounding writes to ~12/hour per online account.
CLAIM_TTL_SECONDS = 5 * 60

#: players.account_hash is VARCHAR(100). Longer is not an account we know.
_MAX_HASH_LENGTH = 100

FAILURE_BACKOFF_SECONDS = 60
_skip_until = 0.0

# GREATEST keeps the column monotonic should two writers race, or a backfill
# run land after a live write.
_UPSERT_BY_HASH_SQL = (
    "INSERT INTO player_last_seen (player_id, last_seen_at) "
    "SELECT p.player_id, NOW() FROM players p WHERE p.account_hash = :h LIMIT 1 "
    "ON DUPLICATE KEY UPDATE "
    "last_seen_at = GREATEST(player_last_seen.last_seen_at, VALUES(last_seen_at))"
)
_UPSERT_BY_ID_SQL = (
    "INSERT INTO player_last_seen (player_id, last_seen_at) VALUES (:pid, NOW()) "
    "ON DUPLICATE KEY UPDATE "
    "last_seen_at = GREATEST(player_last_seen.last_seen_at, VALUES(last_seen_at))"
)


def _redis():
    from utils.redis import redis_client

    return getattr(redis_client, "client", None)


def _private_session():
    """A session of this call's own. Never the submission's: that one may be
    mid-transaction, and a failure here must not reach it."""
    from db.models.base import db_session

    return db_session()


def _write(sql: str, params: dict) -> None:
    from sqlalchemy import text

    with _private_session() as session:
        session.execute(text(sql), params)
        session.commit()


def _claim(kind: str, ident: str) -> Optional[str]:
    """The claimed Redis key, or None when there is nothing to write now."""
    from utils.mirror_context import is_mirrored_submission

    # A mirrored copy of production traffic is not this instance's players.
    if is_mirrored_submission():
        return None
    if time.monotonic() < _skip_until:
        return None
    client = _redis()
    if client is None:
        return None
    key = CLAIM_KEY.format(kind=kind, ident=ident)
    if not client.set(key, "1", nx=True, ex=CLAIM_TTL_SECONDS):
        return None
    return key


def _write_claimed(key: str, sql: str, params: dict) -> None:
    global _skip_until
    try:
        _write(sql, params)
    except Exception as exc:
        _skip_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
        log.warning("player last-seen not recorded (pausing %ss): %s",
                    FAILURE_BACKOFF_SECONDS, exc)
        # Give the claim back so the next signal retries instead of this
        # account going unrecorded for a whole window.
        try:
            _redis().delete(key)
        except Exception:
            pass


def record_submission(data: dict) -> None:
    """Note that this submission's account used the plugin just now. Never raises.

    Call it after the submission is committed: the statement finds the player
    by account hash, and a first submission creates that row.
    """
    try:
        if not isinstance(data, dict):
            return
        if data.get("intake_source") == "manual":
            return
        acc_hash = data.get("acc_hash")
        if acc_hash in (None, ""):
            return
        acc_hash = str(acc_hash).strip()
        if not acc_hash or len(acc_hash) > _MAX_HASH_LENGTH:
            return
        key = _claim("h", acc_hash)
        if key is None:
            return
    except Exception:
        return
    _write_claimed(key, _UPSERT_BY_HASH_SQL, {"h": acc_hash})


def record_player(player_id) -> None:
    """Note that this player's plugin polled us just now. Never raises.

    ``player_id`` 0 is a real account, so only ``None`` means "no player".
    """
    try:
        if player_id is None:
            return
        player_id = int(player_id)
        key = _claim("p", str(player_id))
        if key is None:
            return
    except Exception:
        return
    _write_claimed(key, _UPSERT_BY_ID_SQL, {"pid": player_id})
