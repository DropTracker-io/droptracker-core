"""Per-team event lootboards (dev-tracker t63).

A normal lootboard is keyed by ``group_id``: the roster comes from the group's
WOM membership and the image lands at ``clans/{gid}/lb/lootboard.png``. An
event team is a different shape — an explicit roster over a bounded window —
but the aggregation underneath is already player-id driven, so the only thing
that really changes is *where the player ids come from* and *which partitions
are summed*.

That is exactly what :class:`lootboard.flexible_generator.FlexibleBoardGenerator`
already does, so this module reuses its aggregation
(``_aggregate_player_data``) and the shared draw helpers in
:mod:`lootboard.generator` rather than duplicating
``generate_server_board_temporary``. It deliberately does NOT call
``generate_flexible_board``: that entry point fetches the group's WOM roster
(useless here, and it would silently drop team members who aren't in the clan),
zadds ``gleaderboard:{partition}`` and rewrites the group's
``recent_drops_flexible.json`` — all group-shaped side effects a team board
must not have.

Output path::

    static/assets/img/clans/{group_id}/events/{event_id}/teams/{team_id}/lootboard.png

The ``clans/{group_id}/`` root is deliberate: ``generator._ensure_public_dir``
chmods every component up to (not including) ``clans/`` to 0777, which is what
lets both service accounts (``user`` for the bots/generators, ``debian`` for
the web API) write this tree. Anything outside ``clans/`` loses that.

Nothing here runs on its own. Every entry point is gated on the
``EVENT_TEAM_LOOTBOARDS`` env flag (off by default), so deploying this module
writes no images until it is switched on. ``force=True`` (what the CLI
``event_team_board_cli.py`` passes) means "ignore the hourly throttle" and
nothing else — it bypasses neither the flag nor the visibility gate.

That visibility gate is load-bearing: the output path is served
UNAUTHENTICATED under ``/img/`` and is trivially enumerable, while the image
carries the event name, the team name and every member's RSN + GP. Only
``visibility='public'`` events are ever rendered — see :func:`event_is_public`.
Nothing here posts to Discord — the generated PNG is served from
``/img/clans/{gid}/events/{eid}/teams/{tid}/lootboard.png``.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence, Tuple

IMG_ROOT = "/store/droptracker/disc/static/assets/img"

# Env switch. Off by default — see the module docstring.
FEATURE_FLAG_ENV = "EVENT_TEAM_LOOTBOARDS"

# A team board regenerates at most once an hour per target, same cadence the
# non-premium group boards settled on (see board_generator.NON_PREMIUM_*).
# State lives in the PNG's mtime: the driver runs in a fresh subprocess.
REFRESH_SECONDS = 3600
# Cap boards per sweep so one run can't grow unbounded with active events.
TEAM_BOARDS_PER_RUN = 25

# Daily Redis hashes are the only partition granularity that can be summed to
# an exact window; they carry a 90-day TTL (RedisLootTracker._DAILY_TTL).
DEFAULT_RETENTION_DAYS = 90

GRANULARITY_DAILY = "daily"
GRANULARITY_MONTHLY = "monthly"


def feature_enabled() -> bool:
    """True when per-team lootboards are switched on for this deployment."""
    return str(os.getenv(FEATURE_FLAG_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def event_is_public(event) -> bool:
    """Whether ``event``'s content may be shown to anyone at all.

    The inverse of ``web_api.routes.events._is_restricted``, mirrored rather
    than imported (that module is the Quart route stack, which this subprocess
    must not load). Same definition: an event is restricted iff an admin set
    ``visibility='private'``. Draft status is deliberately NOT restricting
    there either; here a draft produces no partitions anyway, so it renders
    nothing regardless.

    Anything a board renders lands on a public, enumerable ``/img`` URL, so a
    restricted event must never reach :func:`render_team_board`."""
    return (getattr(event, "visibility", None) or "public") != "private"


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def board_group_id(event, team) -> int:
    """The ``clans/{gid}`` bucket a team's board belongs in.

    The event's owning group first (every team of one event then shares a
    directory tree), falling back to the team's own clan for global events
    with no owner, and finally 0 for the ownerless case."""
    return int(getattr(event, "group_id", None)
               or getattr(team, "group_id", None)
               or 0)


def team_board_dir(group_id: int, event_id: int, team_id: int) -> str:
    return (f"{IMG_ROOT}/clans/{int(group_id)}/events/{int(event_id)}"
            f"/teams/{int(team_id)}")


def team_board_path(group_id: int, event_id: int, team_id: int) -> str:
    return f"{team_board_dir(group_id, event_id, team_id)}/lootboard.png"


def team_board_url(path: str) -> Optional[str]:
    """Public ``/img`` URL for a generated team board, or None if unservable."""
    from lootboard.timeframe import image_path_to_url

    return image_path_to_url(path)


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #

def board_age_seconds(path: str, now: Optional[float] = None) -> float:
    """Seconds since this team's board was last written (inf when missing)."""
    try:
        return (time.time() if now is None else now) - os.path.getmtime(path)
    except OSError:
        return float("inf")


