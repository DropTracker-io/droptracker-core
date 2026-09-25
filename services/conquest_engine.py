"""Conquest: the database side of the ``conquest`` event kind.

:mod:`services.conquest` decides what a troop does; this module loads and
locks the rows, writes the results and fans out the side effects. Callers:

- the event engine's apply/revoke paths (:func:`apply_conquest`,
  :func:`revoke_conquest`), both in the event worker and in the web API's
  confirm/revoke routes;
- the lifecycle: :func:`conquest_blockers` before activation,
  :func:`seed_conquest` at activation, :func:`settle_conquest` on every 60s
  sweep tick, :func:`finalize_conquest` + :func:`conquest_final_standings` at
  the end, :func:`forget_team` when a team is deleted;
- the Conquest routes: :func:`conquest_payload`, :func:`battles_page`,
  :func:`adjust_tile`.

Never imports ``web_api`` (the event worker runs this module).

Lock order, everywhere: EventProgress (task, team) -> region -> tile -> troop
book. Team rows are only ever locked by :func:`settle_conquest`, which takes
nothing else, so the worker's parallel apply lanes can never deadlock against
a score write (the models carry no team FKs for the same reason). That is also
why the apply path never writes team scores: the sweep materializes them from
the ownership history within a minute, in both scoring modes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

APPLIED_STATUSES = ("auto", "confirmed", "manual")
# Battle-log outcomes worth showing in the feed (fortify/full are bookkeeping).
FEED_OUTCOMES = ("claim", "capture", "attack", "breach", "repelled", "adjust")
# Outcomes that post an event_conquest_battle message (one per apply).
BATTLE_POST_OUTCOMES = ("attack", "breach", "repelled")
MAX_BATTLE_LINES = 4
# No periodic summary this close to the end: the wrap-up post covers it.
SUMMARY_MIN_LEFT = timedelta(hours=1)
IMG_BASE = "https://www.droptracker.io/img"
_MEDALS = ("\U0001F947", "\U0001F948", "\U0001F949")


def _cq():
    from services import conquest

    return conquest


def _ts(dt) -> Optional[int]:
    return int(dt.timestamp()) if dt else None


def _threshold(task: dict) -> int:
    """Progress per batch of troops: the task's target (additive task types
    only reach this module — see conquest.RULE_TASK_TYPES)."""
    try:
        return max(int(task.get("target_value") or 0), 1)
    except (TypeError, ValueError):
        return 1


def _dice_str(faces) -> Optional[str]:
    return ",".join(str(int(f)) for f in faces) if faces else None


def _dice_list(raw) -> list:
    out = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def tile_icon_url(tile) -> Optional[str]:
    if getattr(tile, "icon_item_id", None):
        return f"{IMG_BASE}/itemdb/{int(tile.icon_item_id)}.png"
    if getattr(tile, "icon_npc_id", None):
        return f"{IMG_BASE}/npcdb/{int(tile.icon_npc_id)}.png"
    return None


def event_window(event, now: Optional[datetime] = None) -> tuple:
    """``(start, end)`` of an Event row's scoring window — scheduled dates
    narrowed by the explicit activate/end stamps (the engine's rule), with
    the end clamped to ``now``. Either side may be None."""
    starts = [d for d in (event.starts_at, event.activated_at) if d is not None]
    ends = [d for d in (event.ends_at, event.ended_at) if d is not None]
    start = max(starts) if starts else None
    end = min(ends) if ends else None
    if now is not None:
        end = min(end, now) if end is not None else now
    return start, end


# --------------------------------------------------------------------------- #
# Row helpers
# --------------------------------------------------------------------------- #
def load_map(session, event_id: int):
    from db.models import ConquestMap

    return (session.query(ConquestMap)
            .filter(ConquestMap.event_id == event_id).first())


def ensure_map(session, event_id: int):
    """The event's map row, created with default settings when missing."""
    row = load_map(session, event_id)
    if row is None:
        from db.models import ConquestMap

        row = ConquestMap(event_id=event_id, revision=0)
        session.add(row)
        session.flush()
    return row


def map_settings(session, event_id: int) -> dict:
    row = load_map(session, event_id)
    return _cq().conquest_settings(row.settings if row is not None else None)


def _team_names(session, team_ids) -> dict:
    ids = sorted({int(t) for t in team_ids if t is not None})
    if not ids:
        return {}
    from db.models import EventTeam

    return {tid: name for tid, name in (
        session.query(EventTeam.id, EventTeam.name)
        .filter(EventTeam.id.in_(ids)).all())}


def _team_label(names: dict, team_id) -> str:
    if team_id is None:
        return "Nobody"
    return names.get(team_id) or f"Team {team_id}"


def _engine():
    from services import event_engine

    return event_engine


def _publish(event_id: int, frame: dict) -> None:
    try:
        _engine()._publish(event_id, frame)
    except Exception:
        pass


def _enqueue(session, notification_type: str, event: dict, player_id, data: dict) -> None:
    """Queue a Discord post. A troop credited without a player (a manual
    award) borrows one from the acting team: notification_queue.player_id is
    NOT NULL and the post is about the team anyway."""
    engine = _engine()
    if player_id is None:
        player_id = engine._team_representative_player(session, data.get("team_id"))
    engine._enqueue_notification(session, notification_type, event, player_id, data)


def _move_hold(session, event_id: int, tile_id: int, new_owner, now: datetime) -> None:
    """Close the tile's open hold and open one for ``new_owner`` (None = the
    tile went back to nobody). Caller holds the tile lock."""
    from db.models import ConquestHold

    (session.query(ConquestHold)
     .filter(ConquestHold.tile_id == tile_id, ConquestHold.ended_at.is_(None))
     .update({ConquestHold.ended_at: now}, synchronize_session=False))
    if new_owner is not None:
        session.add(ConquestHold(event_id=event_id, tile_id=tile_id,
                                 team_id=int(new_owner), started_at=now))


def _refresh_region(session, region, now: datetime) -> Optional[tuple]:
    """Re-derive who controls ``region`` from its tiles (flushed first).
    Returns ``(previous_owner, new_owner)`` when control changed."""
    if region is None:
        return None
    from db.models import ConquestTile

    session.flush()
    owners = [owner for owner, kind in (
        session.query(ConquestTile.owner_team_id, ConquestTile.kind)
        .filter(ConquestTile.region_id == region.id).all())
        if (kind or "normal") == "normal"]
    new_owner = _cq().region_owner(owners)
    if new_owner == region.owner_team_id:
        return None
    previous = region.owner_team_id
    region.owner_team_id = new_owner
    region.owner_since = now if new_owner is not None else None
    return previous, new_owner


def _tile_counts(session, event_id: int) -> dict:
    """{team_id: tiles owned now} for one event."""
    from sqlalchemy import func

    from db.models import ConquestTile

    return {tid: n for tid, n in (
        session.query(ConquestTile.owner_team_id, func.count(ConquestTile.id))
        .filter(ConquestTile.event_id == event_id,
                ConquestTile.owner_team_id.isnot(None))
        .group_by(ConquestTile.owner_team_id).all())}


# --------------------------------------------------------------------------- #
# Apply / revoke (the event engine's conquest branch)
# --------------------------------------------------------------------------- #
def apply_conquest(session, redis_conn, event: dict, task: dict, completion,
                   player_name: Optional[str] = None, *, rng=None,
                   now: Optional[datetime] = None) -> dict:
    """Fold one applied ledger row into the (task, team) running total, turn
    every newly crossed multiple of the task's target into troops on the
    rule's tile, resolve them, and announce what happened. Caller owns the
    transaction; this only flushes. Never "completes" the task."""
    from db.models import (
        ConquestBattle,
        ConquestRegion,
        ConquestRule,
        ConquestTile,
        ConquestTroops,
        EventProgress,
    )

    cq = _cq()
    team_id = completion.team_id
    player_id = completion.player_id
    result = {"kind": "conquest", "event_id": event["id"], "task_id": task["id"],
              "team_id": team_id, "player_id": player_id, "troops": 0}
    if team_id is None:
        return result

    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .with_for_update()
                .first())
    if progress is None:
        progress = EventProgress(event_id=event["id"], task_id=task["id"],
                                 team_id=team_id, progress=0, completed=False)
        session.add(progress)
    previous = int(progress.progress or 0)
    current = previous + max(int(completion.quantity or 1), 1)
    progress.progress = current
    progress.completed = False
    threshold = _threshold(task)

    rule = (session.query(ConquestRule)
            .filter(ConquestRule.task_id == task["id"]).first())
    if rule is None:
        # A task no tile uses: it keeps its running total and nothing else.
        session.flush()
        return result
    earned = cq.troops_for_progress(previous, current, threshold, rule.troops)
    have, need = cq.progress_to_next(current, threshold)
    frame = {"kind": "conquest_progress", "event_id": event["id"],
             "task_id": task["id"], "tile_id": rule.tile_id, "team_id": team_id,
             "player_id": player_id, "progress": have, "target": need}
    if player_name:
        frame["player_name"] = player_name
    result.update(tile_id=rule.tile_id, progress=have, target=need)
    if earned <= 0:
        session.flush()
        _publish(event["id"], frame)
        return result

    # Region before tile (the lock order in the module docstring).
    region_id = (session.query(ConquestTile.region_id)
                 .filter(ConquestTile.id == rule.tile_id).scalar())
    region = None
    if region_id is not None:
        region = (session.query(ConquestRegion)
                  .filter(ConquestRegion.id == region_id)
                  .with_for_update().first())
    tile = (session.query(ConquestTile)
            .filter(ConquestTile.id == rule.tile_id)
            .with_for_update().first())
    if tile is None or (tile.kind or "normal") != "normal":
        session.flush()
        return result

    book = (session.query(ConquestTroops)
            .filter(ConquestTroops.tile_id == tile.id,
                    ConquestTroops.team_id == team_id)
            .with_for_update().first())
    if book is None:
        book = ConquestTroops(event_id=event["id"], tile_id=tile.id,
                              team_id=team_id, earned=0, debt=0)
        session.add(book)
    book.earned = int(book.earned or 0) + earned
    paid = min(int(book.debt or 0), earned)
    book.debt = int(book.debt or 0) - paid
    usable = earned - paid
    result["troops"] = earned
    if paid:
        result["troop_debt_paid"] = paid

    settings = map_settings(session, event["id"])
    now = now or datetime.now()
    owner_before = tile.owner_team_id
    outcomes = cq.resolve_troops(tile.owner_team_id, tile.defense, team_id, usable,
                                 settings, rng or cq.make_rng())
    captures = 0
    for o in outcomes:
        session.add(ConquestBattle(
            event_id=event["id"], tile_id=tile.id, team_id=team_id,
            outcome=o.outcome, owner_before=o.owner_before,
            owner_after=o.owner_after, defense_before=o.defense_before,
            defense_after=o.defense_after,
            attack_dice=_dice_str(o.attack_dice), defense_dice=_dice_str(o.defense_dice),
            player_id=player_id, completion_id=getattr(completion, "id", None),
            task_id=task["id"], source="troop", created_at=now,
        ))
        if o.captured:
            captures += 1
            _move_hold(session, event["id"], tile.id, o.owner_after, now)
    if outcomes:
        tile.owner_team_id = outcomes[-1].owner_after
        tile.defense = outcomes[-1].defense_after
        tile.last_battle_at = now
        if captures:
            tile.captures = int(tile.captures or 0) + captures
            tile.owner_since = now
    region_change = _refresh_region(session, region, now) if captures else None
    session.flush()

    result.update(owner_team_id=tile.owner_team_id, defense=tile.defense,
                  outcomes=[o.outcome for o in outcomes], captured=bool(captures))
    frame.update(kind="conquest", owner_team_id=tile.owner_team_id,
                 defense=tile.defense, troops=earned,
                 outcomes=[o.outcome for o in outcomes], captured=bool(captures))
    if region_change is not None:
        frame["region_id"] = region.id
        frame["region_owner_team_id"] = region.owner_team_id
    _publish(event["id"], frame)

    try:
        _announce_troops(session, event, task, completion, player_name, tile, region,
                         owner_before, outcomes, region_change, settings, earned)
    except Exception:
        # A broken announcement must never undo the battle itself.
        log.exception("conquest announce failed (event %s, tile %s)",
                      event["id"], tile.id)
    return result


