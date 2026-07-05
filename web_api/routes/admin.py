"""Task 12 — superadmin surfaces.

Every endpoint independently enforces superadmin (403 otherwise):

  GET  /api/v1/admin/services
  POST /api/v1/admin/services/{unit}            { action: start|stop|restart }
  GET  /api/v1/admin/services/{unit}/logs
  POST /api/v1/admin/discord/send               { channel_id, content }
  GET  /api/v1/admin/lookup?q=
  POST   /api/v1/admin/subscriptions/tiers      { SubscriptionTier }
  PATCH  /api/v1/admin/subscriptions/tiers/{key}
  DELETE /api/v1/admin/subscriptions/tiers/{key}
  GET  /api/v1/admin/audit?action=&actor_user_id=&group_id=&q=&page=&limit=
  GET  /api/v1/admin/users/{id}/overview
  POST /api/v1/admin/users/{id}/superadmin       { grant: bool }
  GET    /api/v1/admin/badges
  POST   /api/v1/admin/badges                    { key, name, ... } (upsert)
  DELETE /api/v1/admin/badges/{key}              (soft delete)
  POST   /api/v1/admin/players/{id}/badges       { badge_key, note? }
  DELETE /api/v1/admin/players/{id}/badges/{award_id}

Service control is whitelisted to three units and shells out via systemctl /
journalctl with **no** user input interpolated into the command. No SQL executor
is provided (deliberately omitted, §9/§14.1). No direct Discord connection —
messages go through the outbox for the bot to send.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta

from sqlalchemy import or_

from quart import Blueprint, jsonify, request

from db import (
    Announcement,
    AuditLog,
    DiscordOutbox,
    Drop,
    Group,
    GroupAdmin,
    GroupConfiguration,
    GroupSubscription,
    ItemList,
    Log,
    NotificationQueue,
    NpcList,
    Player,
    SubscriptionTier,
    User,
)
from web_api import admin_registry as registry
from web_api import billing
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import assert_superadmin, current_user_id, json_body, load_user
from web_api.entitlements_registry import (
    EntitlementValidationError,
    entitlements_to_storage,
    validate_entitlements_input,
)
from web_api.routes.subscriptions import _serialize_sub

admin_bp = Blueprint("v1_admin", __name__)

# Whitelisted units the superadmin may control (contract SERVICE_UNITS).
SERVICE_UNITS = {"droptracker-core", "droptracker-api", "droptracker-webhooks", "droptracker-webapi"}
SERVICE_NAMES = {
    "droptracker-core": "Discord bot (core)",
    "droptracker-api": "RuneLite intake API",
    "droptracker-webhooks": "Webhook reader bot",
    "droptracker-webapi": "Web API (this backend)",
}
_MAX_LOG_LINES = 200


async def _require_superadmin() -> int:
    user_id = current_user_id()

    def _check():
        with db_session() as s:
            assert_superadmin(load_user(s, user_id))

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
    code, out, _ = _run([
        "systemctl", "show", unit,
        "--property=ActiveState,SubState,ActiveEnterTimestampMonotonic,ActiveEnterTimestamp",
    ])
    active_state = "unknown"
    since = None
    if code == 0 and out:
        props = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        active_state = props.get("ActiveState", "unknown")
        ts = props.get("ActiveEnterTimestamp", "")
        if ts:
            for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
                try:
                    since = int(datetime.strptime(ts.strip(), fmt).timestamp())
                    break
                except Exception:
                    continue
    status_map = {"active": "running", "inactive": "stopped", "failed": "failed"}
    status = status_map.get(active_state, "unknown")
    return {
        "unit": unit,
        "name": SERVICE_NAMES.get(unit, unit),
        "status": status,
        "active": active_state == "active",
        "since": since,
    }


@admin_bp.get("/admin/services")
async def admin_services():
    await _require_superadmin()
    statuses = await asyncio.to_thread(
        lambda: [_service_status(u) for u in sorted(SERVICE_UNITS)]
    )
    return private_no_store(jsonify(statuses))


@admin_bp.post("/admin/services/<unit>")
async def admin_service_action(unit: str):
    actor = await _require_superadmin()
    if unit not in SERVICE_UNITS:
        abort_problem(404, "Unknown unit", "That service is not managed.")
    body = await json_body()
    action = body.get("action")
    if action not in ("start", "stop", "restart"):
        abort_problem(422, "Invalid action", "action must be start|stop|restart.")

    # Guard: stopping the intake API halts submission processing, and stopping
    # the web API kills the backend serving this dashboard (no UI to restart it).
    if action == "stop" and unit in ("droptracker-api", "droptracker-webapi") and not body.get("confirm"):
        abort_problem(409, "Confirmation required", "Stopping this service requires confirm:true.")

    def _do():
        # Try direct systemctl, then sudo -n (non-interactive) as fallback.
        code, _out, err = _run(["systemctl", action, unit], timeout=20)
        if code != 0:
            code, _out, err = _run(["sudo", "-n", "systemctl", action, unit], timeout=20)
        return code, err

    code, err = await asyncio.to_thread(_do)
    _audit(actor, f"service.{action}", unit, after="ok" if code == 0 else err[:200])
    if code != 0:
        abort_problem(502, "Service action failed", err[:200] or "systemctl error")
    return jsonify({"ok": True})


@admin_bp.get("/admin/services/<unit>/logs")
async def admin_service_logs(unit: str):
    await _require_superadmin()
    if unit not in SERVICE_UNITS:
        abort_problem(404, "Unknown unit", "That service is not managed.")

    def _logs():
        code, out, err = _run(
            ["journalctl", "-u", unit, "-n", str(_MAX_LOG_LINES), "--no-pager"], timeout=15
        )
        if code != 0:
            code, out, err = _run(
                ["sudo", "-n", "journalctl", "-u", unit, "-n", str(_MAX_LOG_LINES), "--no-pager"],
                timeout=15,
            )
        text = out if code == 0 else (err or "logs unavailable")
        return [ln for ln in text.splitlines()][-_MAX_LOG_LINES:]

    lines = await asyncio.to_thread(_logs)
    return private_no_store(jsonify({"unit": unit, "lines": lines}))


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
    features = body.get("features")
    features_json = json.dumps(features) if isinstance(features, list) else (
        existing.features if existing else "[]"
    )
    entitlements_json = None
    if "entitlements" in body:
        try:
            validated = validate_entitlements_input(body.get("entitlements") or {})
            entitlements_json = entitlements_to_storage(validated)
        except EntitlementValidationError as e:
            abort_problem(422, "Invalid entitlements", e.detail)
    elif existing is not None:
        entitlements_json = existing.entitlements
    return key, features_json, entitlements_json


@admin_bp.post("/admin/subscriptions/tiers")
async def create_tier():
    actor = await _require_superadmin()
    body = await json_body()

    def _apply():
        with db_session() as s:
            key, features_json, entitlements_json = _tier_from_body(body)
            if s.query(SubscriptionTier).filter(SubscriptionTier.key == key).first():
                abort_problem(409, "Tier exists", f"Tier '{key}' already exists.")
            tier = SubscriptionTier(
                key=key,
                name=body.get("name") or key,
                description=body.get("description"),
                price_cents=int(body.get("price_cents") or 0),
                currency=body.get("currency") or "USD",
                interval=body.get("interval") or "month",
                features=features_json,
                entitlements=entitlements_json,
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
                    validated = validate_entitlements_input(body.get("entitlements") or {})
                    tier.entitlements = entitlements_to_storage(validated)
                except EntitlementValidationError as e:
                    abort_problem(422, "Invalid entitlements", e.detail)
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
                    s.query(GroupSubscription).filter(GroupSubscription.status == "active")
                ),
            })
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
            sub = (
                s.query(GroupSubscription)
                .filter(GroupSubscription.group_id == group_id)
                .first()
            )
            before = _serialize_sub(group_id, sub) if sub else None
            if not sub:
                sub = GroupSubscription(group_id=group_id)
                s.add(sub)
            sub.provider = grant["provider"]
            sub.status = grant["status"]
            sub.tier_key = grant["tier_key"]
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
            sub = (
                s.query(GroupSubscription)
                .filter(GroupSubscription.group_id == group_id)
                .first()
            )
            if not sub or sub.status == "none":
                abort_problem(404, "No subscription", "This group has no subscription.")
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
    "drop_channel_id",
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

            sub = (
                s.query(GroupSubscription)
                .filter(GroupSubscription.group_id == group_id)
                .first()
            )
            subscription = _serialize_sub(group_id, sub)

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
            if not cfg_map.get("drop_channel_id"):
                warnings.append("No drop_channel_id configured — drop notifications won't post.")
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
            }

    after = await asyncio.to_thread(_apply)
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
            return before

    before = await asyncio.to_thread(_apply)
    _audit(actor, "badge.revoke", f"player:{player_id}", before=json.dumps(before))
    return jsonify({"ok": True})
