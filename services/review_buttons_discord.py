"""Approve / Reject / Undo buttons on Discord review messages (suggestion #198).

Two kinds of message carry them (custom ids in services/review_buttons_core.py):

* the manual-review ping a held manual drop posts to a group's review channel
  (``data/submissions/manual_policy.py``), answered with the same
  ``services/drop_moderation`` calls as the website's Submissions tab;
* the "awaiting review" card for a pending event completion
  (``services/notification_service.py``), answered with
  ``services/event_review``, which the website's review queue also uses.

So a decision made in Discord has exactly the effect of one made on the site,
and is audited the same way (with ``via: discord`` and the clicker's Discord
id). Who may press is :func:`review_buttons_core.may_review`. Clicks are read
from the raw interaction so the card's components can be rewritten as plain
JSON (Components V2 cards included): the buttons go, a "who decided" line
appears, and a held drop gets an Undo button.
"""
from __future__ import annotations

import asyncio
import json
import logging

from interactions import Extension, listen
from interactions.api.events import RawGatewayEvent

from services import review_buttons_core as core

log = logging.getLogger("review_buttons")

_INTERACTION_COMPONENT = 3
_CALLBACK_DEFERRED_UPDATE = 6
_FLAG_EPHEMERAL = 64

_NOT_ALLOWED = ("You can't review submissions for this group. Group admins can, and "
                "so can anyone with the review staff role set in the group's settings.")


def _facts(data: dict) -> dict:
    member = data.get("member") or {}
    user = member.get("user") or data.get("user") or {}
    return {
        "discord_id": str(user.get("id") or ""),
        "guild_id": data.get("guild_id"),
        "permissions": member.get("permissions"),
        "roles": member.get("roles") or [],
    }


def _linked_user_id(s, discord_id):
    from db.models import User

    if not discord_id:
        return None
    row = s.query(User.user_id).filter(User.discord_id == str(discord_id)).first()
    return row[0] if row else None


def _review_role(s, group_id):
    from utils import group_config as gc

    return gc.get_bulk(s, [group_id], [core.REVIEW_ROLE_KEY]).get((group_id, core.REVIEW_ROLE_KEY))


def _is_group_admin(s, user_id, group_ids) -> bool:
    from db.models import GroupAdmin

    if user_id is None or not group_ids:
        return False
    return s.query(GroupAdmin.id).filter(GroupAdmin.user_id == user_id,
                                         GroupAdmin.group_id.in_(list(group_ids))).first() is not None


def _is_event_manager(s, user_id, group_ids) -> bool:
    from db.models import GroupEventManager

    if user_id is None or not group_ids:
        return False
    return (s.query(GroupEventManager.id)
            .filter(GroupEventManager.user_id == user_id,
                    GroupEventManager.group_id.in_(list(group_ids))).first() is not None)


def _decide_drop(s, parsed: dict, facts: dict):
    """``(ok, status_line_or_error, undo_custom_id)`` for a held manual drop."""
    from db.models import AuditLog, Group
    from services.drop_moderation import (
        ModerationError,
        approve_drop_for_group,
        reject_drop_for_group,
        undo_review_for_group,
    )

    group_id, drop_id, action = parsed["group_id"], parsed["drop_id"], parsed["action"]
    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        return False, "That group no longer exists.", None
    user_id = _linked_user_id(s, facts["discord_id"])
    if not core.may_review(
            interaction_guild_id=facts["guild_id"], group_guild_id=group.guild_id,
            is_group_admin=_is_group_admin(s, user_id, [group_id]),
            member_permissions=facts["permissions"], member_role_ids=facts["roles"],
            review_role_id=_review_role(s, group_id)):
        return False, _NOT_ALLOWED, None
    handler = {"approve": approve_drop_for_group, "reject": reject_drop_for_group,
               "undo": undo_review_for_group}[action]
    try:
        result = handler(s, drop_id, group_id, reviewer_user_id=user_id)
    except ModerationError as e:
        s.rollback()
        return False, e.detail, None
    s.add(AuditLog(
        actor_user_id=user_id,
        group_id=group_id,
        action=f"manual_submission.{action}",
        target=f"drops.{drop_id}.group.{group_id}",
        before=result.get("previous_status", "pending"),
        after=json.dumps({"status": result["status"], "via": "discord",
                          "discord_user_id": facts["discord_id"]}),
    ))
    s.commit()
    who = f"<@{facts['discord_id']}>"
    if action == "approve":
        return True, f"✅ Approved by {who}", core.drop_custom_id("undo", group_id, drop_id)
    if action == "reject":
        return True, f"❌ Rejected by {who}", core.drop_custom_id("undo", group_id, drop_id)
    return True, f"↩️ {who} took that back. It's waiting for review again.", None


