"""One review decision settles both queues a manual drop can be held in.

A manual drop can wait in two independent places at once:

* the clan's manual-review queue (``drop_group_moderation``, the group's
  ``manual_submission_policy``), which decides whether it counts on the
  clan's own boards and posts a card to the clan's review channel;
* an event's review queue (a ``pending`` ``EventCompletion``, the event's
  ``submission_policy``), which decides whether it scores in the event.

They never talked to each other, so a clan had to approve the same drop twice
in two places, and a clan Reject left the drop scoring in an event (Frontier
PvM, 2026-11-22). The owner's rule: one decision settles both, for events
hosted by that same clan. A cross-clan event's host keeps its own decision.

Only pending rows are settled; anything already decided in the other queue is
left alone. ``undo`` is not mirrored (event confirmations have no undo).
Authorization stays with the caller: the reviewer already passed the clan's
(or the event's) check, and for a clan-hosted event that is the same clan.
"""
from __future__ import annotations

import json
from typing import Optional


def settle_event_for_clan_decision(s, drop_id: int, group_id: int, action: str,
                                   actor_user_id: Optional[int],
                                   via: str = "clan_review") -> list:
    """Mirror a committed clan approve/reject onto the drop's pending
    completions in events hosted by ``group_id``. Returns the settled
    completion ids. Each confirm/reject commits on its own."""
    if action not in ("approve", "reject") or not drop_id or not group_id:
        return []
    from db.models import Event, EventCompletion
    from services import event_review

    pending = (s.query(EventCompletion)
               .filter(EventCompletion.source_type == "drop",
                       EventCompletion.source_id == int(drop_id),
                       EventCompletion.status == "pending")
               .all())
    settled = []
    for comp in pending:
        ev = s.query(Event).filter(Event.id == comp.event_id).first()
        if ev is None or getattr(ev, "group_id", None) != int(group_id):
            continue
        if action == "approve" and ev.status == "past":
            continue  # confirming would score after the final standings
        extra = {"via": via, "linked_drop_id": int(drop_id)}
        try:
            if action == "approve":
                event_review.confirm(s, ev, comp.id, actor_user_id,
                                     audit_extra=extra, settle_clan=False)
            else:
                event_review.reject(s, ev, comp.id, actor_user_id,
                                    audit_extra=extra, settle_clan=False)
            settled.append(comp.id)
        except event_review.ReviewError:
            s.rollback()  # decided concurrently elsewhere: nothing to mirror
        except Exception as e:
            s.rollback()
            print(f"[review_link] event mirror failed for completion {comp.id}: {e}")
    return settled


def settle_clan_for_event_decision(s, ev, comp, action: str,
                                   actor_user_id: Optional[int]) -> bool:
    """Mirror a committed event confirm/reject onto the hosting clan's pending
    review row for the same drop. Returns whether a row was settled. Commits."""
    if action not in ("approve", "reject"):
        return False
    group_id = getattr(ev, "group_id", None)
    drop_id = getattr(comp, "source_id", None)
    if not group_id or not drop_id or getattr(comp, "source_type", None) != "drop":
        return False
    from db.models import AuditLog
    from services.drop_moderation import (
        ModerationError,
        _load_moderation_row,
        approve_drop_for_group,
        reject_drop_for_group,
    )

    row = _load_moderation_row(s, int(drop_id), int(group_id))
    if row is None or row.status != "pending":
        return False
    handler = approve_drop_for_group if action == "approve" else reject_drop_for_group
    try:
        result = handler(s, int(drop_id), int(group_id), reviewer_user_id=actor_user_id)
    except ModerationError:
        s.rollback()
        return False
    s.add(AuditLog(
        actor_user_id=actor_user_id,
        group_id=int(group_id),
        action=f"manual_submission.{action}",
        target=f"drops.{int(drop_id)}.group.{int(group_id)}",
        before="pending",
        after=json.dumps({"status": result["status"], "via": "event_review",
                          "event_id": ev.id, "completion_id": comp.id}),
    ))
    s.commit()
    return True
