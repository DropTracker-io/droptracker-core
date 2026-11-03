"""Publish / Dismiss / Unpublish / Reopen buttons on clan leader feedback
posts in the staff Discord (web155a).

Answers land in the raw channel (posted through the outbox by
``web_api/routes/feedback.py``). A site superadmin presses Publish to repost
the quote in the testimonials channel; the raw post then shows who published
it, with Unpublish. Custom ids and message shapes live in
``services/feedback_core.py``.

Double posts are impossible by construction: Publish first claims the row
(``status`` -> ``publishing``) with a conditional UPDATE, so a second click or
a second admin gets "already published" instead of another post.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from interactions import Extension, listen
from interactions.api.events import RawGatewayEvent

from services import feedback_core as core

log = logging.getLogger("feedback_buttons")

_INTERACTION_COMPONENT = 3
_CALLBACK_DEFERRED_UPDATE = 6
_FLAG_EPHEMERAL = 64

#: A publish claim older than this is assumed dead (the bot restarted
#: mid-post) and may be taken again.
_STALE_CLAIM = timedelta(minutes=5)

_STAFF_ONLY = "Only DropTracker site admins can do that."


def _staff_user_id(s, discord_id):
    from db.models import User

    if not discord_id:
        return None
    row = (s.query(User.user_id, User.is_superadmin)
           .filter(User.discord_id == str(discord_id)).first())
    if row is None or not row[1]:
        return None
    return int(row[0])


def _audit(s, actor, action, response_id, after=None):
    from db.models import AuditLog

    s.add(AuditLog(actor_user_id=actor, group_id=None, action=action,
                   target=f"feedback_responses:{response_id}", before=None,
                   after=json.dumps(after) if after is not None else None))


def _load(s, response_id):
    from db.models import FeedbackResponse

    return s.query(FeedbackResponse).filter(FeedbackResponse.id == response_id).first()


def _published_payload(s, resp) -> dict:
    from db.models import Group, User
    from utils.site_urls import group_url

    username = s.query(User.username).filter(User.user_id == resp.user_id).scalar()
    group = None
    if resp.group_id is not None:
        group = s.query(Group).filter(Group.group_id == resp.group_id).first()
    return core.published_message(
        rating=int(resp.rating), quote=resp.liked_text or "", attribution=resp.attribution,
        username=username, group_name=group.group_name if group else None,
        group_icon_url=group.icon_url if group else None,
        group_page_url=group_url(group.group_id) if group else None,
        role=resp.role, member_count=resp.member_count,
        group_since=group.date_added if group else None,
    )


def _claim(parsed: dict, discord_id: str):
    """Authorize and take the first step, in a worker thread. Returns
    ``(error, plan)``: an error string for the clicker, or what to do next."""
    from sqlalchemy import or_

    from db.models import FeedbackResponse
    from db.models.base import Session

    s = Session()
    try:
        actor = _staff_user_id(s, discord_id)
        if actor is None:
            return _STAFF_ONLY, None
        rid = parsed["response_id"]
        resp = _load(s, rid)
        if resp is None:
            return "That feedback no longer exists.", None
        now = datetime.now()
        q = s.query(FeedbackResponse).filter(FeedbackResponse.id == rid)
        action = parsed["action"]

        if action == "publish":
            if not core.is_quotable(resp.quote_consent, resp.liked_text):
                return "This one wasn't shared for quoting, so it can't be published.", None
            claimed = q.filter(or_(
                FeedbackResponse.status.in_(("new", "dismissed")),
                (FeedbackResponse.status == "publishing")
                & (FeedbackResponse.reviewed_at < now - _STALE_CLAIM),
            )).update({"status": "publishing", "reviewed_by": actor, "reviewed_at": now},
                      synchronize_session=False)
            s.commit()
            if not claimed:
                return "That's already published (or being published right now).", None
            return None, {"actor": actor, "payload": _published_payload(s, resp)}

        if action == "unpublish":
            if resp.status != "published" or not resp.published_message_id:
                return "That isn't published.", None
            claimed = q.filter(FeedbackResponse.status == "published").update(
                {"status": "publishing", "reviewed_by": actor, "reviewed_at": now},
                synchronize_session=False)
            s.commit()
            if not claimed:
                return "Someone else just changed this one.", None
            return None, {"actor": actor, "message_id": resp.published_message_id}

        if action == "dismiss":
            done = q.filter(FeedbackResponse.status == "new").update(
                {"status": "dismissed", "reviewed_by": actor, "reviewed_at": now},
                synchronize_session=False)
            if not done:
                s.rollback()
                return "That's already been dealt with.", None
            _audit(s, actor, "feedback.dismiss", rid, {"via": "discord"})
            s.commit()
            return None, {"actor": actor, "quotable": core.is_quotable(resp.quote_consent, resp.liked_text)}

        # reopen
        done = q.filter(FeedbackResponse.status == "dismissed").update(
            {"status": "new", "reviewed_by": actor, "reviewed_at": now},
            synchronize_session=False)
        if not done:
            s.rollback()
            return "That isn't dismissed.", None
        _audit(s, actor, "feedback.reopen", rid, {"via": "discord"})
        s.commit()
        return None, {"actor": actor, "quotable": core.is_quotable(resp.quote_consent, resp.liked_text)}
    except Exception:
        s.rollback()
        log.exception("[FeedbackButtons] claim failed: %s", parsed)
        return "Something went wrong. Please try again.", None
    finally:
        s.close()


def _finish_publish(rid: int, actor: int, message_id):
    """Record the outcome of a publish. ``message_id`` None = the post failed,
    so the claim is released back to ``new``."""
    from db.models import FeedbackResponse
    from db.models.base import Session

    s = Session()
    try:
        q = s.query(FeedbackResponse).filter(FeedbackResponse.id == rid,
                                             FeedbackResponse.status == "publishing")
        if message_id is None:
            q.update({"status": "new"}, synchronize_session=False)
        else:
            now = datetime.now()
            q.update({"status": "published", "published_message_id": str(message_id),
                      "published_by": actor, "published_at": now},
                     synchronize_session=False)
            _audit(s, actor, "feedback.publish", rid,
                   {"via": "discord", "message_id": str(message_id)})
        s.commit()
    finally:
        s.close()


def _finish_unpublish(rid: int, actor: int, ok: bool):
    from db.models import FeedbackResponse
    from db.models.base import Session

    s = Session()
    try:
        q = s.query(FeedbackResponse).filter(FeedbackResponse.id == rid,
                                             FeedbackResponse.status == "publishing")
        if ok:
            q.update({"status": "new", "published_message_id": None,
                      "published_by": None, "published_at": None},
                     synchronize_session=False)
            _audit(s, actor, "feedback.unpublish", rid, {"via": "discord"})
        else:
            q.update({"status": "published"}, synchronize_session=False)
        s.commit()
    finally:
        s.close()


class FeedbackButtons(Extension):
    def __init__(self, bot):
        self.bot = bot

    @listen("raw_interaction_create")
    async def on_raw_feedback(self, event: RawGatewayEvent) -> None:
        data = event.data or {}
        if data.get("type") != _INTERACTION_COMPONENT:
            return
        parsed = core.parse_custom_id((data.get("data") or {}).get("custom_id"))
        if parsed is None:
            return
        try:
            await self._handle(data, parsed)
        except Exception:
            log.exception("[FeedbackButtons] click failed")

    async def _reply(self, token: str, text: str) -> None:
        await self.bot.http.post_followup({"content": text, "flags": _FLAG_EPHEMERAL},
                                          self.bot.app.id, token)

    async def _handle(self, data: dict, parsed: dict) -> None:
        from utils.interaction_payload import interaction_user_id

        interaction_id, token = data.get("id"), data.get("token")
        if not interaction_id or not token:
            return
        # Acknowledge first (3 s limit): posting can take a moment.
        await self.bot.http.post_initial_response({"type": _CALLBACK_DEFERRED_UPDATE},
                                                  interaction_id, token)
        discord_id = interaction_user_id(data) or ""
        error, plan = await asyncio.to_thread(_claim, parsed, discord_id)
        if error:
            await self._reply(token, error)
            return

        rid, action = parsed["response_id"], parsed["action"]
        who = f"<@{discord_id}>"
        edit: dict

        if action == "publish":
            message_id = None
            try:
                sent = await self.bot.http.create_message(plan["payload"], int(core.PUBLISHED_CHANNEL_ID))
                message_id = (sent or {}).get("id")
            except Exception:
                log.exception("[FeedbackButtons] publish post failed for %s", rid)
            await asyncio.to_thread(_finish_publish, rid, plan["actor"], message_id)
            if message_id is None:
                await self._reply(token, "Couldn't post in the testimonials channel. Nothing was published.")
                return
            url = core.message_url(data.get("guild_id"), core.PUBLISHED_CHANNEL_ID, message_id)
            edit = {"content": f"✅ Published by {who}",
                    "components": core.published_components(rid, url)}
        elif action == "unpublish":
            ok = True
            try:
                await self.bot.http.delete_message(int(core.PUBLISHED_CHANNEL_ID),
                                                   int(plan["message_id"]),
                                                   reason="Feedback unpublished")
            except Exception as e:  # noqa: BLE001
                # Already deleted by hand counts as unpublished.
                ok = getattr(e, "status", None) == 404 or "Unknown Message" in str(e)
                if not ok:
                    log.exception("[FeedbackButtons] unpublish failed for %s", rid)
            await asyncio.to_thread(_finish_unpublish, rid, plan["actor"], ok)
            if not ok:
                await self._reply(token, "Couldn't delete the testimonials post. It's still published.")
                return
            edit = {"content": f"↩️ Unpublished by {who}",
                    "components": core.open_components(rid, True)}
        elif action == "dismiss":
            edit = {"content": f"🗑️ Dismissed by {who}",
                    "components": core.dismissed_components(rid)}
        else:
            edit = {"content": f"↩️ Reopened by {who}",
                    "components": core.open_components(rid, plan["quotable"])}

        edit["allowed_mentions"] = {"parse": []}
        try:
            await self.bot.http.edit_interaction_message(edit, self.bot.app.id, token)
        except Exception:
            # The decision stands; only the post couldn't be updated.
            log.warning("[FeedbackButtons] couldn't update the feedback post", exc_info=True)


def setup(bot) -> None:
    FeedbackButtons(bot)