def _decide_event(s, parsed: dict, facts: dict):
    """``(ok, status_line_or_error, None)`` for a pending event completion."""
    from db.models import Event, EventCompletion, EventGroup, Group
    from services import event_review

    comp = (s.query(EventCompletion)
            .filter(EventCompletion.id == parsed["completion_id"]).first())
    if comp is None:
        return False, "That completion no longer exists.", None
    ev = s.query(Event).filter(Event.id == comp.event_id).first()
    if ev is None:
        return False, "That event no longer exists.", None
    if not ev.group_id:
        return False, "Staff-run events are reviewed on the website.", None
    # Clan-vs-clan: any accepted participating clan may co-manage, the same
    # rule as the website (web_api/routes/events._assert_event_admin).
    group_ids = {ev.group_id}
    if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
        group_ids |= {gid for (gid,) in s.query(EventGroup.group_id)
                      .filter(EventGroup.event_id == ev.id,
                              EventGroup.status == "accepted").all()}
    groups = s.query(Group).filter(Group.group_id.in_(list(group_ids))).all()
    here = next((g for g in groups
                 if g.guild_id and str(g.guild_id) == str(facts["guild_id"] or "")), None)
    user_id = _linked_user_id(s, facts["discord_id"])
    if not core.may_review(
            interaction_guild_id=facts["guild_id"],
            group_guild_id=here.guild_id if here else None,
            is_group_admin=_is_group_admin(s, user_id, group_ids),
            is_event_manager=_is_event_manager(s, user_id, group_ids),
            member_permissions=facts["permissions"], member_role_ids=facts["roles"],
            review_role_id=_review_role(s, here.group_id) if here else None):
        return False, _NOT_ALLOWED, None
    extra = {"via": "discord", "discord_user_id": facts["discord_id"]}
    try:
        if parsed["action"] == "approve":
            event_review.confirm(s, ev, comp.id, user_id, audit_extra=extra)
            event_review.bump(ev.id)
        else:
            event_review.reject(s, ev, comp.id, user_id, audit_extra=extra)
    except event_review.ReviewError as e:
        s.rollback()
        return False, e.detail, None
    who = f"<@{facts['discord_id']}>"
    if parsed["action"] == "approve":
        return True, f"✅ Approved by {who}", None
    return True, f"❌ Rejected by {who}", None


def decide(parsed: dict, facts: dict):
    """Run one click in its own session (called in a worker thread)."""
    from db.models.base import Session

    s = Session()
    try:
        if parsed["kind"] == "drop":
            return _decide_drop(s, parsed, facts)
        return _decide_event(s, parsed, facts)
    except Exception:
        s.rollback()
        log.exception("[ReviewButtons] review failed: %s", parsed)
        return False, "Something went wrong. Please try again, or review on the website.", None
    finally:
        s.close()


class _BotResponder:
    """Answers an interaction our own app received, through its HTTP client
    (same calls as before the relay; ``InteractionResponder`` is the twin)."""

    def __init__(self, bot, interaction_id, token):
        self.bot, self.interaction_id, self.token = bot, interaction_id, token

    async def initial(self, payload: dict):
        return await self.bot.http.post_initial_response(
            payload, self.interaction_id, self.token)

    async def followup(self, payload: dict):
        return await self.bot.http.post_followup(payload, self.bot.app.id, self.token)

    async def edit_original(self, payload: dict):
        return await self.bot.http.edit_interaction_message(
            payload, self.bot.app.id, self.token)


class ReviewButtons(Extension):
    def __init__(self, bot):
        self.bot = bot

    @listen("raw_interaction_create")
    async def on_raw_review(self, event: RawGatewayEvent) -> None:
        data = event.data or {}
        if data.get("type") != _INTERACTION_COMPONENT:
            return
        parsed = core.parse_custom_id((data.get("data") or {}).get("custom_id"))
        if parsed is None:
            return
        try:
            await self._handle(data, parsed)
        except Exception:
            log.exception("[ReviewButtons] click failed")

    async def _handle(self, data: dict, parsed: dict, responder=None) -> None:
        """``responder`` answers the interaction — by default through our own
        app's HTTP client. A click forwarded by a relay integration's bot
        passes a ``discord_relay.InteractionResponder``: that bot already
        deferred it, and the token belongs to the customer's application."""
        interaction_id, token = data.get("id"), data.get("token")
        if not interaction_id or not token:
            return
        responder = responder or _BotResponder(self.bot, interaction_id, token)
        # Acknowledge first (3 s limit): the decision can take a moment.
        await responder.initial({"type": _CALLBACK_DEFERRED_UPDATE})
        facts = _facts(data)
        ok, text, undo_id = await asyncio.to_thread(decide, parsed, facts)
        if not ok:
            await responder.followup({"content": text, "flags": _FLAG_EPHEMERAL})
            return
        message = data.get("message") or {}
        if parsed["action"] == "undo":
            edit = core.undone_message(message, text, parsed["group_id"], parsed["drop_id"])
        else:
            edit = core.decided_message(message, text, undo_id)
        edit["allowed_mentions"] = {"parse": []}
        try:
            await responder.edit_original(edit)
        except Exception:
            # The decision stands; only the card couldn't be updated.
            log.warning("[ReviewButtons] couldn't update the review message", exc_info=True)


def setup(bot) -> None:
    ReviewButtons(bot)
