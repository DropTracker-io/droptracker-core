"""Per-team Discord channels & roles (web53a) — desired-state model.

``web_event_team_discord`` holds one row per (team, guild) that should carry
an auto-created team role and/or team channel. The Web API only ever writes
*desired* state here — :func:`sync_event_team_discord` from the config PUT and
the team/lifecycle mutation routes, :func:`retire_event_team_discord` from
``end_event``, plus ``members_dirty`` flips from every roster mutation — and
never opens a Discord connection. The core bot's
``reconcile_event_team_discord`` task (services/event_team_discord_bot.py,
driven from bots/main.py) is the only place that talks to Discord: it creates
the role + channel (a text channel, or a thread inside the configured forum
channel), syncs role/thread membership against the materialized roster, and
writes back ``role_id``/``channel_id``.

Config lives as JSON on ``web_events.team_discord_config`` (event scope,
targeting ``Event.discord_guild_id``) and — clan-vs-clan only — on
``web_event_groups.team_discord_config`` (each clan provisions its OWN teams
into its OWN ``discord_guild_id``; deliberately no inheritance, nothing is
created in a clan's server unasked). Merge semantics in
:func:`effective_team_discord_config`.

Module-level imports are stdlib-only on purpose (the unit tests load this file
directly — see services/event_notifications.py); DB models are lazy-imported
inside functions.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# Redis list of Discord roles/channels orphaned by a *hard delete* (event or
# single team): the rows are FK-wiped immediately, so the bot could never see
# them — the Web API pushes ``{"guild_id","role_id","channel_id"}`` JSON here
# and the reconciler drains it best-effort (the events:sched:orphans pattern).
ORPHAN_TEAM_DISCORD_KEY = "events:team_discord:orphans"

# Natural-end grace: roles/channels of a 'delete_48h' event stay usable this
# long after the event ends (wrap-up pings), then the reconciler tears them
# down. A hard delete ignores this and tears down immediately.
END_GRACE = timedelta(hours=48)

# Message types a team channel can receive, with per-team defaults (editable
# by the team captain when leadership is enabled + captain_config is on, else
# by event admins). All team-scoped except event_lead_change, which posts to
# EVERY team channel (each team cares who leads). event_board_roll_prompt is
# team-channel-first: its main-channel default is off
# (services/event_notifications.DEFAULT_MESSAGE_TOGGLES).
DEFAULT_TEAM_MESSAGE_TOGGLES = {
    "event_completion": True,
    "event_task_progress": True,
    "event_line": True,
    "event_blackout": True,
    "event_lead_change": True,
    "event_board_turn": True,
    "event_board_roll_prompt": True,
}

# Progress verbosity for team channels ('off'|'milestones'|'all'). Default
# 'all': a team channel only ever carries its own team's progress, so the
# noise argument that keeps the event-level default at 'off' doesn't apply.
DEFAULT_TEAM_TASK_PROGRESS = "all"

DEFAULT_TEAM_DISCORD_CONFIG = {
    "channels_enabled": False,
    "roles_enabled": False,
    # Snowflake of a FORUM channel: when set, team channels are auto-created
    # threads inside it instead of guild text channels.
    "forum_channel_id": None,
    # EVENT_TEAM_DISCORD_RETENTIONS — what happens on natural event end.
    "retention": "delete_48h",
    # Whether team captains (leadership feature) may edit their own team's
    # notification toggles; off = event admins only.
    "captain_config": True,
    # Per-team overrides: {"<team_id>": {"role": bool, "channel": bool,
    # "toggles": {...}, "task_progress": mode}}. Absent team = both on +
    # default toggles.
    "teams": {},
}


def effective_team_discord_config(raw_json) -> dict:
    """Defaults overlaid with the stored JSON; corrupt JSON/unknown keys are
    ignored (a bad config must never break a sync or a send)."""
    import json

    config = {
        "channels_enabled": DEFAULT_TEAM_DISCORD_CONFIG["channels_enabled"],
        "roles_enabled": DEFAULT_TEAM_DISCORD_CONFIG["roles_enabled"],
        "forum_channel_id": None,
        "retention": DEFAULT_TEAM_DISCORD_CONFIG["retention"],
        "captain_config": DEFAULT_TEAM_DISCORD_CONFIG["captain_config"],
        "teams": {},
    }
    if not raw_json:
        return config
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else dict(raw_json)
    except (ValueError, TypeError):
        return config
    if not isinstance(data, dict):
        return config
    for key in ("channels_enabled", "roles_enabled", "captain_config"):
        if isinstance(data.get(key), bool):
            config[key] = data[key]
    forum = data.get("forum_channel_id")
    if isinstance(forum, (str, int)) and str(forum).isdigit():
        config["forum_channel_id"] = str(forum)
    if data.get("retention") in ("delete_48h", "keep"):
        config["retention"] = data["retention"]
    teams = data.get("teams")
    if isinstance(teams, dict):
        for tid, entry in teams.items():
            if not isinstance(entry, dict):
                continue
            clean = {}
            for key in ("role", "channel"):
                if isinstance(entry.get(key), bool):
                    clean[key] = entry[key]
            toggles = entry.get("toggles")
            if isinstance(toggles, dict):
                clean["toggles"] = {
                    k: bool(v) for k, v in toggles.items()
                    if k in DEFAULT_TEAM_MESSAGE_TOGGLES and isinstance(v, bool)
                }
            if entry.get("task_progress") in ("off", "milestones", "all"):
                clean["task_progress"] = entry["task_progress"]
            if clean:
                config["teams"][str(tid)] = clean
    return config


def config_enabled(config: dict) -> bool:
    """Whether this scope provisions anything at all."""
    return bool(config.get("channels_enabled") or config.get("roles_enabled"))


def team_flags(config: dict, team_id) -> dict:
    """{"role": bool, "channel": bool} for one team under one scope config —
    the scope-level toggles ANDed with the per-team override (absent = on)."""
    entry = (config.get("teams") or {}).get(str(team_id)) or {}
    return {
        "role": bool(config.get("roles_enabled")) and entry.get("role", True),
        "channel": bool(config.get("channels_enabled")) and entry.get("channel", True),
    }


def team_message_toggles(config: dict, team_id) -> dict:
    """Effective notification toggles for one team's channel."""
    entry = (config.get("teams") or {}).get(str(team_id)) or {}
    toggles = dict(DEFAULT_TEAM_MESSAGE_TOGGLES)
    toggles.update(entry.get("toggles") or {})
    return toggles


