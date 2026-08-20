"""Clan Log on Discord — the standing board message and the ``/clan-log`` card.

Two surfaces over one artifact:

* **The standing message.** A clan that turns ``clan_log_enabled`` on gets a
  single bot-owned message in ``clan_log_channel_id`` which is *edited* as
  members pull things — the shape the suggestion pointed at (Log Chasers keep a
  hand-maintained checklist in a ``#2026-hunt`` channel; this is that, generated).
* **``/clan-log``.** The same card on demand, for clans who would rather ask
  than dedicate a channel.

The refresher only touches Discord when the picture actually changed. The board
image is cached in Redis against a state hash (:mod:`services.clan_log_image`),
and the last hash posted is kept in Redis too, so a quiet clan costs one
in-memory comparison per cycle rather than an edit.

Both surfaces attach the PNG as a file and reference it as ``attachment://`` —
Components-V2 media galleries render attachments reliably where external URLs
spin forever, the same reason ``services/event_board`` does it.
"""
from __future__ import annotations

import asyncio
import io
from typing import Optional

from db.app_logger import AppLogger

app_logger = AppLogger()

# The refresher's per-cycle budget. A screenshot is ~1-2s of a headless
# chromium, so this bounds how much of a cycle the feature can consume no
# matter how many clans enable it; the rest are picked up next cycle.
MAX_RENDERS_PER_CYCLE = 6

_POSTED_HASH_KEY = "clan_log:{group_id}:posted_hash"
_POSTED_HASH_TTL = 30 * 24 * 3600


def board_url(group_id: int) -> str:
    return f"https://www.droptracker.io/groups/{int(group_id)}/log"


def build_message_text(payload: dict) -> str:
    """The one-line body under the card.

    Kept short on purpose: everything worth reading is in the image, and a wall
    of text under a picture is what makes a standing message feel like spam.
    """
    summary = payload.get("summary") or {}
    obtained = summary.get("obtained", 0)
    total = summary.get("total", 0)
    pct = summary.get("pct", 0)
    name = payload.get("group_name") or "The clan"
    period = payload.get("period", "all")
    window = "all time" if period == "all" else period
    return (
        f"**{name} — Clan Log** ({window})\n"
        f"{obtained} of {total} tracked uniques obtained ({pct}%). "
        f"Full board: {board_url(payload.get('group_id') or 0)}"
    )


async def build_card_payload(session, group_id: int, period: str = "all"):
    """``(text, file, state_hash)`` for a Clan Log post, or ``(None, None, None)``.

    The file is ``None`` when rendering is unconfigured or failed — callers
    still post the text, which carries the numbers and the link.
    """
    from services.clan_log import load_board
    from services.clan_log_image import clan_log_image_with_hash

    payload = load_board(session, group_id, period)
    if not payload:
        return None, None, None

    png, state_hash, _rendered = await clan_log_image_with_hash(
        session, group_id, period
    )
    file = None
    if png:
        import interactions

        file = interactions.File(
            io.BytesIO(png), file_name=f"clan-log-{group_id}-{period}.png"
        )
    return build_message_text(payload), file, state_hash