def _announce_troops(session, event: dict, task: dict, completion, player_name,
                     tile, region, owner_before, outcomes, region_change,
                     settings: dict, earned: int) -> None:
    cq = _cq()
    team_id = completion.team_id
    player_id = completion.player_id
    involved = {team_id, owner_before}
    involved |= {o.owner_before for o in outcomes}
    if region_change:
        involved |= set(region_change)
    names = _team_names(session, involved)
    team = _team_label(names, team_id)
    via = completion.matched_target or task.get("label")
    by = f"**{player_name}**" if player_name else "the team"
    base = {
        "team_id": team_id, "team_name": team, "player_id": player_id,
        "player_name": player_name, "task_id": task["id"],
        "task_label": task.get("label"), "tile_id": tile.id,
        "tile_label": tile.label, "region_name": region.name if region else None,
        "troops": earned, "conquest_icon": tile_icon_url(tile),
    }
    earned_line = (f"-# {earned} troop{'s' if earned != 1 else ''} earned by {by}"
                   + (f" ({via})" if via else ""))

    for o in outcomes:
        if not o.captured:
            continue
        previous = names.get(o.owner_before) if o.owner_before else None
        held = _tile_counts(session, event["id"]).get(team_id, 0)
        data = dict(base, previous_team_id=o.owner_before,
                    previous_team_name=previous)
        data["conquest_headline"] = cq.outcome_headline(
            o.outcome, team=team, tile=tile.label, owner=previous)
        data["conquest_detail_line"] = (
            f"{earned_line}\n-# {team} now holds {held} tile{'s' if held != 1 else ''}")
        _enqueue(session, "event_conquest_capture", event, player_id, data)

    fights = [o for o in outcomes if o.outcome in BATTLE_POST_OUTCOMES]
    if fights:
        decisive = next((o for o in fights if o.outcome == "breach"), fights[-1])
        defender = (names.get(decisive.owner_before) if decisive.owner_before
                    else "the neutral garrison")
        lines = [line for line in (cq.battle_detail(o) for o in fights[:MAX_BATTLE_LINES])
                 if line]
        if len(fights) > MAX_BATTLE_LINES:
            lines.append(f"-# and {len(fights) - MAX_BATTLE_LINES} more attacks")
        data = dict(base, defender_team_id=decisive.owner_before,
                    defender_team_name=defender, outcome=decisive.outcome)
        data["conquest_headline"] = cq.outcome_headline(
            decisive.outcome, team=team, tile=tile.label, owner=defender)
        data["conquest_dice_line"] = "\n".join(lines) or None
        tail = ("\n-# The next enemy troop takes it."
                if decisive.outcome == "breach" else "")
        data["conquest_detail_line"] = earned_line + tail
        _enqueue(session, "event_conquest_battle", event, player_id, data)

    if region_change:
        previous, new_owner = region_change
        per = ("per hour held" if settings["scoring_mode"] == "hold_time"
               else "at the end if held")
        bonus = cq.fmt_points(region.bonus or 0)
        data = dict(base, region_id=region.id, region_owner_team_id=new_owner,
                    previous_team_id=previous)
        if new_owner is not None:
            data["team_id"] = new_owner
            data["team_name"] = _team_label(names, new_owner)
            data["conquest_headline"] = (
                f"\U0001F451 **{_team_label(names, new_owner)}** now controls "
                f"**{region.name}**")
            detail = f"-# Worth +{bonus} points {per}"
            if previous is not None:
                detail += f", taken from **{_team_label(names, previous)}**"
        else:
            data["team_id"] = previous
            data["team_name"] = _team_label(names, previous)
            data["conquest_headline"] = (
                f"\U0001F494 **{_team_label(names, previous)}** lost control of "
                f"**{region.name}**")
            detail = f"-# **{team}** broke the hold by taking **{tile.label}**"
        data["conquest_detail_line"] = detail
        _enqueue(session, "event_conquest_region", event, player_id, data)


