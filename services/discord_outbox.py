"""Discord outbox: Web API enqueues, the Discord bot drains (Tasks 09 + 12).

The Web API must never open a Discord connection (§10.2). It writes rows to the
``discord_outbox`` table via :func:`enqueue`; the bot calls :func:`drain_once`
on an interval to send them and (for announcements) write back the resulting
``discord_message_id`` onto the announcement row.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from db.models import DiscordOutbox, Announcement


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


async def drain_once(bot, session_factory, limit: int = 20) -> int:
    """Send up to ``limit`` pending outbox rows via ``bot``. Returns the number
    processed. Safe to call repeatedly; failures mark the row 'failed'.

    ``session_factory`` is a callable returning a fresh Session (e.g. ``Session``).
    """
    sent = 0
    session = session_factory()
    try:
        rows = (
            session.query(DiscordOutbox)
            .filter(DiscordOutbox.status == "pending")
            .order_by(DiscordOutbox.created_at.asc())
            .limit(limit)
            .all()
        )
        for row in rows:
            try:
                channel = await bot.fetch_channel(int(row.channel_id))
                if channel is None:
                    raise RuntimeError(f"channel {row.channel_id} not found")

                kwargs = {}
                if row.content:
                    kwargs["content"] = row.content[:2000]
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
                sent += 1
            except Exception as e:  # noqa: BLE001
                row.status = "failed"
                row.error = str(e)[:500]
                row.processed_at = datetime.now()
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"[discord_outbox] drain_once error: {e}")
    finally:
        session.close()
    return sent
