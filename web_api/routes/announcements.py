"""Task 09 — announcements + Discord syndication.

Public reads (cached):
  GET /api/v1/announcements?scope=global|group:{id}&cursor=  -> AnnouncementPage
  GET /api/v1/announcements/{id}                              -> Announcement

Writes (session + authorization):
  POST   /api/v1/groups/{groupId}/announcements  (group admin) -> { id }
  POST   /api/v1/announcements                    (superadmin)  -> { id }
  PATCH  /api/v1/announcements/{id}               (author/admin)-> Announcement
  DELETE /api/v1/announcements/{id}               (author/admin)-> { ok }

Web pages are canonical; Discord is a syndication target via the outbox
(``services/discord_outbox.py``) — never a direct Discord call from the API.
Publishing also emits a realtime ``announcement`` event.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

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
    if not user_id:
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
def _validate_input(body: dict):
    title = (body.get("title") or "").strip()
    body_md = (body.get("body_md") or "").strip()
    if not (1 <= len(title) <= 200):
        abort_problem(422, "Invalid title", "Title must be 1–200 characters.")
    if not body_md:
        abort_problem(422, "Invalid body", "body_md must not be empty.")
    return title, body_md


def _create_announcement(user_id, scope_type, group_id, body):
    title, body_md = _validate_input(body)
    pinned = bool(body.get("pinned", False))
    cover = body.get("cover_image_url")
    post_to_discord = bool(body.get("post_to_discord", True))

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
        )
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
                        embed={
                            "title": title,
                            "description": body_md[:4000],
                            "color": 0xC8AA6E,
                        },
                        kind="announcement",
                        ref_type="announcement",
                        ref_id=ann_id,
                        actor_user_id=user_id,
                    )
            except Exception as e:
                print(f"[announcements] syndication enqueue failed: {e}")

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

    return ann_id


@announcements_bp.post("/groups/<int:group_id>/announcements")
async def create_group_announcement(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _authorize():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

    await asyncio.to_thread(_authorize)
    ann_id = await asyncio.to_thread(_create_announcement, user_id, "group", group_id, body)
    return jsonify({"id": ann_id})


@announcements_bp.post("/announcements")
async def create_global_announcement():
    user_id = current_user_id()
    body = await json_body()

    def _authorize():
        with db_session() as s:
            assert_superadmin(load_user(s, user_id))

    await asyncio.to_thread(_authorize)
    ann_id = await asyncio.to_thread(_create_announcement, user_id, "global", None, body)
    return jsonify({"id": ann_id})


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
                ann.cover_image_url = body["cover_image_url"]
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
            ann.status = "archived"
            s.commit()

    await asyncio.to_thread(_archive)
    return jsonify({"ok": True})
