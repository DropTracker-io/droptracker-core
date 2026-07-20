"""Event manager audit log (web57a).

A single, filterable, event-scoped timeline of **everything that happened
inside one event** — so whoever runs it can trace every point:

  * auto plugin credits and pending web/manual submissions come from the
    ``EventCompletion`` ledger (which the engine writes but never audit-logs);
  * approvals, rejections, manual awards, revokes, and every roster / team /
    board / prize / task / settings change come from the ``audit_log`` table,
    scoped to the event via ``AuditLog.event_id`` (web57a).

The two sources are disjoint by design (the ledger contributes only ``auto``
and ``pending`` rows; the admin *action* on a row is the audit_log entry), so
the merge never double-counts. Completion-target audit rows are enriched with
their ledger row (player / team / task / screenshot / source) so a "confirmed"
line reads in full.

  GET /events/{id}/audit
     ?category=auto_credit,pending_submission,approval,manual_award,revoke,
               task,settings,team,participant,signup,board,prize   (multi, CSV)
     &actor_user_id=&player_id=&team_id=&task_id=&source_type=
     &has_proof=1&from=<unix>&to=<unix>&q=<text>&page=&limit=

Admin-only (``_assert_event_admin``). Reads are bounded by a per-source safety
cap; ``meta.capped`` flags an event large enough that the oldest rows fell
outside the window (narrow with filters or a date range to see them)."""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request
from sqlalchemy import or_

from db import (
    AuditLog,
    EventCompletion,
    EventTask,
    EventTeam,
    Player,
    User,
)
from web_api.common import abort_problem, db_session, parse_page, private_no_store
from web_api.deps import current_user_id
from web_api.routes.events import _assert_event_admin, _load_event_or_404, _ts

event_audit_bp = Blueprint("v1_event_audit", __name__)

# Per-source load ceiling. An event's audit_log + auto/pending ledger is
# normally hundreds–low thousands of rows; the cap only bites on a very large
# loot_sweep event, where filters/date narrow the window back down.
_CAP = 4000

# Coarse buckets the filter bar exposes. Ledger-only categories have no
# audit_log analogue; audit-only ones have no ledger analogue.
_LEDGER_CATEGORIES = {"auto_credit", "pending_submission"}
_AUDIT_CATEGORIES = {
    "approval", "manual_award", "revoke", "task", "settings",
    "team", "participant", "signup", "board", "prize", "discord",
}
_ALL_CATEGORIES = _LEDGER_CATEGORIES | _AUDIT_CATEGORIES


def _category_for(action: str) -> str:
    """Map an ``audit_log.action`` to a filter bucket."""
    if action in ("event.completion.confirm", "event.completion.reject"):
        return "approval"
    if action == "event.award":
        return "manual_award"
    if action == "event.revoke":
        return "revoke"
    if action.startswith("event.task."):
        return "task"
    if action in ("event.settings.update", "event.delete"):
        return "settings"
    if (action.startswith("event.team.") or action.startswith("event.member.")
            or action.startswith("event.leadership.")):
        return "team"
    if action.startswith("event.participant."):
        return "participant"
    if action.startswith("event.signup.") or action == "event.populate_random":
        return "signup"
    if (action.startswith("event.board.") or action == "event.bingo.replace"
            or action == "event.loot_sweep.image"):
        return "board"
    if action.startswith("event.buyin.") or action == "event.pot.announce":
        return "prize"
    if action.startswith("event.discord.") or action.startswith("event.team_discord."):
        return "discord"
    return "other"


def _audit_action_clause(categories: set):
    """OR clause selecting the audit_log actions in ``categories`` (only the
    audit-backed ones matter here)."""
    clauses = []
    if "approval" in categories:
        clauses.append(AuditLog.action.in_(
            ["event.completion.confirm", "event.completion.reject"]))
    if "manual_award" in categories:
        clauses.append(AuditLog.action == "event.award")
    if "revoke" in categories:
        clauses.append(AuditLog.action == "event.revoke")
    if "task" in categories:
        clauses.append(AuditLog.action.like("event.task.%"))
    if "settings" in categories:
        clauses.append(AuditLog.action.in_(["event.settings.update", "event.delete"]))
    if "team" in categories:
        clauses.append(or_(AuditLog.action.like("event.team.%"),
                           AuditLog.action.like("event.member.%"),
                           AuditLog.action.like("event.leadership.%")))
    if "participant" in categories:
        clauses.append(AuditLog.action.like("event.participant.%"))
    if "signup" in categories:
        clauses.append(or_(AuditLog.action.like("event.signup.%"),
                           AuditLog.action == "event.populate_random"))
    if "board" in categories:
        clauses.append(or_(AuditLog.action.like("event.board.%"),
                           AuditLog.action == "event.bingo.replace",
                           AuditLog.action == "event.loot_sweep.image"))
    if "prize" in categories:
        clauses.append(or_(AuditLog.action.like("event.buyin.%"),
                           AuditLog.action == "event.pot.announce"))
    if "discord" in categories:
        clauses.append(or_(AuditLog.action.like("event.discord.%"),
                           AuditLog.action.like("event.team_discord.%")))
    return or_(*clauses) if clauses else None


