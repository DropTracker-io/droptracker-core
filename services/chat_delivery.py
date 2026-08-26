"""Who a thread's messages actually reached (web103a).

``services/chat`` deliberately never stores a thread's roster: a group party is
whoever holds that clan's admin or event-manager rights *right now*, resolved
per request. That is the right model for authorization and the wrong one for
the question an administrator actually asks about a relayed notice — "who got
this, and did it land?"

Two answers exist and they are not interchangeable:

  * **Reach** — who can read the thread on the site today. Derived live from
    ``group_admins`` + ``group_event_managers``, so it silently follows a
    change of clan leadership.
  * **Delivery** — who the bot DM'd and what became of it. Recorded durably as
    ``discord_outbox`` rows tagged with the thread's ref, so it still names
    somebody since demoted, and it knows about failures.

This module merges the two: every person the thread reaches, annotated with
their DM outcome (``none`` when they were never DM'd — the MANAGE_GUILD-only
admin gap ``event_invites.dm_recipients`` documents), plus anybody who was
DM'd but no longer resolves to a party member.

**Visibility.** Names are returned for parties the caller belongs to, and for
everything to support staff. Other parties collapse to counts: a clan-vs-clan
host learns that three of the challenged clan's leaders were notified without
being handed their leadership roster.
"""
from __future__ import annotations

from typing import Optional

#: Per-party name cap. A clan-vs-clan thread can reach both clans' full
#: leadership; the UI only ever shows a handful before collapsing, so the
#: payload stays small and reports how many it dropped.
DEFAULT_PARTY_CAP = 40

#: ``discord_outbox.status`` → what we report. ``sending`` is a mid-drain
#: claim, indistinguishable from pending to a reader.
_STATUS_MAP = {"sent": "sent", "failed": "failed", "pending": "pending",
               "sending": "pending"}

_ROLE_ORDER = {"owner": 0, "admin": 1, "event_manager": 2}


def outbox_ref(thread) -> Optional[tuple[str, int]]:
    """The ``(ref_type, ref_id)`` the thread's fan-out DMs were tagged with.

    The two emitters disagree on what they anchor to and neither is wrong:
    a notice DM points at the thread it opens, a clan challenge at the
    ``web_event_groups`` row that *is* the thread's subject. Encoding that here
    keeps the disagreement in one place.
    """
    kind = getattr(thread, "kind", None)
    if kind == "group_notice":
        return ("group_notice", int(thread.id))
    if kind == "event_invite":
        return ("event_invite", int(thread.subject_id))
    return None


def _recipient(*, user_id, name, discord_id, role, dm):
    """One row of the list, whether or not a DM was ever queued for them."""
    status = _STATUS_MAP.get((dm or {}).get("status"), "none") if dm else "none"
    return {
        "user_id": int(user_id) if user_id is not None else None,
        "name": name,
        "discord_id": str(discord_id) if discord_id else None,
        "role": role,
        "delivery": status,
        "at": dm.get("at") if dm else None,
        "error": (dm.get("error") or None) if dm else None,
        "attempts": int(dm.get("attempts") or 0) if dm else 0,
    }


def _sort_key(r: dict):
    return (
        _ROLE_ORDER.get(r.get("role") or "", 9),
        (r.get("name") or "").lower(),
        r.get("user_id") if r.get("user_id") is not None else 0,
    )


def _dm_targets(thread, participants) -> set[tuple[str, int]]:
    """Parties a fan-out DM was ever aimed at.

    Not the same as the parties on the thread. A clan-vs-clan challenge DMs
    the clan being challenged, never the host that wrote it; a staff DM goes
    to its subject, not to the staff member who opened it. Without this the
    host would be reported as "2 people not notified" for a message they sent
    themselves.
    """
    kind = getattr(thread, "kind", None)
    if kind == "group_notice":
        return {
            (p.party_type, int(p.party_id))
            for p in participants
            if p.party_type == "group"
        }
    if kind in ("event_invite", "staff_dm"):
        return {
            (p.party_type, int(p.party_id))
            for p in participants
            if p.role != "owner"
        }
    return set()


