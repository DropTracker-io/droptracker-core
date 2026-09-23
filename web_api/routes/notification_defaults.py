"""Site-wide notification defaults — the staff editor over the template group.

Every group that has not designed its own notifications, or whose plan does
not include Custom notification designs, is sent the template group's
(group 1's) designs. Three systems keep their defaults there, and this module
is the one staff surface over all three, backing the ACP's Default embeds page:

  embeds             ``group_embeds`` rows on group 1. Seven types have a row
                     and nothing behind it — a missing row stops the send — so
                     those can be edited but never removed. quest/death/diary/
                     slayer have no row and are sent the embed built in code
                     (``NotificationService._build_default_*_embed``); a row
                     saved here replaces that built-in for every group, and
                     removing it brings the built-in back.
  event layouts      ``web_event_message_layouts`` rows on group 1 (event_id 0).
                     The resolver falls back to the code default
                     (``services/event_message_layouts.DEFAULT_LAYOUTS``) when
                     a row is missing, so removing one restores the built-in.
  component layouts  ``group_component_layouts`` rows on group 1. Never sent —
                     group 1 has no channels and these rows are never active.
                     They replace the code default as the starting point a
                     group's builder copies (routes/notification_layouts.py).

  GET    /api/v1/admin/notification-defaults/embeds
  PUT    /api/v1/admin/notification-defaults/embeds/{type}
  DELETE /api/v1/admin/notification-defaults/embeds/{type}             (quest/death/diary/slayer)
  GET    /api/v1/admin/notification-defaults/event-layouts
  PUT    /api/v1/admin/notification-defaults/event-layouts/{type}
  DELETE /api/v1/admin/notification-defaults/event-layouts/{type}
  GET    /api/v1/admin/notification-defaults/component-layouts
  PUT    /api/v1/admin/notification-defaults/component-layouts/{type}
  DELETE /api/v1/admin/notification-defaults/component-layouts/{type}

Superadmin only. Bodies go through the group editors' own validators and rows
through their own writers, so a default is always something a group could have
saved itself. Each listing also says who an edit does NOT reach: groups whose
own row wins at send time, decided by the send path's own ``has_custom_embeds``.
"""
from __future__ import annotations

import asyncio
import importlib
import json

from quart import Blueprint, jsonify

from db import AuditLog, EVENT_MESSAGE_LAYOUT_TYPES, GroupEmbed
from db.models import EventMessageLayout, GroupComponentLayout
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user
from web_api.routes import embeds as embed_routes
from web_api.routes import event_layouts as event_layout_routes
from web_api.routes import notification_layouts as component_layout_routes

notification_defaults_bp = Blueprint("v1_notification_defaults", __name__)

TEMPLATE_GROUP_ID = 1

_PREFIX = "/admin/notification-defaults"


def _event_layouts_module():
    # Dotted import: the unit-test conftest stubs the ``services`` package.
    return importlib.import_module("services.event_message_layouts")


def _component_layouts_module():
    return importlib.import_module("services.component_layout")


# --------------------------------------------------------------------------- #
# Built-in embeds
# --------------------------------------------------------------------------- #
def _builtin_embed(embed_type: str, title: str, description: str, color: str,
                   fields: tuple, thumbnail: str | None = None) -> dict:
    return {
        "embed_type": embed_type,
        "title": title,
        "url": None,
        "description": description,
        "color": color,
        "thumbnail": thumbnail,
        "image": None,
        "timestamp": False,
        "fields": [{"name": n, "value": v, "inline": inline} for n, v, inline in fields],
    }