def _completion_id_from_target(target) -> int | None:
    if target and isinstance(target, str) and target.startswith("web_event_completions."):
        try:
            return int(target.split(".", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def _int_arg(name: str):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        abort_problem(422, f"Invalid {name}", f"'{name}' must be an integer.")


@event_audit_bp.get("/events/<int:event_id>/audit")
async def event_audit(event_id: int):
    user_id = current_user_id()

    raw_cats = (request.args.get("category") or "").strip()
    categories = {c.strip() for c in raw_cats.split(",") if c.strip()} if raw_cats else set()
    bad = categories - _ALL_CATEGORIES
    if bad:
        abort_problem(422, "Invalid category",
                      f"Unknown category(ies): {sorted(bad)}. "
                      f"Valid: {sorted(_ALL_CATEGORIES)}.")
    active = categories or set(_ALL_CATEGORIES)

    actor_id = _int_arg("actor_user_id")
    player_id = _int_arg("player_id")
    team_id = _int_arg("team_id")
    task_id = _int_arg("task_id")
    source_type = (request.args.get("source_type") or "").strip() or None
    has_proof = (request.args.get("has_proof") or "").strip().lower() in ("1", "true", "yes")
    date_from = _int_arg("from")
    date_to = _int_arg("to")
    q = (request.args.get("q") or "").strip()
    page, limit = parse_page(request, default_limit=50, max_limit=100)

    # Which sources can contribute under the current category selection.
    want_ledger = bool(active & _LEDGER_CATEGORIES)
    want_audit = bool(active & _AUDIT_CATEGORIES)
    # actor / audit-only narrowing filters mean the ledger can't match.
    if actor_id is not None:
        want_ledger = False
    # ledger-shaped narrowing (a specific drop's source) means generic
    # non-completion audit rows can't match — but completion audit rows still
    # can, so we keep want_audit and post-filter after enrichment.

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)

            ledger_rows = []
            ledger_total = 0
            if want_ledger:
                statuses = []
                if "auto_credit" in active:
                    statuses.append("auto")
                if "pending_submission" in active:
                    statuses.append("pending")
                lq = (
                    s.query(EventCompletion)
                    .filter(EventCompletion.event_id == event_id,
                            EventCompletion.status.in_(statuses))
                )
                if player_id is not None:
                    lq = lq.filter(EventCompletion.player_id == player_id)
                if team_id is not None:
                    lq = lq.filter(EventCompletion.team_id == team_id)
                if task_id is not None:
                    lq = lq.filter(EventCompletion.task_id == task_id)
                if source_type is not None:
                    lq = lq.filter(EventCompletion.source_type == source_type)
                if has_proof:
                    lq = lq.filter(EventCompletion.proof_url.isnot(None))
                if date_from is not None:
                    lq = lq.filter(EventCompletion.created_at >= _dt(date_from))
                if date_to is not None:
                    lq = lq.filter(EventCompletion.created_at <= _dt(date_to))
                ledger_total = lq.count()
                ledger_rows = (
                    lq.order_by(EventCompletion.created_at.desc(),
                                EventCompletion.id.desc())
                    .limit(_CAP).all()
                )

            audit_rows = []
            audit_total = 0
            if want_audit:
                aq = s.query(AuditLog).filter(AuditLog.event_id == event_id)
                # Only constrain by action when the caller picked categories.
                # With no selection we return EVERY event.* row (so an action
                # not yet mapped to a bucket still shows in the default view).
                clause = (_audit_action_clause(categories & _AUDIT_CATEGORIES)
                          if categories else None)
                if clause is not None:
                    aq = aq.filter(clause)
                if actor_id is not None:
                    aq = aq.filter(AuditLog.actor_user_id == actor_id)
                if date_from is not None:
                    aq = aq.filter(AuditLog.created_at >= _dt(date_from))
                if date_to is not None:
                    aq = aq.filter(AuditLog.created_at <= _dt(date_to))
                audit_total = aq.count()
                audit_rows = (
                    aq.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .limit(_CAP).all()
                )

            capped = ledger_total > len(ledger_rows) or audit_total > len(audit_rows)

            # ---- enrichment lookups (batched) -------------------------------
            comp_ids = {
                cid for r in audit_rows
                if (cid := _completion_id_from_target(r.target)) is not None
            }
            comp_map: dict = {}
            if comp_ids:
                for c in (s.query(EventCompletion)
                          .filter(EventCompletion.id.in_(comp_ids)).all()):
                    comp_map[c.id] = c

            task_ids = {r.task_id for r in ledger_rows}
            team_ids = {r.team_id for r in ledger_rows if r.team_id}
            player_ids = {r.player_id for r in ledger_rows if r.player_id}
            for c in comp_map.values():
                task_ids.add(c.task_id)
                if c.team_id:
                    team_ids.add(c.team_id)
                if c.player_id:
                    player_ids.add(c.player_id)
            actor_ids = {r.actor_user_id for r in audit_rows if r.actor_user_id}

            task_labels = dict(
                s.query(EventTask.id, EventTask.label)
                .filter(EventTask.id.in_(task_ids)).all()) if task_ids else {}
            team_names = dict(
                s.query(EventTeam.id, EventTeam.name)
                .filter(EventTeam.id.in_(team_ids)).all()) if team_ids else {}
            player_names = dict(
                s.query(Player.player_id, Player.player_name)
                .filter(Player.player_id.in_(player_ids)).all()) if player_ids else {}
            actors = {
                u.user_id: {"user_id": u.user_id,
                            "discord_id": str(u.discord_id) if u.discord_id else None,
                            "username": u.username}
                for u in (s.query(User).filter(User.user_id.in_(actor_ids)).all())
            } if actor_ids else {}

            events = []
            for r in ledger_rows:
                events.append(_ledger_event(r, task_labels, team_names, player_names))
            for r in audit_rows:
                events.append(_audit_event(r, comp_map, task_labels, team_names,
                                           player_names, actors))

            # ---- post-filters that couldn't be pushed to SQL ---------------
            def _keep(e) -> bool:
                if has_proof and not e.get("proof_url"):
                    return False
                if source_type is not None and e.get("source_type") != source_type:
                    return False
                if player_id is not None and e.get("player_id") != player_id:
                    return False
                if team_id is not None and e.get("team_id") != team_id:
                    return False
                if task_id is not None and e.get("task_id") != task_id:
                    return False
                if q:
                    hay = " ".join(str(x) for x in (
                        e.get("summary"), e.get("task_label"), e.get("player_name"),
                        e.get("team_name"), e.get("note"), e.get("action"),
                        e.get("target"), e.get("before"), e.get("after"),
                        (e.get("actor") or {}).get("username"),
                    ) if x).lower()
                    if q.lower() not in hay:
                        return False
                return True

            events = [e for e in events if _keep(e)]
            events.sort(key=lambda e: (e["created_at"] or 0, e["id"]), reverse=True)

            total = len(events)
            start = (page - 1) * limit
            return {
                "event_id": event_id,
                "entries": events[start:start + limit],
                "meta": {"page": page, "limit": limit, "total": total, "capped": capped},
            }

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


def _dt(unix_ts: int):
    from datetime import datetime
    return datetime.fromtimestamp(int(unix_ts))


def _ledger_event(r, task_labels, team_names, player_names) -> dict:
    category = "auto_credit" if r.status == "auto" else "pending_submission"
    player_name = player_names.get(r.player_id)
    team_name = team_names.get(r.team_id)
    task_label = task_labels.get(r.task_id)
    qty = int(r.quantity or 1)
    what = r.matched_target or task_label or "task"
    if category == "auto_credit":
        summary = (f"Auto-credited {qty}× {what}"
                   f"{f' to {player_name}' if player_name else ''}"
                   f"{f' — {team_name}' if team_name else ''}")
    else:
        summary = (f"Pending submission: {qty}× {what}"
                   f"{f' from {player_name}' if player_name else ''}"
                   f"{f' — {team_name}' if team_name else ''}"
                   " (awaiting review)")
    return {
        "id": f"ledger:{r.id}",
        "source": "ledger",
        "category": category,
        "action": category,
        "completion_id": r.id,
        "created_at": _ts(r.created_at),
        "actor": None,
        "task_id": r.task_id,
        "task_label": task_label,
        "team_id": r.team_id,
        "team_name": team_name,
        "player_id": r.player_id,
        "player_name": player_name,
        "matched_target": r.matched_target,
        "quantity": qty,
        "source_type": r.source_type,
        "status": r.status,
        "proof_url": r.proof_url,
        "note": r.note,
        "before": None,
        "after": None,
        "target": f"web_event_completions.{r.id}",
        "summary": summary,
    }


def _audit_event(r, comp_map, task_labels, team_names, player_names, actors) -> dict:
    category = _category_for(r.action)
    actor = actors.get(r.actor_user_id)
    comp = comp_map.get(_completion_id_from_target(r.target))
    task_id = team_id = player_id = None
    task_label = team_name = player_name = matched_target = None
    source_type = proof_url = None
    quantity = None
    if comp is not None:
        task_id, team_id, player_id = comp.task_id, comp.team_id, comp.player_id
        task_label = task_labels.get(comp.task_id)
        team_name = team_names.get(comp.team_id)
        player_name = player_names.get(comp.player_id)
        matched_target = comp.matched_target
        source_type = comp.source_type
        proof_url = comp.proof_url
        quantity = int(comp.quantity or 1)

    who = actor["username"] if actor and actor.get("username") else "An admin"
    verb = _ACTION_VERB.get(r.action, r.action.replace("event.", "").replace(".", " "))
    subject = matched_target or task_label or ""
    tail = ""
    if player_name:
        tail += f" — {player_name}"
    if team_name:
        tail += f" ({team_name})"
    summary = f"{who} {verb}{f': {subject}' if subject else ''}{tail}".strip()

    return {
        "id": f"audit:{r.id}",
        "source": "audit",
        "category": category,
        "action": r.action,
        "completion_id": comp.id if comp is not None else None,
        "created_at": _ts(r.created_at),
        "actor": actor,
        "task_id": task_id,
        "task_label": task_label,
        "team_id": team_id,
        "team_name": team_name,
        "player_id": player_id,
        "player_name": player_name,
        "matched_target": matched_target,
        "quantity": quantity,
        "source_type": source_type,
        "status": comp.status if comp is not None else None,
        "proof_url": proof_url,
        "note": None,
        "before": r.before,
        "after": r.after,
        "target": r.target,
        "summary": summary,
    }


_ACTION_VERB = {
    "event.completion.confirm": "confirmed a submission",
    "event.completion.reject": "rejected a submission",
    "event.award": "manually awarded",
    "event.revoke": "revoked a completion",
    "event.task.create": "added task",
    "event.task.update": "edited task",
    "event.task.delete": "deleted task",
    "event.settings.update": "changed event settings",
    "event.delete": "deleted the event",
    "event.team.update": "updated a team",
    "event.team.delete": "deleted a team",
    "event.member.add": "added a member",
    "event.member.bulk_add": "bulk-added members",
    "event.member.remove": "removed a member",
    "event.leadership.assign": "assigned a leader",
    "event.leadership.remove": "removed a leader",
    "event.participant.invite": "invited a clan",
    "event.participant.invite_bulk": "invited clans",
    "event.participant.remove": "removed a clan",
    "event.participant.accept": "accepted an invite",
    "event.participant.decline": "declined an invite",
    "event.signup.assign": "assigned a signup",
    "event.signup.randomize": "randomized signups",
    "event.signup.remove": "removed a signup",
    "event.signup.message": "messaged signups",
    "event.populate_random": "populated random teams",
    "event.board.settings": "changed board settings",
    "event.board.background": "changed the board background",
    "event.board.shop.buy": "bought a board item",
    "event.board.item.use": "used a board item",
    "event.board.task.choose": "chose a board task",
    "event.board.shop.config": "configured the board shop",
    "event.bingo.replace": "replaced the bingo board",
    "event.loot_sweep.image": "generated a loot-sweep image",
    "event.buyin.record": "recorded a buy-in",
    "event.buyin.update": "updated a buy-in",
    "event.buyin.delete": "deleted a buy-in",
    "event.buyin.bulk_seed": "seeded buy-ins",
    "event.pot.announce": "announced the prize pot",
    "event.discord.update": "updated Discord settings",
    "event.team_discord.update": "updated team-Discord settings",
    "event.team_discord.notifications": "changed team-Discord notifications",
}
