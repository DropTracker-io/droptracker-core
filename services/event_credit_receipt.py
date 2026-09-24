"""Before/after "receipt" for an organizer's manual credit on an event.

A manual award (``POST /events/{id}/award``) or a confirmed submission
(``.../completions/{cid}/confirm``) moves a team's score, the task's progress
and, when a player is named, that player's own points. The organizer used to
get back only a row id and had to go and compare the standings by hand to see
whether the credit landed. :func:`capture` reads those numbers inside the
action's transaction, once before the row is applied and once after, and
:func:`receipt` turns the pair into what the review panel shows.

The reads are plain (unlocked) queries. The apply path takes its own row
locks afterwards; locking here as well would change the lock order the
worker relies on. So on a busy event a concurrent auto submission that
commits between the two reads is counted in the difference too. The numbers
are what the standings showed at each moment, not a strict attribution.

Player points follow the standings' own definition:

- ordinary events: the player's summed ``EventPlayerPoints`` shares for the
  event. Shares are only written when a task completes, so partial progress
  leaves them unchanged, which is correct.
- sotw/botw races: the player's ranked value from the race fold (XP or kills
  gained, or combined points under points ranking).
"""
from __future__ import annotations

from typing import Optional


def _num(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _race_unit(config) -> str:
    """What a competition's ranked number counts."""
    if getattr(config, "ranking_mode", None) == "points":
        return "points"
    return "kills" if getattr(config, "metric_kind", None) == "boss" else "xp"


def _player_race_value(session, task: dict, player_id) -> tuple:
    """``(value, unit)`` for one player's ranked number on a race task."""
    from services.competition import rank_value
    from services.event_engine import _competition_event_rows, _competition_fold

    rows = _competition_event_rows(session, task)
    per, config = _competition_fold(session, task, None, rows=rows)
    entry = per.get(player_id)
    return (_num(rank_value(entry, config)) if entry else 0.0), _race_unit(config)


def _player_event_points(session, event_id: int, player_id) -> float:
    from sqlalchemy import func

    from db.models import EventPlayerPoints

    total = (session.query(func.coalesce(func.sum(EventPlayerPoints.points), 0))
             .filter(EventPlayerPoints.event_id == event_id,
                     EventPlayerPoints.player_id == player_id)
             .scalar())
    return _num(total)


def capture(session, *, event_id: int, task: dict, team_id,
            player_id=None) -> dict:
    """Snapshot the numbers a manual credit can move.

    ``task`` is the engine's task dict (``event_engine._task_to_dict``).
    Never raises: the receipt is a convenience, and a failed read must not
    undo the organizer's award. A part that can't be read comes back None.
    """
    from db.models import EventProgress, EventTeam

    snap: dict = {"team_score": None, "progress": None, "completed": None,
                  "player_value": None, "player_unit": "points"}
    try:
        row = (session.query(EventTeam.score)
               .filter(EventTeam.id == team_id).first())
        snap["team_score"] = _num(row[0]) if row else None
    except Exception:
        pass
    try:
        row = (session.query(EventProgress.progress, EventProgress.completed)
               .filter(EventProgress.task_id == task["id"],
                       EventProgress.team_id == team_id).first())
        snap["progress"] = _num(row[0]) if row else 0.0
        snap["completed"] = bool(row[1]) if row else False
    except Exception:
        pass
    if player_id is not None:
        try:
            if task.get("type") == "competition":
                value, unit = _player_race_value(session, task, player_id)
                snap["player_value"], snap["player_unit"] = value, unit
            else:
                snap["player_value"] = _player_event_points(
                    session, event_id, player_id)
        except Exception:
            pass
    return snap


def _pair(before, after) -> Optional[dict]:
    if before is None or after is None:
        return None
    return {"before": before, "after": after, "delta": round(after - before, 2)}


def receipt(before: dict, after: dict, *, team: dict, task: dict,
            player: Optional[dict] = None, threshold=None,
            score_unit: str = "points") -> dict:
    """The response body's ``score_change``: each moved number as
    ``{before, after, delta}`` (None when it couldn't be read).

    ``team`` / ``player`` are ``{"id", "name"}``; ``threshold`` is the task's
    completion target for this team, when it has one."""
    out = {
        "team": {**team, "unit": score_unit,
                 "score": _pair(before.get("team_score"), after.get("team_score"))},
        "task": {
            "id": task.get("id"),
            "label": task.get("label"),
            "progress": _pair(before.get("progress"), after.get("progress")),
            "threshold": threshold,
            "completed_before": before.get("completed"),
            "completed_after": after.get("completed"),
        },
        "player": None,
    }
    if player is not None:
        out["player"] = {
            **player,
            "unit": after.get("player_unit") or "points",
            "value": _pair(before.get("player_value"), after.get("player_value")),
        }
    return out


def score_unit_for(task: dict) -> str:
    """The unit a team's score is kept in for this task's event. A race's
    team score is its ranked number; everything else is points."""
    if task.get("type") != "competition":
        return "points"
    try:
        from services.competition import CompetitionConfig

        return _race_unit(CompetitionConfig(task.get("config") or {}))
    except Exception:
        return "points"


def threshold_for(session, task: dict, team_id):
    """The task's completion target for this team (None for races and
    anything the engine can't size)."""
    if task.get("type") == "competition":
        return None
    try:
        from services.event_engine import effective_threshold

        value = effective_threshold(session, task, team_id)
        return _num(value) if value else None
    except Exception:
        return None


def _names(session, team_id, player_id) -> tuple:
    """``(team_name, player_name)``, each None when unknown."""
    team_name = player_name = None
    try:
        from db.models import EventTeam

        row = session.query(EventTeam.name).filter(EventTeam.id == team_id).first()
        team_name = row[0] if row else None
    except Exception:
        pass
    if player_id is not None:
        try:
            from db.models import Player

            row = (session.query(Player.player_name)
                   .filter(Player.player_id == player_id).first())
            player_name = row[0] if row else None
        except Exception:
            pass
    return team_name, player_name


def finish(session, before: dict, *, event_id: int, task: dict, team_id,
           player_id=None) -> Optional[dict]:
    """Take the after-snapshot (call it once the row is applied, before the
    commit) and build the receipt. Returns None rather than raising."""
    try:
        after = capture(session, event_id=event_id, task=task,
                        team_id=team_id, player_id=player_id)
        team_name, player_name = _names(session, team_id, player_id)
        player = None
        if player_id is not None:
            player = {"id": player_id,
                      "name": player_name or f"Player {player_id}"}
        return receipt(
            before, after,
            team={"id": team_id, "name": team_name},
            task=task, player=player,
            threshold=threshold_for(session, task, team_id),
            score_unit=score_unit_for(task))
    except Exception:
        return None
