"""Event clan-point awards (web114a).

An event can pay out a clan's custom points (the ``player_points`` ledger the
group Points page, leaderboards and seasons all sum over) for two things:

**Placement** — "1st place: 100, 2nd: 50, 3rd: 25". Every member of a team
that finished in a paid place receives that place's amount; on SOTW/BOTW
(individual races) the player who finished there does. By default only members
who *took part* are paid (any credited contribution, tracked effort, or — on a
competition — any gain), so an AFK sign-up on the winning team earns nothing.
Ties share a place and the next place is skipped (standard competition ranking,
"1, 1, 3"); a team or player that scored nothing is never placed.

**Participation** — priced from EHE (Efficient Hours towards Event,
services/event_effort.py): ``per_hour`` points per EHE hour, an optional
``flat`` amount for everyone who took part, a ``min_hours`` floor and a
per-player ``max``. SOTW/BOTW record no EHE (the race metric IS the effort), so
there only the flat amount applies, to everyone who gained anything.

**Clans.** Each clan's payout is its own ``web_event_point_configs`` row: a
standard event has one (its group's); on a clan-vs-clan event every
participating clan configures what its own members earn in its own ledger — the
host can never mint points in an opponent's economy. Only current members of
the clan are ever paid.

**When.** ``award_mode`` "auto" awards in end_event's wrap-up (and the ended
announcement carries a ``{clan_points_line}``); "review" waits for an admin to
confirm the preview. Either way the award is re-runnable: every payout lands in
``web_event_point_awards`` naming the ``player_points`` row it wrote, so a
re-sync after a post-event revoke (the one standings correction still allowed
once an event is over) edits those rows in place, and a revoke deletes exactly
what the event paid. A group points reset deletes ``player_points`` rows out
from under the ledger; a re-sync leaves them deleted rather than resurrecting
points the clan wiped.

**EHE pricing is a single point of failure** (see bingo-ehb-effort): with the
WOM rate cache cold every WOM-priced boss reads 0h. An award that needs hours
therefore refuses to run on a cold cache — the auto path defers and the
lifecycle sweep retries — rather than paying a confident undercount.

The top half of this module is pure and stdlib-only (loaded by file path in
the unit tests, like services/event_prizes.py); everything touching the DB is
lazy-imported below the divider. Never import the ``web_api`` package from
here: the event worker calls this, and that package drags in every blueprint.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Iterable, Optional

# Re-declared locally so this module stays stdlib-only; keep in sync with
# db.models.events.EVENT_POINT_AWARD_MODES / EVENT_POINT_AWARD_KINDS.
AWARD_MODES = ("auto", "review")
AWARD_KINDS = ("placement", "participation")

# player_points.entry_type for event awards (NULL = submission award, 99 =
# manual admin adjustment); entry_id carries the event id.
PLACEMENT_ENTRY_TYPE = 10
PARTICIPATION_ENTRY_TYPE = 11
ENTRY_TYPE_FOR_KIND = {
    "placement": PLACEMENT_ENTRY_TYPE,
    "participation": PARTICIPATION_ENTRY_TYPE,
}

# Input ceilings. MAX_POINTS mirrors routes/points.py MAX_ADJUST — the largest
# single change an admin can make to one player's balance by hand.
MAX_PLACES = 10
MAX_POINTS = 1_000_000
MAX_PER_HOUR = 10_000
MAX_MIN_HOURS = 1_000

# Deferred auto awards (cold EHE pricing at end) are retried by the lifecycle
# sweep for this long; after that an admin awards from the manager.
DEFERRED_RETRY_DAYS = 7

# player_points.reason is VARCHAR(125).
_REASON_MAX_LEN = 125

DEFAULT_POINTS_CONFIG = {
    "enabled": False,
    "award_mode": "auto",
    # Points each member of the team (player, on SOTW/BOTW) finishing 1st,
    # 2nd, … receives. Empty = no placement awards.
    "placement": [],
    # Placement pays only members who took part.
    "placement_active_only": True,
    "participation": {
        "flat": 0,        # for everyone who took part (and met min_hours)
        "per_hour": 0,    # per EHE hour, rounded half-up per player
        "min_hours": 0,   # EHE floor below which nothing is paid (0 = none)
        "max": 0,         # per-player participation cap (0 = uncapped)
    },
}


class PointsConfigError(ValueError):
    """A config payload failed validation; the message is user-facing."""


# ══════════════════════════════════════════════════════════════════════════════
# Config (pure)
# ══════════════════════════════════════════════════════════════════════════════

def _int_in(value, lo: int, hi: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    value = int(value)
    return value if lo <= value <= hi else None


def _num_in(value, lo: float, hi: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if math.isnan(value) or not (lo <= value <= hi):
        return None
    return round(value, 2)


def _clean_placement(value) -> Optional[list]:
    """A list of per-place amounts (1st first), or None when invalid. Trailing
    zeros are trimmed — a zero place pays nothing, so it only means something
    ahead of a paid place ("nothing for 2nd, 10 for 3rd" is odd but legal)."""
    if not isinstance(value, list) or len(value) > MAX_PLACES:
        return None
    out = []
    for v in value:
        amount = _int_in(v, 0, MAX_POINTS)
        if amount is None:
            return None
        out.append(amount)
    while out and out[-1] == 0:
        out.pop()
    return out


def _parse(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def effective_points_config(raw) -> dict:
    """Full config for one clan's payout: defaults overlaid with the stored
    JSON. Corrupt or out-of-range values fall back to their defaults, so every
    caller gets every key back, always valid."""
    data = _parse(raw)
    cfg = json.loads(json.dumps(DEFAULT_POINTS_CONFIG))  # deep copy
    if isinstance(data.get("enabled"), bool):
        cfg["enabled"] = data["enabled"]
    if data.get("award_mode") in AWARD_MODES:
        cfg["award_mode"] = data["award_mode"]
    placement = _clean_placement(data.get("placement"))
    if placement is not None:
        cfg["placement"] = placement
    if isinstance(data.get("placement_active_only"), bool):
        cfg["placement_active_only"] = data["placement_active_only"]
    part = data.get("participation")
    if isinstance(part, dict):
        for key, parser in (("flat", lambda v: _int_in(v, 0, MAX_POINTS)),
                            ("max", lambda v: _int_in(v, 0, MAX_POINTS)),
                            ("per_hour", lambda v: _num_in(v, 0, MAX_PER_HOUR)),
                            ("min_hours", lambda v: _num_in(v, 0, MAX_MIN_HOURS))):
            parsed = parser(part.get(key))
            if parsed is not None:
                cfg["participation"][key] = parsed
    return cfg


def normalize_points_input(body) -> dict:
    """Validate a PUT payload into the stored shape. Accepts a partial object
    (absent keys keep their current values when the caller merges); raises
    :class:`PointsConfigError` with a readable reason on anything invalid —
    an admin minting clan currency deserves to know exactly what was wrong."""
    if not isinstance(body, dict):
        raise PointsConfigError("The clan points config must be an object.")
    out: dict = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise PointsConfigError("'enabled' must be true or false.")
        out["enabled"] = body["enabled"]
    if "award_mode" in body:
        if body["award_mode"] not in AWARD_MODES:
            raise PointsConfigError("'award_mode' must be 'auto' or 'review'.")
        out["award_mode"] = body["award_mode"]
    if "placement" in body:
        placement = _clean_placement(body["placement"])
        if placement is None:
            raise PointsConfigError(
                f"'placement' must be a list of at most {MAX_PLACES} whole numbers "
                f"between 0 and {MAX_POINTS:,} (1st place first).")
        out["placement"] = placement
    if "placement_active_only" in body:
        if not isinstance(body["placement_active_only"], bool):
            raise PointsConfigError("'placement_active_only' must be true or false.")
        out["placement_active_only"] = body["placement_active_only"]
    if "participation" in body:
        part = body["participation"]
        if not isinstance(part, dict):
            raise PointsConfigError("'participation' must be an object.")
        norm_part: dict = {}
        for key, lo, hi, whole in (("flat", 0, MAX_POINTS, True),
                                   ("max", 0, MAX_POINTS, True),
                                   ("per_hour", 0, MAX_PER_HOUR, False),
                                   ("min_hours", 0, MAX_MIN_HOURS, False)):
            if key not in part:
                continue
            parsed = (_int_in(part[key], lo, hi) if whole
                      else _num_in(part[key], lo, hi))
            if parsed is None:
                kind = "a whole number" if whole else "a number"
                raise PointsConfigError(
                    f"participation.{key} must be {kind} between {lo} and {hi:,}.")
            norm_part[key] = parsed
        out["participation"] = norm_part
    return out


def merge_points_config(current_raw, patch: dict) -> dict:
    """``patch`` (already normalized) over the effective current config;
    ``participation`` merges key-wise."""
    merged = effective_points_config(current_raw)
    for key, value in patch.items():
        if key == "participation":
            merged["participation"].update(value)
        else:
            merged[key] = value
    return effective_points_config(merged)


def pays_anything(config: dict) -> bool:
    """Whether an enabled config could pay a single point."""
    part = config.get("participation") or {}
    return bool(config.get("enabled")) and (
        any(int(a or 0) > 0 for a in config.get("placement") or [])
        or float(part.get("per_hour") or 0) > 0
        or int(part.get("flat") or 0) > 0
    )


def needs_ehe_pricing(config: dict, ehe_supported: bool = True) -> bool:
    """Whether computing this config's participation reads EHE hours — the
    one input that can be silently wrong (a cold WOM rate cache)."""
    if not ehe_supported:
        return False
    part = config.get("participation") or {}
    if float(part.get("per_hour") or 0) > 0:
        return True
    # No hourly rate: hours still matter when a floor gates the flat amount.
    return float(part.get("min_hours") or 0) > 0 and int(part.get("flat") or 0) > 0


# ══════════════════════════════════════════════════════════════════════════════
# Scoring (pure)
# ══════════════════════════════════════════════════════════════════════════════

def competition_places(ordered: Iterable) -> list:
    """Standard competition ranking over a best-first ``[(entity_id, key)]``:
    equal keys share a place and the next distinct key takes its 1-based
    position ("1, 1, 3"). Returns ``[(entity_id, place)]`` in the same order.
    A tie decided only by an id or a name is still a tie — the orderings this
    feeds all break ties that way, and that is not a result anyone earned."""
    out = []
    prev_key = object()
    prev_place = 0
    for pos, (entity_id, key) in enumerate(ordered, start=1):
        place = prev_place if key == prev_key else pos
        out.append((entity_id, place))
        prev_key, prev_place = key, place
    return out


def placement_amount(config: dict, place: Optional[int]) -> int:
    """Points one member earns for ``place`` (1-based); 0 outside the paid
    places or when placement isn't configured."""
    if not place or place < 1:
        return 0
    amounts = config.get("placement") or []
    return int(amounts[place - 1]) if place <= len(amounts) else 0


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5)) if value > 0 else 0