def should_regenerate(path: str, *, min_interval: int = REFRESH_SECONDS,
                      now: Optional[float] = None) -> bool:
    """True when the board on disk is missing or older than ``min_interval``."""
    return board_age_seconds(path, now=now) >= min_interval


# --------------------------------------------------------------------------- #
# Roster resolution
# --------------------------------------------------------------------------- #

def merge_roster(explicit_ids: Iterable[int], clan_ids: Iterable[int],
                 auto_clan: bool) -> List[int]:
    """The player ids a team board should aggregate over.

    Explicit ``EventTeamMember`` rows win whenever there are any. A whole-clan
    (``auto_clan``) team represents "every current member of group_id" and can
    legitimately have NO roster rows — the sweep materializes them, but between
    activation and the first sweep, and for events that predate it, there are
    none — so it falls back to live clan membership. A non-auto team with an
    empty roster is genuinely empty and stays that way.

    Ignored players (``IgnoredPlayer``) are deliberately NOT filtered out: the
    event engine credits every roster member regardless, and a team board that
    disagreed with the event standings would be worse than one that shows a
    player the group hides from its own group board."""
    pids = sorted({int(p) for p in explicit_ids if p is not None})
    if pids or not auto_clan:
        return pids
    return sorted({int(p) for p in clan_ids if p is not None})


def resolve_team_player_ids(session, team) -> List[int]:
    """:func:`merge_roster` against the DB for one team."""
    from db.models import EventTeamMember

    explicit = [
        pid for (pid,) in
        session.query(EventTeamMember.player_id)
        .filter(EventTeamMember.team_id == team.id)
        .all()
    ]
    auto_clan = bool(getattr(team, "auto_clan", False)) and bool(
        getattr(team, "group_id", None))
    clan: List[int] = []
    if not explicit and auto_clan:
        from db.models.associations import user_group_association

        clan = [
            pid for (pid,) in
            session.query(user_group_association.c.player_id)
            .filter(
                user_group_association.c.group_id == team.group_id,
                user_group_association.c.player_id.isnot(None),
            )
            .distinct()
            .all()
        ]
    return merge_roster(explicit, clan, auto_clan)


# --------------------------------------------------------------------------- #
# Window -> partitions
# --------------------------------------------------------------------------- #

def event_windows(session, event, now: Optional[datetime] = None) -> List[Tuple[datetime, datetime]]:
    """The event's loot-counting spans, split by its recurring schedule.

    Same rules the website's event GP figures use
    (:mod:`web_api.event_loot`) so a team board and the event's Players/Teams
    tabs describe the same period. Empty for drafts / future starts."""
    from web_api.event_loot import event_window, intersect_windows

    return intersect_windows(event_window(event, now),
                             _schedule_windows(session, event))


def _schedule_windows(session, event) -> List[Tuple[datetime, datetime]]:
    """Materialized scoring windows (web82a), or [] for continuous events.
    Fails open to [] — a lookup error should widen the board, never blank it."""
    if not getattr(event, "schedule_config", None):
        return []
    try:
        from db.models import EventWindow

        return [
            (w.starts_at, w.ends_at)
            for w in session.query(EventWindow)
            .filter(EventWindow.event_id == event.id)
            .order_by(EventWindow.starts_at)
            .all()
        ]
    except Exception:
        return []