def _counts(recipients: list[dict], *, dm_target: bool = True) -> dict:
    """``reached`` is everyone who can read the thread; the delivery numbers
    only describe the ones a DM was aimed at. ``missed`` therefore stays 0 for
    a party nobody tried to DM, rather than reading as a delivery failure."""
    out = {"reached": len(recipients), "sent": 0, "failed": 0, "pending": 0,
           "missed": 0}
    for r in recipients:
        if r["delivery"] == "none":
            if dm_target:
                out["missed"] += 1
        else:
            out[r["delivery"]] += 1
    return out


def _dm_rows(s, thread) -> dict[str, dict]:
    """Latest DM attempt per recipient Discord id, for this thread.

    Two sources, unioned: the thread-level fan-out (``outbox_ref``) and the
    per-message staff relays, which tag ``ref_type='chat_message'`` — the only
    delivery record a ``staff_dm`` thread has.
    """
    from sqlalchemy import and_, or_

    from db.models import ChatMessage, DiscordOutbox

    clauses = []
    ref = outbox_ref(thread)
    if ref is not None:
        clauses.append(
            and_(DiscordOutbox.ref_type == ref[0], DiscordOutbox.ref_id == ref[1])
        )
    message_ids = s.query(ChatMessage.id).filter(ChatMessage.thread_id == thread.id)
    clauses.append(
        and_(
            DiscordOutbox.ref_type == "chat_message",
            DiscordOutbox.ref_id.in_(message_ids),
        )
    )

    return fold_dm_rows(
        s.query(DiscordOutbox)
        .filter(DiscordOutbox.kind == "dm", or_(*clauses))
        .order_by(DiscordOutbox.id.asc())
        .all()
    )


def fold_dm_rows(rows) -> dict[str, dict]:
    """Collapse outbox rows, id-ascending, to one entry per recipient.

    A notice that reopens fans out again, so the same person can hold several
    rows. The newest attempt is the one that describes reality — an old
    ``failed`` must not outrank today's ``sent`` — and the earlier ones survive
    only as ``attempts``.
    """
    out: dict[str, dict] = {}
    for row in rows:
        key = str(row.channel_id or "")
        if not key:
            continue
        stamp = row.processed_at or row.created_at
        out[key] = {
            "status": row.status,
            "at": int(stamp.timestamp()) if stamp else None,
            "error": row.error,
            "attempts": int((out.get(key) or {}).get("attempts") or 0) + 1,
        }
    return out


