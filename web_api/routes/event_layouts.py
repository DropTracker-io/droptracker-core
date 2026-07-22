"""Event message-layout editor (web66a) — the Components-V2 analogue of
routes/embeds.py, over ``web_event_message_layouts``.

  GET    /api/v1/event-layouts/meta                     (any signed-in user)
  GET    /api/v1/groups/{id}/event-layouts              (session + group admin)
  PUT    /api/v1/groups/{id}/event-layouts/{type}       (group admin + custom_embeds)
  DELETE /api/v1/groups/{id}/event-layouts/{type}       (session + group admin)
  GET    /api/v1/events/{id}/layouts                    (event admin)
  PUT    /api/v1/events/{id}/layouts/{type}             (event admin + custom_embeds)
  DELETE /api/v1/events/{id}/layouts/{type}             (event admin)

Group-level rows are ``event_id = 0``; per-event overrides carry the event's
id and live under the event's host group (template group 1 for global
events). Resolution at send time is event -> group -> group 1 -> code
default (services/event_message_layouts.load_layout). Group 1's rows are the
system defaults: editable by superadmins only, DELETE refuses them. DELETE
needs no entitlement anywhere (a downgraded group must be able to revert),
and the renderer only honors a non-template group's rows while
``custom_embeds`` is active, so saved rows never leak past a lapsed
subscription.

``services.event_message_layouts`` is lazy-imported inside handlers — the
unit-test conftest stubs the ``services`` package (event_discord.py pattern).
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import AuditLog, EVENT_MESSAGE_LAYOUT_TYPES
from db.models import EventMessageLayout
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    assert_group_entitlement,
    assert_superadmin,
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
)

event_layouts_bp = Blueprint("v1_event_layouts", __name__)

TEMPLATE_GROUP_ID = 1

# Hard cap on the stored JSON document — well above anything the block
# limits allow, purely an anti-abuse backstop.
_MAX_LAYOUT_JSON = 20_000


def _layouts_module():
    from services import event_message_layouts

    return event_message_layouts


def _validate_message_type(message_type: str) -> None:
    if message_type not in EVENT_MESSAGE_LAYOUT_TYPES:
        abort_problem(
            422,
            "Unknown message type",
            f"'{message_type}' is not a customizable event message type.",
        )


def _serialize_row(row: EventMessageLayout) -> dict | None:
    try:
        layout = json.loads(row.layout)
    except (ValueError, TypeError):
        return None
    if not isinstance(layout, dict) or not isinstance(layout.get("blocks"), list):
        return None
    return {
        "message_type": row.message_type,
        "accent_color": row.accent_color or None,
        "blocks": layout["blocks"],
    }


def _serialize_default(message_type: str, default: dict) -> dict:
    return {
        "message_type": message_type,
        "accent_color": default.get("accent_color") or None,
        "blocks": default.get("blocks") or [],
    }


def _load_row(s, group_id: int, message_type: str, event_id: int = 0):
    return (
        s.query(EventMessageLayout)
        .filter(
            EventMessageLayout.group_id == group_id,
            EventMessageLayout.message_type == message_type,
            EventMessageLayout.event_id == event_id,
        )
        .order_by(EventMessageLayout.id)
        .first()
    )


def _validate_body(body: dict, ml) -> dict:
    """Validate a PUT body ({accent_color?, blocks}) via the service-layer
    validator; returns {"accent_color": str|None, "blocks": [...]}."""
    if not isinstance(body, dict):
        abort_problem(422, "Invalid layout", "Body must be an object.")
    accent = body.get("accent_color")
    if accent in (None, ""):
        accent = None
    layout = {"accent_color": accent, "blocks": body.get("blocks")}
    errors = ml.validate_layout_spec(layout)
    if errors:
        abort_problem(422, "Invalid layout", " ".join(errors[:8]))
    if len(json.dumps(layout["blocks"])) > _MAX_LAYOUT_JSON:
        abort_problem(422, "Invalid layout", "Layout is too large.")
    return {"accent_color": accent, "blocks": layout["blocks"]}


def _upsert(s, user_id: int, group_id: int, message_type: str, event_id: int,
            data: dict, ml, audit_group_id=None) -> dict:
    row = _load_row(s, group_id, message_type, event_id)
    before = _serialize_row(row) if row is not None else None
    if row is None:
        row = EventMessageLayout(
            group_id=group_id, message_type=message_type, event_id=event_id,
            schema_version=ml.LAYOUT_SCHEMA_VERSION,
        )
        s.add(row)
    row.accent_color = data["accent_color"]
    row.layout = json.dumps({"blocks": data["blocks"]})
    row.schema_version = ml.LAYOUT_SCHEMA_VERSION
    s.flush()
    after = _serialize_row(row)
    s.add(
        AuditLog(
            actor_user_id=user_id,
            group_id=audit_group_id,
            event_id=event_id or None,
            action="event_layouts.update",
            target=f"web_event_message_layouts.{message_type}",
            before=json.dumps(before) if before else None,
            after=json.dumps(after),
        )
    )
    s.commit()
    return after


# --------------------------------------------------------------------------- #
# Editor metadata
# --------------------------------------------------------------------------- #
@event_layouts_bp.get("/event-layouts/meta")
async def event_layouts_meta():
    current_user_id()  # signed-in only; the docs are otherwise static
    ml = _layouts_module()

    types = []
    for key, meta in ml.TYPE_META.items():
        if key not in EVENT_MESSAGE_LAYOUT_TYPES:
            continue
        tokens = []
        for token in tuple(ml._COMMON_TOKENS) + tuple(meta.get("tokens") or ()):
            doc = ml.TOKEN_DOCS.get(token) or {}
            tokens.append({
                "token": token,
                "help": doc.get("help") or "",
                "sample": doc.get("sample", ""),
            })
        types.append({
            "key": key,
            "label": meta["label"],
            "group": meta["group"],
            "description": meta.get("description") or "",
            "supports_standings": bool(meta.get("standings")),
            "tokens": tokens,
        })
    payload = {
        "types": types,
        "limits": {
            "max_blocks": ml.MAX_BLOCKS,
            "max_text_len": ml.MAX_TEXT_LEN,
            "max_title_len": ml.MAX_TITLE_LEN,
            "max_url_len": ml.MAX_URL_LEN,
            "max_buttons": ml.MAX_BUTTONS,
            "max_label_len": ml.MAX_LABEL_LEN,
        },
        "sample_standings": ml.SAMPLE_STANDINGS,
        "schema_version": ml.LAYOUT_SCHEMA_VERSION,
    }
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Group-level layouts (event_id = 0)
# --------------------------------------------------------------------------- #
@event_layouts_bp.get("/groups/<int:group_id>/event-layouts")
async def list_group_event_layouts(group_id: int):
    user_id = current_user_id()
    ml = _layouts_module()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            out = []
            for message_type in EVENT_MESSAGE_LAYOUT_TYPES:
                custom_row = (
                    _load_row(s, group_id, message_type)
                    if group_id != TEMPLATE_GROUP_ID
                    else None
                )
                default_row = _load_row(s, TEMPLATE_GROUP_ID, message_type)
                default = (
                    _serialize_row(default_row) if default_row is not None else None
                ) or _serialize_default(
                    message_type, ml.DEFAULT_LAYOUTS.get(message_type) or {})
                out.append({
                    "message_type": message_type,
                    "custom": _serialize_row(custom_row) if custom_row is not None else None,
                    "default": default,
                })
            return {"layouts": out}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_layouts_bp.put("/groups/<int:group_id>/event-layouts/<message_type>")
async def put_group_event_layout(group_id: int, message_type: str):
    user_id = current_user_id()
    _validate_message_type(message_type)
    ml = _layouts_module()
    body = await json_body()
    data = _validate_body(body, ml)

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            if group_id == TEMPLATE_GROUP_ID:
                # Group 1 rows are the system defaults — site staff only.
                assert_superadmin(user)
            else:
                assert_group_admin(
                    s, user_id, group_id, manageable_guild_ids(user_id), user=user)
                assert_group_entitlement(
                    s, user_id, group_id, "custom_embeds",
                    manage_guild_ids=manageable_guild_ids(user_id), user=user,
                )
            return _upsert(
                s, user_id, group_id, message_type, 0, data, ml,
                audit_group_id=group_id if group_id != TEMPLATE_GROUP_ID else None,
            )

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "layout": saved}))


@event_layouts_bp.delete("/groups/<int:group_id>/event-layouts/<message_type>")
async def delete_group_event_layout(group_id: int, message_type: str):
    user_id = current_user_id()
    _validate_message_type(message_type)
    if group_id == TEMPLATE_GROUP_ID:
        abort_problem(
            422,
            "Cannot delete defaults",
            "The template group's layouts are the system defaults; edit them instead.",
        )

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            row = _load_row(s, group_id, message_type)
            if row is None:
                return False
            before = _serialize_row(row)
            s.delete(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="event_layouts.reset",
                    target=f"web_event_message_layouts.{message_type}",
                    before=json.dumps(before) if before else None,
                    after=None,
                )
            )
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Per-event overrides (event_id = the event's id)
# --------------------------------------------------------------------------- #
def _event_owner_group(ev) -> int:
    """Override rows live under the event's host group; global events (no
    group) use the template group — superadmin-only via _assert_event_admin."""
    return ev.group_id or TEMPLATE_GROUP_ID


def _assert_event_layout_entitlement(s, user_id: int, ev, user) -> None:
    """PUT gate: the host group must hold ``custom_embeds`` (the renderer
    won't honor the rows otherwise). Checked directly — not via
    assert_group_entitlement — because clan-vs-clan co-managers may edit an
    event without administering the HOST group."""
    if is_superadmin(user) or not ev.group_id:
        return
    try:
        from db.entitlements import has_custom_embeds

        entitled = has_custom_embeds(ev.group_id)
    except Exception:
        entitled = False
    if not entitled:
        abort_problem(
            403,
            "Subscription required",
            "The host group's subscription does not include Custom embeds. "
            "Upgrade on the Subscription tab.",
            extra={"code": "entitlement_required", "entitlement": "custom_embeds"},
        )


@event_layouts_bp.get("/events/<int:event_id>/layouts")
async def list_event_layouts(event_id: int):
    user_id = current_user_id()
    ml = _layouts_module()

    def _load():
        from web_api.routes.events import _assert_event_admin, _load_event_or_404

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            owner_gid = _event_owner_group(ev)

            out = []
            for message_type in EVENT_MESSAGE_LAYOUT_TYPES:
                override_row = _load_row(s, owner_gid, message_type, ev.id)
                # The layout the event would use WITHOUT its override — what
                # the editor seeds from and what "revert" returns to.
                effective = ml.load_layout(s, ev.group_id, message_type)
                out.append({
                    "message_type": message_type,
                    "override": (
                        _serialize_row(override_row) if override_row is not None else None
                    ),
                    "effective": _serialize_default(message_type, effective),
                })
            return {"layouts": out}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_layouts_bp.put("/events/<int:event_id>/layouts/<message_type>")
async def put_event_layout(event_id: int, message_type: str):
    user_id = current_user_id()
    _validate_message_type(message_type)
    ml = _layouts_module()
    body = await json_body()
    data = _validate_body(body, ml)

    def _apply():
        from web_api.routes.events import _assert_event_admin, _load_event_or_404

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            user = load_user(s, user_id)
            _assert_event_layout_entitlement(s, user_id, ev, user)
            return _upsert(
                s, user_id, _event_owner_group(ev), message_type, ev.id, data, ml,
                audit_group_id=ev.group_id,
            )

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "layout": saved}))


@event_layouts_bp.delete("/events/<int:event_id>/layouts/<message_type>")
async def delete_event_layout(event_id: int, message_type: str):
    user_id = current_user_id()
    _validate_message_type(message_type)

    def _apply():
        from web_api.routes.events import _assert_event_admin, _load_event_or_404

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            row = _load_row(s, _event_owner_group(ev), message_type, ev.id)
            if row is None:
                return False
            before = _serialize_row(row)
            s.delete(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=ev.group_id,
                    event_id=ev.id,
                    action="event_layouts.reset",
                    target=f"web_event_message_layouts.{message_type}",
                    before=json.dumps(before) if before else None,
                    after=None,
                )
            )
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
