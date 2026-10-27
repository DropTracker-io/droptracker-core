"""Set a member's server nickname to the RSN they just claimed.

Opt-in per group (``auto_set_member_nicknames``, off by default). After a
successful ``/claim-rsn`` the core bot walks every group that turned it on,
and in each of those groups' Discord servers where the claimer is a member,
renames them to the claimed RSN. Servers they are not in are skipped — being
in the server is the only membership that matters for a nickname, so a member
whose account is not on the clan's WOM roster yet is still renamed.

Best effort by contract: Discord refuses to rename the server owner, or anyone
whose highest role sits at or above the bot's, and the bot may lack Manage
Nicknames. Each server fails on its own and is logged; the claim itself has
already succeeded and nothing is reported back to the user.

Runs as a background task after the command has replied
(:func:`schedule_rsn_nickname`), so N opted-in servers cost the user no
latency. The DB read happens on a worker thread with its own session — the
same shape as ``services/news_optin._load_pending_grants``.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from db.app_logger import AppLogger
from utils import group_config as gc

app_logger = AppLogger()

# Discord's nickname limit. OSRS names are at most 12, so this only guards
# against a bad row.
MAX_NICKNAME_LENGTH = 32

# The global group every unaffiliated player lands in; never a clan server.
GLOBAL_GROUP_ID = 2

# Strong references to in-flight tasks: the event loop only holds weak ones,
# so an unreferenced task can be collected before it finishes.
_pending: set = set()


def nickname_for(rsn: Optional[str]) -> Optional[str]:
    """The nickname to apply for ``rsn``, or ``None`` when there is nothing
    sensible to set."""
    name = (rsn or "").strip()
    if not name:
        return None
    return name[:MAX_NICKNAME_LENGTH]


def _load_target_guilds() -> List[Tuple[int, int]]:
    """Thread-side: ``(group_id, guild_id)`` for every group with the option on
    and a usable guild id. One guild is returned once even if two groups share
    it."""
    from db.models import Group, GroupConfiguration
    from db.models.base import Session

    session = Session()
    try:
        rows = (
            session.query(Group.group_id, Group.guild_id, GroupConfiguration.config_value)
            .join(GroupConfiguration, GroupConfiguration.group_id == Group.group_id)
            .filter(GroupConfiguration.config_key == gc.AUTO_SET_MEMBER_NICKNAMES)
            .filter(Group.group_id != GLOBAL_GROUP_ID)
            .all()
        )
    finally:
        session.rollback()
        session.close()
    return select_target_guilds(rows)


def select_target_guilds(rows) -> List[Tuple[int, int]]:
    """Pure: ``(group_id, guild_id, config_value)`` rows → the servers to
    visit. Truthiness is ``gc.is_truthy``, not SQL, so every stored form
    ("true", "1", " True ") reads the way it does everywhere else."""
    targets: List[Tuple[int, int]] = []
    seen = set()
    for group_id, guild_id, value in rows:
        if not gc.is_truthy(value):
            continue
        try:
            gid = int(str(guild_id).strip())
        except (TypeError, ValueError):
            continue
        if gid <= 0 or gid in seen:
            continue
        seen.add(gid)
        targets.append((group_id, gid))
    return targets


async def _rename_in_guild(bot, guild_id: int, discord_id: int, nickname: str) -> str:
    """Rename one member in one server. Returns the outcome for the summary:
    ``renamed`` | ``unchanged`` | ``not_member`` | ``owner`` | ``no_guild`` |
    ``failed``."""
    guild = await bot.fetch_guild(guild_id)
    if guild is None:
        return "no_guild"
    member = await guild.fetch_member(discord_id)
    if member is None:
        return "not_member"
    if guild.is_owner(discord_id):
        # Discord rejects every rename of the owner, whatever our permissions.
        return "owner"
    if (member.nick or "") == nickname:
        return "unchanged"
    await member.edit_nickname(nickname, reason="DropTracker: set to claimed RSN")
    return "renamed"


async def apply_rsn_nickname(bot, discord_id, rsn) -> dict:
    """Rename ``discord_id`` to ``rsn`` in every opted-in group's server they
    are in. Never raises; returns outcome counts for the log."""
    counts: dict = {}
    nickname = nickname_for(rsn)
    if not nickname:
        return counts
    try:
        targets = await asyncio.to_thread(_load_target_guilds)
    except Exception as e:  # noqa: BLE001
        app_logger.log(log_type="warning", data=f"auto nickname: target lookup failed: {e}",
                       app_name="core", description="auto_nickname")
        return counts

    for group_id, guild_id in targets:
        try:
            outcome = await _rename_in_guild(bot, guild_id, int(discord_id), nickname)
        except Exception as e:  # noqa: BLE001 - Forbidden / hierarchy / HTTP
            outcome = "failed"
            app_logger.log(
                log_type="warning",
                data=f"auto nickname: couldn't rename {discord_id} to {nickname!r} "
                     f"in guild {guild_id} (group {group_id}): {e}",
                app_name="core",
                description="auto_nickname",
            )
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def schedule_rsn_nickname(bot, discord_id, rsn) -> None:
    """Fire-and-forget :func:`apply_rsn_nickname` from a command handler."""
    task = asyncio.create_task(apply_rsn_nickname(bot, discord_id, rsn))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