# The code-built embeds the row-less types are sent, written as templates
# so the editor can show them and start from them. A template cannot branch, so
# these are close rather than exact: the builders print "?" for a missing
# count, fall back to the region id when there is no location, and wrap the
# value lost in code ticks — here a missing value drops its field instead
# (replace_placeholders drops a field whose value resolves blank; ticks would
# leave "``" behind). Titles, colours and wording must follow the builders;
# tests/unit/test_notification_default_embeds.py holds them together.
BUILTIN_EMBEDS = {
    "quest": _builtin_embed(
        "quest",
        "Quest Completed",
        "{player_name} completed **{quest_name}**.",
        "#5A8DEE",
        (
            ("Quest Progress", "`{quests_completed}`/`{total_quests}` ({completion_percentage})", True),
            ("Quest Points", "`{quest_points}`/`{total_quest_points}` ({qp_percentage})", True),
            ("Video", "{video_link}", False),
        ),
    ),
    "death": _builtin_embed(
        "death",
        "Player Death",
        "{player_name} has died.",
        "#B23B3B",
        (
            ("Killed By", "{source}", True),
            ("Location", "{region_name}", True),
            ("Value Lost", "{value_lost}", True),
            ("Video", "{video_link}", False),
        ),
    ),
    "diary": _builtin_embed(
        "diary",
        "Achievement Diary Completed",
        "{player_name} completed the **{diary_tier} {diary_name}** diary.",
        "#5A8DEE",
        (("Video", "{video_link}", False),),
    ),
    # Every field below the description can be absent (an unknown master, a
    # task that earned no points), and each drops on its own.
    "slayer": _builtin_embed(
        "slayer",
        "Slayer Task Completed",
        "{player_name} killed **{slayer_kills} {slayer_task}**.",
        "#8B1A1A",
        (
            ("Master", "{slayer_master}", True),
            ("Task streak", "{slayer_streak}", True),
            ("Points earned", "{slayer_points}", True),
            ("Slayer XP", "{slayer_xp}", True),
            ("Video", "{video_link}", False),
        ),
        thumbnail="{slayer_icon}",
    ),
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _owners(pairs) -> dict:
    """``[(type, group_id), ...]`` -> ``{type: {group_id, ...}}``."""
    out: dict = {}
    for kind, group_id in pairs:
        out.setdefault(kind, set()).add(group_id)
    return out


def _entitled_group_ids(group_ids) -> set:
    """The groups whose own designs are sent instead of the defaults.

    The send path's own check, so these counts cannot disagree with what groups
    receive. One cached lookup per group (60s TTL in db.entitlements).
    """
    from db.entitlements import has_custom_embeds

    out = set()
    for group_id in group_ids:
        try:
            if has_custom_embeds(group_id):
                out.add(group_id)
        except Exception:
            # Fail closed, as the send path does.
            continue
    return out


def _all_ids(owners: dict) -> set:
    return set().union(*owners.values()) if owners else set()


def _audit(s, user_id: int, action: str, target: str, before, after) -> None:
    # Site-wide rather than a group's change, so no group_id — the same call
    # routes/event_layouts.py makes for template rows. Find these under
    # action "notification_defaults" in the audit log.
    s.add(
        AuditLog(
            actor_user_id=user_id,
            group_id=None,
            action=action,
            target=target,
            before=json.dumps(before) if before is not None else None,
            after=json.dumps(after) if after is not None else None,
        )
    )


def _load_staff(s, user_id: int) -> None:
    assert_superadmin(load_user(s, user_id))


# --------------------------------------------------------------------------- #
# Embed templates
# --------------------------------------------------------------------------- #
@notification_defaults_bp.get(f"{_PREFIX}/embeds")
async def list_embed_defaults():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _load_staff(s, user_id)
            templates = {}
            for row in (
                s.query(GroupEmbed)
                .filter(GroupEmbed.group_id == TEMPLATE_GROUP_ID)
                .order_by(GroupEmbed.embed_id)
                .all()
            ):
                # First row per type, as _load_rows and the sender read it.
                if row.embed_type not in templates:
                    templates[row.embed_type] = embed_routes._serialize_embed(row)
            owners = _owners(
                s.query(GroupEmbed.embed_type, GroupEmbed.group_id)
                .filter(GroupEmbed.group_id != TEMPLATE_GROUP_ID)
                .distinct()
                .all()
            )
            return templates, owners

    templates, owners = await asyncio.to_thread(_load)
    entitled = await asyncio.to_thread(_entitled_group_ids, _all_ids(owners))

    out = []
    for embed_type in embed_routes.EMBED_TYPES:
        saved_by = owners.get(embed_type, set())
        out.append({
            "embed_type": embed_type,
            "template": templates.get(embed_type),
            "builtin": BUILTIN_EMBEDS.get(embed_type),
            "custom_count": len(saved_by),
            "override_count": len(saved_by & entitled),
        })
    return private_no_store(jsonify({"embeds": out}))


@notification_defaults_bp.put(f"{_PREFIX}/embeds/<embed_type>")
async def put_embed_default(embed_type: str):
    user_id = current_user_id()
    embed_routes._validate_embed_type(embed_type)
    data = embed_routes._validate_body(await json_body())

    def _apply():
        with db_session() as s:
            _load_staff(s, user_id)
            before, row = embed_routes._store_embed(s, TEMPLATE_GROUP_ID, embed_type, data)
            after = embed_routes._serialize_embed(row)
            _audit(s, user_id, "notification_defaults.embed.update",
                   f"group_embeds.{embed_type}", before, after)
            s.commit()
            return after

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "embed": saved}))


