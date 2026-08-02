"""Task 12 — superadmin surfaces.

Every endpoint independently enforces superadmin (403 otherwise):

  GET  /api/v1/admin/services
  POST /api/v1/admin/services/{unit}            { action: start|stop|restart }
  GET  /api/v1/admin/services/{unit}/logs
  GET  /api/v1/admin/backups                    (timer/service state + local sets)
  GET  /api/v1/admin/backups/logs
  GET  /api/v1/admin/backups/offsite            (B2 dt_backups/ listing)
  POST /api/v1/admin/backups/run
  GET  /api/v1/admin/b2/usage                   (bucket-wide storage + cost estimate)
  POST /api/v1/admin/discord/send               { channel_id, content }
  GET  /api/v1/admin/lookup?q=
  POST   /api/v1/admin/subscriptions/tiers      { SubscriptionTier }
  PATCH  /api/v1/admin/subscriptions/tiers/{key}
  DELETE /api/v1/admin/subscriptions/tiers/{key}
  GET  /api/v1/admin/audit?action=&actor_user_id=&group_id=&q=&page=&limit=
  GET  /api/v1/admin/users/{id}/overview
  POST /api/v1/admin/users/{id}/superadmin       { grant: bool }
  POST /api/v1/admin/users/{id}/moderator        { grant: bool } (+ profile badge)
  GET    /api/v1/admin/badges
  POST   /api/v1/admin/badges                    { key, name, ... } (upsert)
  DELETE /api/v1/admin/badges/{key}              (soft delete)
  POST   /api/v1/admin/players/{id}/badges       { badge_key, note? }
  DELETE /api/v1/admin/players/{id}/badges/{award_id}
  GET    /api/v1/admin/pb-blocks                  (blocked-PB NPC list)
  GET    /api/v1/admin/pb-blocks/search?q=        (bosses to block, with impact)
  POST   /api/v1/admin/pb-blocks                  { npc_id|npc_ids, confirm } (block + purge)
  DELETE /api/v1/admin/pb-blocks/{npc_id}         (unblock; rows NOT restored)
  GET    /api/v1/admin/item-values                 (list overrides + live preview)
  GET    /api/v1/admin/item-values/item-search?q=  (resolve item name → id)
  GET    /api/v1/admin/item-values/export          ({ txt } for valued_items.txt)
  POST   /api/v1/admin/item-values                 { item_id, item_name, components, ... }
  PATCH  /api/v1/admin/item-values/{override_id}   (partial update)
  DELETE /api/v1/admin/item-values/{override_id}

Service control is whitelisted to the units in SERVICE_REGISTRY (all app units
grouped by tier, plus read-only infrastructure rows) and shells out via
systemctl / journalctl with **no** user input interpolated into the command.
No SQL executor is provided (deliberately omitted, §9/§14.1). No direct Discord
connection — messages go through the outbox for the bot to send.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, text as sa_text

from quart import Blueprint, jsonify, request

from db import (
    Announcement,
    AuditLog,
    DiscordOutbox,
    Drop,
    EventRateLimit,
    EventType,
    EventTypeTestGroup,
    Group,
    GroupAdmin,
    GroupConfiguration,
    GroupSubscription,
    ItemList,
    ItemValueOverride,
    Log,
    NotificationQueue,
    NpcList,
    Player,
    SubscriptionPayment,
    SubscriptionTier,
    User,
    UserConfiguration,
    UserSubscription,
)
from db.entitlements import (
    NITRO_PROVIDER,
    NON_REVENUE_PROVIDERS,
    effective_group_tiers,
    leg_monthly_cents,
    paid_group_tiers_desc,
    subscription_is_live,
)
from web_api import admin_registry as registry
from web_api import billing
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import assert_moderator, assert_superadmin, current_user_id, json_body, load_user
from utils import value_overrides
from web_api.entitlements_registry import (
    EntitlementValidationError,
    entitlements_to_storage,
    validate_entitlements_input,
)
from web_api.tier_flair import FlairValidationError, validate_flair
from web_api.routes.subscriptions import _serialize_group_sub, _serialize_sub, _serialize_user_sub

admin_bp = Blueprint("v1_admin", __name__)

# Whitelisted units the superadmin may see/control. Kinds drive the allowed
# actions and the frontend's grouping:
#   service — normal long-running unit: start/stop/restart
#   web     — traffic-serving Next.js colour: restart only, and only with
#             confirm:true (restarting the active colour drops the site;
#             deploys should go through droptracker-node instead)
#   deploy  — the blue-green deploy trigger (oneshot): restart only, issued
#             --no-block since a deploy takes ~2 min; watch its logs to follow
#   infra   — shared system service (nginx/MariaDB/Redis): status + logs only
# `confirm_stop` mirrors the old guard: stopping these interrupts intake or
# kills the backend serving this dashboard.
SERVICE_REGISTRY: list[dict] = [
    # --- Web & APIs -------------------------------------------------------
    {"unit": "droptracker-api", "name": "RuneLite intake API", "category": "Web & APIs",
     "description": "Receives plugin submissions (drops, PBs, clogs…)", "port": 31323,
     "kind": "service", "confirm_stop": True},
    {"unit": "droptracker-webapi", "name": "Web API (this backend)", "category": "Web & APIs",
     "description": "Serves /api/v1 for the website — including this dashboard", "port": 31325,
     "kind": "service", "confirm_stop": True},
    {"unit": "droptracker-node", "name": "Website deploy (blue-green)", "category": "Web & APIs",
     "description": "Restart = zero-downtime deploy: builds the idle colour, health-checks, flips nginx (~2 min)",
     "port": None, "kind": "deploy", "confirm_stop": False},
    {"unit": "droptracker-node-blue", "name": "Website front-end — blue", "category": "Web & APIs",
     "description": "Next.js instance; one colour serves live traffic", "port": 31380,
     "kind": "web", "confirm_stop": True},
    {"unit": "droptracker-node-green", "name": "Website front-end — green", "category": "Web & APIs",
     "description": "Next.js instance; one colour serves live traffic", "port": 31381,
     "kind": "web", "confirm_stop": True},
    # --- Discord bots -----------------------------------------------------
    {"unit": "droptracker-core", "name": "Discord bot (core)", "category": "Discord bots",
     "description": "Slash commands, notifications, lootboard posting", "port": None,
     "kind": "service", "confirm_stop": False},
    {"unit": "droptracker-webhooks", "name": "Webhook reader bot", "category": "Discord bots",
     "description": "Reads webhook-channel messages (legacy intake fallback)", "port": None,
     "kind": "service", "confirm_stop": False},
    {"unit": "droptracker-heartbeat", "name": "Heartbeat bot", "category": "Discord bots",
     "description": "Uptime heartbeat", "port": None,
     "kind": "service", "confirm_stop": False},
    {"unit": "droptracker-hof", "name": "Hall of Fame bot", "category": "Discord bots",
     "description": "Generates and posts Hall of Fame images", "port": None,
     "kind": "service", "confirm_stop": False},
    # --- Processing & workers --------------------------------------------
    {"unit": "droptracker-webhook-consumer", "name": "Intake queue consumer", "category": "Processing & workers",
     "description": "Drains webhook:queue (fast-accept intake processing)", "port": None,
     "kind": "service", "confirm_stop": True},
    {"unit": "droptracker-events", "name": "Events consumer", "category": "Processing & workers",
     "description": "Applies submissions to active event tasks / bingo / teams", "port": None,
     "kind": "service", "confirm_stop": False},
    {"unit": "droptracker-lootboards", "name": "Lootboard generator", "category": "Processing & workers",
     "description": "Regenerates group lootboard images on a rolling schedule", "port": None,
     "kind": "service", "confirm_stop": False},
    {"unit": "droptracker-player-updates", "name": "Player updater", "category": "Processing & workers",
     "description": "WOM sync + Redis leaderboard maintenance", "port": None,
     "kind": "service", "confirm_stop": False},
    {"unit": "droptracker-video-worker", "name": "Video worker", "category": "Processing & workers",
     "description": "MJPEG→MP4 conversion + Backblaze B2 upload", "port": None,
     "kind": "service", "confirm_stop": False},
    # --- Infrastructure (read-only) ---------------------------------------
    {"unit": "nginx", "name": "nginx", "category": "Infrastructure",
     "description": "Reverse proxy fronting every HTTP service (behind Cloudflare)", "port": 80,
     "kind": "infra", "confirm_stop": False},
    {"unit": "mariadb", "name": "MariaDB", "category": "Infrastructure",
     "description": "Primary database (data + xenforo schemas)", "port": 3306,
     "kind": "infra", "confirm_stop": False},
    {"unit": "redis-server", "name": "Redis", "category": "Infrastructure",
     "description": "Leaderboards, queues, realtime pub/sub", "port": 6379,
     "kind": "infra", "confirm_stop": False},
]
SERVICE_META = {s["unit"]: s for s in SERVICE_REGISTRY}
SERVICE_UNITS = set(SERVICE_META)
# Allowed control actions by kind (infra is status/logs only).
_KIND_ACTIONS = {
    "service": ("start", "stop", "restart"),
    "web": ("restart",),
    "deploy": ("restart",),
    "infra": (),
}
_MAX_LOG_LINES = 500
_DEFAULT_LOG_LINES = 200


async def _require_superadmin() -> int:
    user_id = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, user_id))

    await asyncio.to_thread(_check)
    return user_id


async def _require_moderator() -> int:
    """Moderator-or-superadmin gate for the shared moderation surfaces
    (pb-blocks, item values). Every mutation below is audit-logged with the
    actor, so superadmins can review moderator activity in /admin/audit."""
    user_id = current_user_id()

    def _check():
        with db_session() as s:
            assert_moderator(load_user(s, user_id))

    await asyncio.to_thread(_check)
    return user_id


def _audit(actor_user_id, action, target, before=None, after=None):
    try:
        with db_session() as s:
            s.add(AuditLog(
                actor_user_id=actor_user_id, group_id=None, action=action,
                target=target, before=before, after=after,
            ))
            s.commit()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Service management (systemctl, whitelisted)
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return 1, "", str(e)


def _service_status(unit: str) -> dict:
    meta = SERVICE_META.get(unit, {})
    code, out, _ = _run([
        "systemctl", "show", unit,
        "--property=ActiveState,SubState,ActiveEnterTimestamp,MainPID,"
        "MemoryCurrent,NRestarts,UnitFileState,Result",
    ])
    props: dict[str, str] = {}
    if code == 0 and out:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v

    active_state = props.get("ActiveState", "unknown")
    sub_state = props.get("SubState", "")
    since = None
    ts = props.get("ActiveEnterTimestamp", "")
    if ts:
        for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
            try:
                since = int(datetime.strptime(ts.strip(), fmt).timestamp())
                break
            except Exception:
                continue

    # MemoryCurrent is bytes, or "[not set]" for units without accounting.
    memory_mb = None
    try:
        memory_mb = round(int(props.get("MemoryCurrent", "")) / (1024 * 1024), 1)
    except (TypeError, ValueError):
        pass
    try:
        n_restarts = int(props.get("NRestarts", "0"))
    except ValueError:
        n_restarts = 0

    status_map = {
        "active": "running",
        "inactive": "stopped",
        "failed": "failed",
        "activating": "starting",
        "deactivating": "stopping",
        "reloading": "starting",
    }
    status = status_map.get(active_state, "unknown")

    kind = meta.get("kind", "service")
    return {
        "unit": unit,
        "name": meta.get("name", unit),
        "status": status,
        "active": active_state == "active",
        "since": since,
        # -- enriched fields (frontend renders richer rows from these) -----
        "description": meta.get("description"),
        "category": meta.get("category", "Services"),
        "kind": kind,
        "port": meta.get("port"),
        "sub_state": sub_state or None,
        "memory_mb": memory_mb,
        "n_restarts": n_restarts,
        "enabled": props.get("UnitFileState") in ("enabled", "static", "enabled-runtime"),
        # Oneshot deploy trigger: Result reflects the last deploy outcome.
        "last_result": props.get("Result") or None,
        "actions": list(_KIND_ACTIONS.get(kind, ())),
        "confirm_stop": bool(meta.get("confirm_stop")),
        # Restarting a traffic-serving colour directly can drop the site.
        "confirm_restart": kind == "web",
    }


@admin_bp.get("/admin/seasonal")
async def admin_get_seasonal():
    """Current state of the global seasonal-processing switch."""
    await _require_superadmin()
    from services.seasonal_state import is_seasonal_active

    active = await asyncio.to_thread(is_seasonal_active)
    return private_no_store(jsonify({"active": active}))


@admin_bp.post("/admin/seasonal")
async def admin_set_seasonal():
    """Toggle seasonal-world submission processing globally.

    Turned off between Leagues/Deadman seasons so the intake paths skip
    seasonal submissions entirely instead of running the seasonal processors.
    """
    actor = await _require_superadmin()
    body = await json_body()
    active = body.get("active")
    if not isinstance(active, bool):
        abort_problem(422, "Invalid value", "active must be a boolean.")

    from services.seasonal_state import set_seasonal_active

    try:
        await asyncio.to_thread(set_seasonal_active, active)
    except Exception:
        abort_problem(502, "Toggle failed", "Could not persist the seasonal switch.")
    _audit(actor, "seasonal.toggle", "global", after="on" if active else "off")
    return jsonify({"ok": True, "active": active})


# --------------------------------------------------------------------------- #
# Event types (game formats) — site-wide enable/disable + test-group allowlist.
# The durable analogue of the seasonal switch: rows in web_event_types gate
# which kinds non-superadmins may CREATE (services/event_types.py); existing
# events keep running regardless.
# --------------------------------------------------------------------------- #
def _event_type_row(s, t: EventType) -> dict:
    test_rows = (
        s.query(EventTypeTestGroup, Group.group_name)
        .outerjoin(Group, Group.group_id == EventTypeTestGroup.group_id)
        .filter(EventTypeTestGroup.type_key == t.key)
        .order_by(EventTypeTestGroup.created_at)
        .all()
    )
    return {
        "key": t.key,
        "label": t.label,
        "description": t.description or None,
        "enabled": bool(t.enabled),
        "admin_only": bool(t.admin_only),
        "sort": int(t.sort or 0),
        "test_groups": [
            {"group_id": tg.group_id, "group_name": name or f"Group {tg.group_id}"}
            for (tg, name) in test_rows
        ],
    }


@admin_bp.get("/admin/event-types")
async def admin_list_event_types():
    await _require_superadmin()

    def _read():
        with db_session() as s:
            rows = s.query(EventType).order_by(EventType.sort, EventType.key).all()
            return [_event_type_row(s, t) for t in rows]

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


@admin_bp.patch("/admin/event-types/<key>")
async def admin_patch_event_type(key: str):
    actor = await _require_superadmin()
    body = await json_body()
    for field in ("enabled", "admin_only"):
        if field in body and not isinstance(body[field], bool):
            abort_problem(422, "Invalid value", f"'{field}' must be a boolean.")

    def _apply():
        with db_session() as s:
            t = s.query(EventType).filter(EventType.key == key).first()
            if not t:
                abort_problem(404, "Unknown event type", f"No event type '{key}'.")
            before = f"enabled={int(t.enabled)},admin_only={int(t.admin_only)}"
            if "enabled" in body:
                t.enabled = body["enabled"]
            if "admin_only" in body:
                t.admin_only = body["admin_only"]
            s.commit()
            after = f"enabled={int(t.enabled)},admin_only={int(t.admin_only)}"
            row = _event_type_row(s, t)
            return before, after, row

    before, after, row = await asyncio.to_thread(_apply)
    from services.event_types import invalidate_cache

    invalidate_cache()
    _audit(actor, "event_type.toggle", key, before=before, after=after)
    return jsonify(row)


@admin_bp.post("/admin/event-types/<key>/test-groups")
async def admin_add_event_type_test_group(key: str):
    actor = await _require_superadmin()
    body = await json_body()
    group_id = body.get("group_id")
    if not isinstance(group_id, int) or isinstance(group_id, bool):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer.")

    def _apply():
        with db_session() as s:
            t = s.query(EventType).filter(EventType.key == key).first()
            if not t:
                abort_problem(404, "Unknown event type", f"No event type '{key}'.")
            if not s.query(Group.group_id).filter(Group.group_id == group_id).first():
                abort_problem(404, "Unknown group", f"No group {group_id}.")
            exists = (
                s.query(EventTypeTestGroup.id)
                .filter(
                    EventTypeTestGroup.type_key == key,
                    EventTypeTestGroup.group_id == group_id,
                )
                .first()
            )
            if not exists:
                s.add(EventTypeTestGroup(
                    type_key=key, group_id=group_id, added_by_user_id=actor,
                ))
                s.commit()
            return _event_type_row(s, t)

    row = await asyncio.to_thread(_apply)
    from services.event_types import invalidate_cache

    invalidate_cache()
    _audit(actor, "event_type.test_group.add", key, after=str(group_id))
    return jsonify(row)


@admin_bp.delete("/admin/event-types/<key>/test-groups/<int:group_id>")
async def admin_remove_event_type_test_group(key: str, group_id: int):
    actor = await _require_superadmin()

    def _apply():
        with db_session() as s:
            t = s.query(EventType).filter(EventType.key == key).first()
            if not t:
                abort_problem(404, "Unknown event type", f"No event type '{key}'.")
            s.query(EventTypeTestGroup).filter(
                EventTypeTestGroup.type_key == key,
                EventTypeTestGroup.group_id == group_id,
            ).delete(synchronize_session=False)
            s.commit()
            return _event_type_row(s, t)

    row = await asyncio.to_thread(_apply)
    from services.event_types import invalidate_cache

    invalidate_cache()
    _audit(actor, "event_type.test_group.remove", key, before=str(group_id))
    return jsonify(row)


# --------------------------------------------------------------------------- #
# Event rate limits (web65a) — per-tier event frequency caps. One rule per
# (tier, event kind) — or the "*" all-kinds sentinel — capping how many events
# a group on that tier may activate per rolling window. No rules = unlimited
# (the events entitlement alone gates access); a >0 rule also grants
# rate-limited event access to tiers without the entitlement. Enforced by
# db/event_rate_limits.py at activation.
# --------------------------------------------------------------------------- #
def _rate_limit_row(row: EventRateLimit) -> dict:
    return {
        "id": int(row.id),
        "tier_key": row.tier_key,
        "type_key": row.type_key,
        "max_events": int(row.max_events),
        "window_days": int(row.window_days),
        "enabled": bool(row.enabled),
    }


@admin_bp.get("/admin/event-rate-limits")
async def admin_list_event_rate_limits():
    await _require_superadmin()

    def _read():
        with db_session() as s:
            rows = (
                s.query(EventRateLimit)
                .order_by(EventRateLimit.tier_key, EventRateLimit.type_key)
                .all()
            )
            return [_rate_limit_row(r) for r in rows]

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


@admin_bp.put("/admin/event-rate-limits")
async def admin_put_event_rate_limit():
    """Upsert one rule, keyed by (tier_key, type_key)."""
    from db.event_rate_limits import (
        ALL_TYPES,
        MAX_EVENTS_CEILING,
        WINDOW_DAYS_CEILING,
        invalidate_cache,
    )

    actor = await _require_superadmin()
    body = await json_body()
    tier_key = body.get("tier_key")
    type_key = body.get("type_key") or ALL_TYPES
    max_events = body.get("max_events")
    window_days = body.get("window_days")
    enabled = body.get("enabled", True)
    if not isinstance(tier_key, str) or not tier_key:
        abort_problem(422, "Invalid tier", "'tier_key' is required.")
    if not isinstance(type_key, str) or not type_key:
        abort_problem(422, "Invalid event type", "'type_key' must be a string.")
    if (not isinstance(max_events, int) or isinstance(max_events, bool)
            or not (0 <= max_events <= MAX_EVENTS_CEILING)):
        abort_problem(
            422, "Invalid limit",
            f"'max_events' must be an integer between 0 and {MAX_EVENTS_CEILING}.",
        )
    if (not isinstance(window_days, int) or isinstance(window_days, bool)
            or not (1 <= window_days <= WINDOW_DAYS_CEILING)):
        abort_problem(
            422, "Invalid window",
            f"'window_days' must be an integer between 1 and {WINDOW_DAYS_CEILING}.",
        )
    if not isinstance(enabled, bool):
        abort_problem(422, "Invalid value", "'enabled' must be a boolean.")

    def _apply():
        with db_session() as s:
            tier = (
                s.query(SubscriptionTier)
                .filter(SubscriptionTier.key == tier_key)
                .first()
            )
            if tier is None:
                abort_problem(404, "Unknown tier", f"No subscription tier '{tier_key}'.")
            if tier.scope != "group":
                abort_problem(
                    422, "Invalid tier",
                    "Event rate limits apply to group tiers only.",
                )
            if type_key != ALL_TYPES and not (
                s.query(EventType.key).filter(EventType.key == type_key).first()
            ):
                abort_problem(404, "Unknown event type", f"No event type '{type_key}'.")
            row = (
                s.query(EventRateLimit)
                .filter(
                    EventRateLimit.tier_key == tier_key,
                    EventRateLimit.type_key == type_key,
                )
                .first()
            )
            before = (
                f"max={row.max_events},days={row.window_days},on={int(row.enabled)}"
                if row is not None else None
            )
            if row is None:
                row = EventRateLimit(tier_key=tier_key, type_key=type_key,
                                     max_events=max_events, window_days=window_days,
                                     enabled=enabled)
                s.add(row)
            else:
                row.max_events = max_events
                row.window_days = window_days
                row.enabled = enabled
            s.commit()
            return before, _rate_limit_row(row)

    before, row = await asyncio.to_thread(_apply)
    invalidate_cache()
    _audit(
        actor, "event_rate_limit.set", f"{tier_key}:{type_key}",
        before=before,
        after=f"max={max_events},days={window_days},on={int(enabled)}",
    )
    return jsonify(row)


@admin_bp.delete("/admin/event-rate-limits/<int:limit_id>")
async def admin_delete_event_rate_limit(limit_id: int):
    from db.event_rate_limits import invalidate_cache

    actor = await _require_superadmin()

    def _apply():
        with db_session() as s:
            row = (
                s.query(EventRateLimit)
                .filter(EventRateLimit.id == limit_id)
                .first()
            )
            if row is None:
                abort_problem(404, "Unknown rule", f"No event rate limit #{limit_id}.")
            target = f"{row.tier_key}:{row.type_key}"
            before = f"max={row.max_events},days={row.window_days},on={int(row.enabled)}"
            s.delete(row)
            s.commit()
            return target, before

    target, before = await asyncio.to_thread(_apply)
    invalidate_cache()
    _audit(actor, "event_rate_limit.delete", target, before=before)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Board-game shop catalog (web45a) — the site-wide power-up product table.
# --------------------------------------------------------------------------- #
def _shop_item_row(item) -> dict:
    return {
        "id": item.id,
        "key": item.key,
        "name": item.name,
        "description": item.description or None,
        "icon_item_id": item.icon_item_id,
        "item_type": item.item_type,
        "effect": item.effect,
        "effect_config": item.effect_config or None,
        "cost_coins": int(item.cost_coins or 0),
        "type_cooldown_turns": int(item.type_cooldown_turns or 0),
        "sort": int(item.sort or 0),
        "active": bool(item.active),
    }


@admin_bp.get("/admin/boardgame-shop")
async def admin_list_shop_items():
    await _require_superadmin()

    def _read():
        from db.models import BoardgameShopItem

        with db_session() as s:
            rows = (s.query(BoardgameShopItem)
                    .order_by(BoardgameShopItem.sort, BoardgameShopItem.id).all())
            return [_shop_item_row(i) for i in rows]

    return private_no_store(jsonify(await asyncio.to_thread(_read)))


@admin_bp.patch("/admin/boardgame-shop/<int:item_id>")
async def admin_patch_shop_item(item_id: int):
    """Edit a catalog row: pricing, cooldown, icon, copy, active flag.
    ``key``/``effect`` are code-level identity and stay immutable here."""
    actor = await _require_superadmin()
    body = await json_body()

    def _apply():
        from db.models import BOARDGAME_ITEM_TYPES, BoardgameShopItem

        with db_session() as s:
            item = (s.query(BoardgameShopItem)
                    .filter(BoardgameShopItem.id == item_id).first())
            if not item:
                abort_problem(404, "Item not found", f"No shop item {item_id}.")
            if "name" in body:
                name = (body.get("name") or "").strip()
                if not (1 <= len(name) <= 80):
                    abort_problem(422, "Invalid name", "Name must be 1–80 characters.")
                item.name = name
            if "description" in body:
                item.description = (body.get("description") or None)
            if "icon_item_id" in body:
                icon = body.get("icon_item_id")
                if icon is not None and (not isinstance(icon, int) or isinstance(icon, bool)
                                         or icon <= 0):
                    abort_problem(422, "Invalid icon",
                                  "'icon_item_id' must be a positive item id or null.")
                item.icon_item_id = icon
            if "item_type" in body:
                if body["item_type"] not in BOARDGAME_ITEM_TYPES:
                    abort_problem(422, "Invalid type",
                                  f"item_type must be one of {list(BOARDGAME_ITEM_TYPES)}.")
                item.item_type = body["item_type"]
            for field, lo, hi in (("cost_coins", 0, 1_000_000),
                                  ("type_cooldown_turns", 0, 100),
                                  ("sort", 0, 10_000)):
                if field in body:
                    v = body.get(field)
                    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                        abort_problem(422, "Invalid value",
                                      f"'{field}' must be an integer {lo}–{hi}.")
                    setattr(item, field, v)
            if "effect_config" in body:
                cfg = body.get("effect_config")
                if cfg is not None:
                    if not isinstance(cfg, str) or len(cfg) > 2000:
                        abort_problem(422, "Invalid config",
                                      "'effect_config' must be a JSON string ≤2000 chars.")
                    try:
                        json.loads(cfg)
                    except ValueError:
                        abort_problem(422, "Invalid config",
                                      "'effect_config' must be valid JSON.")
                item.effect_config = cfg
            if "active" in body:
                if not isinstance(body["active"], bool):
                    abort_problem(422, "Invalid value", "'active' must be a boolean.")
                item.active = body["active"]
            s.commit()
            return _shop_item_row(item)

    row = await asyncio.to_thread(_apply)
    _audit(actor, "boardgame_shop.update", str(item_id),
           after=f"active={row['active']},cost={row['cost_coins']}")
    return jsonify(row)


@admin_bp.get("/admin/services")
async def admin_services():
    await _require_superadmin()
    # Registry order, not alphabetical — the frontend groups by category and
    # the registry encodes the intended layout.
    statuses = await asyncio.to_thread(
        lambda: [_service_status(s["unit"]) for s in SERVICE_REGISTRY]
    )
    return private_no_store(jsonify(statuses))


@admin_bp.post("/admin/services/<unit>")
async def admin_service_action(unit: str):
    actor = await _require_superadmin()
    meta = SERVICE_META.get(unit)
    if meta is None:
        abort_problem(404, "Unknown unit", "That service is not managed.")
    body = await json_body()
    action = body.get("action")
    if action not in ("start", "stop", "restart"):
        abort_problem(422, "Invalid action", "action must be start|stop|restart.")
    if action not in _KIND_ACTIONS.get(meta["kind"], ()):
        abort_problem(
            403, "Action not allowed",
            "Infrastructure services are read-only here." if meta["kind"] == "infra"
            else f"'{action}' is not available for this unit.",
        )

    # Guards. Stopping intake halts submissions; stopping the web API kills the
    # backend serving this dashboard; restarting a serving colour can drop the
    # live site (deploys should go through droptracker-node).
    if action == "stop" and meta.get("confirm_stop") and not body.get("confirm"):
        abort_problem(409, "Confirmation required", "Stopping this service requires confirm:true.")
    if action == "restart" and meta["kind"] == "web" and not body.get("confirm"):
        abort_problem(
            409, "Confirmation required",
            "Restarting a serving colour directly can take the site down — deploys go "
            "through the 'Website deploy' unit. Pass confirm:true to proceed anyway.",
        )

    # --no-block where waiting is wrong: the deploy trigger runs ~2 min (poll
    # status / read its logs to follow), and a webapi restart would otherwise
    # kill the very worker handling this request before it could respond.
    no_block = unit == "droptracker-webapi" or meta["kind"] == "deploy"
    cmd_action = [action, "--no-block", unit] if no_block else [action, unit]

    def _do():
        # Try direct systemctl, then sudo -n (non-interactive) as fallback.
        code, _out, err = _run(["systemctl", *cmd_action], timeout=30)
        if code != 0:
            code, _out, err = _run(["sudo", "-n", "systemctl", *cmd_action], timeout=30)
        return code, err

    code, err = await asyncio.to_thread(_do)
    _audit(actor, f"service.{action}", unit, after="ok" if code == 0 else err[:200])
    if code != 0:
        abort_problem(502, "Service action failed", err[:200] or "systemctl error")
    return jsonify({"ok": True, "queued": no_block})


@admin_bp.get("/admin/services/<unit>/logs")
async def admin_service_logs(unit: str):
    await _require_superadmin()
    if unit not in SERVICE_UNITS:
        abort_problem(404, "Unknown unit", "That service is not managed.")
    try:
        n_lines = int(request.args.get("lines", _DEFAULT_LOG_LINES))
    except ValueError:
        n_lines = _DEFAULT_LOG_LINES
    n_lines = max(10, min(n_lines, _MAX_LOG_LINES))

    def _logs():
        code, out, err = _run(
            ["journalctl", "-u", unit, "-n", str(n_lines), "--no-pager"], timeout=15
        )
        if code != 0:
            code, out, err = _run(
                ["sudo", "-n", "journalctl", "-u", unit, "-n", str(n_lines), "--no-pager"],
                timeout=15,
            )
        text = out if code == 0 else (err or "logs unavailable")
        return [ln for ln in text.splitlines()][-n_lines:]

    lines = await asyncio.to_thread(_logs)
    return private_no_store(jsonify({"unit": unit, "lines": lines}))


# --------------------------------------------------------------------------- #
# Database backups (droptracker-db-backup.timer → scripts/db_backup.sh)
# --------------------------------------------------------------------------- #
BACKUP_UNIT = "droptracker-db-backup"
BACKUP_ROOT = Path("/store/droptracker/backups")
BACKUP_B2_PREFIX = "dt_backups/"
_BACKUP_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_B2_DATE_IN_KEY = re.compile(r"/(\d{4}-\d{2}-\d{2})/")
# Required artifacts per nightly set; the Redis snapshot is best-effort and a
# set without it still counts as complete (leaderboards are rebuildable).
_BACKUP_REQUIRED = ("data-{d}.sql.gz", "data-schema-{d}.sql.gz", "xenforo-{d}.sql.gz")
# Mirror the retention defaults in scripts/db_backup.sh.
_BACKUP_LOCAL_RETENTION_DAYS = 7
_BACKUP_REMOTE_RETENTION_DAYS = 30


def _systemd_show(unit: str, props: list[str]) -> dict:
    code, out, _ = _run(["systemctl", "show", unit, "--property=" + ",".join(props)])
    parsed: dict[str, str] = {}
    if code == 0:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                parsed[k] = v.strip()
    return parsed


def _systemd_ts(value: str | None) -> int | None:
    """Parse systemctl's human timestamps ('Sun 2026-07-13 08:36:13 UTC')."""
    if not value or value in ("n/a", "0"):
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(value.strip(), fmt).timestamp())
        except ValueError:
            continue
    return None


