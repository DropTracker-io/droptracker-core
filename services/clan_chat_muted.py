"""Muted clanmates in the chat bridge: their lines reach the rest of the clan.

A Jagex-muted player's clan chat line never leaves their client. The game
still prints it in their OWN chat box as if it had been sent, so the plugin
relays it like any other line, and nobody else in the clan ever sees it.

The client cannot tell the plugin it is muted (no varp or varbit carries it),
so the server works it out from who reported each line. Every relayer's copy
of a chat line is recorded in the ``chatbridge:seen:{slug}:{digest}`` hash
(``clan_chat_bridge.claim_relayed_line``). A line the sender reported about
themselves (``self``), while other bridge members were online in the clan,
that no other relayer reports within :data:`CHECK_WINDOW_SECONDS` was not
delivered by the game. The sender is then flagged muted
(:data:`MUTED_FLAG_TTL_SECONDS`, refreshed per unheard line), and later lines
fan out at intake instead of waiting for the window. The first line from that
sender that someone else does report clears the flag, which is how a lifted
mute is noticed.

Delivery rides the plugin-notification inbox like Discord lines do, as a
:data:`ENVELOPE_TYPE` envelope that the plugin renders as real clan chat with
a muted icon by the name. Builds that predate the type drop it unread. A
recipient's plugin also drops the line if the same text already arrived as
real clan chat, so a wrong guess (a slow witness, a mute that just lifted)
shows nothing twice. The plugin never relays an injected line onward, so a
copy from a recipient is always one the game delivered; that is what lets a
lifted mute be noticed even after its lines were sent straight out.

Only the authenticated relayer can speak for themselves: ``clan_chat``
accepts a ``self`` line only when the sender is the relayer's own name, so
no one can put words in another clanmate's mouth through this path.

Module-level imports are stdlib-only (same contract as clan_chat_bridge.py).
"""
from __future__ import annotations

import json
import time

#: Group config key (registry: web_api/config_registry.py). Default on: it
#: only acts inside clans that already run the two-way bridge.
MUTED_RELAY_KEY = "clan_chat_relay_muted"
MUTED_RELAY_DEFAULT = True

#: Envelope type the plugin renders as a muted clanmate's clan chat line.
ENVELOPE_TYPE = "clan_chat_muted"

#: How long other relayers get to report a self line before it counts as
#: unheard. Relayer copies normally land within a few seconds of each other;
#: a later one only costs a duplicate the recipient's plugin suppresses.
CHECK_WINDOW_SECONDS = 12

#: Pending checks: ZSET of JSON records scored by their due time.
CHECK_QUEUE_KEY = "chatbridge:mutedcheck"
CHECK_DRAIN_BATCH = 200
#: Lines older than this are settled but not delivered (the plugin drops
#: bridge lines past 120s anyway).
STALE_AFTER_SECONDS = 90

MUTED_FLAG_KEY_TEMPLATE = "chatbridge:muted:{clan_slug}:{player_id}"
#: Mutes run hours to permanent; a quiet day re-derives it from one line.
MUTED_FLAG_TTL_SECONDS = 6 * 3600

#: Inbox recipients of one line, so a second pass never delivers twice.
DELIVERED_KEY_TEMPLATE = "chatbridge:mutedsent:{clan_slug}:{digest}"
DELIVERED_TTL_SECONDS = 180

#: Matches clan_chat.CHAT_SEEN_TTL_SECONDS: the seen hash must outlive the
#: check window.
SEEN_KEY_TEMPLATE = "chatbridge:seen:{clan_slug}:{digest}"


def _redis():
    from utils.redis import RedisClient

    return RedisClient().client


def _online(clan_slug: str) -> list:
    from services.clan_chat_bridge import online_player_ids

    return online_player_ids(clan_slug)


def _push(player_id, payload: dict) -> bool:
    from services.plugin_notifications import build_envelope, push_to_inbox

    return push_to_inbox(player_id, build_envelope(ENVELOPE_TYPE, payload))


def relay_enabled(session, group_ids) -> bool:
    """Whether any of these (bridge-bound) groups relays muted members."""
    from utils import group_config as gc

    ids = [int(g) for g in group_ids or []]
    if not ids:
        return False
    values = gc.get_bulk(session, ids, [MUTED_RELAY_KEY])
    for gid in ids:
        raw = values.get((gid, MUTED_RELAY_KEY))
        if raw is None or str(raw).strip() == "":
            if MUTED_RELAY_DEFAULT:
                return True
            continue
        if gc.is_truthy(raw):
            return True
    return False


def is_flagged(clan_slug: str, player_id) -> bool:
    """Whether this sender was recently seen speaking unheard in this clan."""
    try:
        key = MUTED_FLAG_KEY_TEMPLATE.format(clan_slug=clan_slug, player_id=int(player_id))
        return bool(_redis().exists(key))
    except Exception:
        return False


def clear_flag(clan_slug: str, player_id) -> None:
    """The sender was just heard by someone else: no longer muted."""
    try:
        _redis().delete(MUTED_FLAG_KEY_TEMPLATE.format(clan_slug=clan_slug, player_id=int(player_id)))
    except Exception:
        pass


