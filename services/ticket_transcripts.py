"""Ticket transcript archiving (web21a).

Every message posted in a ticket channel is mirrored into ticket_messages so
the conversation survives the channel deletion that happens at close time.
Three writers share the same idempotent upsert (unique discord_message_id):

  1. Live mirroring — Tickets extension MessageCreate/MessageUpdate listeners.
  2. Close-time re-archive — the FULL channel history is synced right before
     the channel is deleted, which heals any gap from bot downtime. A channel
     is never deleted unless this pass succeeds.
  3. Startup backfill — on webhook-bot startup every open ticket's history is
     synced once, which also seeds transcripts for tickets that predate this
     feature.

Attachments are downloaded to static/assets/img/tickets/{ticket_id}/ (served
by the legacy image server as www.droptracker.io/img/tickets/...) because
Discord CDN URLs are signed and expire.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Optional

import aiohttp

from db.models import Session, Ticket, TicketMessage, User

# Staff/support roles (same ids the close-permission checks use).
STAFF_ROLE_IDS = {1342871954885050379, 1176291872143052831}

# Served by web/front.py's /img/<path> route (send_from_directory relative to
# the repo root), i.e. https://www.droptracker.io/img/tickets/...
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKET_IMG_ROOT = os.path.join(_REPO_ROOT, "static", "assets", "img", "tickets")

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    name = _SAFE_NAME.sub("_", os.path.basename(name or "file"))
    return name[-100:] if len(name) > 100 else name


def _is_staff_author(author) -> bool:
    roles = getattr(author, "roles", None) or []
    for role in roles:
        try:
            if int(role.id) in STAFF_ROLE_IDS:
                return True
        except Exception:
            continue
    return False


async def _download_attachments(ticket_id: int, message) -> Optional[str]:
    """Mirror a message's attachments to disk; returns attachments_json or None.

    Failures degrade to recording the original (expiring) CDN URL so the
    transcript still shows that a file existed.
    """
    attachments = list(getattr(message, "attachments", None) or [])
    if not attachments:
        return None
    dest_dir = os.path.join(TICKET_IMG_ROOT, str(ticket_id))
    os.makedirs(dest_dir, exist_ok=True)
    entries = []
    for i, att in enumerate(attachments):
        filename = _safe_filename(getattr(att, "filename", "") or f"file_{i}")
        entry = {
            "filename": filename,
            "content_type": getattr(att, "content_type", None),
            "size": getattr(att, "size", None),
            "path": None,
        }
        disk_name = f"{message.id}_{i}_{filename}"
        try:
            if (att.size or 0) <= MAX_ATTACHMENT_BYTES:
                async with aiohttp.ClientSession() as http:
                    async with http.get(att.url) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                with open(os.path.join(dest_dir, disk_name), "wb") as f:
                    f.write(data)
                entry["path"] = f"tickets/{ticket_id}/{disk_name}"
        except Exception as e:  # noqa: BLE001
            print(f"[tickets] attachment mirror failed (ticket {ticket_id}, msg {message.id}): {e}")
        if entry["path"] is None:
            entry["original_url"] = str(att.url)
        entries.append(entry)
    return json.dumps(entries)


def _resolve_author_user_id(session, discord_id: str) -> Optional[int]:
    row = session.query(User.user_id).filter(User.discord_id == str(discord_id)).first()
    return row[0] if row else None


def _plain_datetime(value) -> datetime:
    """Coerce to an exact ``datetime``. interactions' Timestamp subclasses
    datetime, and PyMySQL only type-maps exact datetime — a subclass falls
    back to ``str()``, which for Timestamp is Discord's ``<t:...>`` format
    and gets rejected by MySQL."""
    if isinstance(value, datetime):
        return datetime.fromtimestamp(value.timestamp())
    return datetime.now()


async def upsert_message(ticket: Ticket, message, *, session=None) -> Optional[bool]:
    """Mirror one Discord message into ticket_messages.

    Returns True if newly inserted, False if already stored (or skipped),
    None on failure — archiving treats None as fatal so a channel is never
    deleted with an incomplete transcript.
    Idempotent on discord_message_id; edits refresh content/date_edited.
    """
    author = message.author
    content = (message.content or "").strip()
    if not content and not getattr(message, "attachments", None):
        # Nothing displayable (e.g. bare embeds from the bot's own panels).
        if getattr(author, "bot", False):
            return False
    own_session = session is None
    s = session or Session()
    try:
        existing = (
            s.query(TicketMessage)
            .filter(TicketMessage.discord_message_id == str(message.id))
            .first()
        )
        if existing is not None:
            if (existing.content or "") != content:
                existing.content = content
                existing.date_edited = datetime.now()
                s.commit()
            return False

        attachments_json = await _download_attachments(ticket.ticket_id, message)
        is_bot = bool(getattr(author, "bot", False))
        row = TicketMessage(
            ticket_id=ticket.ticket_id,
            discord_message_id=str(message.id),
            author_user_id=_resolve_author_user_id(s, str(author.id)),
            author_discord_id=str(author.id),
            author_name=str(getattr(author, "display_name", None) or getattr(author, "username", None) or author.id)[:100],
            is_staff=_is_staff_author(author),
            is_bot=is_bot,
            kind="message",
            content=content,
            attachments_json=attachments_json,
            date_sent=_plain_datetime(getattr(message, "created_at", None)),
        )
        s.add(row)

        # First real message from the ticket creator becomes the subject.
        if not ticket.subject and not is_bot and content:
            creator_discord = (
                s.query(User.discord_id).filter(User.user_id == ticket.created_by).first()
            )
            if creator_discord and str(creator_discord[0]) == str(author.id):
                live = s.query(Ticket).filter(Ticket.ticket_id == ticket.ticket_id).first()
                if live is not None and not live.subject:
                    live.subject = content[:255]
        if not is_bot:
            live = s.query(Ticket).filter(Ticket.ticket_id == ticket.ticket_id).first()
            if live is not None:
                live.last_reply_uid = str(author.id)
                live.date_updated = datetime.now()
        s.commit()
        return True
    except Exception as e:  # noqa: BLE001
        s.rollback()
        print(f"[tickets] upsert_message failed (ticket {ticket.ticket_id}): {e}")
        return None
    finally:
        if own_session:
            s.close()


def add_system_message(ticket_id: int, text: str, *, actor_name: str = "DropTracker") -> None:
    """Append a synthetic system row (claims, closes) to the transcript."""
    s = Session()
    try:
        s.add(
            TicketMessage(
                ticket_id=ticket_id,
                discord_message_id=None,
                author_user_id=None,
                author_discord_id="0",
                author_name=actor_name[:100],
                is_staff=True,
                is_bot=True,
                kind="system",
                content=text,
                date_sent=datetime.now(),
            )
        )
        s.commit()
    except Exception as e:  # noqa: BLE001
        s.rollback()
        print(f"[tickets] system message failed (ticket {ticket_id}): {e}")
    finally:
        s.close()


def _recompute_subject(ticket_id: int) -> None:
    """Set subject from the EARLIEST archived creator message.

    Needed after history syncs: channel.history() iterates newest-first, so
    the insert-time "first creator message wins" heuristic picks the most
    recent message during a backfill. Live mirroring arrives chronologically
    and is unaffected.
    """
    s = Session()
    try:
        t = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        if t is None:
            return
        row = (
            s.query(TicketMessage.content)
            .filter(
                TicketMessage.ticket_id == ticket_id,
                TicketMessage.author_user_id == t.created_by,
                TicketMessage.is_bot.is_(False),
                TicketMessage.kind == "message",
                TicketMessage.content != "",
            )
            .order_by(TicketMessage.date_sent.asc(), TicketMessage.id.asc())
            .first()
        )
        if row and row[0] and (t.subject or "") != row[0][:255]:
            t.subject = row[0][:255]
            s.commit()
    except Exception as e:  # noqa: BLE001
        s.rollback()
        print(f"[tickets] subject recompute failed ({ticket_id}): {e}")
    finally:
        s.close()


async def archive_channel_history(bot, ticket: Ticket) -> bool:
    """Sync the FULL channel history into ticket_messages.

    Returns True when the channel was fully read (or is already gone), False
    when history could not be fetched — callers must not delete the channel
    in that case.
    """
    try:
        channel = await bot.fetch_channel(int(ticket.channel_id))
    except Exception as e:  # noqa: BLE001
        # 404/unknown channel: nothing left to archive; treat as done so the
        # ticket row can still be closed.
        print(f"[tickets] fetch_channel {ticket.channel_id} failed: {e}")
        return "404" in str(e) or "Unknown Channel" in str(e)
    if channel is None:
        return True
    try:
        failures = 0
        async for message in channel.history(limit=0):
            if await upsert_message(ticket, message) is None:
                failures += 1
        if failures:
            print(f"[tickets] history sync had {failures} failed upserts (ticket {ticket.ticket_id})")
            return False
        _recompute_subject(ticket.ticket_id)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[tickets] history sync failed (ticket {ticket.ticket_id}): {e}")
        return False


async def close_and_archive(
    bot,
    ticket_id: int,
    *,
    closed_by_user_id: Optional[int] = None,
    closed_by_name: str = "staff",
    reason: str = "",
) -> bool:
    """Archive the channel, mark the ticket closed, then delete the channel.

    The deletion only happens after a successful full-history sync so a
    transient Discord error can never destroy an unarchived conversation.
    """
    s = Session()
    try:
        ticket = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        if ticket is None or ticket.status == "closed":
            return False
        s.expunge(ticket)
    finally:
        s.close()

    if not await archive_channel_history(bot, ticket):
        return False

    note = f"Ticket closed by {closed_by_name}"
    if reason:
        note += f": {reason}"
    add_system_message(ticket_id, note)

    s = Session()
    try:
        live = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        live.status = "closed"
        live.date_closed = datetime.now()
        if closed_by_user_id is not None:
            live.closed_by = closed_by_user_id
        s.commit()
    except Exception as e:  # noqa: BLE001
        s.rollback()
        print(f"[tickets] close_and_archive DB update failed ({ticket_id}): {e}")
        return False
    finally:
        s.close()

    try:
        channel = await bot.fetch_channel(int(ticket.channel_id))
        if channel is not None:
            await channel.delete()
    except Exception as e:  # noqa: BLE001
        # Channel may already be gone; the archive + DB state are what matter.
        print(f"[tickets] channel delete failed ({ticket_id}): {e}")
    return True


async def backfill_open_tickets(bot) -> None:
    """Startup pass: sync history for every open ticket (idempotent)."""
    s = Session()
    try:
        open_tickets = (
            s.query(Ticket).filter(Ticket.status.in_(["open", "close_requested"])).all()
        )
        for t in open_tickets:
            s.expunge(t)
    finally:
        s.close()
    synced = 0
    for ticket in open_tickets:
        try:
            if await archive_channel_history(bot, ticket):
                synced += 1
        except Exception as e:  # noqa: BLE001
            print(f"[tickets] backfill failed for ticket {ticket.ticket_id}: {e}")
        await asyncio.sleep(1)  # be gentle with the Discord API on startup
    print(f"[tickets] startup backfill synced {synced}/{len(open_tickets)} open tickets")