def participation_amount(config: dict, hours: float, took_part: bool,
                         *, ehe_supported: bool = True) -> int:
    """One player's participation points.

    Nothing for someone who didn't take part. With EHE tracked, ``min_hours``
    is a floor (below it nothing is paid, not even the flat amount) and
    ``per_hour`` prices the hours, rounded half-up; without EHE (SOTW/BOTW)
    only the flat amount applies. ``max`` caps the total."""
    part = config.get("participation") or {}
    if not took_part:
        return 0
    hours = round(max(float(hours or 0), 0.0), 2)
    amount = int(part.get("flat") or 0)
    if ehe_supported:
        min_hours = float(part.get("min_hours") or 0)
        if min_hours > 0 and hours < min_hours:
            return 0
        amount += _round_half_up(hours * float(part.get("per_hour") or 0))
    cap = int(part.get("max") or 0)
    if cap > 0:
        amount = min(amount, cap)
    return max(amount, 0)


def plan_awards(config: dict, *, members: list, placements: dict,
                hours: dict, took_part: set, clan_member_ids: set,
                competition: bool = False, ehe_supported: bool = True) -> dict:
    """The desired payout for one clan — pure, so preview and award agree by
    construction.

    - ``members``: the roster in scope, ``[{player_id, player_name, team_id}]``.
    - ``placements``: ``{entity_id: place}`` for placed entities (team ids, or
      player ids on a competition). Only scoring entities appear.
    - ``hours``: ``{player_id: EHE hours}``; ``took_part``: player ids with any
      activity; ``clan_member_ids``: roster players who belong to the clan.

    Returns ``{"rows": [...], "skipped": [...]}``. Each row is ``{player_id,
    player_name, team_id, place, placement, participation, hours, total}``;
    rows paying nothing are dropped, and roster players who can't be paid for
    a reason worth showing land in ``skipped`` (``not_member`` — the clan pays
    only its own members; ``inactive`` — on a placed team but took no part)."""
    rows, skipped = [], []
    active_only = bool(config.get("placement_active_only", True))
    seen: set = set()
    for m in members:
        pid = m.get("player_id")
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        entity = pid if competition else m.get("team_id")
        place = placements.get(entity)
        active = pid in took_part
        placement = placement_amount(config, place)
        if placement and active_only and not active:
            placement = 0
            if pid in clan_member_ids:
                skipped.append({"player_id": pid, "player_name": m.get("player_name"),
                                "team_id": m.get("team_id"), "reason": "inactive"})
        player_hours = round(float(hours.get(pid) or 0.0), 2)
        participation = participation_amount(
            config, player_hours, active, ehe_supported=ehe_supported)
        if not (placement or participation):
            continue
        if pid not in clan_member_ids:
            skipped.append({"player_id": pid, "player_name": m.get("player_name"),
                            "team_id": m.get("team_id"), "reason": "not_member"})
            continue
        rows.append({
            "player_id": pid,
            "player_name": m.get("player_name"),
            "team_id": m.get("team_id"),
            "place": place if placement else None,
            "placement": placement,
            "participation": participation,
            "hours": player_hours if ehe_supported else None,
            "total": placement + participation,
        })
    rows.sort(key=lambda r: (-r["total"], r["place"] or 10 ** 6,
                             str(r["player_name"] or "").lower(), r["player_id"]))
    return {"rows": rows, "skipped": skipped}


