"""Hall of Fame layout editor — the Hall of Fame counterpart of
routes/notification_layouts.py, over the same ``group_component_layouts`` table
(``notification_type = "hall_of_fame"``).

  GET    /api/v1/hall-of-fame/layout/meta                  (any signed-in user)
  GET    /api/v1/groups/{id}/hall-of-fame/layout           (group admin)
  PUT    /api/v1/groups/{id}/hall-of-fame/layout           (group admin + hall_of_fame)
  DELETE /api/v1/groups/{id}/hall-of-fame/layout           (group admin)
  POST   /api/v1/groups/{id}/hall-of-fame/layout/preview   (group admin)

The DSL itself lives in ``services/hof_layout.py``. Like a notification
layout, a saved Hall of Fame layout is a draft until ``active`` is set, and only
an active one changes what the channel shows; DELETE puts the group back on the
default.

**The preview is rendered here, not in the browser.** A Hall of Fame message is
mostly the group's own standings, and the interesting questions — does this
line survive for a boss nobody has looted, does a raid with three modes still
fit — can only be answered with real numbers. So the editor posts its draft and
gets back exactly what the bot would send for one of the group's bosses, built
by the same ``hof_data`` collector and ``hof_layout`` renderer the bot uses.

Writes are gated on the ``hall_of_fame`` entitlement, the same one that gates
the Hall of Fame itself. GET reports the gate as ``enabled`` so the page can
tell "needs an upgrade" from "the backend is down".

Service modules are imported lazily inside handlers: the unit-test conftest
stubs the ``services`` package.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import AuditLog
from db.models import GroupComponentLayout, GroupConfiguration
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

hof_layouts_bp = Blueprint("v1_hof_layouts", __name__)

# The global group always runs a Hall of Fame (services/hall_of_fame.py).
_GLOBAL_GROUP_ID = 2
_MAX_LAYOUT_JSON = 30_000
# Preview a boss outside the group's list only by exact name, and only this
# many characters of it.
_MAX_BOSS_NAME = 100


def _hl():
    import importlib

    return importlib.import_module("services.hof_layout")


def _entitled(s, group_id: int, user) -> bool:
    if group_id == _GLOBAL_GROUP_ID:
        return True
    from web_api.entitlements import resolve_group_entitlements

    try:
        return bool(resolve_group_entitlements(s, group_id, user=user).get("hall_of_fame"))
    except Exception:
        return False


def _load_row(s, group_id: int, layout_type: str):
    return (
        s.query(GroupComponentLayout)
        .filter(
            GroupComponentLayout.group_id == group_id,
            GroupComponentLayout.notification_type == layout_type,
        )
        .first()
    )


def _serialize_layout(document) -> dict | None:
    if not isinstance(document, dict) or not isinstance(document.get("blocks"), list):
        return None
    return {"accent_color": document.get("accent_color") or None, "blocks": document["blocks"]}


def _serialize_row(row) -> dict | None:
    try:
        return _serialize_layout(json.loads(row.layout))
    except (TypeError, ValueError):
        return None


def _config(s, group_id: int) -> dict:
    rows = (
        s.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key.in_([
                "personal_best_embed_boss_list",
                "number_of_pbs_to_display",
                "hof_managed_by_core",
            ]),
        )
        .all()
    )
    return {r.config_key: r for r in rows}


def _boss_names(config: dict) -> list[str]:
    from utils.hof import build_boss_plan, parse_boss_list

    row = config.get("personal_best_embed_boss_list")
    if row is None:
        return []
    names = parse_boss_list(row.config_value, row.long_value)
    return [e.display_name for e in build_boss_plan(names)]


def _pb_entries(config: dict) -> int:
    # Mirrors services/hall_of_fame._load_group_config: '0' is the legacy
    # "unset" sentinel, and the default is 5.
    row = config.get("number_of_pbs_to_display")
    try:
        value = int(row.config_value) if row is not None else 0
    except (TypeError, ValueError):
        value = 0
    return min(10, value) if value > 0 else 5


def _emoji_supported(config: dict) -> bool:
    """Whether the group's Hall of Fame is posted by the core bot, which is
    the only application that owns the boss/item emoji. The retiring Hall of
    Fame bot leaves them out."""
    row = config.get("hof_managed_by_core")
    return bool(row is not None and str(row.config_value).strip() == "1")


def _validate_body(body, hl) -> dict:
    if not isinstance(body, dict):
        abort_problem(422, "Invalid layout", "Body must be an object.")
    accent = body.get("accent_color") or None
    document = {"accent_color": accent, "blocks": body.get("blocks")}
    ok, errors = hl.validate_layout(document)
    if not ok:
        abort_problem(422, "Invalid layout", " ".join(errors[:8]))
    if len(json.dumps(document)) > _MAX_LAYOUT_JSON:
        abort_problem(422, "Invalid layout", "Layout is too large.")
    return {"accent_color": accent, "blocks": document["blocks"], "active": bool(body.get("active"))}


def _admin(s, user_id: int, group_id: int):
    user = load_user(s, user_id)
    assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
    return user


# --------------------------------------------------------------------------- #
# Editor metadata
# --------------------------------------------------------------------------- #
@hof_layouts_bp.get("/hall-of-fame/layout/meta")
async def hof_layout_meta():
    current_user_id()
    hl = _hl()
    payload = hl.meta()
    try:
        from utils.game_emojis import catalog

        # The core application's glyphs: the Hall of Fame is posted by the core
        # bot, and a browser can draw these from Discord's CDN by id.
        payload["emojis"] = catalog("core")
    except Exception:
        payload["emojis"] = []
    payload["default_layout"] = hl.default_layout()
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Group layout
# --------------------------------------------------------------------------- #
@hof_layouts_bp.get("/groups/<int:group_id>/hall-of-fame/layout")
async def get_group_hof_layout(group_id: int):
    user_id = current_user_id()
    hl = _hl()

    def _load():
        with db_session() as s:
            user = _admin(s, user_id, group_id)
            row = _load_row(s, group_id, hl.LAYOUT_TYPE)
            custom = _serialize_row(row) if row is not None else None
            # Live means what the bot means: saved active AND still valid.
            live = bool(row is not None and row.active and custom and hl.validate_layout(custom)[0])
            config = _config(s, group_id)
            return {
                "enabled": _entitled(s, group_id, user),
                "custom": custom,
                "active": live,
                "default": hl.default_layout(),
                "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
                "bosses": _boss_names(config),
                "emoji_supported": _emoji_supported(config),
            }

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@hof_layouts_bp.put("/groups/<int:group_id>/hall-of-fame/layout")
async def put_group_hof_layout(group_id: int):
    user_id = current_user_id()
    hl = _hl()
    data = _validate_body(await json_body(), hl)

    def _apply():
        with db_session() as s:
            user = _admin(s, user_id, group_id)
            if not _entitled(s, group_id, user):
                abort_problem(
                    403, "Upgrade required",
                    "Customizing the Hall of Fame is part of the Hall of Fame plan.",
                    extra={"code": "hof_requires_upgrade"},
                )
            row = _load_row(s, group_id, hl.LAYOUT_TYPE)
            before = _serialize_row(row) if row is not None else None
            was_active = bool(row is not None and row.active)
            if row is None:
                row = GroupComponentLayout(group_id=group_id, notification_type=hl.LAYOUT_TYPE)
                s.add(row)
            row.layout = json.dumps({"accent_color": data["accent_color"], "blocks": data["blocks"]})
            row.active = data["active"]
            s.flush()
            after = _serialize_row(row)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=group_id,
                action="hof_layout.update",
                target=f"group_component_layouts.{hl.LAYOUT_TYPE}",
                before=json.dumps({"layout": before, "active": was_active}) if before else None,
                after=json.dumps({"layout": after, "active": data["active"]}),
            ))
            s.commit()
            return {"layout": after, "active": data["active"]}

    saved = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True, **saved}))


@hof_layouts_bp.delete("/groups/<int:group_id>/hall-of-fame/layout")
async def delete_group_hof_layout(group_id: int):
    user_id = current_user_id()
    hl = _hl()

    def _apply():
        with db_session() as s:
            _admin(s, user_id, group_id)
            row = _load_row(s, group_id, hl.LAYOUT_TYPE)
            if row is None:
                return
            before = _serialize_row(row)
            was_active = bool(row.active)
            s.delete(row)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=group_id,
                action="hof_layout.reset",
                target=f"group_component_layouts.{hl.LAYOUT_TYPE}",
                before=json.dumps({"layout": before, "active": was_active}) if before else None,
                after=None,
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
@hof_layouts_bp.post("/groups/<int:group_id>/hall-of-fame/layout/preview")
async def preview_group_hof_layout(group_id: int):
    """Render a draft for one boss with the group's real standings.

    Validation problems come back as ``errors`` with a 200 rather than a 422:
    the editor calls this on every pause in typing, and a half-written layout
    is the normal state, not a failed request.
    """
    user_id = current_user_id()
    hl = _hl()
    body = await json_body()
    if not isinstance(body, dict):
        abort_problem(422, "Invalid preview", "Body must be an object.")
    layout = {"accent_color": body.get("accent_color") or None, "blocks": body.get("blocks")}
    boss = str(body.get("boss") or "").strip()[:_MAX_BOSS_NAME]

    def _render():
        with db_session() as s:
            _admin(s, user_id, group_id)
            ok, errors = hl.validate_layout(layout)
            if not ok:
                return {"ok": False, "errors": errors[:8]}
            return {"ok": True, **_render_preview(s, group_id, layout, boss, hl)}

    return private_no_store(jsonify(await asyncio.to_thread(_render)))


def _render_preview(s, group_id: int, layout: dict, boss: str, hl) -> dict:
    import importlib

    from db.models import Group, GroupPersonalBestMessage, NpcList
    from db.ops import get_formatted_name
    from utils.hof import DIRECTORY_KEY, build_boss_plan, npc_name_candidates, parse_boss_list

    hof_data = importlib.import_module("services.hof_data")

    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        abort_problem(404, "Not found", "Group not found.")
    config = _config(s, group_id)
    bosses = _boss_names(config)
    # Stay inside the group's list when it has one; a group still setting up
    # can preview any boss by name.
    name = boss if boss and (boss in bosses or not bosses) else (bosses[0] if bosses else "Zulrah")

    config_row = config.get("personal_best_embed_boss_list")
    configured = parse_boss_list(config_row.config_value, config_row.long_value) if config_row else []
    plan = [e for e in build_boss_plan(configured or [name]) if e.display_name == name]
    entry = plan[0] if plan else build_boss_plan([name])[0]

    npcs = []
    for variant in entry.variant_names:
        for candidate in npc_name_candidates(variant):
            npc = s.query(NpcList).filter(NpcList.npc_name == candidate).first()
            if npc:
                npcs.append(npc)
                break
    if not npcs:
        return {"boss": name, "bosses": bosses, "payload": None, "used_default": False,
                "message": f"No boss called '{name}' is known to the DropTracker."}

    pb_count = _pb_entries(config)
    if entry.grouped:
        pb_count = min(pb_count, 3)
    needs = hl.needed_boards(hl.DEFAULT_LAYOUT, pb_count)
    for board, rows in hl.needed_boards(layout, pb_count).items():
        needs[board] = max(needs.get(board, 0), rows)

    player_ids = list({p.player_id for p in group.get_players()})

    def display(player):
        if player is None:
            return "Unknown"
        try:
            return get_formatted_name(player.player_name, group_id, s, player_id=player.player_id)
        except Exception:
            return player.player_name or "Unknown"

    collector = hof_data.HofDataCollector(s, group_id, player_ids, display_name=display)
    data = collector.build_entry(entry.display_name, entry.grouped, npcs, needs)

    directory = (
        s.query(GroupPersonalBestMessage)
        .filter(
            GroupPersonalBestMessage.group_id == group_id,
            GroupPersonalBestMessage.boss_name == DIRECTORY_KEY,
        )
        .order_by(GroupPersonalBestMessage.message_id.asc())
        .first()
    )
    directory_url = (
        f"https://discord.com/channels/{group.guild_id}/{directory.channel_id}/{directory.message_id}"
        if directory is not None else f"https://discord.com/channels/{group.guild_id or '@me'}"
    )
    # Glyphs resolve against this process's emoji profile, which for the web
    # API is the default "core" — the preview is drawn as the core bot posts it.
    emojis = hof_data.resolve_emoji_refs(hl.emoji_refs(layout))
    common = hof_data.common_tokens(directory_url)
    payload, used_default = hl.render_entry(layout, data, common, pb_count, emojis)
    return {"boss": name, "bosses": bosses, "payload": payload, "used_default": used_default}
