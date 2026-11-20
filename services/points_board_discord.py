"""Clan points board on Discord (suggestion #256): one standing message.

A clan that picks ``points_board_channel_id`` gets a single bot-owned message
there listing its top members (``points_board_size``, 10 by default) for the
window in ``points_board_period``, with a button to the full leaderboard on the
site. The core bot's ``points_board_updates`` task rebuilds every board on a
short interval and edits the message only when its content changed, the same
shape as the bank board (``services/group_bank_discord``).

``season`` (the default) is whichever of the clan's leaderboard seasons is
running now, and falls back to all-time when none is, so the board rolls over
on its own when a new season starts. Totals come from
``db/point_standings`` like every other points surface: current members only,
RSNs combined per Discord user when the clan has that on, hidden players left
out with their rank kept.

Stdlib-only at import time (DB, Redis and Discord are imported lazily), so the
unit-test conftest can load the real module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Optional, Sequence

CHANNEL_KEY = "points_board_channel_id"
SIZE_KEY = "points_board_size"
PERIOD_KEY = "points_board_period"
# "<channel_id>:<message_id>" of the board message. Written by the bot only.
MESSAGE_KEY = "points_board_message_id"

DEFAULT_SIZE = 10
MIN_SIZE = 3
MAX_SIZE = 25
PERIODS = ("season", "all", "month", "week")
DEFAULT_PERIOD = "season"

#: Boards edited per bot tick; the rest wait for the next one.
MAX_BOARDS_PER_CYCLE = 10

_POSTED_HASH_KEY = "points:board:{group_id}:posted_hash"
_POSTED_HASH_TTL = 30 * 24 * 3600

_COLOR = 0xD4AF37
_DESCRIPTION_LIMIT = 4000


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _unset(value) -> bool:
    return value is None or str(value).strip() in ("", "0", "None", "null")


def channel_id_from(value) -> Optional[str]:
    if _unset(value):
        return None
    raw = str(value).strip()
    return raw if raw.isdigit() else None


def board_size(value) -> int:
    try:
        size = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_SIZE
    return max(MIN_SIZE, min(MAX_SIZE, size))


def board_period(value) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in PERIODS else DEFAULT_PERIOD


def current_season(seasons: Sequence[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """The season running at ``now``: started, and not yet ended (an open end
    counts as running). The latest start wins if a clan's seasons overlap."""
    now = now or datetime.now()
    running = [
        s for s in seasons
        if s.get("start_at") is not None and s["start_at"] <= now
        and (s.get("end_at") is None or now < s["end_at"])
    ]
    if not running:
        return None
    return max(running, key=lambda s: s["start_at"])


def parse_message_ref(raw) -> tuple:
    """``(channel_id, message_id)`` from the stored board ref, or ``(None, None)``."""
    if _unset(raw):
        return None, None
    channel_id, sep, message_id = str(raw).strip().partition(":")
    if not sep or not channel_id.isdigit() or not message_id.isdigit():
        return None, None
    return channel_id, message_id


def board_embed(*, group_name: str, label: str, standings: Sequence, size: int,
                combined: bool, url: str) -> dict:
    """The board as an embed dict. No timestamps in it: the hash of this dict
    is the change detector, so it must only move when the board does."""
    from db.point_standings import visible_standings
    from services.points_cards import standing_line

    shown = visible_standings(standings)[:size]
    if shown:
        body = "\n".join(standing_line(s) for s in shown)
    else:
        body = "No points have been earned in this period yet."
    header = f"**{label}** · top {size}"
    if combined:
        header += "\n-# RSNs claimed by the same Discord user are counted together."
    description = f"{header}\n\n{body}"
    if len(description) > _DESCRIPTION_LIMIT:
        description = description[: _DESCRIPTION_LIMIT - 1] + "…"
    return {
        "title": f"{group_name} — Points leaderboard",
        "url": url,
        "description": description,
        "color": _COLOR,
        "footer": {"text": "Updates automatically · full standings on droptracker.io"},
    }


