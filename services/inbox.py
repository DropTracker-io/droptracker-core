"""Read-state for the site's unified inbox (web102a).

Chat threads badge through ``chat_reads`` (services/chat.py). Tickets and
suggestions keep their own message tables, so they get their own per-user
pointer (``surface_reads``) plus the counting logic here. The HTTP composition
lives in ``web_api/routes/inbox.py``; this module answers only "how many
entries has this user not seen?" and "advance their pointer".

**The own-reply floor.** Unread is counted above
``max(stored pointer, the user's own latest message id in that surface)``.
A reply is proof its author was caught up at that moment — and because
Discord-side replies are mirrored into the same message tables
(services/ticket_transcripts.py, services/suggestion_sync.py), someone who
answered their ticket *in Discord* shows zero unread on the site with no
explicit read report and no pointer backfill. The explicit hooks below merely
keep the pointer warm so the floor rarely has to do the work.

Lazy DB imports throughout: tests/conftest.py stubs ``db`` for route tests.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

SURFACES = ("ticket", "suggestion")


# --------------------------------------------------------------------------- #
# Pointer
# --------------------------------------------------------------------------- #
def mark_surface_read(
    s,
    surface: str,
    ref_id: int,
    user_id: int,
    message_id: int,
    *,
    commit: bool = True,
):
    """Advance one user's pointer. Never moves backwards — a stale tab
    reporting an old id must not resurrect a badge the user already cleared.
    Mirrors ``services.chat.mark_read``."""
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}")
    from db.models import SurfaceRead

    row = (
        s.query(SurfaceRead)
        .filter(
            SurfaceRead.surface == surface,
            SurfaceRead.ref_id == int(ref_id),
            SurfaceRead.user_id == int(user_id),
        )
        .first()
    )
    target = max(0, int(message_id or 0))
    if row is None:
        row = SurfaceRead(
            surface=surface,
            ref_id=int(ref_id),
            user_id=int(user_id),
            last_read_message_id=target,
        )
        s.add(row)
    elif target > (row.last_read_message_id or 0):
        row.last_read_message_id = target
        row.updated_at = datetime.now()
    if commit:
        s.commit()
    return row


def advance_own_reply(s, surface: str, ref_id: int, user_id: Optional[int],
                      message_id: int) -> None:
    """Pointer hook for the mirrors: called (uncommitted, inside the caller's
    transaction) whenever a resolvable human user authors a row. Best-effort —
    the own-reply floor already guarantees correctness without it."""
    if user_id is None:
        return
    try:
        mark_surface_read(
            s, surface, ref_id, user_id, message_id, commit=False
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Unread counts
# --------------------------------------------------------------------------- #
def _floors(pointers: dict, own_latest: dict, ids: list[int]) -> dict[int, int]:
    return {
        i: max(int(pointers.get(i, 0) or 0), int(own_latest.get(i, 0) or 0))
        for i in ids
    }


def ticket_unread_counts(
    s, ticket_ids: Iterable[int], user_id: int
) -> dict[int, int]:
    """Unread transcript entries per ticket for one user.

    Excludes the user's own rows (via the floor), and bot chatter
    (welcome cards, snapshots) — but bot ``system`` rows ("ticket closed by
    staff") DO count: that is news the opener should see.
    """
    from sqlalchemy import and_, func, not_, or_

    from db.models import SurfaceRead, TicketMessage

    ids = [int(t) for t in ticket_ids]
    if not ids:
        return {}
    pointers = dict(
        s.query(SurfaceRead.ref_id, SurfaceRead.last_read_message_id)
        .filter(
            SurfaceRead.surface == "ticket",
            SurfaceRead.ref_id.in_(ids),
            SurfaceRead.user_id == user_id,
        )
        .all()
    )
    own_latest = dict(
        s.query(TicketMessage.ticket_id, func.max(TicketMessage.id))
        .filter(
            TicketMessage.ticket_id.in_(ids),
            TicketMessage.author_user_id == user_id,
        )
        .group_by(TicketMessage.ticket_id)
        .all()
    )
    floors = _floors(pointers, own_latest, ids)
    above_floor = or_(
        *[
            and_(TicketMessage.ticket_id == tid, TicketMessage.id > floors[tid])
            for tid in ids
        ]
    )
    rows = (
        s.query(TicketMessage.ticket_id, func.count(TicketMessage.id))
        .filter(
            above_floor,
            # The floor already excludes own rows; this keeps the exclusion
            # explicit for rows that (defensively) postdate the floor.
            or_(
                TicketMessage.author_user_id.is_(None),
                TicketMessage.author_user_id != user_id,
            ),
            not_(
                and_(
                    TicketMessage.is_bot.is_(True),
                    TicketMessage.kind == "message",
                )
            ),
        )
        .group_by(TicketMessage.ticket_id)
        .all()
    )
    return {int(tid): int(n) for tid, n in rows}


def suggestion_unread_counts(
    s, suggestion_ids: Iterable[int], user_id: int
) -> dict[int, int]:
    """Unread replies per suggestion for one user. The forum mirror never
    stores bot-authored rows, so only the own-row exclusion applies."""
    from sqlalchemy import and_, func, or_

    from db.models import SuggestionMessage, SurfaceRead

    ids = [int(i) for i in suggestion_ids]
    if not ids:
        return {}
    pointers = dict(
        s.query(SurfaceRead.ref_id, SurfaceRead.last_read_message_id)
        .filter(
            SurfaceRead.surface == "suggestion",
            SurfaceRead.ref_id.in_(ids),
            SurfaceRead.user_id == user_id,
        )
        .all()
    )
    own_latest = dict(
        s.query(SuggestionMessage.suggestion_id, func.max(SuggestionMessage.id))
        .filter(
            SuggestionMessage.suggestion_id.in_(ids),
            SuggestionMessage.author_user_id == user_id,
        )
        .group_by(SuggestionMessage.suggestion_id)
        .all()
    )
    floors = _floors(pointers, own_latest, ids)
    above_floor = or_(
        *[
            and_(
                SuggestionMessage.suggestion_id == sid,
                SuggestionMessage.id > floors[sid],
            )
            for sid in ids
        ]
    )
    rows = (
        s.query(SuggestionMessage.suggestion_id, func.count(SuggestionMessage.id))
        .filter(
            above_floor,
            or_(
                SuggestionMessage.author_user_id.is_(None),
                SuggestionMessage.author_user_id != user_id,
            ),
        )
        .group_by(SuggestionMessage.suggestion_id)
        .all()
    )
    return {int(sid): int(n) for sid, n in rows}


# --------------------------------------------------------------------------- #
# Participants + realtime hints
# --------------------------------------------------------------------------- #
def ticket_participant_user_ids(s, ticket) -> set[int]:
    """Site users with a stake in a ticket: creator, claimer, anyone who
    wrote in it. Drives the ``inbox_unread`` badge fan-out."""
    from db.models import TicketMessage

    out: set[int] = set()
    # `is not None`, never truthiness: user_id 0 is a real account (and ids run
    # negative too), so `if ticket.created_by` would silently drop that person
    # from their own ticket's badge fan-out.
    if ticket.created_by is not None:
        out.add(int(ticket.created_by))
    if ticket.claimed_by is not None:
        out.add(int(ticket.claimed_by))
    for (uid,) in (
        s.query(TicketMessage.author_user_id)
        .filter(
            TicketMessage.ticket_id == ticket.ticket_id,
            TicketMessage.author_user_id.isnot(None),
        )
        .distinct()
        .all()
    ):
        out.add(int(uid))
    return out


def suggestion_participant_user_ids(s, suggestion) -> set[int]:
    from db.models import SuggestionMessage

    out: set[int] = set()
    if suggestion.user_id is not None:  # user_id 0 is a real account
        out.add(int(suggestion.user_id))
    for (uid,) in (
        s.query(SuggestionMessage.author_user_id)
        .filter(
            SuggestionMessage.suggestion_id == suggestion.id,
            SuggestionMessage.author_user_id.isnot(None),
        )
        .distinct()
        .all()
    ):
        out.add(int(uid))
    return out


def publish_inbox_unread(
    surface: str, ref_id: int, user_ids: Iterable[int],
    *, exclude_user_id: Optional[int] = None
) -> None:
    """Bodyless badge poke on ``rt:user:{uid}`` — the widget refetches its
    inbox; content never travels on the wide scope. Best-effort, never raises,
    and safe to call from a bot event loop (pure Redis publish)."""
    try:
        from services.realtime import publish_event

        hint = {"surface": surface, "ref_id": int(ref_id)}
        for uid in user_ids:
            if exclude_user_id is not None and int(uid) == int(exclude_user_id):
                continue
            publish_event("inbox_unread", f"user:{int(uid)}", hint)
    except Exception:
        pass