def revoke_conquest(session, event: dict, task: dict, team_id, completion) -> dict:
    """Re-fold a (task, team) total after one of its ledger rows was revoked
    (the caller already flipped the row). Dice can't be un-rolled, so troops
    the row had earned become debt on the rule's tile: the team's next troops
    there pay it off before doing anything. Returns the audit summary."""
    from db.models import ConquestRule, ConquestTroops, EventCompletion, EventProgress

    cq = _cq()
    summary = {"progress": 0, "completed": False, "team_score": None, "troop_debt": 0}
    if team_id is None:
        return summary
    survivors = (session.query(EventCompletion.quantity, EventCompletion.source_type)
                 .filter(EventCompletion.task_id == task["id"],
                         EventCompletion.team_id == team_id,
                         EventCompletion.status.in_(APPLIED_STATUSES))
                 .all())
    current = sum(max(int(q or 1), 1) for q, source in survivors
                  if (source or "") != "bonus")
    progress = (session.query(EventProgress)
                .filter(EventProgress.task_id == task["id"],
                        EventProgress.team_id == team_id)
                .with_for_update().first())
    previous = int(progress.progress or 0) if progress is not None else 0
    if progress is None:
        if current <= 0:
            return summary
        progress = EventProgress(event_id=event["id"], task_id=task["id"],
                                 team_id=team_id, progress=0, completed=False)
        session.add(progress)
    progress.progress = current
    progress.completed = False
    summary["progress"] = current

    rule = (session.query(ConquestRule)
            .filter(ConquestRule.task_id == task["id"]).first())
    threshold = _threshold(task)
    lost = 0
    if rule is not None:
        lost = max(-cq.troops_for_progress(previous, current, threshold, rule.troops), 0)
    if lost:
        book = (session.query(ConquestTroops)
                .filter(ConquestTroops.tile_id == rule.tile_id,
                        ConquestTroops.team_id == team_id)
                .with_for_update().first())
        if book is None:
            book = ConquestTroops(event_id=event["id"], tile_id=rule.tile_id,
                                  team_id=team_id, earned=0, debt=0)
            session.add(book)
        book.debt = int(book.debt or 0) + lost
        book.earned = max(int(book.earned or 0) - lost, 0)
        summary["troop_debt"] = lost
    session.flush()
    have, need = cq.progress_to_next(current, threshold)
    _publish(event["id"], {
        "kind": "revoke", "event_id": event["id"], "task_id": task["id"],
        "team_id": team_id, "progress": have, "target": need,
        "tile_id": rule.tile_id if rule is not None else None,
        "troop_debt": lost,
    })
    return summary


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def conquest_blockers(session, event) -> list:
    """Structured activation blockers (event_lifecycle.activation_blocker_items
    shape). ``target`` "board" is the manager's map tab."""
    from db.models import ConquestRule, ConquestTile, EventTask, EventTeam

    blockers = []
    tiles = (session.query(ConquestTile)
             .filter(ConquestTile.event_id == event.id)
             .order_by(ConquestTile.idx, ConquestTile.id).all())
    normal = [t for t in tiles if (t.kind or "normal") == "normal"]
    if len(normal) < 2:
        blockers.append({
            "code": "conquest_no_map", "target": "board",
            "message": ("The map needs at least two tiles. Build it in the map "
                        "designer, or start from the Gielinor preset."),
        })
        return blockers
    task_ids = {tid for (tid,) in session.query(EventTask.id)
                .filter(EventTask.event_id == event.id).all()}
    ruled = {r.tile_id for r in session.query(ConquestRule)
             .filter(ConquestRule.event_id == event.id).all()
             if r.task_id in task_ids}
    bare = [t.label for t in normal if t.id not in ruled]
    if bare:
        more = f" and {len(bare) - 5} more" if len(bare) > 5 else ""
        blockers.append({
            "code": "conquest_tiles_without_rules", "target": "board",
            "message": ("Every tile needs a way to earn troops. Missing on: "
                        + ", ".join(bare[:5]) + more + "."),
        })
    is_cvc = (getattr(event, "mode", None) or "standard") == "clan_vs_clan"
    team_count = (session.query(EventTeam)
                  .filter(EventTeam.event_id == event.id).count())
    if not is_cvc and team_count == 1:
        blockers.append({
            "code": "conquest_min_teams", "target": "teams",
            "message": "Conquest needs at least two teams to fight over the map.",
        })
    settings = map_settings(session, event.id)
    if settings["start_mode"] == "dealt" and team_count and len(normal) < team_count:
        blockers.append({
            "code": "conquest_deal_short", "target": "board",
            "message": (f"Dealing the map needs a tile per team: {team_count} teams "
                        f"but only {len(normal)} tiles."),
        })
    if getattr(event, "schedule_config", None):
        blockers.append({
            "code": "schedule_conquest", "target": "dates",
            "message": ("Conquest events can't use a recurring schedule yet. Remove "
                        "the schedule or change the event type."),
        })
    return blockers


