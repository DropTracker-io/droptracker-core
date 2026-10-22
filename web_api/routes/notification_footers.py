"""Notification footers (web140a): replace or remove the DropTracker footer.

  GET    /api/v1/groups/{id}/notification-footers   (group admin)
  PUT    /api/v1/groups/{id}/notification-footers   (group admin + custom_footer)
  DELETE /api/v1/groups/{id}/notification-footers   (group admin; no entitlement)

One document per group: ``all`` is the choice for every notification type
(null = the DropTracker footer), ``types`` maps a type to its own choice (a
type left out follows ``all``). A choice is ``{"mode": "default"|"none"|
"custom", "text": ...}``. PUT replaces the whole document. Rows live in
``group_notification_footers`` and are read by
``services.notification_text.footer_template`` only while the group holds
``custom_footer``, so a lapsed group's settings are kept but not sent. DELETE
needs no entitlement, so a lapsed group can still clear them.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import AuditLog
from db.models import GroupNotificationFooter
from services import notification_text
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    assert_group_entitlement,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

notification_footers_bp = Blueprint("v1_notification_footers", __name__)

_ENTITLEMENT = notification_text.FOOTER_ENTITLEMENT


def _choice(row) -> dict:
    return {"mode": row.mode, "text": row.text if row.mode == "custom" else None}


def _document(rows) -> dict:
    all_row = next((r for r in rows if not r.notification_type), None)
    return {
        "all": _choice(all_row) if all_row else None,
        "types": {
            r.notification_type: _choice(r)
            for r in sorted(rows, key=lambda r: r.notification_type)
            if r.notification_type
        },
    }


def _load_rows(s, group_id: int):
    return (
        s.query(GroupNotificationFooter)
        .filter(GroupNotificationFooter.group_id == group_id)
        .all()
    )


def _entitled(s, group_id: int, user) -> bool:
    from web_api.entitlements import resolve_group_entitlements

    try:
        return bool(resolve_group_entitlements(s, group_id, user=user).get(_ENTITLEMENT))
    except Exception:
        return False


def _validate_choice(value, where: str):
    if value is None:
        return None
    if not isinstance(value, dict):
        abort_problem(422, "Invalid footer", f"{where} must be an object.")
    try:
        return notification_text.validate_footer(value.get("mode"), value.get("text"))
    except ValueError as exc:
        abort_problem(422, "Invalid footer", f"{where}: {exc}")


def _validate_body(body: dict) -> dict:
    """``{"": (mode, text)?, type: (mode, text), ...}`` from a PUT body."""
    if not isinstance(body, dict):
        abort_problem(422, "Invalid footer", "Body must be an object.")
    out = {}
    group_choice = _validate_choice(body.get("all"), "The footer for every message")
    if group_choice is not None:
        out[""] = group_choice
    types = body.get("types") or {}
    if not isinstance(types, dict):
        abort_problem(422, "Invalid footer", "'types' must be an object.")
    for ntype, value in types.items():
        if ntype not in notification_text.FOOTER_TYPES:
            abort_problem(
                422, "Invalid footer",
                f"'{ntype}' is not a notification type. Valid types: "
                f"{', '.join(notification_text.FOOTER_TYPES)}.",
            )
        choice = _validate_choice(value, f"The {ntype} footer")
        if choice is not None:
            out[ntype] = choice
    return out


def _payload(s, group_id: int, user, rows) -> dict:
    return {
        "entitled": _entitled(s, group_id, user),
        "default_footer": notification_text.global_footer(),
        "footer_types": list(notification_text.FOOTER_TYPES),
        "fallback_types": dict(notification_text.FOOTER_FALLBACK_TYPES),
        **_document(rows),
    }


@notification_footers_bp.get("/groups/<int:group_id>/notification-footers")
async def get_notification_footers(group_id: int):
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            return _payload(s, group_id, user, _load_rows(s, group_id))

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


@notification_footers_bp.put("/groups/<int:group_id>/notification-footers")
async def put_notification_footers(group_id: int):
    user_id = current_user_id()
    if group_id == notification_text.TEMPLATE_GROUP_ID:
        abort_problem(422, "Not a group", "The template group always sends the DropTracker footer.")
    wanted = _validate_body(await json_body())

    def _run():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_entitlement(
                s, user_id, group_id, _ENTITLEMENT,
                manage_guild_ids=manageable_guild_ids(user_id), user=user,
            )
            rows = _load_rows(s, group_id)
            before = _document(rows)
            by_type = {r.notification_type or "": r for r in rows}
            for ntype, row in by_type.items():
                if ntype not in wanted:
                    s.delete(row)
            for ntype, (mode, text) in wanted.items():
                row = by_type.get(ntype)
                if row is None:
                    row = GroupNotificationFooter(group_id=group_id, notification_type=ntype)
                    s.add(row)
                row.mode = mode
                row.text = text
                row.updated_by = user_id
            s.flush()
            rows = _load_rows(s, group_id)
            after = _document(rows)
            if after != before:
                s.add(AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="notification_footers.update",
                    target="group_notification_footers",
                    before=json.dumps(before),
                    after=json.dumps(after),
                ))
            s.commit()
            return _payload(s, group_id, user, rows)

    payload = await asyncio.to_thread(_run)
    notification_text.invalidate(group_id)
    return private_no_store(jsonify({"ok": True, **payload}))


@notification_footers_bp.delete("/groups/<int:group_id>/notification-footers")
async def delete_notification_footers(group_id: int):
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            rows = _load_rows(s, group_id)
            if rows:
                before = _document(rows)
                for row in rows:
                    s.delete(row)
                s.add(AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="notification_footers.reset",
                    target="group_notification_footers",
                    before=json.dumps(before),
                    after=None,
                ))
                s.commit()
            return _payload(s, group_id, user, [])

    payload = await asyncio.to_thread(_run)
    notification_text.invalidate(group_id)
    return private_no_store(jsonify({"ok": True, **payload}))
