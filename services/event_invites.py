"""Clan-vs-clan challenge notifications (web96a).

The half of the invite flow that reaches out. ``web_api/routes/event_participants.py``
still owns the ``web_event_groups`` rows and their authorization; this module
turns each of those state changes into the things a human notices:

* a chat thread per (event, invited clan) — one private negotiation per
  challenge, not one shared room, so a 12-clan event doesn't put every rival in
  earshot of every conversation;
* a typed system entry on that thread for every invite / accept / decline, so
  the record and the discussion are one timeline;
* a Discord DM to each authorized admin of the invited clan, carrying a link
  button straight to the page where they can answer.

Before this existed an invitation was silent: a row appeared in a table and the
challenged clan found out only if somebody happened to open the right admin
page. That is the problem this module solves.

Module-level imports are stdlib-only (lazy DB/service imports inside functions)
so unit tests can load it under conftest's stubbed ``db``/``services``.
"""
from __future__ import annotations

from typing import Optional

THREAD_KIND = "event_invite"
SUBJECT_TYPE = "event_group"

#: Users we will DM per invited clan. A clan with more leaders than this gets
#: the first N; the rest still see the invitation in the website inbox. Bounds
#: a bulk invite of 30 clans to a sane number of queued rows.
MAX_DM_RECIPIENTS = 15

#: user_configurations key. Absent means ON — this is an administrative duty
#: notification (somebody has challenged your clan and is waiting on an
#: answer), not a supporter perk, so it must not need opting into.
DM_OPT_OUT_KEY = "dm_clan_invites"

#: Who a staff-hosted event (web119a: clan-vs-clan with no host group) is
#: "from" in DMs and on the thread.
STAFF_HOST_NAME = "The DropTracker"

#: Staff invite clans site-wide, so each clan's DM fan-out is kept small:
#: the owner first, then admins, then event managers.
STAFF_DM_RECIPIENTS = 5


def is_staff_hosted(event) -> bool:
    """Mirror of web_api.event_scope.is_staff_hosted (services can't import
    web_api — the event worker runs without it)."""
    return (
        (getattr(event, "mode", None) or "standard") == "clan_vs_clan"
        and getattr(event, "group_id", None) is None
    )


def roster_size_text(lo, hi) -> Optional[str]:
    """"5 to 10", "at least 5", "up to 10" — or None when unbounded."""
    if lo and hi:
        return f"{int(lo)} to {int(hi)}" if int(lo) != int(hi) else f"exactly {int(lo)}"
    if lo:
        return f"at least {int(lo)}"
    if hi:
        return f"up to {int(hi)}"
    return None


def invitation_url(group_id: int, event_id: int) -> str:
    """The page the DM button opens: this clan's view of that challenge."""
    from utils.site_urls import WEBSITE_URL

    return f"{WEBSITE_URL}/groups/{int(group_id)}/events/invitations/{int(event_id)}"


def group_name(s, group_id, fallback: Optional[str] = None) -> Optional[str]:
    """Look up a clan's display name.

    Lives here rather than in the route so the query sits INSIDE the
    best-effort boundary: notifications must never add a query to a path whose
    failure would roll back the invitation itself.
    """
    if fallback:
        return fallback
    if not group_id:
        return None
    from db.models import Group

    row = s.query(Group.group_name).filter(Group.group_id == group_id).first()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Thread + timeline
# --------------------------------------------------------------------------- #
def ensure_thread(
    s,
    *,
    event,
    event_group,
    host_group_name: Optional[str] = None,
    invited_group_name: Optional[str] = None,
    created_by_user_id: Optional[int] = None,
    commit: bool = True,
):
    """Get-or-create the negotiation thread for one invited clan.

    Anchored to the ``web_event_groups`` row rather than to the event, which is
    what keeps each challenge private. The host is the ``owner`` party (display
    only — it grants no extra rights).
    """
    from services.chat import get_or_create_thread

    host_id = int(event.group_id) if event.group_id else None
    invited_id = int(event_group.group_id)
    parties = [("group", invited_id)]
    if host_id is not None and host_id != invited_id:
        parties.insert(0, ("group", host_id))

    title = None
    if host_id is not None and host_group_name and invited_group_name:
        title = f"{host_group_name} vs {invited_group_name}"

    return get_or_create_thread(
        s,
        kind=THREAD_KIND,
        subject_type=SUBJECT_TYPE,
        subject_id=int(event_group.id),
        parties=parties,
        title=(title or getattr(event, "name", None)),
        created_by_user_id=created_by_user_id,
        owner_party=("group", host_id) if host_id is not None else None,
        commit=commit,
    )