def _tile_dicts(tiles) -> list:
    return [{"id": t.id, "value": float(t.value or 0), "region_id": t.region_id,
             "kind": t.kind or "normal", "owner_team_id": t.owner_team_id}
            for t in tiles]


def seed_conquest(session, event, now: Optional[datetime] = None, *, rng=None) -> dict:
    """Deal the starting map at activation (idempotent via
    ``ConquestMap.seeded_at``): every tile reset to unowned with the neutral
    garrison, then, for ``start_mode == "dealt"``, dealt out evenly with the
    starting defense and an open hold from ``now``."""
    from db.models import (
        ConquestBattle,
        ConquestHold,
        ConquestRegion,
        ConquestTile,
        ConquestTroops,
        EventTeam,
    )

    cq = _cq()
    now = now or datetime.now()
    map_row = ensure_map(session, event.id)
    if map_row.seeded_at is not None:
        return {"seeded": False}
    settings = cq.conquest_settings(map_row.settings)
    # A clean start: nothing a pre-start test left behind carries over.
    for model in (ConquestHold, ConquestBattle, ConquestTroops):
        (session.query(model).filter(model.event_id == event.id)
         .delete(synchronize_session=False))
    tiles = session.query(ConquestTile).filter(ConquestTile.event_id == event.id).all()
    for t in tiles:
        normal = (t.kind or "normal") == "normal"
        t.owner_team_id = None
        t.defense = settings["neutral_defense"] if normal else 0
        t.owner_since = None
        t.captures = 0
        t.last_battle_at = None
    dealt = {}
    if settings["start_mode"] == "dealt":
        team_ids = [tid for (tid,) in session.query(EventTeam.id)
                    .filter(EventTeam.event_id == event.id)
                    .order_by(EventTeam.id).all()]
        normal_tiles = [t for t in tiles if (t.kind or "normal") == "normal"]
        dealt = cq.deal_tiles([(t.id, t.value) for t in normal_tiles], team_ids,
                              rng or cq.make_rng())
        by_id = {t.id: t for t in tiles}
        for tile_id, team_id in dealt.items():
            tile = by_id[tile_id]
            tile.owner_team_id = team_id
            tile.defense = settings["start_defense"]
            tile.owner_since = now
            session.add(ConquestHold(event_id=event.id, tile_id=tile_id,
                                     team_id=team_id, started_at=now))
    session.flush()
    owners = cq.region_owners(_tile_dicts(tiles))
    for region in session.query(ConquestRegion).filter(
            ConquestRegion.event_id == event.id).all():
        region.owner_team_id = owners.get(region.id)
        region.owner_since = now if region.owner_team_id is not None else None
    map_row.seeded_at = now
    map_row.settled_at = None
    map_row.summary_at = now
    session.flush()
    return {"seeded": True, "dealt": len(dealt)}