def _backup_running(svc_props: dict) -> bool:
    # The backup service is Type=oneshot: it reports "activating" for the
    # whole run and never a steady "active".
    return svc_props.get("ActiveState") in ("activating", "active", "deactivating")


def _backup_overview() -> dict:
    svc = _systemd_show(f"{BACKUP_UNIT}.service", [
        "ActiveState", "Result", "ExecMainStatus",
        "ExecMainStartTimestamp", "ExecMainExitTimestamp",
    ])
    timer = _systemd_show(f"{BACKUP_UNIT}.timer", [
        "ActiveState", "UnitFileState", "NextElapseUSecRealtime", "LastTriggerUSec",
    ])

    running = _backup_running(svc)
    started = _systemd_ts(svc.get("ExecMainStartTimestamp"))
    finished = None if running else _systemd_ts(svc.get("ExecMainExitTimestamp"))
    result = svc.get("Result") or "unknown"
    exit_status = svc.get("ExecMainStatus") or ""
    last_run = None
    if started:
        last_run = {
            "started": started,
            "finished": finished,
            "duration_seconds": (finished - started) if (finished and finished >= started) else None,
            "success": (not running) and result == "success",
            "result": "running" if running else result,
            "exit_status": int(exit_status) if exit_status.lstrip("-").isdigit() else None,
        }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sets = []
    if BACKUP_ROOT.is_dir():
        for d in sorted(BACKUP_ROOT.iterdir(), key=lambda p: p.name, reverse=True):
            if not (d.is_dir() and _BACKUP_DATE_RE.match(d.name)):
                continue
            files = []
            for f in sorted(d.iterdir()):
                if f.is_file():
                    st = f.stat()
                    files.append({"name": f.name, "size": st.st_size, "modified": int(st.st_mtime)})
            required = {t.format(d=d.name) for t in _BACKUP_REQUIRED}
            present = {f["name"] for f in files if f["size"] > 0}
            if required <= present:
                status = "complete"
            elif running and d.name == today:
                status = "in_progress"
            else:
                status = "incomplete"
            sets.append({
                "date": d.name,
                "status": status,
                "total_bytes": sum(f["size"] for f in files),
                "files": files,
            })

    du = shutil.disk_usage(BACKUP_ROOT if BACKUP_ROOT.is_dir() else BACKUP_ROOT.parent)

    return {
        "unit": BACKUP_UNIT,
        "running": running,
        "timer": {
            "enabled": timer.get("UnitFileState") == "enabled",
            "active": timer.get("ActiveState") == "active",
            "next_run": _systemd_ts(timer.get("NextElapseUSecRealtime")),
            "last_trigger": _systemd_ts(timer.get("LastTriggerUSec")),
        },
        "last_run": last_run,
        "sets": sets,
        "disk": {"free_bytes": du.free, "total_bytes": du.total},
        "retention": {
            "local_days": _BACKUP_LOCAL_RETENTION_DAYS,
            "remote_days": _BACKUP_REMOTE_RETENTION_DAYS,
        },
    }


