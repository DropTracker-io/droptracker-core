"""Threaded messaging API (web96a).

  GET    /api/v1/chat/threads                  the caller's threads + unread counts
  GET    /api/v1/chat/threads/{id}             metadata, participants, my parties
  GET    /api/v1/chat/threads/{id}/messages    ?before=&limit=  (oldest-first page)
  POST   /api/v1/chat/threads/{id}/messages    {body, attachments[], as_party?}
  POST   /api/v1/chat/threads/{id}/read        {message_id}
  DELETE /api/v1/chat/messages/{id}            moderator tombstone (superadmin)

This module is transport only: every access decision comes from
``services/chat.py`` so the HTTP surface and the clan-vs-clan invite flow can
never drift apart on who is allowed to say what.

Thread ids are opaque handles, not capabilities — a non-participant gets 404,
not 403, so probing ids cannot enumerate which clans are negotiating.

Attachments are uploaded first through ``POST /api/v1/uploads/proof`` (which
already validates the bytes with Pillow, caps at 10 MB and streams to B2); the
client posts only the returned key, and the URL is re-derived server-side.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from quart import Blueprint, jsonify, request

from db import AuditLog, ChatMessage, ChatThread, Group, User
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
)

chat_bp = Blueprint("v1_chat", __name__)

# Posting budget per user: a negotiation is a conversation, not a firehose.
# Generous enough that nobody typing in good faith will ever see it.
_RATE_LIMIT_MESSAGES = 20
_RATE_LIMIT_WINDOW_SECONDS = 60

# Threads listed per request. The CvC surface produces one thread per invited
# clan, so even a busy multi-clan host stays well inside this.
_THREAD_LIST_LIMIT = 100


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_thread_for(s, thread_id: int, user_id: int):
    """Return ``(thread, membership)`` or 404. Non-members get the same 404 a
    missing thread gets — see the module docstring."""
    from services.chat import resolve_membership

    thread = s.query(ChatThread).filter(ChatThread.id == thread_id).first()
    if thread is None:
        abort_problem(404, "Conversation not found", "No such conversation.")
    membership = resolve_membership(s, thread, user_id)
    if membership is None:
        abort_problem(404, "Conversation not found", "No such conversation.")
    return thread, membership


def _party_names(s, participants) -> dict:
    """Display names for the parties on a thread, batched by type."""
    group_ids = {int(p.party_id) for p in participants if p.party_type == "group"}
    user_ids = {int(p.party_id) for p in participants if p.party_type == "user"}
    names: dict = {}
    if group_ids:
        for gid, gname in (
            s.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(group_ids))
            .all()
        ):
            names[("group", int(gid))] = gname
    if user_ids:
        for uid, uname in (
            s.query(User.user_id, User.username).filter(User.user_id.in_(user_ids)).all()
        ):
            names[("user", int(uid))] = uname
    return names


def _author_names(s, messages) -> dict:
    ids = {m.author_user_id for m in messages if m.author_user_id is not None}
    if not ids:
        return {}
    return {
        int(uid): uname
        for uid, uname in s.query(User.user_id, User.username)
        .filter(User.user_id.in_(ids))
        .all()
    }


def _post_count_this_window(user_id: int) -> Optional[int]:
    """Posts this user has made in the current window, or None if Redis is
    unavailable. Kept separate from the decision below so a Redis failure can
    fail OPEN without the bare `except` also swallowing the 429."""
    try:
        from web_api.common import _rc

        conn = _rc()
        if conn is None:
            return None
        window = int(time.time()) // _RATE_LIMIT_WINDOW_SECONDS
        key = f"chat:rate:{user_id}:{window}"
        count = int(conn.incr(key))
        if count == 1:
            conn.expire(key, _RATE_LIMIT_WINDOW_SECONDS * 2)
        return count
    except Exception:
        return None


def _check_rate_limit(user_id: int) -> None:
    """Fixed-window post budget. Fails OPEN: a Redis outage must not silence an
    in-flight negotiation."""
    count = _post_count_this_window(user_id)
    if count is not None and count > _RATE_LIMIT_MESSAGES:
        abort_problem(
            429,
            "Slow down",
            "You're sending messages too quickly. Try again in a moment.",
        )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@chat_bp.get("/chat/threads")
async def list_threads():
    """Threads the caller can speak in, newest activity first.

    Scoped to the parties they actually hold — a superadmin sees their own
    clans here, not every negotiation on the site (they can still open any
    individual thread by id to moderate).
    """
    user_id = current_user_id()

    def _load():
        from db.models import ChatParticipant
        from services.chat import (
            resolve_membership,
            speakable_group_ids,
            thread_payload,
            unread_counts,
        )

        with db_session() as s:
            group_ids = speakable_group_ids(s, user_id)
            party_filter = [("user", user_id)]
            party_filter += [("group", gid) for gid in group_ids]

            from sqlalchemy import and_, or_

            rows = (
                s.query(ChatParticipant.thread_id)
                .filter(
                    or_(
                        *[
                            and_(
                                ChatParticipant.party_type == ptype,
                                ChatParticipant.party_id == pid,
                            )
                            for ptype, pid in party_filter
                        ]
                    )
                )
                .all()
            )
            thread_ids = sorted({int(t) for (t,) in rows})
            if not thread_ids:
                return []

            threads = (
                s.query(ChatThread)
                .filter(
                    ChatThread.id.in_(thread_ids),
                    ChatThread.status != "archived",
                )
                # MySQL sorts NULLs last under DESC, which is what we want:
                # a thread with no messages yet belongs at the bottom.
                .order_by(ChatThread.last_message_at.desc())
                .limit(_THREAD_LIST_LIMIT)
                .all()
            )
            if not threads:
                return []
            ids = [t.id for t in threads]
            counts = unread_counts(s, ids, user_id)
            participants = (
                s.query(ChatParticipant)
                .filter(ChatParticipant.thread_id.in_(ids))
                .order_by(ChatParticipant.id.asc())
                .all()
            )
            by_thread: dict = {}
            for p in participants:
                by_thread.setdefault(int(p.thread_id), []).append(p)
            names = _party_names(s, participants)

            out = []
            for t in threads:
                out.append(
                    thread_payload(
                        t,
                        participants=by_thread.get(int(t.id), []),
                        unread=counts.get(int(t.id), 0),
                        membership=resolve_membership(s, t, user_id),
                        party_names=names,
                    )
                )
            return out

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@chat_bp.get("/chat/threads/<int:thread_id>")
async def get_thread(thread_id: int):
    user_id = current_user_id()

    def _load():
        from db.models import ChatParticipant
        from services.chat import last_read_id, thread_payload, unread_counts

        with db_session() as s:
            thread, membership = _load_thread_for(s, thread_id, user_id)
            participants = (
                s.query(ChatParticipant)
                .filter(ChatParticipant.thread_id == thread.id)
                .order_by(ChatParticipant.id.asc())
                .all()
            )
            payload = thread_payload(
                thread,
                participants=participants,
                unread=unread_counts(s, [thread.id], user_id).get(int(thread.id), 0),
                membership=membership,
                party_names=_party_names(s, participants),
            )
            payload["last_read_message_id"] = last_read_id(s, thread.id, user_id)
            return payload

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@chat_bp.get("/chat/threads/<int:thread_id>/messages")
async def list_messages(thread_id: int):
    """One page of a thread, oldest-first. ``before`` pages backwards."""
    user_id = current_user_id()
    raw_before = request.args.get("before")
    raw_limit = request.args.get("limit")
    try:
        before_id = int(raw_before) if raw_before else None
    except ValueError:
        before_id = None
    try:
        limit = int(raw_limit) if raw_limit else None
    except ValueError:
        limit = None

    def _load():
        from services.chat import DEFAULT_PAGE_SIZE, message_payload, messages_page

        with db_session() as s:
            thread, _membership = _load_thread_for(s, thread_id, user_id)
            rows = messages_page(
                s,
                thread.id,
                before_id=before_id,
                limit=limit or DEFAULT_PAGE_SIZE,
            )
            names = _author_names(s, rows)
            return {
                "messages": [
                    message_payload(m, author_name=names.get(m.author_user_id))
                    for m in rows
                ],
                # The client pages back until this goes false.
                "has_more": bool(rows) and len(rows) >= (limit or DEFAULT_PAGE_SIZE),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@chat_bp.post("/chat/threads/<int:thread_id>/messages")
async def post_message_route(thread_id: int):
    user_id = current_user_id()
    body = await json_body()
    text = body.get("body")
    attachments = body.get("attachments")
    as_party = body.get("as_party")

    _check_rate_limit(user_id)

    def _apply():
        from services.chat import (
            ChatError,
            Party,
            message_payload,
            post_message,
        )

        with db_session() as s:
            thread, membership = _load_thread_for(s, thread_id, user_id)
            if not membership.can_post:
                abort_problem(
                    409,
                    "Conversation closed",
                    "This conversation is no longer accepting messages.",
                )

            party = membership.primary
            if isinstance(as_party, dict):
                requested = Party(
                    str(as_party.get("party_type") or ""),
                    int(as_party.get("party_id") or 0),
                )
                if not membership.allows(requested):
                    abort_problem(
                        403,
                        "Not your party",
                        "You can't post on behalf of that clan.",
                    )
                party = requested
            if party is None:
                abort_problem(403, "Forbidden", "You have no party on this thread.")

            try:
                row = post_message(
                    s,
                    thread=thread,
                    author_user_id=user_id,
                    party=party,
                    body=text,
                    attachments=attachments,
                )
            except ChatError as e:
                abort_problem(422, "Message rejected", str(e))

            # Posting implies reading everything up to your own message.
            from services.chat import mark_read

            mark_read(s, thread.id, user_id, row.id)
            user = load_user(s, user_id)

            # Staff replies on a staff_dm thread ping the user on Discord
            # (collapsed to one DM/minute per thread — the link button
            # carries the rest of the conversation). The subject user's own
            # messages never relay: their Discord surface is the DM they
            # reply to, not a DM from us.
            if thread.kind == "staff_dm" and int(thread.subject_id) != int(user_id):
                try:
                    from services.staff_dm import queue_staff_dm_relay

                    queue_staff_dm_relay(
                        s,
                        thread=thread,
                        message=row,
                        staff_name=getattr(user, "username", None) or "DropTracker staff",
                        commit=True,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[chat] staff_dm relay enqueue failed (thread {thread.id}): {e}")
            return message_payload(
                row, author_name=getattr(user, "username", None)
            )

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@chat_bp.post("/chat/threads/<int:thread_id>/read")
async def mark_thread_read(thread_id: int):
    user_id = current_user_id()
    body = await json_body(required=False)
    raw = body.get("message_id")
    try:
        message_id = int(raw)
    except (TypeError, ValueError):
        abort_problem(422, "Invalid message_id", "'message_id' must be an integer.")

    def _apply():
        from services.chat import mark_read, unread_counts

        with db_session() as s:
            thread, _membership = _load_thread_for(s, thread_id, user_id)
            row = mark_read(s, thread.id, user_id, message_id)
            return {
                "last_read_message_id": int(row.last_read_message_id or 0),
                "unread": unread_counts(s, [thread.id], user_id).get(
                    int(thread.id), 0
                ),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@chat_bp.delete("/chat/messages/<int:message_id>")
async def delete_message(message_id: int):
    """Moderator takedown. v1 has no author edit/delete, so this is the ONLY
    way an uploaded image or an abusive line comes down — superadmin-gated and
    audit-logged, and it tombstones rather than purges."""
    user_id = current_user_id()

    def _apply():
        from services.chat import soft_delete_message

        with db_session() as s:
            user = load_user(s, user_id)
            if not is_superadmin(user):
                abort_problem(
                    403, "Forbidden", "Only site staff can remove a message."
                )
            row = (
                s.query(ChatMessage).filter(ChatMessage.id == message_id).first()
            )
            if row is None:
                abort_problem(404, "Not found", "No such message.")
            soft_delete_message(s, row, by_user_id=user_id, commit=False)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    action="chat.message.delete",
                    target=f"chat_messages.{message_id}",
                    before="visible",
                    after="deleted",
                )
            )
            s.commit()
            return {"ok": True}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))
