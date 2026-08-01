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
    "event_line",
    "event_blackout",
    "event_lead_change",
    "event_ended",
    # Loot Sweep bonus payouts change standings — keep the board hot. The
    # per-item event_sweep_item is deliberately excluded (the 2-min sweep +
    # Redis-cached image already cover it without an edit per drop).
    "event_sweep_group",
    "event_sweep_set",
)

# Skip hook-triggered refreshes when the board was edited this recently; the
# interval sweep (force=True) trues everything up regardless.
REFRESH_COOLDOWN_SECONDS = 20

# How long after ending an event keeps getting swept (renders the final
# standings once or twice, then goes quiet forever).
RECENT_END_WINDOW_MINUTES = 15


def _board_rows(session, event) -> list:
    """The leaderboard EventChannel rows to keep boards on, each paired with
    the message_config that governs it: ``[(row, effective_config)]``.

    Default events: the single shared row (group_id NULL) + event config.
    Per-group clan-vs-clan (web48a): every leaderboard row — each clan's own
    row runs under that clan's verbosity override — deduped by channel id so
    a clan pointing at the host's channel doesn't get a second board."""
    from db.models import EventChannel, EventGroup
    from services.event_notifications import (
        effective_message_config,
        per_group_discord_enabled,
    )

    event_config = effective_message_config(getattr(event, "message_config", None))
    query = (session.query(EventChannel)
             .filter(EventChannel.event_id == event.id,
                     EventChannel.kind == "leaderboard")
             .order_by(EventChannel.id.asc()))
    if not per_group_discord_enabled(event):
        rows = [r for r in query.all() if getattr(r, "group_id", None) is None]
        return [(r, event_config) for r in rows]

    group_configs = {
        g.group_id: effective_message_config(
            g.message_config if g.message_config
            else getattr(event, "message_config", None))
        for g in session.query(EventGroup)
        .filter(EventGroup.event_id == event.id, EventGroup.status == "accepted")
        .all()
    }
    out, seen = [], set()
    for r in query.all():
        gid = getattr(r, "group_id", None)
        if gid is not None and gid not in group_configs:
            continue  # declined/removed clan — leave its board alone
        if not r.channel_id or str(r.channel_id) in seen:
            continue
        seen.add(str(r.channel_id))
        out.append((r, event_config if gid is None else group_configs[gid]))
    return out


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


def _window_state(session, event) -> dict | None:
    """``{open, current_end, next_start}`` (unix seconds) for a
    recurring-schedule event (web82a), or ``None`` for a continuous one.
    Fails open to None — the board must render regardless."""
    if not getattr(event, "schedule_config", None):
        return None
    try:
        from datetime import datetime as _dt

        from services.event_schedule import current_window, load_windows, next_window

        now = _dt.now()
        windows = load_windows(session, event.id)
        if not windows:
            return None
        cur, nxt = current_window(windows, now), next_window(windows, now)
        return {
            "open": cur is not None,
            "current_end": int(cur[1].timestamp()) if cur else None,
            "next_start": int(nxt[0].timestamp()) if nxt else None,
        }
    except Exception:
        return None


