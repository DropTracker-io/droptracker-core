"""Group setup checklist (web138a).

  GET /api/v1/groups/{id}/setup-status      group admins

One answer to "is this group actually working?", shared by the admin
Overview, the Discord setup panel and the Activity. Each check is
ok | warn | fail | unknown, with a ``fix`` key the client turns into a button.
Cheap by design (Redis lookups plus a handful of indexed queries), so admin
pages can show it on every load.

The checks follow the 2026-10-21 onboarding funnel: the bot missing from the
server, no or broken drops channel, and failing posts account for most groups
that never post to Discord.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from quart import Blueprint, jsonify

from web_api.common import db_session, private_no_store
from web_api.deps import assert_group_admin, current_user_id, manageable_guild_ids

group_setup_bp = Blueprint("v1_group_setup", __name__)

HIGH_THRESHOLD = 5_000_000
# A group this young is still onboarding: clients show the full checklist
# even when nothing is failing. Older groups only see it when something breaks.
ONBOARDING_DAYS = 14


def _redis():
    from utils.redis import redis_client

    return getattr(redis_client, "client", None)


def _bot_guild(s, group):
    """(server to report on, bot present?) across every server linked to the
    group, not just ``groups.guild_id`` (which goes stale when a group moves
    servers). Presence None when the bot's server list is unavailable."""
    from db import Guild
    from services.setup_alerts import _present_ids, bot_guild

    linked = [str(r.guild_id) for r in s.query(Guild.guild_id).filter(Guild.group_id == group.group_id)]
    try:
        present = _present_ids()
    except Exception:
        present = None
    return bot_guild(group.guild_id, linked, present)


def _channel_cache(conn, guild_id):
    if conn is None or not guild_id:
        return None
    try:
        raw = conn.get(f"guild:{guild_id}:channels")
        return {str(c.get("id")): c for c in json.loads(raw)} if raw else None
    except Exception:
        return None


def _tracking_members(conn, group_id: int) -> int:
    """Members with tracked loot this month (or last, early in a month), from
    the per-group Redis board the intake keeps."""
    if conn is None:
        return 0
    now = datetime.now()
    last = (now.replace(day=1) - timedelta(days=1))
    try:
        for part in (now.strftime("%Y%m"), last.strftime("%Y%m")):
            n = conn.zcard(f"leaderboard:{part}:group:{group_id}")
            if n:
                return int(n)
    except Exception:
        return 0
    return 0


