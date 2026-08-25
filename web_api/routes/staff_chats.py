"""Staff-initiated user chats (web102a).

  POST /api/v1/staff/chats           -> {user_id, body} open (or reopen) the
                                        target's staff_dm thread + DM them
  GET  /api/v1/staff/chats?kind=     -> threads of one kind + unread, paged
                                        (staff_dm | event_invite | group_notice)
  GET  /api/v1/staff/users/search    -> ?q= find a site user to message

All gated on ``is_support_staff`` (v1: developers + superadmins). Ongoing
conversation happens through the ordinary ``/chat/*`` routes — the
``resolve_membership`` staff branch seats staff on any thread — and every
staff post on a staff_dm is relayed as a Discord DM by the chat route via
``services/staff_dm.queue_staff_dm_relay`` (collapsed to at most one ping a
minute per thread).

``kind=event_invite`` is how staff reach clan-vs-clan negotiations they are
asked to mediate. Those threads never enter a staff member's own inbox (see
``speakable_group_ids``); a clan's leaders see their own without coming here.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request
from sqlalchemy import or_

from db.models import ChatParticipant, ChatThread, User
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import assert_support_staff, current_user_id, json_body, load_user

staff_chats_bp = Blueprint("v1_staff_chats", __name__)

_SEARCH_LIMIT = 12
_OPEN_LIMIT_PER_DAY = 30  # per staff member; a sanity cap, not a workflow

#: Kinds staff may browse. Mirrors services.chat.THREAD_KINDS; kept as its own
#: tuple so adding a kind is a deliberate decision about staff visibility
#: rather than an automatic consequence.
_BROWSABLE_KINDS = ("staff_dm", "event_invite", "group_notice")


def _staff_open_limited(user_id: int) -> bool:
    try:
        from web_api.common import _rc

        conn = _rc()
        if conn is None:
            return False
        key = f"web:ratelimit:staff_chat_open:{user_id}"
        count = conn.incr(key)
        if count == 1:
            conn.expire(key, 86400)
        return count > _OPEN_LIMIT_PER_DAY
    except Exception:
        return False


def _user_hit(u: User) -> dict:
    return {
        "user_id": int(u.user_id),
        "discord_id": str(u.discord_id or ""),
        "display_name": u.username,
        "avatar_url": None,
    }


@staff_chats_bp.get("/staff/users/search")
async def staff_user_search():
    user_id = current_user_id()
    q = (request.args.get("q") or "").strip()

    def _load():
        with db_session() as s:
            assert_support_staff(load_user(s, user_id))
            if len(q) < 2:
                return {"items": []}
            like = f"%{q}%"
            rows = (
                s.query(User)
                .filter(or_(User.username.ilike(like), User.discord_id == q))
                .order_by(User.username.asc())
                .limit(_SEARCH_LIMIT)
                .all()
            )
            return {"items": [_user_hit(u) for u in rows]}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@staff_chats_bp.post("/staff/chats")
async def open_staff_chat():
    user_id = current_user_id()
    body = await json_body()
    raw_target = body.get("user_id")
    # Presence, not truthiness/positivity: user_id 0 is a real account and ids
    # run negative. Whether the target EXISTS is settled by the lookup below.
    if raw_target is None:
        abort_problem(400, "Bad request", "user_id is required.")
    try:
        target_user_id = int(raw_target)
    except (TypeError, ValueError):
        abort_problem(400, "Bad request", "user_id must be an integer.")
    text = str(body.get("body") or "").strip()
    if not text:
        abort_problem(422, "Invalid body", "Write an opening message.")

    def _create():
        from services.chat import (
            ChatError,
            Party,
            normalize_body,
            resolve_membership,
            thread_payload,
        )
        from services.staff_dm import get_or_create_staff_thread, queue_staff_dm_relay
        from web_api.routes.chat import _party_names

        with db_session() as s:
            staff = load_user(s, user_id)
            assert_support_staff(staff)
            target = s.query(User).filter(User.user_id == target_user_id).first()
            if target is None:
                abort_problem(404, "Not found", "No such user.")
            if target_user_id == user_id:
                abort_problem(400, "Bad request", "That's you.")
            if _staff_open_limited(user_id):
                abort_problem(429, "Too many chats", "Daily staff-chat budget reached.")
            try:
                normalize_body(text)
            except ChatError as e:
                abort_problem(422, "Invalid body", str(e))

            thread, _reopened = get_or_create_staff_thread(
                s, target_user_id=target_user_id, staff_user_id=user_id
            )
            s.flush()
            from services.chat import post_message

            try:
                message = post_message(
                    s,
                    thread=thread,
                    author_user_id=user_id,
                    party=Party("user", user_id),
                    body=text,
                    commit=False,
                    publish=False,
                )
            except ChatError as e:
                abort_problem(422, "Invalid body", str(e))
            s.flush()
            queue_staff_dm_relay(
                s,
                thread=thread,
                message=message,
                staff_name=getattr(staff, "username", None) or "DropTracker staff",
                commit=False,
            )
            s.commit()
            from services.chat import publish_message

            publish_message(s, thread, message)
            membership = resolve_membership(s, thread, user_id)
            participants = (
                s.query(ChatParticipant)
                .filter(ChatParticipant.thread_id == thread.id)
                .order_by(ChatParticipant.id.asc())
                .all()
            )
            return thread_payload(
                thread,
                participants=participants,
                unread=0,
                membership=membership,
                party_names=_party_names(s, participants),
            )

    payload = await asyncio.to_thread(_create)
    return private_no_store(jsonify(payload)), 201


@staff_chats_bp.get("/staff/chats")
async def list_staff_chats():
    """Staff browse over threads of one kind.

    Deliberately a separate surface from ``/chat/threads``: staff hold a seat
    on every thread by ``resolve_membership``, but folding every clan's
    negotiation into their own inbox would bury their actual conversations.
    They come here when they go looking.
    """
    user_id = current_user_id()
    page, limit = parse_page(request, default_limit=25, max_limit=100)
    kind = (request.args.get("kind") or "staff_dm").strip().lower()
    if kind not in _BROWSABLE_KINDS:
        abort_problem(
            400, "Bad request", f"kind must be one of {'|'.join(_BROWSABLE_KINDS)}."
        )

    def _load():
        from services.chat import resolve_membership, thread_payload, unread_counts
        from web_api.routes.chat import _party_names

        with db_session() as s:
            assert_support_staff(load_user(s, user_id))
            query = (
                s.query(ChatThread)
                .filter(ChatThread.kind == kind, ChatThread.status != "archived")
                .order_by(ChatThread.last_message_at.desc())
            )
            total = query.count()
            rows = query.offset((page - 1) * limit).limit(limit).all()
            ids = [t.id for t in rows]
            unread = unread_counts(s, ids, user_id) if ids else {}
            participants = (
                s.query(ChatParticipant)
                .filter(ChatParticipant.thread_id.in_(ids))
                .order_by(ChatParticipant.id.asc())
                .all()
                if ids
                else []
            )
            by_thread: dict[int, list] = {}
            for p in participants:
                by_thread.setdefault(int(p.thread_id), []).append(p)
            names = _party_names(s, participants)
            return {
                "items": [
                    thread_payload(
                        t,
                        participants=by_thread.get(int(t.id), []),
                        unread=unread.get(int(t.id), 0),
                        # Resolve the seat per row. Without this every thread
                        # reported my_parties=[] and can_post=false — including
                        # to a superadmin who can in fact post the moment they
                        # open it — so anything gating a composer on the list
                        # payload would be lying. Staff-only surface, ≤100
                        # rows a page, and the per-user lookups inside are
                        # cached, so the cost is worth the honesty.
                        membership=resolve_membership(s, t, user_id),
                        party_names=names,
                    )
                    for t in rows
                ],
                "meta": {"page": page, "limit": limit, "total": int(total)},
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))