def _heard_by_others(seen: dict, sender_id: int) -> bool:
    """Whether any relayer other than the sender reported the line. ``seen`` is the claim hash: relayer id ->
    copy count, plus the ``_shown`` bookkeeping field."""
    for field in seen or {}:
        if isinstance(field, bytes):
            field = field.decode("utf-8", "replace")
        if field == "_shown":
            continue
        try:
            relayer = int(field)
        except (TypeError, ValueError):
            continue
        if relayer != sender_id:
            return True
    return False


def _decode_ids(values) -> set:
    out = set()
    for v in values or ():
        try:
            out.add(int(v.decode("utf-8") if isinstance(v, bytes) else v))
        except (TypeError, ValueError):
            continue
    return out


def _deliver(record: dict, exclude: set) -> int:
    """Push the line to every online bridge member not in ``exclude``."""
    slug = record["slug"]
    recipients = [pid for pid in _online(slug) if pid not in exclude]
    if not recipients:
        return 0
    payload = {"sender": record["sender"], "message": record["message"]}
    if record.get("account_type"):
        payload["account_type"] = record["account_type"]
    delivered = []
    for pid in recipients:
        if _push(pid, payload):
            delivered.append(pid)
    if delivered:
        try:
            key = DELIVERED_KEY_TEMPLATE.format(clan_slug=slug, digest=record["digest"])
            pipe = _redis().pipeline()
            pipe.sadd(key, *[str(p) for p in delivered])
            pipe.expire(key, DELIVERED_TTL_SECONDS)
            pipe.execute()
        except Exception:
            pass
    return len(delivered)


def on_self_line(clan_slug: str, digest: str, sender_id, sender: str, message: str,
                 account_type=None, now: float = None) -> int:
    """Intake hook for a line the relayer reported about themselves.

    Queues the heard/unheard check, and when the sender is already flagged
    muted, delivers right away. Returns the inboxes pushed now. Lines with no
    other bridge member online are not queued: nobody could have reported
    them, so they prove nothing either way."""
    from utils.mirror_context import is_mirrored_submission

    # Mirrored production chat must never reach a dev tester's game.
    if is_mirrored_submission():
        return 0
    now = time.time() if now is None else now
    sender_id = int(sender_id)
    witnesses = [pid for pid in _online(clan_slug) if pid != sender_id]
    if not witnesses:
        return 0
    record = {
        "slug": clan_slug,
        "digest": digest,
        "sender_id": sender_id,
        "sender": str(sender)[:32],
        "message": str(message),
        "account_type": account_type or None,
        "ts": int(now),
    }
    delivered = 0
    if is_flagged(clan_slug, sender_id):
        delivered = _deliver(record, {sender_id})
    try:
        _redis().zadd(CHECK_QUEUE_KEY, {json.dumps(record): now + CHECK_WINDOW_SECONDS})
    except Exception as e:
        print(f"[ClanChatMuted] check queue push failed: {e}")
    return delivered


def _settle(record: dict) -> str:
    """Decide one due line. Returns "heard", "unheard" or "error"."""
    slug = record["slug"]
    digest = record["digest"]
    sender_id = int(record["sender_id"])
    client = _redis()
    pipe = client.pipeline()
    pipe.hgetall(SEEN_KEY_TEMPLATE.format(clan_slug=slug, digest=digest))
    pipe.smembers(DELIVERED_KEY_TEMPLATE.format(clan_slug=slug, digest=digest))
    seen, delivered_raw = pipe.execute()
    delivered = _decode_ids(delivered_raw)
    flag_key = MUTED_FLAG_KEY_TEMPLATE.format(clan_slug=slug, player_id=sender_id)

    if _heard_by_others(seen, sender_id):
        client.delete(flag_key)
        return "heard"

    client.set(flag_key, "1", ex=MUTED_FLAG_TTL_SECONDS)
    # A backlog (the core bot was down) is history, not conversation.
    if time.time() - int(record.get("ts") or 0) <= STALE_AFTER_SECONDS:
        _deliver(record, {sender_id} | delivered)
    return "unheard"


def process_due_checks(now: float = None, limit: int = CHECK_DRAIN_BATCH) -> dict:
    """Settle every queued line whose window has closed. Called from the core
    bot's bridge drain task, off the event loop. Single consumer."""
    now = time.time() if now is None else now
    counts = {"heard": 0, "unheard": 0, "error": 0}
    try:
        client = _redis()
        due = client.zrangebyscore(CHECK_QUEUE_KEY, "-inf", now, start=0, num=int(limit))
        if not due:
            return counts
        client.zrem(CHECK_QUEUE_KEY, *due)
    except Exception as e:
        print(f"[ClanChatMuted] check queue read failed: {e}")
        return counts
    for raw in due:
        try:
            record = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            counts[_settle(record)] += 1
        except Exception as e:
            counts["error"] += 1
            print(f"[ClanChatMuted] settle failed: {e}")
    if counts["unheard"]:
        _bump_stats(counts)
    return counts


def _bump_stats(counts: dict) -> None:
    try:
        day = time.strftime("%Y%m%d", time.gmtime())
        key = f"clanchat:stats:{day}"
        pipe = _redis().pipeline()
        pipe.hincrby(key, "muted_unheard", int(counts.get("unheard", 0)))
        pipe.expire(key, 14 * 86400)
        pipe.execute()
    except Exception:
        pass
