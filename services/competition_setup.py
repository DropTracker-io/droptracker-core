"""Competition event scaffolding (the ``sotw``/``botw`` kinds) — impure
companion to :mod:`services.competition` (which stays pure/stdlib for the
unit-test conftest).

A competition event's moving parts are deliberately minimal:

- **one hidden ``competition`` task** carrying the whole scoring config —
  the engine, validation and serializers all read task config already, so
  the config needs no second home. Managed here only: the generic task
  routes refuse to create/edit/delete it.
- **one roster team** ("Participants") — the engine and every read surface
  are team-shaped, and activation requires ≥1 team. Whole-clan participation
  flags it ``auto_clan`` (the matcher expands the clan's current membership,
  and ``sync_auto_clan_rosters`` mirrors it into materialized rows); opt-in
  participation leaves it a plain team the existing sign-up flow places
  players onto (``formation_mode="auto_assign"`` — one team, so placement is
  trivial).
- **one ``web_event_competitions`` row** — WOM linkage + sync state
  (``source_mode`` starts ``hosted``; the wom-link / wom-create routes move
  it).

Participation mode is not stored anywhere new: ``team.auto_clan`` IS the
fact ("whole_clan" when set, "signup" otherwise), and the event's
``formation_mode`` follows it.
"""
from __future__ import annotations

import json
from typing import Optional

from services.competition import COMPETITION_KINDS, COMPETITION_TASK_TYPE

PARTICIPATION_MODES = ("whole_clan", "signup")
DEFAULT_TEAM_NAME = "Participants"

# formation_mode per participation: whole_clan needs no sign-up at all
# (admin_assign = no self-signup surfaces); signup uses auto_assign so the
# existing event-page/Discord sign-up flow places players onto the one team
# with no team-pick step.
_FORMATION_FOR_PARTICIPATION = {
    "whole_clan": "admin_assign",
    "signup": "auto_assign",
}


def is_competition_kind(kind) -> bool:
    return (kind or "") in COMPETITION_KINDS


def competition_task(session, event_id: int):
    """The event's managed race task (None while unscaffolded)."""
    from db.models import EventTask

    return (session.query(EventTask)
            .filter(EventTask.event_id == event_id,
                    EventTask.type == COMPETITION_TASK_TYPE)
            .order_by(EventTask.id.asc())
            .first())


def competition_team(session, event_id: int):
    """The event's single roster team (None while unscaffolded)."""
    from db.models import EventTeam

    return (session.query(EventTeam)
            .filter(EventTeam.event_id == event_id)
            .order_by(EventTeam.id.asc())
            .first())


def competition_row(session, event_id: int):
    """The event's ``web_event_competitions`` row (None while unscaffolded)."""
    from db.models import EventCompetition

    return (session.query(EventCompetition)
            .filter(EventCompetition.event_id == event_id)
            .first())


def participation_mode(team) -> str:
    """Derive the participation mode from the roster team (see module doc)."""
    return "whole_clan" if team is not None and getattr(team, "auto_clan", False) else "signup"


def task_label_for(config: dict) -> str:
    """Human label for the managed task — shows in the ledger/audit surfaces."""
    if (config or {}).get("metric_kind") == "skill":
        skill = str((config or {}).get("skill") or "").strip()
        return f"Skill race: {skill.title()}" if skill else "Skill race"
    npcs = (config or {}).get("npcs") or []
    if len(npcs) == 1:
        return f"Boss race: {str(npcs[0]).strip()}"
    if npcs:
        return f"Boss race: {len(npcs)} bosses"
    return "Boss race"


def ensure_competition_scaffold(session, event, config: dict,
                                participation: Optional[str] = None) -> dict:
    """Idempotently (re)build the scaffold for one competition event.

    ``config`` is the VALIDATED canonical task config
    (web_api.routes.event_task_validation._validated_competition output);
    ``participation`` switches the roster mode (None = keep the current
    mode, defaulting to whole_clan on first scaffold when the event has a
    group). Caller owns the transaction and must only call this while the
    event is a draft (competition settings lock at activation) — except the
    defensive activation-time call, which passes the stored config verbatim
    and can only heal missing pieces, never change settings.

    Returns ``{"task", "team", "row", "created"}``.
    """
    from db.models import EventCompetition, EventTask, EventTeam

    if participation is not None and participation not in PARTICIPATION_MODES:
        raise ValueError(f"unknown participation mode: {participation!r}")

    created = []
    task = competition_task(session, event.id)
    config_json = json.dumps(config or {})
    label = task_label_for(config)
    target = ((config or {}).get("skill")
              or ((config or {}).get("npcs") or [None])[0] or "")
    if task is None:
        task = EventTask(
            event_id=event.id,
            type=COMPETITION_TASK_TYPE,
            label=label[:255],
            target=str(target)[:120] or None,
            target_value=0,
            points=0,
            requires_confirmation=False,
            config=config_json,
            # Never offered to the shared task library — the config is bound
            # to this event's WOM linkage and roster.
            visibility="private",
        )
        session.add(task)
        created.append("task")
    else:
        task.label = label[:255]
        task.target = str(target)[:120] or None
        task.config = config_json

    team = competition_team(session, event.id)
    if participation is None:
        participation = (participation_mode(team) if team is not None
                         else ("whole_clan" if event.group_id else "signup"))
    if participation == "whole_clan" and not event.group_id:
        # A global event has no clan to auto-enroll; only sign-up works.
        participation = "signup"
    if team is None:
        team = EventTeam(
            event_id=event.id,
            name=DEFAULT_TEAM_NAME,
            score=0,
            group_id=event.group_id if participation == "whole_clan" else None,
            auto_clan=participation == "whole_clan",
        )
        session.add(team)
        created.append("team")
    else:
        team.auto_clan = participation == "whole_clan"
        team.group_id = event.group_id if participation == "whole_clan" else None

    event.formation_mode = _FORMATION_FOR_PARTICIPATION[participation]

    row = competition_row(session, event.id)
    if row is None:
        row = EventCompetition(event_id=event.id, source_mode="hosted")
        session.add(row)
        created.append("row")

    session.flush()
    return {"task": task, "team": team, "row": row, "created": created}


def remove_competition_scaffold(session, event) -> None:
    """Tear the scaffold down (a DRAFT switched away from a competition kind).
    Drafts have no ledger rows, so this is a plain delete of the managed
    pieces; the roster team is kept only if players already sit on it (the
    kind switch flow re-purposes it like any other team)."""
    from db.models import EventCompetition, EventTask, EventTeamMember

    task = competition_task(session, event.id)
    if task is not None:
        session.delete(task)
    row = competition_row(session, event.id)
    if row is not None:
        session.delete(row)
    team = competition_team(session, event.id)
    if team is not None and getattr(team, "auto_clan", False):
        has_members = (session.query(EventTeamMember)
                       .filter(EventTeamMember.team_id == team.id)
                       .count())
        if not has_members:
            session.delete(team)
        else:
            team.auto_clan = False
            team.group_id = None
    session.flush()
