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