def record_invite(
    s,
    *,
    event,
    event_group,
    host_group_name: Optional[str] = None,
    invited_group_name: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    commit: bool = True,
):
    """Create the thread and open it with an ``invite_sent`` system entry."""
    from services.chat import Party, post_system

    thread = ensure_thread(
        s,
        event=event,
        event_group=event_group,
        host_group_name=host_group_name,
        invited_group_name=invited_group_name,
        created_by_user_id=actor_user_id,
        commit=False,
    )
    post_system(
        s,
        thread=thread,
        code="invite_sent",
        data={
            "event_id": int(event.id),
            "event_name": getattr(event, "name", None),
            "host_group_id": int(event.group_id) if event.group_id else None,
            "host_group_name": host_group_name,
            "invited_group_id": int(event_group.group_id),
            "invited_group_name": invited_group_name,
        },
        actor_user_id=actor_user_id,
        party=(
            Party("group", int(event.group_id)) if event.group_id else None
        ),
        commit=commit,
        publish=commit,
    )
    return thread


def record_response(
    s,
    *,
    event,
    event_group,
    accepted: bool,
    actor_user_id: Optional[int] = None,
    invited_group_name: Optional[str] = None,
    commit: bool = True,
):
    """Append the accept/decline entry to the existing thread.

    The thread deliberately stays **open** after an accept — the two clans keep
    coordinating rosters and timing right through the event, which is the point
    of having built this rather than a one-shot yes/no button.
    """
    from services.chat import Party, post_system, thread_by_subject

    thread = thread_by_subject(s, THREAD_KIND, SUBJECT_TYPE, int(event_group.id))
    if thread is None:
        # Invited before this feature shipped, or the thread was pruned. Create
        # it now so the response is still recorded somewhere coherent.
        thread = ensure_thread(
            s,
            event=event,
            event_group=event_group,
            invited_group_name=invited_group_name,
            created_by_user_id=actor_user_id,
            commit=False,
        )
    post_system(
        s,
        thread=thread,
        code="invite_accepted" if accepted else "invite_declined",
        data={
            "event_id": int(event.id),
            "event_name": getattr(event, "name", None),
            "invited_group_id": int(event_group.group_id),
            "invited_group_name": invited_group_name,
        },
        actor_user_id=actor_user_id,
        party=Party("group", int(event_group.group_id)),
        commit=commit,
        publish=commit,
    )
    return thread


# --------------------------------------------------------------------------- #
# Discord DMs
# --------------------------------------------------------------------------- #
def dm_recipients(s, group_id: int, *,
                  limit: int = MAX_DM_RECIPIENTS,
                  owner_first: bool = False) -> list[tuple[int, str]]:
    """``(user_id, discord_id)`` for each admin of ``group_id`` we may DM.

    Sources: explicit ``group_admins`` grants (owner + admin) and
    ``group_event_managers`` grants — the same two tables that decide who may
    answer the invite.

    **Known gap:** somebody whose rights come only from Discord ``MANAGE_GUILD``
    is not enumerable from our side (that permission is discovered per-user from
    their own OAuth guild list, never queried server-side), so they get no DM.
    They still see the challenge in the website inbox, which resolves them via
    ``manageable_guild_ids``.

    ``owner_first`` (staff-hosted invites, capped small): order the owner,
    then admins, then event managers, so the cap trims the least senior.
    """
    from db.models import GroupAdmin, GroupEventManager, User, UserConfiguration

    user_ids = {
        int(uid)
        for (uid,) in s.query(GroupAdmin.user_id)
        .filter(GroupAdmin.group_id == group_id)
        .all()
    }
    user_ids |= {
        int(uid)
        for (uid,) in s.query(GroupEventManager.user_id)
        .filter(GroupEventManager.group_id == group_id)
        .all()
    }
    if not user_ids:
        return []

    # Opt-outs. Absent row = opted IN (see DM_OPT_OUT_KEY).
    opted_out = {
        int(uid)
        for (uid, value) in s.query(
            UserConfiguration.user_id, UserConfiguration.config_value
        )
        .filter(
            UserConfiguration.user_id.in_(user_ids),
            UserConfiguration.config_key == DM_OPT_OUT_KEY,
        )
        .all()
        if str(value).lower() in ("false", "0")
    }

    rows = (
        s.query(User.user_id, User.discord_id)
        .filter(User.user_id.in_(user_ids))
        .all()
    )
    out = []
    for uid, discord_id in rows:
        if int(uid) in opted_out:
            continue
        if not discord_id:
            continue
        out.append((int(uid), str(discord_id)))
    if owner_first and out:
        # Owner, then admins, then event managers (no GroupAdmin row).
        rank = {
            int(uid): (0 if role == "owner" else 1)
            for (uid, role) in s.query(GroupAdmin.user_id, GroupAdmin.role)
            .filter(GroupAdmin.group_id == group_id).all()
        }
        out.sort(key=lambda r: (rank.get(r[0], 2), r[0]))
    else:
        out.sort()
    return out[:limit]