def compute_standings(session, event, settings: Optional[dict] = None,
                      now: Optional[datetime] = None) -> dict:
    """{team_id: conquest.TeamStanding} for an Event row, as of ``now``."""
    from db.models import ConquestHold, ConquestRegion, ConquestTile, EventTeam

    cq = _cq()
    settings = settings or map_settings(session, event.id)
    tiles = session.query(ConquestTile).filter(ConquestTile.event_id == event.id).all()
    regions = [{"id": rid, "bonus": float(bonus or 0)} for rid, bonus in (
        session.query(ConquestRegion.id, ConquestRegion.bonus)
        .filter(ConquestRegion.event_id == event.id).all())]
    team_ids = [tid for (tid,) in session.query(EventTeam.id)
                .filter(EventTeam.event_id == event.id).all()]
    holds = []
    if settings["scoring_mode"] == "hold_time":
        holds = [cq.Hold(tile_id, team_id, start, end) for tile_id, team_id, start, end in (
            session.query(ConquestHold.tile_id, ConquestHold.team_id,
                          ConquestHold.started_at, ConquestHold.ended_at)
            .filter(ConquestHold.event_id == event.id).all())]
    start, end = event_window(event, now or datetime.now())
    return cq.compute_standings(settings["scoring_mode"], _tile_dicts(tiles), regions,
                                holds, team_ids, start, end)


def settle_conquest(session, redis_conn, event, now: Optional[datetime] = None, *,
                    announce: bool = True) -> dict:
    """Materialize every team's score from the map (the lifecycle sweep, each
    tick). Writes ``EventTeam.score`` absolutely, announces a lead change and
    posts the periodic map update when due. The caller commits."""
    from db.models import EventTeam

    now = now or datetime.now()
    map_row = load_map(session, event.id)
    if map_row is None or map_row.seeded_at is None:
        return {"settled": False}
    settings = _cq().conquest_settings(map_row.settings)
    standings = compute_standings(session, event, settings, now)
    engine = _engine()
    ev_dict = engine._event_to_dict(event)
    lead_before = engine._leader_snapshot(session, ev_dict) if announce else None
    teams = (session.query(EventTeam)
             .filter(EventTeam.event_id == event.id)
             .order_by(EventTeam.id)
             .with_for_update().all())
    changed = False
    for team in teams:
        st = standings.get(team.id)
        new_score = st.score if st is not None else 0.0
        if round(float(team.score or 0), 2) != new_score:
            team.score = new_score
            changed = True
    map_row.settled_at = now
    session.flush()
    if announce and changed:
        engine._announce_lead_change(session, ev_dict, lead_before, reason="conquest")
    if announce:
        _maybe_post_summary(session, event, ev_dict, map_row, settings, standings,
                            teams, now)
    return {"settled": True, "changed": changed}