def board_partitions(windows: Sequence[Tuple[datetime, datetime]], *,
                     retention_days: Optional[int] = None,
                     now: Optional[datetime] = None) -> Tuple[str, List[str]]:
    """``(granularity, partitions)`` for a set of loot-counting windows.

    Daily partitions are the only ones that can be summed to an event's actual
    window, so they are preferred — but they only exist for the last
    ``retention_days`` (the daily Redis hashes' TTL). An event that started
    before that falls back to MONTHLY, which is approximate: a month partition
    holds the whole month, including loot earned outside the event."""
    from lootboard.timeframe import (
        daily_tokens, effective_retention_days, month_partitions,
    )

    spans = [(s, e) for s, e in (windows or []) if s and e and e >= s]
    if not spans:
        return GRANULARITY_DAILY, []

    if retention_days is None:
        retention_days = effective_retention_days()
    now = now or datetime.now()
    earliest = min(s for s, _ in spans)
    if earliest >= now - timedelta(days=retention_days):
        days: set = set()
        for start, end in spans:
            days.update(daily_tokens(start.date(), end.date()))
        return GRANULARITY_DAILY, sorted(days)

    months: set = set()
    for start, end in spans:
        months.update(str(p) for p in month_partitions(start.date(), end.date()))
    return GRANULARITY_MONTHLY, sorted(months)


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #

def _group_config(session, group_id: int) -> dict:
    if not group_id:
        return {}
    from db.models import GroupConfiguration

    rows = (session.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id).all())
    return {row.config_key: row.config_value for row in rows}


def _background(session, config: dict):
    """The group's configured lootboard style, so a team board looks like the
    board its clan already has."""
    from db.models import LootboardStyle
    from lootboard.generator import config_int, load_background_image

    style_id = config_int(config.get('loot_board_type'), 1) or 1
    style = (session.query(LootboardStyle)
             .filter(LootboardStyle.id == style_id).first())
    if not style:
        style = (session.query(LootboardStyle)
                 .filter(LootboardStyle.id == 1).first())
    local_url = getattr(style, "local_url", None) or (
        "/store/droptracker/disc/lootboard/themes/bank-new-clean-dark.png")
    return load_background_image(local_url)


def _draw_team_header(bg_img, draw, title: str, total_loot: int, *,
                      dynamic_colors: bool):
    """``generator.draw_headers`` centred title, with the team/event name in
    place of the group name (draw_headers resolves the group from the DB and
    has no way to say "this is a team")."""
    from lootboard.generator import black, main_font, yellow
    from utils.dynamic_handling import get_dynamic_color, get_value_color
    from utils.format import format_number

    prefix = f"{title} - "
    value_text = format_number(total_loot)
    prefix_bbox = draw.textbbox((0, 0), prefix, font=main_font)
    prefix_width = prefix_bbox[2] - prefix_bbox[0]
    value_bbox = draw.textbbox((0, 0), value_text, font=main_font)
    value_width = value_bbox[2] - value_bbox[0]

    bg_img_w, _ = bg_img.size
    head_loc_x = int((bg_img_w - (prefix_width + value_width)) / 2)
    head_loc_y = 20
    text_color = get_dynamic_color(bg_img) if dynamic_colors else yellow
    draw.text((head_loc_x, head_loc_y), prefix, font=main_font,
              fill=text_color, stroke_width=2, stroke_fill=black)
    draw.text((head_loc_x + prefix_width, head_loc_y), value_text,
              font=main_font, fill=get_value_color(total_loot),
              stroke_width=1, stroke_fill=black)
    return bg_img


def board_title(event, team) -> str:
    name = (getattr(team, "name", None) or "Team").strip()
    event_name = (getattr(event, "name", None) or "Event").strip()
    return f"{name} | {event_name}"


