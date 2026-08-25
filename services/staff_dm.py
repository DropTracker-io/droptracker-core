"""Staff↔user direct chats: the connection-free half (web102a).

A ``staff_dm`` chat thread is anchored ``subject = ('user', target_user_id)``,
and the unique subject triple on chat_threads means **at most one staff DM
thread per user, ever** — reused (reopened) across incidents. That identity is
what makes the Discord DM bridge unambiguous: an inbound DM maps to zero or
one thread, never "which of their open chats did they mean?".

This module owns everything that does NOT need a Discord connection: thread
resolution, the outbound relay enqueue (``discord_outbox`` ``kind='dm'``),
and its spam budgets. The inbound listener lives in
``services/staff_dm_bridge.py`` on the core bot — the same bot that drains
the outbox, so replies always land in its DM channels.

Relay opt-out posture: these DMs ignore the ``dm_*`` feature toggles and
``never_ping`` (a guild-mention setting) — a staff-initiated support
conversation is transactional mail, same stance as ``services/event_alerts``.
A recipient with closed DMs degrades via the drain's ``DMClosed`` →
``dm_bounced`` system entry, and the site inbox remains their surface.
"""
from __future__ import annotations

from typing import Optional

from utils.site_urls import WEBSITE_URL

THREAD_KIND = "staff_dm"
SUBJECT_TYPE = "user"

#: At most one relay DM per thread per this window: a staff member sending
#: five quick messages produces one ping, and the link carries the rest.
RELAY_COOLDOWN_SECONDS = 60
#: Hard daily ceiling per recipient across all staff activity.
RELAY_DAILY_CAP = 20

# Body budget for the relay copy, leaving room for the attribution prefix
# inside Discord's 2000-char cap.
_RELAY_BODY_MAX = 1800


def thread_url(thread_id: int) -> str:
    return f"{WEBSITE_URL}/messages/{int(thread_id)}"


def relay_dm_content(staff_name: str, body: Optional[str]) -> str:
    text = (body or "").strip() or "(attachment)"
    if len(text) > _RELAY_BODY_MAX:
        text = text[: _RELAY_BODY_MAX - 1] + "…"
    return f"**{(staff_name or 'DropTracker staff')[:100]}** (DropTracker staff): {text}"[:2000]


def get_or_create_staff_thread(s, *, target_user_id: int,
                               staff_user_id: int, commit: bool = False):
    """The (single) staff_dm thread for a user, reopened if it was locked.
    Returns ``(thread, reopened)``."""
    from db.models import User
    from services.chat import get_or_create_thread, post_system, set_thread_status

    target = s.query(User).filter(User.user_id == int(target_user_id)).first()
    title = f"Chat with {getattr(target, 'username', None) or f'user {target_user_id}'}"
    thread = get_or_create_thread(
        s,
        kind=THREAD_KIND,
        subject_type=SUBJECT_TYPE,
        subject_id=int(target_user_id),
        parties=[("user", int(target_user_id)), ("user", int(staff_user_id))],
        title=title,
        created_by_user_id=staff_user_id,
        owner_party=("user", int(staff_user_id)),
        commit=False,
    )
    reopened = False
    if thread.status != "open":
        set_thread_status(s, thread, "open", commit=False)
        post_system(
            s,
            thread=thread,
            code="staff_dm_opened",
            actor_user_id=staff_user_id,
            commit=False,
            publish=False,
        )
        reopened = True
    if commit:
        s.commit()
    return thread, reopened


def find_open_staff_thread(s, user_id: int):
    """The user's open staff_dm thread, if any — the DM bridge's router."""
    from db.models import ChatThread

    return (
        s.query(ChatThread)
        .filter(
            ChatThread.kind == THREAD_KIND,
            ChatThread.subject_type == SUBJECT_TYPE,
            ChatThread.subject_id == int(user_id),
            ChatThread.status == "open",
        )
        .first()
    )


def _relay_budget_ok(thread_id: int, target_user_id: int) -> bool:
    """Cooldown + daily cap, both fail-open (Redis down must not silence
    support outreach — the DM is the only signal the user gets)."""
    try:
        from datetime import date

        from web_api.common import _rc

        conn = _rc()
        if conn is None:
            return True
        # SET NX EX: first message in the window wins the ping.
        if not conn.set(
            f"staffdm:relay:{int(thread_id)}", "1",
            nx=True, ex=RELAY_COOLDOWN_SECONDS,
        ):
            return False
        day_key = f"staffdm:relay:day:{int(target_user_id)}:{date.today().isoformat()}"
        count = conn.incr(day_key)
        if count == 1:
            conn.expire(day_key, 172800)
        return int(count) <= RELAY_DAILY_CAP
    except Exception:
        return True


def queue_staff_dm_relay(s, *, thread, message, staff_name: str,
                         commit: bool = False) -> bool:
    """Enqueue the Discord DM copy of a staff message. Returns True when a DM
    was queued (False = no discord id, or collapsed by the budgets)."""
    from db.models import User

    target = (
        s.query(User).filter(User.user_id == int(thread.subject_id)).first()
    )
    discord_id = str(getattr(target, "discord_id", "") or "")
    if not discord_id:
        return False
    if not _relay_budget_ok(thread.id, thread.subject_id):
        return False
    from services.discord_outbox import enqueue

    enqueue(
        s,
        channel_id=discord_id,  # a USER id for kind='dm'
        content=relay_dm_content(staff_name, getattr(message, "body", None)),
        kind="dm",
        ref_type="chat_message",
        ref_id=int(message.id),
        actor_user_id=getattr(message, "author_user_id", None),
        components=[{"label": "Reply on DropTracker", "url": thread_url(thread.id)}],
        commit=commit,
    )
    return True