def _status(s, group) -> dict:
    from sqlalchemy import func

    from db.models import GroupConfiguration, GroupSetupAlert, NotificationQueue, NotifiedSubmission, user_group_association
    from web_api.config_registry import parse_channel_ref

    gid = group.group_id
    conn = _redis()
    checks = []

    def add(key, status, title, detail=None, fix=None):
        checks.append({"key": key, "status": status, "title": title, "detail": detail, "fix": fix})

    # 1. bot in the server
    guild_id, present = _bot_guild(s, group)
    if not guild_id:
        add("bot", "fail", "No Discord server linked", "Link your server so drops can post there.", "link_server")
    elif present is None:
        add("bot", "unknown", "Couldn't check the bot right now", None, None)
    elif present:
        add("bot", "ok", "The DropTracker bot is in your server")
    else:
        add("bot", "fail", "The DropTracker bot isn't in your server",
            "Nothing can post to Discord until it's added back. Members' loot is still tracked.", "invite_bot")

    # 2. drops channel
    cfg = {
        r.config_key: (r.config_value or "")
        for r in s.query(GroupConfiguration.config_key, GroupConfiguration.config_value).filter(
            GroupConfiguration.group_id == gid,
            GroupConfiguration.config_key.in_(["channel_id_to_post_loot", "lootboard_channel_id", "minimum_value_to_notify"]),
        )
    }
    drops = (cfg.get("channel_id_to_post_loot") or "").strip()
    parsed = parse_channel_ref(drops)
    channels = _channel_cache(conn, guild_id)
    if not parsed:
        if drops and drops != "0":
            add("drops_channel", "fail", "Your drops channel setting isn't a channel",
                f"It's set to “{drops[:40]}”. Pick the channel again.", "pick_channel")
        else:
            add("drops_channel", "warn", "No drops channel chosen",
                "Your group works, but nothing posts to Discord until you pick one.", "pick_channel")
    elif channels is not None and parsed not in channels:
        add("drops_channel", "fail", "Your drops channel can't be found",
            "It was deleted, or the bot can't see it. Pick another or give the bot access.", "pick_channel")
    else:
        name = (channels or {}).get(parsed, {}).get("name")
        add("drops_channel", "ok", f"Drops post to #{name}" if name else "Drops channel set")

    # 3. recent posts
    last = (
        s.query(NotificationQueue.status, NotificationQueue.error_message, NotificationQueue.created_at)
        .filter(NotificationQueue.group_id == gid, NotificationQueue.status.in_(["sent", "failed"]))
        .order_by(NotificationQueue.id.desc())
        .first()
    )
    if last is None:
        add("posting", "unknown", "Nothing has tried to post yet",
            "The first post goes out when a member gets a drop above your minimum value.", None)
    elif last.status == "sent":
        add("posting", "ok", "Posts are reaching Discord")
    else:
        add("posting", "fail", "Your last post to Discord failed",
            (last.error_message or "")[:200] or None, "pick_channel")

    # 4. Wise Old Man + members
    members = (
        s.query(func.count(func.distinct(user_group_association.c.player_id)))
        .filter(user_group_association.c.group_id == gid)
        .scalar()
    ) or 0
    if not group.wom_id:
        add("wom", "warn", "No Wise Old Man group linked",
            "Linking it keeps your member list in sync automatically.", "link_wom")
    elif members == 0:
        add("wom", "warn", "No members synced yet",
            "Members join once they're in your Wise Old Man group. The sync runs hourly.", "sync_members")
    else:
        add("wom", "ok", f"{members:,} members synced")

    # 5. plugin use
    tracking = _tracking_members(conn, gid)
    if tracking:
        add("plugin", "ok", f"{tracking:,} members tracking loot this month")
    else:
        add("plugin", "warn", "No member is tracking loot yet",
            "Members need the DropTracker plugin from the RuneLite plugin hub.", "share_plugin")

    # 6. first post + threshold hint
    posted_week = s.query(NotifiedSubmission.id).filter(
        NotifiedSubmission.group_id == gid,
        NotifiedSubmission.date_added >= datetime.now() - timedelta(days=7),
    ).first() is not None
    try:
        min_value = int(float(cfg.get("minimum_value_to_notify") or 0))
    except ValueError:
        min_value = 0
    if tracking and not posted_week and min_value >= HIGH_THRESHOLD:
        add("threshold", "warn", "Your minimum drop value may be too high",
            f"Members are tracking loot, but nothing in the last week cleared {min_value:,} GP.", "minimum_value")

    alerts = [
        {"kind": a.kind, "since": a.first_seen_at.isoformat() if a.first_seen_at else None, "detail": a.detail}
        for a in s.query(GroupSetupAlert).filter(GroupSetupAlert.group_id == gid,
                                                 GroupSetupAlert.state.in_(["open", "baseline"]))
    ]
    bad = sum(1 for c in checks if c["status"] == "fail")
    # Onboarding = young, or never posted to Discord at all.
    young = bool(group.date_added and group.date_added >= datetime.now() - timedelta(days=ONBOARDING_DAYS))
    onboarding = young or (
        not posted_week
        and s.query(NotifiedSubmission.id).filter(NotifiedSubmission.group_id == gid).first() is None
    )
    return {
        "group_id": gid,
        "guild_id": guild_id,
        "onboarding": onboarding,
        "checks": checks,
        "alerts": alerts,
        "summary": {
            "ok": sum(1 for c in checks if c["status"] == "ok"),
            "total": len(checks),
            "failing": bad,
            # The one-line banner admin pages show when something is broken.
            "headline": next((c["title"] for c in checks if c["status"] == "fail"), None),
        },
    }


@group_setup_bp.get("/groups/<int:group_id>/setup-status")
async def setup_status(group_id: int):
    user_id = current_user_id()

    def _run():
        from db import Group

        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if group is None:
                from web_api.common import abort_problem

                abort_problem(404, "Not found", "No such group.")
            return _status(s, group)

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


# --------------------------------------------------------------------------- #
# Features: what the group can switch on (db/group_features.py)
#
#   GET  /groups/{id}/features                 group admins
#   POST /groups/{id}/features/{key}           {channel_id?} switch one on
#   POST /groups/{id}/member-guide             {channel_id}  post the guide
# --------------------------------------------------------------------------- #
def _load_group_or_404(s, group_id: int):
    from db import Group
    from web_api.common import abort_problem

    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        abort_problem(404, "Not found", "No such group.")
    return group