@admin_bp.get("/admin/backups")
async def admin_backups():
    await _require_superadmin()
    overview = await asyncio.to_thread(_backup_overview)
    return private_no_store(jsonify(overview))


@admin_bp.get("/admin/backups/logs")
async def admin_backup_logs():
    await _require_superadmin()
    unit = f"{BACKUP_UNIT}.service"

    def _logs():
        # short-iso: the job runs nightly, so lines need full dates.
        cmd = ["journalctl", "-u", unit, "-n", str(_MAX_LOG_LINES), "--no-pager", "-o", "short-iso"]
        code, out, err = _run(cmd, timeout=15)
        if code != 0:
            code, out, err = _run(["sudo", "-n", *cmd], timeout=15)
        text = out if code == 0 else (err or "logs unavailable")
        return text.splitlines()[-_MAX_LOG_LINES:]

    lines = await asyncio.to_thread(_logs)
    return private_no_store(jsonify({"unit": BACKUP_UNIT, "lines": lines}))


@admin_bp.get("/admin/backups/offsite")
async def admin_backup_offsite():
    """List the offsite (B2) copies under dt_backups/, grouped by date."""
    await _require_superadmin()

    def _list():
        from utils.b2_storage import B2_BUCKET_NAME, _get_s3_client

        client = _get_s3_client()
        days: dict[str, dict] = {}
        total = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=B2_BUCKET_NAME, Prefix=BACKUP_B2_PREFIX):
            for obj in page.get("Contents", []):
                m = _B2_DATE_IN_KEY.search(obj["Key"])
                day = days.setdefault(m.group(1) if m else "other", {
                    "date": m.group(1) if m else "other",
                    "objects": 0, "total_bytes": 0, "files": [],
                })
                day["objects"] += 1
                day["total_bytes"] += obj["Size"]
                day["files"].append({
                    "name": obj["Key"].rsplit("/", 1)[-1],
                    "size": obj["Size"],
                    "modified": int(obj["LastModified"].timestamp()),
                })
                total += obj["Size"]
        return {
            "bucket": B2_BUCKET_NAME,
            "prefix": BACKUP_B2_PREFIX,
            "total_bytes": total,
            "days": sorted(days.values(), key=lambda d: d["date"], reverse=True),
        }

    try:
        data = await asyncio.to_thread(_list)
    except Exception as e:
        abort_problem(502, "Offsite check failed", str(e)[:200])
    return private_no_store(jsonify(data))


@admin_bp.post("/admin/backups/run")
async def admin_backup_run():
    """Kick off a manual backup run (same unit the nightly timer starts)."""
    actor = await _require_superadmin()

    svc = await asyncio.to_thread(
        _systemd_show, f"{BACKUP_UNIT}.service", ["ActiveState"]
    )
    if _backup_running(svc):
        abort_problem(409, "Backup already running", "Wait for the current run to finish.")

    def _start():
        # --no-block: a full run takes ~20 min; don't hold the request open.
        cmd = ["systemctl", "start", "--no-block", f"{BACKUP_UNIT}.service"]
        code, _out, err = _run(cmd, timeout=20)
        if code != 0:
            code, _out, err = _run(["sudo", "-n", *cmd], timeout=20)
        return code, err

    code, err = await asyncio.to_thread(_start)
    _audit(actor, "backup.run", BACKUP_UNIT, after="ok" if code == 0 else err[:200])
    if code != 0:
        abort_problem(502, "Could not start backup", err[:200] or "systemctl error")
    return jsonify({"ok": True})


# Backblaze B2 pricing (https://www.backblaze.com/cloud-storage/pricing):
# $6/TB/month storage after a 10 GB free tier; egress is free up to 3x the
# monthly average bytes stored, then $0.01/GB. Bandwidth actually used is NOT
# exposed by B2's public API (console-only), so the estimate covers storage.
_B2_STORAGE_USD_PER_GB_MONTH = 0.006
_B2_FREE_STORAGE_GB = 10
_B2_LARGEST_COUNT = 10


@admin_bp.get("/admin/b2/usage")
async def admin_b2_usage():
    """Bucket-wide B2 storage usage (everything under the key's dt_ namePrefix),
    grouped by top-level prefix, with a monthly storage-cost estimate."""
    await _require_superadmin()

    def _scan():
        import heapq

        from utils.b2_storage import B2_BUCKET_NAME, _get_s3_client

        client = _get_s3_client()
        prefixes: dict[str, dict] = {}
        heap: list[tuple] = []  # (size, key, modified) — top N largest objects
        objects = 0
        total = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=B2_BUCKET_NAME, Prefix="dt_"):
            for obj in page.get("Contents", []):
                objects += 1
                total += obj["Size"]
                top = obj["Key"].split("/", 1)[0]
                p = prefixes.setdefault(top, {"prefix": top, "objects": 0, "total_bytes": 0})
                p["objects"] += 1
                p["total_bytes"] += obj["Size"]
                item = (obj["Size"], obj["Key"], int(obj["LastModified"].timestamp()))
                if len(heap) < _B2_LARGEST_COUNT:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)

        billable_gb = max(0.0, total / 1_000_000_000 - _B2_FREE_STORAGE_GB)
        return {
            "bucket": B2_BUCKET_NAME,
            "generated_at": int(datetime.now(timezone.utc).timestamp()),
            "objects": objects,
            "total_bytes": total,
            "prefixes": sorted(prefixes.values(), key=lambda p: -p["total_bytes"]),
            "largest": [
                {"key": k, "size": s, "modified": m}
                for s, k, m in sorted(heap, reverse=True)
            ],
            "estimate": {
                "storage_rate_usd_per_gb_month": _B2_STORAGE_USD_PER_GB_MONTH,
                "free_storage_bytes": _B2_FREE_STORAGE_GB * 1_000_000_000,
                "storage_usd_per_month": round(billable_gb * _B2_STORAGE_USD_PER_GB_MONTH, 2),
                "free_egress_bytes_per_month": total * 3,
            },
        }

    try:
        data = await asyncio.to_thread(_scan)
    except Exception as e:
        abort_problem(502, "B2 usage scan failed", str(e)[:200])
    return private_no_store(jsonify(data))


