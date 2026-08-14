"""Notification Components-V2 layout editor — the components analogue of
routes/embeds.py, over ``group_component_layouts``.

  GET    /api/v1/notification-layouts/meta                  (any signed-in user)
  GET    /api/v1/groups/{id}/notification-layouts           (session + group admin)
  PUT    /api/v1/groups/{id}/notification-layouts/{type}    (group admin + pilot)
  DELETE /api/v1/groups/{id}/notification-layouts/{type}    (session + group admin)

Deliberately shaped like routes/event_layouts.py — same ``/meta`` endpoint
serving token docs and limits so the editor cannot drift from the renderer,
same ``{accent_color, blocks}`` document, same audit entries. A group admin who
has authored an event layout already knows this screen.

The one structural difference is the ``active`` flag. An event layout applies
the moment it is saved; a notification layout replaces what every member of the
group receives, so it is authored and previewed first and only goes live when
its type is switched over. DELETE removes the row entirely and the type is back
on the embed path.

Writes are gated on ``component_layout.components_enabled_for_group`` — the
same allowlist the send path checks — rather than on an entitlement, so the
editor can never save something the bot will not honour. GET reports that gate
as ``enabled`` instead of refusing, so the frontend can tell "not in the pilot"
apart from "the backend is down".

``services.component_layout`` is lazy-imported inside handlers: the unit-test
conftest stubs the ``services`` package (event_discord.py pattern).
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import AuditLog
from db.models import GroupComponentLayout
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

notification_layouts_bp = Blueprint("v1_notification_layouts", __name__)

# Hard cap on the stored JSON document — well above anything the block limits
# allow, purely an anti-abuse backstop.
_MAX_LAYOUT_JSON = 20_000


def _layouts_module():
    """The component-layout service, imported lazily.

    By dotted path rather than ``from services import component_layout``: the
    unit-test conftest stubs the ``services`` package with a MagicMock, whose
    attributes resolve to further mocks, while ``sys.modules`` carries the real
    submodule — which is what ``import_module`` returns.
    """
    import importlib

    return importlib.import_module("services.component_layout")


def _validate_notification_type(cl, notification_type: str) -> None:
    if notification_type not in cl.NOTIFICATION_TYPES:
        abort_problem(
            422,
            "Unknown notification type",
            f"'{notification_type}' cannot be sent as components.",
        )


def _assert_pilot(cl, group_id: int) -> None:
    if not cl.components_enabled_for_group(group_id):
        abort_problem(
            403,
            "Not available yet",
            "Component layouts are being trialled on a small number of groups. "
            "Your notifications keep using their embed templates.",
            extra={"code": "components_pilot_only"},
        )


def _serialize_layout(document) -> dict | None:
    """A stored document as the editor wants it, or None when unusable."""
    if not isinstance(document, dict) or not isinstance(document.get("blocks"), list):
        return None
    return {
        "accent_color": document.get("accent_color") or None,
        "blocks": document["blocks"],
    }


def _serialize_row(row: GroupComponentLayout) -> dict | None:
    try:
        return _serialize_layout(json.loads(row.layout))
    except (ValueError, TypeError):
        return None


def _load_row(s, group_id: int, notification_type: str):
    return (
        s.query(GroupComponentLayout)
        .filter(
            GroupComponentLayout.group_id == group_id,
            GroupComponentLayout.notification_type == notification_type,
        )
        .first()
    )


def _validate_body(body: dict, cl) -> dict:
    """Validate a PUT body ({accent_color?, blocks, active?}) with the same
    validator the renderer runs, so nothing can be saved that would later be
    rejected at send time and silently fall back to the embed."""
    if not isinstance(body, dict):
        abort_problem(422, "Invalid layout", "Body must be an object.")
    accent = body.get("accent_color")
    if accent in (None, ""):
        accent = None
    document = {"accent_color": accent, "blocks": body.get("blocks")}
    ok, errors = cl.validate_layout(document)
    if not ok:
        abort_problem(422, "Invalid layout", " ".join(errors[:8]))
    if len(json.dumps(document)) > _MAX_LAYOUT_JSON:
        abort_problem(422, "Invalid layout", "Layout is too large.")
    return {
        "accent_color": accent,
        "blocks": document["blocks"],
        "active": bool(body.get("active")),
    }


# --------------------------------------------------------------------------- #
# Editor metadata
# --------------------------------------------------------------------------- #
@notification_layouts_bp.get("/notification-layouts/meta")
async def notification_layouts_meta():
    current_user_id()  # signed-in only; the docs are otherwise static
    cl = _layouts_module()

    types = []
    for key in cl.NOTIFICATION_TYPES:
        meta = cl.TYPE_META.get(key) or {}
        types.append({
            "key": key,
            "label": meta.get("label") or key,
            "group": meta.get("group") or "Notifications",
            "description": meta.get("description") or "",
            "tokens": cl.tokens_for(key),
        })
    payload = {
        "types": types,
        "limits": {
            "max_blocks": cl.MAX_BLOCKS,
            "max_text_len": cl.MAX_TEXT_LENGTH,
            "max_total_text": cl.MAX_TOTAL_TEXT,
            "max_media_items": cl.MAX_MEDIA_ITEMS,
            "max_buttons": cl.MAX_BUTTONS,
            "max_label_len": cl.MAX_LABEL_LENGTH,
        },
    }
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Group layouts
# --------------------------------------------------------------------------- #
@notification_layouts_bp.get("/groups/<int:group_id>/notification-layouts")
async def list_group_notification_layouts(group_id: int):
    user_id = current_user_id()
    cl = _layouts_module()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            out = []
            for notification_type in cl.NOTIFICATION_TYPES:
                row = _load_row(s, group_id, notification_type)
                out.append({
                    "notification_type": notification_type,
                    "custom": _serialize_row(row) if row is not None else None,
                    # A row whose JSON no longer parses reads as inactive here
                    # for the same reason the send path ignores it.
                    "active": bool(row is not None and row.active and _serialize_row(row)),
                    "default": _serialize_layout(cl.default_layout(notification_type)) or {
                        "accent_color": None, "blocks": []},
                    "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
                })
            return {"enabled": cl.components_enabled_for_group(group_id), "layouts": out}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@notification_layouts_bp.put("/groups/<int:group_id>/notification-layouts/<notification_type>")
async def put_group_notification_layout(group_id: int, notification_type: str):
    user_id = current_user_id()
    cl = _layouts_module()
    _validate_notification_type(cl, notification_type)
    body = await json_body()
    data = _validate_body(body, cl)

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            # After the admin check: whether a group is in the pilot is not
            # something a non-admin needs to learn by probing.
            _assert_pilot(cl, group_id)

            row = _load_row(s, group_id, notification_type)
            before = _serialize_row(row) if row is not None else None
            was_active = bool(row is not None and row.active)
            if row is None:
                row = GroupComponentLayout(
                    group_id=group_id, notification_type=notification_type)
                s.add(row)
            row.layout = json.dumps({
                "accent_color": data["accent_color"], "blocks": data["blocks"]})
            row.active = data["active"]
            s.flush()
            after = _serialize_row(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="notification_layouts.update",
                    target=f"group_component_layouts.{notification_type}",
                    before=json.dumps({"layout": before, "active": was_active}) if before else None,
                    after=json.dumps({"layout": after, "active": data["active"]}),
                )
            )
            s.commit()
            return {"layout": after, "active": data["active"]}

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({
        "ok": True,
        "notification_type": notification_type,
        **saved,
    }))


@notification_layouts_bp.delete("/groups/<int:group_id>/notification-layouts/<notification_type>")
async def delete_group_notification_layout(group_id: int, notification_type: str):
    user_id = current_user_id()
    cl = _layouts_module()
    _validate_notification_type(cl, notification_type)

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            row = _load_row(s, group_id, notification_type)
            if row is None:
                return False
            before = _serialize_row(row)
            was_active = bool(row.active)
            s.delete(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="notification_layouts.reset",
                    target=f"group_component_layouts.{notification_type}",
                    before=json.dumps({"layout": before, "active": was_active}) if before else None,
                    after=None,
                )
            )
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
