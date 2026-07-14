"""Live event standings board — the lootboard pattern applied to events.

When an event has a ``leaderboard`` channel configured (web_event_channels)
and its message_config keeps the live board on (default), the core bot posts
one standings message to that channel as the event activates and then keeps
*that same message* edited in place for the whole event:

- immediately after score-changing notifications send (event started /
  completion / bingo / lead change / ended — see REFRESH_AFTER_TYPES), with a
  short cooldown so a burst of completions doesn't hammer the edit endpoint;
- on a 2-minute interval sweep from the core bot (bots/main.py) as a
  catch-all, which also renders the final "ended" state for events that
  finished in the last few minutes and then lets them go quiet.

The message id lives on the leaderboard EventChannel row itself
(``message_id`` / ``message_updated_at``, web41a) — post once, edit in
place, repost if it vanished. Rendering goes through the component layout
registry (services/event_message_layouts.py, message type ``event_board``).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from db.app_logger import AppLogger

app_logger = AppLogger()

# Notification types whose successful send should immediately refresh the
# board (anything that changes scores or lifecycle state).
REFRESH_AFTER_TYPES = (
    "event_started",
    "event_completion",
    "event_cell",
    "event_line",
    "event_blackout",
    "event_lead_change",
    "event_ended",
)

# Skip hook-triggered refreshes when the board was edited this recently; the
# interval sweep (force=True) trues everything up regardless.
REFRESH_COOLDOWN_SECONDS = 20

# How long after ending an event keeps getting swept (renders the final
# standings once or twice, then goes quiet forever).
RECENT_END_WINDOW_MINUTES = 15


def _board_row(session, event_id: int):
    from db.models import EventChannel

    return (
        session.query(EventChannel)
        .filter(EventChannel.event_id == event_id, EventChannel.kind == "leaderboard")
        .first()
    )


def _standings(session, event_id: int, limit: int) -> list:
    from db.models import EventTeam

    rows = (
        session.query(EventTeam)
        .filter(EventTeam.event_id == event_id)
        .order_by(EventTeam.score.desc(), EventTeam.id.asc())
        .limit(limit)
        .all()
    )
    return [{"name": t.name, "score": int(t.score or 0)} for t in rows]


def _tasks_summary(session, event) -> Optional[str]:
    """Aggregate progress line: task completions across all teams, plus
    claimed bingo cells when the event has a board."""
    from db.models import EventBingoCell, EventBingoCompletion, EventProgress, EventTeam

    completed = (
        session.query(EventProgress)
        .filter(EventProgress.event_id == event.id, EventProgress.completed.is_(True))
        .count()
    )
    parts = []
    if completed:
        parts.append(f"\U0001F4CB {completed} task completion{'s' if completed != 1 else ''}")
    if event.has_bingo:
        cells = (
            session.query(EventBingoCompletion)
            .join(EventBingoCell, EventBingoCompletion.cell_id == EventBingoCell.id)
            .filter(EventBingoCell.event_id == event.id)
            .count()
        )
        if cells:
            parts.append(f"\U0001F3AF {cells} bingo cell{'s' if cells != 1 else ''} claimed")
    if not parts:
        return None
    return "-# " + " • ".join(parts)


def _board_context(session, event, config: dict) -> dict:
    from db.models import EventTeam
    from services.event_notifications import event_url

    team_count = session.query(EventTeam).filter(EventTeam.event_id == event.id).count()
    if event.status == "past":
        status_line = "Final standings \U0001F3C1"
    else:
        bits = ["Live"]
        if event.ends_at:
            bits.append(f"Ends <t:{int(event.ends_at.timestamp())}:R>")
        bits.append(f"{team_count} team{'s' if team_count != 1 else ''}")
        status_line = " • ".join(bits)

    context = {
        "event_name": event.name,
        "event_url": event_url(event.id),
        "board_status_line": status_line,
        "team_count": team_count or None,
        "updated_ts": f"<t:{int(datetime.now().timestamp())}:R>",
    }
    if config["leaderboard"].get("show_tasks", True):
        summary = _tasks_summary(session, event)
        if summary:
            context["tasks_summary"] = summary
    return context


def _apply_top_n(layout: dict, top_n: int) -> dict:
    """Copy of ``layout`` with every standings block clamped to the event's
    configured top_n (the layout's own limit is just the template default)."""
    patched = dict(layout)
    patched["blocks"] = [
        {**b, "limit": top_n} if isinstance(b, dict) and b.get("type") == "standings" else b
        for b in layout.get("blocks") or []
    ]
    return patched


async def refresh_event_board(bot, session, event, *, force: bool = False) -> bool:
    """Render and post/edit one event's live standings board.

    Returns True when a Discord write happened. Never raises — board upkeep
    must not take down the caller (notification send loop / bot task)."""
    try:
        from services.event_message_layouts import (
            build_components,
            load_layout,
            render_message_spec,
        )
        from services.event_notifications import effective_message_config

        config = effective_message_config(getattr(event, "message_config", None))
        if not config["leaderboard"].get("live", True):
            return False
        row = _board_row(session, event.id)
        if row is None or not row.channel_id:
            return False
        if (
            not force
            and row.message_updated_at is not None
            and datetime.now() - row.message_updated_at
            < timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
        ):
            return False

        top_n = int(config["leaderboard"].get("top_n") or 10)
        layout = _apply_top_n(load_layout(session, event.group_id, "event_board"), top_n)
        spec = render_message_spec(
            layout,
            _board_context(session, event, config),
            standings=_standings(session, event.id, top_n),
        )
        components = build_components(spec)

        channel = await bot.fetch_channel(channel_id=row.channel_id)
        if channel is None or not callable(getattr(channel, "send", None)):
            return False

        message = None
        if row.message_id:
            try:
                message = await channel.fetch_message(message_id=row.message_id)
            except Exception:
                message = None  # deleted / inaccessible — repost below
        if message is not None:
            await message.edit(components=components)
        else:
            message = await channel.send(components=components)
            row.message_id = str(message.id)
        row.message_updated_at = datetime.now()
        session.commit()
        return True
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        app_logger.log(
            log_type="error",
            data=f"Event board refresh failed for event {getattr(event, 'id', '?')}: {e}",
            app_name="event_board",
            description="refresh_event_board",
        )
        return False


async def refresh_after_notification(bot, session, event, notification_type: str) -> None:
    """Post-send hook (services/notification_service.py): keep the board hot
    on score changes without waiting for the interval sweep."""
    if notification_type not in REFRESH_AFTER_TYPES:
        return
    # Lifecycle transitions redraw unconditionally (first post / final state);
    # mid-event bursts respect the cooldown.
    force = notification_type in ("event_started", "event_ended")
    await refresh_event_board(bot, session, event, force=force)


async def run_board_sweep(bot) -> None:
    """Interval catch-all (bots/main.py, every 2 minutes): refresh every
    active event's board — plus recently-ended events so their message shows
    the final standings — regardless of notification traffic (time-remaining
    and 'updated' stamps drift otherwise)."""
    from db.models import Event, EventChannel, Session

    session = Session()
    try:
        cutoff = datetime.now() - timedelta(minutes=RECENT_END_WINDOW_MINUTES)
        events = (
            session.query(Event)
            .join(EventChannel, EventChannel.event_id == Event.id)
            .filter(
                EventChannel.kind == "leaderboard",
                (Event.status == "active")
                | ((Event.status == "past") & (Event.ended_at >= cutoff)),
            )
            .all()
        )
        for event in events:
            await refresh_event_board(bot, session, event, force=True)
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"Event board sweep failed: {e}",
            app_name="event_board",
            description="run_board_sweep",
        )
    finally:
        session.close()
