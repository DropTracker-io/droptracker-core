"""Posts PvP lootboards to Discord (core bot).

The PvP counterpart of ``lootboard_updates`` in ``bots/main.py``, with the same
rule: post a group's board whenever its image is newer than the one last
posted (``lootboard/schedule.py``, kind ``pvp``). The board itself is drawn by
the lootboards service (``lootboard/pvp_board.py``).

Settings read, all ``group_configurations`` rows: ``pvp_lootboard_channel_id``
(no channel, no board), ``pvp_lootboard_message_id`` (the message edited in
place, written here) and ``pvp_repost_lootboard`` (delete and repost instead).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Tuple

import interactions

from lootboard import schedule

CHANNEL_KEY = "pvp_lootboard_channel_id"
MESSAGE_KEY = "pvp_lootboard_message_id"
REPOST_KEY = "pvp_repost_lootboard"
_KEYS = (CHANNEL_KEY, MESSAGE_KEY, REPOST_KEY)

EMBED_COLOR = "#D4A017"
FILE_NAME = "pvp_lootboard.png"

_policy_cache: dict = {"at": 0.0, "by_group": {}}


def _unset(value) -> bool:
    return value in (None, "", "0", 0)


def targets() -> Dict[int, dict]:
    """``{group_id: {"channel", "message", "repost"}}`` for groups with a PvP
    board channel. Raw ORM, not the cached config layer: this module rewrites
    the message id, and a stale cached one would make it repost."""
    from db.models import GroupConfiguration, Session

    session = Session()
    try:
        rows = (
            session.query(GroupConfiguration.group_id, GroupConfiguration.config_key,
                          GroupConfiguration.config_value)
            .filter(GroupConfiguration.config_key.in_(_KEYS))
            .order_by(GroupConfiguration.id)
            .all()
        )
    finally:
        session.close()
    config: Dict[int, dict] = {}
    for group_id, key, value in rows:
        config.setdefault(int(group_id), {}).setdefault(key, value)
    out = {}
    for group_id, keys in config.items():
        if _unset(keys.get(CHANNEL_KEY)):
            continue
        out[group_id] = {
            "channel": str(keys[CHANNEL_KEY]),
            "message": keys.get(MESSAGE_KEY),
            "repost": str(keys.get(REPOST_KEY) or "").lower() in ("1", "true", "yes", "on"),
        }
    return out


def due() -> List[Tuple[int, dict, float, "schedule.LootboardPolicy"]]:
    """Boards whose image is newer than the one last posted. Runs off the loop."""
    from db.models import Session

    found = targets()
    if not found:
        return []
    now = time.time()
    if now - _policy_cache["at"] > 60:
        with Session() as s:
            _policy_cache["by_group"] = schedule.load_policies(s, found.keys())
        _policy_cache["at"] = now
    posted = schedule.posted_mtimes(found.keys(), kind=schedule.PVP)
    out = []
    for group_id, target in found.items():
        drawn_at = schedule.board_mtime(group_id, schedule.PVP)
        if schedule.needs_post(drawn_at, posted.get(group_id, 0.0)):
            policy = _policy_cache["by_group"].get(group_id, schedule.DEFAULT_POLICY)
            out.append((group_id, target, drawn_at, policy))
    out.sort(key=lambda d: (not d[3].instant, d[3].refresh_minutes, d[2]))
    return out


def _save_message_id(group_id: int, message_id) -> None:
    from db.models import GroupConfiguration, Session
    from utils import group_config as gc

    session = Session()
    try:
        row = (session.query(GroupConfiguration)
               .filter(GroupConfiguration.group_id == group_id,
                       GroupConfiguration.config_key == MESSAGE_KEY)
               .order_by(GroupConfiguration.id).first())
        if row is None:
            session.add(GroupConfiguration(group_id=group_id, config_key=MESSAGE_KEY,
                                           config_value=str(message_id)))
        else:
            row.config_value = str(message_id)
        session.commit()
    finally:
        session.close()
    gc.invalidate(group_id, MESSAGE_KEY)


def build_embed(group_id: int, policy, drawn_at: float) -> interactions.Embed:
    next_at = int(schedule.next_refresh_at(policy, drawn_at, time.time()))
    embed = interactions.Embed(
        title="PvP Loot Leaderboard",
        description=(
            "Loot taken from player kills this month. Kills in LMS and other PvP "
            f"minigames don't count.\nNext refresh <t:{next_at}:R>"
        ),
        color=EMBED_COLOR,
    )
    embed.set_image(url=f"attachment://{FILE_NAME}")
    from services import notification_text

    # The group's lootboard footer choice covers both boards.
    notification_text.apply_footer(embed, group_id, "lb")
    return embed


async def post_one(bot, group_id: int, target: dict, drawn_at: float, policy) -> bool:
    from utils.discord_threads import PROBLEM_STATES, ensure_thread_open

    path = schedule.board_path(group_id, schedule.PVP)
    if not os.path.exists(path):
        return False
    channel = await bot.fetch_channel(channel_id=target["channel"])
    if not channel:
        return False
    if await ensure_thread_open(bot.http, channel, group_id=group_id,
                                feature="pvp_lootboard") in PROBLEM_STATES:
        return False

    embed = build_embed(group_id, policy, drawn_at)
    image = interactions.File(path, file_name=FILE_NAME)
    message = None
    if not _unset(target["message"]):
        try:
            message = await channel.fetch_message(message_id=target["message"])
        except Exception:
            message = None
    if message is not None and target["repost"]:
        try:
            await message.delete()
        except Exception as e:
            print(f"[PvpBoard] Couldn't delete the old PvP board for group {group_id}: {e}")
        message = None
    if message is None:
        message = await channel.send(embed=embed, files=image)
        await asyncio.to_thread(_save_message_id, group_id, message.id)
        return True
    message.attachments.clear()
    await message.edit(content="", embed=embed, files=image)
    return True


async def post_due(bot) -> int:
    """Post every PvP board that has a newer image. Returns how many posted."""
    boards = await asyncio.to_thread(due)
    posted = 0
    for group_id, target, drawn_at, policy in boards:
        # Recorded first, whatever happens: a board that cannot be posted is
        # retried with its next image, not every tick.
        try:
            schedule.record_posted(group_id, drawn_at, kind=schedule.PVP)
        except Exception:
            pass
        try:
            if await post_one(bot, group_id, target, drawn_at, policy):
                posted += 1
        except Exception as e:
            print(f"[PvpBoard] Couldn't post the PvP board for group {group_id}: {e}")
        await asyncio.sleep(1)
    return posted
