"""Discord side of data removal: the card in the ticket and its buttons.

Runs in the webhook bot, which provisions web-created tickets. When the ticket
belongs to a ``data_removal_requests`` row, ``post_removal_card`` adds the
people who must confirm (a group's admins who are in our server), pings them,
and shows staff the Approve / Reject buttons. ``handle_component`` takes those
presses; only roles that may close tickets can decide (helpers cannot).

All DB work runs in a thread: removal decisions touch a players row and the
ticket transcript, and the gateway loop must never wait on MariaDB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

PREFIX = "dtrm:"
_ACCENT = 0xC0392B


def _load(ticket_id: int):
    from db.models import Session
    from db.models.data_removal import DataRemovalRequest
    from services import data_removal as dr

    s = Session()
    try:
        req = (s.query(DataRemovalRequest)
               .filter(DataRemovalRequest.ticket_id == ticket_id)
               .order_by(DataRemovalRequest.id.desc()).first())
        if req is None:
            return None, []
        ids = dr.group_admin_discord_ids(s, req.subject_id) if req.kind == dr.KIND_GROUP else []
        return dr.status_payload(req), ids
    finally:
        s.close()


def card_text(req: dict, mentions: list[str]) -> tuple[str, str, str]:
    """(heading, verification steps, what happens next) for the card. Pure."""
    who = " ".join(f"<@{d}>" for d in mentions)
    if req["kind"] == "player":
        steps = (
            f"**To verify:** log in to **{req['subject_name']}**, type\n"
            f"```{req['verification_phrase']}```\n"
            "in the chatbox, then post a screenshot of your **whole RuneLite client** "
            "(not a crop) here. Staff check it against the account before approving."
        )
    else:
        steps = (
            f"{who}\n**You are an admin of {req['subject_name']}.** The group's owner has "
            "asked for the group and all of its data to be removed. Please reply here to "
            "confirm, or to object. Staff will not approve until the group's leaders agree."
        )
    what = (
        "**What happens next:** once staff approve, removal is scheduled for **7 days** "
        "later. During that time it can still be cancelled from the website or by staff. "
        "Then the data is erased, and a private backup is kept for another 7 days in case "
        "of a mistake before it is deleted for good."
    )
    if req["kind"] == "player":
        what += (" The account is hidden from every board as soon as removal is approved, "
                 "and the bot's past Discord posts about it are changed to say *Deleted user*.")
    heading = (
        f"## Data removal request: {req['subject_name']}\n"
        f"-# {'Account' if req['kind'] == 'player' else 'Group'} removal • request #{req['id']}"
    )
    return heading, steps, what


def build_card(req: dict, mentions: list[str]):
    """Components-V2 card for a removal request."""
    from interactions import ActionRow, Button, ButtonStyle
    from interactions.models import ContainerComponent, SeparatorComponent, TextDisplayComponent

    heading, steps, what = card_text(req, mentions)
    return [
        ContainerComponent(
            TextDisplayComponent(content=heading),
            SeparatorComponent(divider=True),
            TextDisplayComponent(content=steps),
            TextDisplayComponent(content=what),
            SeparatorComponent(divider=True),
            TextDisplayComponent(content="-# Staff only:"),
            ActionRow(
                Button(label="Approve removal", style=ButtonStyle.DANGER,
                       custom_id=f"{PREFIX}approve:{req['id']}"),
                Button(label="Reject", style=ButtonStyle.SECONDARY,
                       custom_id=f"{PREFIX}reject:{req['id']}"),
                Button(label="Cancel request", style=ButtonStyle.SECONDARY,
                       custom_id=f"{PREFIX}cancel:{req['id']}"),
            ),
            accent_color=_ACCENT,
        )
    ]


async def post_removal_card(bot, ticket_id: int, channel) -> bool:
    """Called right after a web ticket gets its channel. False when the
    ticket is not a removal request."""
    from interactions import AllowedMentions, OverwriteType

    req, admin_ids = await asyncio.to_thread(_load, ticket_id)
    if req is None:
        return False
    present = []
    guild = getattr(channel, "guild", None)
    for did in admin_ids:
        try:
            member = await guild.fetch_member(did) if guild else None
        except Exception:
            member = None
        if member is None:
            continue
        try:
            await channel.add_permission(
                target=member, type=OverwriteType.MEMBER,
                allow=_opener_permissions(),
                reason=f"Group admin for data removal request #{req['id']}",
            )
            present.append(did)
        except Exception as e:
            print(f"[data_removal] could not add {did} to ticket {ticket_id}: {e}")
    await channel.send(
        components=build_card(req, present),
        allowed_mentions=AllowedMentions(users=present),
    )
    if req["kind"] == "group" and len(present) < len(admin_ids):
        await channel.send(
            f"-# {len(admin_ids) - len(present)} of the group's admins aren't in this "
            "Discord server, so they couldn't be added.",
            allowed_mentions=AllowedMentions.none(),
        )
    return True


def _opener_permissions():
    from services.ticket_system import OPENER_PERMISSIONS

    return OPENER_PERMISSIONS


def _decide(action: str, request_id: int, discord_id: str):
    from db.models import Session, User
    from services import data_removal as dr

    s = Session()
    try:
        staff = s.query(User).filter(User.discord_id == str(discord_id)).first()
        staff_uid = staff.user_id if staff else None
        if action == "approve":
            req = dr.approve(s, request_id, staff_uid)
        elif action == "reject":
            req = dr.reject(s, request_id, staff_uid)
        else:
            req = dr.cancel(s, request_id, staff_uid, staff=True)
        s.commit()
        return dr.status_payload(req), None
    except dr.RemovalError as e:
        s.rollback()
        return None, e.detail
    finally:
        s.close()


def decision_text(req: dict, action: str, by: str) -> str:
    if action == "approve":
        when = f"<t:{req['purge_after']}:F> (<t:{req['purge_after']}:R>)"
        extra = (" The account is now hidden from all boards." if req["kind"] == "player" else "")
        return (f"✅ Removal of **{req['subject_name']}** approved by {by}. "
                f"The data will be erased {when}.{extra} It can still be cancelled until then.")
    if action == "reject":
        return f"Removal of **{req['subject_name']}** was rejected by {by}. Nothing was changed."
    return f"Removal of **{req['subject_name']}** was cancelled by {by}. Nothing will be erased."


async def handle_component(ctx) -> bool:
    """Returns True when the press was a data-removal button (handled)."""
    custom_id = getattr(ctx, "custom_id", "") or ""
    if not custom_id.startswith(PREFIX):
        return False
    from interactions import AllowedMentions
    from services.ticket_system import TICKET_CLOSER_ROLE_IDS, _author_has_staff_role
    from services.ticket_transcripts import add_system_message

    if not _author_has_staff_role(ctx.author, TICKET_CLOSER_ROLE_IDS):
        await ctx.send("Only DropTracker staff can decide removal requests.", ephemeral=True)
        return True
    try:
        action, raw_id = custom_id[len(PREFIX):].split(":", 1)
        request_id = int(raw_id)
    except ValueError:
        return True
    req, error = await asyncio.to_thread(_decide, action, request_id, str(ctx.author.id))
    if error:
        await ctx.send(error, ephemeral=True)
        return True
    by = getattr(ctx.author, "display_name", None) or ctx.author.username
    msg = decision_text(req, action, by)
    await ctx.send(msg, allowed_mentions=AllowedMentions.none())
    if req.get("ticket_id"):
        await asyncio.to_thread(add_system_message, req["ticket_id"], msg)
    print(f"[data_removal] {action} request {request_id} by {ctx.author.id} at {datetime.now()}")
    return True
