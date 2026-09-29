"""Delivery of the event-wide lootboard to Discord.

``lootboard/event_boards.py`` renders the PNG in the ``droptracker-lootboards``
process; this pass (core bot, riding the 2-minute event-board sweep) posts it
as a SECOND message directly beneath the live standings board in each
leaderboard channel, and edits it in place from then on. Same doctrine as the
team-channel lootboards (services/event_team_discord_bot.py, whose helpers it
reuses): the image is an attachment, a torn read is never delivered, an
unchanged image never touches Discord, and a message that can't be reached is
never forked by a blind repost.

Delivery state lives in Redis, one hash per leaderboard channel row
(``events:lootpost:{channel_row_id}`` = message_id / state_hash / mtime), not
in a DB column: it is a cache of "what did we last send where", and losing it
costs one clean repost (the old message is deleted first when its id is known).
The key expires a month after the last write, i.e. after the event is long over.

Gated per channel row by ``message_config.leaderboard.lootboard`` (default
on) and ``live``. Turning it off deletes the message this code posted.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

KEY = "events:lootpost:{row_id}"
KEY_TTL = 30 * 24 * 3600
# Discord round trips per sweep, across every event (a cold rollout converges
# over a few sweeps rather than bursting).
WRITE_BUDGET = 5


def _redis():
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def _state(conn, row_id) -> dict:
    if conn is None:
        return {}
    try:
        raw = conn.hgetall(KEY.format(row_id=row_id)) or {}
    except Exception:
        return {}
    return {(k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}


def _save_state(conn, row_id, **fields) -> None:
    if conn is None:
        return
    key = KEY.format(row_id=row_id)
    try:
        conn.hset(key, mapping={k: str(v) for k, v in fields.items()})
        conn.expire(key, KEY_TTL)
    except Exception:
        pass


def _clear_state(conn, row_id) -> None:
    if conn is None:
        return
    try:
        conn.delete(KEY.format(row_id=row_id))
    except Exception:
        pass


def _below(loot_id, board_id) -> bool:
    """Our message sits under the board post (snowflakes are time-ordered).
    Unparseable ids answer True: this decides deletions, so it never guesses."""
    try:
        return int(loot_id) > int(board_id)
    except (TypeError, ValueError):
        return True


def _state_hash(png: bytes, title: str) -> str:
    digest = hashlib.sha256()
    digest.update(png or b"")
    digest.update(b"\x00")
    digest.update((title or "").encode("utf-8", "replace"))
    return digest.hexdigest()


def _payload(event, png: bytes):
    """``(components, file)``: a short heading and the board as an attachment."""
    import io

    import interactions

    from services.event_message_layouts import build_components, render_message_spec

    layout = {"blocks": [
        {"type": "text", "content": "## \U0001F4B0 Event loot: {event_name}"},
        {"type": "text",
         "content": "-# Every player's loot, KC and EHE (Efficient Hours towards "
                    "Event) for this event. Redrawn about once an hour."},
    ]}
    spec = render_message_spec(layout, {"event_name": (event.name or "Event").strip()},
                               deep_link=False)
    filename = f"event-loot-{event.id}.png"
    file = interactions.File(io.BytesIO(png), file_name=filename)
    return build_components(spec, image_ref=f"attachment://{filename}"), file


async def _retire(bot, conn, row, state) -> bool:
    """Delete the lootboard we posted in a channel that no longer wants one."""
    from services.event_team_discord_bot import _delete_bot_message

    msg_id = state.get("message_id")
    if not msg_id:
        return False
    try:
        channel = await bot.fetch_channel(int(row.channel_id))
        if channel is not None:
            await _delete_bot_message(channel, msg_id)
    finally:
        _clear_state(conn, row.id)
    return True


async def _refresh_row(bot, conn, event, row, path: str, title: str) -> bool:
    """Post/edit the lootboard in one leaderboard channel. Returns whether a
    Discord round trip was attempted (the caller's budget counts those)."""
    from services.event_team_discord_bot import (
        _EDIT_MISSING, _EDIT_UNAVAILABLE, _delete_bot_message,
        _edit_tracked_message, _read_png, _repost_tracked_message,
    )

    state = _state(conn, row.id)
    msg_id = state.get("message_id") or None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False  # not rendered (yet): normal, silent
    below = _below(msg_id, row.message_id)
    try:
        delivered = float(state.get("mtime") or 0)
    except ValueError:
        delivered = 0.0
    if msg_id and below and delivered and mtime <= delivered + 1.0:
        return False  # the overwhelmingly common tick: nothing new on disk

    png, png_mtime = _read_png(path)
    if not png:
        return False  # mid-write; the next sweep reads the finished file
    state_hash = _state_hash(png, title)
    if msg_id and below and state.get("state_hash") == state_hash:
        _save_state(conn, row.id, mtime=png_mtime)
        return False

    channel = await bot.fetch_channel(int(row.channel_id))
    if channel is None or not callable(getattr(channel, "send", None)):
        _save_state(conn, row.id, mtime=png_mtime)  # rotate, don't hammer
        return True

    components, file = _payload(event, png)
    outcome = _EDIT_MISSING
    if msg_id and below:
        _, outcome = await _edit_tracked_message(channel, msg_id, components, file)
        if outcome == _EDIT_UNAVAILABLE:
            return True  # can't tell if it still exists: retry, never fork
    elif msg_id:
        # The board post was re-created and now sits under ours: move ours.
        await _delete_bot_message(channel, msg_id)
        msg_id = None
    if outcome == _EDIT_MISSING:
        # Build the file again: a failed edit may have consumed the buffer.
        components, file = _payload(event, png)
        message = await _repost_tracked_message(channel, msg_id, components, file)
        msg_id = str(message.id)
    _save_state(conn, row.id, message_id=msg_id, state_hash=state_hash, mtime=png_mtime)
    return True


async def refresh_event_lootboards(bot, session, events) -> int:
    """One delivery pass over ``events`` (the board sweep's list). Returns the
    number of Discord round trips spent. Never raises."""
    try:
        from lootboard.event_boards import (
            board_group_id, event_board_path, event_is_public, feature_enabled,
        )
        from services.event_board import _board_rows
        from db.models import EventTeam
    except Exception:
        return 0
    conn = _redis()
    spent = 0
    for event in events:
        if spent >= WRITE_BUDGET:
            break
        try:
            rows = _board_rows(session, event)
            enabled = feature_enabled() and event_is_public(event)
            teams = (session.query(EventTeam)
                     .filter(EventTeam.event_id == event.id).all())
            path = event_board_path(board_group_id(event, teams), event.id)
            title = (event.name or "Event").strip()
            for row, config in rows:
                if spent >= WRITE_BUDGET:
                    break
                board = config["leaderboard"]
                wanted = enabled and board.get("live", True) and board.get("lootboard", True)
                if not row.channel_id:
                    continue
                if not wanted:
                    state = _state(conn, row.id)
                    if state.get("message_id") and await _retire(bot, conn, row, state):
                        spent += 1
                    continue
                if not row.message_id:
                    continue  # "beneath the board" needs the board post first
                if await _refresh_row(bot, conn, event, row, path, title):
                    spent += 1
        except Exception as exc:  # noqa: BLE001 — isolate per event
            spent += 1
            print(f"[event-loot] delivery failed (event {getattr(event, 'id', None)}): {exc}")
    return spent
