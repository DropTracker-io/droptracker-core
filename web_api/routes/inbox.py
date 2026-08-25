"""Unified inbox for the site chat widget (web102a).

  GET  /api/v1/me/inbox               -> chat threads + tickets + suggestions,
                                         each with unread, one sorted list
  POST /api/v1/tickets/{id}/read      -> {message_id} advance read pointer
  POST /api/v1/suggestions/{id}/read  -> {message_id} advance read pointer

Federation, not unification: tickets and suggestions stay in their own tables
with their own Discord mirrors; chat threads carry the kinds that were born on
the chat layer (event_invite, staff_dm, group_notice). This endpoint is the
one place the three are merged, so the widget renders a single badge-accurate
list without three round-trips.

Unread semantics (the "already replied in Discord" rule) live in
``services/inbox.py``.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from quart import Blueprint, jsonify

from db.models import ChatMessage, ChatThread, GroupNotice, Suggestion, Ticket, TicketMessage
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import current_user_id, json_body, load_user

inbox_bp = Blueprint("v1_inbox", __name__)

_ITEM_CAP = 50
_PREVIEW_CHARS = 140
_CLOSED_GRACE_DAYS = 30

_OPENISH_TICKET = ("pending", "open", "close_requested")


def _truncate(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= _PREVIEW_CHARS:
        return text or None
    return text[: _PREVIEW_CHARS - 1] + "…"


def _chat_items(s, user_id: int) -> list[dict]:
    """The caller's speakable threads, same membership rules as
    ``GET /chat/threads`` — group_notice threads reach group admins through
    their existing group party, staff_dm through the user party."""
    from sqlalchemy import and_, func, or_

    from db.models import ChatParticipant
    from services.chat import (
        resolve_membership,
        speakable_group_ids,
        thread_payload,
        unread_counts,
    )
    from web_api.routes.chat import _party_names

    group_ids = speakable_group_ids(s, user_id)
    party_filter = [("user", user_id)] + [("group", gid) for gid in group_ids]
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
        .filter(ChatThread.id.in_(thread_ids), ChatThread.status != "archived")
        .order_by(ChatThread.last_message_at.desc())
        .limit(_ITEM_CAP)
        .all()
    )
    if not threads:
        return []
    ids = [t.id for t in threads]
    unread = unread_counts(s, ids, user_id)

    # Latest visible human line per thread, for the list row.
    latest_ids = dict(
        s.query(ChatMessage.thread_id, func.max(ChatMessage.id))
        .filter(
            ChatMessage.thread_id.in_(ids),
            ChatMessage.deleted_at.is_(None),
            ChatMessage.kind == "message",
        )
        .group_by(ChatMessage.thread_id)
        .all()
    )
    previews = {}
    if latest_ids:
        for m in (
            s.query(ChatMessage)
            .filter(ChatMessage.id.in_(list(latest_ids.values())))
            .all()
        ):
            previews[int(m.thread_id)] = _truncate(m.body)

    notices = {
        int(n.thread_id): n
        for n in s.query(GroupNotice).filter(GroupNotice.thread_id.in_(ids)).all()
    }

    from db.models import ChatParticipant as CP

    participants = (
        s.query(CP).filter(CP.thread_id.in_(ids)).order_by(CP.id.asc()).all()
    )
    by_thread: dict[int, list] = {}
    for p in participants:
        by_thread.setdefault(int(p.thread_id), []).append(p)
    names = _party_names(s, participants)

    items = []
    for t in threads:
        membership = resolve_membership(s, t, user_id)
        payload = thread_payload(
            t,
            participants=by_thread.get(int(t.id), []),
            unread=unread.get(int(t.id), 0),
            membership=membership,
            party_names=names,
        )
        item = {
            "kind": "chat",
            "thread": payload,
            "preview": previews.get(int(t.id)),
        }
        notice = notices.get(int(t.id))
        if notice is not None:
            item["notice"] = {
                "code": notice.code,
                "severity": notice.severity,
                "status": notice.status,
            }
        items.append(item)
    return items


def _ticket_items(s, user_id: int) -> tuple[list[dict], Optional[int]]:
    from datetime import datetime, timedelta

    from sqlalchemy import and_, func, not_, or_

    from services.inbox import ticket_unread_counts
    from web_api.routes.tickets import _ticket_summary

    participant_ids = s.query(TicketMessage.ticket_id).filter(
        TicketMessage.author_user_id == user_id
    )
    cutoff = datetime.now() - timedelta(days=_CLOSED_GRACE_DAYS)
    rows = (
        s.query(Ticket)
        .filter(
            or_(
                Ticket.created_by == user_id,
                Ticket.claimed_by == user_id,
                Ticket.ticket_id.in_(participant_ids),
            ),
            or_(
                Ticket.status.in_(_OPENISH_TICKET),
                and_(Ticket.status == "closed", Ticket.date_closed >= cutoff),
            ),
        )
        .order_by(Ticket.date_updated.desc())
        .limit(_ITEM_CAP)
        .all()
    )
    open_ticket_id = next(
        (
            t.ticket_id
            for t in rows
            if t.created_by == user_id and t.status in _OPENISH_TICKET
        ),
        None,
    )
    if not rows:
        return [], open_ticket_id
    ids = [t.ticket_id for t in rows]
    unread = ticket_unread_counts(s, ids, user_id)

    latest_ids = dict(
        s.query(TicketMessage.ticket_id, func.max(TicketMessage.id))
        .filter(
            TicketMessage.ticket_id.in_(ids),
            not_(
                and_(
                    TicketMessage.is_bot.is_(True),
                    TicketMessage.kind == "message",
                )
            ),
        )
        .group_by(TicketMessage.ticket_id)
        .all()
    )
    previews = {}
    if latest_ids:
        for m in (
            s.query(TicketMessage)
            .filter(TicketMessage.id.in_(list(latest_ids.values())))
            .all()
        ):
            previews[int(m.ticket_id)] = _truncate(m.content)

    counts = dict(
        s.query(TicketMessage.ticket_id, func.count(TicketMessage.id))
        .filter(TicketMessage.ticket_id.in_(ids))
        .group_by(TicketMessage.ticket_id)
        .all()
    )

    items = []
    for t in rows:
        n = unread.get(int(t.ticket_id), 0)
        # A closed ticket earns a row only while it still has news.
        if t.status == "closed" and n == 0:
            continue
        items.append(
            {
                "kind": "ticket",
                "ticket": _ticket_summary(s, t, message_counts=counts),
                "unread": n,
                "preview": previews.get(int(t.ticket_id)),
            }
        )
    return items, open_ticket_id


def _suggestion_items(s, user_id: int) -> list[dict]:
    from sqlalchemy import or_

    from db.models import SuggestionMessage
    from services.inbox import suggestion_unread_counts
    from web_api.routes.suggestions import _summary

    authored_ids = s.query(SuggestionMessage.suggestion_id).filter(
        SuggestionMessage.author_user_id == user_id
    )
    rows = (
        s.query(Suggestion)
        .filter(
            or_(
                Suggestion.user_id == user_id,
                Suggestion.id.in_(authored_ids),
            )
        )
        .order_by(Suggestion.last_activity_at.desc())
        .limit(_ITEM_CAP)
        .all()
    )
    if not rows:
        return []
    unread = suggestion_unread_counts(s, [r.id for r in rows], user_id)
    items = []
    for r in rows:
        n = unread.get(int(r.id), 0)
        if not r.is_open and n == 0:
            continue
        items.append({"kind": "suggestion", "suggestion": _summary(s, r), "unread": n})
    return items


def _item_activity(item: dict) -> int:
    if item["kind"] == "chat":
        t = item["thread"]
        return int(t.get("last_message_at") or t.get("created_at") or 0)
    if item["kind"] == "ticket":
        t = item["ticket"]
        return int(t.get("date_updated") or t.get("date_added") or 0)
    sug = item["suggestion"]
    return int(sug.get("last_activity_at") or sug.get("created_at") or 0)


def _item_unread(item: dict) -> int:
    if item["kind"] == "chat":
        return int(item["thread"].get("unread") or 0)
    return int(item.get("unread") or 0)


@inbox_bp.get("/me/inbox")
async def my_inbox():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            items = _chat_items(s, user_id)
            ticket_items, open_ticket_id = _ticket_items(s, user_id)
            items += ticket_items
            items += _suggestion_items(s, user_id)
            items.sort(key=_item_activity, reverse=True)
            items = items[:_ITEM_CAP]
            return {
                "items": items,
                "total_unread": sum(_item_unread(i) for i in items),
                "open_ticket_id": open_ticket_id,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@inbox_bp.post("/tickets/<int:ticket_id>/read")
async def mark_ticket_read(ticket_id: int):
    user_id = current_user_id()
    body = await json_body()
    message_id = int(body.get("message_id") or 0)

    def _apply():
        from services.inbox import mark_surface_read, ticket_unread_counts
        from web_api.routes.tickets import _is_participant

        with db_session() as s:
            ticket = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if ticket is None:
                abort_problem(404, "Not found", "No such ticket.")
            user = load_user(s, user_id)
            if not (
                getattr(user, "is_superadmin", False)
                or _is_participant(s, user_id, ticket)
            ):
                abort_problem(403, "Forbidden", "You were not part of this ticket.")
            mark_surface_read(s, "ticket", ticket_id, user_id, message_id)
            unread = ticket_unread_counts(s, [ticket_id], user_id)
            return {"unread": unread.get(ticket_id, 0)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@inbox_bp.post("/suggestions/<int:suggestion_id>/read")
async def mark_suggestion_read(suggestion_id: int):
    # No participant gate: suggestions are public, and the pointer only ever
    # changes the caller's own badge.
    user_id = current_user_id()
    body = await json_body()
    message_id = int(body.get("message_id") or 0)

    def _apply():
        from services.inbox import mark_surface_read, suggestion_unread_counts

        with db_session() as s:
            sug = s.query(Suggestion).filter(Suggestion.id == suggestion_id).first()
            if sug is None:
                abort_problem(404, "Not found", "No such suggestion.")
            mark_surface_read(s, "suggestion", suggestion_id, user_id, message_id)
            unread = suggestion_unread_counts(s, [suggestion_id], user_id)
            return {"unread": unread.get(suggestion_id, 0)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))