def build_invite_embed(
    *,
    event,
    host_group_name: Optional[str],
    invited_group_name: Optional[str],
) -> dict:
    """The DM card. Plain dict so ``discord_outbox`` can store it as JSON and
    the bot rebuilds it with ``Embed.from_dict``."""
    from utils.site_urls import WEBSITE_URL

    staff = is_staff_hosted(event)
    challenger = host_group_name or "Another clan"
    if staff:
        lines = [
            f"**{STAFF_HOST_NAME}** has invited "
            f"**{invited_group_name or 'your clan'}** to a global clan-vs-clan event.",
            "",
            "Open it on the website to accept or decline. If you take part, your "
            "members sign up and you pick who plays.",
        ]
    else:
        lines = [
            f"**{challenger}** has challenged "
            f"**{invited_group_name or 'your clan'}** to a clan-vs-clan event.",
            "",
            "Open it on the website to accept, decline, or message them back.",
        ]
    fields = []
    starts_at = getattr(event, "starts_at", None)
    ends_at = getattr(event, "ends_at", None)
    if starts_at is not None:
        fields.append(
            {
                "name": "Starts",
                "value": f"<t:{int(starts_at.timestamp())}:F>",
                "inline": True,
            }
        )
    if ends_at is not None:
        fields.append(
            {
                "name": "Ends",
                "value": f"<t:{int(ends_at.timestamp())}:F>",
                "inline": True,
            }
        )
    if staff:
        lo = getattr(event, "clan_roster_min", None)
        hi = getattr(event, "clan_roster_max", None)
        size = roster_size_text(lo, hi)
        if size:
            fields.append({"name": "Players per clan", "value": size, "inline": True})
    description = getattr(event, "description", None)
    if description:
        fields.append(
            {"name": "Details", "value": str(description)[:1000], "inline": False}
        )

    return {
        # Embed titles never render markdown, so this is plain text and the
        # link rides on embed.url instead.
        "title": (f"{'Global event' if staff else 'Clan challenge'}: "
                  f"{getattr(event, 'name', 'Untitled event')}"),
        "description": "\n".join(lines),
        "color": 0x5865F2,  # blurple — a call to action
        "fields": fields,
        "footer": {"text": "DropTracker | droptracker.io"},
        "url": f"{WEBSITE_URL}/events/{int(event.id)}",
    }


def queue_invite_dms(
    s,
    *,
    event,
    event_group,
    host_group_name: Optional[str] = None,
    invited_group_name: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    commit: bool = True,
) -> int:
    """Queue one DM per authorized admin of the invited clan. Returns how many.

    Writes ``discord_outbox`` rows — the Web API never opens a Discord
    connection; the core bot's drain sends them.
    """
    from services.discord_outbox import enqueue

    recipients = (
        dm_recipients(s, int(event_group.group_id),
                      limit=STAFF_DM_RECIPIENTS, owner_first=True)
        if is_staff_hosted(event)
        else dm_recipients(s, int(event_group.group_id))
    )
    if not recipients:
        return 0

    embed = build_invite_embed(
        event=event,
        host_group_name=host_group_name,
        invited_group_name=invited_group_name,
    )
    components = [
        {
            "label": "Respond on DropTracker",
            "url": invitation_url(int(event_group.group_id), int(event.id)),
        }
    ]
    for _user_id, discord_id in recipients:
        enqueue(
            s,
            channel_id=discord_id,  # a USER id for kind='dm'
            kind="dm",
            embed=embed,
            components=components,
            ref_type="event_invite",
            ref_id=int(event_group.id),
            actor_user_id=actor_user_id,
            commit=False,
        )
    if commit:
        s.commit()
    return len(recipients)


