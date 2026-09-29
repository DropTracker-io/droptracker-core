"""Event-wide lootboards: every participant's loot over the event window, plus
each player's KC and EHE (Efficient Hours towards Event).

The per-team boards (``lootboard/team_boards.py``) answer "what has my team
banked?". This answers the event-wide version, and adds the effort side the
normal lootboard has no room for: the regular board is drawn exactly as a team
board is (items grid, top looters, recent drops, over the event's own window),
then :mod:`lootboard.event_board_panel` splices a player table (# / Player /
Team / KC / EHE / Loot) in above the footer, built from the theme's own table
art.

Output path (public, under ``/img``)::

    static/assets/img/clans/{group_id}/events/{event_id}/lootboard.png

The core bot posts it beneath the event's live standings board
(``services/event_lootboard_post.py``), gated per event by
``message_config.leaderboard.lootboard`` (default on).

Gates, same doctrine as the team boards:

* ``EVENT_LOOTBOARDS`` env flag. Unset, it follows ``EVENT_TEAM_LOOTBOARDS``
  (one switch for "event lootboards"); set it to turn this board alone on/off.
* Private events are never rendered — the path is public and enumerable.
* ``effort_visibility = 'admins'`` hides KC/EHE from the public board (the
  same rule every public event read follows); the loot half still renders.
* SOTW/BOTW record no EHE (the race metric IS the effort), so those boards
  carry loot only.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from lootboard.team_boards import (
    FEATURE_FLAG_ENV as TEAM_FLAG_ENV,
    GRANULARITY_DAILY,
    IMG_ROOT,
    REFRESH_SECONDS,
    board_age_seconds,
    board_partitions,
    event_is_public,
    event_windows,
    resolve_team_player_ids,
)

FEATURE_FLAG_ENV = "EVENT_LOOTBOARDS"
EVENT_BOARDS_PER_RUN = 10
# Ended events get one last render (the final numbers) if their board predates
# the end. Past this window an ended event is left alone.
FINAL_RENDER_WINDOW = timedelta(hours=12)

# Fallback team colours (the site palette's first few) when a team has none.
_FALLBACK_COLOURS = [
    (230, 80, 60), (80, 160, 255), (120, 220, 90), (240, 200, 60),
    (190, 110, 240), (60, 210, 200), (250, 140, 60), (240, 110, 180),
]

EFFORT_NOTE = "KC and EHE count the bosses this event tracks. EHE = Efficient Hours towards Event."


def feature_enabled() -> bool:
    raw = os.getenv(FEATURE_FLAG_ENV)
    if raw is None or not str(raw).strip():
        raw = os.getenv(TEAM_FLAG_ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def board_group_id(event, teams=()) -> int:
    gid = getattr(event, "group_id", None)
    if gid:
        return int(gid)
    for team in teams:
        if getattr(team, "group_id", None):
            return int(team.group_id)
    return 0


def event_board_dir(group_id: int, event_id: int) -> str:
    return f"{IMG_ROOT}/clans/{int(group_id)}/events/{int(event_id)}"


def event_board_path(group_id: int, event_id: int) -> str:
    return f"{event_board_dir(group_id, event_id)}/lootboard.png"


def _hex_rgb(value) -> Optional[Tuple[int, int, int]]:
    try:
        v = str(value or "").strip().lstrip("#")
        if len(v) != 6:
            return None
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return None


def wants_lootboard(session, event) -> bool:
    """Whether any of the event's live-board channels asks for the lootboard."""
    from services.event_board import _board_rows

    try:
        return any(cfg["leaderboard"].get("live", True)
                   and cfg["leaderboard"].get("lootboard", True)
                   and row.channel_id
                   for row, cfg in _board_rows(session, event))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Effort (KC + EHE)
# --------------------------------------------------------------------------- #

def effort_by_player(session, event_id: int, player_ids) -> Tuple[Dict[int, dict], bool]:
    """``({player_id: {"kills", "hours", "estimated"}}, rates_known)`` priced
    exactly like the event pages (services/event_effort.rows_to_summary)."""
    from db.models import EventEffort, NpcEhbRate, NpcList
    from services.event_effort import rows_to_summary

    pids = sorted({int(p) for p in player_ids if p is not None})
    if not pids:
        return {}, True
    try:
        from utils.wiseoldman import get_ehb_rates_sync

        rates = get_ehb_rates_sync() or {}
    except Exception:
        rates = {}
    try:
        derived = {int(n): float(r) for n, r in
                   session.query(NpcEhbRate.npc_id, NpcEhbRate.rate_kph)
                   if r and float(r) > 0}
    except Exception:
        derived = {}
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
    out = {}
    for pid, rows in grouped.items():
        summary = rows_to_summary(rows, rates, derived)
        out[pid] = {
            "kills": int(summary.get("kills") or 0),
            "hours": round(float(summary.get("ehb_hours") or 0.0), 1),
            "estimated": float(summary.get("ehb_estimated_hours") or 0.0) > 0,
        }
    return out, bool(rates)