@group_setup_bp.get("/groups/<int:group_id>/features")
async def group_features(group_id: int):
    user_id = current_user_id()

    def _run():
        from db.group_features import feature_status

        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            _load_group_or_404(s, group_id)
            features = feature_status(s, group_id)
            return {
                "group_id": group_id,
                "features": features,
                "summary": {
                    "on": sum(1 for f in features if f["status"] == "on"),
                    "total": len(features),
                },
            }

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


def enable_group_feature(user_id: int, group_id: int, key: str, channel_id) -> None:
    """Switch one catalog feature on, through the same writer the settings
    page (or, for clan points, the points page) uses. Raises ProblemException."""
    from db.group_features import POINTS_ENABLED_KEY, enable_updates, get_feature
    from web_api.common import abort_problem

    if get_feature(key) is None:
        abort_problem(404, "Unknown feature", f"'{key}' is not a feature.")
    try:
        updates = enable_updates(key, channel_id)
    except ValueError as e:
        abort_problem(422, "Can't switch this on here", str(e))

    if POINTS_ENABLED_KEY in updates:
        from db.point_standings import invalidate_points_active_cache
        from web_api.routes.points import _admin_ctx, _assert_group_exists, _audit, _write_config

        with db_session() as s:
            user = _admin_ctx(s, user_id, group_id, entitlement=True)
            group = _assert_group_exists(s, group_id)
            _write_config(s, group_id, POINTS_ENABLED_KEY, "1")
            _audit(s, action="settings_update", actor=user, group=group,
                   message=f"Points settings updated for {group.group_name}",
                   details={"changed": {"behavior": {POINTS_ENABLED_KEY: True}}, "via": "features"})
            s.commit()
        invalidate_points_active_cache(group_id)
        updates = {k: v for k, v in updates.items() if k != POINTS_ENABLED_KEY}

    if updates:
        from web_api.routes.config import apply_group_config_patch

        apply_group_config_patch(user_id, group_id, updates)


@group_setup_bp.post("/groups/<int:group_id>/features/<key>")
async def post_group_feature(group_id: int, key: str):
    from web_api.deps import json_body

    user_id = current_user_id()
    body = await json_body(required=False)
    channel_id = (body or {}).get("channel_id")
    await asyncio.to_thread(enable_group_feature, user_id, group_id, key, channel_id)

    def _reload():
        from db.group_features import feature_status

        with db_session() as s:
            return next((f for f in feature_status(s, group_id) if f["key"] == key), None)

    return private_no_store(jsonify({"ok": True, "feature": await asyncio.to_thread(_reload)}))


@group_setup_bp.post("/groups/<int:group_id>/member-guide")
async def post_member_guide(group_id: int):
    """Queue the members' getting-started guide for a channel. The bot sends
    it from the outbox (web_api never talks to Discord)."""
    from web_api.common import abort_problem
    from web_api.config_registry import parse_channel_ref
    from web_api.deps import json_body

    user_id = current_user_id()
    body = await json_body(required=False)
    channel_id = parse_channel_ref((body or {}).get("channel_id"))
    if not channel_id:
        abort_problem(422, "Pick a channel", "Choose the channel to post the guide in.")

    def _run():
        from services.discord_outbox import enqueue
        from utils.member_guide import guide_buttons, guide_embed

        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            group = _load_group_or_404(s, group_id)
            # Only a channel in one of the group's own servers: an admin of
            # this group must not be able to make the bot post elsewhere.
            from db import Guild

            conn = _redis()
            guild_ids = {str(r.guild_id) for r in s.query(Guild.guild_id).filter(Guild.group_id == group_id)}
            if group.guild_id:
                guild_ids.add(str(group.guild_id))
            known = [c for g in guild_ids if (c := _channel_cache(conn, g))]
            if not known:
                abort_problem(409, "Channels not loaded yet",
                              "We couldn't load your server's channels. Try again in a minute.")
            if not any(str(channel_id) in c for c in known):
                abort_problem(422, "Not your channel", "That channel isn't in your clan's Discord server.")
            enqueue(
                s,
                channel_id=str(channel_id),
                embed=guide_embed(group.group_name),
                components=guide_buttons(group_id),
                kind="message",
                ref_type="member_guide",
                ref_id=group_id,
                actor_user_id=user_id,
            )
            return {"ok": True, "channel_id": str(channel_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_run)))