async def render_team_board(session, event, team, *, force: bool = False,
                            now: Optional[datetime] = None) -> Optional[str]:
    """Generate one team's event lootboard. Returns the written path, or None
    when nothing was written (feature off, restricted event, throttled, empty
    roster, event not yet running).

    ``force`` bypasses the hourly mtime throttle and nothing else. The feature
    flag and the visibility gate are re-checked here rather than left to the
    sweep, because the CLI calls this directly: no caller may put a private
    event's roster on the public ``/img`` tree."""
    if not feature_enabled() or not event_is_public(event):
        return None

    from lootboard.flexible_generator import (
        BoardFilter, FlexibleBoardGenerator, TimeGranularity,
    )
    from lootboard.generator import (
        _ensure_public_dir, config_int, config_truthy, draw_drops_on_image,
        draw_leaderboard, draw_recent_drops,
    )

    group_id = board_group_id(event, team)
    path = team_board_path(group_id, event.id, team.id)
    if not force and not should_regenerate(path):
        return None

    windows = [(s, e) for s, e in event_windows(session, event, now)
               if s and e and e >= s]
    granularity_name, partitions = board_partitions(windows, now=now)
    if not partitions:
        return None

    player_ids = resolve_team_player_ids(session, team)
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
    # _aggregate_player_data, not generate_flexible_board: we already know the
    # roster, and the public entry point's group-shaped side effects (WOM
    # roster fetch, gleaderboard zadd, recent_drops_flexible.json) must not
    # fire for a team.
    board_data = await FlexibleBoardGenerator()._aggregate_player_data(
        player_ids, partitions, granularity, board_filter)

    bg_img, draw = _background(session, config)
    bg_img = await draw_drops_on_image(
        bg_img, draw, board_data.group_items, group_id,
        dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    bg_img = _draw_team_header(
        bg_img, draw, board_title(event, team), board_data.total_loot,
        dynamic_colors=use_dynamic_colors)
    bg_img = await draw_recent_drops(
        bg_img, draw, board_data.recent_drops, min_value=minimum_value,
        dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors)
    bg_img = await draw_leaderboard(
        bg_img, draw, board_data.player_totals,
        dynamic_colors=use_dynamic_colors, use_gp=use_gp_colors,
        session_to_use=session)

    _ensure_public_dir(team_board_dir(group_id, event.id, team.id))
    bg_img.save(path)
    try:
        # Both service accounts regenerate into this tree; a 0644 file written
        # by one is unwritable by the other (the dirs are already 0777).
        os.chmod(path, 0o666)
    except OSError:
        pass
    return path


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def _session_factory():
    from db.models import Session

    return Session


def collect_targets(session, *, event_id: Optional[int] = None,
                    force: bool = False, now: Optional[float] = None) -> list:
    """``[(event_id, team_id, path, age_seconds)]`` for every team of every
    ACTIVE, PUBLIC event whose board is due, most stale first. Ids, not ORM
    objects — the render pass re-reads each row in its own short-lived session.

    Events, not groups: ``board_generator.update_boards`` throttles by premium
    tier, which is a group concept with no meaning for a team.

    The :func:`event_is_public` filter applies to the explicit ``event_id``
    lookup too — asking for one event by id is not consent to publish it."""
    from db.models import Event, EventTeam

    if event_id is not None:
        query = session.query(Event).filter(Event.id == int(event_id))
    else:
        query = session.query(Event).filter(Event.status == "active")
    events = [e for e in query.order_by(Event.id.asc()).all()
              if event_is_public(e)]
    if not events:
        return []

    teams = (session.query(EventTeam)
             .filter(EventTeam.event_id.in_([e.id for e in events]))
             .order_by(EventTeam.id.asc()).all())
    by_event = {e.id: e for e in events}

    targets = []
    for team in teams:
        event = by_event.get(team.event_id)
        if event is None:
            continue
        path = team_board_path(board_group_id(event, team), event.id, team.id)
        age = board_age_seconds(path, now=now)
        if not force and age < REFRESH_SECONDS:
            continue
        targets.append((event.id, team.id, path, age))
    targets.sort(key=lambda t: t[3], reverse=True)
    return targets


async def sweep_team_boards(session_factory=None, *,
                            event_id: Optional[int] = None,
                            force: bool = False,
                            limit: int = TEAM_BOARDS_PER_RUN) -> List[str]:
    """One driver pass: regenerate the stalest due team boards.

    Returns the paths actually written. A no-op unless ``EVENT_TEAM_LOOTBOARDS``
    is set, so deploying this module changes nothing on its own. ``force`` (the
    CLI) only ignores the hourly throttle — it is checked after the flag, never
    instead of it."""
    if not feature_enabled():
        return []

    factory = session_factory or _session_factory()
    written: List[str] = []
    session = factory()
    try:
        targets = collect_targets(session, event_id=event_id, force=force)
    except Exception as e:
        print(f"[team-lootboard] target collection failed: {e}")
        targets = []
    finally:
        try:
            session.close()
        except Exception:
            pass

    for target_event_id, team_id, _path, _age in targets[:max(0, int(limit))]:
        session = factory()
        try:
            from db.models import Event, EventTeam

            event = (session.query(Event)
                     .filter(Event.id == target_event_id).first())
            team = (session.query(EventTeam)
                    .filter(EventTeam.id == team_id).first())
            if event is None or team is None:
                continue
            path = await render_team_board(session, event, team, force=force)
            if path:
                written.append(path)
                print(f"[team-lootboard] event {target_event_id} "
                      f"team {team_id} -> {path}")
        except Exception as e:
            print(f"[team-lootboard] event {target_event_id} "
                  f"team {team_id} failed: {e}")
        finally:
            try:
                session.close()
            except Exception:
                pass
    return written