def _board_context(session, event, config: dict) -> dict:
    from db.models import EventTeam
    from services.event_notifications import event_url

    team_count = session.query(EventTeam).filter(EventTeam.event_id == event.id).count()
    if event.status == "past":
        status_line = "Final standings \U0001F3C1"
    else:
        # Recurring schedules (web82a): between scoring windows the event is
        # still live but nothing counts — say so, and when it resumes.
        window_state = _window_state(session, event)
        if window_state and not window_state["open"]:
            bits = ["⏸️ Paused"]
            if window_state["next_start"]:
                bits.append(f"Resumes <t:{window_state['next_start']}:R>")
        else:
            bits = ["Live"]
            if window_state and window_state["current_end"]:
                bits.append(f"Window closes <t:{window_state['current_end']}:R>")
            elif event.ends_at:
                bits.append(f"Ends <t:{int(event.ends_at.timestamp())}:R>")
        bits.append(f"{team_count} team{'s' if team_count != 1 else ''}")
        status_line = " • ".join(bits)

    context = {
        "event_name": event.name,
        "event_id": event.id,  # raw id — powers the "Open in Discord" launch button
        "event_url": event_url(event.id),
        "board_status_line": status_line,
        "team_count": team_count or None,
        "updated_ts": f"<t:{int(datetime.now().timestamp())}:R>",
        # Raw unix seconds for the universal event footer (event_footer_line).
        "starts_at_unix": int(event.starts_at.timestamp()) if event.starts_at else None,
        "ends_at_unix": int(event.ends_at.timestamp()) if event.ends_at else None,
    }
    if config["leaderboard"].get("show_tasks", True):
        summary = _tasks_summary(session, event)
        if summary:
            context["tasks_summary"] = summary

    # Prize pot (web52a): a running headline on the board, gated on
    # buyins_enabled AND prize_config.advertise. Read fresh here so a config or
    # buy-in change surfaces on the next 2-min sweep with no pub/sub. The line
    # drops via the token-drop rule when pot_line is unset.
    from web_api.event_prizes import pot_line, pot_summary
    from services.event_notifications import format_gp

    pot = pot_summary(session, event, team_count=team_count)
    if pot["enabled"] and pot["advertise"]:
        context["pot_line"] = pot_line(
            format_gp(pot["total"]), pot["distribution"], pot["top_n"],
        )
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
            deeplink_enabled,
            load_layout,
            render_message_spec,
        )
        from services.activity_launch import channel_supports_launch
        from services.event_notifications import event_footer_line

        wrote = False
        # The live board image (bingo grid / board-game overlay), rendered once
        # per sweep and reused across every leaderboard row. Computed lazily so
        # an all-muted event never renders; None (no visual board, or any
        # failure) just omits the image and keeps the text board. board_image_png
        # is itself Redis-cached on a state hash, so an unchanged board is cheap.
        board_img, board_img_computed = None, False
        for row, config in _board_rows(session, event):
            if not config["leaderboard"].get("live", True):
                continue
            if not row.channel_id:
                continue
            if (
                not force
                and row.message_updated_at is not None
                and datetime.now() - row.message_updated_at
                < timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
            ):
                continue

            channel = await bot.fetch_channel(channel_id=row.channel_id)
            if channel is None or not callable(getattr(channel, "send", None)):
                continue

            # Render the board image once per sweep (reused across rows).
            if not board_img_computed:
                board_img_computed = True
                try:
                    from services.event_board_image import board_image_png
                    board_img = await board_image_png(session, event)
                except Exception:
                    board_img = None  # never let board art block the standings

            top_n = int(config["leaderboard"].get("top_n") or 10)
            # Loot Sweep: the compact standings IMAGE carries the full
            # leaderboard, so the text collapses to a short top-3 fallback (for
            # image-muted clients). Only when the image actually rendered — a
            # render failure keeps the full text standings.
            if getattr(event, "kind", None) == "loot_sweep" and board_img is not None:
                top_n = min(top_n, 3)
            layout = _apply_top_n(
                load_layout(session, event.group_id, "event_board", event_id=event.id),
                top_n)
            # Threads/announcement channels can't host a LAUNCH_ACTIVITY
            # callback — render an Activity Link URL button (client-side
            # launch) instead.
            enabled, supported = deeplink_enabled(), channel_supports_launch(channel)
            context = _board_context(session, event, config)
            spec = render_message_spec(
                layout,
                context,
                standings=_standings(session, event.id, top_n),
                deep_link=enabled and supported,
                launch_link=enabled and not supported,
                footer=event_footer_line(
                    context.get("event_name"),
                    context.get("starts_at_unix"),
                    context.get("ends_at_unix"),
                ),
            )
            # Attach the board as a FILE and reference it as attachment:// —
            # Components-V2 media galleries render attachments reliably where
            # external URLs spin forever. The bytes come from the Redis cache,
            # so re-attaching on every edit costs no re-render; attachments=[]
            # on edit drops the previous upload so files don't accumulate.
            board_file, image_ref = None, None
            if board_img:
                import io
                import interactions

                filename = f"event-board-{event.id}.png"
                board_file = interactions.File(io.BytesIO(board_img),
                                               file_name=filename)
                image_ref = f"attachment://{filename}"
            components = build_components(spec, image_ref=image_ref)

            message = None
            if row.message_id:
                try:
                    message = await channel.fetch_message(message_id=row.message_id)
                except Exception:
                    message = None  # deleted / inaccessible — repost below
            if message is not None:
                if board_file is not None:
                    await message.edit(components=components,
                                       files=board_file, attachments=[])
                else:
                    # Render failed/none: also clear any stale attachment so a
                    # gallery-less body doesn't ride with an orphaned file.
                    await message.edit(components=components, attachments=[])
            else:
                if board_file is not None:
                    message = await channel.send(components=components,
                                                 files=board_file)
                else:
                    message = await channel.send(components=components)
                row.message_id = str(message.id)
            row.message_updated_at = datetime.now()
            session.commit()
            wrote = True
        return wrote
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