def announce_invite(
    s,
    *,
    event,
    event_group,
    host_group_name: Optional[str] = None,
    invited_group_name: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    commit: bool = True,
):
    """Thread + system entry + DMs for one new invitation.

    Best-effort by contract: the invitation row is already committed by the
    time this runs, and a Discord or Redis hiccup must never turn a successful
    invite into a 500. Failures are logged and swallowed — the website inbox
    still shows the challenge.
    """
    try:
        host_group_name = (
            STAFF_HOST_NAME if is_staff_hosted(event)
            else group_name(s, getattr(event, "group_id", None), host_group_name)
        )
        invited_group_name = group_name(s, event_group.group_id,
                                        invited_group_name)
        thread = record_invite(
            s,
            event=event,
            event_group=event_group,
            host_group_name=host_group_name,
            invited_group_name=invited_group_name,
            actor_user_id=actor_user_id,
            commit=False,
        )
        queue_invite_dms(
            s,
            event=event,
            event_group=event_group,
            host_group_name=host_group_name,
            invited_group_name=invited_group_name,
            actor_user_id=actor_user_id,
            commit=False,
        )
        if commit:
            s.commit()
        return thread
    except Exception as e:  # noqa: BLE001
        try:
            s.rollback()
        except Exception:
            pass
        _log_failure("announce_invite", e)
        return None


def announce_withdrawal(
    s,
    *,
    event,
    event_group,
    invited_group_name: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    commit: bool = True,
):
    """The host removed a clan from the roster.

    Must be called BEFORE the caller deletes the ``web_event_groups`` row —
    the thread is anchored to that row's id and is unfindable once it is gone.

    The thread is **archived, not deleted**: it drops out of both clans' lists
    but the record of "you were invited and then dropped" survives, which is
    exactly the thing people argue about afterwards.
    """
    from services.chat import Party, post_system, set_thread_status, thread_by_subject

    try:
        event_group_id = int(event_group.id)
        group_id = int(event_group.group_id)
        thread = thread_by_subject(s, THREAD_KIND, SUBJECT_TYPE, int(event_group_id))
        if thread is None:
            return None
        post_system(
            s,
            thread=thread,
            code="invite_withdrawn",
            data={
                "event_id": int(event.id),
                "event_name": getattr(event, "name", None),
                "invited_group_id": int(group_id),
                "invited_group_name": group_name(s, group_id, invited_group_name),
            },
            actor_user_id=actor_user_id,
            party=(
                Party("group", int(event.group_id)) if event.group_id else None
            ),
            commit=False,
            publish=False,
        )
        set_thread_status(s, thread, "archived", commit=False)
        if commit:
            s.commit()
        return thread
    except Exception as e:  # noqa: BLE001
        try:
            s.rollback()
        except Exception:
            pass
        _log_failure("announce_withdrawal", e)
        return None


def announce_response(
    s,
    *,
    event,
    event_group,
    accepted: bool,
    actor_user_id: Optional[int] = None,
    invited_group_name: Optional[str] = None,
    commit: bool = True,
):
    """System entry for an accept/decline. Same best-effort contract."""
    try:
        return record_response(
            s,
            event=event,
            event_group=event_group,
            accepted=accepted,
            actor_user_id=actor_user_id,
            invited_group_name=group_name(
                s, event_group.group_id, invited_group_name
            ),
            commit=commit,
        )
    except Exception as e:  # noqa: BLE001
        try:
            s.rollback()
        except Exception:
            pass
        _log_failure("announce_response", e)
        return None


