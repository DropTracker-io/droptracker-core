"""Support tickets on the web (web21a).

  GET   /api/v1/me/tickets           -> tickets the user created or wrote in
  GET   /api/v1/tickets/{id}         -> full transcript (participant/superadmin)
  GET   /api/v1/admin/tickets        -> all tickets, filterable + stats (superadmin)
  PATCH /api/v1/admin/tickets/{id}   -> {action: claim|unclaim|close} (superadmin)

Tickets are created and answered in Discord (services/ticket_system.py); the
web surface is a read-only archive plus staff triage. Closing from the web
sets status='close_requested'; the webhook bot's 15s maintenance task archives
the channel history, deletes the channel, and flips the row to 'closed'
(services/ticket_transcripts.py) — the Web API never talks to Discord (§10.2).

Transcript attachments are served from /img/tickets/... by the legacy image
server; rows only store the relative path.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional

from quart import Blueprint, jsonify, request
from sqlalchemy import func, or_

from db.models import AuditLog, Ticket, TicketMessage, User
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user

tickets_bp = Blueprint("v1_tickets", __name__)

_TICKET_TYPES = ("players", "clans", "support", "other")
# close_requested is presented as "closing" so the UI can show the transient
# state without treating it as a distinct lifecycle stage.
_PUBLIC_STATUS = {"open": "open", "close_requested": "closing", "closed": "closed"}


def _ts(dt: Optional[datetime]) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _username(s, user_id: Optional[int]) -> Optional[str]:
    if user_id is None:
        return None
    row = s.query(User.username).filter(User.user_id == user_id).first()
    return row[0] if row else None


def _ticket_summary(s, t: Ticket, *, message_counts: Optional[dict] = None) -> dict:
    if message_counts is not None:
        count = message_counts.get(t.ticket_id, 0)
    else:
        count = (
            s.query(func.count(TicketMessage.id))
            .filter(TicketMessage.ticket_id == t.ticket_id)
            .scalar()
            or 0
        )
    return {
        "ticket_id": t.ticket_id,
        "type": t.type if t.type in _TICKET_TYPES else "other",
        "status": _PUBLIC_STATUS.get(t.status, t.status),
        "subject": t.subject,
        "created_by": t.created_by,
        "created_by_name": _username(s, t.created_by),
        "claimed_by": t.claimed_by,
        "claimed_by_name": _username(s, t.claimed_by),
        "closed_by": t.closed_by,
        "closed_by_name": _username(s, t.closed_by),
        "message_count": int(count),
        "date_added": _ts(t.date_added),
        "date_updated": _ts(t.date_updated),
        "date_closed": _ts(t.date_closed),
    }


def _attachment_entries(raw: Optional[str]) -> list[dict]:
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except Exception:
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        path = e.get("path")
        out.append(
            {
                "filename": e.get("filename") or "file",
                "url": f"/img/{path}" if path else e.get("original_url"),
                "content_type": e.get("content_type"),
                "size": e.get("size"),
            }
        )
    return out


def _message_row(m: TicketMessage) -> dict:
    return {
        "id": m.id,
        "author_name": m.author_name,
        "author_user_id": m.author_user_id,
        "is_staff": bool(m.is_staff),
        "is_bot": bool(m.is_bot),
        "kind": m.kind or "message",
        "content": m.content or "",
        "attachments": _attachment_entries(m.attachments_json),
        "date_sent": _ts(m.date_sent),
        "date_edited": _ts(m.date_edited),
    }


def _is_participant(s, user_id: int, ticket: Ticket) -> bool:
    if ticket.created_by == user_id or ticket.claimed_by == user_id:
        return True
    return (
        s.query(TicketMessage.id)
        .filter(
            TicketMessage.ticket_id == ticket.ticket_id,
            TicketMessage.author_user_id == user_id,
        )
        .first()
        is not None
    )


def _audit(actor_user_id, action, target, before=None, after=None):
    try:
        with db_session() as s:
            s.add(
                AuditLog(
                    actor_user_id=actor_user_id,
                    group_id=None,
                    action=action,
                    target=target,
                    before=before,
                    after=after,
                )
            )
            s.commit()
    except Exception:
        pass


@tickets_bp.get("/me/tickets")
async def my_tickets():
    user_id = current_user_id()
    page, limit = parse_page(request, default_limit=25, max_limit=100)

    def _load():
        with db_session() as s:
            participant_ids = s.query(TicketMessage.ticket_id).filter(
                TicketMessage.author_user_id == user_id
            )
            query = (
                s.query(Ticket)
                .filter(
                    or_(
                        Ticket.created_by == user_id,
                        Ticket.claimed_by == user_id,
                        Ticket.ticket_id.in_(participant_ids),
                    )
                )
                .order_by(Ticket.date_added.desc())
            )
            total = query.count()
            rows = query.offset((page - 1) * limit).limit(limit).all()
            return {
                "items": [_ticket_summary(s, t) for t in rows],
                "meta": {"page": page, "limit": limit, "total": int(total)},
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@tickets_bp.get("/tickets/<int:ticket_id>")
async def ticket_detail(ticket_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            ticket = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if ticket is None:
                abort_problem(404, "Not found", "No such ticket.")
            user = load_user(s, user_id)
            if not (getattr(user, "is_superadmin", False) or _is_participant(s, user_id, ticket)):
                abort_problem(403, "Forbidden", "You were not part of this ticket.")
            messages = (
                s.query(TicketMessage)
                .filter(TicketMessage.ticket_id == ticket_id)
                .order_by(TicketMessage.date_sent.asc(), TicketMessage.id.asc())
                .all()
            )
            payload = _ticket_summary(s, ticket, message_counts={ticket_id: len(messages)})
            payload["messages"] = [_message_row(m) for m in messages]
            return payload

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@tickets_bp.get("/admin/tickets")
async def admin_tickets():
    user_id = current_user_id()
    page, limit = parse_page(request, default_limit=25, max_limit=100)
    status = (request.args.get("status") or "").strip().lower()
    ticket_type = (request.args.get("type") or "").strip().lower()
    q = (request.args.get("q") or "").strip()

    def _load():
        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            query = s.query(Ticket)
            if status in ("open", "closed"):
                # "open" includes the transient close_requested state.
                if status == "open":
                    query = query.filter(Ticket.status.in_(["open", "close_requested"]))
                else:
                    query = query.filter(Ticket.status == "closed")
            elif status == "unclaimed":
                query = query.filter(
                    Ticket.status.in_(["open", "close_requested"]),
                    Ticket.claimed_by.is_(None),
                )
            if ticket_type in _TICKET_TYPES:
                query = query.filter(Ticket.type == ticket_type)
            if q:
                like = f"%{q}%"
                creator_ids = s.query(User.user_id).filter(User.username.ilike(like))
                query = query.filter(
                    or_(
                        Ticket.subject.ilike(like),
                        Ticket.created_by.in_(creator_ids),
                    )
                )
            total = query.count()
            rows = (
                query.order_by(Ticket.date_added.desc())
                .offset((page - 1) * limit)
                .limit(limit)
                .all()
            )
            ids = [t.ticket_id for t in rows]
            counts = dict(
                s.query(TicketMessage.ticket_id, func.count(TicketMessage.id))
                .filter(TicketMessage.ticket_id.in_(ids))
                .group_by(TicketMessage.ticket_id)
                .all()
            ) if ids else {}

            by_status = dict(s.query(Ticket.status, func.count()).group_by(Ticket.status).all())
            open_like = int(by_status.get("open", 0)) + int(by_status.get("close_requested", 0))
            unclaimed = (
                s.query(func.count())
                .select_from(Ticket)
                .filter(Ticket.status.in_(["open", "close_requested"]), Ticket.claimed_by.is_(None))
                .scalar()
                or 0
            )
            by_type = dict(
                s.query(Ticket.type, func.count())
                .filter(Ticket.status.in_(["open", "close_requested"]))
                .group_by(Ticket.type)
                .all()
            )
            return {
                "items": [_ticket_summary(s, t, message_counts=counts) for t in rows],
                "meta": {"page": page, "limit": limit, "total": int(total)},
                "stats": {
                    "open": open_like,
                    "unclaimed": int(unclaimed),
                    "closed": int(by_status.get("closed", 0)),
                    "total": int(sum(by_status.values())),
                    "open_by_type": {k: int(v) for k, v in by_type.items()},
                },
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@tickets_bp.patch("/admin/tickets/<int:ticket_id>")
async def admin_ticket_action(ticket_id: int):
    user_id = current_user_id()
    body = await json_body()
    action = str(body.get("action") or "").strip().lower()
    if action not in ("claim", "unclaim", "close"):
        abort_problem(400, "Bad request", "action must be one of claim|unclaim|close.")

    def _apply():
        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            ticket = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if ticket is None:
                abort_problem(404, "Not found", "No such ticket.")
            before = ticket.status if action == "close" else str(ticket.claimed_by)
            if action == "claim":
                ticket.claimed_by = user_id
            elif action == "unclaim":
                ticket.claimed_by = None
            elif action == "close":
                if ticket.status == "closed":
                    abort_problem(409, "Conflict", "This ticket is already closed.")
                # The webhook bot's maintenance task (15s) archives the
                # channel and completes the close.
                ticket.status = "close_requested"
                ticket.closed_by = user_id
            ticket.date_updated = datetime.now()
            s.commit()
            return _ticket_summary(s, ticket)

    payload = await asyncio.to_thread(_apply)
    _audit(user_id, f"ticket.{action}", f"tickets.{ticket_id}")
    return private_no_store(jsonify(payload))