# --------------------------------------------------------------------------- #
# Rows (pure)
# --------------------------------------------------------------------------- #

def build_rows(player_ids, names: dict, team_of: dict, teams: dict,
               loot: dict, effort: dict, *, show_effort: bool,
               rates_known: bool = True) -> list:
    """Shape and rank the panel rows.

    ``team_of`` is ``{player_id: team_id}``, ``teams`` ``{team_id: (name,
    rgb)}``, ``loot`` ``{player_id: gp}``, ``effort`` ``{player_id: {"kills",
    "hours", "estimated"}}``. Ranked by EHE (then loot) when effort shows,
    by loot otherwise; name breaks ties so the order is stable."""
    from lootboard.event_board_panel import PanelRow

    rows = []
    for pid in player_ids:
        tid = team_of.get(pid)
        team_name, colour = teams.get(tid, (None, None))
        e = effort.get(pid) or {}
        hours = e.get("hours", 0.0) if rates_known else None
        rows.append(PanelRow(
            rank=0,
            name=names.get(pid) or f"Player {pid}",
            team=team_name,
            team_color=colour,
            kc=int(e.get("kills") or 0) if show_effort else None,
            ehe=hours if show_effort else None,
            ehe_estimated=bool(e.get("estimated")) if show_effort else False,
            loot=int(loot.get(pid) or 0),
        ))
    if show_effort:
        rows.sort(key=lambda r: (-(r.ehe or 0.0), -r.loot, -(r.kc or 0), r.name.lower()))
    else:
        rows.sort(key=lambda r: (-r.loot, r.name.lower()))
    for i, r in enumerate(rows, start=1):
        r.rank = i
    return rows


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #

