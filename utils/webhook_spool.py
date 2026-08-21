"""On-disk fallback for the intake queue.

``POST /webhook`` normally validates a submission and ``RPUSH``es it onto
``webhook:queue`` in ~50 ms. When that push fails there is nowhere else for the
submission to go, and the client is told it was accepted anyway — which is how
2026-08-18 lost ~40,800 submissions: a full disk stopped Redis writing its RDB
snapshot, ``stop-writes-on-bgsave-error`` turned that into "reject every write",
and the acceptor answered **HTTP 200** to every one of them for 87 minutes. The
plugin marked each submission processed and never retried, so unlike the
webhook-path traffic (whose Discord message survives, see
``scripts/replay_webhook_window.py``) those were unrecoverable.

This module is the missing durable step: if Redis will not take the entry, it
goes to a file, and only then is the client told we have it. The consumer
drains the directory back into Redis once Redis is healthy again.

Design notes:
- **Entries are small.** One spooled entry is the JSON envelope (payload plus
  the path of the already-saved image), on the order of 1-2 KB. A full 87-minute
  outage at the observed rate would be roughly 130 MB — affordable even on the
  nearly-full disk that causes this failure in the first place.
- **The write must be able to fail.** Spooling happens *because* something is
  wrong, often a full disk, so ``write`` returns False rather than raising and
  the caller answers 503 so the client retries. Never report success we cannot
  back up.
- **Atomic.** Written to ``.tmp`` and renamed, so a drain never sees a partial
  file even if the process dies mid-write.
- **Order is not preserved.** Submissions are independent and deduplicated by
  GUID, so replay order does not matter.
"""
from __future__ import annotations

import json
import os
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPOOL_DIR = os.path.join(REPO_ROOT, "logs", "webhook_spool")

# Refuse to spool without bound: if Redis has been gone long enough to build a
# backlog this large, the disk is the more urgent problem and shedding load is
# better than filling the filesystem that Redis itself needs.
MAX_SPOOL_FILES = 250_000


def _ensure_dir() -> bool:
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        return True
    except Exception:
        return False


def write(entry: dict) -> bool:
    """Persist one queue entry. Returns False if it could not be stored.

    False is a real answer, not an exception: the caller must translate it into
    a retryable response rather than a false acknowledgement.
    """
    if not _ensure_dir():
        return False
    try:
        if len(os.listdir(SPOOL_DIR)) >= MAX_SPOOL_FILES:
            return False
    except Exception:
        return False

    name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex}.json"
    final = os.path.join(SPOOL_DIR, name)
    tmp = f"{final}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(entry, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def pending_count() -> int:
    try:
        return sum(1 for n in os.listdir(SPOOL_DIR) if n.endswith(".json"))
    except Exception:
        return 0


def drain(redis_conn, queue_key: str = "webhook:queue", limit: int = 500) -> tuple:
    """Push spooled entries back onto the queue. Returns (drained, failed).

    Stops at the first push failure: if Redis is still refusing writes there is
    no point walking the rest of the directory, and the file must stay on disk.
    A file is only unlinked after its push has succeeded, so a crash mid-drain
    re-delivers rather than loses (GUID dedup makes the repeat a no-op).
    """
    drained = failed = 0
    try:
        names = sorted(n for n in os.listdir(SPOOL_DIR) if n.endswith(".json"))
    except Exception:
        return 0, 0

    for name in names[:limit]:
        path = os.path.join(SPOOL_DIR, name)
        try:
            with open(path) as fh:
                entry = json.load(fh)
        except Exception:
            # Unreadable/corrupt: move aside rather than retry it forever.
            try:
                os.replace(path, path + ".bad")
            except Exception:
                pass
            failed += 1
            continue

        try:
            redis_conn.rpush(queue_key, json.dumps(entry))
        except Exception:
            break

        try:
            os.unlink(path)
        except Exception:
            pass
        drained += 1

    return drained, failed
