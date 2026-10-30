"""Discord delivery relay — the protocol and the Redis outbox (web153a).

A *relay integration* lets a group's own Discord bot deliver DropTracker's
Discord messages in the group's server instead of the DropTracker bot.
Everything about WHAT is sent stays on DropTracker (layouts, toggles, channel
choices, board images); only WHO sends changes. No bot token is ever stored:

* **Outbound.** Where the core bot would have called ``channel.send`` /
  ``message.edit`` / ``message.delete`` for a relay-owned server, it instead
  enqueues the exact Discord request body (built by the same library call the
  bot uses) here. The customer bot polls the data API (``GET
  /v2/relay/messages``), performs the request with its own token, and acks
  (``POST /v2/relay/messages/<id>/ack``) with the resulting Discord message id
  or the Discord error. DropTracker keeps the id map, so a feature that edits
  in place (the live event board) works unchanged: it is handed a *relay
  message id* (:data:`RELAY_ID_BASE` + n, never a real snowflake) and later
  edits resolve through this module.
* **Inbound.** Buttons on relayed messages fire on the customer's app, so
  their bot forwards each click whose custom_id matches
  :data:`INTERACTION_RULES` (``POST /v2/relay/interactions``). The data API
  tells it how to defer (so the 3-second deadline never depends on us), then
  the core bot runs the same handler it runs for its own buttons and answers
  through the interaction token. Identity in a forwarded click is the
  customer bot's assertion — the integration owner is trusted with it (owner
  decision 2026-10-29) — but the *target* (event, completion, drop) must
  belong to the integration's group.

Stdlib-only at import (redis is passed in), so the unit tests drive every
path against an in-memory fake. All Redis values are bytes on the way out of
the real client; :func:`_s` / :func:`_j` normalise.
"""
from __future__ import annotations

import json
import re
import time
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #

#: What an integration can take over. ``events``: every event message (the
#: notification queue's event_* types, live boards, event lootboards, sign-up
#: prompts and their clicks, completion review buttons). ``notifications``:
#: every other notification-queue type (drops, PBs, clogs, ...) — wired through
#: the same chokepoint but not yet offered to customers.
FEATURES = ("events", "notifications")
FEATURE_LABELS = {
    "events": "Events (notifications, live boards, sign-ups, reviews)",
    "notifications": "Other notifications (drops, PBs, collection log, ...)",
}

STATUSES = ("active", "paused")


def feature_for_notification_type(notification_type) -> str:
    return "events" if str(notification_type or "").startswith("event_") else "notifications"