def _scan_standing_candidates() -> Optional[tuple]:
    """Which clans' standing boards actually moved. MUST run off the event loop.

    Returns ``(checked, skipped, config, candidates)`` where candidates is
    ``[(group_id, channel_id, current_hash), ...]``.

    The module docstring's claim that a quiet clan "costs one in-memory
    comparison" is only true of the *comparison* — reaching it needs a full
    ``load_board`` DB read per clan, and the skip path never awaits, so with
    ~63 enabled clans this ran to completion without once yielding. On
    2026-08-20 that blocked the loop past Discord's 41.25s heartbeat deadline
    and killed the gateway seconds after clan_log_refresh's ledger half had
    just been moved to a thread for exactly the same reason.

    Owns its session so it stays confined to the worker thread.
    """
    from db.models import GroupConfiguration
    from db.models.base import Session
    from services.clan_log import load_board
    from services.clan_log_image import board_state_hash
    from utils import group_config
    from utils.redis import redis_client

    session = Session()
    try:
        rows = (
            session.query(GroupConfiguration)
            .filter(
                GroupConfiguration.config_key == "clan_log_enabled",
                GroupConfiguration.config_value.in_(["1", "true", "True", "yes", "on"]),
            )
            .all()
        )
        group_ids = [int(r.group_id) for r in rows]
        if not group_ids:
            return None

        config = group_config.get_bulk(
            session, group_ids, ["clan_log_channel_id", "clan_log_message_id"]
        )

        checked = 0
        skipped = 0
        candidates = []
        for group_id in group_ids:
            checked += 1
            channel_id = (config.get((group_id, "clan_log_channel_id")) or "").strip()
            if not channel_id or channel_id in {"0", "None"}:
                continue

            posted_key = _POSTED_HASH_KEY.format(group_id=group_id)
            try:
                payload = load_board(session, group_id, "all")
                if not payload:
                    continue
                current_hash = board_state_hash(payload)
                if redis_client.get(posted_key) == current_hash:
                    skipped += 1
                    continue
            except Exception:
                current_hash = None
            candidates.append((group_id, channel_id, current_hash))
        return checked, skipped, config, candidates
    finally:
        session.close()


async def refresh_standing_messages(bot, session, *, limit: int = MAX_RENDERS_PER_CYCLE
                                    ) -> dict:
    """Post/edit the standing board message for every clan that enabled it.

    Skips a clan whose board has not changed since its last post, which is the
    common case. Change detection is blocking and proportional to the number of
    enabled clans, so it happens in a worker thread
    (:func:`_scan_standing_candidates`); only the Discord writes — bounded at
    ``limit`` per cycle — run on the event loop.
    """
    from utils.redis import redis_client

    stats = {"checked": 0, "posted": 0, "edited": 0, "skipped": 0, "failed": 0}

    scan = await asyncio.to_thread(_scan_standing_candidates)
    if not scan:
        return stats
    stats["checked"], stats["skipped"], config, candidates = scan

    # Budget spent past this point; the rest keep their stale message until the
    # next cycle.
    for group_id, channel_id, current_hash in candidates[:limit]:
        posted_key = _POSTED_HASH_KEY.format(group_id=group_id)
        try:
            text, file, state_hash = await build_card_payload(session, group_id, "all")
            if text is None:
                continue

            channel = await bot.fetch_channel(channel_id=channel_id)
            if not channel:
                continue

            message_id = (config.get((group_id, "clan_log_message_id")) or "").strip()
            message = None
            if message_id and message_id not in {"0", "None", ""}:
                try:
                    message = await channel.fetch_message(message_id=message_id)
                except Exception:
                    message = None  # deleted / inaccessible — repost below

            if message is not None:
                if file is not None:
                    await message.edit(content=text, files=file, attachments=[])
                else:
                    await message.edit(content=text)
                stats["edited"] += 1
            else:
                message = (
                    await channel.send(content=text, files=file)
                    if file is not None
                    else await channel.send(content=text)
                )
                _save_message_id(session, group_id, str(message.id))
                stats["posted"] += 1

            if state_hash:
                redis_client.setex(posted_key, _POSTED_HASH_TTL, state_hash)
        except Exception as e:
            stats["failed"] += 1
            app_logger.log(
                log_type="error",
                data=f"Clan Log standing message failed for group {group_id}: {e}",
                app_name="clan_log_discord",
                description="refresh_standing_messages")
    return stats


def _save_message_id(session, group_id: int, message_id: str) -> None:
    from db.models import GroupConfiguration
    from utils import group_config

    row = (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == "clan_log_message_id",
        )
        .first()
    )
    if row is None:
        row = GroupConfiguration(
            group_id=group_id, config_key="clan_log_message_id",
            config_value=message_id,
        )
        session.add(row)
    else:
        row.config_value = message_id
    session.commit()
    group_config.invalidate(group_id, "clan_log_message_id")
