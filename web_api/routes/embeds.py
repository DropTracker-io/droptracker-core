"""Custom Discord embed templates (subscription-gated).

  GET    /api/v1/groups/{id}/embeds               (session + group admin)
  PUT    /api/v1/groups/{id}/embeds/{embed_type}  (group admin + custom_embeds entitlement)
  DELETE /api/v1/groups/{id}/embeds/{embed_type}  (session + group admin)

Templates live in ``group_embeds`` / ``group_embed_fields`` — the same tables
the notification service reads (``db/ops.py get_group_embed``). Group 1 is the
system template group: its rows are the defaults every non-subscribed group
falls back to, so GET returns both the group's custom template and the group-1
default per type. DELETE reverts a type to the default and intentionally needs
no entitlement (a downgraded group must be able to clean up).

The bot only *renders* a group's own rows when ``db.entitlements
.has_custom_embeds()`` passes, so a row saved here never leaks to Discord
without an active subscription — but PUT still requires the entitlement so the
editor can't be used ahead of purchase.
"""
from __future__ import annotations

import asyncio
import json
import re

from quart import Blueprint, jsonify

from db import AuditLog, GroupEmbed
from db.models import Field as EmbedField
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    assert_group_entitlement,
    assert_superadmin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

embeds_bp = Blueprint("v1_embeds", __name__)

TEMPLATE_GROUP_ID = 1

# Embed types the notification pipeline actually renders (see
# services/notification_service.py + the lootboard loop in bots/main.py).
EMBED_TYPES = ("drop", "clog", "pb", "ca", "pet", "level_up", "quest", "lb")

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

# Column limits (db/models/embed.py) — Discord's own limits are looser except
# for title (256) and field counts, so the columns are the binding constraint.
_MAX_TITLE = 255
_MAX_DESCRIPTION = 1000
_MAX_URL = 200
_MAX_FIELDS = 25
_MAX_FIELD_NAME = 256
_MAX_FIELD_VALUE = 1024


def _serialize_embed(row: GroupEmbed) -> dict:
    fields = sorted(row.fields, key=lambda f: f.field_id)
    return {
        "embed_type": row.embed_type,
        "title": row.title or "",
        "description": row.description or "",
        "color": row.color or None,
        "thumbnail": row.thumbnail or None,
        "image": row.image or None,
        "timestamp": bool(row.timestamp),
        "fields": [
            {
                "name": f.field_name or "",
                "value": f.field_value or "",
                "inline": bool(f.inline),
            }
            for f in fields
        ],
    }


def _validate_embed_type(embed_type: str) -> None:
    if embed_type not in EMBED_TYPES:
        abort_problem(
            422,
            "Unknown embed type",
            f"'{embed_type}' is not a valid embed type. Valid types: {', '.join(EMBED_TYPES)}.",
        )


def _optional_url(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        abort_problem(422, "Invalid embed", f"'{key}' must be a string URL.")
    value = value.strip()
    if len(value) > _MAX_URL:
        abort_problem(422, "Invalid embed", f"'{key}' must be at most {_MAX_URL} characters.")
    if not value.lower().startswith(("http://", "https://")):
        abort_problem(422, "Invalid embed", f"'{key}' must be an http(s) URL.")
    return value


def _validate_body(body: dict) -> dict:
    """Validate + normalize a PUT body into the stored shape."""
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        abort_problem(422, "Invalid embed", "A non-empty 'title' is required.")
    title = title.strip()
    if len(title) > _MAX_TITLE:
        abort_problem(422, "Invalid embed", f"'title' must be at most {_MAX_TITLE} characters.")

    description = body.get("description") or ""
    if not isinstance(description, str):
        abort_problem(422, "Invalid embed", "'description' must be a string.")
    if len(description) > _MAX_DESCRIPTION:
        abort_problem(
            422, "Invalid embed", f"'description' must be at most {_MAX_DESCRIPTION} characters."
        )

    color = body.get("color")
    if color in (None, ""):
        color = None
    elif not isinstance(color, str) or not _HEX_COLOR.match(color):
        abort_problem(422, "Invalid embed", "'color' must be a hex color like #ffb83f.")

    timestamp = body.get("timestamp", False)
    if not isinstance(timestamp, bool):
        abort_problem(422, "Invalid embed", "'timestamp' must be a boolean.")

    raw_fields = body.get("fields") or []
    if not isinstance(raw_fields, list):
        abort_problem(422, "Invalid embed", "'fields' must be an array.")
    if len(raw_fields) > _MAX_FIELDS:
        abort_problem(422, "Invalid embed", f"At most {_MAX_FIELDS} fields are allowed.")
    fields = []
    for i, f in enumerate(raw_fields):
        if not isinstance(f, dict):
            abort_problem(422, "Invalid embed", f"Field #{i + 1} must be an object.")
        name = f.get("name")
        value = f.get("value")
        inline = f.get("inline", True)
        if not isinstance(name, str) or not name.strip():
            abort_problem(422, "Invalid embed", f"Field #{i + 1} needs a non-empty 'name'.")
        if not isinstance(value, str) or not value.strip():
            abort_problem(422, "Invalid embed", f"Field #{i + 1} needs a non-empty 'value'.")
        if len(name) > _MAX_FIELD_NAME:
            abort_problem(
                422, "Invalid embed",
                f"Field #{i + 1} name must be at most {_MAX_FIELD_NAME} characters.",
            )
        if len(value) > _MAX_FIELD_VALUE:
            abort_problem(
                422, "Invalid embed",
                f"Field #{i + 1} value must be at most {_MAX_FIELD_VALUE} characters.",
            )
        if not isinstance(inline, bool):
            abort_problem(422, "Invalid embed", f"Field #{i + 1} 'inline' must be a boolean.")
        fields.append({"name": name.strip(), "value": value, "inline": inline})

    return {
        "title": title,
        "description": description,
        "color": color,
        "thumbnail": _optional_url(body, "thumbnail"),
        "image": _optional_url(body, "image"),
        "timestamp": timestamp,
        "fields": fields,
    }


def _load_rows(s, group_id: int, embed_type: str) -> list[GroupEmbed]:
    return (
        s.query(GroupEmbed)
        .filter(GroupEmbed.group_id == group_id, GroupEmbed.embed_type == embed_type)
        .order_by(GroupEmbed.embed_id)
        .all()
    )


def _assert_can_edit_templates(s, user_id: int, group_id: int, user) -> None:
    """Group 1 is the system default set — only site staff may touch it."""
    if group_id == TEMPLATE_GROUP_ID:
        assert_superadmin(user)
    else:
        assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)


