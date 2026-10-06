"""The owner's Android admin widget: device pairing + one read-only summary.

  GET    /api/v1/admin/widget-tokens          paired devices (superadmin session)
  POST   /api/v1/admin/widget-tokens          pair a device -> raw token, shown once
  DELETE /api/v1/admin/widget-tokens/<id>     revoke
  GET    /api/v1/admin/widget/summary         Authorization: Bearer dtw_...

The summary is the only thing a widget token can read. It is checked on every
call: the row must exist and be unrevoked, and its user must still be a
superadmin, so demoting the owner of a token kills it too. The website reaches
this through its BFF (`/api/widget/admin`), since the Web API is internal-only.

Payload: support (tickets, the token owner's inbox badge, file transfers),
business (MRR, subscriptions, growth today) and the dev tracker's open tasks.
The business block is cached in Redis for two minutes; it is global, and
counting today's drops is the only heavy part.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional

from quart import Blueprint, jsonify, request
from sqlalchemy import func

from db.models import AdminWidgetToken
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import current_user_id, is_superadmin, json_body, load_user

admin_widget_bp = Blueprint("v1_admin_widget", __name__)

TOKEN_PREFIX = "dtw_"
PAIR_SCHEME = "droptracker-widget://pair"
_MAX_ACTIVE_TOKENS = 10
_LAST_USED_RESOLUTION = timedelta(minutes=5)
_BUSINESS_CACHE_KEY = "admin_widget:business"
_BUSINESS_TTL = 120
_UNREAD_ITEMS = 5


# --------------------------------------------------------------------------- #
# Token helpers                                                               #
# --------------------------------------------------------------------------- #
def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bearer_token() -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    raw = header[7:].strip()
    return raw if raw.startswith(TOKEN_PREFIX) else None


def _serialize(row) -> dict:
    def ts(dt):
        return int(dt.timestamp()) if dt else None

    return {
        "id": int(row.id),
        "label": row.label,
        "token_hint": row.token_hint,
        "created_at": ts(row.created_at),
        "last_used_at": ts(row.last_used_at),
        "revoked_at": ts(row.revoked_at),
    }


def _audit(actor_user_id, action, target, after=None):
    from web_api.routes.admin import _audit as admin_audit

    admin_audit(actor_user_id, action, target, after=after)


def _require_superadmin_session(user_id: int) -> int:
    with db_session() as s:
        if not is_superadmin(load_user(s, user_id)):
            abort_problem(403, "Forbidden", "Site staff access is required.",
                          extra={"code": "staff_required"})
    return user_id


# --------------------------------------------------------------------------- #
# Pairing                                                                     #
# --------------------------------------------------------------------------- #
@admin_widget_bp.get("/admin/widget-tokens")
async def list_widget_tokens():
    user_id = current_user_id()

    def _load():
        _require_superadmin_session(user_id)
        with db_session() as s:
            rows = (s.query(AdminWidgetToken)
                    .filter(AdminWidgetToken.user_id == user_id)
                    .order_by(AdminWidgetToken.id.desc())
                    .all())
            return {"tokens": [_serialize(r) for r in rows]}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@admin_widget_bp.post("/admin/widget-tokens")
async def create_widget_token():
    body = await json_body(required=False)
    label = str(body.get("label") or "").strip()[:64] or "Phone"
    user_id = current_user_id()

    def _create():
        _require_superadmin_session(user_id)
        raw = new_token()
        with db_session() as s:
            active = (s.query(func.count(AdminWidgetToken.id))
                      .filter(AdminWidgetToken.user_id == user_id,
                              AdminWidgetToken.revoked_at.is_(None))
                      .scalar() or 0)
            if active >= _MAX_ACTIVE_TOKENS:
                abort_problem(409, "Too many devices",
                              f"Revoke a device first; at most {_MAX_ACTIVE_TOKENS} can be paired.",
                              extra={"code": "widget_token_limit"})
            row = AdminWidgetToken(user_id=user_id, label=label, token_hash=hash_token(raw),
                                   token_hint=raw[:len(TOKEN_PREFIX) + 6],
                                   created_at=datetime.now())
            s.add(row)
            s.commit()
            out = _serialize(row)
        _audit(user_id, "admin_widget.pair", f"widget_token:{out['id']}", after={"label": label})
        out["token"] = raw
        out["pair_url"] = f"{PAIR_SCHEME}?token={raw}"
        return out

    return private_no_store(jsonify(await asyncio.to_thread(_create))), 201


@admin_widget_bp.delete("/admin/widget-tokens/<int:token_id>")
async def revoke_widget_token(token_id: int):
    user_id = current_user_id()

    def _revoke():
        _require_superadmin_session(user_id)
        with db_session() as s:
            row = (s.query(AdminWidgetToken)
                   .filter(AdminWidgetToken.id == token_id,
                           AdminWidgetToken.user_id == user_id)
                   .first())
            if row is None:
                abort_problem(404, "Not found", "No such device.")
            if row.revoked_at is None:
                row.revoked_at = datetime.now()
                s.commit()
            out = _serialize(row)
        _audit(user_id, "admin_widget.revoke", f"widget_token:{token_id}")
        return out

    return private_no_store(jsonify(await asyncio.to_thread(_revoke)))


# --------------------------------------------------------------------------- #
# Summary                                                                     #
# --------------------------------------------------------------------------- #
def authenticate_widget(s, raw: Optional[str]) -> int:
    """The token's user id, or abort 401/403. Touches last_used_at at most
    every few minutes so a phone polling every 15 min writes rarely."""
    if not raw:
        abort_problem(401, "Not authenticated", "A widget token is required.",
                      extra={"code": "widget_token_required"})
    row = (s.query(AdminWidgetToken)
           .filter(AdminWidgetToken.token_hash == hash_token(raw))
           .first())
    if row is None or row.revoked_at is not None:
        abort_problem(401, "Not authenticated", "This widget token is invalid or revoked.",
                      extra={"code": "widget_token_invalid"})
    if not is_superadmin(load_user(s, int(row.user_id))):
        abort_problem(403, "Forbidden", "Site staff access is required.",
                      extra={"code": "staff_required"})
    now = datetime.now()
    if row.last_used_at is None or now - row.last_used_at >= _LAST_USED_RESOLUTION:
        row.last_used_at = now
        s.commit()
    return int(row.user_id)


def _item_title(item: dict) -> tuple[str, Optional[str]]:
    kind = item.get("kind")
    if kind == "ticket":
        t = item.get("ticket") or {}
        tid = t.get("ticket_id") or t.get("id")
        return (t.get("subject") or f"Ticket #{tid}"), (f"/tickets/{tid}" if tid else None)
    if kind == "chat":
        t = item.get("thread") or {}
        return (t.get("title") or "Chat"), (f"/messages/{t['id']}" if t.get("id") else None)
    if kind == "suggestion":
        t = item.get("suggestion") or {}
        sid = t.get("id") or t.get("suggestion_id")
        return (t.get("title") or "Suggestion"), (f"/suggestions/{sid}" if sid else None)
    return str(kind or "item"), None


def support_stats(s, user_id: int) -> dict:
    from db.models import FileTransfer, Ticket, TicketMessage, User
    from web_api.routes import inbox
    from web_api.routes.tickets import _OPENISH

    open_rows = (s.query(Ticket.ticket_id, Ticket.claimed_by)
                 .filter(Ticket.status.in_(_OPENISH)).all())
    open_ids = [int(r.ticket_id) for r in open_rows]
    unclaimed = sum(1 for r in open_rows if r.claimed_by is None)

    # Awaiting staff: the newest human message on an open ticket is not from staff.
    awaiting = 0
    if open_ids:
        latest = dict(
            s.query(TicketMessage.ticket_id, func.max(TicketMessage.id))
            .filter(TicketMessage.ticket_id.in_(open_ids),
                    TicketMessage.is_bot.is_(False),
                    TicketMessage.kind == "message")
            .group_by(TicketMessage.ticket_id).all()
        )
        authors = dict(
            s.query(TicketMessage.id, TicketMessage.author_user_id)
            .filter(TicketMessage.id.in_(list(latest.values()))).all()
        ) if latest else {}
        staff = {
            int(uid) for (uid,) in s.query(User.user_id).filter(
                (User.is_superadmin.is_(True)) | (User.is_developer.is_(True))
            ).all()
        }
        for tid in open_ids:
            mid = latest.get(tid)
            # No human message yet = a fresh ticket nobody has answered.
            if mid is None or authors.get(mid) not in staff:
                awaiting += 1

    items = inbox._chat_items(s, user_id)
    items += inbox._ticket_items(s, user_id)[0]
    items += inbox._suggestion_items(s, user_id)
    unread_items = [i for i in items if inbox._item_unread(i) > 0]
    unread_items.sort(key=inbox._item_activity, reverse=True)
    top = []
    for i in unread_items[:_UNREAD_ITEMS]:
        title, path = _item_title(i)
        top.append({"kind": i.get("kind"), "title": title, "preview": i.get("preview"),
                    "unread": inbox._item_unread(i), "path": path})

    since = datetime.now() - timedelta(hours=24)
    transfers = (s.query(func.count(FileTransfer.id))
                 .filter(FileTransfer.created_at >= since).scalar() or 0)
    return {
        "tickets_open": len(open_ids),
        "tickets_unclaimed": unclaimed,
        "tickets_awaiting_staff": awaiting,
        "inbox_unread": sum(inbox._item_unread(i) for i in unread_items),
        "unread_items": top,
        "file_transfers_24h": int(transfers),
    }


def business_stats(s) -> dict:
    from db.models import Drop, Group, Player, User
    from web_api.routes.admin import active_subscriptions_query, headline_mrr_cents

    now = datetime.now()
    day_start = datetime(now.year, now.month, now.day)
    try:
        mrr = int(headline_mrr_cents(s))
    except Exception:
        mrr = None
    return {
        "mrr_cents": mrr,
        "mrr_formatted": f"${mrr / 100:,.2f}" if mrr is not None else None,
        "active_subscriptions": int(active_subscriptions_query(s).count()),
        "users_total": int(s.query(func.count(User.user_id)).scalar() or 0),
        "users_today": int(s.query(func.count(User.user_id))
                           .filter(User.date_added >= day_start).scalar() or 0),
        "players_today": int(s.query(func.count(Player.player_id))
                             .filter(Player.date_added >= day_start).scalar() or 0),
        "groups_total": int(s.query(func.count(Group.group_id))
                            .filter(Group.group_id > 2).scalar() or 0),
        "groups_today": int(s.query(func.count(Group.group_id))
                            .filter(Group.group_id > 2, Group.date_added >= day_start)
                            .scalar() or 0),
        # date_added is indexed; this is the same count /admin/overview shows.
        "drops_today": int(s.query(func.count(Drop.drop_id))
                           .filter(Drop.date_added >= day_start).scalar() or 0),
    }


def dev_stats(s) -> dict:
    from db.models import DevTask

    rows = dict(
        s.query(DevTask.status, func.count(DevTask.id))
        .filter(DevTask.status != "done")
        .group_by(DevTask.status).all()
    )
    return {
        "open_tasks": int(sum(rows.values())),
        "in_progress": int(rows.get("in_progress", 0)),
        "blocked": int(rows.get("blocked", 0)),
    }


def _cached_business(s) -> dict:
    from web_api.common import _rc

    r = _rc()
    if r is not None:
        try:
            hit = r.get(_BUSINESS_CACHE_KEY)
            if hit:
                return json.loads(hit)
        except Exception:
            pass
    out = business_stats(s)
    if r is not None:
        try:
            r.setex(_BUSINESS_CACHE_KEY, _BUSINESS_TTL, json.dumps(out))
        except Exception:
            pass
    return out


@admin_widget_bp.get("/admin/widget/summary")
async def widget_summary():
    raw = bearer_token()

    def _load():
        with db_session() as s:
            user_id = authenticate_widget(s, raw)
            out: dict = {"generated_at": int(datetime.now().timestamp()), "user_id": user_id}
            # Each block fails on its own; a broken query blanks one panel, not the widget.
            for key, fn in (("support", lambda: support_stats(s, user_id)),
                            ("business", lambda: _cached_business(s)),
                            ("dev", lambda: dev_stats(s))):
                try:
                    out[key] = fn()
                except Exception as e:  # noqa: BLE001
                    s.rollback()
                    print(f"[admin_widget] {key} failed: {e!r}")
                    out[key] = None
            return out

    return private_no_store(jsonify(await asyncio.to_thread(_load)))
