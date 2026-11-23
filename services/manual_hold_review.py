"""Review of held manual non-drop submissions (clog / pb / ca / pet).

Under a group's ``confirm`` manual-submission policy a manual drop waits in
``drop_group_moderation``; the other types used to be silently suppressed
with nothing to approve. Now their group notification is written to
``notification_queue`` with status ``held`` (the sender only drains
``pending``) and a review card is posted (data/submissions/manual_policy).

* approve: ``held`` -> ``pending``; the core bot posts it on its next pass.
* reject:  ``held`` -> ``rejected``.
* undo:    back to ``held`` — from ``rejected``, or from an approval the bot
           hasn't posted yet. A posted one can't be taken back here: sent
           non-drop messages aren't recorded by message id (the ``notified``
           table only tracks drops), so there is nothing to delete.

Like a held drop, a held entry never earns group points; approval only
releases the announcement. Authorization is the caller's job.
"""
from __future__ import annotations

import json
from typing import Optional

HELD = "held"
REJECTED = "rejected"
HOLD_TYPES = ("clog", "pb", "ca", "pet")


class HoldReviewError(Exception):
    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def _load(session, notification_id: int, group_id: int, lock: bool = True):
    from db.models import NotificationQueue

    q = (session.query(NotificationQueue)
         .filter(NotificationQueue.id == int(notification_id),
                 NotificationQueue.group_id == int(group_id),
                 NotificationQueue.notification_type.in_(HOLD_TYPES)))
    if lock:
        q = q.with_for_update()
    row = q.first()
    if row is None:
        raise HoldReviewError(404, "Not found",
                              "That submission has no review entry for this group.")
    return row


def _result(row, previous: str) -> dict:
    return {"notification_id": row.id, "group_id": row.group_id,
            "type": row.notification_type, "status": _public_status(row.status),
            "previous_status": _public_status(previous)}


def _public_status(status: Optional[str]) -> str:
    """Review-queue vocabulary (matches drop_group_moderation's)."""
    return {"held": "pending", "pending": "approved", "processing": "approved",
            "sent": "approved", "rejected": "rejected"}.get(status or "", status or "")


def approve(session, notification_id: int, group_id: int,
            reviewer_user_id: Optional[int] = None) -> dict:
    """Release a held notification. Caller owns the commit."""
    row = _load(session, notification_id, group_id)
    if row.status != HELD:
        raise HoldReviewError(409, "Already reviewed",
                              f"That submission is already {_public_status(row.status)}.")
    previous = row.status
    row.status = "pending"
    row.error_message = None
    session.flush()
    return _result(row, previous)


def reject(session, notification_id: int, group_id: int,
           reviewer_user_id: Optional[int] = None) -> dict:
    """Never post a held notification. Caller owns the commit."""
    row = _load(session, notification_id, group_id)
    if row.status != HELD:
        raise HoldReviewError(409, "Already reviewed",
                              f"That submission is already {_public_status(row.status)}.")
    previous = row.status
    row.status = REJECTED
    session.flush()
    return _result(row, previous)


def undo(session, notification_id: int, group_id: int,
         reviewer_user_id: Optional[int] = None) -> dict:
    """Put a decision back in the queue. Caller owns the commit."""
    row = _load(session, notification_id, group_id)
    if row.status == HELD:
        raise HoldReviewError(409, "Nothing to undo",
                              "That submission is still awaiting review.")
    if row.status in ("processing", "sent"):
        raise HoldReviewError(
            409, "Already posted",
            "That approval has already been posted to Discord, so it can't be "
            "taken back here. Delete the message in Discord if it shouldn't stand.")
    if row.status not in ("pending", REJECTED):
        raise HoldReviewError(409, "Not reviewable",
                              f"That submission is {row.status}; there is no decision to undo.")
    previous = row.status
    row.status = HELD
    session.flush()
    return _result(row, previous)


def pending_items(session, group_id: int, limit: int = 100) -> list:
    """Held entries awaiting review for one group, newest first, shaped for
    the website's review queue next to held drops."""
    from db.models import NotificationQueue

    rows = (session.query(NotificationQueue)
            .filter(NotificationQueue.group_id == int(group_id),
                    NotificationQueue.status == HELD,
                    NotificationQueue.notification_type.in_(HOLD_TYPES))
            .order_by(NotificationQueue.id.desc())
            .limit(int(limit))
            .all())
    out = []
    for r in rows:
        try:
            data = json.loads(r.data or "{}")
        except (TypeError, ValueError):
            data = {}
        from data.submissions.manual_policy import hold_summary

        out.append({
            "kind": "notification",
            "notification_id": r.id,
            "type": r.notification_type,
            "player_id": r.player_id,
            "player_name": data.get("player_name"),
            "summary": hold_summary(r.notification_type, data).replace("**", ""),
            "image_url": data.get("image_url"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out
