"""Discord side of API key requests: the card in the ticket and its buttons.

Runs in the webhook bot, which provisions web-created tickets. When the ticket
belongs to an ``api_key_requests`` row, ``post_request_card`` shows what was
asked for and gives staff Approve / Deny. ``handle_component`` takes those
presses; only roles that may close tickets can decide (helpers cannot).

Approving mints the key and posts a one-time claim link in the ticket. The
link only opens for the requester while signed in to the site, so it is safe
in a channel other people can read; the key itself never touches Discord.

All DB work runs in a thread: the gateway loop must never wait on MariaDB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

PREFIX = "dtak:"
_ACCENT = 0x1ABC9C
_SITE = "https://www.droptracker.io"


def _load(ticket_id: int):
    from db import api_key_requests as akr
    from db.models import ApiKey, ApiKeyRequest, Session, User

    s = Session()
    try:
        req = (s.query(ApiKeyRequest)
               .filter(ApiKeyRequest.ticket_id == ticket_id)
               .order_by(ApiKeyRequest.id.desc()).first())
        if req is None:
            return None
        out = akr.status_payload(req, s=s, staff=True)
        user = (s.query(User).filter(User.user_id == req.requested_by_user_id).first()
                if req.requested_by_user_id is not None else None)
        out["requester_discord_id"] = str(getattr(user, "discord_id", "") or "") or None
        q = s.query(ApiKey).filter(ApiKey.revoked_at.is_(None))
        q = (q.filter(ApiKey.group_id == req.group_id) if req.group_id is not None
             else q.filter(ApiKey.owner_user_id == req.requested_by_user_id))
        out["live_keys"] = q.count()
        return out
    finally:
        s.close()


def card_text(req: dict) -> tuple[str, str]:
    """(heading, details) for the card. Pure."""
    if req["scope"] == "group":
        subject = (f"[{req.get('group_name') or 'group'}]({_SITE}/groups/{req['group_id']}) "
                   f"(group {req['group_id']})")
        kind = "Group key"
        reads = "reads that group's members"
    else:
        subject = req.get("requested_by_name") or f"user {req['requested_by_user_id']}"
        kind = "Personal key"
        reads = "reads only the requester's own claimed accounts"
    who = (f"<@{req['requester_discord_id']}>" if req.get("requester_discord_id")
           else (req.get("requested_by_name") or "unknown"))
    heading = (f"## 🔑 API key request: {req.get('group_name') or subject}\n"
               f"-# {kind} • request #{req['id']}")
    details = (
        f"**For:** {subject}: {reads}\n"
        f"**Asked by:** {who}\n"
        f"**Project:** {req['label'] or '(not named)'}\n"
        f"**Active keys already held:** {req.get('live_keys', 0)}\n"
        f"**What it's for:**\n> " + (req["purpose"] or "").replace("\n", "\n> ")
    )
    return heading, details


def state_line(req: dict) -> str:
    return {
        "approved": "-# ✅ Approved. The claim link is posted below.",
        "denied": "-# Declined. No key was created.",
        "cancelled": "-# Withdrawn by the requester.",
    }.get(req["status"], "")


def build_card(req: dict):
    """Components-V2 card. Buttons only while the request is pending."""
    from interactions import ActionRow, Button, ButtonStyle
    from interactions.models import ContainerComponent, SeparatorComponent, TextDisplayComponent

    heading, details = card_text(req)
    parts = [
        TextDisplayComponent(content=heading),
        SeparatorComponent(divider=True),
        TextDisplayComponent(content=details),
        SeparatorComponent(divider=True),
    ]
    if req["status"] == "pending":
        parts += [
            TextDisplayComponent(
                content="-# Staff only. Approving creates a key on the entry tier and "
                        "posts a one-time claim link here; raise its tier later from the "
                        "admin page."),
            ActionRow(
                Button(label="Approve and create key", style=ButtonStyle.SUCCESS,
                       custom_id=f"{PREFIX}approve:{req['id']}"),
                Button(label="Deny", style=ButtonStyle.DANGER,
                       custom_id=f"{PREFIX}deny:{req['id']}"),
                Button(label="API docs", style=ButtonStyle.URL, url=f"{_SITE}/docs/api"),
            ),
        ]
    else:
        parts.append(TextDisplayComponent(content=state_line(req)))
    return [ContainerComponent(*parts, accent_color=_ACCENT)]


async def post_request_card(bot, ticket_id: int, channel) -> bool:
    """Called right after a web ticket gets its channel. False when the
    ticket is not a key request."""
    from interactions import AllowedMentions

    req = await asyncio.to_thread(_load, ticket_id)
    if req is None:
        return False
    await channel.send(components=build_card(req), allowed_mentions=AllowedMentions.none())
    return True


def _decide(action: str, request_id: int, discord_id: str):
    from db import api_key_requests as akr
    from db.models import Session, User

    s = Session()
    try:
        staff = s.query(User).filter(User.discord_id == str(discord_id)).first()
        # The owner is user 0: test for a missing row, never for a falsy id.
        staff_uid = staff.user_id if staff is not None else None
        url = None
        if action == "approve":
            req, url = akr.approve(s, request_id, staff_uid)
        else:
            req = akr.deny(s, request_id, staff_uid)
        s.commit()
        payload = akr.status_payload(req, s=s, staff=True)
        user = (s.query(User).filter(User.user_id == req.requested_by_user_id).first()
                if req.requested_by_user_id is not None else None)
        payload["requester_discord_id"] = str(getattr(user, "discord_id", "") or "") or None
        return payload, url, None
    except akr.RequestError as e:
        s.rollback()
        return None, None, e.detail
    finally:
        s.close()


async def handle_component(ctx) -> bool:
    """Returns True when the press was a key-request button (handled)."""
    custom_id = getattr(ctx, "custom_id", "") or ""
    if not custom_id.startswith(PREFIX):
        return False
    from interactions import AllowedMentions

    from db import api_key_requests as akr
    from services.ticket_system import TICKET_CLOSER_ROLE_IDS, _author_has_staff_role
    from services.ticket_transcripts import add_system_message

    if not _author_has_staff_role(ctx.author, TICKET_CLOSER_ROLE_IDS):
        await ctx.send("Only DropTracker staff can decide API key requests.", ephemeral=True)
        return True
    try:
        action, raw_id = custom_id[len(PREFIX):].split(":", 1)
        request_id = int(raw_id)
    except ValueError:
        return True
    if action not in ("approve", "deny"):
        return True
    # Minting and committing can outlast the 3s interaction window.
    await ctx.defer(edit_origin=True)
    req, url, error = await asyncio.to_thread(_decide, action, request_id, str(ctx.author.id))
    if error:
        await ctx.send(error, ephemeral=True)
        return True
    try:
        await ctx.edit_origin(components=build_card(req))
    except Exception as e:
        print(f"[api_key_requests] card edit failed for request {request_id}: {e}")
    by = getattr(ctx.author, "display_name", None) or ctx.author.username
    did = req.get("requester_discord_id")
    msg = akr.decision_text(req, action, by, url, mention=f"<@{did}>" if did else "")
    await ctx.channel.send(
        msg, allowed_mentions=AllowedMentions(users=[did]) if did else AllowedMentions.none())
    if req.get("ticket_id"):
        # The transcript copy is what a requester outside the server reads.
        await asyncio.to_thread(add_system_message, req["ticket_id"],
                                akr.decision_text(req, action, by, url))
    if action == "approve":
        try:
            from services.discord_roles import request_sync

            request_sync()  # the API Consumer role follows the new key
        except Exception:
            pass
    print(f"[api_key_requests] {action} request {request_id} by {ctx.author.id} at {datetime.now()}")
    return True
