"""Task 09 — announcements + Discord syndication.

Public reads (cached):
  GET /api/v1/announcements?scope=global|group:{id}&cursor=  -> AnnouncementPage
  GET /api/v1/announcements/{id}                              -> Announcement

Writes (session + authorization):
  POST   /api/v1/groups/{groupId}/announcements  (group admin) -> { id, status }
  POST   /api/v1/announcements                    (superadmin)  -> { id, status }
  PATCH  /api/v1/announcements/{id}               (author/admin)-> Announcement
  DELETE /api/v1/announcements/{id}               (author/admin)-> { ok }

Owner review for global posts (web129a, ``services/leader_updates.py``):
  GET    /api/v1/announcements/review             (superadmin)  -> { items, can_approve }
  POST   /api/v1/announcements/{id}/approve       (approver)    -> Announcement

A global post from anyone but an approver is saved as a draft (status
``draft``) and published only when an approver approves it, edits included.

Web pages are canonical; Discord is a syndication target via the outbox
(``services/discord_outbox.py``) — never a direct Discord call from the API.
Publishing also emits a realtime ``announcement`` event.

Group posts can also go out in game (``send_in_game``): a short message lands
in every member's plugin inbox (``services/plugin_notifications.
push_clan_announcement``), at most :data:`IN_GAME_DAILY_LIMIT` per group a day.
The Discord embed takes the same options the embed builder offers (colour,
link, thumbnail, image, footer, timestamp). A group post needs the group's
announcements channel to go to Discord: asking for it without one is refused.
Editing a group post edits its Discord message (title, body, image; the rest
of the embed as posted) and archiving deletes it
(``services/announcement_discord``).
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

from quart import Blueprint, jsonify, request

from db import Announcement, Group, User
from web_api.common import abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import (
    assert_group_admin,
    assert_superadmin,
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
    resolve_group_role,
)
from web_api.routes.auth import get_cached_profile

announcements_bp = Blueprint("v1_announcements", __name__)

PAGE_SIZE = 20


def _author_name(s, user_id):
    if user_id is None:
        return "Staff"
    profile = get_cached_profile(user_id)
    if profile.get("display_name"):
        return profile["display_name"]
    user = s.query(User).filter(User.user_id == user_id).first()
    if user and user.username:
        return user.username
    return "Staff"


def _serialize(ann: Announcement, author_name: str) -> dict:
    return {
        "id": ann.id,
        "scope_type": ann.scope_type,
        "group_id": ann.group_id,
        "title": ann.title,
        "body_md": ann.body_md,
        "cover_image_url": ann.cover_image_url,
        "pinned": bool(ann.pinned),
        "author_name": author_name,
        "published_at": int(ann.published_at.timestamp()) if ann.published_at else 0,
    }


# --------------------------------------------------------------------------- #
# Public reads
# --------------------------------------------------------------------------- #
@announcements_bp.get("/announcements")
async def list_announcements():
    scope = (request.args.get("scope") or "global").strip()
    try:
        offset = max(0, int(request.args.get("cursor") or 0))
    except (ValueError, TypeError):
        offset = 0

    group_id = None
    if scope.startswith("group:"):
        try:
            group_id = int(scope.split(":", 1)[1])
        except (ValueError, TypeError):
            group_id = None
        scope_type = "group"
    else:
        scope_type = "global"

    def _load():
        with db_session() as s:
            q = s.query(Announcement).filter(
                Announcement.status == "published",
                Announcement.scope_type == scope_type,
            )
            if scope_type == "group":
                q = q.filter(Announcement.group_id == group_id)
            q = q.order_by(
                Announcement.pinned.desc(),
                Announcement.published_at.desc(),
                Announcement.id.desc(),
            )
            rows = q.offset(offset).limit(PAGE_SIZE + 1).all()
            has_more = len(rows) > PAGE_SIZE
            rows = rows[:PAGE_SIZE]
            items = [_serialize(a, _author_name(s, a.author_user_id)) for a in rows]
            if scope_type == "group" and rows:
                # Where each post stands on Discord, for the admin list. A
                # classified reason only, never the bot's raw error text.
                try:
                    from services.announcement_discord import discord_states

                    states = discord_states(s, rows)
                    for item in items:
                        if item["id"] in states:
                            item["discord"] = states[item["id"]]
                except Exception as e:
                    print(f"[announcements] discord state lookup failed: {e}")
            next_cursor = str(offset + PAGE_SIZE) if has_more else None
            return {"items": items, "next_cursor": next_cursor}

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=30)


@announcements_bp.get("/announcements/<int:announcement_id>")
async def get_announcement(announcement_id: int):
    def _load():
        with db_session() as s:
            ann = (
                s.query(Announcement)
                .filter(Announcement.id == announcement_id, Announcement.status == "published")
                .first()
            )
            if not ann:
                return None
            return _serialize(ann, _author_name(s, ann.author_user_id))

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Announcement not found", f"No published announcement {announcement_id}.")
    return with_cache_headers(jsonify(payload), max_age=30)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def _validate_input(body: dict, *, allow_empty_body: bool = False):
    """``(title, body_md)``. The body may be left empty only for a group post
    that goes in game (``allow_empty_body``): the one-line in-game message,
    or the title, then stands in for it on the site and in Discord
    (suggestion #247 — a chatbox reminder needs no Markdown write-up)."""
    title = (body.get("title") or "").strip()
    body_md = (body.get("body_md") or "").strip()
    if not (1 <= len(title) <= 200):
        abort_problem(422, "Invalid title", "Title must be 1–200 characters.")
    if not body_md:
        if not allow_empty_body:
            abort_problem(422, "Invalid body", "body_md must not be empty.")
        body_md = " ".join(str(body.get("in_game_message") or "").split()) or title
    return title, body_md


_MAX_PING_IDS = 10


def _validate_ping_ids(values, what: str) -> list[str]:
    """Snowflake-id list from the ping pickers (strings; JS numbers lose
    precision past 2^53). Empty/absent → []."""
    if values is None:
        return []
    if not isinstance(values, list):
        abort_problem(422, f"Invalid {what}", f"'{what}' must be a list of Discord id strings.")
    out: list[str] = []
    for v in values:
        v = str(v).strip()
        if not v.isdigit() or len(v) > 32:
            abort_problem(422, f"Invalid {what}", f"'{v}' is not a Discord snowflake id.")
        if v not in out:
            out.append(v)
    if len(out) > _MAX_PING_IDS:
        abort_problem(422, f"Too many {what}", f"At most {_MAX_PING_IDS} {what} per announcement.")
    return out


def _ping_content(role_ids: list[str], user_ids: list[str], everyone: bool) -> str | None:
    """Mentions must live in the message CONTENT — Discord never pings from
    embed text. Returns the content line, or None when nothing is pinged."""
    parts: list[str] = []
    if everyone:
        parts.append("@everyone")
    parts.extend(f"<@&{rid}>" for rid in role_ids)
    parts.extend(f"<@{uid}>" for uid in user_ids)
    return " ".join(parts) if parts else None


# In-game sends per group per rolling day: a clan-wide pop-up is loud, and a
# mistake can't be recalled once it is in members' inboxes.
IN_GAME_DAILY_LIMIT = 3
_IN_GAME_WINDOW_SECONDS = 24 * 3600
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_EMBED_COLOR = 0xC8AA6E


def _in_game_key(group_id) -> str:
    return f"announcements:in_game:{int(group_id)}"


def _in_game_sends_today(group_id) -> list:
    """Timestamps of this group's in-game sends inside the window. Fail-open:
    no Redis means no limit, never a blocked announcement."""
    try:
        from utils.redis import redis_client

        conn = redis_client.client
        cutoff = time.time() - _IN_GAME_WINDOW_SECONDS
        conn.zremrangebyscore(_in_game_key(group_id), 0, cutoff)
        return [float(score) for _, score in conn.zrange(_in_game_key(group_id), 0, -1, withscores=True)]
    except Exception:
        return []


def _record_in_game_send(group_id, ann_id) -> None:
    try:
        from utils.redis import redis_client

        conn = redis_client.client
        conn.zadd(_in_game_key(group_id), {str(ann_id): time.time()})
        conn.expire(_in_game_key(group_id), _IN_GAME_WINDOW_SECONDS)
    except Exception:
        pass


#: How far ahead an in-game end time may sit. A clan reminder is about the next
#: few days; the inbox itself only lives 24h past the send anyway.
IN_GAME_UNTIL_MAX_DAYS = 30


def _validate_in_game_until(body: dict):
    """The optional instant after which the in-game message stops showing
    (``in_game_until``, ISO 8601; a trailing Z or offset is honoured, a bare
    local time is read as UTC). Naive UTC datetime, or None when unset.

    Carried on the plugin inbox entry only (``expires_at``, dropped by the
    drain once passed), not stored on the post: the site and Discord copies
    are not time-limited."""
    raw = str(body.get("in_game_until") or "").strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        abort_problem(422, "Invalid end time", "in_game_until must be an ISO 8601 date-time.")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if parsed <= now:
        abort_problem(422, "End time already passed",
                      "The in-game message's end time must be in the future.")
    if parsed > now + timedelta(days=IN_GAME_UNTIL_MAX_DAYS):
        abort_problem(422, "End time too far ahead",
                      f"The in-game message can show for at most {IN_GAME_UNTIL_MAX_DAYS} days.")
    return parsed


def _validate_in_game(body: dict, title: str):
    """``(send, message)``: the one-line in-game text, defaulting to the title."""
    from services.plugin_notifications import CLAN_ANNOUNCEMENT_MAX_CHARS

    if not bool(body.get("send_in_game", False)):
        return False, None
    message = " ".join(str(body.get("in_game_message") or "").split()) or title
    if len(message) > CLAN_ANNOUNCEMENT_MAX_CHARS:
        abort_problem(422, "In-game message too long",
                      f"The in-game message is at most {CLAN_ANNOUNCEMENT_MAX_CHARS} characters.")
    return True, message


def _optional_url(body: dict, key: str, label: str):
    value = (body.get(key) or "").strip()
    if not value:
        return None
    if len(value) > 512 or not value.lower().startswith(("https://", "http://")):
        abort_problem(422, f"Invalid {label}", f"The {label} must be an http(s) link.")
    return value


def _validate_embed_style(body: dict) -> dict:
    """The embed-builder options for the Discord post. All optional."""
    color = (body.get("embed_color") or "").strip()
    if color and not _HEX_RE.match(color):
        abort_problem(422, "Invalid colour", "embed_color must look like #c8aa6e.")
    footer = (body.get("embed_footer") or "").strip()
    if len(footer) > 2048:
        abort_problem(422, "Footer too long", "The footer is at most 2048 characters.")
    return {
        "color": int(color[1:], 16) if color else _DEFAULT_EMBED_COLOR,
        "url": _optional_url(body, "embed_url", "title link"),
        "thumbnail": _optional_url(body, "embed_thumbnail_url", "thumbnail"),
        "footer": footer or None,
        "timestamp": bool(body.get("embed_timestamp", False)),
    }


def _discord_embed(title: str, body_md: str, cover, style: dict, published_at: datetime) -> dict:
    embed = {"title": title, "description": body_md[:4000], "color": style["color"]}
    if style.get("url"):
        embed["url"] = style["url"]
    if cover:
        embed["image"] = {"url": cover}
    if style.get("thumbnail"):
        embed["thumbnail"] = {"url": style["thumbnail"]}
    if style.get("footer"):
        embed["footer"] = {"text": style["footer"]}
    if style.get("timestamp"):
        embed["timestamp"] = published_at.astimezone().isoformat()
    return embed


def _create_announcement(user_id, scope_type, group_id, body):
    """Create and publish, or (global post from a non-approver) save as a draft
    for review. Returns ``(id, status, in_game_recipients)`` — the last is
    None unless the post went in game."""
    title, body_md = _validate_input(
        body, allow_empty_body=(scope_type == "group" and bool(body.get("send_in_game", False))))
    pinned = bool(body.get("pinned", False))
    cover = _optional_url(body, "cover_image_url", "image")
    style = _validate_embed_style(body)
    send_in_game, in_game_message = (
        _validate_in_game(body, title) if scope_type == "group" else (False, None))
    in_game_until = _validate_in_game_until(body) if send_in_game else None
    if send_in_game:
        if int(group_id) == 2:
            abort_problem(422, "Not for the global group",
                          "In-game messages go to a clan's members; the global group has every player.")
        sent = _in_game_sends_today(group_id)
        if len(sent) >= IN_GAME_DAILY_LIMIT:
            hours = max(1, int((min(sent) + _IN_GAME_WINDOW_SECONDS - time.time()) // 3600) + 1)
            abort_problem(429, "In-game limit reached",
                          f"Your clan can send {IN_GAME_DAILY_LIMIT} in-game announcements a day. "
                          f"The next one frees up in about {hours}h — or publish without the in-game message.")
    post_to_discord = bool(body.get("post_to_discord", True))
    if post_to_discord and scope_type == "group":
        # Refused, not skipped: a post an admin asked to go to Discord used to
        # publish anyway and silently go nowhere.
        with db_session() as s:
            import utils.group_config as gc

            if not gc.get(s, group_id, "announcements_channel_id"):
                abort_problem(
                    422, "No announcements channel",
                    "Your clan has no announcements channel set. Choose one in Settings → "
                    "Channels, or untick “Also post to Discord”.")
    ping_role_ids = _validate_ping_ids(body.get("ping_role_ids"), "ping_role_ids")
    ping_user_ids = _validate_ping_ids(body.get("ping_user_ids"), "ping_user_ids")
    ping_everyone = bool(body.get("ping_everyone", False))

    if scope_type == "global":
        from services import leader_updates

        if not leader_updates.is_approver(user_id):
            with db_session() as s:
                ann = leader_updates.create_review_draft(
                    s, title=title, body_md=body_md, post_to_discord=post_to_discord,
                    source_label=_author_name(s, user_id), author_user_id=user_id,
                    pinned=pinned, cover_image_url=cover,
                )
                return ann.id, "draft", None

    with db_session() as s:
        ann = Announcement(
            scope_type=scope_type,
            group_id=group_id,
            author_user_id=user_id,
            title=title,
            body_md=body_md,
            cover_image_url=cover,
            pinned=pinned,
            status="published",
            published_at=datetime.now(),
            post_to_discord=post_to_discord,
        )
        if scope_type == "global":
            # Written by an approver, so writing it was the review.
            ann.reviewed_by = user_id
            ann.reviewed_at = ann.published_at
        s.add(ann)
        s.commit()
        ann_id = ann.id
        published_ts = int(ann.published_at.timestamp())

        # Syndicate to Discord via the outbox (group scope + channel configured).
        if post_to_discord and scope_type == "group" and group_id:
            try:
                import utils.group_config as gc
                from services.discord_outbox import enqueue

                channel_id = gc.get(s, group_id, "announcements_channel_id")
                if channel_id:
                    enqueue(
                        s,
                        channel_id=str(channel_id),
                        content=_ping_content(ping_role_ids, ping_user_ids, ping_everyone),
                        embed=_discord_embed(title, body_md, cover, style, ann.published_at),
                        kind="announcement",
                        ref_type="announcement",
                        ref_id=ann_id,
                        actor_user_id=user_id,
                    )
            except Exception as e:
                print(f"[announcements] syndication enqueue failed: {e}")
        # Global: the public news channel, published to following servers
        # (DMed to the pilot accounts instead until LEADER_UPDATES_LIVE).
        elif post_to_discord and scope_type == "global":
            try:
                from services.leader_updates import enqueue_global_announcement

                enqueue_global_announcement(
                    s, ann_id=ann_id, title=title, body_md=body_md,
                    actor_user_id=user_id,
                )
            except Exception as e:
                print(f"[announcements] global syndication enqueue failed: {e}")

    # In game: every member's plugin inbox. After the commit, so a member
    # never sees a message for a post that failed to save.
    in_game_recipients = None
    if send_in_game:
        try:
            from services.plugin_notifications import push_clan_announcement

            with db_session() as s:
                group = s.query(Group).filter(Group.group_id == group_id).first()
                in_game_recipients = push_clan_announcement(
                    s, announcement_id=ann_id, group_id=group_id,
                    group_name=group.group_name if group else None,
                    title=title, message=in_game_message, until=in_game_until,
                )
            if in_game_recipients:
                _record_in_game_send(group_id, ann_id)
        except Exception as e:
            print(f"[announcements] in-game send failed for group {group_id}: {e}")
            in_game_recipients = 0

    # Realtime: notify open browsers instantly.
    try:
        from services.realtime import publish_event

        scope = "global" if scope_type == "global" else f"group:{group_id}"
        publish_event(
            "announcement",
            scope,
            {"id": ann_id, "title": title, "scope_type": scope_type,
             "group_id": group_id, "published_at": published_ts},
        )
    except Exception:
        pass

    return ann_id, "published", in_game_recipients


@announcements_bp.get("/groups/<int:group_id>/discord/roles")
async def list_group_discord_roles(group_id: int):
    """Roles of the group's linked guild, for the announcement ping picker.

    Reads the bot-maintained Redis cache (`guild:{id}:roles`, same pipeline as
    the channel picker); a cold cache returns `stale: true` and asks the bot
    to warm it (~15s), so a retry succeeds shortly.
    """
    user_id = current_user_id()

    def _load():
        import json as _json

        from utils.redis import redis_client

        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            group = s.query(Group).filter(Group.group_id == group_id).first()
            guild_id = str(group.guild_id) if (group and group.guild_id) else None
        if not guild_id:
            return {"roles": [], "stale": False}

        conn = getattr(redis_client, "client", None)
        roles = None
        if conn is not None:
            try:
                raw = conn.get(f"guild:{guild_id}:roles")
                if raw:
                    roles = _json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            except Exception:
                roles = None
        if not isinstance(roles, list):
            # Cold cache: the bot warms channels+roles from the same request set.
            try:
                if conn is not None:
                    conn.sadd("bot:channels:refresh", guild_id)
                    conn.expire("bot:channels:refresh", 300)
            except Exception:
                pass
            return {"roles": [], "stale": True}
        return {"roles": roles, "stale": False}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@announcements_bp.post("/groups/<int:group_id>/announcements")
async def create_group_announcement(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _authorize():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

    await asyncio.to_thread(_authorize)
    ann_id, status, in_game = await asyncio.to_thread(
        _create_announcement, user_id, "group", group_id, body)
    payload = {"id": ann_id, "status": status}
    if in_game is not None:
        payload["in_game_recipients"] = in_game
    return jsonify(payload)


@announcements_bp.post("/announcements")
async def create_global_announcement():
    user_id = current_user_id()
    body = await json_body()

    def _authorize():
        with db_session() as s:
            assert_superadmin(load_user(s, user_id))

    await asyncio.to_thread(_authorize)
    ann_id, status, _ = await asyncio.to_thread(_create_announcement, user_id, "global", None, body)
    return jsonify({"id": ann_id, "status": status})


def _serialize_draft(s, ann: Announcement) -> dict:
    out = _serialize(ann, _author_name(s, ann.author_user_id))
    out.update({
        "status": ann.status,
        "post_to_discord": bool(ann.post_to_discord),
        "source_label": ann.source_label,
        "created_at": int(ann.created_at.timestamp()) if ann.created_at else 0,
    })
    return out


@announcements_bp.get("/announcements/review")
async def list_review_queue():
    """Global drafts waiting for an approver. Superadmins can see the queue;
    only approvers get ``can_approve``."""
    user_id = current_user_id()

    def _load():
        from services import leader_updates

        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            rows = (
                s.query(Announcement)
                .filter(Announcement.scope_type == "global", Announcement.status == "draft")
                .order_by(Announcement.created_at.asc(), Announcement.id.asc())
                .limit(50)
                .all()
            )
            return {
                "items": [_serialize_draft(s, a) for a in rows],
                "can_approve": leader_updates.is_approver(user_id),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@announcements_bp.post("/announcements/<int:announcement_id>/approve")
async def approve_announcement(announcement_id: int):
    """Approve a draft, applying any last edits in the same request, and
    publish it (site, and Discord if the author asked for it)."""
    user_id = current_user_id()
    body = await json_body()

    def _approve():
        from services import leader_updates

        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            if not leader_updates.is_approver(user_id):
                abort_problem(403, "Not an approver", "Only the owner can approve site-wide posts.")
            ann = (
                s.query(Announcement)
                .filter(Announcement.id == announcement_id)
                .with_for_update()
                .first()
            )
            if not ann or ann.scope_type != "global":
                abort_problem(404, "Draft not found", f"No site-wide draft {announcement_id}.")
            if ann.status != "draft":
                abort_problem(409, "Not a draft", f"This post is already {ann.status}.")
            if "title" in body or "body_md" in body:
                title, body_md = _validate_input({
                    "title": body.get("title", ann.title),
                    "body_md": body.get("body_md", ann.body_md),
                })
                ann.title, ann.body_md = title, body_md
            if "pinned" in body:
                ann.pinned = bool(body["pinned"])
            if "post_to_discord" in body:
                ann.post_to_discord = bool(body["post_to_discord"])
            leader_updates.publish_reviewed(s, ann, user_id)
            payload = _serialize(ann, _author_name(s, ann.author_user_id))

        try:
            from services.realtime import publish_event

            publish_event("announcement", "global", {
                "id": payload["id"], "title": payload["title"], "scope_type": "global",
                "group_id": None, "published_at": payload["published_at"],
            })
        except Exception:
            pass
        return payload

    return private_no_store(jsonify(await asyncio.to_thread(_approve)))


def _assert_can_edit(s, user_id, ann: Announcement):
    """Author, a group admin of the scope, or superadmin may edit/delete."""
    user = load_user(s, user_id)
    if is_superadmin(user):
        return
    if ann.author_user_id == user_id:
        return
    if ann.scope_type == "group" and ann.group_id:
        role = resolve_group_role(s, user_id, ann.group_id, manageable_guild_ids(user_id), user=user)
        if role in ("owner", "admin"):
            return
    abort_problem(403, "Forbidden", "You cannot modify this announcement.")


@announcements_bp.patch("/announcements/<int:announcement_id>")
async def update_announcement(announcement_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            ann = s.query(Announcement).filter(Announcement.id == announcement_id).first()
            if not ann:
                abort_problem(404, "Announcement not found", f"No announcement {announcement_id}.")
            _assert_can_edit(s, user_id, ann)
            if ann.scope_type == "global" and ann.status == "published":
                from services import leader_updates

                # An approved post stays as approved: only an approver edits it.
                if not leader_updates.is_approver(user_id):
                    abort_problem(403, "Not an approver",
                                  "Only the owner can edit a published site-wide post.")

            if "title" in body:
                title = (body.get("title") or "").strip()
                if not (1 <= len(title) <= 200):
                    abort_problem(422, "Invalid title", "Title must be 1–200 characters.")
                ann.title = title
            if "body_md" in body:
                bmd = (body.get("body_md") or "").strip()
                if not bmd:
                    abort_problem(422, "Invalid body", "body_md must not be empty.")
                ann.body_md = bmd
            if "pinned" in body:
                ann.pinned = bool(body["pinned"])
            if "cover_image_url" in body:
                ann.cover_image_url = _optional_url(body, "cover_image_url", "image")
            if ann.status == "published" and ann.scope_type == "group":
                # Carry the change to the Discord post (same commit, so the
                # edit and its outbox row land together or not at all).
                from services.announcement_discord import embed_patch, sync_edit

                patch = embed_patch(
                    title=ann.title if "title" in body else None,
                    body_md=ann.body_md if "body_md" in body else None,
                    **({"cover_image_url": ann.cover_image_url} if "cover_image_url" in body else {}),
                )
                sync_edit(s, ann, patch)
            s.commit()
            return _serialize(ann, _author_name(s, ann.author_user_id))

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))


@announcements_bp.delete("/announcements/<int:announcement_id>")
async def delete_announcement(announcement_id: int):
    user_id = current_user_id()

    def _archive():
        with db_session() as s:
            ann = s.query(Announcement).filter(Announcement.id == announcement_id).first()
            if not ann:
                abort_problem(404, "Announcement not found", f"No announcement {announcement_id}.")
            _assert_can_edit(s, user_id, ann)
            was_published = ann.status == "published"
            ann.status = "archived"
            if was_published and ann.scope_type == "group":
                # Archived on the site = down from Discord too.
                from services.announcement_discord import sync_archive

                sync_archive(s, ann)
            s.commit()

    await asyncio.to_thread(_archive)
    return jsonify({"ok": True})