def desired_ledger(plan: dict) -> dict:
    """``{(player_id, kind): row}`` for every non-zero award in a plan."""
    out = {}
    for row in plan.get("rows") or []:
        if row.get("placement"):
            out[(row["player_id"], "placement")] = {
                "amount": int(row["placement"]), "team_id": row.get("team_id"),
                "place": row.get("place"), "hours": None,
            }
        if row.get("participation"):
            out[(row["player_id"], "participation")] = {
                "amount": int(row["participation"]), "team_id": row.get("team_id"),
                "place": None, "hours": row.get("hours"),
            }
    return out


def diff_ledger(desired: dict, existing: dict) -> dict:
    """What a sync must do. ``existing`` is ``{(player_id, kind): {"amount",
    "points_row": bool}}`` — ``points_row`` False when the clan's points reset
    deleted the ``player_points`` row out from under the ledger.

    Returns ``{"insert": [key], "update": [key], "delete": [key],
    "unchanged": [key], "reset": [key]}``. A reset row is never re-inserted or
    edited (the clan wiped it); it is only forgotten when no longer desired."""
    out = {"insert": [], "update": [], "delete": [], "unchanged": [], "reset": []}
    for key, want in desired.items():
        have = existing.get(key)
        if have is None:
            out["insert"].append(key)
        elif not have.get("points_row", True):
            out["reset"].append(key)
        elif int(have.get("amount") or 0) != int(want["amount"]):
            out["update"].append(key)
        else:
            out["unchanged"].append(key)
    for key in existing:
        if key not in desired:
            out["delete"].append(key)
    for bucket in out.values():
        bucket.sort(key=lambda k: (k[0], k[1]))
    return out


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def award_reason(event_name: str, kind: str, *, place: Optional[int] = None,
                 team_name: Optional[str] = None, hours: Optional[float] = None) -> str:
    """The ``player_points.reason`` the clan sees in its points history."""
    name = (event_name or "Event").strip()
    if kind == "placement":
        tail = f"{ordinal(place)} place" if place else "placement"
        if team_name:
            tail += f" ({team_name})"
    else:
        tail = "participation"
        if hours:
            tail += f" ({hours:g}h EHE)"
    return f"Event: {name} — {tail}"[:_REASON_MAX_LEN]