def normalize_features(raw) -> list:
    """The stored feature list, filtered to known values, order kept."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = [raw]
    out = []
    for f in raw or []:
        if f in FEATURES and f not in out:
            out.append(f)
    return out


# --------------------------------------------------------------------------- #
# Redis keyspace
# --------------------------------------------------------------------------- #

SEQ_KEY = "relay:seq"
INTERACTION_QUEUE = "relay:interactions"
RESYNC_QUEUE = "relay:resync"
#: Bumped by the web API whenever an integration changes, so every core
#: process drops its cached routing table at once instead of waiting for TTL.
ROUTES_VERSION_KEY = "relay:routes:version"


def msg_key(mid) -> str:
    return f"relay:m:{int(mid)}"


def pending_key(iid) -> str:
    return f"relay:pending:{int(iid)}"


def leased_key(iid) -> str:
    return f"relay:leased:{int(iid)}"


def coalesce_key(target_mid) -> str:
    return f"relay:co:{int(target_mid)}"


def file_key(mid, index) -> str:
    return f"relay:f:{int(mid)}:{int(index)}"


def stats_key(iid) -> str:
    return f"relay:stats:{int(iid)}"


def log_key(iid) -> str:
    return f"relay:log:{int(iid)}"


def channel_key(channel_id) -> str:
    return f"relay:chan:{channel_id}"


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: A claimed message must be acked within this long or the next poll may hand
#: it out again (the bot died mid-send). Longer than any sane upload.
LEASE_SECONDS = 120
#: Retryable failures (429, 5xx, network) before a message is given up on.
MAX_ATTEMPTS = 5
#: A sent create is kept this long — edits and deletes resolve through it.
SENT_CREATE_TTL = 90 * 86400
#: Everything else (edits, deletes, failures) is history only.
SHORT_TTL = 7 * 86400
FILE_TTL = 24 * 3600
#: Per-message attachment cap (Discord allows 10; we never send more than 2).
MAX_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
LOG_LENGTH = 200
CHANNEL_MAP_TTL = 7 * 86400
CLAIM_LIMIT_MAX = 50

# Discord JSON error codes worth recognising on an ack.
UNKNOWN_CHANNEL = 10003
UNKNOWN_MESSAGE = 10008
MISSING_ACCESS = 50001
MISSING_PERMISSIONS = 50013

OPS = ("create", "edit", "delete")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _s(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _j(value):
    raw = _s(value)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _now(now) -> float:
    return float(now) if now is not None else time.time()


#: Synthetic Discord-side ids for relayed messages: ``RELAY_ID_BASE + mid``.
#: A snowflake reaches 2**62 only in 2049, so these never collide with a real
#: id; they are positive, fit BIGINT/String(32) columns, sort after every real
#: message and increase with each relay message — so code comparing message
#: ids by age (e.g. "is the lootboard below the board post?") stays correct.
RELAY_ID_BASE = 1 << 62


def is_relay_message_id(message_id) -> bool:
    """True for the synthetic ids this module hands out."""
    try:
        return int(str(message_id)) >= RELAY_ID_BASE
    except (TypeError, ValueError):
        return False


def relay_mid(message_id) -> int:
    """Relay message id from a synthetic Discord-side id."""
    return int(str(message_id)) - RELAY_ID_BASE


def synthetic_id(mid) -> int:
    return RELAY_ID_BASE + int(mid)


def load(r, mid) -> Optional[dict]:
    return _j(r.get(msg_key(mid)))


def _save(r, rec: dict, ttl: int) -> None:
    r.set(msg_key(rec["id"]), json.dumps(rec, separators=(",", ":")), ex=int(ttl))


def _log(r, iid, entry: dict) -> None:
    r.lpush(log_key(iid), json.dumps(entry, separators=(",", ":")))
    r.ltrim(log_key(iid), 0, LOG_LENGTH - 1)


def _bump(r, iid, field: str, amount: int = 1) -> None:
    r.hincrby(stats_key(iid), field, amount)


# --------------------------------------------------------------------------- #
# Enqueue (core bot side)
# --------------------------------------------------------------------------- #

def _store_files(r, mid, files) -> list:
    """Persist ``[(filename, bytes)]`` and return their manifest."""
    manifest = []
    for index, (filename, data) in enumerate(list(files or [])[:MAX_FILES]):
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_FILE_BYTES:
            continue
        r.set(file_key(mid, index), bytes(data), ex=FILE_TTL)
        manifest.append({"index": index, "filename": str(filename)[:100],
                         "size": len(data)})
    return manifest


def _new_record(r, iid, op, channel_id, body, ref, now, target=None) -> dict:
    mid = int(r.incr(SEQ_KEY))
    return {
        "id": mid, "iid": int(iid), "op": op, "channel_id": str(channel_id),
        "target": int(target) if target is not None else None,
        "body": body or {}, "files": [], "status": "pending", "attempts": 0,
        "created": _now(now), "ref": (str(ref)[:64] if ref else None),
        "discord_message_id": None, "error": None,
    }


def _queue(r, rec: dict) -> None:
    r.zadd(pending_key(rec["iid"]), {str(rec["id"]): rec["id"]})


def enqueue_create(r, iid, channel_id, body: dict, files=None, ref=None, now=None) -> int:
    """Queue a new message; returns its relay message id."""
    rec = _new_record(r, iid, "create", channel_id, body, ref, now)
    rec["files"] = _store_files(r, rec["id"], files)
    _save(r, rec, SHORT_TTL)
    _queue(r, rec)
    return rec["id"]


def enqueue_edit(r, iid, target_mid, body: dict, files=None, ref=None, now=None) -> int:
    """Queue an edit of a relayed message, coalescing with anything unsent.

    * The target create is still unsent → its body/files are replaced, so
      the first thing Discord ever sees is the latest render.
    * An earlier edit of the same target is still unsent → replaced too.
    * Otherwise a new edit op is queued.

    Returns the relay message id carrying the change.
    """
    target = load(r, target_mid)
    if target is None:
        raise LookupError(f"relay message {target_mid} is unknown")
    if target["status"] == "pending":
        _replace_payload(r, target, body, files)
        return target["id"]
    prior = _s(r.get(coalesce_key(target_mid)))
    if prior:
        rec = load(r, prior)
        if rec is not None and rec["status"] == "pending":
            _replace_payload(r, rec, body, files)
            return rec["id"]
    rec = _new_record(r, iid, "edit", target["channel_id"], body, ref, now,
                      target=target["id"])
    rec["files"] = _store_files(r, rec["id"], files)
    _save(r, rec, SHORT_TTL)
    r.set(coalesce_key(target_mid), str(rec["id"]), ex=SHORT_TTL)
    _queue(r, rec)
    return rec["id"]


def _replace_payload(r, rec: dict, body: dict, files) -> None:
    for f in rec.get("files") or []:
        r.delete(file_key(rec["id"], f["index"]))
    rec["body"] = body or {}
    rec["files"] = _store_files(r, rec["id"], files)
    _save(r, rec, SHORT_TTL)


def enqueue_delete(r, iid, target_mid, ref=None, now=None) -> Optional[int]:
    """Queue a delete. An unsent create is simply withdrawn (never posted)."""
    target = load(r, target_mid)
    if target is None:
        return None
    if target["status"] == "pending":
        if r.zrem(pending_key(target["iid"]), str(target["id"])):
            target["status"] = "withdrawn"
            _save(r, target, SHORT_TTL)
            return None
    rec = _new_record(r, iid, "delete", target["channel_id"], {}, ref, now,
                      target=target["id"])
    _save(r, rec, SHORT_TTL)
    _queue(r, rec)
    return rec["id"]


def target_state(r, mid) -> str:
    """'live' (sent or on its way), 'dead' (gone/failed), or 'unknown'."""
    rec = load(r, mid)
    if rec is None:
        return "unknown"
    if rec["status"] in ("pending", "leased", "sent"):
        return "live"
    return "dead"


# --------------------------------------------------------------------------- #
# Claim (data API side: GET /v2/relay/messages)
# --------------------------------------------------------------------------- #

def _reclaim_expired_leases(r, iid, now: float) -> None:
    for raw in r.zrangebyscore(leased_key(iid), "-inf", now):
        mid = int(_s(raw))
        if r.zrem(leased_key(iid), str(mid)):
            rec = load(r, mid)
            if rec is None:
                continue
            rec["status"] = "pending"
            _save(r, rec, SHORT_TTL)
            r.zadd(pending_key(iid), {str(mid): mid})


def claim(r, iid, limit=20, hold_minutes=360, now=None) -> list:
    """Hand out up to ``limit`` deliverable messages, oldest first.

    Each is leased for :data:`LEASE_SECONDS`; ``ZREM`` on the pending set is
    the atomic claim, so two concurrent polls never get the same message.
    Edits/deletes wait behind their target's create (ordering), and a create
    older than ``hold_minutes`` is dropped as stale instead of posting late.
    """
    now = _now(now)
    limit = max(1, min(int(limit or 20), CLAIM_LIMIT_MAX))
    _reclaim_expired_leases(r, iid, now)
    out = []
    hold_seconds = max(1, int(hold_minutes or 0)) * 60
    for raw in r.zrangebyscore(pending_key(iid), "-inf", "+inf"):
        if len(out) >= limit:
            break
        mid = int(_s(raw))
        rec = load(r, mid)
        if rec is None:
            r.zrem(pending_key(iid), str(mid))
            continue
        if rec["op"] == "create" and now - rec["created"] > hold_seconds:
            if r.zrem(pending_key(iid), str(mid)):
                _finish(r, rec, "expired", "not delivered in time", now)
            continue
        message_id = None
        if rec["op"] in ("edit", "delete"):
            target = load(r, rec["target"])
            if target is None or target["status"] not in ("pending", "leased", "sent"):
                if r.zrem(pending_key(iid), str(mid)):
                    _finish(r, rec, "failed", "target message is gone", now)
                continue
            if not target.get("discord_message_id"):
                continue  # its create hasn't been acked yet — keep order
            message_id = target["discord_message_id"]
        if not r.zrem(pending_key(iid), str(mid)):
            continue  # another poll won it
        rec["status"] = "leased"
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        _save(r, rec, SHORT_TTL)
        r.zadd(leased_key(iid), {str(mid): now + LEASE_SECONDS})
        out.append(deliverable(rec, message_id))
    r.hset(stats_key(iid), "last_poll", str(int(now)))
    return out


def deliverable(rec: dict, message_id=None) -> dict:
    """The wire shape one message takes in ``GET /v2/relay/messages``."""
    item = {
        "id": rec["id"],
        "op": rec["op"],
        "channel_id": rec["channel_id"],
        "attempt": rec.get("attempts") or 1,
    }
    if rec["op"] == "create":
        item["body"] = rec["body"]
    elif rec["op"] == "edit":
        item["message_id"] = message_id
        item["body"] = rec["body"]
    else:
        item["message_id"] = message_id
    if rec["op"] != "delete":
        item["files"] = [
            {"index": f["index"], "filename": f["filename"], "size": f["size"],
             "url": f"/v2/relay/files/{rec['id']}/{f['index']}"}
            for f in rec.get("files") or []
        ]
    return item


def get_file(r, iid, mid, index) -> Optional[bytes]:
    rec = load(r, mid)
    if rec is None or rec["iid"] != int(iid):
        return None
    data = r.get(file_key(mid, index))
    return bytes(data) if data is not None else None


# --------------------------------------------------------------------------- #
# Ack (data API side: POST /v2/relay/messages/<id>/ack)
# --------------------------------------------------------------------------- #

def _finish(r, rec: dict, status: str, error: Optional[str], now: float) -> None:
    rec["status"] = status
    rec["error"] = error
    ttl = SENT_CREATE_TTL if (status == "sent" and rec["op"] == "create") else SHORT_TTL
    _save(r, rec, ttl)
    if status != "sent" or rec["op"] != "create":
        for f in rec.get("files") or []:
            r.delete(file_key(rec["id"], f["index"]))
    _bump(r, rec["iid"], status)
    _log(r, rec["iid"], {"id": rec["id"], "op": rec["op"], "status": status,
                         "channel_id": rec["channel_id"], "ref": rec.get("ref"),
                         "error": error, "at": int(now)})


def _retryable(http_status, code) -> bool:
    if http_status in (None, 0):
        return True  # network error on the customer side
    return http_status == 429 or int(http_status) >= 500


def ack(r, iid, mid, *, ok: bool, discord_message_id=None, http_status=None,
        code=None, error=None, now=None) -> dict:
    """Record the outcome of one delivery attempt. Returns ``{"status": ...}``.

    Raises LookupError for an id that is unknown or not this integration's,
    ValueError for an ack of a message that isn't currently leased.
    """
    now = _now(now)
    rec = load(r, mid)
    if rec is None or rec["iid"] != int(iid):
        raise LookupError("unknown relay message")
    if not r.zrem(leased_key(iid), str(rec["id"])):
        if rec["status"] in ("sent", "failed", "expired"):
            return {"status": rec["status"], "duplicate": True}
        raise ValueError("message is not awaiting an ack")
    r.hset(stats_key(iid), "last_ack", str(int(now)))
    if ok:
        if rec["op"] == "create":
            if not discord_message_id or not str(discord_message_id).isdigit():
                # Without the id nothing can ever edit or delete it — treat as
                # a broken ack rather than silently orphaning the message.
                _finish(r, rec, "failed", "ack carried no discord_message_id", now)
                return {"status": "failed"}
            rec["discord_message_id"] = str(discord_message_id)
        if rec["op"] == "delete" and rec.get("target"):
            target = load(r, rec["target"])
            if target is not None:
                target["status"] = "deleted"
                _save(r, target, SHORT_TTL)
        _finish(r, rec, "sent", None, now)
        return {"status": "sent"}

    detail = (str(error)[:300] if error else None) or f"HTTP {http_status} code {code}"
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if rec["op"] in ("edit", "delete") and code == UNKNOWN_MESSAGE:
        # The message was deleted in Discord: the owning feature will see its
        # target as gone and post a fresh one.
        target = load(r, rec["target"])
        if target is not None:
            target["status"] = "deleted"
            _save(r, target, SHORT_TTL)
        _finish(r, rec, "failed", detail, now)
        return {"status": "failed", "target": "deleted"}
    if _retryable(http_status, code) and int(rec.get("attempts") or 0) < MAX_ATTEMPTS:
        rec["status"] = "pending"
        rec["error"] = detail
        _save(r, rec, SHORT_TTL)
        _queue(r, rec)
        _bump(r, iid, "retried")
        return {"status": "retrying"}
    _finish(r, rec, "failed", detail, now)
    return {"status": "failed"}


def stats(r, iid) -> dict:
    raw = r.hgetall(stats_key(iid)) or {}
    out = {_s(k): _s(v) for k, v in raw.items()}
    out["pending"] = int(r.zcard(pending_key(iid)) or 0)
    out["leased"] = int(r.zcard(leased_key(iid)) or 0)
    return out


def recent_log(r, iid, limit=50) -> list:
    return [x for x in (_j(v) for v in r.lrange(log_key(iid), 0, int(limit) - 1)) if x]


# --------------------------------------------------------------------------- #
# Channel → guild map (the bot reports its channels; we also learn from our
# own bot's fetches). Lets routing work even if DropTracker's bot is not in
# the server.
# --------------------------------------------------------------------------- #

def remember_channels(r, guild_id, channel_ids: Iterable) -> int:
    n = 0
    for cid in channel_ids or []:
        cid = str(cid)
        if cid.isdigit():
            r.set(channel_key(cid), str(guild_id), ex=CHANNEL_MAP_TTL)
            n += 1
    return n


def channel_guild(r, channel_id) -> Optional[str]:
    return _s(r.get(channel_key(channel_id)))


# --------------------------------------------------------------------------- #
# Payload rewriting — make a DropTracker-built body valid for another app
# --------------------------------------------------------------------------- #

_CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_~]+):(\d{15,22})>")

#: Activity launch buttons (services/activity_launch_core). Only the app that
#: owns the Activity can launch it, so on a relayed message they become plain
#: links to the website.
LAUNCH_CUSTOM_ID = "activity_launch_open"
LAUNCH_EVENT_INFIX = ":e:"
SITE_URL = "https://www.droptracker.io"

COMPONENT_BUTTON = 2
BUTTON_STYLE_LINK = 5


def _swap_emoji_text(text: str, foreign: dict) -> str:
    def repl(m):
        fallback = foreign.get(m.group(3))
        return m.group(0) if fallback is None else fallback
    return _CUSTOM_EMOJI_RE.sub(repl, text)


def _launch_url(custom_id: str) -> str:
    prefix = LAUNCH_CUSTOM_ID + LAUNCH_EVENT_INFIX
    if custom_id.startswith(prefix):
        event_id = custom_id[len(prefix):].split(":", 1)[0]
        if event_id.isdigit():
            return f"{SITE_URL}/events/{event_id}"
    return SITE_URL


def _walk(node, foreign: dict):
    if isinstance(node, list):
        return [_walk(x, foreign) for x in node]
    if not isinstance(node, dict):
        if isinstance(node, str):
            return _swap_emoji_text(node, foreign)
        return node
    out = {}
    for k, v in node.items():
        if k == "emoji" and isinstance(v, dict):
            eid = str(v.get("id") or "")
            if eid and eid in foreign:
                fb = foreign[eid]
                if fb:
                    out[k] = {"name": fb}
                continue  # no usable fallback: drop the icon, keep the button
            out[k] = v
        elif k in ("custom_id", "url", "id", "flags", "type", "style"):
            out[k] = v
        else:
            out[k] = _walk(v, foreign)
    if out.get("type") == COMPONENT_BUTTON and str(out.get("custom_id") or "").startswith(
            LAUNCH_CUSTOM_ID):
        url = _launch_url(str(out.pop("custom_id")))
        out["style"] = BUTTON_STYLE_LINK
        out["url"] = url
    return out


def rewrite_body(body: dict, foreign_emojis: Optional[dict] = None) -> dict:
    """A message body made safe for a different Discord application.

    * DropTracker's *application* emojis (``foreign_emojis``: id → Unicode
      fallback, '' to drop) only render for the app that owns them; anywhere
      in the text or on a button they are swapped for the fallback.
    * Activity launch buttons become links to the website.
    * ``nonce``/``enforce_nonce`` are removed — the customer bot sends once
      per lease and acks; a nonce from our retry scheme means nothing to it.
      So is the library's default ``tts: false``.
    """
    body = dict(body or {})
    body.pop("nonce", None)
    body.pop("enforce_nonce", None)
    if body.get("tts") is False:
        body.pop("tts")  # the library's default, not a choice
    return _walk(body, foreign_emojis or {})


# --------------------------------------------------------------------------- #
# Interactions (inbound)
# --------------------------------------------------------------------------- #

INTERACTION_COMPONENT = 3

#: custom_id prefix → how the customer bot must defer before forwarding.
#: ``ephemeral`` = type 5 + flags 64 (a private "thinking" reply),
#: ``update`` = type 6 (acknowledge, edit the clicked message later).
#: Order matters: the longest prefix sharing a stem comes first.
INTERACTION_RULES = (
    ("evtsignup_acct:", "update"),
    ("evtsignup_team:", "update"),
    ("evtsignup:", "ephemeral"),
    ("dtrv:", "update"),
)


def interaction_defer(custom_id) -> Optional[str]:
    cid = str(custom_id or "")
    for prefix, mode in INTERACTION_RULES:
        if cid.startswith(prefix):
            return mode
    return None


def interaction_facts(payload: dict) -> dict:
    from utils.interaction_payload import interaction_user_id

    data = payload.get("data") or {}
    return {
        "id": str(payload.get("id") or ""),
        "token": str(payload.get("token") or ""),
        "application_id": str(payload.get("application_id") or ""),
        "type": payload.get("type"),
        "guild_id": str(payload.get("guild_id") or ""),
        "channel_id": str(payload.get("channel_id") or
                          (payload.get("channel") or {}).get("id") or ""),
        "custom_id": str(data.get("custom_id") or ""),
        # A relaying bot's library may have popped member.user already.
        "user_id": interaction_user_id(payload) or "",
    }


def validate_interaction(payload, application_id, guild_ids) -> tuple:
    """``(defer_mode, None)`` for a click we will take, else ``(None, error)``.

    Structural checks only — whether the clicked target belongs to the
    integration's group is checked by the core bot, which can see the DB.
    """
    if not isinstance(payload, dict):
        return None, "body must be the raw interaction object"
    f = interaction_facts(payload)
    if f["type"] != INTERACTION_COMPONENT:
        return None, "only message component interactions are relayed"
    if not f["id"] or not f["token"]:
        return None, "interaction id and token are required"
    if not application_id:
        return None, "this integration has no application id configured"
    if f["application_id"] != str(application_id):
        return None, "interaction belongs to a different application"
    if f["guild_id"] not in {str(g) for g in guild_ids or []}:
        return None, "interaction is not from a server this integration owns"
    if not f["user_id"]:
        return None, "interaction carries no user"
    mode = interaction_defer(f["custom_id"])
    if mode is None:
        return None, "not a DropTracker component"
    return mode, None


def push_interaction(r, iid, group_id, payload: dict, defer: str, now=None) -> None:
    r.lpush(INTERACTION_QUEUE, json.dumps({
        "iid": int(iid), "group_id": int(group_id), "defer": defer,
        "payload": payload, "received": _now(now),
    }, separators=(",", ":")))
    r.ltrim(INTERACTION_QUEUE, 0, 999)


def push_resync(r, iid, group_id, event_id=None, now=None) -> None:
    r.lpush(RESYNC_QUEUE, json.dumps({
        "iid": int(iid), "group_id": int(group_id),
        "event_id": int(event_id) if event_id else None, "at": _now(now),
    }))
    r.ltrim(RESYNC_QUEUE, 0, 99)