#: Status notes on a staff-hosted event (web119a) -> (DM title, DM line).
#: ``None`` = thread entry only, no DM (the clan did it themselves).
STATUS_NOTES = {
    "clan_withdrew": None,
    "roster_below_min": (
        "Roster below the minimum",
        "**{clan}** has {count} of the {min} players needed for **{event}**. "
        "Pick more players from your sign-ups before the start, or your clan "
        "will be left out.",
    ),
    "clan_dropped": (
        "Your clan was left out",
        "**{event}** has started, and **{clan}** had {count} of the {min} "
        "players needed, so your clan was left out this time.",
    ),
}


def manage_url(group_id: int, event_id: int) -> str:
    """The clan's own page for a staff-hosted event (roster, leaders, Discord)."""
    from utils.site_urls import WEBSITE_URL

    return f"{WEBSITE_URL}/groups/{int(group_id)}/events/{int(event_id)}"


def announce_status_change(
    s,
    *,
    event,
    event_group,
    code: str = "clan_withdrew",
    roster_count: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    commit: bool = True,
):
    """A staff-hosted clan's status note (web119a): withdrew, is under the
    roster minimum ahead of the start, or was dropped at the start. Posts the
    typed entry on the clan's thread and, where :data:`STATUS_NOTES` says so,
    DMs the clan's leaders. Best-effort like every announcer here.

    ``commit=False`` is for callers inside a larger transaction (the
    start-of-event drop): the writes go in a SAVEPOINT, so a failure here
    rolls back only the note, never the caller's work."""
    if not commit:
        try:
            with s.begin_nested():
                return _status_change(s, event=event, event_group=event_group,
                                      code=code, roster_count=roster_count,
                                      actor_user_id=actor_user_id)
        except Exception as e:  # noqa: BLE001
            _log_failure("announce_status_change", e)
            return None
    try:
        thread = _status_change(s, event=event, event_group=event_group, code=code,
                                roster_count=roster_count, actor_user_id=actor_user_id)
        s.commit()
        return thread
    except Exception as e:  # noqa: BLE001
        try:
            s.rollback()
        except Exception:
            pass
        _log_failure("announce_status_change", e)
        return None


def _status_change(s, *, event, event_group, code, roster_count, actor_user_id):
    """The writes behind :func:`announce_status_change` (no commit)."""
    from services.chat import Party, post_system, thread_by_subject

    gid = int(event_group.group_id)
    clan = group_name(s, gid) or "your clan"
    lo = getattr(event, "clan_roster_min", None)
    thread = thread_by_subject(s, THREAD_KIND, SUBJECT_TYPE, int(event_group.id))
    if thread is None:
        thread = ensure_thread(s, event=event, event_group=event_group,
                               invited_group_name=clan,
                               created_by_user_id=actor_user_id, commit=False)
    post_system(
        s,
        thread=thread,
        code=code,
        data={
            "event_id": int(event.id),
            "event_name": getattr(event, "name", None),
            "invited_group_id": gid,
            "invited_group_name": clan,
            "roster_count": roster_count,
            "roster_min": lo,
        },
        actor_user_id=actor_user_id,
        party=Party("group", gid) if code == "clan_withdrew" else None,
        commit=False,
        publish=False,
    )
    note = STATUS_NOTES.get(code)
    if note:
        from services.discord_outbox import enqueue
        from utils.site_urls import WEBSITE_URL

        title, line = note
        embed = {
            "title": f"{title}: {getattr(event, 'name', 'event')}",
            "description": line.format(clan=clan, count=roster_count or 0,
                                       min=lo or 0,
                                       event=getattr(event, "name", "the event")),
            "color": 0xE67E22,
            "footer": {"text": "DropTracker | droptracker.io"},
            "url": f"{WEBSITE_URL}/events/{int(event.id)}",
        }
        components = [{"label": "Open on DropTracker",
                       "url": manage_url(gid, int(event.id))}]
        for _uid, discord_id in dm_recipients(
                s, gid, limit=STAFF_DM_RECIPIENTS, owner_first=True):
            enqueue(s, channel_id=discord_id, kind="dm", embed=embed,
                    components=components, ref_type="event_invite",
                    ref_id=int(event_group.id), actor_user_id=actor_user_id,
                    commit=False)
    return thread


def _log_failure(where: str, error: Exception) -> None:
    try:
        from db.app_logger import AppLogger

        AppLogger().log(
            log_type="error",
            data=f"{where}: {error}",
            app_name="event_invites",
            description=where,
        )
    except Exception:
        print(f"[event_invites] {where}: {error}")