def board_hash(channel_id: str, embed: dict) -> str:
    """Covers the channel, so moving the board reads as a change."""
    raw = json.dumps({"c": str(channel_id), "e": embed}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# DB side (blocking; run off the event loop)
# --------------------------------------------------------------------------- #
def _board_groups(session) -> dict:
    """``{group_id: channel_id}`` for groups with a board channel set."""
    from db.models import GroupConfiguration

    rows = (
        session.query(GroupConfiguration.group_id, GroupConfiguration.config_value)
        .filter(GroupConfiguration.config_key == CHANNEL_KEY)
        .order_by(GroupConfiguration.id.asc())
        .all()
    )
    out = {}
    for gid, value in rows:
        channel_id = channel_id_from(value)
        # Lowest id wins when a key is duplicated (group config is not unique).
        if channel_id and int(gid) not in out:
            out[int(gid)] = channel_id
    return out


def build_board(session, group_id: int, channel_id: str, *, size_raw=None,
                period_raw=None, now: Optional[datetime] = None) -> Optional[dict]:
    """``{"embed", "hash"}`` for one group's board, or None (no such group, or
    its points system is off)."""
    from db.models import Group
    from db.point_standings import (
        combine_enabled, load_seasons, load_standings, points_enabled, resolve_period,
    )
    from services.points_cards import leaderboard_url, period_label

    group_name = session.query(Group.group_name).filter(Group.group_id == group_id).scalar()
    if group_name is None or not points_enabled(session, group_id):
        return None

    size = board_size(size_raw)
    period = board_period(period_raw)
    seasons = load_seasons(session, group_id)
    if period == "season":
        season = current_season(seasons, now)
        if season is not None:
            token, start, end = f"season:{season['id']}", season["start_at"], season["end_at"]
        else:
            token, start, end = resolve_period("all", now)
    else:
        token, start, end = resolve_period(period, now)

    combine = combine_enabled(session, group_id)
    standings = load_standings(session, group_id, start=start, end=end, combine=combine)
    embed = board_embed(
        group_name=group_name, label=period_label(token, seasons), standings=standings,
        size=size, combined=combine, url=leaderboard_url(group_id, token),
    )
    return {"embed": embed, "hash": board_hash(channel_id, embed)}


def _scan(now: Optional[datetime] = None) -> list:
    """Boards that changed: ``[(group_id, channel_id, embed, hash, stored_ref)]``.
    Blocking; owns its session."""
    from db.models.base import Session
    from utils import group_config as gc
    from utils.redis import redis_client

    session = Session()
    try:
        channels = _board_groups(session)
        if not channels:
            return []
        conf = gc.get_bulk(session, list(channels), [MESSAGE_KEY, SIZE_KEY, PERIOD_KEY])
        out = []
        for group_id, channel_id in channels.items():
            try:
                board = build_board(
                    session, group_id, channel_id,
                    size_raw=conf.get((group_id, SIZE_KEY)),
                    period_raw=conf.get((group_id, PERIOD_KEY)),
                    now=now,
                )
            except Exception as exc:
                session.rollback()
                print(f"[points_board] build failed for group {group_id}: {exc}")
                continue
            if board is None:
                continue
            try:
                posted = redis_client.get(_POSTED_HASH_KEY.format(group_id=group_id))
            except Exception:
                posted = None
            stored = conf.get((group_id, MESSAGE_KEY))
            if posted == board["hash"] and parse_message_ref(stored)[0] == channel_id:
                continue
            out.append((group_id, channel_id, board["embed"], board["hash"], stored))
        # A group with no board on Discord yet goes first.
        out.sort(key=lambda c: 1 if parse_message_ref(c[4])[0] else 0)
        return out
    finally:
        session.close()


def _save_message_ref(group_id: int, ref: Optional[str]) -> None:
    from db.models import GroupConfiguration
    from db.models.base import Session
    from utils import group_config

    session = Session()
    try:
        row = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == MESSAGE_KEY)
            .order_by(GroupConfiguration.id.asc())
            .first()
        )
        if row is None:
            if ref is None:
                return
            session.add(GroupConfiguration(group_id=group_id, config_key=MESSAGE_KEY,
                                           config_value=ref))
        else:
            row.config_value = ref or ""
        session.commit()
    finally:
        session.close()
    group_config.invalidate(group_id, MESSAGE_KEY)


# --------------------------------------------------------------------------- #
# Discord side
# --------------------------------------------------------------------------- #
def _components(url: str) -> list:
    from interactions import ActionRow, Button, ButtonStyle

    return [ActionRow(Button(style=ButtonStyle.URL, label="Full leaderboard", url=url))]


async def refresh_boards(bot, *, limit: int = MAX_BOARDS_PER_CYCLE) -> dict:
    """Post or edit the points board for every group whose board changed."""
    from interactions import Embed

    from utils.discord_threads import PROBLEM_STATES, ensure_thread_open
    from utils.redis import redis_client

    stats = {"candidates": 0, "posted": 0, "edited": 0, "failed": 0, "skipped": 0}
    candidates = await asyncio.to_thread(_scan)
    stats["candidates"] = len(candidates)

    for group_id, channel_id, embed_dict, state_hash, stored in candidates[:limit]:
        try:
            channel = await bot.fetch_channel(channel_id=channel_id)
            if not channel:
                stats["skipped"] += 1
                continue
            thread_state = await ensure_thread_open(
                bot.http, channel, group_id=group_id, feature="points_board",
            )
            if thread_state in PROBLEM_STATES:
                stats["skipped"] += 1
                continue
            embed = Embed.from_dict(embed_dict)
            components = _components(embed_dict["url"])
            old_channel_id, old_message_id = parse_message_ref(stored)
            message = None
            if old_message_id and old_channel_id == channel_id:
                try:
                    message = await channel.fetch_message(message_id=old_message_id)
                except Exception:
                    message = None  # deleted: post a fresh one
                if message is not None and int(message.author.id) != int(bot.user.id):
                    message = None
            if message is not None:
                await message.edit(embeds=[embed], components=components)
                stats["edited"] += 1
            else:
                message = await channel.send(embeds=[embed], components=components)
                await asyncio.to_thread(_save_message_ref, group_id, f"{channel_id}:{message.id}")
                stats["posted"] += 1
                if old_message_id and old_channel_id != channel_id:
                    # The board moved channels: tidy up the old one.
                    try:
                        old = await bot.fetch_channel(channel_id=old_channel_id)
                        old_msg = await old.fetch_message(message_id=old_message_id) if old else None
                        if old_msg is not None and int(old_msg.author.id) == int(bot.user.id):
                            await old_msg.delete()
                    except Exception:
                        pass
            redis_client.client.setex(
                _POSTED_HASH_KEY.format(group_id=group_id), _POSTED_HASH_TTL, state_hash,
            )
        except Exception as exc:
            stats["failed"] += 1
            print(f"[points_board] group {group_id} failed: {exc}")
    return stats
