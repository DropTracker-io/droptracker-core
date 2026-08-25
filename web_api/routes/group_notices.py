"""Superadmin console for bot→group notices (web102a).

  GET   /api/v1/admin/group-notices        -> ?status=open|resolved&code=&group_id=&page=
  PATCH /api/v1/admin/group-notices/{id}   -> {action:'resolve', note?}

Group admins never use these routes — their side of a notice is an ordinary
``group_notice`` chat thread reached through /chat/* and the widget inbox.
This is the staff overview: every notice across every group, the clan's
latest human response, and a manual-resolve override for problems the
emitters cannot detect as fixed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from quart import Blueprint, jsonify, request

from db.models import AuditLog, ChatMessage, Group, GroupNotice
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user

group_notices_bp = Blueprint("v1_group_notices", __name__)

_STATUSES = ("open", "resolved")


def _ts(dt) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _notice_payload(n: GroupNotice, *, group_name=None, unread=0,
                    latest_reply=None, last_message_at=None) -> dict:
    try:
        data = json.loads(n.data_json) if n.data_json else None
    except Exception:
        data = None
    return {
        "id": int(n.id),
        "group_id": int(n.group_id),
        "group_name": group_name,
        "code": n.code,
        "severity": n.severity,
        "title": n.title,
        "notice_status": n.status,
        "thread_id": int(n.thread_id) if n.thread_id else None,
        "first_raised_at": _ts(n.first_raised_at),
        "last_raised_at": _ts(n.last_raised_at),
        "raise_count": int(n.raise_count or 0),
        "resolved_at": _ts(n.resolved_at),
        "data": data,
        "unread": int(unread),
        "latest_reply": latest_reply,
        "last_message_at": _ts(last_message_at),
    }


@group_notices_bp.get("/admin/group-notices")
async def list_group_notices():
    user_id = current_user_id()
    page, limit = parse_page(request, default_limit=25, max_limit=100)
    status = (request.args.get("status") or "open").strip().lower()
    code = (request.args.get("code") or "").strip()
    group_id = request.args.get("group_id")

    def _load():
        from sqlalchemy import func

        from db.models import ChatThread
        from services.chat import unread_counts

        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            query = s.query(GroupNotice)
            if status in _STATUSES:
                query = query.filter(GroupNotice.status == status)
            if code:
                query = query.filter(GroupNotice.code == code)
            if group_id:
                try:
                    query = query.filter(GroupNotice.group_id == int(group_id))
                except ValueError:
                    pass
            total = query.count()
            rows = (
                query.order_by(GroupNotice.last_raised_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
                .all()
            )
            group_ids = {int(n.group_id) for n in rows}
            names = dict(
                s.query(Group.group_id, Group.group_name)
                .filter(Group.group_id.in_(group_ids))
                .all()
            ) if group_ids else {}
            thread_ids = [int(n.thread_id) for n in rows if n.thread_id]
            unread = unread_counts(s, thread_ids, user_id) if thread_ids else {}
            threads = {
                int(t.id): t
                for t in s.query(ChatThread).filter(ChatThread.id.in_(thread_ids)).all()
            } if thread_ids else {}
            # The clan's latest human line — "did they answer us?"
            latest = {}
            if thread_ids:
                latest_ids = dict(
                    s.query(ChatMessage.thread_id, func.max(ChatMessage.id))
                    .filter(
                        ChatMessage.thread_id.in_(thread_ids),
                        ChatMessage.kind == "message",
                        ChatMessage.deleted_at.is_(None),
                    )
                    .group_by(ChatMessage.thread_id)
                    .all()
                )
                if latest_ids:
                    for m in (
                        s.query(ChatMessage)
                        .filter(ChatMessage.id.in_(list(latest_ids.values())))
                        .all()
                    ):
                        body = " ".join((m.body or "").split())
                        latest[int(m.thread_id)] = (
                            body[:139] + "…" if len(body) > 140 else body or None
                        )
            items = []
            for n in rows:
                tid = int(n.thread_id) if n.thread_id else None
                thread = threads.get(tid) if tid else None
                items.append(
                    _notice_payload(
                        n,
                        group_name=names.get(int(n.group_id)),
                        unread=unread.get(tid, 0) if tid else 0,
                        latest_reply=latest.get(tid) if tid else None,
                        last_message_at=getattr(thread, "last_message_at", None),
                    )
                )
            open_total = (
                s.query(GroupNotice).filter(GroupNotice.status == "open").count()
            )
            return {
                "items": items,
                "meta": {"page": page, "limit": limit, "total": int(total)},
                "stats": {"open": int(open_total)},
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@group_notices_bp.patch("/admin/group-notices/<int:notice_id>")
async def group_notice_action(notice_id: int):
    user_id = current_user_id()
    body = await json_body()
    action = str(body.get("action") or "").strip().lower()
    note = str(body.get("note") or "").strip() or None
    if action != "resolve":
        abort_problem(400, "Bad request", "action must be 'resolve'.")

    def _apply():
        from services.group_notices import resolve_group_notice

        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            notice = (
                s.query(GroupNotice).filter(GroupNotice.id == notice_id).first()
            )
            if notice is None:
                abort_problem(404, "Not found", "No such notice.")
            if notice.status != "open":
                abort_problem(409, "Conflict", "This notice is already resolved.")
            resolve_group_notice(
                s,
                group_id=int(notice.group_id),
                code=notice.code,
                resolved_by_user_id=user_id,
                note=note,
            )
            s.refresh(notice)
            try:
                s.add(
                    AuditLog(
                        actor_user_id=user_id,
                        group_id=int(notice.group_id),
                        action="group_notice.resolve",
                        target=f"group_notices.{notice_id}",
                        before="open",
                        after="resolved",
                    )
                )
                s.commit()
            except Exception:
                s.rollback()
            return _notice_payload(notice)

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))