_MEDALS = ("\U0001F947", "\U0001F948", "\U0001F949")  # 🥇 🥈 🥉


def clan_points_line(results: list) -> Optional[str]:
    """The ``{clan_points_line}`` for the ended announcement — one line per
    clan that paid anything, or None (the layout's token-drop rule removes the
    block). ``results`` items: ``{group_name, placements: [{place, label,
    amount, players}], participation_players, participation_points}``."""
    lines = []
    paying = [r for r in results or [] if r.get("total_points")]
    for r in paying:
        parts = []
        for p in r.get("placements") or []:
            # A place nobody was paid for (every member inactive or outside
            # the clan) isn't a payout worth announcing.
            if not p.get("amount") or not p.get("players"):
                continue
            place = int(p.get("place") or 0)
            badge = _MEDALS[place - 1] if 1 <= place <= 3 else ordinal(place)
            each = " each" if int(p.get("players") or 0) > 1 or p.get("team") else ""
            parts.append(f"{badge} **{p.get('label')}** `+{int(p['amount']):,}`{each}")
        if r.get("participation_points"):
            n = int(r.get("participation_players") or 0)
            parts.append(f"participation `+{int(r['participation_points']):,}` "
                         f"across {n} member{'s' if n != 1 else ''}")
        if not parts:
            continue
        prefix = f"**{r.get('group_name')}** — " if len(paying) > 1 and r.get("group_name") else ""
        lines.append(f"\U0001FA99 {prefix}Clan points: " + " · ".join(parts))
    return "\n".join(lines) or None


# ══════════════════════════════════════════════════════════════════════════════
# DB layer (lazy imports — the event worker, webapi and the core bot call this)
# ══════════════════════════════════════════════════════════════════════════════

def scope_group_ids(session, event) -> list:
    """Clans that may configure a payout for ``event``: its own group on a
    standard event (none for a global event), every accepted participant on a
    clan-vs-clan one."""
    if (getattr(event, "mode", None) or "standard") == "clan_vs_clan":
        from db.models import EventGroup

        rows = (session.query(EventGroup.group_id)
                .filter(EventGroup.event_id == event.id,
                        EventGroup.status == "accepted")
                .all())
        return sorted({int(gid) for (gid,) in rows})
    return [int(event.group_id)] if event.group_id else []


def points_system_active(group_id) -> bool:
    """The clan's points system (``custom_points`` entitlement). Fails closed:
    minting currency on a guess is worse than a retry."""
    try:
        from db.entitlements import group_has_entitlement

        return bool(group_has_entitlement(int(group_id), "custom_points"))
    except Exception:
        return False


def load_config_row(session, event_id: int, group_id: int, *, for_update: bool = False):
    from db.models import EventPointConfig

    q = session.query(EventPointConfig).filter(
        EventPointConfig.event_id == event_id,
        EventPointConfig.group_id == group_id)
    if for_update:
        q = q.with_for_update()
    return q.first()


def _is_competition(event) -> bool:
    from db.models import COMPETITION_EVENT_KINDS

    return (getattr(event, "kind", None) or "standard") in COMPETITION_EVENT_KINDS


def _roster(session, event, group_id: int) -> list:
    """Roster rows in this clan's scope. Standard events: every team (the
    membership check narrows to the clan). Clan-vs-clan: the clan's own teams,
    plus any team bound to no clan."""
    from db.models import EventTeam, EventTeamMember, Player

    teams = session.query(EventTeam).filter(EventTeam.event_id == event.id).all()
    if (getattr(event, "mode", None) or "standard") == "clan_vs_clan":
        teams = [t for t in teams if t.group_id in (None, group_id)]
    team_ids = [t.id for t in teams]
    if not team_ids:
        return []
    rows = (session.query(EventTeamMember.player_id, EventTeamMember.team_id,
                          Player.player_name)
            .join(Player, Player.player_id == EventTeamMember.player_id)
            .filter(EventTeamMember.team_id.in_(team_ids))
            .all())
    return [{"player_id": int(pid), "team_id": int(tid),
             "player_name": name or f"Player {pid}"} for pid, tid, name in rows]


def _clan_member_ids(session, group_id: int, player_ids) -> set:
    from db.models import user_group_association

    pids = sorted({int(p) for p in player_ids if p is not None})
    if not pids:
        return set()
    rows = (session.query(user_group_association.c.player_id)
            .filter(user_group_association.c.group_id == group_id,
                    user_group_association.c.player_id.in_(pids))
            .distinct()
            .all())
    return {int(pid) for (pid,) in rows}