# --------------------------------------------------------------------------- #
# Discord message sender (via outbox — no direct connection)
# --------------------------------------------------------------------------- #
@admin_bp.post("/admin/discord/send")
async def admin_discord_send():
    actor = await _require_superadmin()
    body = await json_body()
    channel_id = str(body.get("channel_id") or "").strip()
    content = (body.get("content") or "").strip()
    if not channel_id:
        abort_problem(422, "Missing channel_id", "'channel_id' is required.")
    if not (1 <= len(content) <= 2000):
        abort_problem(422, "Invalid content", "content must be 1–2000 characters.")

    def _enqueue():
        with db_session() as s:
            from services.discord_outbox import enqueue

            enqueue(
                s, channel_id=channel_id, content=content, kind="message",
                actor_user_id=actor,
            )

    await asyncio.to_thread(_enqueue)
    _audit(actor, "discord.send", f"channel:{channel_id}", after=content[:200])
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Cross-content lookup
# --------------------------------------------------------------------------- #
@admin_bp.get("/admin/lookup")
async def admin_lookup():
    await _require_superadmin()
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})

    def _search():
        results = []
        like = f"%{q}%"
        with db_session() as s:
            for pid, name in (
                s.query(Player.player_id, Player.player_name)
                .filter(Player.player_name.ilike(like)).limit(10).all()
            ):
                results.append({
                    "category": "player", "id": str(pid), "label": name,
                    "href": f"/players/{pid}",
                })
            for gid, name in (
                s.query(Group.group_id, Group.group_name)
                .filter(Group.group_name.ilike(like), Group.group_id > 2).limit(10).all()
            ):
                results.append({
                    "category": "group", "id": str(gid), "label": name,
                    "href": f"/groups/{gid}",
                })
            for iid, name in (
                s.query(ItemList.item_id, ItemList.item_name)
                .filter(ItemList.item_name.ilike(like)).limit(10).all()
            ):
                results.append({"category": "item", "id": str(iid), "label": name})
            for nid, name in (
                s.query(NpcList.npc_id, NpcList.npc_name)
                .filter(NpcList.npc_name.ilike(like)).limit(10).all()
            ):
                results.append({"category": "npc", "id": str(nid), "label": name})
            # Numeric query: also resolve ids directly.
            if q.isdigit():
                p = s.query(Player).filter(Player.player_id == int(q)).first()
                if p:
                    results.append({
                        "category": "player", "id": str(p.player_id),
                        "label": p.player_name, "href": f"/players/{p.player_id}",
                    })
        return results

    results = await asyncio.to_thread(_search)
    return private_no_store(jsonify({"results": results}))


# --------------------------------------------------------------------------- #
# Subscription tier management (CRUD)
# --------------------------------------------------------------------------- #
def _tier_from_body(body: dict, existing: SubscriptionTier | None = None):
    key = body.get("key") or (existing.key if existing else None)
    if not key:
        abort_problem(422, "Missing key", "Tier 'key' is required.")
    # Scope is set at creation and immutable after — entitlement keys differ
    # per scope, and flipping it would strand subscriber rows.
    scope = (existing.scope if existing else body.get("scope")) or "group"
    if scope not in ("group", "user"):
        abort_problem(422, "Invalid scope", "Tier 'scope' must be 'group' or 'user'.")
    features = body.get("features")
    features_json = json.dumps(features) if isinstance(features, list) else (
        existing.features if existing else "[]"
    )
    entitlements_json = None
    if "entitlements" in body:
        try:
            validated = validate_entitlements_input(body.get("entitlements") or {}, scope)
            entitlements_json = entitlements_to_storage(validated)
        except EntitlementValidationError as e:
            abort_problem(422, "Invalid entitlements", e.detail)
    elif existing is not None:
        entitlements_json = existing.entitlements
    try:
        flair = validate_flair(
            body.get("flair") if "flair" in body else (existing.flair if existing else None)
        )
    except FlairValidationError as e:
        abort_problem(422, "Invalid flair", e.detail)
    return key, scope, features_json, entitlements_json, flair


@admin_bp.post("/admin/subscriptions/tiers")
async def create_tier():
    actor = await _require_superadmin()
    body = await json_body()

    def _apply():
        with db_session() as s:
            key, scope, features_json, entitlements_json, flair = _tier_from_body(body)
            if s.query(SubscriptionTier).filter(SubscriptionTier.key == key).first():
                abort_problem(409, "Tier exists", f"Tier '{key}' already exists.")
            tier = SubscriptionTier(
                key=key,
                name=body.get("name") or key,
                description=body.get("description"),
                scope=scope,
                price_cents=int(body.get("price_cents") or 0),
                currency=body.get("currency") or "USD",
                interval=body.get("interval") or "month",
                features=features_json,
                entitlements=entitlements_json,
                flair=flair,
                recommended=bool(body.get("recommended", False)),
                active=bool(body.get("active", True)),
            )
            # Ensure a provider price exists (best-effort).
            try:
                tier.provider_price_id = billing.ensure_provider_price(tier)
            except Exception:
                pass
            s.add(tier)
            s.commit()

    await asyncio.to_thread(_apply)
    _audit(actor, "tier.create", f"tier:{body.get('key')}")
    return jsonify({"ok": True})


@admin_bp.patch("/admin/subscriptions/tiers/<key>")
async def update_tier(key: str):
    actor = await _require_superadmin()
    body = await json_body()

    def _apply():
        with db_session() as s:
            tier = s.query(SubscriptionTier).filter(SubscriptionTier.key == key).first()
            if not tier:
                abort_problem(404, "Tier not found", f"No tier '{key}'.")
            if "name" in body:
                tier.name = body["name"]
            if "description" in body:
                tier.description = body["description"]
            if "price_cents" in body:
                tier.price_cents = int(body["price_cents"])
            if "currency" in body:
                tier.currency = body["currency"]
            if "interval" in body:
                tier.interval = body["interval"]
            if "features" in body and isinstance(body["features"], list):
                tier.features = json.dumps(body["features"])
            if "entitlements" in body:
                try:
                    tier_scope = getattr(tier, "scope", None) or "group"
                    validated = validate_entitlements_input(body.get("entitlements") or {}, tier_scope)
                    tier.entitlements = entitlements_to_storage(validated)
                except EntitlementValidationError as e:
                    abort_problem(422, "Invalid entitlements", e.detail)
            if "flair" in body:
                try:
                    tier.flair = validate_flair(body.get("flair"))
                except FlairValidationError as e:
                    abort_problem(422, "Invalid flair", e.detail)
            if "recommended" in body:
                tier.recommended = bool(body["recommended"])
            if "active" in body:
                tier.active = bool(body["active"])
            s.commit()

    await asyncio.to_thread(_apply)
    _audit(actor, "tier.update", f"tier:{key}")
    return jsonify({"ok": True})


@admin_bp.delete("/admin/subscriptions/tiers/<key>")
async def delete_tier(key: str):
    actor = await _require_superadmin()

    def _apply():
        with db_session() as s:
            tier = s.query(SubscriptionTier).filter(SubscriptionTier.key == key).first()
            if not tier:
                abort_problem(404, "Tier not found", f"No tier '{key}'.")
            # Don't hard-delete a tier with active subscribers — soft-disable.
            has_subs = (
                s.query(GroupSubscription)
                .filter(
                    GroupSubscription.tier_key == key,
                    GroupSubscription.status.in_(["active", "trialing", "past_due"]),
                )
                .first()
            ) or (
                s.query(UserSubscription)
                .filter(
                    UserSubscription.tier_key == key,
                    UserSubscription.status.in_(["active", "trialing", "past_due"]),
                )
                .first()
            )
            if has_subs:
                tier.active = False
                s.commit()
                return "soft"
            s.delete(tier)
            s.commit()
            return "hard"

    mode = await asyncio.to_thread(_apply)
    _audit(actor, "tier.delete", f"tier:{key}", after=mode)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Item value overrides — runtime-editable "component of X, worth Y" rules.
# Replaces the hard-coded special-cases in utils/ge_value.py. Every write evicts
# the shared cache (value_overrides.invalidate) so live services pick up the
# change without a restart.
# --------------------------------------------------------------------------- #
def _override_to_dict(row) -> dict:
    try:
        components = json.loads(row.components) if row.components else []
    except (ValueError, TypeError):
        components = []
    return {
        "id": row.id,
        "item_id": row.item_id,
        "item_name": row.item_name,
        "divisor": row.divisor,
        "flat_bonus": row.flat_bonus,
        "fallback_value": row.fallback_value,
        "components": components,
        "description": row.description,
        "active": bool(row.active),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _validate_components(raw) -> list:
    if not isinstance(raw, list):
        abort_problem(422, "Invalid components", "components must be a list.")
    out = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            abort_problem(422, "Invalid component", f"Component {i + 1} must be an object.")
        name = str(c.get("item_name") or "").strip()
        if not name:
            abort_problem(422, "Invalid component", f"Component {i + 1} requires an item name.")
        try:
            qty = int(c.get("quantity"))
        except (TypeError, ValueError):
            abort_problem(422, "Invalid component", f"Component {i + 1} quantity must be an integer.")
        if qty == 0:
            abort_problem(422, "Invalid component", f"Component {i + 1} quantity must be non-zero.")
        cid = None
        if c.get("item_id") not in (None, ""):
            try:
                cid = int(c["item_id"])
            except (TypeError, ValueError):
                abort_problem(422, "Invalid component", f"Component {i + 1} item_id must be an integer.")
        out.append({"item_id": cid, "item_name": name[:125], "quantity": qty})
    return out


def _validate_override_body(body: dict, *, require_item: bool) -> dict:
    """Validate + normalize an override payload. On create (require_item) every
    column gets a value; on PATCH only the provided keys are returned."""
    out: dict = {}
    if "item_id" in body and body["item_id"] not in (None, ""):
        try:
            out["item_id"] = int(body["item_id"])
        except (TypeError, ValueError):
            abort_problem(422, "Invalid item_id", "item_id must be an integer.")
    elif require_item:
        out["item_id"] = None
    if require_item or "item_name" in body:
        name = str(body.get("item_name") or "").strip()
        if not name:
            abort_problem(422, "Missing item_name", "item_name is required.")
        out["item_name"] = name[:125]
    if require_item or "divisor" in body:
        try:
            divisor = int(body.get("divisor", 1))
        except (TypeError, ValueError):
            abort_problem(422, "Invalid divisor", "divisor must be an integer.")
        if divisor < 1:
            abort_problem(422, "Invalid divisor", "divisor must be ≥ 1.")
        out["divisor"] = divisor
    if "flat_bonus" in body:
        try:
            out["flat_bonus"] = int(body["flat_bonus"])
        except (TypeError, ValueError):
            abort_problem(422, "Invalid flat_bonus", "flat_bonus must be an integer.")
    elif require_item:
        out["flat_bonus"] = 0
    if "fallback_value" in body:
        try:
            fv = int(body["fallback_value"])
        except (TypeError, ValueError):
            abort_problem(422, "Invalid fallback_value", "fallback_value must be an integer.")
        if fv < 0:
            abort_problem(422, "Invalid fallback_value", "fallback_value must be ≥ 0.")
        out["fallback_value"] = fv
    elif require_item:
        out["fallback_value"] = 0
    if require_item or "components" in body:
        out["components"] = _validate_components(body.get("components") or [])
    if require_item or "description" in body:
        desc = str(body.get("description") or "").strip()
        out["description"] = desc[:255] or None
    if "active" in body:
        out["active"] = bool(body["active"])
    elif require_item:
        out["active"] = True
    return out


def _apply_override_fields(row, fields: dict) -> None:
    for key in ("item_id", "item_name", "divisor", "flat_bonus", "fallback_value",
                "description", "active"):
        if key in fields:
            setattr(row, key, fields[key])
    if "components" in fields:
        row.components = json.dumps(fields["components"])


@admin_bp.get("/admin/item-values")
async def list_item_values():
    await _require_moderator()

    def _load():
        with db_session() as s:
            rows = s.query(ItemValueOverride).order_by(ItemValueOverride.item_name.asc()).all()
            items = [_override_to_dict(r) for r in rows]
            from web_api.item_value_enrich import enrich_overrides
            enrich_overrides(s, items)
            return items

    items = await asyncio.to_thread(_load)
    # Best-effort live preview of each rule's current value. Never fail the list
    # if the GE API is unavailable.
    try:
        from utils.ge_value import build_component_price_map

        price_map = await build_component_price_map(items)
        for it in items:
            computed = value_overrides.compute_override_from_prices(it, price_map)
            if computed is None:
                computed = it["fallback_value"] or None
            it["computed_value"] = computed
    except Exception:
        for it in items:
            it["computed_value"] = None
    return private_no_store(jsonify(items))


@admin_bp.get("/admin/item-values/item-search")
async def item_value_item_search():
    await _require_moderator()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])

    def _search():
        with db_session() as s:
            rows = (
                s.query(ItemList)
                .filter(ItemList.item_name.ilike(f"%{q}%"))
                .order_by(ItemList.item_name.asc())
                .limit(20)
                .all()
            )
            return [{"item_id": r.item_id, "item_name": r.item_name} for r in rows]

    return jsonify(await asyncio.to_thread(_search))


