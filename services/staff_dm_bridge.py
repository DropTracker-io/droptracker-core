"""Discord DM → staff_dm chat ingestion (web102a).

Loaded by the **core bot** — the same application that sends the outbound
relay DMs (services/staff_dm.py via the outbox drain), so a user tapping
"reply" on one of those DMs lands here.

Routing is unambiguous by construction: a staff_dm thread's subject is
``('user', user_id)`` and the unique subject triple caps it at one thread per
user, so an inbound DM maps to exactly zero or one open thread.

Echo-prevention invariants (the ones that make a loop structurally
impossible):
  1. every outbound relay is bot-authored → the bot-author guard here can
     never re-ingest our own traffic;
  2. ingested rows carry the DM's ``discord_message_id`` (unique column) →
     gateway redelivery is idempotent.

A DM from someone with no open thread gets one polite pointer per 24h and is
otherwise ignored — bots receive plenty of stray DMs, and an unmonitored
inbox that pretends to listen is worse than one that says it doesn't.
"""
from __future__ import annotations

import asyncio

from interactions import Extension, listen
from interactions.api.events import MessageCreate

from utils.redis import redis_client

#: Same budget as the web chat route: a conversation, not a firehose.
_INGEST_LIMIT = 20
_INGEST_WINDOW_SECONDS = 60

_NO_THREAD_REPLY = (
    "This inbox isn't monitored. If you need help, open a ticket or reply to "
    "your conversation at <https://www.droptracker.io/tickets> — staff "
    "messages you receive here always carry a reply link."
)


def _rate_limited(user_id: int) -> bool:
    """Fixed window per user; fails open (a Redis outage must not eat a
    support reply)."""
    try:
        import time as _time

        window = int(_time.time()) // _INGEST_WINDOW_SECONDS
        key = f"staffdm:ingest:{int(user_id)}:{window}"
        count = int(redis_client.client.incr(key))
        if count == 1:
            redis_client.client.expire(key, _INGEST_WINDOW_SECONDS * 2)
        return count > _INGEST_LIMIT
    except Exception:
        return False


def _pointer_debounced(discord_id: str) -> bool:
    """True when the 24h "this inbox isn't monitored" reply was already sent."""
    try:
        return not bool(
            redis_client.client.set(
                f"staffdm:noop:{discord_id}", "1", nx=True, ex=86400
            )
        )
    except Exception:
        return True  # when unsure, stay silent


def _attachment_refs(message) -> list[dict]:
    """Record DM attachments as CDN references. Discord CDN links expire —
    accepted for v1; mirroring bytes to B2 is the phase-2 upgrade."""
    out = []
    for att in list(getattr(message, "attachments", None) or [])[:4]:
        out.append(
            {
                "url": str(getattr(att, "url", "") or ""),
                "key": None,
                "content_type": getattr(att, "content_type", None),
                "filename": getattr(att, "filename", None),
            }
        )
    return out


def _ingest(discord_id: str, message_id: str, content: str,
            attachments: list[dict]):
    """DB half, run off the event loop. Returns 'ok' | 'no_user' |
    'no_thread' | 'duplicate' | 'error'."""
    from db.models import ChatMessage, Session, User
    from services.chat import Party, mark_read, post_message
    from services.staff_dm import find_open_staff_thread

    s = Session()
    try:
        user = s.query(User).filter(User.discord_id == str(discord_id)).first()
        if user is None:
            return "no_user"
        thread = find_open_staff_thread(s, user.user_id)
        if thread is None:
            return "no_thread"
        existing = (
            s.query(ChatMessage.id)
            .filter(ChatMessage.discord_message_id == str(message_id))
            .first()
        )
        if existing is not None:
            return "duplicate"
        row = post_message(
            s,
            thread=thread,
            author_user_id=int(user.user_id),
            party=Party("user", int(user.user_id)),
            body=content or ("(attachment)" if attachments else ""),
            attachments=None,
            source="discord_dm",
            discord_message_id=str(message_id),
            commit=False,
            publish=False,
        )
        if attachments:
            import json as _json

            row.attachments_json = _json.dumps(attachments)
        s.flush()
        # Replying from Discord proves the author is caught up — no site badge.
        mark_read(s, thread.id, int(user.user_id), row.id, commit=False)
        s.commit()
        from services.chat import publish_message

        publish_message(s, thread, row)
        return "ok"
    except Exception as e:  # noqa: BLE001
        s.rollback()
        # Unique-key race on redelivery counts as already-stored.
        if "uq_chat_message_discord_id" in str(e) or "Duplicate entry" in str(e):
            return "duplicate"
        print(f"[staff_dm_bridge] ingest failed (msg {message_id}): {e}")
        return "error"
    finally:
        s.close()


class StaffDmBridge(Extension):
    @listen(MessageCreate)
    async def on_dm_message(self, event: MessageCreate):
        message = event.message
        try:
            author = getattr(message, "author", None)
            if author is None or getattr(author, "bot", False):
                return
            if getattr(message, "guild", None) is not None:
                return  # guild traffic is other listeners' business
            discord_id = str(author.id)
            content = (message.content or "").strip()
            attachments = _attachment_refs(message)
            if not content and not attachments:
                return
            if _rate_limited(int(author.id)):
                try:
                    await message.add_reaction("⏳")
                except Exception:
                    pass
                return
            result = await asyncio.to_thread(
                _ingest, discord_id, str(message.id), content[:2000], attachments
            )
            if result == "ok":
                try:
                    await message.add_reaction("✅")
                except Exception:
                    pass
            elif result == "no_thread":
                if not _pointer_debounced(discord_id):
                    try:
                        await message.reply(_NO_THREAD_REPLY)
                    except Exception:
                        pass
            # no_user / duplicate / error: stay silent — strangers get
            # nothing, redelivery already succeeded once, and an error is
            # ours to fix, not the user's.
        except Exception as e:  # noqa: BLE001
            print(f"[staff_dm_bridge] DM handling failed: {e}")


def setup(bot):
    StaffDmBridge(bot)
