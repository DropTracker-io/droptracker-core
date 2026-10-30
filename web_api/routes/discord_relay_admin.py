"""Staff management of Discord delivery relay integrations (web153a).

  GET    /api/v1/admin/discord-relay                 - every integration + live stats (developer)
  POST   /api/v1/admin/discord-relay                 - create one; mints its key, or binds
                                                       the group's existing one (superadmin)
  PATCH  /api/v1/admin/discord-relay/{id}            - edit / pause / resume (superadmin)
  POST   /api/v1/admin/discord-relay/{id}/rotate-key - revoke the key, mint a new one (superadmin)
  DELETE /api/v1/admin/discord-relay/{id}            - remove it and revoke its key (superadmin)
  GET    /api/v1/admin/discord-relay/{id}/log        - recent delivery outcomes (developer)

Staff-only on purpose: the relay is not offered to users yet. An
integration starts **paused** — DropTracker's own bot keeps posting until
staff flip it to active once the customer's bot is polling. Pausing is the
instant fallback; every change bumps ``relay:routes:version`` so the core
bot's routing cache drops within one send.

The key is an ordinary group-scope data-API key, minted here and shown once.
No Discord bot token is ever taken or stored.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError

from db import api_keys as keys
from web_api.common import _rc, abort_problem, db_session
from web_api.deps import assert_developer, assert_superadmin, current_user_id, load_user

discord_relay_admin_bp = Blueprint("v1_discord_relay_admin", __name__)

#: Kept in step with services.discord_relay_core (asserted by the unit tests):
#: web_api must not import services.* at module level.
FEATURES = ("events", "notifications")
STATUSES = ("active", "paused")
ROUTES_VERSION_KEY = "relay:routes:version"
HOLD_MINUTES_RANGE = (5, 7 * 24 * 60)


def _audit(actor_user_id, action, target, before=None, after=None) -> None:
    try:
        from db.models import AuditLog

        with db_session() as s:
            s.add(AuditLog(actor_user_id=actor_user_id, group_id=None,
                           action=action, target=target, before=before, after=after))
            s.commit()
    except Exception:
        pass


def _bump_routes() -> None:
    conn = _rc()
    if conn is None:
        return
    try:
        conn.incr(ROUTES_VERSION_KEY)
    except Exception:
        pass


def _stats(integration_id: int) -> dict:
    conn = _rc()
    if conn is None:
        return {}
    try:
        from services import discord_relay_core as core

        return core.stats(conn, integration_id)
    except Exception:
        return {}


def _serialize(row, guild_ids, key_row=None, include_stats=True) -> dict:
    payload = {
        "id": int(row.id),
        "group_id": int(row.group_id),
        "label": row.label or "",
        "application_id": row.application_id,
        "guild_ids": sorted(str(g) for g in guild_ids),
        "features": [f for f in (row.features or []) if f in FEATURES],
        "status": row.status,
        "hold_minutes": int(row.hold_minutes or 360),
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "api_key": None,
    }
    if key_row is not None:
        payload["api_key"] = {
            "id": int(key_row.id),
            "display": f"dtk_{key_row.id}_{key_row.token_prefix}...",
            "revoked": key_row.revoked_at is not None,
            "last_used_at": key_row.last_used_at.isoformat() if key_row.last_used_at else None,
        }
    if include_stats:
        payload["stats"] = _stats(row.id)
    return payload


def _clean_guild_ids(raw):
    if raw is None:
        return None
    if not isinstance(raw, list):
        return "bad"
    out = []
    for g in raw:
        g = str(g).strip()
        if not g.isdigit() or len(g) > 32:
            return "bad"
        if g not in out:
            out.append(g)
    return out


def _clean_fields(body: dict) -> tuple:
    """``(changes, error)`` from a create/patch body (only keys present)."""
    changes = {}
    if "label" in body:
        changes["label"] = str(body.get("label") or "")[:64]
    if "application_id" in body:
        app_id = str(body.get("application_id") or "").strip()
        if app_id and (not app_id.isdigit() or len(app_id) > 32):
            return None, "application_id must be a Discord snowflake"
        changes["application_id"] = app_id or None
    if "features" in body:
        feats = body.get("features")
        if not isinstance(feats, list) or any(f not in FEATURES for f in feats):
            return None, f"features: a list drawn from {', '.join(FEATURES)}"
        changes["features"] = list(dict.fromkeys(feats))
    if "status" in body:
        if body.get("status") not in STATUSES:
            return None, f"status must be one of {', '.join(STATUSES)}"
        changes["status"] = body["status"]
    if "hold_minutes" in body:
        try:
            hold = int(body.get("hold_minutes"))
        except (TypeError, ValueError):
            return None, "hold_minutes must be an integer"
        lo, hi = HOLD_MINUTES_RANGE
        if not lo <= hold <= hi:
            return None, f"hold_minutes must be between {lo} and {hi}"
        changes["hold_minutes"] = hold
    if "notes" in body:
        changes["notes"] = (str(body.get("notes") or "")[:2000]) or None
    return changes, None


def _load(session, integration_id: int):
    from db.models import ApiKey, DiscordRelayGuild, DiscordRelayIntegration

    row = session.query(DiscordRelayIntegration).filter(
        DiscordRelayIntegration.id == integration_id).first()
    if row is None:
        return None, [], None
    guilds = [g for (g,) in session.query(DiscordRelayGuild.guild_id).filter(
        DiscordRelayGuild.integration_id == row.id).all()]
    key_row = (session.query(ApiKey).filter(ApiKey.id == row.api_key_id).first()
               if row.api_key_id else None)
    return row, guilds, key_row


def _mint(session, row, actor_user_id):
    row_key, token = keys.create_key(
        session, owner_user_id=None, group_id=int(row.group_id), scope="group",
        label=f"Discord relay: {row.label or row.id}"[:64],
        tier_key=keys.DEFAULT_TIER, created_by_user_id=actor_user_id,
    )
    session.flush()
    row.api_key_id = row_key.id
    return row_key, token


def _set_guilds(session, row, guild_ids) -> None:
    from db.models import DiscordRelayGuild

    session.query(DiscordRelayGuild).filter(
        DiscordRelayGuild.integration_id == row.id).delete(synchronize_session=False)
    session.flush()
    for g in guild_ids:
        session.add(DiscordRelayGuild(integration_id=row.id, guild_id=g))
    session.flush()


@discord_relay_admin_bp.route("/admin/discord-relay", methods=["GET"])
async def list_integrations():
    user_id = current_user_id()

    def work():
        from db.models import DiscordRelayIntegration, Group

        with db_session() as session:
            assert_developer(load_user(session, user_id))
            rows = session.query(DiscordRelayIntegration).order_by(
                DiscordRelayIntegration.id.desc()).all()
            out = []
            for r in rows:
                _row, guilds, key_row = _load(session, r.id)
                item = _serialize(r, guilds, key_row)
                grp = session.query(Group.group_name).filter(
                    Group.group_id == r.group_id).first()
                item["group_name"] = grp[0] if grp else None
                out.append(item)
            return {"integrations": out, "features": list(FEATURES)}

    return jsonify(await asyncio.to_thread(work))


@discord_relay_admin_bp.route("/admin/discord-relay", methods=["POST"])
async def create_integration():
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}

    def work():
        from db.models import DiscordRelayIntegration, Group

        with db_session() as session:
            assert_superadmin(load_user(session, user_id))
            try:
                group_id = int(body.get("group_id"))
            except (TypeError, ValueError):
                return None, "group_id is required"
            if session.query(Group.group_id).filter(Group.group_id == group_id).first() is None:
                return None, "no such group"
            guild_ids = _clean_guild_ids(body.get("guild_ids") or [])
            if guild_ids == "bad" or not guild_ids:
                return None, "guild_ids: at least one Discord server id"
            changes, problem = _clean_fields(body)
            if problem:
                return None, problem
            row = DiscordRelayIntegration(
                group_id=group_id,
                label=changes.get("label", ""),
                application_id=changes.get("application_id"),
                features=changes.get("features") or ["events"],
                # Always starts paused: our bot keeps posting until staff
                # confirm the customer's bot is polling.
                status="paused",
                hold_minutes=changes.get("hold_minutes", 360),
                notes=changes.get("notes"),
                created_by_user_id=user_id,
            )
            session.add(row)
            session.flush()
            try:
                _set_guilds(session, row, guild_ids)
            except IntegrityError:
                session.rollback()
                return None, "conflict"
            if body.get("api_key_id") is not None:
                # Bind the group's existing key (a bot already reading the v2
                # API keeps one credential). It must be the same group's,
                # live, and not bound to another integration.
                from db.models import ApiKey

                try:
                    key_id = int(body.get("api_key_id"))
                except (TypeError, ValueError):
                    session.rollback()
                    return None, "api_key_id must be an integer"
                key_row = session.query(ApiKey).filter(ApiKey.id == key_id).first()
                if (key_row is None or key_row.revoked_at is not None
                        or key_row.scope != "group" or key_row.group_id != group_id):
                    session.rollback()
                    return None, "api_key_id must be a live key of this group"
                if session.query(DiscordRelayIntegration.id).filter(
                        DiscordRelayIntegration.api_key_id == key_id).first():
                    session.rollback()
                    return None, "that key is already bound to a relay integration"
                row.api_key_id = key_id
                token = None
            else:
                key_row, token = _mint(session, row, user_id)
            session.commit()
            payload = _serialize(row, guild_ids, key_row, include_stats=False)
            if token:
                payload["token"] = token
                payload["warning"] = ("This token is shown once and cannot be retrieved "
                                      "again. Give it to the bot's operator now.")
            return payload, None

    payload, error = await asyncio.to_thread(work)
    if error == "conflict":
        abort_problem(409, "Conflict",
                      "One of those servers already belongs to another relay integration.")
    if error:
        abort_problem(400, "Bad Request", error)
    _audit(user_id, "discord_relay.create", f"discord_relay_integrations.{payload['id']}",
           after=json.dumps({k: payload[k] for k in ("group_id", "guild_ids", "features",
                                                    "application_id")}))
    _bump_routes()
    return jsonify(payload), 201


@discord_relay_admin_bp.route("/admin/discord-relay/<int:integration_id>", methods=["PATCH"])
async def update_integration(integration_id: int):
    user_id = current_user_id()
    body = await request.get_json(silent=True) or {}

    def work():
        with db_session() as session:
            assert_superadmin(load_user(session, user_id))
            row, guilds, key_row = _load(session, integration_id)
            if row is None:
                return None, None, "not_found"
            before = _serialize(row, guilds, key_row, include_stats=False)
            changes, problem = _clean_fields(body)
            if problem:
                return None, None, problem
            if changes.get("status") == "active":
                if key_row is None or key_row.revoked_at is not None:
                    return None, None, "an active integration needs a live key"
                if not (changes.get("application_id", row.application_id)):
                    return None, None, ("set application_id first: forwarded clicks are "
                                        "checked against it")
            for field, value in changes.items():
                setattr(row, field, value)
            new_guilds = _clean_guild_ids(body.get("guild_ids")) if "guild_ids" in body else None
            if new_guilds == "bad" or new_guilds == []:
                return None, None, "guild_ids: at least one Discord server id"
            if new_guilds is not None:
                try:
                    _set_guilds(session, row, new_guilds)
                except IntegrityError:
                    session.rollback()
                    return None, None, "conflict"
                guilds = new_guilds
            session.commit()
            return before, _serialize(row, guilds, key_row, include_stats=False), None

    before, after, error = await asyncio.to_thread(work)
    if error == "not_found":
        abort_problem(404, "Not Found", "No such relay integration.")
    if error == "conflict":
        abort_problem(409, "Conflict",
                      "One of those servers already belongs to another relay integration.")
    if error:
        abort_problem(400, "Bad Request", error)
    _audit(user_id, "discord_relay.update", f"discord_relay_integrations.{integration_id}",
           before=json.dumps(before), after=json.dumps(after))
    _bump_routes()
    return jsonify(after)


@discord_relay_admin_bp.route("/admin/discord-relay/<int:integration_id>/rotate-key",
                              methods=["POST"])
async def rotate_key(integration_id: int):
    user_id = current_user_id()

    def work():
        from datetime import datetime

        with db_session() as session:
            assert_superadmin(load_user(session, user_id))
            row, guilds, key_row = _load(session, integration_id)
            if row is None:
                return None
            if key_row is not None and key_row.revoked_at is None:
                key_row.revoked_at = datetime.utcnow()
            row.api_key_id = None
            session.flush()
            new_key, token = _mint(session, row, user_id)
            session.commit()
            payload = _serialize(row, guilds, new_key, include_stats=False)
            payload["token"] = token
            payload["warning"] = ("This token is shown once and cannot be retrieved "
                                  "again. The previous key no longer works.")
            return payload

    payload = await asyncio.to_thread(work)
    if payload is None:
        abort_problem(404, "Not Found", "No such relay integration.")
    _audit(user_id, "discord_relay.rotate_key", f"discord_relay_integrations.{integration_id}")
    _bump_routes()
    return jsonify(payload)


@discord_relay_admin_bp.route("/admin/discord-relay/<int:integration_id>", methods=["DELETE"])
async def delete_integration(integration_id: int):
    user_id = current_user_id()

    def work():
        from datetime import datetime

        from db.models import DiscordRelayGuild

        with db_session() as session:
            assert_superadmin(load_user(session, user_id))
            row, guilds, key_row = _load(session, integration_id)
            if row is None:
                return None
            before = _serialize(row, guilds, key_row, include_stats=False)
            if key_row is not None and key_row.revoked_at is None:
                key_row.revoked_at = datetime.utcnow()
            session.query(DiscordRelayGuild).filter(
                DiscordRelayGuild.integration_id == row.id).delete(synchronize_session=False)
            session.delete(row)
            session.commit()
            return before

    before = await asyncio.to_thread(work)
    if before is None:
        abort_problem(404, "Not Found", "No such relay integration.")
    _audit(user_id, "discord_relay.delete", f"discord_relay_integrations.{integration_id}",
           before=json.dumps(before))
    _bump_routes()
    return jsonify({"deleted": True})


@discord_relay_admin_bp.route("/admin/discord-relay/<int:integration_id>/log", methods=["GET"])
async def integration_log(integration_id: int):
    user_id = current_user_id()

    def work():
        with db_session() as session:
            assert_developer(load_user(session, user_id))
            row, _guilds, _key = _load(session, integration_id)
            return row is not None

    if not await asyncio.to_thread(work):
        abort_problem(404, "Not Found", "No such relay integration.")
    conn = _rc()
    entries, stats = [], {}
    if conn is not None:
        from services import discord_relay_core as core

        entries = core.recent_log(conn, integration_id, 100)
        stats = core.stats(conn, integration_id)
    return jsonify({"entries": entries, "stats": stats})