@admin_bp.get("/admin/item-values/export")
async def item_value_export():
    """Comma-separated active item ids for the GitHub Pages valued_items.txt."""
    await _require_moderator()

    def _load():
        with db_session() as s:
            rows = (
                s.query(ItemValueOverride.item_id)
                .filter(ItemValueOverride.active.is_(True), ItemValueOverride.item_id.isnot(None))
                .order_by(ItemValueOverride.item_id.asc())
                .all()
            )
            return [str(r[0]) for r in rows]

    ids = await asyncio.to_thread(_load)
    return jsonify({"txt": ",".join(ids), "count": len(ids)})


@admin_bp.post("/admin/item-values")
async def create_item_value():
    actor = await _require_moderator()
    body = await json_body()
    fields = _validate_override_body(body, require_item=True)

    def _apply():
        with db_session() as s:
            if fields.get("item_id") is not None and (
                s.query(ItemValueOverride)
                .filter(ItemValueOverride.item_id == fields["item_id"])
                .first()
            ):
                abort_problem(409, "Override exists",
                              f"An override for item {fields['item_id']} already exists.")
            row = ItemValueOverride(author_user_id=actor)
            _apply_override_fields(row, fields)
            s.add(row)
            s.commit()
            return row.id

    new_id = await asyncio.to_thread(_apply)
    value_overrides.invalidate()
    _audit(actor, "item_value.create",
           f"item_value_overrides:{fields.get('item_id') or fields.get('item_name')}")
    return jsonify({"id": new_id})


@admin_bp.patch("/admin/item-values/<int:override_id>")
async def update_item_value(override_id: int):
    actor = await _require_moderator()
    body = await json_body()
    fields = _validate_override_body(body, require_item=False)
    if not fields:
        abort_problem(422, "No changes", "Provide at least one field to update.")

    def _apply():
        with db_session() as s:
            row = s.query(ItemValueOverride).filter(ItemValueOverride.id == override_id).first()
            if not row:
                abort_problem(404, "Override not found", f"No override #{override_id}.")
            if fields.get("item_id") is not None and fields["item_id"] != row.item_id:
                if (
                    s.query(ItemValueOverride)
                    .filter(ItemValueOverride.item_id == fields["item_id"],
                            ItemValueOverride.id != override_id)
                    .first()
                ):
                    abort_problem(409, "Override exists",
                                  f"An override for item {fields['item_id']} already exists.")
            _apply_override_fields(row, fields)
            s.commit()

    await asyncio.to_thread(_apply)
    value_overrides.invalidate()
    _audit(actor, "item_value.update", f"item_value_overrides:{override_id}")
    return jsonify({"ok": True})


@admin_bp.delete("/admin/item-values/<int:override_id>")
async def delete_item_value(override_id: int):
    actor = await _require_moderator()

    def _apply():
        with db_session() as s:
            row = s.query(ItemValueOverride).filter(ItemValueOverride.id == override_id).first()
            if not row:
                abort_problem(404, "Override not found", f"No override #{override_id}.")
            s.delete(row)
            s.commit()

    await asyncio.to_thread(_apply)
    value_overrides.invalidate()
    _audit(actor, "item_value.delete", f"item_value_overrides:{override_id}")
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Site overview KPIs (dashboard landing)
# --------------------------------------------------------------------------- #
@admin_bp.get("/admin/overview")
async def admin_overview():
    await _require_superadmin()

    def _load():
        stats = []

        def _count(q):
            try:
                return int(q.count())
            except Exception:
                return 0

        with db_session() as s:
            now = datetime.now()
            day_start = datetime(now.year, now.month, now.day)
            since_24h = now - timedelta(hours=24)

            stats.append({"key": "users", "label": "Users", "value": _count(s.query(User))})
            stats.append({"key": "players", "label": "Players", "value": _count(s.query(Player))})
            stats.append({
                "key": "groups", "label": "Groups",
                "value": _count(s.query(Group).filter(Group.group_id > 2)),
                "hint": "Excludes system groups",
            })
            stats.append({
                "key": "drops_today", "label": "Drops today",
                "value": _count(s.query(Drop).filter(Drop.date_added >= day_start)),
            })
            stats.append({
                "key": "active_subscriptions", "label": "Active subscriptions",
                "value": _count(
                    s.query(GroupSubscription).filter(
                        GroupSubscription.status == "active",
                        # Nitro-boost credit isn't a subscription; keep comps + legacy NULL.
                        or_(
                            GroupSubscription.provider.is_(None),
                            GroupSubscription.provider != NITRO_PROVIDER,
                        ),
                    )
                ),
            })
            # Headline MRR — the full breakdown lives on /admin/subscriptions.
            # Comped grants and Nitro-boost credit (NON_REVENUE_PROVIDERS) keep
            # their entitlements but are not income, so they are excluded from
            # every revenue figure.
            try:
                tiers_by_key = {t.key: t for t in s.query(SubscriptionTier).all()}
                mrr = sum(
                    leg_monthly_cents(leg, tiers_by_key)
                    for leg in s.query(GroupSubscription).all()
                    if subscription_is_live(leg) and leg.provider not in NON_REVENUE_PROVIDERS
                )
                for u in s.query(UserSubscription).all():
                    if not subscription_is_live(u) or u.provider in NON_REVENUE_PROVIDERS:
                        continue
                    tier = tiers_by_key.get(u.tier_key) if u.tier_key else None
                    amount = u.amount_cents if u.amount_cents else (tier.price_cents if tier else 0)
                    mrr += _monthly_cents(amount, tier.interval if tier else "month")
                stats.append({
                    "key": "mrr", "label": "Monthly recurring revenue",
                    "value": f"${mrr / 100:,.2f}",
                    "hint": "Live paid subscriptions, monthly-normalized (comped excluded)",
                })
            except Exception:
                pass
            stats.append({
                "key": "pending_discord_outbox", "label": "Pending Discord outbox",
                "value": _count(
                    s.query(DiscordOutbox).filter(DiscordOutbox.status == "pending")
                ),
                "hint": "Messages awaiting the bot",
            })
            stats.append({
                "key": "pending_notifications", "label": "Pending notifications",
                "value": _count(
                    s.query(NotificationQueue).filter(NotificationQueue.status == "pending")
                ),
            })
            stats.append({
                "key": "audit_entries_24h", "label": "Audit entries (24h)",
                "value": _count(s.query(AuditLog).filter(AuditLog.created_at >= since_24h)),
            })
            stats.append({
                "key": "announcements", "label": "Published announcements",
                "value": _count(
                    s.query(Announcement).filter(Announcement.status == "published")
                ),
            })
        return stats

    stats = await asyncio.to_thread(_load)
    return private_no_store(jsonify({"stats": stats, "generated_at": int(datetime.now().timestamp())}))


# --------------------------------------------------------------------------- #
# Monetization dashboard (/admin/subscriptions)
# --------------------------------------------------------------------------- #
def _ts(dt) -> int | None:
    return int(dt.timestamp()) if dt else None


def _monthly_cents(amount, interval: str | None) -> int:
    amt = int(amount or 0)
    return amt // 12 if (interval or "month") == "year" else amt


@admin_bp.get("/admin/subscriptions/overview")
async def admin_subscriptions_overview():
    """Everything the superadmin monetization dashboard renders, one payload:
    MRR/lifetime KPIs, 12-month income (from the payments ledger), every
    subscription across both scopes, and recent payments."""
    await _require_superadmin()

    def _load():
        with db_session() as s:
            legs = s.query(GroupSubscription).all()
            usubs = s.query(UserSubscription).all()
            tiers = s.query(SubscriptionTier).all()
            tiers_by_key = {t.key: t for t in tiers}

            live_legs = [leg for leg in legs if subscription_is_live(leg)]
            live_usubs = [u for u in usubs if subscription_is_live(u)]

            # Comped grants and Nitro-boost credit (NON_REVENUE_PROVIDERS) keep
            # their entitlements but are not income — exclude from every metric.
            paid_legs = [leg for leg in live_legs if leg.provider not in NON_REVENUE_PROVIDERS]
            paid_usubs = [u for u in live_usubs if u.provider not in NON_REVENUE_PROVIDERS]

            group_mrr = sum(leg_monthly_cents(leg, tiers_by_key) for leg in paid_legs)
            user_mrr = 0
            for u in paid_usubs:
                tier = tiers_by_key.get(u.tier_key) if u.tier_key else None
                amount = u.amount_cents if u.amount_cents else (tier.price_cents if tier else 0)
                user_mrr += _monthly_cents(amount, tier.interval if tier else "month")

            # Effective tier per paying group (pool resolution). Groups whose
            # only live leg is a comp are not "paying".
            effective = effective_group_tiers(s, {leg.group_id for leg in paid_legs})
            tier_counts: dict[str, int] = {}
            for tier, _total in effective.values():
                tier_counts[tier.key] = tier_counts.get(tier.key, 0) + 1
            tier_distribution = [
                {
                    "tier_key": key,
                    "tier_name": tiers_by_key[key].name if key in tiers_by_key else key,
                    "groups": count,
                }
                for key, count in sorted(tier_counts.items(), key=lambda kv: -kv[1])
            ]

            past_due = sum(1 for leg in legs if leg.status == "past_due") + sum(
                1 for u in usubs if u.status == "past_due"
            )

            # Ledger aggregates. Refunds/reversals subtract; manual rows are
            # excluded defensively (no live path writes them today).
            signed = "SUM(CASE WHEN kind = 'payment' THEN amount_cents ELSE -amount_cents END)"
            lifetime = s.execute(
                sa_text(
                    f"SELECT COALESCE({signed}, 0) FROM subscription_payments "
                    "WHERE provider != 'manual'"
                )
            ).scalar()
            months = s.execute(
                sa_text(
                    "SELECT DATE_FORMAT(paid_at, '%Y-%m') AS month, "
                    f"{signed} AS cents "
                    "FROM subscription_payments "
                    "WHERE provider != 'manual' "
                    "AND paid_at >= DATE_SUB(NOW(), INTERVAL 12 MONTH) "
                    "GROUP BY month ORDER BY month ASC"
                )
            ).fetchall()

            # Names for every row we're about to list.
            group_ids = {leg.group_id for leg in legs}
            payer_ids = {leg.user_id for leg in legs if leg.user_id} | {
                u.user_id for u in usubs
            }
            group_names = dict(
                s.query(Group.group_id, Group.group_name)
                .filter(Group.group_id.in_(group_ids))
                .all()
            ) if group_ids else {}
            user_names = dict(
                s.query(User.user_id, User.username)
                .filter(User.user_id.in_(payer_ids))
                .all()
            ) if payer_ids else {}

            subscriptions = []
            for leg in legs:
                subscriptions.append({
                    "scope": "group",
                    "id": leg.id,
                    "group_id": leg.group_id,
                    "group_name": group_names.get(leg.group_id),
                    "user_id": leg.user_id,
                    "user_name": user_names.get(leg.user_id),
                    "tier_key": leg.tier_key,
                    "amount_cents": int(leg.amount_cents) if leg.amount_cents is not None else None,
                    "provider": leg.provider,
                    "status": leg.status,
                    "live": leg in live_legs,
                    "current_period_end": _ts(leg.current_period_end),
                    "cancel_at_period_end": bool(leg.cancel_at_period_end),
                })
            for u in usubs:
                subscriptions.append({
                    "scope": "user",
                    "id": u.id,
                    "group_id": None,
                    "group_name": None,
                    "user_id": u.user_id,
                    "user_name": user_names.get(u.user_id),
                    "tier_key": u.tier_key,
                    "amount_cents": int(u.amount_cents) if u.amount_cents else None,
                    "provider": u.provider,
                    "status": u.status,
                    "live": u in live_usubs,
                    "current_period_end": _ts(u.current_period_end),
                    "cancel_at_period_end": bool(u.cancel_at_period_end),
                })
            # Live first, then by soonest renewal.
            subscriptions.sort(key=lambda r: (not r["live"], r["current_period_end"] or 0))

            recent_rows = (
                s.query(SubscriptionPayment)
                .order_by(SubscriptionPayment.paid_at.desc())
                .limit(20)
                .all()
            )
            pay_group_ids = {p.group_id for p in recent_rows if p.group_id}
            pay_user_ids = {p.user_id for p in recent_rows if p.user_id}
            pay_group_names = dict(
                s.query(Group.group_id, Group.group_name)
                .filter(Group.group_id.in_(pay_group_ids))
                .all()
            ) if pay_group_ids else {}
            pay_user_names = dict(
                s.query(User.user_id, User.username)
                .filter(User.user_id.in_(pay_user_ids))
                .all()
            ) if pay_user_ids else {}
            recent_payments = [
                {
                    "id": p.id,
                    "scope": p.scope,
                    "group_id": p.group_id,
                    "group_name": pay_group_names.get(p.group_id),
                    "user_id": p.user_id,
                    "user_name": pay_user_names.get(p.user_id),
                    "tier_key": p.tier_key,
                    "provider": p.provider,
                    "amount_cents": int(p.amount_cents),
                    "currency": p.currency,
                    "kind": p.kind,
                    "paid_at": _ts(p.paid_at),
                }
                for p in recent_rows
            ]

            return {
                "kpis": {
                    "mrr_cents": group_mrr + user_mrr,
                    "group_mrr_cents": group_mrr,
                    "user_mrr_cents": user_mrr,
                    "paying_groups": len(effective),
                    "active_user_subscriptions": len(live_usubs),
                    "past_due": past_due,
                    "lifetime_cents": int(lifetime or 0),
                },
                "tier_distribution": tier_distribution,
                "income_by_month": [
                    {"month": m, "amount_cents": int(c or 0)} for m, c in months
                ],
                "subscriptions": subscriptions,
                "recent_payments": recent_payments,
            }

    payload = await asyncio.to_thread(_load)
    payload["generated_at"] = int(datetime.now().timestamp())
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Comped subscriptions (reuse group_subscriptions — no new table)
# --------------------------------------------------------------------------- #
@admin_bp.post("/admin/groups/<int:group_id>/subscription/grant")
async def admin_grant_subscription(group_id: int):
    actor = await _require_superadmin()
    body = await json_body()
    grant = registry.build_comped_grant(body.get("tier_key"), body.get("days"))

    def _apply():
        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                abort_problem(404, "Group not found", f"No group with id {group_id}.")
            tier = (
                s.query(SubscriptionTier)
                .filter(SubscriptionTier.key == grant["tier_key"])
                .first()
            )
            if not tier:
                abort_problem(404, "Unknown tier", f"No tier '{grant['tier_key']}'.")
            # Pool model: comp = the group's single MANUAL leg. Never touch
            # paid legs (stripe/paypal) — a comp sits alongside them.
            sub = (
                s.query(GroupSubscription)
                .filter(
                    GroupSubscription.group_id == group_id,
                    GroupSubscription.provider == "manual",
                )
                .first()
            )
            before = _serialize_sub(group_id, sub) if sub else None
            if not sub:
                sub = GroupSubscription(group_id=group_id)
                s.add(sub)
            sub.provider = grant["provider"]
            sub.status = grant["status"]
            sub.tier_key = grant["tier_key"]
            sub.amount_cents = tier.price_cents
            sub.current_period_end = grant["current_period_end"]
            sub.cancel_at_period_end = grant["cancel_at_period_end"]
            s.commit()
            return before, _serialize_sub(group_id, sub)

    before, after = await asyncio.to_thread(_apply)
    _audit(
        actor, "subscription.grant", f"group:{group_id}",
        before=json.dumps(before) if before else None, after=json.dumps(after),
    )
    return jsonify(after)


