"""Keep the threads our edit-in-place messages live in from going stale.

Several features post one message and then keep *editing* it: the Hall of Fame
boards, the lootboard, event standings, Clan Log boards. Groups
often put those in a thread. Discord archives a thread after a period with no
new messages (1 hour to 7 days, the thread's ``auto_archive_duration``), and
**an edit is not activity**. So a thread that only holds a board the bot keeps
editing always archives, and from then on every edit fails with
``400 Thread is archived`` (code 50083). The board silently freezes. One group's
lootboard sat frozen for weeks like this.

Fix: before editing into a thread, look at its live state and, if it is
archived, reopen it (``PATCH /channels/{id} {"archived": false}``). No message
is posted and nothing is renamed. Reopening also restarts Discord's inactivity
clock (``thread_metadata.archive_timestamp`` changes, and Discord computes
activity from it), so this costs one request per thread per archive period.

Permissions: reopening an *unlocked* thread needs only Send Messages in the
thread. A **locked** thread (an admin chose "lock", usually so members cannot
chat under the board) can only be reopened with **Manage Threads**. When the bot
cannot reopen a thread, the outcome is recorded in Redis so the group's
Diagnostics page can say exactly what to fix, rather than the board freezing
with nothing but a log line.

Deliberately REST-only: the core bot runs without the GUILDS intent, so it
receives no THREAD_UPDATE events and a cached thread's ``archived`` flag can be
arbitrarily stale. The cached object is used only for its *type*, which never
changes, so channels that are not threads cost nothing.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: GUILD_NEWS_THREAD, GUILD_PUBLIC_THREAD, GUILD_PRIVATE_THREAD.
THREAD_TYPES = frozenset({10, 11, 12})

# Outcomes.
NOT_THREAD = "not_thread"
OPEN = "open"
REOPENED = "reopened"
LOCKED = "locked"          # archived + locked, and we lack Manage Threads
FORBIDDEN = "forbidden"    # archived, unlocked, and we still may not reopen it
ERROR = "error"            # could not find out (transient) — nothing recorded

#: Outcomes the Diagnostics page reports as a problem.
PROBLEM_STATES = frozenset({LOCKED, FORBIDDEN})

#: What each feature is called on the Diagnostics page.
FEATURE_LABELS = {
    "hall_of_fame": "Hall of Fame",
    "lootboard": "Loot leaderboard",
    "event_board": "Event standings",
    "clan_log": "Clan Log",
}

_KEY = "thread_health:{group_id}:{feature}"
# Long enough to survive between passes of the slowest caller; short enough
# that a feature switched off (or moved out of a thread) stops being reported.
_TTL_SECONDS = 3 * 24 * 3600

_REOPEN_REASON = "DropTracker: reopen an auto-archived thread so its board can keep updating"


def channel_is_thread(channel: Any) -> bool:
    """True for a thread, from a channel object or a raw channel dict."""
    if channel is None:
        return False
    raw_type = channel.get("type") if isinstance(channel, dict) else getattr(channel, "type", None)
    try:
        return int(raw_type) in THREAD_TYPES
    except (TypeError, ValueError):
        return False


def classify(thread: Dict[str, Any]) -> str:
    """What to do about a raw thread: already ``open``, or needs reopening."""
    meta = thread.get("thread_metadata") or {}
    return "archived" if meta.get("archived") else OPEN


async def ensure_thread_open(http, channel, *, group_id: Optional[int] = None,
                             feature: Optional[str] = None) -> str:
    """Reopen ``channel`` if it is an archived thread; report what happened.

    ``http`` is the bot's ``interactions`` HTTP client (``bot.http``). Never
    raises: a failure here must not cost the caller its pass, since the edit
    that follows reports its own error if the thread really is still archived.
    """
    if not channel_is_thread(channel):
        return NOT_THREAD
    channel_id = int(getattr(channel, "id", 0) or (channel.get("id") if isinstance(channel, dict) else 0))
    try:
        live = await http.get_channel(channel_id)
    except Exception as e:
        log.warning("threads: could not read thread %s (%s: %s)", channel_id, type(e).__name__, e)
        return ERROR
    if classify(live) == OPEN:
        _record(group_id, feature, channel_id, live, OPEN)
        return OPEN
    locked = bool((live.get("thread_metadata") or {}).get("locked"))
    try:
        await http.modify_channel(channel_id, {"archived": False}, reason=_REOPEN_REASON)
    except Exception as e:
        status = getattr(e, "status", None)
        state = (LOCKED if locked else FORBIDDEN) if status in (401, 403) or "permission" in str(e).lower() else ERROR
        log.warning(
            "threads: could not reopen archived%s thread %s for group %s %s (%s: %s)",
            " LOCKED" if locked else "", channel_id, group_id, feature, type(e).__name__, e,
        )
        if state != ERROR:
            _record(group_id, feature, channel_id, live, state)
        return state
    log.warning("threads: reopened archived thread %s (group %s, %s)", channel_id, group_id, feature)
    _record(group_id, feature, channel_id, live, REOPENED)
    return REOPENED


# --------------------------------------------------------------------------- #
# Health records for the Diagnostics page
# --------------------------------------------------------------------------- #

def _redis():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None


def _record(group_id: Optional[int], feature: Optional[str], channel_id: int,
            live: Dict[str, Any], state: str) -> None:
    if group_id is None or not feature:
        return
    conn = _redis()
    if conn is None:
        return
    meta = live.get("thread_metadata") or {}
    record = {
        "feature": feature,
        "channel_id": str(channel_id),
        "thread_name": live.get("name") or "",
        "parent_id": str(live.get("parent_id") or ""),
        "state": state,
        "locked": bool(meta.get("locked")),
        "checked_at": int(time.time()),
    }
    key = _KEY.format(group_id=int(group_id), feature=feature)
    try:
        if state == OPEN:
            # Keep a previous "reopened" note (useful context) but let a
            # resolved problem disappear.
            raw = conn.get(key)
            previous = json.loads(raw) if raw else None
            if previous and previous.get("state") in PROBLEM_STATES:
                conn.delete(key)
            return
        conn.setex(key, _TTL_SECONDS, json.dumps(record))
    except Exception as e:
        log.debug("threads: could not record health for group %s %s: %s", group_id, feature, e)


def group_thread_health(group_id: int) -> List[Dict[str, Any]]:
    """Recorded thread outcomes for one group, problems first."""
    conn = _redis()
    if conn is None:
        return []
    out: List[Dict[str, Any]] = []
    try:
        for feature in FEATURE_LABELS:
            raw = conn.get(_KEY.format(group_id=int(group_id), feature=feature))
            if not raw:
                continue
            record = json.loads(raw)
            record["label"] = FEATURE_LABELS.get(record.get("feature"), record.get("feature"))
            record["problem"] = record.get("state") in PROBLEM_STATES
            out.append(record)
    except Exception:
        return out
    out.sort(key=lambda r: (not r["problem"], r["label"]))
    return out