def _team_placements(session, event) -> tuple:
    """``(placements, labels)`` for a team event: ``{team_id: place}`` over
    teams that scored (ties share a place), and ``{team_id: name}``. Board
    games rank by the finish-line race exactly as the final standings do."""
    from db.models import EventTeam

    teams = session.query(EventTeam).filter(EventTeam.event_id == event.id).all()
    labels = {t.id: t.name for t in teams}
    if (getattr(event, "kind", None) or "standard") == "board_game":
        from db.models import EventBoardPosition
        from services.boardgame_engine import load_board_settings
        from services.event_lifecycle import _rank_board_teams

        tiebreak = (load_board_settings(session, event.id).get("win") or {}).get("tiebreak")
        if not isinstance(tiebreak, list) or not tiebreak:
            tiebreak = ["score"]
        positions = {
            p.team_id: p for p in session.query(EventBoardPosition)
            .filter(EventBoardPosition.event_id == event.id).all()
        }

        def _key(t):
            # The ranking's own terms minus the team-id tiebreak: two teams
            # equal on every one of these genuinely tied.
            p = positions.get(t.id)
            finished = p is not None and getattr(p, "status", None) == "finished"
            tile = int(getattr(p, "tile_idx", 0) or 0) if p is not None else 0
            metrics = {"score": float(t.score or 0), "coins": float(t.coins or 0)}
            return (finished, tile) + tuple(round(metrics[tok], 2)
                                            for tok in tiebreak if tok in metrics)

        ranked = [(t, _key(t)) for t in _rank_board_teams(teams, positions, tiebreak)]
        # Placed = went anywhere: finished, moved off the start, or scored.
        places = competition_places([
            (t.id, key) for t, key in ranked
            if key[0] or key[1] > 0 or float(t.score or 0) > 0
        ])
    else:
        ordered = sorted((t for t in teams if round(float(t.score or 0), 2) > 0),
                         key=lambda t: (-float(t.score or 0), t.id))
        places = competition_places(
            [(t.id, round(float(t.score or 0), 2)) for t in ordered])
    return dict(places), labels


def _competition_standings(session, event) -> tuple:
    """``(ranked_rows, ranking_mode)`` for SOTW/BOTW — the frozen result when
    the event has been finalized, the live fold otherwise."""
    from services.competition import CompetitionConfig
    from services.competition_setup import competition_row, competition_task

    task = competition_task(session, event.id)
    mode = CompetitionConfig(task.config).ranking_mode if task is not None else "gained"
    row = competition_row(session, event.id)
    if row is not None and row.final_standings:
        try:
            ranked = json.loads(row.final_standings)
            if isinstance(ranked, list):
                return ranked, mode
        except (TypeError, ValueError):
            pass
    from services.event_lifecycle import _competition_ranked_rows

    ranked, _config, _team = _competition_ranked_rows(session, event)
    return ranked or [], mode


def _competition_placements(ranked: list, mode: str) -> dict:
    """``{player_id: place}`` over players who gained or scored anything.
    Unregistered (WOM-only) rows keep their true rank — "you finished 2nd"
    is what the standings page says — but can't be paid."""
    def _value(r):
        return int((r.get("points") if mode == "points" else r.get("gained")) or 0)

    scored = [r for r in ranked if _value(r) > 0]
    ordered = [(r.get("player_id") if r.get("registered") else ("wom", i),
                (_value(r), int(r.get("bonus_points") or 0), int(r.get("gained") or 0)))
               for i, r in enumerate(scored)]
    return {pid: place for pid, place in competition_places(ordered)
            if isinstance(pid, int)}


def _ehe_hours(session, event_id: int, player_ids) -> tuple:
    """``({player_id: EHE hours}, rates_known)``, priced exactly like
    web_api/event_effort.py (WOM rate first, our derived rate as fallback)."""
    from db.models import EventEffort, NpcEhbRate, NpcList
    from services.event_effort import rows_to_summary

    try:
        from utils.wiseoldman import get_ehb_rates_sync

        rates = get_ehb_rates_sync() or {}
    except Exception:
        rates = {}
    derived = {}
    try:
        derived = {int(n): float(r) for n, r in
                   session.query(NpcEhbRate.npc_id, NpcEhbRate.rate_kph)
                   if r and float(r) > 0}
    except Exception:
        derived = {}
    pids = sorted({int(p) for p in player_ids if p is not None})
    if not pids:
        return {}, bool(rates)
    grouped: dict = {}
    for (pid, npc_id, npc_name, metric, kills, completions, rolls,
         last_at, frozen_at) in (
            session.query(EventEffort.player_id, EventEffort.npc_id, NpcList.npc_name,
                          EventEffort.boss_metric, EventEffort.kills,
                          EventEffort.completions, EventEffort.rolls,
                          EventEffort.last_at, EventEffort.frozen_at)
            .outerjoin(NpcList, NpcList.npc_id == EventEffort.npc_id)
            .filter(EventEffort.event_id == event_id,
                    EventEffort.player_id.in_(pids))
            .all()):
        grouped.setdefault(int(pid), []).append({
            "npc_id": npc_id, "npc_name": npc_name, "boss_metric": metric,
            "kills": kills, "completions": completions, "rolls": rolls,
            "last_at": last_at, "frozen_at": frozen_at,
        })
    hours = {pid: round(float(rows_to_summary(rows, rates, derived)
                              .get("ehb_hours") or 0.0), 2)
             for pid, rows in grouped.items()}
    return hours, bool(rates)