@admin_bp.post("/admin/groups/<int:group_id>/subscription/revoke")
async def admin_revoke_subscription(group_id: int):
    actor = await _require_superadmin()

    def _apply():
        with db_session() as s:
            # Pool model: revoke targets the comped MANUAL leg only; paid
            # legs are managed by their payers/providers.
            sub = (
                s.query(GroupSubscription)
                .filter(
                    GroupSubscription.group_id == group_id,
                    GroupSubscription.provider == "manual",
                )
                .first()
            )
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "This group has no comped subscription.")
            before = _serialize_sub(group_id, sub)
            sub.status = "canceled"
            s.commit()
            return before, _serialize_sub(group_id, sub)

    before, after = await asyncio.to_thread(_apply)
    _audit(
        actor, "subscription.revoke", f"group:{group_id}",
        before=json.dumps(before), after=json.dumps(after),
    )
    return jsonify(after)


@admin_bp.post("/admin/users/<int:user_id>/subscription/grant")
async def admin_grant_user_subscription(user_id: int):
    actor = await _require_superadmin()
    body = await json_body()
    grant = registry.build_comped_grant(body.get("tier_key"), body.get("days"))

    def _apply():
        with db_session() as s:
            target = s.query(User).filter(User.user_id == user_id).first()
            if not target:
                abort_problem(404, "User not found", f"No user with id {user_id}.")
            tier = (
                s.query(SubscriptionTier)
                .filter(
                    SubscriptionTier.key == grant["tier_key"],
                    SubscriptionTier.scope == "user",
                )
                .first()
            )
            if not tier:
                abort_problem(404, "Unknown tier", f"No user-scoped tier '{grant['tier_key']}'.")
            sub = (
                s.query(UserSubscription)
                .filter(UserSubscription.user_id == user_id)
                .first()
            )
            before = _serialize_user_sub(user_id, sub) if sub else None
            if not sub:
                sub = UserSubscription(user_id=user_id)
                s.add(sub)
            sub.provider = grant["provider"]
            sub.status = grant["status"]
            sub.tier_key = grant["tier_key"]
            sub.current_period_end = grant["current_period_end"]
            sub.cancel_at_period_end = grant["cancel_at_period_end"]
            s.commit()
            return before, _serialize_user_sub(user_id, sub)

    before, after = await asyncio.to_thread(_apply)
    try:
        from db.entitlements import invalidate_user_entitlement_cache

        invalidate_user_entitlement_cache(user_id)
    except Exception:
        pass
    _audit(
        actor, "subscription.grant", f"user:{user_id}",
        before=json.dumps(before) if before else None, after=json.dumps(after),
    )
    return jsonify(after)


@admin_bp.post("/admin/users/<int:user_id>/subscription/revoke")
async def admin_revoke_user_subscription(user_id: int):
    actor = await _require_superadmin()

    def _apply():
        with db_session() as s:
            sub = (
                s.query(UserSubscription)
                .filter(UserSubscription.user_id == user_id)
                .first()
            )
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "This user has no supporter subscription.")
            before = _serialize_user_sub(user_id, sub)
            sub.status = "canceled"
            s.commit()
            return before, _serialize_user_sub(user_id, sub)

    before, after = await asyncio.to_thread(_apply)
    try:
        from db.entitlements import invalidate_user_entitlement_cache

        invalidate_user_entitlement_cache(user_id)
    except Exception:
        pass
    _audit(
        actor, "subscription.revoke", f"user:{user_id}",
        before=json.dumps(before), after=json.dumps(after),
    )
    return jsonify(after)


# --------------------------------------------------------------------------- #
# Nitro boost slots
#
# Discord exposes no per-member boost count: the member list only says WHO
# boosts, boost system messages carry a count that can go stale, and
# premium_subscription_count is an unattributable total. So some slots can only
# be assigned by hand — that is what these endpoints are for. See
# services/nitro_attribution.py for the full precedence rules.
#
# The bot owns the Discord facts and publishes a snapshot to Redis (the web API
# never opens a Discord connection); this reads that snapshot and writes the
# override the next reconcile will honour.
# --------------------------------------------------------------------------- #
@admin_bp.get("/admin/nitro-boosts")
async def admin_nitro_boosts():
    """Current boost attribution: who is credited how many slots, what the guild
    says the total is, and how many slots nothing could account for."""
    await _require_superadmin()

    def _load():
        from services.nitro_attribution import (
            NITRO_BOOST_CENTS,
            NITRO_COUNT_OVERRIDE_KEY,
            load_boost_snapshot,
        )

        snapshot = load_boost_snapshot()
        with db_session() as s:
            # Every override on record, including for members who have since
            # stopped boosting — those are exactly the ones worth clearing.
            rows = (
                s.query(User.user_id, User.username, User.discord_id, UserConfiguration.config_value)
                .join(UserConfiguration, UserConfiguration.user_id == User.user_id)
                .filter(UserConfiguration.config_key == NITRO_COUNT_OVERRIDE_KEY)
                .all()
            )
            overrides = []
            for user_id, username, discord_id, value in rows:
                try:
                    slots = int(value)
                except (TypeError, ValueError):
                    continue
                overrides.append({
                    "user_id": user_id,
                    "username": username,
                    "discord_id": str(discord_id) if discord_id else None,
                    "slots": slots,
                })
        return {
            "per_boost_cents": NITRO_BOOST_CENTS,
            "snapshot": snapshot,
            "overrides": sorted(overrides, key=lambda o: -o["slots"]),
        }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@admin_bp.post("/admin/users/<int:user_id>/nitro-boosts")
async def admin_set_nitro_boosts(user_id: int):
    """Set (or clear, with a null/absent ``slots``) a user's boost-slot count.

    Takes effect on the next reconcile, which is nudged to run promptly. The
    reconciler still refuses to credit anyone who is not actually boosting, and
    still clamps the guild-wide total, so this cannot mint credit out of nothing.
    """
    actor = await _require_superadmin()
    body = await json_body()
    slots = body.get("slots")
    if slots is not None:
        if isinstance(slots, bool) or not isinstance(slots, int):
            abort_problem(422, "Invalid value", "'slots' must be an integer or null.")
        if slots < 1:
            abort_problem(422, "Invalid value", "'slots' must be at least 1 (send null to clear).")

    def _apply():
        from services.nitro_attribution import (
            MAX_BOOST_SLOTS_PER_USER,
            get_boost_count_override,
            set_boost_count_override,
        )

        if slots is not None and slots > MAX_BOOST_SLOTS_PER_USER:
            abort_problem(
                422, "Invalid value",
                f"'slots' may not exceed {MAX_BOOST_SLOTS_PER_USER}.",
            )
        with db_session() as s:
            user = s.query(User).filter(User.user_id == user_id).first()
            if not user:
                abort_problem(404, "User not found", f"No user with id {user_id}.")
            before = get_boost_count_override(s, user_id)
            set_boost_count_override(s, user_id, slots)
            s.commit()
            return before, {
                "user_id": user_id,
                "username": user.username,
                "discord_id": str(user.discord_id) if user.discord_id else None,
                "slots": get_boost_count_override(s, user_id),
            }

    before, after = await asyncio.to_thread(_apply)

    def _nudge():
        from services.nitro_attribution import request_reconcile

        return request_reconcile()

    await asyncio.to_thread(_nudge)
    _audit(
        actor, "nitro.override", f"user:{user_id}",
        before=json.dumps({"slots": before}), after=json.dumps(after),
    )
    return jsonify(after)


# --------------------------------------------------------------------------- #
# Curated data viewer / editor (SAFE — no arbitrary SQL)
# --------------------------------------------------------------------------- #
def _apply_search(query, spec, q: str):
    if not q:
        return query
    model = spec["model"]
    conds = []
    like = f"%{q}%"
    for col in spec.get("search_text", []):
        conds.append(getattr(model, col).ilike(like))
    if q.isdigit():
        for col in spec.get("search_int", []):
            conds.append(getattr(model, col) == int(q))
    if conds:
        query = query.filter(or_(*conds))
    return query


@admin_bp.get("/admin/data/<entity>")
async def admin_data_list(entity: str):
    await _require_superadmin()
    q = (request.args.get("q") or "").strip()
    page, limit = parse_page(request, default_limit=50, max_limit=registry.MAX_LIMIT)

    def _load():
        spec = registry.get_spec(entity)
        model = spec["model"]
        with db_session() as s:
            query = _apply_search(s.query(model), spec, q)
            total = query.count()
            pk_col = getattr(model, spec["pk"])
            rows = (
                query.order_by(pk_col.desc())
                .offset((page - 1) * limit)
                .limit(limit)
                .all()
            )
            return {
                "entity": entity,
                "columns": spec["columns"],
                "rows": [registry.serialize_row(spec, r) for r in rows],
                "editable": spec["editable"],
                "meta": {"page": page, "limit": limit, "total": int(total)},
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@admin_bp.get("/admin/data/<entity>/<record_id>")
async def admin_data_get(entity: str, record_id: str):
    await _require_superadmin()

    def _load():
        spec = registry.get_spec(entity)
        model = spec["model"]
        pk_val = registry.coerce_pk(spec, record_id)
        with db_session() as s:
            row = s.query(model).filter(getattr(model, spec["pk"]) == pk_val).first()
            if not row:
                abort_problem(404, "Record not found", f"No {entity} with id {record_id}.")
            return {
                "entity": entity,
                "id": registry.serialize_value(pk_val),
                "record": registry.serialize_row(spec, row),
                "editable": spec["editable"],
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@admin_bp.patch("/admin/data/<entity>/<record_id>")
async def admin_data_patch(entity: str, record_id: str):
    actor = await _require_superadmin()
    body = await json_body()
    fields = registry.validate_editable_fields(entity, body.get("fields"))

    def _apply():
        spec = registry.get_spec(entity)
        model = spec["model"]
        pk_val = registry.coerce_pk(spec, record_id)
        with db_session() as s:
            row = s.query(model).filter(getattr(model, spec["pk"]) == pk_val).first()
            if not row:
                abort_problem(404, "Record not found", f"No {entity} with id {record_id}.")
            before = {k: registry.serialize_value(getattr(row, k, None)) for k in fields}
            for k, v in fields.items():
                setattr(row, k, v)
            s.commit()
            after = {k: registry.serialize_value(getattr(row, k, None)) for k in fields}
            return {
                "entity": entity,
                "id": registry.serialize_value(pk_val),
                "record": registry.serialize_row(spec, row),
                "editable": spec["editable"],
            }, before, after

    payload, before, after = await asyncio.to_thread(_apply)
    _audit(
        actor, "data.update", f"{entity}:{record_id}",
        before=json.dumps(before), after=json.dumps(after),
    )
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Application logs (from the `logs` analytics table)
# --------------------------------------------------------------------------- #
@admin_bp.get("/admin/logs")
async def admin_logs():
    await _require_superadmin()
    source = (request.args.get("source") or "").strip()
    try:
        limit = int(request.args.get("limit", 200))
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 500))

    def _load():
        entries = []
        sources = []
        with db_session() as s:
            try:
                sources = sorted(
                    src for (src,) in s.query(Log.source).distinct().all() if src
                )
            except Exception:
                sources = []
            try:
                q = s.query(Log)
                if source:
                    q = q.filter(Log.source == source)
                rows = q.order_by(Log.timestamp.desc()).limit(limit).all()
                for r in rows:
                    entries.append({
                        "ts": int(r.timestamp) if r.timestamp is not None else 0,
                        "level": r.level,
                        "source": r.source,
                        "message": r.message,
                    })
            except Exception:
                entries = []
        return {"entries": entries, "sources": sources}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