@notification_defaults_bp.delete(f"{_PREFIX}/embeds/<embed_type>")
async def delete_embed_default(embed_type: str):
    user_id = current_user_id()
    embed_routes._validate_embed_type(embed_type)

    def _apply():
        with db_session() as s:
            _load_staff(s, user_id)
            if embed_type not in BUILTIN_EMBEDS:
                abort_problem(
                    422,
                    "This default can't be removed",
                    "There is no built-in embed behind this type, so without the default "
                    "these notifications would stop being sent. Edit it instead.",
                )
            rows = embed_routes._load_rows(s, TEMPLATE_GROUP_ID, embed_type)
            if not rows:
                return False
            before = embed_routes._serialize_embed(rows[0])
            for row in rows:
                s.delete(row)
            _audit(s, user_id, "notification_defaults.embed.reset",
                   f"group_embeds.{embed_type}", before, None)
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Event message layouts
# --------------------------------------------------------------------------- #
def _validate_event_type(message_type: str) -> None:
    if message_type not in EVENT_MESSAGE_LAYOUT_TYPES:
        abort_problem(
            422,
            "Unknown message type",
            f"'{message_type}' is not a customizable event message type.",
        )


@notification_defaults_bp.get(f"{_PREFIX}/event-layouts")
async def list_event_layout_defaults():
    user_id = current_user_id()
    ml = _event_layouts_module()

    def _load():
        with db_session() as s:
            _load_staff(s, user_id)
            templates = {}
            for row in (
                s.query(EventMessageLayout)
                .filter(
                    EventMessageLayout.group_id == TEMPLATE_GROUP_ID,
                    EventMessageLayout.event_id == 0,
                )
                .order_by(EventMessageLayout.id)
                .all()
            ):
                if row.message_type not in templates:
                    templates[row.message_type] = event_layout_routes._serialize_row(row)
            owners = _owners(
                s.query(EventMessageLayout.message_type, EventMessageLayout.group_id)
                .filter(
                    EventMessageLayout.group_id != TEMPLATE_GROUP_ID,
                    EventMessageLayout.event_id == 0,
                )
                .distinct()
                .all()
            )
            return templates, owners

    templates, owners = await asyncio.to_thread(_load)
    entitled = await asyncio.to_thread(_entitled_group_ids, _all_ids(owners))

    out = []
    for message_type in EVENT_MESSAGE_LAYOUT_TYPES:
        saved_by = owners.get(message_type, set())
        out.append({
            "message_type": message_type,
            # None for a row that no longer parses: the resolver skips it too.
            "template": templates.get(message_type),
            "builtin": event_layout_routes._serialize_default(
                message_type, ml.DEFAULT_LAYOUTS.get(message_type) or {}),
            "custom_count": len(saved_by),
            "override_count": len(saved_by & entitled),
        })
    return private_no_store(jsonify({"layouts": out}))


@notification_defaults_bp.put(f"{_PREFIX}/event-layouts/<message_type>")
async def put_event_layout_default(message_type: str):
    user_id = current_user_id()
    _validate_event_type(message_type)
    ml = _event_layouts_module()
    data = event_layout_routes._validate_body(await json_body(), ml)

    def _apply():
        with db_session() as s:
            _load_staff(s, user_id)
            return event_layout_routes._upsert(
                s, user_id, TEMPLATE_GROUP_ID, message_type, 0, data, ml,
                audit_group_id=None,
                action="notification_defaults.event_layout.update",
            )

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, "layout": saved}))


