"""Confirm or reject a pending event completion: one implementation for every
surface that can do it.

The website's review queue (``web_api/routes/event_admin.py``) and the Discord
review buttons (``services/review_buttons_discord.py``, suggestion #198) both
call these, so a confirmation from Discord has exactly the effect of one on the
site: the same row lock, the same engine apply, the same audit row, the same
live board update.

Authorization is the caller's job (each surface knows its own identity
model); everything here assumes the actor may act. Failures are raised as
:class:`ReviewError` carrying an HTTP-ish ``(status, title, detail)`` so the
web route can turn them into problems and the bot into a short reply.
"""
from __future__ import annotations

import json
from typing import Optional


class ReviewError(Exception):
    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def snapshot(c) -> str:
    """Before/after JSON for audit rows. web_api/routes/event_admin._snapshot
    (the award/revoke routes) writes the same shape; keep the two in step."""
    return json.dumps({
        "status": c.status,
        "task_id": c.task_id,
        "team_id": c.team_id,
        "player_id": c.player_id,
        "quantity": int(c.quantity or 1),
        "source_type": c.source_type,
        "matched_target": c.matched_target,
        "note": c.note,
        "acted_by_user_id": c.acted_by_user_id,
    })


def _after(comp, audit_extra: Optional[dict]) -> str:
    if not audit_extra:
        return snapshot(comp)
    data = json.loads(snapshot(comp))
    data.update(audit_extra)
    return json.dumps(data)


def _load_pending(s, event_id: int, completion_id: int, verb: str):
    from db.models import EventCompletion

    # P0-4: lock the row so two reviewers acting at once (or a double click,
    # or one on the site and one in Discord) serialize — the second re-reads a
    # non-pending status under the lock and is refused instead of applying
    # points twice.
    comp = (
        s.query(EventCompletion)
        .filter(EventCompletion.id == completion_id,
                EventCompletion.event_id == event_id)
        .with_for_update()
        .first()
    )
    if comp is None:
        raise ReviewError(404, "Completion not found",
                          f"No completion {completion_id} in this event.")
    if comp.status != "pending":
        raise ReviewError(
            409, "Not pending",
            f"Completion {completion_id} is '{comp.status}'; only pending rows can be {verb}.")
    return comp


def publish_pending_update(s, ev, comp) -> None:
    """After a confirm/reject, push a fresh ``kind: "pending"`` SSE frame so
    the board's amber tint updates (or clears) live (web53a). Best-effort —
    a realtime hiccup must never fail the review."""
    try:
        from db.models import EventTask
        from services.event_engine import _publish, pending_projection

        task = s.query(EventTask).filter(EventTask.id == comp.task_id).first()
        if task is None or comp.team_id is None:
            return
        config = task.config
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (ValueError, TypeError):
                config = None
        proj = pending_projection(
            s, {"id": task.id, "type": task.type,
                "target_value": task.target_value,
                "config": config}, comp.team_id)
        frame = {
            "kind": "pending", "event_id": ev.id, "task_id": comp.task_id,
            "team_id": comp.team_id,
            "pending": proj["pending_count"] if proj else 0,
            "pending_complete": bool(proj and proj["pending_complete"]),
        }
        if proj:
            frame["progress"] = proj["applied"]
        _publish(ev.id, frame)
    except Exception:
        pass


def bump(event_id: int) -> None:
    """Nudge the events worker to refresh its matcher state now."""
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(event_id)
    except Exception:
        pass


def confirm(s, ev, completion_id: int, actor_user_id: Optional[int],
            audit_extra: Optional[dict] = None):
    """pending -> confirmed, applied exactly like an automatic completion.
    Commits. Returns the before/after score receipt (or None)."""
    from db.models import AuditLog, EventTask
    from services import event_credit_receipt, event_engine

    # An ended event (manual premature ends included) processes no further
    # completions — confirming would award points and post notifications
    # after the final standings went out. Reject stays available.
    if ev.status == "past":
        raise ReviewError(
            409, "Event has ended",
            "This event is over — pending completions can no longer be "
            "confirmed. Reject them (or leave them) for the record.")
    comp = _load_pending(s, ev.id, completion_id, "confirmed")
    before = snapshot(comp)

    task_row = s.query(EventTask).filter(EventTask.id == comp.task_id).first()
    task = event_engine._task_to_dict(task_row) if task_row else None
    credit_before = (event_credit_receipt.capture(
        s, event_id=ev.id, task=task, team_id=comp.team_id,
        player_id=comp.player_id) if task else None)
    comp.status = "confirmed"
    comp.acted_by_user_id = actor_user_id
    # Single shared apply path with the worker: a confirmed row takes effect
    # exactly like an auto completion (progress fold, points, bingo cells,
    # SSE, notification).
    event_engine.apply_completion(s, comp)
    score_change = (event_credit_receipt.finish(
        s, credit_before, event_id=ev.id, task=task,
        team_id=comp.team_id, player_id=comp.player_id) if task else None)
    s.add(AuditLog(
        actor_user_id=actor_user_id,
        group_id=ev.group_id,
        event_id=ev.id,
        action="event.completion.confirm",
        target=f"web_event_completions.{completion_id}",
        before=before,
        after=_after(comp, audit_extra),
    ))
    s.commit()
    publish_pending_update(s, ev, comp)
    return score_change


def reject(s, ev, completion_id: int, actor_user_id: Optional[int],
           note: Optional[str] = None, audit_extra: Optional[dict] = None) -> None:
    """pending -> rejected: no side effects, note stored. Commits."""
    from db.models import AuditLog

    comp = _load_pending(s, ev.id, completion_id, "rejected")
    before = snapshot(comp)
    comp.status = "rejected"
    comp.acted_by_user_id = actor_user_id
    if note:
        comp.note = note
    s.add(AuditLog(
        actor_user_id=actor_user_id,
        group_id=ev.group_id,
        event_id=ev.id,
        action="event.completion.reject",
        target=f"web_event_completions.{completion_id}",
        before=before,
        after=_after(comp, audit_extra),
    ))
    s.commit()
    publish_pending_update(s, ev, comp)