def team_task_progress_mode(config: dict, team_id) -> str:
    entry = (config.get("teams") or {}).get(str(team_id)) or {}
    return entry.get("task_progress", DEFAULT_TEAM_TASK_PROGRESS)


def channel_name_for_team(name: str) -> str:
    """Discord-safe channel name from a team name (lowercase, dashes)."""
    out = []
    prev_dash = False
    for ch in (name or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash and out:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-") or "team"
    return ("team-" + slug)[:90]


# --------------------------------------------------------------------------- #
# Desired-state materialization (Web API side)
# --------------------------------------------------------------------------- #
def team_discord_scopes(session, event) -> list:
    """Provisioning scopes for one event:
    ``[{"group_id": None|gid, "guild_id": str, "config": effective}]``.

    The event scope (group_id None) covers EVERY team and targets
    ``Event.discord_guild_id``; a clan scope (clan-vs-clan) covers only that
    clan's teams and targets its own ``EventGroup.discord_guild_id``. Scopes
    with the feature off or no guild resolve to nothing.
    """
    scopes = []
    config = effective_team_discord_config(getattr(event, "team_discord_config", None))
    if config_enabled(config) and getattr(event, "discord_guild_id", None):
        scopes.append({
            "group_id": None,
            "guild_id": str(event.discord_guild_id),
            "config": config,
        })
    if (getattr(event, "mode", None) or "standard") == "clan_vs_clan":
        from db.models import EventGroup

        rows = (session.query(EventGroup)
                .filter(EventGroup.event_id == event.id,
                        EventGroup.status == "accepted",
                        EventGroup.team_discord_config.isnot(None))
                .order_by(EventGroup.id.asc())
                .all())
        for g in rows:
            gconfig = effective_team_discord_config(g.team_discord_config)
            if config_enabled(gconfig) and g.discord_guild_id:
                scopes.append({
                    "group_id": g.group_id,
                    "guild_id": str(g.discord_guild_id),
                    "config": gconfig,
                })
    return scopes


def _desired_rows(session, event) -> dict:
    """{(team_id, guild_id): scope_group_id} the event should have."""
    from db.models import EventTeam

    teams = (session.query(EventTeam)
             .filter(EventTeam.event_id == event.id)
             .all())
    desired = {}
    for scope in team_discord_scopes(session, event):
        for team in teams:
            if scope["group_id"] is not None and team.group_id != scope["group_id"]:
                continue
            flags = team_flags(scope["config"], team.id)
            if not (flags["role"] or flags["channel"]):
                continue
            desired[(team.id, scope["guild_id"])] = scope["group_id"]
    return desired


def sync_event_team_discord(session, event) -> None:
    """Reconcile the ``web_event_team_discord`` rows with the current config +
    team list (the ``sync_event_guilds`` pattern). Creates missing rows as
    ``pending``, re-pends existing rows so renames/config edits (and
    ``failed`` rows) get another pass, and marks rows whose (team, guild) is
    no longer desired ``delete_pending`` for immediate teardown. A past event
    desires nothing new but is left to :func:`retire_event_team_discord` /
    the reconciler for teardown."""
    from db.models import EventTeamDiscord

    rows = (session.query(EventTeamDiscord)
            .filter(EventTeamDiscord.event_id == event.id)
            .all())
    if getattr(event, "status", None) == "past":
        return
    desired = _desired_rows(session, event)
    existing = {(r.team_id, str(r.guild_id)): r for r in rows}
    for key, scope_group_id in desired.items():
        row = existing.get(key)
        if row is None:
            session.add(EventTeamDiscord(
                event_id=event.id, team_id=key[0], guild_id=key[1],
                group_id=scope_group_id, sync_status="pending",
                members_dirty=True,
            ))
        elif row.sync_status != "pending":
            # delete_pending included: an un-toggled team toggled back on (or
            # a rename) beats the pending teardown — the bot re-creates
            # whatever it already tore down.
            row.sync_status = "pending"
            row.group_id = scope_group_id
            row.delete_after = None
            row.last_error = None
    for key, row in existing.items():
        if key not in desired and row.sync_status != "delete_pending":
            row.sync_status = "delete_pending"
            row.delete_after = None  # immediate: config/team removal
    session.flush()


def retire_event_team_discord(session, event, *, now: Optional[datetime] = None) -> None:
    """Natural event end: apply each scope's retention. ``keep`` releases the
    rows (delete them; the Discord role/channel stays forever), ``delete_48h``
    marks them ``delete_pending`` with a grace deadline so pings stay usable
    for wrap-up, then the reconciler tears them down."""
    from db.models import EventTeamDiscord

    now = now or datetime.now()
    retention_by_scope = {
        scope["group_id"]: scope["config"].get("retention", "delete_48h")
        for scope in team_discord_scopes(session, event)
    }
    rows = (session.query(EventTeamDiscord)
            .filter(EventTeamDiscord.event_id == event.id)
            .all())
    for row in rows:
        retention = retention_by_scope.get(row.group_id, "delete_48h")
        if retention == "keep":
            session.delete(row)
        elif row.sync_status != "delete_pending":
            row.sync_status = "delete_pending"
            row.delete_after = now + END_GRACE
    session.flush()


def mark_team_members_dirty(session, event_id: int, team_id=None) -> None:
    """Roster changed: flag the affected rows so the bot re-syncs role/thread
    membership on its next tick. Cheap no-op when the feature is off."""
    from db.models import EventTeamDiscord

    query = (session.query(EventTeamDiscord)
             .filter(EventTeamDiscord.event_id == event_id))
    if team_id is not None:
        query = query.filter(EventTeamDiscord.team_id == team_id)
    query.update({EventTeamDiscord.members_dirty: True}, synchronize_session=False)


def orphan_team_discord_payloads(session, event_id: int, team_id=None) -> list:
    """``{"guild_id","role_id","channel_id","channel_kind"}`` dicts for every
    row that carries live Discord objects — enqueued on
    :data:`ORPHAN_TEAM_DISCORD_KEY` before a hard delete FK-wipes the rows."""
    from db.models import EventTeamDiscord

    query = (session.query(EventTeamDiscord)
             .filter(EventTeamDiscord.event_id == event_id))
    if team_id is not None:
        query = query.filter(EventTeamDiscord.team_id == team_id)
    out = []
    for row in query.all():
        if not (row.role_id or row.channel_id):
            continue
        out.append({
            "guild_id": str(row.guild_id),
            "role_id": str(row.role_id) if row.role_id else None,
            "channel_id": str(row.channel_id) if row.channel_id else None,
            "channel_kind": row.channel_kind,
        })
    return out


def enqueue_team_discord_orphans(redis_conn, payloads: list) -> None:
    """RPUSH orphan payloads (best-effort; teardown loss is tolerable, FK
    integrity is not — mirrors _enqueue_orphan_scheduled_events)."""
    import json

    if not payloads or redis_conn is None:
        return
    try:
        redis_conn.rpush(ORPHAN_TEAM_DISCORD_KEY, *[json.dumps(p) for p in payloads])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Notification routing (send + enqueue side)
# --------------------------------------------------------------------------- #
# Types that post to the owning team's channel only. event_lead_change instead
# posts to EVERY team channel (toggle-gated per team).
TEAM_SCOPED_TYPES = (
    "event_completion",
    "event_task_progress",
    "event_line",
    "event_blackout",
    "event_board_turn",
    "event_board_roll_prompt",
)


def team_channel_interest(session, event_id: int, notification_type: str,
                          team_id=None) -> bool:
    """Enqueue-side gate helper: could ANY team channel want this message?
    Used to enqueue types the event-level config has muted (the sender does
    the precise per-destination toggle filtering). Deliberately coarse — an
    existing live/pending row is 'interest'; a team that toggled the type off
    is filtered at send time."""
    if notification_type not in TEAM_SCOPED_TYPES + ("event_lead_change",):
        return False
    from db.models import EventTeamDiscord

    query = (session.query(EventTeamDiscord.id)
             .filter(EventTeamDiscord.event_id == event_id,
                     EventTeamDiscord.sync_status.in_(("pending", "synced"))))
    if notification_type in TEAM_SCOPED_TYPES and team_id is not None:
        query = query.filter(EventTeamDiscord.team_id == team_id)
    return query.first() is not None


def team_progress_interest(session, event_id: int, team_id) -> str:
    """The most verbose task-progress mode ('off'|'milestones'|'all') any of
    this team's live team channels wants — the enqueue-side counterpart of the
    per-destination filtering in :func:`load_team_destinations`."""
    from db.models import Event, EventTeamDiscord

    has_row = (session.query(EventTeamDiscord.id)
               .filter(EventTeamDiscord.event_id == event_id,
                       EventTeamDiscord.team_id == team_id,
                       EventTeamDiscord.sync_status.in_(("pending", "synced")))
               .first())
    if not has_row:
        return "off"
    event = session.query(Event).filter(Event.id == event_id).first()
    if event is None:
        return "off"
    order = {"off": 0, "milestones": 1, "all": 2}
    best = "off"
    for scope in team_discord_scopes(session, event):
        if not team_message_toggles(scope["config"], team_id).get(
                "event_task_progress", True):
            continue
        mode = team_task_progress_mode(scope["config"], team_id)
        if order[mode] > order[best]:
            best = mode
    return best


def load_team_destinations(session, event, notification_type: str,
                           team_id=None, milestone: bool = True) -> list:
    """Send destinations for the team channels of one event:
    ``[{"channel_id", "role_id", "team_id"}]``, toggle-filtered per team.

    Team-scoped types resolve to the owning team's channel(s) (one per guild
    in clan-vs-clan); ``event_lead_change`` resolves to every team channel
    whose toggle is on. ``milestone`` carries whether an
    ``event_task_progress`` row crossed a milestone — teams in 'milestones'
    mode skip non-milestone increments."""
    is_lead = notification_type == "event_lead_change"
    if notification_type not in TEAM_SCOPED_TYPES and not is_lead:
        return []
    if not is_lead and team_id is None:
        return []
    from db.models import EventTeamDiscord

    query = (session.query(EventTeamDiscord)
             .filter(EventTeamDiscord.event_id == event.id,
                     EventTeamDiscord.sync_status == "synced",
                     EventTeamDiscord.channel_id.isnot(None)))
    if not is_lead:
        query = query.filter(EventTeamDiscord.team_id == team_id)
    rows = query.all()
    if not rows:
        return []

    configs = {
        scope["group_id"]: scope["config"]
        for scope in team_discord_scopes(session, event)
    }
    out = []
    for row in rows:
        config = configs.get(row.group_id)
        if config is None:
            continue  # scope disabled since the row was created
        toggles = team_message_toggles(config, row.team_id)
        if not toggles.get(notification_type, True):
            continue
        if notification_type == "event_task_progress":
            mode = team_task_progress_mode(config, row.team_id)
            if mode == "off" or (mode == "milestones" and not milestone):
                continue
        out.append({
            "channel_id": str(row.channel_id),
            "role_id": str(row.role_id) if row.role_id else None,
            "team_id": row.team_id,
        })
    return out
