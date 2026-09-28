"""Discord posts for board-game moments that are not a plain dice roll: every
shop item a team uses, a choose-task pick, and the mercy time-out that skips a
stuck task. Each becomes ONE ``event_board_action`` message.

Before 2026-09-28 only the four attacks (freeze, knockback, steal, reroll a
rival's task) were announced, so a team could reroll or skip its own task,
teleport, or even win on an item without Discord saying a word, and players
were left unsure which task a team was actually on. Now every moment that
changes a team's task, position or coming turns is posted, and anything that
lands a team on a new task names it.

The lines are composed here, at enqueue, so the legacy embed and the V2 layout
render the same text. :func:`compose_item_use` / :func:`compose_task_choice` /
:func:`compose_mercy` are pure (unit-tested); the ``announce_*`` wrappers look
up names and enqueue, and never raise into the caller.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# The item a rival's ward can name in its ``blocks`` list, as players say it.
_ATTACK_NAMES = {
    "freeze_opponent": "freeze",
    "knockback": "knockback",
    "steal_item": "steal",
    "reroll_opponent_task": "task reroll",
}


def _b(text) -> str:
    return f"**{text}**"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _move_lines(summary: dict, *, whose: Optional[str] = None,
                rolled: bool = False) -> list:
    """What a move did, as lines: the dice (when ``rolled``), the tiles, any
    roadblock / ladder / checkpoint / overshoot / toll it met, then the win or
    the new task. ``whose`` names the team in the new-task line when it is not
    the acting team (a knockback victim)."""
    from services.boardgame_engine import turn_notification_data
    from services.event_notifications import BOARD_TURN_LINE_KEYS

    summary = summary or {}
    turn = turn_notification_data(team_id=0, roll=summary)
    lines = []
    dice = summary.get("dice") or []
    if rolled and dice:
        lines.append(f"\U0001F3B2 Rolled `{turn['dice_str']}`")
    if summary.get("from") is not None and summary.get("to") is not None:
        lines.append(f"Tile `{summary['from']}` → `{summary['to']}`")
    lines.extend(str(turn[k]) for k in BOARD_TURN_LINE_KEYS if turn.get(k))
    if summary.get("won"):
        lines.append("\U0001F3C6 Reached the finish. The board is theirs!")
    elif summary.get("task_label"):
        label = "New task" if not whose else f"New task for {whose}"
        lines.append(f"{_b(label)} {summary['task_label']}")
    return lines


def _data(*, title: str, line: str, detail: list, actor: str, result: dict,
          victim: Optional[str] = None, next_task: Optional[str] = None,
          won: bool = False) -> dict:
    return {
        "action_title": title,
        "action_line": line,
        "action_detail_line": "\n".join(x for x in detail if x) or None,
        "team_name": actor,
        "target_team_name": victim,
        "item_name": result.get("item_name"),
        "effect": result.get("effect"),
        "absorbed": bool(result.get("absorbed")),
        "absorbed_by": result.get("absorbed_by"),
        "next_task_label": next_task,
        "won": bool(won),
    }


def compose_item_use(result: dict, actor: str, victim: Optional[str] = None) -> Optional[dict]:
    """The event_board_action payload for one item use, or None when there is
    nothing to announce (a refund, or a choose_task draw: that one is posted
    when the team makes its pick)."""
    result = result or {}
    if result.get("refunded"):
        return None
    effect = result.get("effect")
    item = _b(result.get("item_name") or "an item")
    a = _b(actor)
    v = _b(victim or "a rival")
    old = result.get("previous_task_label")
    new = result.get("task_label")

    if result.get("absorbed"):
        defense = result.get("absorbed_by") or "a defense"
        return _data(title="\U0001F6E1️ Attack blocked!",
                     line=f"{v} blocked {a}'s {item} with their {defense}!",
                     detail=[], actor=actor, victim=victim, result=result)

    if effect == "skip_task":
        roll = result.get("roll") or {}
        line = f"{a} used {item} to skip " + (_b(old) if old else "their task") + "."
        detail = (_move_lines(roll, rolled=True) if roll
                  else ["Roll the dice to move on."])
        return _data(title=("\U0001F3C6 " + actor + " reached the finish!"
                            if roll.get("won") else "⏭️ Task skipped"),
                     line=line, detail=detail, actor=actor, result=result,
                     next_task=roll.get("task_label"), won=bool(roll.get("won")))

    if effect == "reroll_task":
        line = f"{a} used {item} to reroll " + (_b(old) if old else "their task") + "."
        return _data(title="\U0001F504 Task rerolled", line=line,
                     detail=[f"{_b('New task')} {new}" if new else None],
                     actor=actor, result=result, next_task=new)

    if effect in ("advance", "reroll_move"):
        won = bool(result.get("won"))
        if effect == "advance":
            title = "✨ Teleport"
            line = f"{a} used {item} to teleport ahead"
        else:
            title = "\U0001F501 Move rerolled"
            line = f"{a} used {item} to redo their last move"
        line += (f", leaving {_b(old)} behind." if old else ".")
        if won:
            title = f"\U0001F3C6 {actor} reached the finish!"
        return _data(title=title, line=line,
                     detail=_move_lines(result, rolled=effect == "reroll_move"),
                     actor=actor, result=result, next_task=new, won=won)

    if effect == "knockback":
        tiles = int(result.get("tiles") or 0)
        line = (f"{a} knocked {v} back {_plural(tiles, 'tile')} with {item}"
                + (f", so {_b(old)} is gone." if old else "."))
        return _data(title="\U0001F4A5 Knocked back", line=line,
                     detail=_move_lines(result, whose=victim or "them"),
                     actor=actor, victim=victim, result=result, next_task=new)

    if effect == "reroll_opponent_task":
        line = (f"{a} used {item} to reroll {v}'s task"
                + (f" ({_b(old)})." if old else "."))
        label = f"New task for {victim or 'them'}"
        return _data(title="\U0001F52E Rival task rerolled", line=line,
                     detail=[f"{_b(label)} {new}" if new else None],
                     actor=actor, victim=victim, result=result, next_task=new)

    if effect == "freeze_opponent":
        rolls = int(result.get("frozen_rolls") or 0)
        line = (f"{a} froze {v}" + (f" for {_b(rolls)} rolls" if rolls > 1
                                    else " for their next roll" if rolls else "")
                + f" with {item}.")
        return _data(title="❄️ Frozen!", line=line,
                     detail=["-# Their dice still roll, but the piece stays put."],
                     actor=actor, victim=victim, result=result)

    if effect == "steal_item":
        stolen = result.get("stolen_item_name")
        line = (f"{a} stole " + (_b(stolen) if stolen else "an item")
                + f" from {v} with {item}.")
        return _data(title="\U0001F99D Item stolen", line=line, detail=[],
                     actor=actor, victim=victim, result=result)

    if effect == "roadblock":
        tile = result.get("roadblock_tile_idx")
        behavior = result.get("behavior") or {}
        stall = int(behavior.get("stall_turns") or 0)
        line = (f"{a} placed {item} on tile `{tile}`. The next team to reach it "
                "stops there" + (f" and loses {_plural(stall, 'turn')}." if stall
                                 else "."))
        return _data(title="\U0001F6A7 Roadblock placed", line=line, detail=[],
                     actor=actor, result=result)

    if effect == "boost_coins":
        mult = int(result.get("boost_multiplier") or 2)
        return _data(title="\U0001F4B0 Coin boost",
                     line=(f"{a} used {item}: coins from their next finished task "
                           f"are multiplied by {mult}."),
                     detail=[], actor=actor, result=result)

    if effect == "shield":
        return _data(title="\U0001F6E1️ Shield up",
                     line=f"{a} raised {item}. The next attack on them will be blocked.",
                     detail=[], actor=actor, result=result)

    if effect == "ward":
        blocks = [b for b in (result.get("blocks") or []) if b]
        if not blocks or "offensive" in blocks:
            what = "attack"
        else:
            what = " or ".join(_ATTACK_NAMES.get(b, b) for b in blocks)
        return _data(title="\U0001F6E1️ Ward up",
                     line=f"{a} used {item}. The next {what} aimed at them will be blocked.",
                     detail=[], actor=actor, result=result)

    if effect == "cleanse":
        parts = []
        if "freeze_opponent" in (result.get("cleansed") or []):
            parts.append("shook off a freeze")
        if result.get("unblocked"):
            parts.append("broke free of a roadblock stall")
        line = (f"{a} used {item} and " + " and ".join(parts) + "."
                if parts else f"{a} used {item}, but had nothing to clear.")
        return _data(title="✨ Cleansed", line=line,
                     detail=[f"{_b('New task')} {new}" if new else None],
                     actor=actor, result=result, next_task=new)

    if effect == "extra_dice":
        n = int(result.get("extra_dice") or 1)
        return _data(title="\U0001F3B2 Extra dice",
                     line=(f"{a} used {item}: {n} extra {'die' if n == 1 else 'dice'} "
                           "on their next roll."),
                     detail=[], actor=actor, result=result)

    if effect == "choose_roll":
        return _data(title="\U0001F3AF Roll chosen",
                     line=(f"{a} used {item}: their next roll will be "
                           f"{_b(result.get('chosen_roll'))}."),
                     detail=[], actor=actor, result=result)

    if effect == "coin_toll":
        per = int(result.get("coins_per_team") or 0)
        return _data(title="\U0001FA99 Coin toll armed",
                     line=(f"{a} used {item}: on their next move, every team they "
                           f"pass pays them {_b(per)} coins."),
                     detail=[], actor=actor, result=result)

    if effect == "choose_task":
        return None  # announced by the pick (compose_task_choice)

    return _data(title="\U0001F381 Item used", line=f"{a} used {item}.",
                 detail=[], actor=actor, result=result)


def compose_task_choice(result: dict, actor: str) -> dict:
    """The post for a choose_task pick: the team swapped its task for one of
    the drawn candidates."""
    result = result or {}
    old = result.get("previous_task_label")
    new = result.get("task_label")
    n = int(result.get("candidates") or 0)
    line = (f"{_b(actor)} picked a new task"
            + (f" from {n} choices" if n > 1 else "")
            + (f", replacing {_b(old)}." if old else "."))
    return _data(title="\U0001F52E Task chosen", line=line,
                 detail=[f"{_b('New task')} {new}" if new else None],
                 actor=actor, result={"effect": "choose_task"}, next_task=new)


def compose_mercy(actor: str, task_label: Optional[str],
                  roll: Optional[dict]) -> dict:
    """The post for the mercy rule: the team's task ran past its time limit
    and was skipped for no coins; auto games roll on at once."""
    roll = roll or {}
    line = (f"{_b(actor)} ran out of time on "
            + (_b(task_label) if task_label else "their task")
            + ", so it was skipped (no coins).")
    detail = (_move_lines(roll, rolled=True) if roll
              else ["Roll the dice to move on."])
    won = bool(roll.get("won"))
    return _data(title=(f"\U0001F3C6 {actor} reached the finish!" if won
                        else "⏳ Time's up"),
                 line=line, detail=detail, actor=actor,
                 result={"effect": "mercy"}, next_task=roll.get("task_label"),
                 won=won)


# --------------------------------------------------------------------------- #
# Enqueue wrappers (best-effort: a notification hiccup never fails the action)
# --------------------------------------------------------------------------- #
def _team_names(session, ids) -> dict:
    from db.models import EventTeam

    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return {}
    return {t.id: t.name for t in
            session.query(EventTeam).filter(EventTeam.id.in_(ids)).all()}


def _enqueue(session, ev, team_id: int, data: Optional[dict],
             target_team_id=None) -> None:
    if not data:
        return
    from services import event_engine
    from services.event_lifecycle import _representative_player_id

    rep = _representative_player_id(session, ev.id)
    if rep is None:
        return
    payload = {"team_id": int(team_id), **data}
    if target_team_id is not None:
        payload["target_team_id"] = int(target_team_id)
    event_engine._enqueue_notification(
        session, "event_board_action", event_engine._event_to_dict(ev), rep,
        payload)


def announce_item_use(session, ev, team_id: int, result: dict) -> None:
    try:
        target = (result or {}).get("target_team_id")
        names = _team_names(session, [team_id, target])
        actor = names.get(int(team_id)) or f"Team {team_id}"
        victim = (names.get(int(target)) or f"Team {target}") if target else None
        _enqueue(session, ev, team_id, compose_item_use(result, actor, victim),
                 target_team_id=target)
    except Exception:
        log.exception("board item announcement failed (event %s)",
                      getattr(ev, "id", None))


def announce_task_choice(session, ev, team_id: int, result: dict) -> None:
    try:
        actor = _team_names(session, [team_id]).get(int(team_id)) or f"Team {team_id}"
        _enqueue(session, ev, team_id, compose_task_choice(result, actor))
    except Exception:
        log.exception("board task-choice announcement failed (event %s)",
                      getattr(ev, "id", None))


def announce_mercy(session, ev, team_id: int, task_label: Optional[str],
                   roll: Optional[dict]) -> None:
    try:
        actor = _team_names(session, [team_id]).get(int(team_id)) or f"Team {team_id}"
        _enqueue(session, ev, team_id, compose_mercy(actor, task_label, roll))
    except Exception:
        log.exception("board mercy announcement failed (event %s)",
                      getattr(ev, "id", None))