@notification_defaults_bp.delete(f"{_PREFIX}/event-layouts/<message_type>")
async def delete_event_layout_default(message_type: str):
    user_id = current_user_id()
    _validate_event_type(message_type)

    def _apply():
        with db_session() as s:
            _load_staff(s, user_id)
            row = event_layout_routes._load_row(s, TEMPLATE_GROUP_ID, message_type)
            if row is None:
                return False
            before = event_layout_routes._serialize_row(row)
            s.delete(row)
            _audit(s, user_id, "notification_defaults.event_layout.reset",
                   f"web_event_message_layouts.{message_type}", before, None)
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Notification component layouts (starting points)
# --------------------------------------------------------------------------- #
@notification_defaults_bp.get(f"{_PREFIX}/component-layouts")
async def list_component_layout_defaults():
    user_id = current_user_id()
    cl = _component_layouts_module()

    def _load():
        with db_session() as s:
            _load_staff(s, user_id)
            # Only templates the group editor would actually hand out.
            templates = component_layout_routes.template_layouts(s, cl)
            saved: dict = {}
            sending: dict = {}
            for row in (
                s.query(GroupComponentLayout)
                .filter(GroupComponentLayout.group_id != TEMPLATE_GROUP_ID)
                .all()
            ):
                saved.setdefault(row.notification_type, set()).add(row.group_id)
                layout = component_layout_routes._serialize_row(row)
                # Live the way load_active_layout means it, less the entitlement
                # (resolved below, once per group).
                if row.active and layout is not None and cl.validate_layout(layout)[0]:
                    sending.setdefault(row.notification_type, set()).add(row.group_id)
            return templates, saved, sending

    templates, saved, sending = await asyncio.to_thread(_load)
    entitled = await asyncio.to_thread(_entitled_group_ids, _all_ids(sending))

    out = []
    for notification_type in cl.NOTIFICATION_TYPES:
        builtin = component_layout_routes._serialize_layout(
            cl.default_layout(notification_type)
        ) or {"accent_color": None, "blocks": []}
        out.append({
            "notification_type": notification_type,
            "template": templates.get(notification_type),
            "builtin": builtin,
            "custom_count": len(saved.get(notification_type, ())),
            "live_count": len(sending.get(notification_type, set()) & entitled),
        })
    return private_no_store(jsonify({"layouts": out}))


@notification_defaults_bp.put(f"{_PREFIX}/component-layouts/<notification_type>")
async def put_component_layout_default(notification_type: str):
    user_id = current_user_id()
    cl = _component_layouts_module()
    component_layout_routes._validate_notification_type(cl, notification_type)
    data = component_layout_routes._validate_body(await json_body(), cl)
    # A starting point, never something group 1 sends — whatever the body says.
    data["active"] = False

    def _apply():
        with db_session() as s:
            _load_staff(s, user_id)
            before, _was_active, row = component_layout_routes.store_row(
                s, TEMPLATE_GROUP_ID, notification_type, data)
            after = component_layout_routes._serialize_row(row)
            _audit(s, user_id, "notification_defaults.component_layout.update",
                   f"group_component_layouts.{notification_type}", before, after)
            s.commit()
            return after

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({
        "ok": True,
        "notification_type": notification_type,
        "layout": saved,
        "active": False,
    }))


@notification_defaults_bp.delete(f"{_PREFIX}/component-layouts/<notification_type>")
async def delete_component_layout_default(notification_type: str):
    user_id = current_user_id()
    cl = _component_layouts_module()
    component_layout_routes._validate_notification_type(cl, notification_type)

    def _apply():
        with db_session() as s:
            _load_staff(s, user_id)
            row = component_layout_routes._load_row(s, TEMPLATE_GROUP_ID, notification_type)
            if row is None:
                return False
            before = component_layout_routes._serialize_row(row)
            s.delete(row)
            _audit(s, user_id, "notification_defaults.component_layout.reset",
                   f"group_component_layouts.{notification_type}", before, None)
            s.commit()
            return True

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