def thread_delivery(s, thread, *, membership=None, staff: bool = False,
                    party_cap: int = DEFAULT_PARTY_CAP) -> dict:
    """Reach + delivery for one thread, grouped by party.

    ``membership`` is the caller's seat (``services.chat.resolve_membership``);
    it decides whose names come back. Pass None and the caller sees counts
    only.

    ``staff`` must come from ``deps.is_support_staff`` and NOT from
    ``membership.is_moderator``, which is superadmin-only. A developer holds a
    seat on every support thread and reads every message in it, so redacting
    the recipient list from them would withhold the panel from most of the
    people it was built for.
    """
    from db.models import (
        ChatParticipant,
        Group,
        GroupAdmin,
        GroupEventManager,
        User,
    )

    participants = (
        s.query(ChatParticipant)
        .filter(ChatParticipant.thread_id == thread.id)
        .order_by(ChatParticipant.id.asc())
        .all()
    )
    unredacted = bool(staff) or bool(getattr(membership, "is_moderator", False))
    mine = {
        (p.type, int(p.id)) for p in (getattr(membership, "parties", None) or ())
    }

    group_ids = [int(p.party_id) for p in participants if p.party_type == "group"]
    party_user_ids = [int(p.party_id) for p in participants if p.party_type == "user"]

    # --- reach ---------------------------------------------------------- #
    reach: dict[int, dict[int, str]] = {gid: {} for gid in group_ids}
    if group_ids:
        for gid, uid, role in (
            s.query(GroupAdmin.group_id, GroupAdmin.user_id, GroupAdmin.role)
            .filter(GroupAdmin.group_id.in_(group_ids))
            .all()
        ):
            reach[int(gid)][int(uid)] = str(role or "admin")
        for gid, uid in (
            s.query(GroupEventManager.group_id, GroupEventManager.user_id)
            .filter(GroupEventManager.group_id.in_(group_ids))
            .all()
        ):
            # An explicit admin grant outranks the event-manager one.
            reach[int(gid)].setdefault(int(uid), "event_manager")

    # --- identities ------------------------------------------------------ #
    dm_by_discord = _dm_rows(s, thread)
    known_user_ids = {uid for members in reach.values() for uid in members}
    known_user_ids |= set(party_user_ids)

    users: dict[int, tuple[Optional[str], Optional[str]]] = {}
    by_discord: dict[str, int] = {}
    if known_user_ids:
        for uid, uname, did in (
            s.query(User.user_id, User.username, User.discord_id)
            .filter(User.user_id.in_(known_user_ids))
            .all()
        ):
            users[int(uid)] = (uname, str(did) if did else None)
            if did:
                by_discord[str(did)] = int(uid)

    # Anybody DM'd who is not on the reach list: a since-demoted admin, or a
    # holder of the legacy bot-side `authed_users` grant that
    # `event_alerts.alert_recipient_discord_ids` also fans out to.
    stray_ids = [d for d in dm_by_discord if d not in by_discord]
    strays: list[dict] = []
    if stray_ids:
        resolved = {
            str(did): (int(uid), uname)
            for uid, uname, did in (
                s.query(User.user_id, User.username, User.discord_id)
                .filter(User.discord_id.in_(stray_ids))
                .all()
            )
            if did
        }
        for did in stray_ids:
            uid, uname = resolved.get(did, (None, None))
            strays.append(
                _recipient(user_id=uid, name=uname, discord_id=did, role=None,
                           dm=dm_by_discord[did])
            )
        strays.sort(key=_sort_key)

    group_names = {}
    if group_ids:
        group_names = {
            int(gid): gname
            for gid, gname in s.query(Group.group_id, Group.group_name)
            .filter(Group.group_id.in_(group_ids))
            .all()
        }

    # A single group party owns the strays outright — there is nowhere else
    # they could have come from. With two clans on the thread we cannot say
    # which one a demoted admin belonged to, so they stay at thread level.
    stray_owner = group_ids[0] if len(group_ids) == 1 else None

    # --- assemble -------------------------------------------------------- #
    targets = _dm_targets(thread, participants)
    parties = []
    for p in participants:
        ptype, pid = p.party_type, int(p.party_id)
        visible = unredacted or (ptype, pid) in mine
        dm_target = (ptype, pid) in targets
        if ptype == "group":
            recipients = [
                _recipient(
                    user_id=uid,
                    name=users.get(uid, (None, None))[0],
                    discord_id=users.get(uid, (None, None))[1],
                    role=role,
                    dm=dm_by_discord.get(users.get(uid, (None, None))[1] or ""),
                )
                for uid, role in reach.get(pid, {}).items()
            ]
            if stray_owner == pid:
                recipients += strays
            name = group_names.get(pid)
        else:
            uname, did = users.get(pid, (None, None))
            recipients = [
                _recipient(user_id=pid, name=uname, discord_id=did,
                           role=p.role, dm=dm_by_discord.get(did or ""))
            ]
            name = uname
        recipients.sort(key=_sort_key)
        shown = recipients[:party_cap] if visible else []
        parties.append(
            {
                "party_type": ptype,
                "party_id": pid,
                "name": name,
                "role": p.role,
                "visible": visible,
                "dm_target": dm_target,
                "counts": _counts(recipients, dm_target=dm_target),
                "recipients": shown,
                "hidden": max(0, len(recipients) - len(shown)) if visible else 0,
            }
        )

    # Unattributed strays are always *counted*, but only staff see the names:
    # on a two-clan thread there is no party whose visibility could vouch for
    # them, and they are somebody's ex-leadership either way.
    unattributed = strays if stray_owner is None else []
    totals = {"reached": 0, "sent": 0, "failed": 0, "pending": 0, "missed": 0}
    for counts in [p["counts"] for p in parties] + [_counts(unattributed)]:
        for key, value in counts.items():
            totals[key] += value

    return {
        "thread_id": int(thread.id),
        "kind": getattr(thread, "kind", None),
        # Whether a DM fan-out is even expected for this kind, so the UI can
        # say "nobody was DM'd" instead of implying a failure.
        "dm_expected": outbox_ref(thread) is not None,
        "parties": parties,
        "others": unattributed if unredacted else [],
        "others_count": len(unattributed),
        "counts": totals,
    }
