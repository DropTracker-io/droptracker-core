"""Discord scheduled-event mirroring for web events (desired-state model).

``web_event_guilds`` holds one row per (event, guild) that should advertise a
Discord scheduled event. The Web API only ever writes *desired* state here —
:func:`sync_event_guilds` from the event-mutation routes, plus the
``delete_pending`` marks in :func:`services.event_lifecycle.end_event` — and
never opens a Discord connection or holds a bot token. The core bot's
``reconcile_event_scheduled_events`` task (bots/main.py) is the only place
that talks to Discord: it creates/edits/deletes the real scheduled events and
writes back ``discord_scheduled_event_id``.

Idempotent by construction: a row that already has an id is edited, never
re-created, so repeated edits cannot spawn duplicate Discord events. One row
per guild keeps the design dual-guild ready (clan-vs-clan events later insert
multiple rows per event).

Module-level imports are stdlib-only on purpose (same convention as
``services/event_lifecycle.py``): the unit tests load this file directly, so
the conftest ``db``/``services`` stubs never interfere. DB models are
lazy-imported inside functions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Discord scheduled-event field limits.
NAME_MAX = 100
DESCRIPTION_MAX = 1000

# EXTERNAL scheduled events require an end time; used when ends_at is unset
# (or not after starts_at).
DEFAULT_EVENT_DURATION = timedelta(hours=2)

# The "location" shown on the Discord event: the event's page on the site.
EVENT_LOCATION_TMPL = "https://www.droptracker.io/events/{event_id}"


# ══════════════════════════════════════════════════════════════════════════════
# Pure desired-state logic (no I/O — unit-tested in isolation)
# ══════════════════════════════════════════════════════════════════════════════

def desired_guild_ids(event, participant_guild_ids=()) -> set:
    """Guild ids whose Discord scheduled event should mirror ``event``.

    Standard events target ``discord_guild_id`` when set; a past event desires
    nothing (its rows are also explicitly retired by ``end_event``, but this
    guard keeps a post-end edit from resurrecting them). A *draft* also
    desires nothing unless its ``discord_event_policy`` is ``immediate`` —
    by default the mirror only goes live when the event activates
    (``activate_event`` re-syncs), so abandoned drafts never surface on
    Discord (and can't resurrect later, e.g. when an invite-accept sync
    retried a failed draft row). Clan-vs-clan events pass the accepted,
    opted-in participants' linked guilds as ``participant_guild_ids`` (see
    :func:`_participant_guild_ids`).
    """
    if getattr(event, "status", None) == "past":
        return set()
    if (
        getattr(event, "status", None) == "draft"
        and getattr(event, "discord_event_policy", "on_activate") != "immediate"
    ):
        return set()
    out = {str(event.discord_guild_id)} if event.discord_guild_id else set()
    out |= {str(g) for g in participant_guild_ids if g}
    return out


def guild_sync_plan(desired: set, existing: dict) -> dict:
    """Diff the desired guild set against the existing rows (pure; no I/O).

    ``existing`` maps guild_id -> current sync_status. Returns guild-id lists
    ``{"create": [...], "pend": [...], "retire": [...]}``: rows to insert as
    ``pending``, rows to flip back to ``pending`` (re-sync after an edit —
    this is also what retries ``failed`` rows), and rows to mark
    ``delete_pending``. Rows already ``delete_pending`` are left alone: the
    bot is about to delete them, and a later edit recreates the row if that
    guild is desired again.
    """
    return {
        "create": sorted(g for g in desired if g not in existing),
        "pend": sorted(
            g for g in desired if existing.get(g) not in (None, "delete_pending")
        ),
        "retire": sorted(
            g for g, status in existing.items()
            if g not in desired and status != "delete_pending"
        ),
    }


def schedulable(event) -> bool:
    """Whether a Discord scheduled event can be *created* for ``event`` —
    Discord rejects a scheduled start in the past. Rows without one stay
    ``pending`` until the event has a valid future start. NOT a gate for
    editing an existing scheduled event: name/description (and a still-future
    end) must keep syncing after the start passes (see ``future_end``)."""
    if not event.starts_at:
        return False
    return event.starts_at.astimezone(timezone.utc) > datetime.now(timezone.utc)


def future_end(event, now=None):
    """``ends_at`` as aware UTC when it is still in the future, else None.

    Partial edits for already-started (or past-start) scheduled events:
    Discord rejects ``start_time`` changes there, but a moved *end* is still
    editable — and name/description edits must never be blocked just because
    the event already started (that was the "edits stop applying after the
    event starts" bug)."""
    if not getattr(event, "ends_at", None):
        return None
    end = event.ends_at.astimezone(timezone.utc)
    if end <= (now or datetime.now(timezone.utc)):
        return None
    return end


def event_created_ping(event, guild_id, scheduled_event_id, channels_by_kind) -> tuple:
    """``(channel_id, content)`` for the companion ping message the bot posts
    right after *creating* the Discord scheduled event — or ``(None, None)``
    when nothing should be sent.

    Discord scheduled events can't mention anyone by themselves, so the ping
    lives in a normal message linking the freshly created event. Fires only
    for the event's *primary* guild (``discord_guild_id`` — the channel config
    lives there; opt-in clan-vs-clan mirrors in other guilds have no channels)
    and only when ``ping_config['event_created']`` has roles and an
    announcements channel is configured.
    """
    import json

    if not scheduled_event_id or not guild_id:
        return None, None
    if str(guild_id) != (str(event.discord_guild_id) if event.discord_guild_id else None):
        return None, None
    # Inline ping_config parse (stdlib-only, same rules as
    # services/event_notifications.event_ping_role_ids): [] on unset/corrupt.
    role_ids = []
    raw = getattr(event, "ping_config", None)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("event_created"), list):
                role_ids = [str(r) for r in data["event_created"] if r]
        except (ValueError, TypeError):
            role_ids = []
    if not role_ids:
        return None, None
    channel_id = (channels_by_kind or {}).get("announcements")
    if not channel_id:
        return None, None
    mentions = " ".join(f"<@&{rid}>" for rid in role_ids)
    link = f"https://discord.com/events/{guild_id}/{scheduled_event_id}"
    return str(channel_id), f"{mentions} — **{event.name}** has been scheduled!\n{link}"


