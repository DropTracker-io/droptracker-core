"""Clan chat mirror intake (``type=clan_chat``): game → Discord direction of
the chat bridge.

The plugin relays ``CLAN_CHAT`` player lines (only when its bridge toggle is
on) as ``{clan_name, sender, rank, message}`` plus the standard relayer
identity fields. This processor authenticates the RELAYER, binds the line to
the relayer's bridge-enabled groups (``clan_chat_name`` match — same trust
shape as clan_broadcast), collapses the copies N relaying clanmates produce,
and stages the line for the core bot's batched channel send
(``services/clan_chat_bridge``). No DB rows: mirrored chatter is ephemeral.

Deliberately NOT reusing the broadcast processor: broadcasts are system lines
that become tracked records; this is player speech that becomes display. The
only shared pieces are the binding rule and the dedupe idiom.
"""

import hashlib
from datetime import datetime

from utils.clan_broadcasts import clan_slug, clean_broadcast_text

from .common import (
    RedisClient,
    SubmissionResponse,
    ensure_player_and_auth,
    select_session_and_flag,
)
from .raid_dedupe import _bundle_is_new

redis_client = RedisClient()

#: Repeated identical lines are legitimate in chat ("gz", "grats") — keep the
#: multi-relayer window short, like the interval between two humans typing the
#: same thing, not the interval between two relayers seeing one line.
CHAT_SEEN_TTL_SECONDS = 10

#: Chat is chattier than broadcasts; still far above any human channel's rate.
RELAYER_RATE_LIMIT_PER_MIN = 120

#: Defensive caps (the client caps chat at ~80 visible chars already).
MESSAGE_MAX_CHARS = 200
SENDER_MAX_CHARS = 32


def _relayer_within_rate_limit(relayer_player_id: int) -> bool:
    try:
        minute = int(datetime.now().timestamp() // 60)
        key = f"chatbridge:rate:{relayer_player_id}:{minute}"
        client = redis_client.client
        count = client.incr(key)
        if count == 1:
            client.expire(key, 120)
        return int(count) <= RELAYER_RATE_LIMIT_PER_MIN
    except Exception:
        return True


async def clan_chat_processor(chat_data, external_session=None, world_type="main"):
    """Process one relayed clan chat line. See module docstring."""

    session, _use_external_session = select_session_and_flag(external_session)

    if world_type != "main":
        return SubmissionResponse(True, "Clan chat is not bridged for this world type")

    message = clean_broadcast_text(chat_data.get("message"))[:MESSAGE_MAX_CHARS]
    sender = clean_broadcast_text(chat_data.get("sender"))[:SENDER_MAX_CHARS]
    clan_name = chat_data.get("clan_name")
    relayer_name = chat_data.get("player_name", chat_data.get("player"))
    account_hash = chat_data.get("acc_hash")
    if not message or not sender or not clan_name or not relayer_name or not account_hash:
        return SubmissionResponse(False, "Missing clan chat fields")

    relayer, authed, user_exists = await ensure_player_and_auth(
        session, str(relayer_name).strip(), str(account_hash), chat_data.get("auth_key")
    )
    if not relayer or not user_exists or not authed:
        return SubmissionResponse(False, "Relayer failed auth check")
    if not _relayer_within_rate_limit(relayer.player_id):
        return SubmissionResponse(False, "Clan chat rate limit exceeded")

    slug = clan_slug(clan_name)
    if not slug:
        return SubmissionResponse(False, "Missing clan name")

    from services.clan_chat_bridge import bridge_bound_groups, push_mirror_line

    bound = bridge_bound_groups(session, relayer.player_id, slug)
    if not bound:
        return SubmissionResponse(
            False, "No group of yours has the clan chat bridge enabled for this clan"
        )

    digest = hashlib.sha256(f"{sender}|{message}".encode("utf-8")).hexdigest()[:24]
    if not _bundle_is_new(f"chatbridge:seen:{slug}:{digest}", CHAT_SEEN_TTL_SECONDS):
        return SubmissionResponse(True, "Line already relayed by another clanmate")

    staged = 0
    for group_id, channel_id in bound.items():
        if push_mirror_line(group_id, channel_id, sender, message, rank=chat_data.get("rank")):
            staged += 1
    return SubmissionResponse(True, f"Clan chat line staged for {staged} group(s)")
