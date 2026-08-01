"""Discord outbox: Web API enqueues, the Discord bot drains (Tasks 09 + 12).

The Web API must never open a Discord connection (§10.2). It writes rows to the
``discord_outbox`` table via :func:`enqueue`; the bot calls :func:`drain_once`
on an interval to send them and (for announcements) write back the resulting
``discord_message_id`` onto the announcement row.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

from db.models import DiscordOutbox, Announcement

_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")

# How long a claimed ('sending') row may sit before a later drain assumes the
# claimer died and takes it back. Comfortably longer than any single send,
# so a slow send is never stolen out from under the drain that owns it.
RECLAIM_AFTER_MINUTES = 15


def _allowed_mentions_for(content: str):
    """AllowedMentions covering exactly the mentions present in ``content``.

    Explicit rather than Discord's parse-everything default: only the pings
    the author selected (role/user tokens, @everyone/@here) may fire, and
    nothing an embed or later content tweak sneaks in.
    """
    try:
        from interactions import AllowedMentions
    except Exception:
        return None
    role_ids = _ROLE_MENTION_RE.findall(content)
    user_ids = [u for u in _USER_MENTION_RE.findall(content) if u not in role_ids]
    everyone = "@everyone" in content or "@here" in content
    if not role_ids and not user_ids and not everyone:
        return None
    kwargs = {}
    if role_ids:
        kwargs["roles"] = role_ids
    if user_ids:
        kwargs["users"] = user_ids
    if everyone:
        kwargs["parse"] = ["everyone"]
    return AllowedMentions(**kwargs)


def enqueue(
    session,
    *,
    channel_id: str,
    content: Optional[str] = None,
    embed: Optional[dict] = None,
    kind: str = "message",
    ref_type: Optional[str] = None,
    ref_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    commit: bool = True,
) -> DiscordOutbox:
    """Insert a pending outbox row. Caller supplies the session."""
    row = DiscordOutbox(
        kind=kind,
        channel_id=str(channel_id),
        content=content,
        embed_json=json.dumps(embed) if embed else None,
        ref_type=ref_type,
        ref_id=ref_id,
        actor_user_id=actor_user_id,
        status="pending",
    )
    session.add(row)
    if commit:
        session.commit()
    return row


_FORUM_NAME_PREFIX = {"suggestion": "\N{ELECTRIC LIGHT BULB} ", "bug": "\N{BUG} "}


async def _create_forum_post(bot, session, row):
    """Send a ``kind='forum_post'`` outbox row: create a post (thread +
    starter message) in the target forum channel. For suggestion rows the
    thread name comes from the referenced ``suggestions`` row, whose
    ``discord_thread_id``/``status`` are written back on success."""
    from db.models import Suggestion

    suggestion = None
    if row.ref_type == "suggestion" and row.ref_id:
        suggestion = session.query(Suggestion).filter(Suggestion.id == row.ref_id).first()

    channel = await bot.fetch_channel(int(row.channel_id))
    if channel is None:
        raise RuntimeError(f"channel {row.channel_id} not found")
    if not hasattr(channel, "create_post"):
        raise RuntimeError(f"channel {row.channel_id} is not a forum channel")

    name = (suggestion.title if suggestion else None) or "New submission"
    if suggestion is not None:
        name = _FORUM_NAME_PREFIX.get(suggestion.type, "") + name
    content = (row.content or "")[:2000]

    kwargs = {}
    allowed = _allowed_mentions_for(content)
    if allowed is not None:
        kwargs["allowed_mentions"] = allowed
    # Apply a matching tag when the forum defines one (e.g. "Suggestion").
    if suggestion is not None:
        try:
            tag = channel.get_tag(suggestion.type, case_insensitive=True)
            if tag is not None:
                kwargs["applied_tags"] = [tag]
        except Exception:
            pass

    post = await channel.create_post(name[:100], content, **kwargs)
    thread_id = str(getattr(post, "id", "") or "")
    row.discord_message_id = thread_id
    if suggestion is not None and thread_id:
        suggestion.discord_thread_id = thread_id
        suggestion.status = "posted"


def _mark_ref_failed(session, row) -> None:
    """Propagate a send failure onto the referenced suggestion row so the
    website can surface it instead of showing 'pending' forever."""
    if row.kind != "forum_post" or row.ref_type != "suggestion" or not row.ref_id:
        return
    try:
        from db.models import Suggestion

        suggestion = session.query(Suggestion).filter(Suggestion.id == row.ref_id).first()
        if suggestion is not None:
            suggestion.status = "failed"
    except Exception:
        pass


async def drain_once(bot, session_factory, limit: int = 20) -> int:
    """Send up to ``limit`` pending outbox rows via ``bot``. Returns the number
    processed. Safe to call repeatedly; failures mark the row 'failed'.

    ``session_factory`` is a callable returning a fresh Session (e.g. ``Session``).
    """
    sent = 0
    session = session_factory()
    try:
        # Reclaim rows a previous drain claimed and never resolved (a crash
        # between the claim and the outcome). Bounded by age so it can never
        # steal a row the running drain is mid-send on.
        stale = datetime.now() - timedelta(minutes=RECLAIM_AFTER_MINUTES)
        reclaimed = (
            session.query(DiscordOutbox)
            .filter(DiscordOutbox.status == "sending",
                    DiscordOutbox.created_at < stale)
            .update({"status": "pending"}, synchronize_session=False)
        )
        if reclaimed:
            session.commit()
            print(f"[discord_outbox] reclaimed {reclaimed} stranded row(s)")

        rows = (
            session.query(DiscordOutbox)
            .filter(DiscordOutbox.status == "pending")
            .order_by(DiscordOutbox.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        # Claim the batch before anything is sent. Previously the whole batch's
        # statuses lived in memory until a single commit at the end, so a crash
        # (or a failure in that commit) re-posted every message that had already
        # gone out, and two overlapping drains could both pick up the same rows.
        for row in rows:
            row.status = "sending"
        session.commit()

        for row in rows:
            try:
                if row.kind == "forum_post":
                    await _create_forum_post(bot, session, row)
                    row.status = "sent"
                    row.processed_at = datetime.now()
                    sent += 1
                    continue

                channel = await bot.fetch_channel(int(row.channel_id))
                if channel is None:
                    raise RuntimeError(f"channel {row.channel_id} not found")

                kwargs = {}
                if row.content:
                    kwargs["content"] = row.content[:2000]
                    allowed = _allowed_mentions_for(kwargs["content"])
                    if allowed is not None:
                        kwargs["allowed_mentions"] = allowed
                if row.embed_json:
                    try:
                        from interactions import Embed

                        data = json.loads(row.embed_json)
                        kwargs["embeds"] = [Embed.from_dict(data)]
                    except Exception:
                        pass

                message = await channel.send(**kwargs) if kwargs else None
                row.status = "sent"
                row.processed_at = datetime.now()
                if message is not None:
                    row.discord_message_id = str(getattr(message, "id", "") or "")
                    # Write the message id back onto the announcement for sync.
                    if row.ref_type == "announcement" and row.ref_id:
                        ann = (
                            session.query(Announcement)
                            .filter(Announcement.id == row.ref_id)
                            .first()
                        )
                        if ann:
                            ann.discord_message_id = row.discord_message_id
                            ann.discord_channel_id = str(row.channel_id)
                    # Web forum replies: record the relayed message id so the
                    # Discord mirror can map edits back to this row.
                    elif row.ref_type == "suggestion_message" and row.ref_id:
                        from db.models import SuggestionMessage

                        sm = (
                            session.query(SuggestionMessage)
                            .filter(SuggestionMessage.id == row.ref_id)
                            .first()
                        )
                        if sm:
                            sm.discord_message_id = row.discord_message_id
                sent += 1
            except Exception as e:  # noqa: BLE001
                row.status = "failed"
                row.error = str(e)[:500]
                row.processed_at = datetime.now()
                _mark_ref_failed(session, row)
            finally:
                # Per row, and in a finally so the forum-post branch's
                # `continue` can't skip it: this row's outcome must be durable
                # before the next send begins, or a later crash resurrects it
                # as pending and posts it twice.
                try:
                    session.commit()
                except Exception as commit_error:  # noqa: BLE001
                    session.rollback()
                    print(f"[discord_outbox] could not record row {row.id}: {commit_error}")
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[discord_outbox] drain_once error: {e}")
    finally:
        session.close()
    return sent