def sched_fields(event) -> tuple:
    """``(start, end, location)`` for the Discord scheduled event.

    DB datetimes are naive local (``web_api/routes/events.py _dt``); Discord
    parses an offset-less isoformat as UTC, which would shift the time — so
    convert to aware UTC here. EXTERNAL events require an end: fall back to
    ``start + DEFAULT_EVENT_DURATION`` when ``ends_at`` is unset or not after
    the start.
    """
    start = event.starts_at.astimezone(timezone.utc)
    end = event.ends_at.astimezone(timezone.utc) if event.ends_at else None
    if end is None or end <= start:
        end = start + DEFAULT_EVENT_DURATION
    return start, end, EVENT_LOCATION_TMPL.format(event_id=event.id)


# ══════════════════════════════════════════════════════════════════════════════
# DB applier (Web API side — pure DB, the caller owns the commit)
# ══════════════════════════════════════════════════════════════════════════════

def _participant_guild_ids(session, event) -> set:
    """clan_vs_clan: the linked guilds of accepted participant clans that
    *opted in* to mirroring (``mirror_discord_event``, the accept-time
    checkbox) — accepting an invite must not create anything in the accepting
    clan's server by default. Standard/global events return the empty set
    without touching the DB."""
    if (getattr(event, "mode", None) or "standard") != "clan_vs_clan":
        return set()
    from db.models import EventGroup, Group

    rows = (
        session.query(Group.guild_id)
        .join(EventGroup, EventGroup.group_id == Group.group_id)
        .filter(
            EventGroup.event_id == event.id,
            EventGroup.status == "accepted",
            EventGroup.mirror_discord_event.is_(True),
            Group.guild_id.isnot(None),
        )
        .all()
    )
    return {str(g) for (g,) in rows if g}


def sync_event_guilds(session, event) -> None:
    """Reconcile the desired ``web_event_guilds`` rows for ``event``.

    Pure DB — never talks to Discord. Call after any mutation that changes
    what the Discord scheduled event should look like (create with a guild,
    name/description/time edits, guild re-point, clan-vs-clan participant
    accept/remove); the bot reconciler picks the rows up within its ~30s tick.
    """
    from db.models import EventGuild

    rows = session.query(EventGuild).filter(EventGuild.event_id == event.id).all()
    by_guild = {r.guild_id: r for r in rows}
    plan = guild_sync_plan(
        desired_guild_ids(event, _participant_guild_ids(session, event)),
        {gid: r.sync_status for gid, r in by_guild.items()},
    )
    for gid in plan["create"]:
        session.add(EventGuild(event_id=event.id, guild_id=gid, sync_status="pending"))
    for gid in plan["pend"]:
        by_guild[gid].sync_status = "pending"
    for gid in plan["retire"]:
        by_guild[gid].sync_status = "delete_pending"
