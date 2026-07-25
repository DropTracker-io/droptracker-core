"""Retiring the Discord sign-up prompt when sign-ups close (web70a).

An admin posts the "Sign up" prompt from the event manager
(``POST /events/{id}/signup-message``) and the notification service attaches
the ``evtsignup:{event_id}`` button as it sends. That post used to live
forever: once the event began, the button was still there and the message
still advertised a sign-up window, so players kept clicking a control that
would refuse them (or, worse, believed they were entered).

Two halves fix that:

- :func:`record_prompt` — the sender stores every posted prompt
  (``web_event_signup_messages``: event, channel, message) as each destination
  send succeeds. Nothing recorded the message id before, which is why the post
  could never be revisited.
- :func:`close_signup_prompts` — edits those messages into the
  ``event_signup_closed`` layout with **no button**, and stamps ``closed_at``.
  Driven from two places, the same belt-and-braces as the live board:
  immediately after the ``event_started`` announcement sends
  (services/notification_service.py), and from the core bot's 2-minute sweep
  (:func:`run_signup_prompt_sweep`) which also catches events that ended, a
  muted start announcement, an admin flipping ``allow_late_signups`` off, and
  anything missed while the bot was down.

The button handler re-checks ``signups_closed`` itself
(services/event_signup_discord.py), so a message we cannot edit — deleted
channel, lost permissions — still can't take entries. Editing the post is
about honesty, not enforcement.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from db.app_logger import AppLogger

app_logger = AppLogger()

# How long after an event ends its prompts still get swept. The prompt closes
# at the *start* for a default event, so this only matters for the
# allow_late_signups events whose window runs to the end — after which they
# go quiet forever (mirrors event_board.RECENT_END_WINDOW_MINUTES).
RECENT_END_WINDOW_HOURS = 24


def record_prompt(session, event_id: int, channel_id, message_id, group_id=None) -> None:
    """Remember one posted sign-up prompt so it can be retired later.

    Best-effort by design: a bookkeeping failure must never turn a delivered
    notification into a failed one (the caller has already sent the message).
    """
    from db.models import EventSignupMessage

    if not event_id or not channel_id or not message_id:
        return
    try:
        row = (
            session.query(EventSignupMessage)
            .filter(EventSignupMessage.event_id == event_id,
                    EventSignupMessage.message_id == str(message_id))
            .first()
        )
        if row is None:
            session.add(EventSignupMessage(
                event_id=event_id,
                channel_id=str(channel_id),
                message_id=str(message_id),
                group_id=group_id,
            ))
            session.commit()
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        app_logger.log(
            log_type="error",
            data=f"Could not record sign-up prompt for event {event_id}: {e}",
            app_name="event_signup_prompt",
            description="record_prompt",
        )


def _closed_context(event) -> dict:
    """The token dict the closed layout renders from."""
    from services.event_message_layouts import notification_context
    from services.event_signup import signup_close_at

    close_at = signup_close_at(event)
    if getattr(event, "allow_late_signups", False):
        line = "This event has ended — sign-ups are closed."
    elif close_at:
        line = ("The event is underway — sign-ups closed "
                f"<t:{int(close_at.timestamp())}:R>.")
    else:
        line = "The event is underway — sign-ups are closed."
    return notification_context("event_signup_prompt", {
        "event_id": event.id,
        "event_name": event.name,
        "starts_at": int(event.starts_at.timestamp()) if event.starts_at else None,
        "ends_at": int(event.ends_at.timestamp()) if event.ends_at else None,
        "signups_closed": True,
        "signup_closed_line": line,
    })


async def close_signup_prompts(bot, session, event) -> int:
    """Retire every still-open sign-up prompt for ``event``. Returns how many
    messages were edited. Never raises — this is upkeep hanging off a send."""
    from db.models import EventSignupMessage

    edited = 0
    try:
        rows = (
            session.query(EventSignupMessage)
            .filter(EventSignupMessage.event_id == event.id,
                    EventSignupMessage.closed_at.is_(None))
            .all()
        )
        if not rows:
            return 0

        from services.activity_launch import channel_supports_launch
        from services.event_message_layouts import render_event_components

        context = _closed_context(event)
        for row in rows:
            try:
                channel = await bot.fetch_channel(channel_id=row.channel_id)
            except Exception:
                channel = None
            if channel is None:
                # Unreachable right now — could be a deleted channel, could be
                # a Discord blip. Leave the row open so the next sweep tries
                # again; it stops being swept once the event has been over for
                # RECENT_END_WINDOW_HOURS. The button handler's own gate keeps
                # the meanwhile-stale post harmless.
                continue
            try:
                message = await channel.fetch_message(message_id=row.message_id)
            except Exception:
                message = None
            if message is None:
                row.closed_at = datetime.now()  # deleted by a human; nothing to do
                session.commit()
                continue
            components = render_event_components(
                session, event.group_id, "event_signup_prompt", context,
                allow_launch=channel_supports_launch(channel),
                event_id=event.id,
            )
            try:
                # No extra_rows: dropping the "Sign up" button IS the fix.
                await message.edit(components=components)
            except Exception as edit_error:
                # A prompt that fell back to the legacy embed at send time is
                # not a Components-V2 message and cannot be re-rendered as one.
                # Stripping its components still removes the button, which is
                # the part that misleads people.
                try:
                    await message.edit(components=[])
                except Exception:
                    # We fetched the message, so this is not a transient
                    # reachability problem — retrying every sweep for the rest
                    # of the event would only spam Discord and the log. The
                    # button handler's gate keeps the stale post harmless.
                    app_logger.log(
                        log_type="error",
                        data=f"Could not retire sign-up prompt {row.message_id} "
                             f"(event {event.id}): {edit_error}",
                        app_name="event_signup_prompt",
                        description="close_signup_prompts",
                    )
                    row.closed_at = datetime.now()
                    session.commit()
                    continue
            row.closed_at = datetime.now()
            session.commit()
            edited += 1
        return edited
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        app_logger.log(
            log_type="error",
            data=f"Could not retire sign-up prompts for event "
                 f"{getattr(event, 'id', '?')}: {e}",
            app_name="event_signup_prompt",
            description="close_signup_prompts",
        )
        return edited


async def close_after_notification(bot, session, event, notification_type: str) -> None:
    """Post-send hook (services/notification_service.py): the moment the start
    announcement lands, the prompt that advertised sign-ups stops offering
    them. ``event_ended`` covers allow_late_signups events."""
    if notification_type not in ("event_started", "event_ended"):
        return
    await close_signup_prompts(bot, session, event)


async def run_signup_prompt_sweep(bot) -> None:
    """Interval catch-all (bots/main.py): retire the prompts of every event
    whose sign-ups have since closed — manual activations with the start
    announcement muted, a toggle flipped off, or anything the bot missed
    while it was down."""
    from db.models import Event, EventSignupMessage, Session
    from services.event_signup import signups_closed

    session = Session()
    try:
        cutoff = datetime.now() - timedelta(hours=RECENT_END_WINDOW_HOURS)
        events = (
            session.query(Event)
            .join(EventSignupMessage, EventSignupMessage.event_id == Event.id)
            .filter(
                EventSignupMessage.closed_at.is_(None),
                (Event.ended_at.is_(None)) | (Event.ended_at >= cutoff),
            )
            .distinct()
            .all()
        )
        for event in events:
            if signups_closed(event) is None:
                continue  # window still open — leave the button alone
            await close_signup_prompts(bot, session, event)
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"Sign-up prompt sweep failed: {e}",
            app_name="event_signup_prompt",
            description="run_signup_prompt_sweep",
        )
    finally:
        session.close()