async def render_event_board(session, event, *, force: bool = False,
                             now: Optional[datetime] = None) -> Optional[str]:
    """Generate one event's lootboard. Returns the written path, or None when
    nothing was written (feature off, private event, throttled, no roster,
    event not yet running)."""
    if not feature_enabled() or not event_is_public(event):
        return None

    from db.models import COMPETITION_EVENT_KINDS, EventTeam, Player
    from lootboard.event_board_panel import PanelSpec, compose
    from lootboard.flexible_generator import (
        BoardFilter, FlexibleBoardGenerator, TimeGranularity,
    )
    from lootboard.generator import (
        _ensure_public_dir, _save_public_image, config_int, config_truthy,
        draw_drops_on_image, draw_leaderboard, draw_recent_drops,
    )
    from lootboard.team_boards import _background, _draw_team_header, _group_config
    from utils.dynamic_handling import get_dynamic_color, get_value_color
    from utils.format import format_number

    teams = (session.query(EventTeam).filter(EventTeam.event_id == event.id)
             .order_by(EventTeam.id.asc()).all())
    group_id = board_group_id(event, teams)
    path = event_board_path(group_id, event.id)
    if not force and not _due(event, path):
        return None

    windows = [(s, e) for s, e in event_windows(session, event, now)
               if s and e and e >= s]
    granularity_name, partitions = board_partitions(windows, now=now)
    if not partitions:
        return None

    team_of: dict = {}
    team_info: dict = {}
    for i, team in enumerate(teams):
        colour = _hex_rgb(getattr(team, "color", None)) or _FALLBACK_COLOURS[i % len(_FALLBACK_COLOURS)]
        team_info[team.id] = ((team.name or f"Team {team.id}").strip(), colour)
        for pid in resolve_team_player_ids(session, team):
            team_of.setdefault(pid, team.id)
    player_ids = sorted(team_of)
    if not player_ids:
        return None

    config = _group_config(session, group_id)
    use_dynamic_colors = config_truthy(
        config.get('use_dynamic_colors', config.get('use_dynamic_lootboard_colors')),
        default=True,
    )
    use_gp_colors = config_truthy(config.get('use_gp_colors'), default=True)
    minimum_value = config_int(config.get('minimum_value_to_notify'), 2500000)

    granularity = (TimeGranularity.DAILY if granularity_name == GRANULARITY_DAILY
                   else TimeGranularity.MONTHLY)
    board_filter = BoardFilter(
        start_time=min(s for s, _ in windows),
        end_time=max(e for _, e in windows),
        time_granularity=granularity,
    )
    board_data = await FlexibleBoardGenerator()._aggregate_player_data(
        player_ids, partitions, granularity, board_filter)

    bg_img, draw = _background(session, config)
    template = bg_img.copy()
    bg_img = await draw_drops_on_image(
        bg_img, draw, board_data.group_items, group_id,
        dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    bg_img = _draw_team_header(
        bg_img, draw, (event.name or "Event").strip(), board_data.total_loot,
        dynamic_colors=use_dynamic_colors)
    bg_img = await draw_recent_drops(
        bg_img, draw, board_data.recent_drops, min_value=minimum_value,
        dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    bg_img = await draw_leaderboard(
        bg_img, draw, board_data.player_totals,
        dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors,
        session_to_use=session)

    kind = getattr(event, "kind", None) or "standard"
    show_effort = (kind not in COMPETITION_EVENT_KINDS
                   and (getattr(event, "effort_visibility", None) or "public") != "admins")
    effort, rates_known = ({}, True)
    if show_effort:
        effort, rates_known = effort_by_player(session, event.id, player_ids)
        # An event that tracks no bosses (skilling, pure item hunts) would
        # show two columns of zeroes forever: leave them off until a kill lands.
        show_effort = bool(effort)

    names = {int(pid): nm for pid, nm in
             session.query(Player.player_id, Player.player_name)
             .filter(Player.player_id.in_(player_ids)).all()}
    loot = {int(pid): int(v or 0) for pid, v in (board_data.player_totals or {}).items()}
    rows = build_rows(player_ids, names, team_of, team_info, loot, effort,
                      show_effort=show_effort, rates_known=rates_known)

    from lootboard.event_board_panel import MAX_ROWS

    note = None
    if show_effort:
        note = EFFORT_NOTE if rates_known else "EHE is unavailable right now and will fill in shortly."
    text_color = get_dynamic_color(bg_img) if use_dynamic_colors else (255, 255, 0)
    spec = PanelSpec(
        title="Players This Event",
        rows=rows[:MAX_ROWS],
        more=max(0, len(rows) - MAX_ROWS),
        show_team=len(teams) > 1,
        show_effort=show_effort,
        note=note,
        text_color=text_color,
        use_gp_colors=use_gp_colors,
        teams=[team_info[t.id] for t in teams],
    )
    image = compose(bg_img, template, spec, format_number, get_value_color)

    _ensure_public_dir(event_board_dir(group_id, event.id))
    _save_public_image(image, path)
    return path


def _due(event, path: str, now: Optional[datetime] = None) -> bool:
    """Hourly while running; once more after the end if the board on disk
    predates it (so the final numbers are what stays posted)."""
    if (getattr(event, "status", None) or "") == "past":
        ended = getattr(event, "ended_at", None)
        if ended is None:
            return False
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            mtime = None
        return mtime is None or mtime < ended
    return board_age_seconds(path) >= REFRESH_SECONDS


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def collect_targets(session, *, event_id: Optional[int] = None,
                    force: bool = False, now: Optional[datetime] = None) -> list:
    """``[(event_id, age_seconds)]`` for public events whose board is due and
    whose Discord config wants it, most stale first: active events, plus
    events that ended within :data:`FINAL_RENDER_WINDOW`."""
    from db.models import Event, EventTeam

    now = now or datetime.now()
    if event_id is not None:
        events = session.query(Event).filter(Event.id == int(event_id)).all()
    else:
        cutoff = now - FINAL_RENDER_WINDOW
        events = (session.query(Event)
                  .filter((Event.status == "active")
                          | ((Event.status == "past") & (Event.ended_at >= cutoff)))
                  .order_by(Event.id.asc()).all())
    out = []
    for event in events:
        if not event_is_public(event):
            continue
        if event_id is None and not wants_lootboard(session, event):
            continue
        teams = session.query(EventTeam).filter(EventTeam.event_id == event.id).all()
        path = event_board_path(board_group_id(event, teams), event.id)
        if not force and not _due(event, path, now):
            continue
        out.append((event.id, board_age_seconds(path)))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


async def sweep_event_boards(session_factory=None, *, event_id: Optional[int] = None,
                             force: bool = False,
                             limit: int = EVENT_BOARDS_PER_RUN) -> List[str]:
    """One driver pass (runs inside ``droptracker-lootboards``)."""
    if not feature_enabled():
        return []
    if session_factory is None:
        from db.models import Session as session_factory  # noqa: N813

    written: List[str] = []
    session = session_factory()
    try:
        targets = collect_targets(session, event_id=event_id, force=force)
    except Exception as e:
        print(f"[event-lootboard] target collection failed: {e}")
        targets = []
    finally:
        session.close()

    for target_id, _age in targets[:max(0, int(limit))]:
        session = session_factory()
        try:
            from db.models import Event

            event = session.query(Event).filter(Event.id == target_id).first()
            if event is None:
                continue
            path = await render_event_board(session, event, force=force)
            if path:
                written.append(path)
                print(f"[event-lootboard] event {target_id} -> {path}")
        except Exception as e:
            print(f"[event-lootboard] event {target_id} failed: {e}")
        finally:
            session.close()
    return written