def _maybe_post_summary(session, event, ev_dict: dict, map_row, settings: dict,
                        standings: dict, teams, now: datetime) -> None:
    hours = int(settings.get("summary_hours") or 0)
    if not hours or getattr(event, "status", None) != "active":
        return
    last = map_row.summary_at or map_row.seeded_at
    if last is not None and now - last < timedelta(hours=hours):
        return
    if event.ends_at is not None and event.ends_at - now < SUMMARY_MIN_LEFT:
        return
    from sqlalchemy import func

    from db.models import ConquestBattle

    cq = _cq()
    since = last or now - timedelta(hours=hours)
    gained = {tid: n for tid, n in (
        session.query(ConquestBattle.team_id, func.count(ConquestBattle.id))
        .filter(ConquestBattle.event_id == event.id,
                ConquestBattle.outcome.in_(cq.CAPTURE_OUTCOMES),
                ConquestBattle.source == "troop",
                ConquestBattle.created_at >= since)
        .group_by(ConquestBattle.team_id).all())}
    names = {t.id: t.name for t in teams}
    order = cq.rank_teams(standings)
    if not order:
        return
    rows = []
    for rank, tid in enumerate(order[:10]):
        st = standings[tid]
        medal = _MEDALS[rank] if rank < 3 else f"`#{rank + 1}`"
        parts = [f"{cq.fmt_points(st.score)} pts",
                 f"{st.tiles} tile{'s' if st.tiles != 1 else ''}"]
        if st.regions:
            parts.append(f"{st.regions} region{'s' if st.regions != 1 else ''}")
        if gained.get(tid):
            parts.append(f"+{gained[tid]} taken")
        rows.append(f"{medal} **{_team_label(names, tid)}** " + " · ".join(parts))
    taken = sum(gained.values())
    leader = order[0]
    data = {
        "team_id": leader, "team_name": _team_label(names, leader),
        "summary_hours": hours,
        "conquest_headline": "\U0001F5FA️ Map update",
        "conquest_summary_block": "\n".join(rows),
        "conquest_detail_line": (f"-# {taken} tile{'s' if taken != 1 else ''} "
                                 f"changed hands in the last {hours}h"),
    }
    _enqueue(session, "event_conquest_summary", ev_dict, None, data)
    map_row.summary_at = now


def finalize_conquest(session, event, now: Optional[datetime] = None) -> dict:
    """Final settlement at the end: scores up to the event's end, no posts
    (the wrap-up message carries the standings)."""
    return settle_conquest(session, None, event, now or event.ended_at or datetime.now(),
                           announce=False)


def conquest_final_standings(session, event, limit: int = 5) -> list:
    """``[{team_id, name, score, tiles, regions}]`` best first (score, then
    tiles held, then regions held)."""
    from db.models import EventTeam

    cq = _cq()
    standings = compute_standings(session, event)
    names = {tid: name for tid, name in session.query(EventTeam.id, EventTeam.name)
             .filter(EventTeam.event_id == event.id).all()}
    out = []
    for tid in cq.rank_teams(standings)[:limit]:
        st = standings[tid]
        score = round(st.score, 2)
        out.append({"team_id": tid, "name": names.get(tid) or f"Team {tid}",
                    "score": int(score) if score == int(score) else score,
                    "tiles": st.tiles, "regions": st.regions})
    return out


def forget_team_if_conquest(session, event_id: int, team_id: int) -> None:
    """:func:`forget_team` for a Conquest event, a no-op for any other kind.
    The team-delete cascade calls this for every event, so it checks the kind
    first: the Conquest tables may not exist at all on a database that has not
    run the web120a migration."""
    from db.models import Event

    kind = session.query(Event.kind).filter(Event.id == event_id).scalar()
    if kind == "conquest":
        forget_team(session, event_id, team_id)


def forget_team(session, event_id: int, team_id: int) -> None:
    """Scrub a team that is being deleted: its tiles and regions go back to
    nobody, and its holds and troop books go. Battle history stays (it names
    the team by id only). Flushes; the caller commits."""
    from db.models import ConquestHold, ConquestRegion, ConquestTile, ConquestTroops

    settings = map_settings(session, event_id)
    (session.query(ConquestTile)
     .filter(ConquestTile.event_id == event_id, ConquestTile.owner_team_id == team_id)
     .update({ConquestTile.owner_team_id: None,
              ConquestTile.defense: settings["neutral_defense"],
              ConquestTile.owner_since: None}, synchronize_session=False))
    (session.query(ConquestRegion)
     .filter(ConquestRegion.event_id == event_id,
             ConquestRegion.owner_team_id == team_id)
     .update({ConquestRegion.owner_team_id: None, ConquestRegion.owner_since: None},
             synchronize_session=False))
    for model in (ConquestHold, ConquestTroops):
        (session.query(model)
         .filter(model.event_id == event_id, model.team_id == team_id)
         .delete(synchronize_session=False))
    session.flush()


# --------------------------------------------------------------------------- #
# Reads (the Conquest routes)
# --------------------------------------------------------------------------- #
def _player_names(session, player_ids) -> dict:
    ids = sorted({int(p) for p in player_ids if p is not None})
    if not ids:
        return {}
    from db.models import Player

    return {pid: name for pid, name in session.query(Player.player_id, Player.player_name)
            .filter(Player.player_id.in_(ids)).all()}