def _took_part(session, event_id: int, player_ids) -> set:
    """Players with any credited ledger row or any tracked effort — the bar
    for "took part" (competition gains are ledger rows too)."""
    from db.models import EventCompletion, EventEffort

    pids = {int(p) for p in player_ids if p is not None}
    if not pids:
        return set()
    active = {
        int(pid) for (pid,) in
        session.query(EventCompletion.player_id)
        .filter(EventCompletion.event_id == event_id,
                EventCompletion.status.in_(("auto", "confirmed", "manual")),
                EventCompletion.player_id.isnot(None))
        .distinct()
        .all()
    }
    active |= {
        int(pid) for (pid,) in
        session.query(EventEffort.player_id)
        .filter(EventEffort.event_id == event_id, EventEffort.kills > 0)
        .distinct()
        .all()
    }
    return active & pids


def compute_plan(session, event, group_id: int, config: dict) -> dict:
    """Everything the preview and the award need for one clan: the plan rows,
    the placed entities with their labels, and whether EHE could be priced.
    Read-only."""
    competition = _is_competition(event)
    ehe_supported = not competition
    members = _roster(session, event, group_id)
    pids = [m["player_id"] for m in members]
    clan_ids = _clan_member_ids(session, group_id, pids)
    took_part = _took_part(session, event.id, pids)

    labels: dict = {}
    if competition:
        ranked, mode = _competition_standings(session, event)
        placements = _competition_placements(ranked, mode)
        took_part |= {int(r["player_id"]) for r in ranked
                      if r.get("registered") and r.get("player_id") is not None
                      and (int(r.get("gained") or 0) > 0 or int(r.get("points") or 0) > 0)}
        took_part &= set(pids)
        names = {m["player_id"]: m["player_name"] for m in members}
        labels = {pid: names.get(pid) or f"Player {pid}" for pid in placements}
    else:
        placements, labels = _team_placements(session, event)

    hours, rates_known = ({}, True)
    if ehe_supported:
        hours, rates_known = _ehe_hours(session, event.id, pids)

    plan = plan_awards(config, members=members, placements=placements, hours=hours,
                       took_part=took_part, clan_member_ids=clan_ids,
                       competition=competition, ehe_supported=ehe_supported)
    counts: dict = {}
    for row in plan["rows"]:
        if row["placement"]:
            entity = row["player_id"] if competition else row["team_id"]
            counts[entity] = counts.get(entity, 0) + 1
    placed = []
    for entity, place in sorted(placements.items(), key=lambda kv: (kv[1], str(labels.get(kv[0])))):
        amount = placement_amount(config, place)
        if not amount:
            continue
        placed.append({
            "place": place,
            "team_id": None if competition else entity,
            "player_id": entity if competition else None,
            "label": labels.get(entity) or "",
            "amount": amount,
            "players": counts.get(entity, 0),
            "team": not competition,
        })
    return {
        **plan,
        "placements": placed,
        "competition": competition,
        "ehe_supported": ehe_supported,
        "rates_known": rates_known,
        "roster_size": len({m["player_id"] for m in members}),
    }


def summarize(plan: dict) -> dict:
    rows = plan.get("rows") or []
    part_rows = [r for r in rows if r.get("participation")]
    return {
        "players": len(rows),
        "total_points": sum(int(r["total"]) for r in rows),
        "placement_points": sum(int(r["placement"]) for r in rows),
        "participation_points": sum(int(r["participation"]) for r in part_rows),
        "participation_players": len(part_rows),
    }


def _existing_ledger(session, event_id: int, group_id: int) -> tuple:
    """``(award_rows_by_key, points_rows_by_id)`` for one clan's awards."""
    from db.models import EventPointAward, PlayerPoints

    awards = (session.query(EventPointAward)
              .filter(EventPointAward.event_id == event_id,
                      EventPointAward.group_id == group_id)
              .all())
    pp_ids = [a.player_points_id for a in awards if a.player_points_id is not None]
    points = {}
    if pp_ids:
        points = {p.id: p for p in session.query(PlayerPoints)
                  .filter(PlayerPoints.id.in_(pp_ids)).all()}
    return {(a.player_id, a.kind): a for a in awards}, points


def _existing_state(awards: dict, points: dict) -> dict:
    return {
        key: {"amount": a.amount,
              "points_row": a.player_points_id is not None and a.player_points_id in points}
        for key, a in awards.items()
    }


def pending_changes(session, event, group_id: int, config: dict, plan: dict) -> dict:
    """How far an awarded payout has drifted from what the event would pay
    now (a post-end revoke moved the standings, or the config changed):
    ``{"insert", "update", "remove", "reset"}`` counts. All zero = in sync."""
    desired = desired_ledger(plan) if config.get("enabled") else {}
    awards, points = _existing_ledger(session, event.id, group_id)
    ops = diff_ledger(desired, _existing_state(awards, points))
    return {"insert": len(ops["insert"]), "update": len(ops["update"]),
            "remove": len(ops["delete"]), "reset": len(ops["reset"])}


def _audit(session, *, event, group_id: int, actor_user_id, action: str, after: dict) -> None:
    from db.models import AuditLog

    session.add(AuditLog(
        actor_user_id=actor_user_id,
        group_id=group_id,
        event_id=event.id,
        action=action,
        target=f"web_event_point_configs.{event.id}:{group_id}",
        after=json.dumps(after, default=str),
    ))


