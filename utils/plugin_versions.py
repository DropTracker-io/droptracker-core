"""Which plugin versions each player has been seen running.

Every submission carries the plugin's version (``p_v``). Recording the
first and latest time each account sent each version answers the questions
the tester program needs answered: did a Bug Tester actually run the build
they downloaded, and who had a version before it reached the Plugin Hub.

:func:`record_sighting` sits on the submission path of both transports
(workers/webhook_consumer.py, bots/webhook_bot.py), after the commit. It
must cost next to nothing and must never fail a submission, so:

* one Redis ``SET NX`` per submission decides whether there is anything to
  write. The claim lasts six hours, so an account costs at most four
  writes a day per version, however much it submits;
* the write is one statement in a session of its own, closed before return;
* every failure is swallowed.

Stdlib only at import. Redis, the database and the build manifest are
reached lazily, inside the functions.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

CLAIM_KEY = "pluginver:claim:{acc_hash}:{version}"
CLAIM_TTL_SECONDS = 6 * 60 * 60

#: player_plugin_versions.version is VARCHAR(32).
MAX_VERSION_LENGTH = 32
#: players.account_hash is VARCHAR(100). Longer is not an account we know.
_MAX_HASH_LENGTH = 100

#: After a failed write, record nothing for this long. Without it, a missing
#: table (this code live before its migration) or a database that is down
#: would cost every submission a failed statement and a pool checkout.
FAILURE_BACKOFF_SECONDS = 60
_skip_until = 0.0

# "6.0.19", "v6.0.19", "6.0" and "6.0.6-SNAPSHOT". A suffix is ignored, so a
# snapshot compares equal to its release. It has to start with a separator,
# and "." is not one: allowing it lets "6.0.19abc" backtrack into 6.0 with
# ".19abc" as the suffix.
_VERSION_RE = re.compile(r"^[vV]?(\d{1,6})\.(\d{1,6})(?:\.(\d{1,6}))?(?:[-+_ ].*)?$")

# prerelease is written on first sight only, and that is deliberate: it says
# the player ran this version BEFORE it reached the Plugin Hub, which stays
# true after the release.
#
# ``sightings`` is qualified in the UPDATE because that clause can also see
# the SELECT's table: a ``players`` column of the same name, added one day,
# would make the bare name ambiguous and fail every write.
_UPSERT_SQL = (
    "INSERT INTO player_plugin_versions "
    "(player_id, version, first_seen, last_seen, sightings, prerelease) "
    "SELECT p.player_id, :version, NOW(), NOW(), 1, :pre "
    "FROM players p WHERE p.account_hash = :h LIMIT 1 "
    "ON DUPLICATE KEY UPDATE last_seen = NOW(), "
    "sightings = player_plugin_versions.sightings + 1"
)


def version_tuple(text: Any) -> Optional[Tuple[int, int, int]]:
    """``"6.0.19"`` as ``(6, 0, 19)`` for comparing; None when it is not one."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def is_prerelease(version: Any, release_version: Any) -> bool:
    """Whether ``version`` is ahead of the Plugin Hub's ``release_version``.

    False whenever either side is unreadable: not knowing the release must
    never mark every player as a tester of something.
    """
    seen, released = version_tuple(version), version_tuple(release_version)
    return seen is not None and released is not None and seen > released


def _redis():
    from utils.redis import redis_client

    return getattr(redis_client, "client", None)


def _private_session():
    """A session of this call's own. Never the submission's: that one may be
    mid-transaction, and a failure here must not reach it."""
    from db.models.base import db_session

    return db_session()


def _release_version() -> Optional[str]:
    from utils.tester_builds import load_current, release_version

    return release_version(load_current())


def _write(acc_hash: str, version: str) -> None:
    from sqlalchemy import text

    prerelease = is_prerelease(version, _release_version())
    with _private_session() as session:
        session.execute(
            text(_UPSERT_SQL),
            {"version": version, "pre": 1 if prerelease else 0, "h": acc_hash},
        )
        session.commit()


def record_sighting(data: dict) -> None:
    """Note that this submission's account is running its ``p_v``. Never raises.

    Call it after the submission is committed: the statement finds the player
    by account hash, and a first submission creates that row.
    """
    global _skip_until
    try:
        from utils.mirror_context import is_mirrored_submission

        # A mirrored copy of production traffic says what production's
        # players run, which is not something this instance should record.
        if is_mirrored_submission():
            return
        if time.monotonic() < _skip_until:
            return
        acc_hash = data.get("acc_hash")
        plugin_version = data.get("p_v")
        if acc_hash in (None, "") or plugin_version in (None, ""):
            return
        acc_hash = str(acc_hash).strip()
        version = str(plugin_version).strip()[:MAX_VERSION_LENGTH].strip()
        if not acc_hash or len(acc_hash) > _MAX_HASH_LENGTH:
            return
        if not version or version.lower() == "unknown":
            return

        client = _redis()
        if client is None:
            return
        key = CLAIM_KEY.format(acc_hash=acc_hash, version=version)
        if not client.set(key, "1", nx=True, ex=CLAIM_TTL_SECONDS):
            return
    except Exception:
        return

    try:
        _write(acc_hash, version)
    except Exception as exc:
        _skip_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
        log.warning("plugin version sighting not recorded (pausing %ss): %s",
                    FAILURE_BACKOFF_SECONDS, exc)
        # Give the claim back, so the next submission tries again instead of
        # this version going unrecorded for six hours.
        try:
            client.delete(key)
        except Exception:
            pass