def _battle_row(b, players: dict) -> dict:
    return {
        "id": int(b.id), "tile_id": b.tile_id, "team_id": b.team_id,
        "outcome": b.outcome, "owner_before": b.owner_before,
        "owner_after": b.owner_after, "defense_before": int(b.defense_before or 0),
        "defense_after": int(b.defense_after or 0),
        "attack_dice": _dice_list(b.attack_dice), "defense_dice": _dice_list(b.defense_dice),
        "player_id": b.player_id, "player_name": players.get(b.player_id),
        "source": b.source or "troop", "at": _ts(b.created_at),
    }


def conquest_payload(session, event, *, conceal: bool = False,
                     battles_limit: int = 40, now: Optional[datetime] = None) -> dict:
    """The whole map as the site draws it: settings, regions, tiles with
    their rules and each team's progress toward the next troop, the teams'
    live standings and the latest battles. ``conceal`` (tasks hidden from
    this viewer, web112a) drops the rules but keeps the map playable."""
    from db.models import (
        ConquestBattle,
        ConquestEdge,
        ConquestRegion,
        ConquestRule,
        ConquestTile,
        ConquestTroops,
        EventProgress,
        EventTask,
        EventTeam,
    )

    cq = _cq()
    now = now or datetime.now()
    map_row = load_map(session, event.id)
    settings = cq.conquest_settings(map_row.settings if map_row is not None else None)
    regions = (session.query(ConquestRegion)
               .filter(ConquestRegion.event_id == event.id)
               .order_by(ConquestRegion.sort, ConquestRegion.id).all())
    tiles = (session.query(ConquestTile)
             .filter(ConquestTile.event_id == event.id)
             .order_by(ConquestTile.idx, ConquestTile.id).all())
    rules = (session.query(ConquestRule)
             .filter(ConquestRule.event_id == event.id)
             .order_by(ConquestRule.tile_id, ConquestRule.sort, ConquestRule.id).all())
    edges = (session.query(ConquestEdge.tile_a_id, ConquestEdge.tile_b_id)
             .filter(ConquestEdge.event_id == event.id).all())
    teams = (session.query(EventTeam)
             .filter(EventTeam.event_id == event.id)
             .order_by(EventTeam.id).all())

    task_ids = [r.task_id for r in rules]
    tasks = {t.id: t for t in session.query(EventTask)
             .filter(EventTask.id.in_(task_ids)).all()} if task_ids else {}
    progress: dict = {}
    if task_ids and not conceal:
        for task_id, team_id, value in (
                session.query(EventProgress.task_id, EventProgress.team_id,
                              EventProgress.progress)
                .filter(EventProgress.task_id.in_(task_ids)).all()):
            progress[(task_id, team_id)] = int(value or 0)
    troops: dict = {}
    for tile_id, team_id, earned in (
            session.query(ConquestTroops.tile_id, ConquestTroops.team_id,
                          ConquestTroops.earned)
            .filter(ConquestTroops.event_id == event.id).all()):
        if earned:
            troops.setdefault(tile_id, {})[str(team_id)] = int(earned)

    rules_by_tile: dict = {}
    for r in rules:
        task = tasks.get(r.task_id)
        if task is None:
            continue
        target = max(int(task.target_value or 0), 1)
        rules_by_tile.setdefault(r.tile_id, []).append({
            "id": r.id, "task_id": r.task_id, "label": task.label, "type": task.type,
            "troops": int(r.troops or 1), "target": target,
            "progress": {str(team.id): progress[(r.task_id, team.id)] % target
                         for team in teams if progress.get((r.task_id, team.id))},
        })

    seeded = map_row is not None and map_row.seeded_at is not None
    standings = compute_standings(session, event, settings, now) if seeded else {}
    start, end = event_window(event, now)

    battle_rows = []
    if seeded and battles_limit > 0:
        battle_rows = (session.query(ConquestBattle)
                       .filter(ConquestBattle.event_id == event.id,
                               ConquestBattle.outcome.in_(FEED_OUTCOMES))
                       .order_by(ConquestBattle.id.desc())
                       .limit(int(battles_limit)).all())
    players = _player_names(session, [b.player_id for b in battle_rows])

    tile_ids_by_region: dict = {}
    for t in tiles:
        if t.region_id is not None:
            tile_ids_by_region.setdefault(t.region_id, []).append(t.id)

    return {
        "event_id": event.id,
        "status": getattr(event, "status", None),
        "settings": settings,
        "preset": map_row.preset if map_row is not None else None,
        "revision": int(map_row.revision or 0) if map_row is not None else 0,
        "seeded": seeded,
        "background_url": map_row.background_url if map_row is not None else None,
        "bg_width": map_row.bg_width if map_row is not None else None,
        "bg_height": map_row.bg_height if map_row is not None else None,
        "shape_width": map_row.shape_width if map_row is not None else None,
        "shape_height": map_row.shape_height if map_row is not None else None,
        "rules_hidden": bool(conceal),
        "regions": [{
            "id": r.id, "name": r.name, "color": r.color,
            "bonus": float(r.bonus or 0), "sort": int(r.sort or 0),
            "label_x": r.label_x, "label_y": r.label_y,
            "owner_team_id": r.owner_team_id, "owner_since": _ts(r.owner_since),
            "tile_ids": tile_ids_by_region.get(r.id, []), "shape": r.shape,
        } for r in regions],
        "tiles": [{
            "id": t.id, "idx": int(t.idx or 0), "label": t.label,
            "x": float(t.x), "y": float(t.y), "kind": t.kind or "normal",
            "value": float(t.value or 0), "region_id": t.region_id,
            "icon_npc_id": t.icon_npc_id, "icon_item_id": t.icon_item_id,
            "shape": t.shape,
            "owner_team_id": t.owner_team_id, "defense": int(t.defense or 0),
            "owner_since": _ts(t.owner_since), "captures": int(t.captures or 0),
            "rules": [] if conceal else rules_by_tile.get(t.id, []),
            "troops": troops.get(t.id, {}),
        } for t in tiles],
        "edges": [[a, b] for a, b in edges],
        "teams": [{
            "id": team.id, "name": team.name, "color": team.color,
            "score": round(float(team.score or 0), 2),
            "live_score": standings[team.id].score if team.id in standings else 0.0,
            "tiles": standings[team.id].tiles if team.id in standings else 0,
            "regions": standings[team.id].regions if team.id in standings else 0,
            "holding": round(standings[team.id].holding, 2) if team.id in standings else 0.0,
        } for team in teams],
        "battles": [_battle_row(b, players) for b in battle_rows],
        "window_start": _ts(start),
        "window_end": _ts(end),
        "now": _ts(now),
    }