def apply_award(session, event, cfg_row, *, actor_user_id=None,
                now: Optional[datetime] = None, plan: Optional[dict] = None) -> dict:
    """Award (or re-sync) one clan's payout: diff the desired ledger against
    what the event already paid and apply only the difference. Caller owns
    the commit and has already checked the event is over, the clan's points
    system is live, and (for EHE-priced configs) that rates are known.

    Returns the summary the routes/announcement render."""
    from db.models import EventPointAward, PlayerPoints

    now = now or datetime.now()
    config = effective_points_config(cfg_row.config)
    plan = plan or compute_plan(session, event, cfg_row.group_id, config)
    desired = desired_ledger(plan) if config.get("enabled") else {}
    awards, points = _existing_ledger(session, event.id, cfg_row.group_id)
    ops = diff_ledger(desired, _existing_state(awards, points))
    team_names = {p["team_id"]: p["label"] for p in plan.get("placements") or []
                  if p.get("team_id") is not None}

    def _reason(key, want):
        kind = key[1]
        return award_reason(event.name, kind, place=want.get("place"),
                            team_name=team_names.get(want.get("team_id")),
                            hours=want.get("hours"))

    for key in ops["insert"]:
        pid, kind = key
        want = desired[key]
        pp = PlayerPoints(
            player_id=pid, group_id=cfg_row.group_id, amount=int(want["amount"]),
            reason=_reason(key, want), entry_type=ENTRY_TYPE_FOR_KIND[kind],
            entry_id=event.id, date_added=now,
        )
        session.add(pp)
        session.flush()
        session.add(EventPointAward(
            event_id=event.id, group_id=cfg_row.group_id, player_id=pid,
            kind=kind, amount=int(want["amount"]), team_id=want["team_id"],
            place=want["place"], hours=want["hours"], player_points_id=pp.id,
        ))
    for key in ops["update"]:
        want = desired[key]
        award = awards[key]
        pp = points[award.player_points_id]
        pp.amount = int(want["amount"])
        pp.reason = _reason(key, want)
        award.amount = int(want["amount"])
        award.team_id, award.place, award.hours = want["team_id"], want["place"], want["hours"]
    for key in ops["unchanged"]:
        # Same amount; refresh the display snapshot (a re-sync may have moved
        # a tied team to a shared place without changing what it pays).
        want, award = desired[key], awards[key]
        award.team_id, award.place, award.hours = want["team_id"], want["place"], want["hours"]
    for key in ops["delete"]:
        award = awards[key]
        pp = points.get(award.player_points_id) if award.player_points_id else None
        if pp is not None:
            session.delete(pp)
        session.delete(award)

    first_award = cfg_row.status != "awarded"
    cfg_row.status = "awarded"
    cfg_row.awarded_at = now
    if actor_user_id is not None or first_award:
        cfg_row.awarded_by_user_id = actor_user_id
    cfg_row.last_error = None
    session.flush()

    summary = {
        **summarize(plan if config.get("enabled") else {"rows": []}),
        "status": "awarded",
        "inserted": len(ops["insert"]),
        "updated": len(ops["update"]),
        "removed": len(ops["delete"]),
        "unchanged": len(ops["unchanged"]),
        "reset_skipped": len(ops["reset"]),
        "placements": plan.get("placements") or [],
    }
    changed = ops["insert"] or ops["update"] or ops["delete"]
    if first_award or changed:
        # A re-sync that moved nothing (a double click, a no-op check)
        # leaves no audit row.
        _audit(session, event=event, group_id=cfg_row.group_id, actor_user_id=actor_user_id,
               action="event.points.award" if first_award else "event.points.sync",
               after={k: v for k, v in summary.items() if k != "placements"})
    return summary


def revoke_awards(session, event, cfg_row, *, actor_user_id=None) -> dict:
    """Remove every award the event paid this clan (and the ``player_points``
    rows behind them). Caller owns the commit."""
    awards, points = _existing_ledger(session, event.id, cfg_row.group_id)
    removed_points = 0
    for award in awards.values():
        pp = points.get(award.player_points_id) if award.player_points_id else None
        if pp is not None:
            removed_points += int(pp.amount or 0)
            session.delete(pp)
        session.delete(award)
    cfg_row.status = "revoked"
    cfg_row.last_error = None
    session.flush()
    summary = {"status": "revoked", "removed": len(awards), "removed_points": removed_points}
    _audit(session, event=event, group_id=cfg_row.group_id, actor_user_id=actor_user_id,
           action="event.points.revoke", after=summary)
    return summary


def award_blocker(session, event, cfg_row, plan: Optional[dict] = None) -> Optional[str]:
    """Why this clan's payout can't be awarded right now (None = it can)."""
    if getattr(event, "status", None) != "past":
        return "The event hasn't ended yet — clan points are awarded once it's over."
    config = effective_points_config(cfg_row.config)
    if not config.get("enabled"):
        return "Clan points are switched off for this event."
    if not points_system_active(cfg_row.group_id):
        return ("This clan's points system isn't active — it needs the Clan Points "
                "feature on its subscription.")
    if plan is not None and needs_ehe_pricing(config, plan.get("ehe_supported", True)) \
            and not plan.get("rates_known", True):
        return ("EHE can't be priced right now (the WiseOldMan rate table is "
                "unavailable) — try again in a few minutes.")
    return None


