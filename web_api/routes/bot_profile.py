"""Custom bot profile (web139a): the core bot's look inside a group's server.

  GET    /api/v1/groups/{id}/bot-profile             group admins
  PUT    /api/v1/groups/{id}/bot-profile             nickname + bio (entitled)
  POST   /api/v1/groups/{id}/bot-profile/{kind}      avatar|banner upload (entitled)
  DELETE /api/v1/groups/{id}/bot-profile/{kind}      remove one image (entitled)
  DELETE /api/v1/groups/{id}/bot-profile             back to the default look
  GET    /api/v1/admin/bot-profiles                  developers (view)
  POST   /api/v1/admin/bot-profiles/{id}/reset       superadmins

This only stores what the group asked for and asks the core bot to sync
(services/bot_profile.py does the Discord side). Staff don't approve changes,
but every one is audited and listed on /admin/bot-profiles.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from quart import Blueprint, jsonify, request

from services import bot_profile as bp
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_developer,
    assert_group_admin,
    assert_group_entitlement,
    assert_superadmin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

bot_profile_bp = Blueprint("v1_bot_profile", __name__)

_KINDS = ("avatar", "banner")


def _row_payload(row, *, entitled: bool, group=None) -> dict:
    def ts(dt):
        # Epoch seconds: the columns are naive server-local times, and
        # .timestamp() reads them as such wherever the server runs.
        return int(dt.timestamp()) if dt else None

    out = {
        "entitled": bool(entitled),
        "nick": getattr(row, "nick", None),
        "bio": getattr(row, "bio", None),
        "avatar_url": getattr(row, "avatar_url", None),
        "banner_url": getattr(row, "banner_url", None),
        "applied": {
            "nick": getattr(row, "applied_nick", None),
            "bio": getattr(row, "applied_bio", None),
            "avatar_url": getattr(row, "applied_avatar_url", None),
            "banner_url": getattr(row, "applied_banner_url", None),
        },
        "status": getattr(row, "status", None) or "default",
        "last_error": getattr(row, "last_error", None),
        "applied_at": ts(getattr(row, "applied_at", None)),
        "updated_at": ts(getattr(row, "updated_at", None)),
        "limits": {"nick_max": bp.NICK_MAX, "bio_max": bp.BIO_MAX, "bio_max_lines": bp.BIO_MAX_LINES,
                   "avatar_size": list(bp.AVATAR_SIZE), "banner_size": list(bp.BANNER_SIZE),
                   "image_cooldown_seconds": bp.IMAGE_COOLDOWN_SECONDS},
    }
    if group is not None:
        out["group_id"] = group.group_id
    return out


def _snapshot(row) -> dict:
    return {f: getattr(row, f, None) for f in bp.ALL_FIELDS}


def _entitled(s, group_id: int) -> bool:
    from web_api.entitlements import resolve_group_entitlements

    try:
        return bool(resolve_group_entitlements(s, group_id).get("custom_bot_profile"))
    except Exception:
        return False


def _save(s, group_id: int, user_id: int, changes: dict, action: str = "bot_profile.update"):
    """Apply ``changes`` (stored field -> value) to the group's row, audit the
    fields that actually changed, and ask the bot to sync. Returns the row."""
    import json

    from db.models import AuditLog, GroupBotProfile

    row = s.get(GroupBotProfile, group_id)
    if row is None:
        row = GroupBotProfile(group_id=group_id, status="pending")
        s.add(row)
    before = _snapshot(row)
    changed = {k: v for k, v in changes.items() if (before.get(k) or None) != (v or None)}
    if not changed:
        return row, False
    for k, v in changed.items():
        setattr(row, k, v)
    row.status = "pending"
    row.last_error = None
    row.retry_after = None
    row.updated_by_user_id = user_id
    row.updated_at = datetime.now()
    s.add(AuditLog(actor_user_id=user_id, group_id=group_id, action=action,
                   target="group_bot_profiles." + ",".join(sorted(changed)),
                   before=json.dumps({k: before.get(k) for k in changed}),
                   after=json.dumps(changed)))
    s.commit()
    s.refresh(row)
    return row, True


@bot_profile_bp.get("/groups/<int:group_id>/bot-profile")
async def get_bot_profile(group_id: int):
    user_id = current_user_id()

    def _run():
        from db.models import GroupBotProfile

        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            return _row_payload(s.get(GroupBotProfile, group_id), entitled=_entitled(s, group_id))

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


@bot_profile_bp.put("/groups/<int:group_id>/bot-profile")
async def put_bot_profile(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    try:
        changes = {}
        if "nick" in body:
            changes["nick"] = bp.clean_nick(body.get("nick"))
        if "bio" in body:
            changes["bio"] = bp.clean_bio(body.get("bio"))
    except bp.ProfileValidationError as e:
        abort_problem(422, "Invalid bot profile", str(e))

    def _run():
        with db_session() as s:
            assert_group_entitlement(s, user_id, group_id, "custom_bot_profile",
                                     manage_guild_ids=manageable_guild_ids(user_id))
            row, changed = _save(s, group_id, user_id, changes)
            return _row_payload(row, entitled=True), changed

    payload, changed = await asyncio.to_thread(_run)
    if changed:
        bp.request_sync(group_id)
    return private_no_store(jsonify(payload))


@bot_profile_bp.post("/groups/<int:group_id>/bot-profile/<kind>")
async def upload_bot_image(group_id: int, kind: str):
    if kind not in _KINDS:
        abort_problem(404, "Not found", "Unknown image kind.")
    user_id = current_user_id()

    def _check():
        with db_session() as s:
            assert_group_entitlement(s, user_id, group_id, "custom_bot_profile",
                                     manage_guild_ids=manageable_guild_ids(user_id))

    await asyncio.to_thread(_check)

    files = await request.files
    upload = files.get("file")
    if upload is None:
        abort_problem(422, "Invalid body", "A multipart 'file' field is required.")
    raw = upload.read()
    try:
        png = await asyncio.to_thread(bp.process_image, raw, kind)
    except bp.ProfileValidationError as e:
        abort_problem(422, "Unsupported image", str(e))

    wait = bp.claim_image_slot(group_id, kind)
    if wait:
        minutes = max(1, round(wait / 60))
        abort_problem(429, "Too many changes",
                      f"Discord limits how often the bot's {kind} can change. Try again in about "
                      f"{minutes} minute{'s' if minutes != 1 else ''}.",
                      extra={"code": "bot_profile_cooldown", "retry_after": wait})

    key = bp.image_key(group_id, kind)
    try:
        from utils.b2_storage import upload_bytes

        await upload_bytes(png, key, "image/png")
    except Exception as e:
        bp.release_image_slot(group_id, kind)
        abort_problem(502, "Upload service unavailable", str(e))
    url = bp.public_url(key)

    def _run():
        with db_session() as s:
            row, _ = _save(s, group_id, user_id, {f"{kind}_url": url})
            return _row_payload(row, entitled=True)

    payload = await asyncio.to_thread(_run)
    bp.request_sync(group_id)
    return private_no_store(jsonify(payload))


@bot_profile_bp.delete("/groups/<int:group_id>/bot-profile/<kind>")
async def delete_bot_image(group_id: int, kind: str):
    if kind not in _KINDS:
        abort_problem(404, "Not found", "Unknown image kind.")
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            assert_group_entitlement(s, user_id, group_id, "custom_bot_profile",
                                     manage_guild_ids=manageable_guild_ids(user_id))
            row, changed = _save(s, group_id, user_id, {f"{kind}_url": None})
            return _row_payload(row, entitled=True), changed

    payload, changed = await asyncio.to_thread(_run)
    if changed:
        bp.request_sync(group_id)
    return private_no_store(jsonify(payload))


@bot_profile_bp.delete("/groups/<int:group_id>/bot-profile")
async def reset_bot_profile(group_id: int):
    """Back to the default look. Allowed without the entitlement, so a lapsed
    group can still clear what it saved."""
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id))
            row, changed = _save(s, group_id, user_id, {f: None for f in bp.ALL_FIELDS},
                                 action="bot_profile.reset")
            return _row_payload(row, entitled=_entitled(s, group_id)), changed

    payload, changed = await asyncio.to_thread(_run)
    if changed:
        bp.request_sync(group_id)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Staff
# --------------------------------------------------------------------------- #
@bot_profile_bp.get("/admin/bot-profiles")
async def admin_list_bot_profiles():
    user_id = current_user_id()

    def _run():
        from db.models import AuditLog, Group, GroupBotProfile, User

        with db_session() as s:
            assert_developer(load_user(s, user_id))
            rows = (s.query(GroupBotProfile, Group)
                    .outerjoin(Group, Group.group_id == GroupBotProfile.group_id)
                    .order_by(GroupBotProfile.updated_at.desc()).all())
            actor_ids = {r.updated_by_user_id for r, _ in rows if r.updated_by_user_id is not None}
            names = {}
            if actor_ids:
                names = {u.user_id: u.username for u in
                         s.query(User.user_id, User.username).filter(User.user_id.in_(actor_ids)).all()}
            history = {}
            group_ids = [r.group_id for r, _ in rows]
            if group_ids:
                for a in (s.query(AuditLog)
                          .filter(AuditLog.group_id.in_(group_ids), AuditLog.action.like("bot_profile.%"))
                          .order_by(AuditLog.created_at.desc()).limit(500).all()):
                    bucket = history.setdefault(a.group_id, [])
                    if len(bucket) < 10:
                        bucket.append({
                            "action": a.action,
                            "actor_user_id": a.actor_user_id,
                            "before": a.before,
                            "after": a.after,
                            "created_at": int(a.created_at.timestamp()) if a.created_at else None,
                        })
            out = []
            for row, group in rows:
                item = _row_payload(row, entitled=_entitled(s, row.group_id))
                item.update({
                    "group_id": row.group_id,
                    "group_name": getattr(group, "group_name", None),
                    "guild_id": row.guild_id or (str(group.guild_id) if group and group.guild_id else None),
                    "updated_by_user_id": row.updated_by_user_id,
                    "updated_by_name": names.get(row.updated_by_user_id),
                    "history": history.get(row.group_id, []),
                })
                out.append(item)
            return {"profiles": out}

    return private_no_store(jsonify(await asyncio.to_thread(_run)))


@bot_profile_bp.post("/admin/bot-profiles/<int:group_id>/reset")
async def admin_reset_bot_profile(group_id: int):
    user_id = current_user_id()

    def _run():
        with db_session() as s:
            assert_superadmin(load_user(s, user_id))
            _, changed = _save(s, group_id, user_id, {f: None for f in bp.ALL_FIELDS},
                               action="bot_profile.staff_reset")
            return changed

    if await asyncio.to_thread(_run):
        bp.request_sync(group_id)
    return private_no_store(jsonify({"ok": True}))