@embeds_bp.get("/groups/<int:group_id>/embeds")
async def list_group_embeds(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            out = []
            for embed_type in EMBED_TYPES:
                custom_rows = _load_rows(s, group_id, embed_type)
                default_rows = (
                    _load_rows(s, TEMPLATE_GROUP_ID, embed_type)
                    if group_id != TEMPLATE_GROUP_ID
                    else custom_rows
                )
                out.append(
                    {
                        "embed_type": embed_type,
                        "custom": (
                            _serialize_embed(custom_rows[0])
                            if custom_rows and group_id != TEMPLATE_GROUP_ID
                            else None
                        ),
                        "default": _serialize_embed(default_rows[0]) if default_rows else None,
                    }
                )
            return {"embeds": out}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@embeds_bp.put("/groups/<int:group_id>/embeds/<embed_type>")
async def put_group_embed(group_id: int, embed_type: str):
    user_id = current_user_id()
    _validate_embed_type(embed_type)
    body = await json_body()
    data = _validate_body(body)

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            _assert_can_edit_templates(s, user_id, group_id, user)
            if group_id != TEMPLATE_GROUP_ID:
                assert_group_entitlement(
                    s,
                    user_id,
                    group_id,
                    "custom_embeds",
                    manage_guild_ids=manageable_guild_ids(user_id),
                    user=user,
                )

            rows = _load_rows(s, group_id, embed_type)
            before = _serialize_embed(rows[0]) if rows else None

            # One template per (group, type): keep the first row, drop strays
            # (legacy XF editor bug left duplicates for some groups).
            for stray in rows[1:]:
                s.delete(stray)
            row = rows[0] if rows else None
            if row is None:
                row = GroupEmbed(group_id=group_id, embed_type=embed_type)
                s.add(row)

            row.title = data["title"]
            row.description = data["description"]
            row.color = data["color"]
            row.thumbnail = data["thumbnail"]
            row.image = data["image"]
            row.timestamp = data["timestamp"]
            row.fields.clear()
            for f in data["fields"]:
                row.fields.append(
                    EmbedField(field_name=f["name"], field_value=f["value"], inline=f["inline"])
                )

            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="embeds.update",
                    target=f"group_embeds.{embed_type}",
                    before=json.dumps(before) if before else None,
                    after=json.dumps(_serialize_embed(row)),
                )
            )
            s.commit()
            return _serialize_embed(row)

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "embed": saved}))


@embeds_bp.delete("/groups/<int:group_id>/embeds/<embed_type>")
async def delete_group_embed(group_id: int, embed_type: str):
    user_id = current_user_id()
    _validate_embed_type(embed_type)
    if group_id == TEMPLATE_GROUP_ID:
        abort_problem(
            422,
            "Cannot delete defaults",
            "The template group's embeds are the system defaults; edit them instead.",
        )

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            rows = _load_rows(s, group_id, embed_type)
            if not rows:
                return False
            before = _serialize_embed(rows[0])
            for row in rows:
                s.delete(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="embeds.reset",
                    target=f"group_embeds.{embed_type}",
                    before=json.dumps(before),
                    after=None,
                )
            )
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