def _group_name(session, group_id: int) -> Optional[str]:
    from db.models import Group

    row = session.query(Group.group_name).filter(Group.group_id == group_id).first()
    return row[0] if row else None


def _hidden_ids(session, player_ids) -> set:
    """Opted-out players among ``player_ids`` (announcement names only)."""
    from sqlalchemy import or_

    from db.models import Player, User

    pids = sorted({int(p) for p in player_ids if p is not None})
    if not pids:
        return set()
    try:
        return {int(pid) for (pid,) in
                session.query(Player.player_id)
                .outerjoin(User, User.user_id == Player.user_id)
                .filter(Player.player_id.in_(pids),
                        or_(Player.hidden.is_(True), User.hidden.is_(True)))
                .all()}
    except Exception:
        return set(pids)  # fail closed for names: mask everyone


def _announce_result(session, cfg_row, summary: dict) -> dict:
    placements = [dict(p) for p in summary.get("placements") or []]
    hidden = _hidden_ids(session, [p.get("player_id") for p in placements])
    for p in placements:
        if p.get("player_id") in hidden:
            p["label"] = "Hidden player"
    return {
        "group_id": cfg_row.group_id,
        "group_name": _group_name(session, cfg_row.group_id),
        "placements": placements,
        "participation_points": summary.get("participation_points", 0),
        "participation_players": summary.get("participation_players", 0),
        "total_points": summary.get("total_points", 0),
    }


def _missing_table(exc) -> bool:
    """A query against the web114a tables before the migration ran. Code can
    reach a box ahead of ``alembic upgrade`` (a restart before the migrate
    step); every event end must not then report a failed wrap-up to the
    admin channel over tables nobody can have configured yet."""
    text = str(getattr(exc, "orig", exc))
    return "1146" in text or "doesn't exist" in text or "no such table" in text


def award_auto(session, event, *, now: Optional[datetime] = None) -> list:
    """Run every ``auto`` payout that hasn't landed yet (end_event's wrap-up,
    and the sweep's retry of deferred ones). Commits per clan so one clan's
    failure never loses another's award. Returns announcement results for the
    clans that paid out."""
    from db.models import EventPointConfig

    now = now or datetime.now()
    try:
        rows = (session.query(EventPointConfig)
                .filter(EventPointConfig.event_id == event.id,
                        EventPointConfig.status.in_(("pending", "deferred")))
                .all())
    except Exception as exc:
        if not _missing_table(exc):
            raise
        session.rollback()
        return []
    allowed = set(scope_group_ids(session, event))
    results = []
    for row in rows:
        config = effective_points_config(row.config)
        if (row.group_id not in allowed or not config.get("enabled")
                or config.get("award_mode") != "auto" or not pays_anything(config)):
            continue
        cfg_row = load_config_row(session, event.id, row.group_id, for_update=True)
        if cfg_row is None or cfg_row.status not in ("pending", "deferred"):
            session.commit()
            continue
        plan = compute_plan(session, event, cfg_row.group_id, config)
        blocker = award_blocker(session, event, cfg_row, plan)
        if blocker:
            # EHE unpriceable => retry from the sweep; anything else (points
            # system off) waits for an admin, who sees the reason.
            unpriceable = needs_ehe_pricing(config, plan.get("ehe_supported", True)) \
                and not plan.get("rates_known", True)
            cfg_row.status = "deferred" if unpriceable else "pending"
            cfg_row.last_error = blocker[:255]
            session.commit()
            continue
        summary = apply_award(session, event, cfg_row, now=now, plan=plan)
        session.commit()
        results.append(_announce_result(session, cfg_row, summary))
        publish_update(event.id, cfg_row.group_id, summary)
    return results


def retry_deferred(session, now: Optional[datetime] = None) -> list:
    """Lifecycle-sweep pass: retry auto payouts deferred at end (cold EHE
    pricing) for events that ended within :data:`DEFERRED_RETRY_DAYS`. One
    tiny indexed query when nothing is deferred."""
    from db.models import Event, EventPointConfig

    now = now or datetime.now()
    try:
        event_ids = sorted({
            eid for (eid,) in
            session.query(EventPointConfig.event_id)
            .join(Event, Event.id == EventPointConfig.event_id)
            .filter(EventPointConfig.status == "deferred",
                    Event.status == "past",
                    Event.ended_at >= now - timedelta(days=DEFERRED_RETRY_DAYS))
            .all()
        })
    except Exception as exc:
        if not _missing_table(exc):
            raise
        session.rollback()
        return []
    done = []
    for eid in event_ids:
        event = session.query(Event).filter(Event.id == eid).first()
        if event is None:
            continue
        if award_auto(session, event, now=now):
            done.append(eid)
    return done


def publish_update(event_id: int, group_id: int, summary: dict) -> None:
    """``rt:event:{id}`` frame so open event pages refresh their clan-points
    card. Best-effort; the web treats unknown frame kinds as a no-op."""
    try:
        from services.realtime import publish_event_update

        publish_event_update(event_id, {
            "kind": "clan_points", "event_id": event_id, "group_id": group_id,
            "status": summary.get("status"),
            "total_points": summary.get("total_points", 0),
        })
    except Exception:
        pass
