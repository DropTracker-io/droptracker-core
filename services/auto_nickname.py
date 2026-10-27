"""Set a member's server nickname to their claimed RSN.

Opt-in per group (``auto_set_member_nicknames``, off by default). Two jobs:

* **After a claim.** Every successful claim (``/claim-rsn``, the website, the
  Activity, plugin registration: all go through ``db.player_claims``) queues
  a ``rsn_nickname`` outbox row. The core bot then walks every opted-in
  group's Discord server the claimer is a member of and renames them to the
  claimed RSN. Being in the server is what counts, so a member whose account
  is not on the clan's WOM roster yet is still renamed.
* **When a group turns it on.** Flipping the setting off->on (website PATCH or
  the Discord settings panel) queues a ``nickname_backfill`` row, and the bot
  renames everyone in the group who has already claimed an account. Members
  with several accounts in the group keep their nickname when it already is
  one of them; otherwise they get the oldest-tracked one, usually the main.

The backfill works from the group's own membership, not the server's member
list: the core app does not hold the privileged Server Members intent (it is
in 400+ servers), so it cannot list members, only look one up. Someone in the
server whose claimed account is not in the group is not found by the
backfill; their next ``/claim-rsn`` renames them.

Why the outbox and not a direct call: ``web_api`` never opens a Discord
connection, and one queue keeps every surface identical. The drain only
*starts* each job (:func:`handle_outbox_row`) and moves on; renames run as
background tasks, since a backfill can take minutes of rate-limited calls and
must not hold up the other queued messages. A bot restart mid-backfill drops
the rest of it; re-saving the setting off then on runs it again.

Best effort by contract: Discord refuses to rename the server owner, or anyone
whose highest role sits at or above the bot's, and the bot may lack Manage
Nicknames. Each rename fails on its own and is logged.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from db.app_logger import AppLogger
from utils import group_config as gc

app_logger = AppLogger()

# Discord's nickname limit. OSRS names are at most 12, so this only guards
# against a bad row.
MAX_NICKNAME_LENGTH = 32

# The global group every unaffiliated player lands in; never a clan server.
GLOBAL_GROUP_ID = 2

# Outbox kinds this module owns (discord_outbox.drain_once hands them here).
KIND_CLAIM = "rsn_nickname"        # channel_id = Discord user id, content = RSN
KIND_BACKFILL = "nickname_backfill"  # channel_id = guild id, ref_id = group id
OUTBOX_KINDS = (KIND_CLAIM, KIND_BACKFILL)

# Pause between members during a backfill. interactions already honours
# Discord's rate limits; this keeps one big clan from hogging the bucket.
BACKFILL_PAUSE_SECONDS = 0.25

REASON = "DropTracker: set to claimed RSN"

# Strong references to in-flight tasks: the event loop only holds weak ones,
# so an unreferenced task can be collected before it finishes.
_pending: set = set()
# Guilds with a backfill running, so a double toggle doesn't run two at once.
_backfilling: set = set()


def nickname_for(rsn: Optional[str]) -> Optional[str]:
    """The nickname to apply for ``rsn``, or ``None`` when there is nothing
    sensible to set."""
    name = (rsn or "").strip()
    if not name:
        return None
    return name[:MAX_NICKNAME_LENGTH]


def _parse_guild_id(raw) -> Optional[int]:
    try:
        gid = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return gid if gid > 0 else None


# --------------------------------------------------------------------------- #
# Enqueue side (any process; caller supplies the session)
# --------------------------------------------------------------------------- #
def turned_on(before, after) -> bool:
    """True only for an off->on flip. ``before`` is the stored string, or
    None when there was no row (which reads as the default: off)."""
    return gc.is_truthy(after) and not gc.is_truthy(before)


def enqueue_claim_rename(session, discord_id, rsn, *, commit: bool = True) -> bool:
    """Queue the after-claim rename. The bot decides which servers apply."""
    nickname = nickname_for(rsn)
    if not nickname or not discord_id:
        return False
    from services import discord_outbox

    discord_outbox.enqueue(session, channel_id=str(discord_id), content=nickname,
                           kind=KIND_CLAIM, ref_type="player_claim", commit=commit)
    return True


def enqueue_backfill_if_turned_on(session, group_id: int, before, after, *,
                                  actor_user_id=None) -> bool:
    """Queue a backfill when this write turns the setting on. Does not
    commit: call it inside the transaction that saves the setting, so the
    job exists exactly when the change does."""
    if not turned_on(before, after) or int(group_id) == GLOBAL_GROUP_ID:
        return False
    from db.models import Group
    from services import discord_outbox

    row = session.query(Group.guild_id).filter(Group.group_id == group_id).first()
    guild_id = _parse_guild_id(row[0]) if row else None
    if guild_id is None:
        return False
    discord_outbox.enqueue(session, channel_id=str(guild_id), kind=KIND_BACKFILL,
                           ref_type="group", ref_id=int(group_id),
                           actor_user_id=actor_user_id, commit=False)
    return True


# --------------------------------------------------------------------------- #
# Pure planning
# --------------------------------------------------------------------------- #
def select_target_guilds(rows) -> List[Tuple[int, int]]:
    """``(group_id, guild_id, config_value)`` rows → the servers to visit.
    Truthiness is ``gc.is_truthy``, not SQL, so every stored form ("true",
    "1", " True ") reads the way it does everywhere else. One guild is
    returned once even if two groups share it."""
    targets: List[Tuple[int, int]] = []
    seen = set()
    for group_id, guild_id, value in rows:
        if not gc.is_truthy(value):
            continue
        gid = _parse_guild_id(guild_id)
        if gid is None or gid in seen:
            continue
        seen.add(gid)
        targets.append((group_id, gid))
    return targets


def plan_backfill(rows: Iterable[Tuple[object, object]]) -> List[Tuple[int, List[str]]]:
    """``(discord_id, player_name)`` rows, oldest player first → one entry
    per Discord user with every usable name they hold in the group, in that
    order. The first name is the one a rename uses."""
    names: Dict[int, List[str]] = {}
    for discord_id, player_name in rows:
        try:
            uid = int(str(discord_id).strip())
        except (TypeError, ValueError):
            continue
        nickname = nickname_for(player_name)
        if not nickname:
            continue
        bucket = names.setdefault(uid, [])
        if nickname not in bucket:
            bucket.append(nickname)
    return list(names.items())


# --------------------------------------------------------------------------- #
# DB reads (worker thread, own session — the services/news_optin shape)
# --------------------------------------------------------------------------- #
def _load_target_guilds() -> List[Tuple[int, int]]:
    """``(group_id, guild_id)`` for every group with the option on."""
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


def _load_backfill(group_id: int, guild_id: int) -> Optional[List[Tuple[int, List[str]]]]:
    """The group's claimed members, or None when the job is stale: the
    setting was switched off again, or the group moved server, before the
    bot got to it. Read straight from the table, not the 30s config cache,
    because the write happened in another process."""
    from db.models import Group, GroupConfiguration, Player, User
    from db.models.associations import user_group_association as uga
    from db.models.base import Session

    session = Session()
    try:
        value = (
            session.query(GroupConfiguration.config_value)
            .filter(GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == gc.AUTO_SET_MEMBER_NICKNAMES)
            .order_by(GroupConfiguration.id)
            .first()
        )
        group = session.query(Group.guild_id).filter(Group.group_id == group_id).first()
        if not value or not gc.is_truthy(value[0]):
            return None
        if not group or _parse_guild_id(group[0]) != int(guild_id):
            return None
        rows = (
            session.query(User.discord_id, Player.player_name)
            .join(Player, Player.user_id == User.user_id)
            .join(uga, uga.c.player_id == Player.player_id)
            .filter(uga.c.group_id == group_id)
            .order_by(Player.player_id)
            .all()
        )
    finally:
        session.rollback()
        session.close()
    return plan_backfill(rows)


# --------------------------------------------------------------------------- #
# Discord side
# --------------------------------------------------------------------------- #
async def _rename_member(guild, discord_id: int, nickname: str,
                         keep: Sequence[str] = ()) -> str:
    """Rename one member of ``guild``. ``keep`` lists nicknames that already
    count as right. Returns ``renamed`` | ``unchanged`` | ``not_member`` |
    ``owner``; Discord errors propagate."""
    member = await guild.fetch_member(discord_id)
    if member is None:
        return "not_member"
    if guild.is_owner(discord_id):
        # Discord rejects every rename of the owner, whatever our permissions.
        return "owner"
    current = member.nick or ""
    if current == nickname or (current and current in keep):
        return "unchanged"
    await member.edit_nickname(nickname, reason=REASON)
    return "renamed"


async def _rename_in_guild(bot, guild_id: int, discord_id: int, nickname: str) -> str:
    """:func:`_rename_member` by guild id; ``no_guild`` when the bot is not
    in it any more."""
    guild = await bot.fetch_guild(guild_id)
    if guild is None:
        return "no_guild"
    return await _rename_member(guild, discord_id, nickname)


def _warn(message: str) -> None:
    app_logger.log(log_type="warning", data=f"auto nickname: {message}",
                   app_name="core", description="auto_nickname")


async def apply_rsn_nickname(bot, discord_id, rsn) -> dict:
    """Rename ``discord_id`` to ``rsn`` in every opted-in group's server they
    are in. Never raises; returns outcome counts."""
    counts: dict = {}
    nickname = nickname_for(rsn)
    if not nickname:
        return counts
    try:
        targets = await asyncio.to_thread(_load_target_guilds)
    except Exception as e:  # noqa: BLE001
        _warn(f"target lookup failed: {e}")
        return counts

    for group_id, guild_id in targets:
        try:
            outcome = await _rename_in_guild(bot, guild_id, int(discord_id), nickname)
        except Exception as e:  # noqa: BLE001 - Forbidden / hierarchy / HTTP
            outcome = "failed"
            _warn(f"couldn't rename {discord_id} to {nickname!r} in guild {guild_id} "
                  f"(group {group_id}): {e}")
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


async def backfill_group(bot, group_id: int, guild_id: int) -> dict:
    """Rename every claimed member of ``group_id`` in ``guild_id``. Never
    raises; returns outcome counts (``skipped`` when the job was stale or one
    is already running for this server)."""
    guild_id = int(guild_id)
    if guild_id in _backfilling:
        return {"skipped": 1}
    _backfilling.add(guild_id)
    counts: dict = {}
    try:
        try:
            plan = await asyncio.to_thread(_load_backfill, int(group_id), guild_id)
        except Exception as e:  # noqa: BLE001
            _warn(f"backfill lookup failed for group {group_id}: {e}")
            return counts
        if plan is None:
            return {"skipped": 1}
        try:
            guild = await bot.fetch_guild(guild_id)
        except Exception as e:  # noqa: BLE001
            _warn(f"backfill: guild {guild_id} unavailable: {e}")
            guild = None
        if guild is None:
            return {"no_guild": 1}

        for discord_id, names in plan:
            try:
                outcome = await _rename_member(guild, discord_id, names[0], keep=names)
            except Exception as e:  # noqa: BLE001
                counts["failed"] = counts.get("failed", 0) + 1
                if counts["failed"] <= 3:  # one bad permission fails them all
                    _warn(f"backfill: couldn't rename {discord_id} in guild {guild_id} "
                          f"(group {group_id}): {e}")
            else:
                counts[outcome] = counts.get(outcome, 0) + 1
            if BACKFILL_PAUSE_SECONDS:
                await asyncio.sleep(BACKFILL_PAUSE_SECONDS)

        app_logger.log(log_type="info",
                       data=f"auto nickname: backfill group {group_id} guild {guild_id}: {counts}",
                       app_name="core", description="auto_nickname")
        return counts
    finally:
        _backfilling.discard(guild_id)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def handle_outbox_row(bot, row) -> None:
    """Start the job an outbox row describes and return at once (see the
    module docstring for why the drain must not wait on it)."""
    if row.kind == KIND_CLAIM:
        _spawn(apply_rsn_nickname(bot, row.channel_id, row.content))
    elif row.kind == KIND_BACKFILL:
        if not row.ref_id:
            raise ValueError("nickname_backfill row without a group id")
        _spawn(backfill_group(bot, int(row.ref_id), int(row.channel_id)))
    else:
        raise ValueError(f"not an auto-nickname kind: {row.kind}")