def battles_page(session, event_id: int, *, before_id: Optional[int] = None,
                 limit: int = 50, tile_id: Optional[int] = None,
                 team_id: Optional[int] = None) -> dict:
    """One page of the battle log, newest first."""
    from sqlalchemy import or_

    from db.models import ConquestBattle

    limit = min(max(int(limit or 50), 1), 200)
    query = (session.query(ConquestBattle)
             .filter(ConquestBattle.event_id == event_id,
                     ConquestBattle.outcome.in_(FEED_OUTCOMES)))
    if before_id:
        query = query.filter(ConquestBattle.id < int(before_id))
    if tile_id:
        query = query.filter(ConquestBattle.tile_id == int(tile_id))
    if team_id:
        query = query.filter(or_(ConquestBattle.team_id == int(team_id),
                                 ConquestBattle.owner_before == int(team_id)))
    rows = query.order_by(ConquestBattle.id.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    players = _player_names(session, [b.player_id for b in rows])
    return {"battles": [_battle_row(b, players) for b in rows],
            "next_before": int(rows[-1].id) if more and rows else None}


# --------------------------------------------------------------------------- #
# Admin corrections
# --------------------------------------------------------------------------- #
class AdjustError(ValueError):
    """A bad admin adjustment (unknown tile/team, defense out of range)."""


def adjust_tile(session, event, tile_id: int, *, owner_team_id=None,
                defense: Optional[int] = None, actor_user_id=None,
                now: Optional[datetime] = None) -> dict:
    """Set a tile's owner and/or defense by hand (an organiser fixing a
    mistake). Logged as an ``adjust`` battle with ``source = 'admin'``; the
    next sweep re-scores. Silent on Discord: corrections don't announce."""
    from db.models import ConquestBattle, ConquestRegion, ConquestTile, EventTeam

    now = now or datetime.now()
    settings = map_settings(session, event.id)
    region_id = (session.query(ConquestTile.region_id)
                 .filter(ConquestTile.id == tile_id,
                         ConquestTile.event_id == event.id).scalar())
    region = None
    if region_id is not None:
        region = (session.query(ConquestRegion)
                  .filter(ConquestRegion.id == region_id)
                  .with_for_update().first())
    tile = (session.query(ConquestTile)
            .filter(ConquestTile.id == tile_id, ConquestTile.event_id == event.id)
            .with_for_update().first())
    if tile is None:
        raise AdjustError("No such tile on this map.")
    if (tile.kind or "normal") != "normal":
        raise AdjustError("Respawn tiles can't be owned.")
    if owner_team_id is not None:
        exists = (session.query(EventTeam.id)
                  .filter(EventTeam.id == owner_team_id,
                          EventTeam.event_id == event.id).first())
        if exists is None:
            raise AdjustError("That team isn't in this event.")
    new_defense = tile.defense if defense is None else int(defense)
    if not 0 <= new_defense <= settings["max_defense"]:
        raise AdjustError(f"Defense must be 0 to {settings['max_defense']}.")

    before_owner, before_defense = tile.owner_team_id, int(tile.defense or 0)
    if owner_team_id != before_owner:
        _move_hold(session, event.id, tile.id, owner_team_id, now)
        tile.owner_team_id = owner_team_id
        tile.owner_since = now if owner_team_id is not None else None
    tile.defense = new_defense
    session.add(ConquestBattle(
        event_id=event.id, tile_id=tile.id, team_id=owner_team_id,
        outcome="adjust", owner_before=before_owner, owner_after=owner_team_id,
        defense_before=before_defense, defense_after=new_defense,
        source="admin", created_at=now,
    ))
    _refresh_region(session, region, now)
    session.flush()
    _publish(event.id, {"kind": "conquest", "event_id": event.id, "tile_id": tile.id,
                        "owner_team_id": tile.owner_team_id, "defense": tile.defense,
                        "outcomes": ["adjust"], "captured": False})
    return {"tile_id": tile.id, "owner_team_id": tile.owner_team_id,
            "defense": tile.defense,
            "region_owner_team_id": region.owner_team_id if region is not None else None}