# --------------------------------------------------------------------------- #
# Audit log (superadmin browser over the `audit_log` table)
# --------------------------------------------------------------------------- #
def _actor_map(s, rows) -> dict:
    """Batch-load {user_id: {user_id, discord_id, username}} for a set of
    AuditLog rows, avoiding an N+1 lookup per row."""
    actor_ids = {r.actor_user_id for r in rows if r.actor_user_id}
    if not actor_ids:
        return {}
    return {
        u.user_id: {
            "user_id": u.user_id,
            "discord_id": str(u.discord_id) if u.discord_id else None,
            "username": u.username,
        }
        for u in s.query(User).filter(User.user_id.in_(actor_ids)).all()
    }


def _serialize_audit_row(r, actors: dict) -> dict:
    return {
        "id": r.id,
        "actor": actors.get(r.actor_user_id) if r.actor_user_id else None,
        "group_id": r.group_id,
        "action": r.action,
        "target": r.target,
        "before": r.before,
        "after": r.after,
        "created_at": int(r.created_at.timestamp()) if r.created_at else None,
    }


@admin_bp.get("/admin/audit")
async def admin_audit_log():
    await _require_superadmin()
    action = (request.args.get("action") or "").strip()
    actor_raw = (request.args.get("actor_user_id") or "").strip()
    group_raw = (request.args.get("group_id") or "").strip()
    q = (request.args.get("q") or "").strip()
    page, limit = parse_page(request, default_limit=50, max_limit=100)

    def _load():
        with db_session() as s:
            query = s.query(AuditLog)
            if action:
                query = query.filter(AuditLog.action.ilike(f"%{action}%"))
            if actor_raw.isdigit():
                query = query.filter(AuditLog.actor_user_id == int(actor_raw))
            if group_raw.isdigit():
                query = query.filter(AuditLog.group_id == int(group_raw))
            if q:
                like = f"%{q}%"
                query = query.filter(
                    or_(AuditLog.target.ilike(like), AuditLog.action.ilike(like))
                )

            total = query.count()
            rows = (
                query.order_by(AuditLog.created_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
                .all()
            )

            actors = _actor_map(s, rows)
            entries = [_serialize_audit_row(r, actors) for r in rows]
            return {
                "entries": entries,
                "meta": {"page": page, "limit": limit, "total": int(total)},
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


# --------------------------------------------------------------------------- #
# Group staff overview (superadmin view of any group)
# --------------------------------------------------------------------------- #
_CONFIG_SUMMARY_KEYS = [
    "channel_id_to_post_loot",
    "lootboard_channel_id",
    "minimum_value_to_notify",
    "only_send_messages_with_images",
    "send_stacks_of_items",
    "loot_board_type",
    "announcements_channel_id",
]


@admin_bp.get("/admin/groups/<int:group_id>/overview")
async def admin_group_overview(group_id: int):
    await _require_superadmin()

    def _load():
        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                abort_problem(404, "Group not found", f"No group with id {group_id}.")

            try:
                member_count = int(group.get_player_count(s))
            except Exception:
                member_count = 0

            # Effective pool view (computed tier + all contribution legs).
            subscription = _serialize_group_sub(s, group_id)

            cfg_rows = (
                s.query(GroupConfiguration.config_key, GroupConfiguration.config_value)
                .filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key.in_(_CONFIG_SUMMARY_KEYS),
                )
                .all()
            )
            cfg_map = {k: v for k, v in cfg_rows}
            config_summary = {k: cfg_map.get(k) for k in _CONFIG_SUMMARY_KEYS}

            from db.models import NotifiedSubmission

            last_sub = (
                s.query(NotifiedSubmission.date_added)
                .filter(NotifiedSubmission.group_id == group_id)
                .order_by(NotifiedSubmission.date_added.desc())
                .first()
            )
            last_submission_ts = (
                int(last_sub[0].timestamp()) if last_sub and last_sub[0] else None
            )

            activity = []
            for i in range(6, -1, -1):
                day = (datetime.now() - timedelta(days=i)).date()
                start = datetime(day.year, day.month, day.day)
                end = start + timedelta(days=1)
                count = (
                    s.query(NotifiedSubmission)
                    .filter(
                        NotifiedSubmission.group_id == group_id,
                        NotifiedSubmission.date_added >= start,
                        NotifiedSubmission.date_added < end,
                    )
                    .count()
                )
                activity.append({"date": day.isoformat(), "submissions": int(count)})

            warnings = []
            if not cfg_map.get("channel_id_to_post_loot"):
                warnings.append("No drops channel (channel_id_to_post_loot) configured — drop notifications won't post.")
            if not group.guild_id:
                warnings.append("No Discord guild linked to this group.")
            if subscription["status"] not in ("active", "trialing"):
                warnings.append("Group has no active subscription.")

            return {
                "group": {
                    "id": group.group_id,
                    "name": group.group_name,
                    "member_count": member_count,
                    "guild_id": str(group.guild_id) if group.guild_id else None,
                    "wom_id": int(group.wom_id) if group.wom_id else None,
                },
                "subscription": subscription,
                "config_summary": config_summary,
                "activity_7d": activity,
                "last_submission_ts": last_submission_ts,
                "warnings": warnings,
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


# --------------------------------------------------------------------------- #
# User moderation (superadmin view of any user)
# --------------------------------------------------------------------------- #
@admin_bp.get("/admin/users/<int:user_id>/overview")
async def admin_user_overview(user_id: int):
    await _require_superadmin()

    def _load():
        with db_session() as s:
            user = s.query(User).filter(User.user_id == user_id).first()
            if not user:
                abort_problem(404, "User not found", f"No user with id {user_id}.")

            players = [
                {
                    "id": p.player_id,
                    "name": p.player_name,
                    "wom_id": int(p.wom_id) if p.wom_id else None,
                    "hidden": bool(p.hidden),
                }
                for p in s.query(Player).filter(Player.user_id == user_id).all()
            ]

            # Groups: membership union explicit GroupAdmin grants (the durable
            # path). Deliberately omits the volatile MANAGE_GUILD Redis cache used
            # by role derivation elsewhere (deps.resolve_group_role) — that cache
            # is only populated by the *browsing* user's own login and would show
            # stale or misleading roles here for a different user being moderated.
            group_names: dict[int, str] = {}
            group_roles: dict[int, str] = {}
            for g in user.groups:
                group_names[g.group_id] = g.group_name
                group_roles.setdefault(g.group_id, "member")
            for ga in s.query(GroupAdmin).filter(GroupAdmin.user_id == user_id).all():
                group_roles[ga.group_id] = ga.role if ga.role in ("owner", "admin") else "admin"
                if ga.group_id not in group_names:
                    grp = s.query(Group).filter(Group.group_id == ga.group_id).first()
                    group_names[ga.group_id] = grp.group_name if grp else None

            groups = [
                {"id": gid, "name": group_names.get(gid) or f"Group {gid}", "role": group_roles[gid]}
                for gid in group_names
            ]

            recent_rows = (
                s.query(AuditLog)
                .filter(
                    or_(AuditLog.actor_user_id == user_id, AuditLog.target == f"user:{user_id}")
                )
                .order_by(AuditLog.created_at.desc())
                .limit(20)
                .all()
            )
            actors = _actor_map(s, recent_rows)
            recent_audit = [_serialize_audit_row(r, actors) for r in recent_rows]

            return {
                "user": {
                    "user_id": user.user_id,
                    "discord_id": str(user.discord_id) if user.discord_id else None,
                    "username": user.username,
                    "is_superadmin": bool(getattr(user, "is_superadmin", False)),
                    "is_moderator": bool(getattr(user, "is_moderator", False)),
                    "public": bool(user.public),
                    "hidden": bool(user.hidden),
                    "date_added": int(user.date_added.timestamp()) if user.date_added else None,
                },
                "players": players,
                "groups": groups,
                "recent_audit": recent_audit,
            }

    payload = await asyncio.to_thread(_load)

    from web_api.routes.auth import get_cached_profile

    profile = get_cached_profile(user_id)
    payload["user"]["display_name"] = profile.get("display_name")
    payload["user"]["avatar_url"] = profile.get("avatar_url")
    return private_no_store(jsonify(payload))


@admin_bp.post("/admin/users/<int:user_id>/superadmin")
async def admin_set_user_superadmin(user_id: int):
    actor = await _require_superadmin()
    body = await json_body()
    grant = body.get("grant")
    if not isinstance(grant, bool):
        abort_problem(422, "Invalid grant", "'grant' must be a boolean.")
    if not grant and user_id == actor:
        abort_problem(409, "Cannot self-revoke", "You cannot revoke your own superadmin access.")

    def _apply():
        with db_session() as s:
            user = s.query(User).filter(User.user_id == user_id).first()
            if not user:
                abort_problem(404, "User not found", f"No user with id {user_id}.")
            before = bool(getattr(user, "is_superadmin", False))
            user.is_superadmin = grant
            s.commit()
            return before

    before = await asyncio.to_thread(_apply)
    _audit(
        actor,
        "user.superadmin_grant" if grant else "user.superadmin_revoke",
        f"user:{user_id}",
        before=json.dumps(before),
        after=json.dumps(grant),
    )
    return jsonify({"ok": True})


# The profile badge that moderator status carries. Created on first grant;
# awarded to every player account the user owns (slot "p:{player_id}" —
# the manual-award slot, so revoking frees it for a future re-grant).
_MODERATOR_BADGE = {
    "key": "moderator",
    "name": "Moderator",
    "description": "DropTracker site moderator.",
    "icon_emoji": "\U0001F6E1\uFE0F",  # shield
    "tone": "sky",
    "semantic": "permanent",
}


@admin_bp.post("/admin/users/<int:user_id>/moderator")
async def admin_set_user_moderator(user_id: int):
    """Grant/revoke the moderator flag (superadmin only). Also awards or
    revokes the "moderator" profile badge on all the user's player accounts,
    and audit-logs the change."""
    actor = await _require_superadmin()
    body = await json_body()
    grant = body.get("grant")
    if not isinstance(grant, bool):
        abort_problem(422, "Invalid grant", "'grant' must be a boolean.")

    def _apply():
        from db import Badge, Player, PlayerBadge
        from services.badges import award_badge, revoke_badge

        with db_session() as s:
            user = s.query(User).filter(User.user_id == user_id).first()
            if not user:
                abort_problem(404, "User not found", f"No user with id {user_id}.")
            before = bool(getattr(user, "is_moderator", False))
            user.is_moderator = grant

            badge = s.query(Badge).filter(Badge.key == _MODERATOR_BADGE["key"]).first()
            if badge is None and grant:
                badge = Badge(active=True, criteria=None, scope="global", **_MODERATOR_BADGE)
                s.add(badge)
                s.flush()

            player_ids = [
                pid for (pid,) in s.query(Player.player_id).filter(Player.user_id == user_id).all()
            ]
            if badge is not None:
                if grant:
                    for pid in player_ids:
                        award_badge(
                            s, badge, pid, slot_key=f"p:{pid}",
                            context={"note": "site moderator"}, awarded_by=actor,
                        )
                else:
                    active = (
                        s.query(PlayerBadge)
                        .filter(
                            PlayerBadge.badge_id == badge.badge_id,
                            PlayerBadge.player_id.in_(player_ids or [0]),
                            PlayerBadge.status == "active",
                        )
                        .all()
                    )
                    for award in active:
                        revoke_badge(s, award)
            s.commit()
            return before

    before = await asyncio.to_thread(_apply)
    _audit(
        actor,
        "user.moderator_grant" if grant else "user.moderator_revoke",
        f"user:{user_id}",
        before=json.dumps(before),
        after=json.dumps(grant),
    )
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Badges (catalog CRUD + manual award/revoke)
# --------------------------------------------------------------------------- #

def _serialize_badge_admin(b, active_awards: int = 0) -> dict:
    return {
        "key": b.key,
        "name": b.name,
        "description": b.description,
        "icon_url": b.icon_url,
        "icon_emoji": b.icon_emoji,
        "tone": b.tone,
        "semantic": b.semantic,
        "active": bool(b.active),
        "automatic": b.criteria is not None,
        "criteria": b.criteria,  # read-only in the UI (code-owned behavior)
        "active_awards": int(active_awards),
    }


@admin_bp.get("/admin/badges")
async def admin_list_badges():
    await _require_superadmin()

    def _load():
        from sqlalchemy import func as safunc

        from db import Badge, PlayerBadge

        with db_session() as s:
            counts = dict(
                s.query(PlayerBadge.badge_id, safunc.count(PlayerBadge.id))
                .filter(PlayerBadge.status == "active")
                .group_by(PlayerBadge.badge_id)
                .all()
            )
            rows = s.query(Badge).order_by(Badge.badge_id.asc()).all()
            return [_serialize_badge_admin(b, counts.get(b.badge_id, 0)) for b in rows]

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@admin_bp.post("/admin/badges")
async def admin_save_badge():
    """Upsert a badge definition by key. ``semantic`` is fixed once any award
    exists; ``criteria`` is never editable from the UI (evaluators are
    code-owned) — new badges created here are manual-only (criteria NULL)."""
    actor = await _require_superadmin()
    body = await json_body()

    from db.models.badge import BADGE_SEMANTICS, BADGE_TONES

    key = (body.get("key") or "").strip().lower()
    if not key or len(key) > 64 or not key.replace("_", "").isalnum():
        abort_problem(422, "Invalid key", "Badge key must be a short snake_case identifier.")
    name = (body.get("name") or "").strip()
    if not name:
        abort_problem(422, "Invalid name", "Badge name is required.")
    description = (body.get("description") or "").strip()
    if not description:
        abort_problem(422, "Invalid description", "Badge description is required.")
    tone = body.get("tone") or "gold"
    if tone not in BADGE_TONES:
        abort_problem(422, "Invalid tone", f"Tone must be one of: {', '.join(BADGE_TONES)}.")
    semantic = body.get("semantic") or "permanent"
    if semantic not in BADGE_SEMANTICS:
        abort_problem(422, "Invalid semantic", "Semantic must be 'permanent' or 'held'.")
    icon_url = (body.get("icon_url") or "").strip() or None
    icon_emoji = (body.get("icon_emoji") or "").strip() or None
    active = bool(body.get("active", True))

    def _apply():
        from db import Badge, PlayerBadge

        with db_session() as s:
            b = s.query(Badge).filter(Badge.key == key).first()
            before = _serialize_badge_admin(b) if b else None
            if b is None:
                b = Badge(
                    key=key, name=name, description=description, tone=tone,
                    semantic=semantic, icon_url=icon_url, icon_emoji=icon_emoji,
                    active=active, criteria=None,
                )
                s.add(b)
            else:
                if semantic != b.semantic:
                    has_awards = (
                        s.query(PlayerBadge.id)
                        .filter(PlayerBadge.badge_id == b.badge_id)
                        .first()
                        is not None
                    )
                    if has_awards:
                        abort_problem(
                            409, "Semantic locked",
                            "Cannot change semantics of a badge that has awards.",
                        )
                    b.semantic = semantic
                b.name = name
                b.description = description
                b.tone = tone
                b.icon_url = icon_url
                b.icon_emoji = icon_emoji
                b.active = active
            s.commit()
            return before, _serialize_badge_admin(b)

    before, after = await asyncio.to_thread(_apply)
    _audit(
        actor, "badge.definition.save", f"badge:{key}",
        before=json.dumps(before) if before else None,
        after=json.dumps(after),
    )
    return jsonify(after)


@admin_bp.delete("/admin/badges/<key>")
async def admin_delete_badge(key: str):
    """Soft delete: hides the badge (and its awards) everywhere; history kept."""
    actor = await _require_superadmin()

    def _apply():
        from db import Badge

        with db_session() as s:
            b = s.query(Badge).filter(Badge.key == key).first()
            if b is None:
                abort_problem(404, "Badge not found", f"No badge with key '{key}'.")
            before = _serialize_badge_admin(b)
            b.active = False
            s.commit()
            return before

    before = await asyncio.to_thread(_apply)
    _audit(actor, "badge.definition.delete", f"badge:{key}", before=json.dumps(before))
    return jsonify({"ok": True})


@admin_bp.post("/admin/players/<int:player_id>/badges")
async def admin_award_badge(player_id: int):
    actor = await _require_superadmin()
    body = await json_body()
    badge_key = (body.get("badge_key") or "").strip()
    if not badge_key:
        abort_problem(422, "Invalid badge_key", "'badge_key' is required.")
    note = (body.get("note") or "").strip() or None

    def _apply():
        from db import Badge, Player

        from services.badges import award_badge

        with db_session() as s:
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if player is None:
                abort_problem(404, "Player not found", f"No player with id {player_id}.")
            b = s.query(Badge).filter(Badge.key == badge_key, Badge.active == True).first()  # noqa: E712
            if b is None:
                abort_problem(404, "Badge not found", f"No active badge with key '{badge_key}'.")
            context = {"note": note} if note else None
            award = award_badge(
                s, b, player_id, slot_key=f"p:{player_id}", context=context,
                awarded_by=actor,
            )
            if award is None:
                abort_problem(
                    409, "Already awarded",
                    f"Player {player_id} already holds an active '{badge_key}' badge.",
                )
            s.commit()
            return {
                "award_id": int(award.id),
                "badge_key": badge_key,
                "player_id": player_id,
                "player_name": player.player_name,
                "note": note,
                "_user_id": player.user_id,
            }

    after = await asyncio.to_thread(_apply)
    # Badges can carry entitlement grants (e.g. Bug Tester → supporter perks) —
    # bust the owner's cached entitlements so the change is immediate here.
    _invalidate_badge_user_entitlements(after.pop("_user_id", None))
    _audit(actor, "badge.award", f"player:{player_id}", after=json.dumps(after))
    return jsonify(after)


@admin_bp.delete("/admin/players/<int:player_id>/badges/<int:award_id>")
async def admin_revoke_badge(player_id: int, award_id: int):
    actor = await _require_superadmin()

    def _apply():
        from db import Badge, PlayerBadge

        from services.badges import revoke_badge

        with db_session() as s:
            award = (
                s.query(PlayerBadge)
                .filter(PlayerBadge.id == award_id, PlayerBadge.player_id == player_id)
                .first()
            )
            if award is None:
                abort_problem(404, "Award not found",
                              f"No award {award_id} for player {player_id}.")
            if award.status != "active":
                abort_problem(409, "Not active", "Only active awards can be revoked.")
            b = s.query(Badge).filter(Badge.badge_id == award.badge_id).first()
            before = {
                "award_id": award_id,
                "badge_key": b.key if b else None,
                "player_id": player_id,
                "status": award.status,
            }
            revoke_badge(s, award)
            s.commit()
            owner = s.query(Player).filter(Player.player_id == player_id).first()
            return before, (owner.user_id if owner else None)

    before, owner_user_id = await asyncio.to_thread(_apply)
    # Badge removal may withdraw an entitlement grant (Bug Tester → supporter
    # perks) — bust the owner's cached entitlements so it applies immediately.
    _invalidate_badge_user_entitlements(owner_user_id)
    _audit(actor, "badge.revoke", f"player:{player_id}", before=json.dumps(before))
    return jsonify({"ok": True})


def _invalidate_badge_user_entitlements(user_id) -> None:
    if user_id is None:
        return
    try:
        from db.entitlements import invalidate_user_entitlement_cache

        invalidate_user_entitlement_cache(int(user_id))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Personal-best NPC blocklist
#
# Some NPCs have no real personal best; the plugin still reports "kill times"
# for them, producing junk rows. Superadmins maintain a global blocklist of
# npc_ids — blocked NPCs are dropped at PB intake, and adding one permanently
# purges its existing rows. Storage + enforcement live in utils.pb_blocklist;
# a boss is always blocked/unblocked together with its variant ids (same name).
# --------------------------------------------------------------------------- #
def _group_npcs_by_name(s, npc_ids):
    """Group ids into ``[{name, npc_ids, pb_count}]`` by case-insensitive name."""
    from utils import pb_blocklist

    ids = sorted({int(i) for i in npc_ids})
    if not ids:
        return []
    rows = s.query(NpcList.npc_id, NpcList.npc_name).filter(NpcList.npc_id.in_(ids)).all()
    by_name: dict = {}
    seen: set = set()
    for nid, name in rows:
        seen.add(int(nid))
        by_name.setdefault((name or "").strip().lower(), {"name": name, "npc_ids": []})[
            "npc_ids"
        ].append(int(nid))
    for nid in ids:  # ids with no npc_list row — surface so they can be removed
        if nid not in seen:
            by_name[f"#{nid}"] = {"name": f"Unknown NPC #{nid}", "npc_ids": [nid]}
    out = []
    for g in by_name.values():
        g["npc_ids"] = sorted(g["npc_ids"])
        g["pb_count"] = pb_blocklist.pb_entry_count(s, g["npc_ids"])
        out.append(g)
    out.sort(key=lambda x: (x["name"] or "").lower())
    return out


def _body_npc_ids(body) -> list:
    raw = body.get("npc_ids")
    if raw is None and body.get("npc_id") is not None:
        raw = [body.get("npc_id")]
    if not isinstance(raw, list) or not raw:
        abort_problem(422, "Missing npc_id", "Provide 'npc_id' or a non-empty 'npc_ids' list.")
    out = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            abort_problem(422, "Invalid npc_id", f"'{v}' is not a valid npc id.")
    return out


@admin_bp.get("/admin/pb-blocks")
async def admin_pb_blocks_list():
    """Currently-blocked bosses (grouped by name, with remaining PB counts)."""
    await _require_moderator()

    def _load():
        from utils import pb_blocklist

        with db_session() as s:
            blocked = sorted(pb_blocklist.get_blocked_ids(s))
            return {"bosses": _group_npcs_by_name(s, blocked), "blocked_ids": blocked}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@admin_bp.get("/admin/pb-blocks/search")
async def admin_pb_blocks_search():
    """Search npc_list for bosses to block; annotate impact + current state."""
    await _require_moderator()
    q = (request.args.get("q") or "").strip()
    if not q:
        return private_no_store(jsonify({"results": []}))

    def _search():
        from utils import pb_blocklist

        like = f"%{q}%"
        with db_session() as s:
            rows = (
                s.query(NpcList.npc_id, NpcList.npc_name)
                .filter(NpcList.npc_name.ilike(like))
                .order_by(NpcList.npc_name)
                .limit(200)
                .all()
            )
            blocked = pb_blocklist.get_blocked_ids(s)
            by_name: dict = {}
            for nid, name in rows:
                by_name.setdefault((name or "").strip().lower(), {"name": name, "npc_ids": []})[
                    "npc_ids"
                ].append(int(nid))
            results = []
            for g in by_name.values():
                ids = sorted(g["npc_ids"])
                results.append(
                    {
                        "name": g["name"],
                        "npc_ids": ids,
                        "pb_count": pb_blocklist.pb_entry_count(s, ids),
                        "blocked": all(i in blocked for i in ids),
                    }
                )
            results.sort(key=lambda x: (-x["pb_count"], (x["name"] or "").lower()))
            return results[:25]

    return private_no_store(jsonify({"results": await asyncio.to_thread(_search)}))


@admin_bp.post("/admin/pb-blocks")
async def admin_pb_blocks_add():
    """Block a boss (all its variant ids) AND purge its existing PB rows.

    Destructive: requires ``confirm=true``; without it, returns 409 echoing the
    number of rows that would be deleted so the UI can hard-confirm first.
    """
    actor = await _require_moderator()
    body = await json_body()
    seed_ids = _body_npc_ids(body)
    confirm = bool(body.get("confirm"))

    def _apply():
        from utils import pb_blocklist

        with db_session() as s:
            ids = sorted(pb_blocklist.sibling_ids(s, seed_ids) or set(seed_ids))
            if not ids:
                abort_problem(404, "Unknown NPC", "No npc_list rows matched the given id(s).")
            bosses = _group_npcs_by_name(s, ids)
            pb_count = pb_blocklist.pb_entry_count(s, ids)
            if not confirm:
                abort_problem(
                    409,
                    "Confirmation required",
                    f"Blocking will permanently delete {pb_count} personal-best row(s). "
                    "Re-send with confirm=true.",
                )
            result = pb_blocklist.block_and_purge(s, ids)
            result["bosses"] = bosses
            return result

    result = await asyncio.to_thread(_apply)
    label = ", ".join(b["name"] for b in result.get("bosses", [])) or str(seed_ids)
    _audit(actor, "pb_block.add", label, after=f"deleted_pb={result.get('deleted_pb', 0)}")
    return jsonify({"ok": True, **result})


@admin_bp.delete("/admin/pb-blocks/<int:npc_id>")
async def admin_pb_blocks_remove(npc_id: int):
    """Unblock a boss (all its variant ids). Purged rows are NOT restored."""
    actor = await _require_moderator()

    def _apply():
        from utils import pb_blocklist

        with db_session() as s:
            ids = sorted(pb_blocklist.sibling_ids(s, [npc_id]) or {npc_id})
            bosses = _group_npcs_by_name(s, ids)
            result = pb_blocklist.unblock(s, ids)
            result["bosses"] = bosses
            return result

    result = await asyncio.to_thread(_apply)
    label = ", ".join(b["name"] for b in result.get("bosses", [])) or str(npc_id)
    _audit(actor, "pb_block.remove", label)
    return jsonify({"ok": True, **result})
