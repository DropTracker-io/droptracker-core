import datetime
import json
import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from collections import deque
from typing import List, Dict, Optional, Tuple

log = logging.getLogger(__name__)
from interactions import BaseComponent, Extension, listen
import interactions
from interactions import ComponentContext, Extension, ActionRow, Button, ButtonStyle, FileComponent, PartialEmoji, Permissions, SlashContext, UnfurledMediaItem, listen, slash_command
from interactions.api.events import Startup, Component, ComponentCompletion, ComponentError, ModalCompletion, ModalError, MessageCreate
from interactions.models import ContainerComponent, ThumbnailComponent, SeparatorComponent, UserSelectMenu, SlidingWindowSystem, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, MediaGalleryComponent, MediaGalleryItem, OverwriteType
from db.models import GroupConfiguration, GroupPersonalBestMessage, Guild, get_current_partition, session, Group, NpcList, User, Player, user_group_association, PersonalBestEntry
from sqlalchemy import select, func, text
from sqlalchemy.orm import aliased
from db.ops import get_formatted_name
from utils.format import convert_from_ms, format_number, get_npc_image_url
import asyncio
from utils.redis import redis_client

_MEDAL_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}
_DIRECTORY_BOSS_NAME = "_hof_directory"
_SEPULCHRE_CANONICAL = "Hallowed Sepulchre"
_SEPULCHRE_FLOOR_RE = re.compile(r"^Hallowed Sepulchre Floor \d+$")
_RAID_GROUPS = {
    "Chambers of Xeric": ["Chambers of Xeric", "Chambers of Xeric: Challenge Mode"],
    "Theatre of Blood": ["Theatre of Blood", "Theatre of Blood: Entry Mode", "Theatre of Blood: Hard Mode"],
    "Tombs of Amascut": ["Tombs of Amascut", "Tombs of Amascut: Entry Mode", "Tombs of Amascut: Expert Mode"],
    "Nightmare of Ashihama": ["Nightmare", "Phosani's Nightmare", "Nightmare of Ashihama"],
    "The Gauntlet": ["The Gauntlet", "The Corrupted Gauntlet"],
}
_RAID_VARIANT_TO_CANONICAL = {
    variant: canonical
    for canonical, variants in _RAID_GROUPS.items()
    for variant in variants
}

class HallOfFame(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        # Async job queue and workers for controlled updates
        self._hof_queue: asyncio.Queue["HOFJob"] = asyncio.Queue(maxsize=1000)
        self._pending_jobs: set[str] = set()
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        self._guild_limiters: Dict[int, "RateLimiter"] = {}
        self._global_limiter: RateLimiter = RateLimiter(max_calls=8, period_seconds=1.0)
        self._group_forbidden_until: Dict[int, float] = {}
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(3)]
        self._stats_updates = 0
        self._stats_cleanups = 0
        self._stats_skipped_hash = 0
        log.warning("HOF: %d workers started, main loop every 6min", len(self._workers))
        asyncio.create_task(self.update_hall_of_fame())
        # print("Hall of Fame service initialized.")
    

    def _is_in_development(self):
        cfg = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == 2,
                                                GroupConfiguration.config_key == "is_in_development").first()
        if cfg and cfg.config_value == "1":
            return True
        return False
    
    async def guild_has_bot(self, guild_id: int):
        try:
            guild = await self.bot.fetch_guild(guild_id)
            return guild.get_member(self.bot.user.id) is not None
        except Exception as e:
            log.warning("HOF: guild_has_bot %s failed: %s", guild_id, e)
            return False

    def _parse_group_boss_list(self, required_bosses: GroupConfiguration) -> List[str]:
        boss_list = required_bosses.config_value or ""
        if boss_list == "" or len(str(boss_list)) < 10:
            boss_list = required_bosses.long_value or ""
        if boss_list == "" or len(str(boss_list)) < 10:
            return []
        bosses = boss_list.replace("[", "").replace("]", "").split(",")
        return [boss.strip().replace('"', '') for boss in bosses if boss.strip()]
    

    async def update_hall_of_fame(self):
        iteration = 0
        while True:
            iteration += 1
            last_updates = self._stats_updates
            last_cleanups = self._stats_cleanups
            last_skipped = self._stats_skipped_hash
            self._stats_updates = self._stats_cleanups = self._stats_skipped_hash = 0
            log.warning(
                "HOF loop %d wake | last 6min: %d updated, %d cleaned (404), %d skipped (no change) | pending=%d",
                iteration, last_updates, last_cleanups, last_skipped, len(self._pending_jobs)
            )
            try:
                groups_configured = session.query(GroupConfiguration).filter(
                    GroupConfiguration.config_key == "create_pb_embeds",
                    GroupConfiguration.config_value == "1"
                ).all()
                final_list = []
                group_obj_list = []
                skipped_no_group = 0
                skipped_no_guild = 0
                skipped_no_bot = 0
                for group_cfg in groups_configured:
                    group_id = group_cfg.group_id
                    group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                    if not group_obj:
                        skipped_no_group += 1
                        continue
                    guild_id = group_obj.guild_id
                    try:
                        guild = await self.bot.fetch_guild(guild_id)
                    except Exception as e:
                        log.warning("HOF loop %d: fetch guild %s failed for group %d: %s", iteration, guild_id, group_id, e)
                        continue
                    if not guild:
                        group_cfg.config_value = "0"
                        session.commit()
                        skipped_no_guild += 1
                        continue
                    group_obj_list.append(group_obj)
                    if await self.guild_has_bot(guild_id):
                        final_list.append(group_cfg)
                    else:
                        skipped_no_bot += 1
                jobs_before = len(self._pending_jobs)
                try:
                    qsize_before = self._hof_queue.qsize()
                except Exception:
                    qsize_before = "?"
                total_enqueued = 0
                total_skipped_pending = 0
                for group in final_list:
                    group_obj = next((obj for obj in group_obj_list if obj.group_id == group.group_id), None)
                    if not group_obj:
                        continue
                    enqueued, skipped = await self._update_group_hof(group_obj)
                    total_enqueued += enqueued
                    total_skipped_pending += skipped
                try:
                    qsize_after = self._hof_queue.qsize()
                except Exception:
                    qsize_after = "?"
                skipped_total = skipped_no_guild + skipped_no_group + skipped_no_bot
                log.warning(
                    "HOF loop %d done | groups %d→%d active | jobs enq %d (skip %d pending) | queue %s→%s | sleeping 360s",
                    iteration, len(groups_configured), len(final_list),
                    total_enqueued, total_skipped_pending, qsize_before, qsize_after
                )
                if total_skipped_pending > total_enqueued and total_skipped_pending > 50:
                    log.warning("HOF loop %d: backlog - %d jobs skipped (already pending)", iteration, total_skipped_pending)
            except Exception as e:
                log.exception("HOF loop %d: UNCAUGHT EXCEPTION (loop will continue): %s", iteration, e)
            await asyncio.sleep(360)

    async def _update_group_hof(self, group: Group) -> Tuple[int, int]:
        """Return (enqueued, skipped_pending)."""
        if self._is_in_development() and group.group_id != 2:
            return 0, 0
        required_bosses: GroupConfiguration = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group.group_id,
            GroupConfiguration.config_key == "personal_best_embed_boss_list"
        ).first()
        if not required_bosses:
            log.warning("HOF: group %d has no personal_best_embed_boss_list config", group.group_id)
            return 0, 0
        bosses_to_update = self._parse_group_boss_list(required_bosses)
        if not bosses_to_update:
            return 0, 0

        bosses_to_update.sort(key=str.casefold)
        enqueued = 0
        skipped_pending = 0
        grouped_bosses: Dict[str, set[int]] = {}

        for boss in bosses_to_update:
            canonical_name = _RAID_VARIANT_TO_CANONICAL.get(boss)
            if _SEPULCHRE_FLOOR_RE.match(boss):
                canonical_name = _SEPULCHRE_CANONICAL

            npc = session.query(NpcList).filter(NpcList.npc_name == boss).first()
            if not npc:
                log.warning("HOF: group %d boss '%s' not in NpcList", group.group_id, boss)
                continue

            if canonical_name:
                grouped_bosses.setdefault(canonical_name, set()).add(npc.npc_id)
                continue

            added = await self._enqueue_job(group_id=group.group_id, npc_id=npc.npc_id)
            if added:
                enqueued += 1
            else:
                skipped_pending += 1

        for canonical_name, npc_ids in grouped_bosses.items():
            added = await self._enqueue_grouped_job(
                group_id=group.group_id,
                raid_canonical=canonical_name,
                npc_ids=sorted(npc_ids),
            )
            if added:
                enqueued += 1
            else:
                skipped_pending += 1

        directory_added = await self._enqueue_directory_job(group.group_id)
        if directory_added:
            enqueued += 1
        else:
            skipped_pending += 1

        return enqueued, skipped_pending

    async def _should_send_hof(self, group_id: int, npc: NpcList):
        required_bosses: GroupConfiguration = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == "personal_best_embed_boss_list"
        ).first()
        if required_bosses:
            bosses_to_update = self._parse_group_boss_list(required_bosses)
            if npc.npc_name in bosses_to_update:
                return True
        return False
    
    async def _update_boss_component(self, group_id: int, npc: NpcList):
        if await self._should_send_hof(group_id, npc):
            await self._enqueue_job(group_id=group_id, npc_id=npc.npc_id)
        else:
            # # print(f"[HALL OF FAME]No need to update boss component for {npc.npc_name}")
            pass

    async def _send_boss_components(
        self,
        group_id: int,
        npc: Optional[NpcList],
        components: List[BaseComponent],
        boss_name_override: Optional[str] = None,
    ):
        group = session.query(Group).filter(Group.group_id == group_id).first()
        if not group:
            log.warning("HOF: group %d not found", group_id)
            return False
        boss_name = boss_name_override or (npc.npc_name if npc else None)
        if not boss_name:
            log.warning("HOF: group %d unable to resolve boss name", group_id)
            return False
        skip_config_update = boss_name_override is not None
        channel_cfg = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == "channel_id_to_send_pb_embeds"
        ).first()
        existing_message = session.query(GroupPersonalBestMessage).filter(
            GroupPersonalBestMessage.group_id == group_id,
            GroupPersonalBestMessage.boss_name == boss_name
        ).first()
        if existing_message:
            message_id = existing_message.message_id
            channel_id = existing_message.channel_id
            if not message_id or message_id == "":
                log.warning("HOF: group %d boss %s empty message_id", group_id, boss_name)
                return False
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
                if not channel:
                    self._cleanup_deleted_message(group_id, boss_name, existing_message, skip_config_update=skip_config_update)
                    return "cleaned"
                message = await channel.fetch_message(int(message_id))
                if message is None:
                    self._cleanup_deleted_message(group_id, boss_name, existing_message, skip_config_update=skip_config_update)
                    return "cleaned"
                await self._rate_limited_send_or_edit("edit", group_id)
                await message.edit(components=components)
                existing_message.date_updated = datetime.datetime.now()
                session.commit()
                await asyncio.sleep(random.uniform(0.15, 0.35))
                return True
            except Exception as e:
                if self._is_message_not_found_error(e):
                    self._cleanup_deleted_message(group_id, boss_name, existing_message, skip_config_update=skip_config_update)
                    return "cleaned"
                raise
        elif channel_cfg and channel_cfg.config_value and channel_cfg.config_value != "":
            channel_id = channel_cfg.config_value
            channel = await self.bot.fetch_channel(int(channel_id))
            if not channel:
                raise RuntimeError(f"Channel not found for id {channel_id}")
            await self._rate_limited_send_or_edit("send", group_id)
            message = await channel.send(components=components)
            session.add(GroupPersonalBestMessage(group_id=group_id, message_id=message.id, channel_id=channel_id, boss_name=boss_name))
            session.commit()
            await asyncio.sleep(random.uniform(0.15, 0.35))
            return True
        else:
            log.warning("HOF: group %d boss %s no channel config for new message", group_id, boss_name)
            return False

    async def _finalize_boss_components(self, npc: NpcList, group: Group):
        # Create components matching message_handler.py structure
        pb_components, summary_content = self._create_pb_components(group.group_id, npc)
        # print(f"[HALL OF FAME]PB components returned: {pb_components}")
        # print(f"[HALL OF FAME]PB component types: {[type(c) for c in pb_components]}")

        container = ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content=f"## {self._get_linked_name(npc)} 🏆\n" + 
                        f"{summary_content}"
                    )
                ],
                accessory=ThumbnailComponent(
                    media=UnfurledMediaItem(
                        url=self._get_npc_img_url(npc)
                    )
                )
            ),
            SeparatorComponent(divider=True),
            *pb_components,
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=f"-# Powered by the [DropTracker](https://www.droptracker.io) • [View all Personal Bests](https://www.droptracker.io/personal_bests)"
            ),
            SeparatorComponent(divider=True),
        )

        components = [container]

        return components

    def _create_base_boss_component(self, npc: NpcList):
        """
        Creates the base component layout for a boss message
        """
        components = [
            ContainerComponent(
                SeparatorComponent(divider=True),
                SectionComponent(
                    components=[
                        TextDisplayComponent(
                            content=f"### 🏆 {self._get_linked_name(npc)} - Hall of Fame 🏆\n" + 
                            f""
                        )
                    ],
                    accessory=ThumbnailComponent(
                        media=UnfurledMediaItem(
                            url=self._get_npc_img_url(npc)
                        )
                    )
                ),
                SeparatorComponent(divider=True),
            ),
        ]
        return components
    

    def _create_pb_components(self, group_id: int, npc: NpcList):
        """
        Create the personal best components for a given group and npc
        """
        pbs = self._get_pbs(group_id, npc.npc_name)
        components = []
        fastest_kill = None
        # print(f"[HALL OF FAME]Got PBs: {pbs}")
        fastest_kill_part = ""
        total_pbs = 0
        for team_size, entries in pbs.items():
            # print(f"[HALL OF FAME]Team size: {team_size}")
            for pb in entries:
                # print(f"[HALL OF FAME]PB: {pb}")
                total_pbs += 1
                if fastest_kill is None or pb.personal_best < fastest_kill[0]:
                    fastest_kill = [pb.personal_best, team_size, pb.player_id, None]
        # print(f"[HALL OF FAME]Fastest kill: {fastest_kill}")
        if total_pbs > 0:
            if fastest_kill:
                fastest_kill[3] = session.query(Player).filter(Player.player_id == fastest_kill[2]).first()
                fastest_kill_part = (f"-# • Fastest kill: `{convert_from_ms(fastest_kill[0])}` ({self._get_team_size_string(fastest_kill[1])})\n" +
                                     f"-# ↳ by {get_formatted_name(fastest_kill[3].player_name, group_id, session)}")
            else:
                fastest_kill = [0, 0, 0, "No data"]
        partition = get_current_partition()
        if group_id != 2:
            key = f"leaderboard:group:{group_id}:npc:{npc.npc_id}:{partition}"
            all_key = f"leaderboard:group:{group_id}:npc:{npc.npc_id}"
        else:
            key = f"leaderboard:npc:{npc.npc_id}:{partition}"
            all_key = f"leaderboard:npc:{npc.npc_id}"
        # print(f"[HALL OF FAME]Using key: {key}")
        most_loot_month = redis_client.client.zrevrange(key, 0, 4, withscores=True)
        most_loot_part = ""
        total_loot_part = ""
        if len(most_loot_month) > 1:
       
            month_looters = []
            for loot in most_loot_month:
                player = session.query(Player).filter(Player.player_id == loot[0]).first()
                month_looters.append([loot[0], 1, loot[1], player])
            most_loot = month_looters[0]
            # print(f"[HALL OF FAME]Most loot: {most_loot}")
            
            most_loot_alltime = redis_client.client.zrevrange(all_key, 0, 4, withscores=True)
            if len(most_loot_alltime) > 1:
                most_loot_alltime = most_loot_alltime[0]
                alltime_most_loot = [most_loot_alltime[0], 1, most_loot_alltime[1], None]
            else:
                alltime_most_loot = [0, 0, 0, "No data"]
            # print(f"[HALL OF FAME]All-time most loot: {alltime_most_loot}")
            alltime_most_loot[3] = session.query(Player).filter(Player.player_id == alltime_most_loot[0]).first()
            # print(f"[HALL OF FAME]All-time most loot player: {alltime_most_loot[3]}")
            total_loot = redis_client.zsum(all_key)
            most_loot_part = (f"\n-# • Most Loot: `{format_number(most_loot[2])}` gp (this month)\n" +
                f"-# ↳ by {get_formatted_name(most_loot[3].player_name, group_id, session)}")
            total_loot_part = f"-# • Total loot tracked: `{format_number(total_loot)}` gp\n"
            
        # Debug the content being created
        summary_content = (
            f"📊 **__Overview__**\n" +
            f"-# • Total PBs tracked: `{total_pbs}`\n" +
            f"{total_loot_part}" +
            f"{fastest_kill_part}" +
            f"{most_loot_part}"
        )
        # # print(f"[HALL OF FAME]Summary content: {summary_content}")
        
        # summary_component = TextDisplayComponent(content=summary_content)
        # # print(f"[HALL OF FAME]Summary component type: {type(summary_component)}")
        # components.append(summary_component)
        if len(most_loot_month) > 1:
            loot_str = ""
            for i in range(len(most_loot_month)):
                rank = i + 1
                rank_prefix = _MEDAL_EMOJIS.get(rank, f"{rank}.")
                loot_str += f"-# {rank_prefix} {get_formatted_name(month_looters[i][3].player_name, group_id, session)} - `{format_number(month_looters[i][2])}` gp\n"
            looters_content = (
                f"💰 **__Loot Leaderboard__**\n" +
                f"-# Top 5 players (this month):\n" +
                loot_str
            )
            looters_component = TextDisplayComponent(content=looters_content)
            components.append(looters_component)
            components.append(SeparatorComponent(divider=True))
        components.append(
            TextDisplayComponent(
                content=f":hourglass: **__Personal Best Leaderboards__**\n" 
        ))

        ## Sort the team sizes to place solo first, then 2, 3, 4, etc
        team_size_order = ["Solo", "1", "2", "3", "4", "5", "6+", "7", "8", "9", "10"]
        pbs = {k: v for k, v in sorted(pbs.items(), key=lambda item: team_size_order.index(str(item[0])) if str(item[0]) in team_size_order else len(team_size_order))}

        for team_size, entries in pbs.items():
            team_size_string = self._get_team_size_string(team_size)
            team_size_component = TextDisplayComponent(content=f"-# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + 
                                                       f"-# **{team_size_string}**")
            # print(f"[HALL OF FAME]Team size component type: {type(team_size_component)}")
            components.append(team_size_component)
            pb_text = ""
            for i, pb in enumerate(entries):
                if i >= 5:
                    break
                pb: PersonalBestEntry = pb
                rank = i + 1
                rank_prefix = _MEDAL_EMOJIS.get(rank, f"{rank}.")
                pb_text += f"-# {rank_prefix} `{convert_from_ms(pb.personal_best)}` - {get_formatted_name(pb.player.player_name, group_id, session)}\n"
            pb_component = TextDisplayComponent(content=pb_text)
            components.append(pb_component)
        # print(f"[HALL OF FAME]Final components list: {components}")
        # print(f"[HALL OF FAME]Component types: {[type(c) for c in components]}")
        return components, summary_content
    
    def _get_team_size_string(self, team_size: int):
        match team_size:
            case 1 | "Solo":
                return "Solo"
            case 2 | "Duo":
                return "Duo"
            case 3 | "Trio":
                return "Trio"
            case _:
                return f"{team_size} players"

    def _get_pbs(self, group_id: int, npc_name: str):
        """
        Get the personal bests for a given group and npc name
        """
        npc_ids = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc_name).all()
        npc_ids = [npc_id[0] for npc_id in npc_ids]
        group = session.query(Group).filter(Group.group_id == group_id).first()
        players = group.get_players()
        player_ids = [player.player_id for player in players]
        # player_ids = session.query(text("player_id FROM user_group_association WHERE group_id = :group_id")).params(group_id=group_id).all()
        # player_ids = [player_id[0] for player_id in player_ids]
        ## Remove duplicates
        player_ids = list(set(player_ids))
        pbs = session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id.in_(player_ids), PersonalBestEntry.npc_id.in_(npc_ids)).all()
        personal_bests = {}
        # # print(f"[HALL OF FAME]Got {len(pbs)} pbs")
        unique_team_sizes = set()
        for pb in pbs:
            if pb.team_size not in unique_team_sizes:
                unique_team_sizes.add(pb.team_size)
        # # print(f"[HALL OF FAME]Unique team sizes: {unique_team_sizes}")
        if len(unique_team_sizes) > 5:
            ## Remove the largest team sizes if there are more than 5
            pbs = [pb for pb in pbs if pb.team_size in ["Solo", "2", 2, "3", 3, "4", 4, "5", 5]]
        for pb in pbs:
            if pb.team_size not in personal_bests:
                # # print(f"[HALL OF FAME]Adding team size: {pb.team_size}")
                personal_bests[pb.team_size] = []
            personal_bests[pb.team_size].append(pb)
        for team_size in personal_bests:
            ## Sort the entries by the lowest personal best
            personal_bests[team_size].sort(key=lambda x: x.personal_best)
        return personal_bests

    def _get_linked_name(self, npc: NpcList):
        npc_name = npc.npc_name
        if "Theatre" in npc.npc_name:
            if "Hard Mode" in npc.npc_name:
                npc_name = "HM ToB"
            elif "Entry Mode" in npc.npc_name:
                npc_name = "EM ToB"
            else:
                npc_name = "ToB"
        if "Chambers" in npc.npc_name:
            if "Challenge" in npc.npc_name or "CM" in npc.npc_name:
                npc_name = "CM CoX"
            else:
                npc_name = "CoX"
        if "Tombs" in npc.npc_name:
            if "Expert" in npc.npc_name:
                npc_name = "Expert ToA"
            elif "Entry Mode" in npc.npc_name:
                npc_name = "Entry ToA"
            else:
                npc_name = "ToA"
        if "Nightmare" in npc.npc_name:
            if "Phosani" in npc.npc_name:
                npc_name = "Phosani's"
            else:
                npc_name = "NM"
        return f"[{npc_name}]({self._get_npc_url(npc)})"

    def _get_npc_img_url(self, npc: NpcList):
        return f"https://www.droptracker.io/img/npcdb/{npc.npc_id}.png"
    
    def _get_npc_url(self, npc: NpcList):
        npc_name = npc.npc_name.replace(" ", "-")
        return f"https://www.droptracker.io/npcs/{npc_name}.{npc.npc_id}/view"

    def _components_equal(self, component1, component2):
        """
        Compare two components for equality, handling common differences like trailing whitespace
        """
        if type(component1) != type(component2):
            return False
        
        dict1 = component1.__dict__.copy()
        dict2 = component2.__dict__.copy()
        
        # Recursively normalize the dictionaries
        self._normalize_dict(dict1, visited=set())
        self._normalize_dict(dict2, visited=set())
        
        return dict1 == dict2
    
    def _normalize_dict(self, d, visited=None):
        """
        Recursively normalize a dictionary by stripping trailing whitespace from strings
        and handling nested structures
        """
        if visited is None:
            visited = set()
        
        # Get the object's id to track visited objects
        obj_id = id(d)
        if obj_id in visited:
            return
        visited.add(obj_id)
        
        for key, value in list(d.items()):  # Use list() to avoid dict modification during iteration
            if isinstance(value, str):
                # Strip trailing whitespace from strings
                d[key] = value.rstrip()
            elif isinstance(value, list):
                # Handle lists of components or other objects
                for i, item in enumerate(value):
                    if hasattr(item, '__dict__'):
                        # Check if it's a component object (not a SQLAlchemy model)
                        if hasattr(item, '__class__') and 'Component' in item.__class__.__name__:
                            # If it's an object with attributes, normalize its dict
                            item_dict = item.__dict__.copy()
                            self._normalize_dict(item_dict, visited)
                            # Update the item's dict in place
                            for k, v in item_dict.items():
                                setattr(item, k, v)
                    elif isinstance(item, str):
                        # If it's a string in the list, strip it
                        value[i] = item.rstrip()
            elif isinstance(value, dict):
                # Recursively handle nested dictionaries
                self._normalize_dict(value, visited)
            elif hasattr(value, '__dict__'):
                # Only normalize component objects, not SQLAlchemy models
                if hasattr(value, '__class__') and 'Component' in value.__class__.__name__:
                    # Handle nested objects with their own attributes
                    nested_dict = value.__dict__.copy()
                    self._normalize_dict(nested_dict, visited)
                    # Update the nested object's attributes
                    for k, v in nested_dict.items():
                        setattr(value, k, v)

    # -------------------- New: Queue, Rate Limiter and Hashing Utilities --------------------

    def _job_key(self, job: "HOFJob") -> str:
        if job.update_directory:
            return f"{job.group_id}:directory"
        if job.raid_canonical:
            return f"{job.group_id}:grouped:{job.raid_canonical}"
        return f"{job.group_id}:{job.npc_id}"

    def _grouped_npc_id(self, npc_ids: List[int]) -> int:
        return min(npc_ids) if npc_ids else 0

    async def _enqueue_job(self, group_id: int, npc_id: int) -> bool:
        """Enqueue a normal HOF boss job."""
        job = HOFJob(group_id=group_id, npc_id=npc_id)
        key = self._job_key(job)
        if key in self._pending_jobs:
            return False
        self._pending_jobs.add(key)
        try:
            await self._hof_queue.put(job)
            return True
        except Exception as e:
            self._pending_jobs.discard(key)
            log.warning("HOF: enqueue %s failed: %s", key, e)
            return False

    async def _enqueue_grouped_job(self, group_id: int, raid_canonical: str, npc_ids: List[int]) -> bool:
        unique_npc_ids = sorted(set(npc_ids))
        if not unique_npc_ids:
            return False
        job = HOFJob(
            group_id=group_id,
            npc_id=self._grouped_npc_id(unique_npc_ids),
            raid_canonical=raid_canonical,
            npc_ids=unique_npc_ids,
        )
        key = self._job_key(job)
        if key in self._pending_jobs:
            return False
        self._pending_jobs.add(key)
        try:
            await self._hof_queue.put(job)
            return True
        except Exception as e:
            self._pending_jobs.discard(key)
            log.warning("HOF: enqueue grouped %s failed: %s", key, e)
            return False

    async def _enqueue_directory_job(self, group_id: int) -> bool:
        job = HOFJob(group_id=group_id, npc_id=0, update_directory=True)
        key = self._job_key(job)
        if key in self._pending_jobs:
            return False
        self._pending_jobs.add(key)
        try:
            await self._hof_queue.put(job)
            return True
        except Exception as e:
            self._pending_jobs.discard(key)
            log.warning("HOF: enqueue directory %s failed: %s", key, e)
            return False

    async def _worker(self, worker_index: int):
        while True:
            try:
                job: HOFJob = await self._hof_queue.get()
            except asyncio.CancelledError:
                raise
            key = self._job_key(job)
            try:
                await self._process_job(job)
            except Exception:
                log.exception("HOF worker %d job %s failed", worker_index, key)
            finally:
                self._hof_queue.task_done()
                self._pending_jobs.discard(key)

    async def _process_job(self, job: "HOFJob"):
        lock = self._get_guild_lock(job.group_id)
        async with lock:
            forbidden_until = self._group_forbidden_until.get(job.group_id)
            if forbidden_until and time.monotonic() < forbidden_until:
                return
            await self._process_boss_update(job)

    async def _process_boss_update(self, job: "HOFJob"):
        # Avoid stale identity-map results across loop cycles.
        # This ensures newly inserted PB rows are visible to every job run.
        session.expire_all()
        group = session.query(Group).filter(Group.group_id == job.group_id).first()
        if not group:
            log.warning("HOF: group %d not found", job.group_id)
            return
        if not await self.guild_has_bot(group.guild_id):
            log.warning("HOF: bot not in guild %s (group %d)", group.guild_id, job.group_id)
            return

        if job.update_directory:
            try:
                components = await self._update_directory_message(group)
            except Exception:
                log.exception("HOF: _update_directory_message failed group=%d", job.group_id)
                return
            new_hash = self._compute_components_hash(components)
            if self._is_same_hash(job.group_id, job.npc_id, new_hash):
                self._stats_skipped_hash += 1
                self._maybe_log_progress()
                return
            try:
                result = await self._send_boss_components(
                    job.group_id,
                    None,
                    components,
                    boss_name_override=_DIRECTORY_BOSS_NAME,
                )
                if result is True:
                    self._store_components_hash(job.group_id, job.npc_id, new_hash)
                    self._stats_updates += 1
                    self._maybe_log_progress()
                elif result == "cleaned":
                    self._maybe_log_progress()
                return
            except Exception:
                log.exception("HOF: sending directory failed group=%d", job.group_id)
                return

        npc = session.query(NpcList).filter(NpcList.npc_id == job.npc_id).first()
        if not npc:
            log.warning("HOF: npc %d not found", job.npc_id)
            return

        boss_name_override = None
        try:
            if job.raid_canonical and job.npc_ids:
                npcs = session.query(NpcList).filter(NpcList.npc_id.in_(job.npc_ids)).all()
                if not npcs:
                    log.warning("HOF: grouped npcs missing for group=%d canonical=%s", job.group_id, job.raid_canonical)
                    return
                npcs_by_id = {n.npc_id: n for n in npcs}
                ordered_npcs = [npcs_by_id[nid] for nid in job.npc_ids if nid in npcs_by_id]
                if not ordered_npcs:
                    return
                npc = ordered_npcs[0]
                components = await self._finalize_raid_components(group, job.raid_canonical, ordered_npcs)
                boss_name_override = job.raid_canonical
            else:
                components = await self._finalize_boss_components(npc, group)
        except Exception:
            log.exception("HOF: finalize components failed group=%d npc=%d", job.group_id, job.npc_id)
            return
        new_hash = self._compute_components_hash(components)
        if self._is_same_hash(job.group_id, job.npc_id, new_hash):
            self._stats_skipped_hash += 1
            self._maybe_log_progress()
            return
        max_attempts = 5
        max_429_attempts = 2
        base_delay = 0.5
        max_sleep = 2.0
        for attempt in range(1, max_attempts + 1):
            try:
                await self._rate_limited_send_or_edit("send_or_edit", job.group_id)
                result = await self._send_boss_components(
                    job.group_id,
                    npc,
                    components,
                    boss_name_override=boss_name_override,
                )
                if result is True:
                    self._store_components_hash(job.group_id, job.npc_id, new_hash)
                    self._stats_updates += 1
                    self._maybe_log_progress()
                    return
                if result == "cleaned":
                    self._maybe_log_progress()
                    return
                log.warning("HOF: send returned False group=%d npc=%d attempt=%d", job.group_id, job.npc_id, attempt)
            except Exception as e:
                if self._is_forbidden_error(e):
                    self._group_forbidden_until[job.group_id] = time.monotonic() + 330.0
                    log.warning("HOF: 403 Forbidden group %d, cooldown 330s", job.group_id)
                    return
                is_rate_limit = self._is_rate_limit_error(e)
                if is_rate_limit and attempt >= max_429_attempts:
                    log.warning("HOF: group=%d npc=%d rate-limited, failing after %d attempts to free worker", job.group_id, job.npc_id, max_429_attempts)
                    break
                retry_after = getattr(e, "retry_after", None)
                if retry_after is None and is_rate_limit:
                    retry_after = 1.0
                delay = retry_after if retry_after is not None else base_delay * attempt
                delay = min(float(delay), max_sleep) + random.uniform(0.05, 0.2)
                log.warning("HOF: group=%d npc=%d attempt %d/%d failed: %s", job.group_id, job.npc_id, attempt, max_attempts, e)
                await asyncio.sleep(delay)
        log.error("HOF: FAILED after %d attempts group=%d npc=%d (%s)", max_attempts, job.group_id, job.npc_id, npc.npc_name)

    def _get_variant_mode_name(self, canonical_name: str, npc_name: str) -> str:
        if canonical_name == "Chambers of Xeric":
            if "Challenge" in npc_name or "CM" in npc_name:
                return "Challenge Mode"
            return "Normal"
        if canonical_name == "Theatre of Blood":
            if "Entry Mode" in npc_name:
                return "Entry"
            if "Hard Mode" in npc_name:
                return "Hard Mode"
            return "Normal"
        if canonical_name == "Tombs of Amascut":
            if "Entry Mode" in npc_name:
                return "Entry"
            if "Expert" in npc_name:
                return "Expert"
            return "Normal"
        if canonical_name == "Nightmare of Ashihama":
            if "Phosani" in npc_name:
                return "Phosani's Nightmare"
            return "Nightmare"
        if canonical_name == "The Gauntlet":
            if "Corrupted" in npc_name:
                return "Corrupted"
            return "Crystalline"
        if canonical_name == _SEPULCHRE_CANONICAL:
            floor_match = re.search(r"Floor\s+(\d+)", npc_name)
            if floor_match:
                return f"Floor {floor_match.group(1)}"
        return npc_name

    async def _finalize_raid_components(self, group: Group, canonical_name: str, npcs: List[NpcList]):
        mode_order = {
            "Entry": 0,
            "Normal": 1,
            "Hard Mode": 2,
            "Challenge Mode": 2,
            "Expert": 2,
            "Nightmare": 0,
            "Phosani's Nightmare": 1,
            "Crystalline": 0,
            "Corrupted": 1,
        }
        mode_npcs = [(self._get_variant_mode_name(canonical_name, npc.npc_name), npc) for npc in npcs]
        mode_npcs.sort(key=lambda item: (mode_order.get(item[0], 50), item[0].casefold()))

        grouped_components: List[BaseComponent] = []
        for mode_name, mode_npc in mode_npcs:
            pb_components, summary_content = self._create_pb_components(group.group_id, mode_npc)
            grouped_components.append(
                TextDisplayComponent(content=f"### {mode_name}\n{summary_content}")
            )
            grouped_components.extend(pb_components)
            grouped_components.append(SeparatorComponent(divider=True))

        container = ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(content=f"## {canonical_name} 🏆")
                ],
                accessory=ThumbnailComponent(
                    media=UnfurledMediaItem(
                        url=self._get_npc_img_url(npcs[0])
                    )
                )
            ),
            SeparatorComponent(divider=True),
            *grouped_components,
            TextDisplayComponent(
                content="-# Powered by the [DropTracker](https://www.droptracker.io) • [View all Personal Bests](https://www.droptracker.io/personal_bests)"
            ),
            SeparatorComponent(divider=True),
        )
        return [container]

    async def _update_directory_message(self, group: Group) -> List[BaseComponent]:
        required_bosses: GroupConfiguration = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group.group_id,
            GroupConfiguration.config_key == "personal_best_embed_boss_list"
        ).first()
        boss_names = self._parse_group_boss_list(required_bosses) if required_bosses else []
        boss_names.sort(key=str.casefold)

        display_names: List[str] = []
        seen_canonical = set()
        for boss_name in boss_names:
            canonical_name = _RAID_VARIANT_TO_CANONICAL.get(boss_name)
            if _SEPULCHRE_FLOOR_RE.match(boss_name):
                canonical_name = _SEPULCHRE_CANONICAL
            if canonical_name:
                if canonical_name in seen_canonical:
                    continue
                seen_canonical.add(canonical_name)
                display_names.append(canonical_name)
            else:
                display_names.append(boss_name)
        display_names.sort(key=str.casefold)

        message_rows = session.query(GroupPersonalBestMessage).filter(
            GroupPersonalBestMessage.group_id == group.group_id
        ).all()
        message_by_name = {row.boss_name: row for row in message_rows}
        directory_lines = []
        for name in display_names:
            row = message_by_name.get(name)
            if row and row.channel_id and row.message_id:
                jump_url = f"https://discord.com/channels/{group.guild_id}/{row.channel_id}/{row.message_id}"
                directory_lines.append(f"- [{name}]({jump_url})")
            else:
                directory_lines.append(f"- {name}")

        directory_body = "\n".join(directory_lines) if directory_lines else "- No Hall of Fame bosses configured yet."
        container = ContainerComponent(
            SeparatorComponent(divider=True),
            TextDisplayComponent(content=f"## Hall of Fame Directory\n{directory_body}"),
            SeparatorComponent(divider=True),
        )
        return [container]

    def _maybe_log_progress(self):
        """Log every 5 jobs processed (update or skip) so we know workers are active."""
        processed = self._stats_updates + self._stats_skipped_hash + self._stats_cleanups
        if processed > 0 and processed % 5 == 0:
            log.warning(
                "HOF: 5 checked (%d updated, %d unchanged, %d cleaned)",
                self._stats_updates, self._stats_skipped_hash, self._stats_cleanups
            )

    def _get_guild_lock(self, group_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[group_id] = lock
        return lock

    def _get_guild_limiter(self, group_id: int) -> "RateLimiter":
        limiter = self._guild_limiters.get(group_id)
        if limiter is None:
            # Discord: 5 edits per 5s per channel. All HOF msgs for a group share one channel.
            limiter = RateLimiter(max_calls=1, period_seconds=1.1)
            self._guild_limiters[group_id] = limiter
        return limiter

    async def _rate_limited_send_or_edit(self, _op: str, group_id: int):
        # Acquire both global and per-guild limiter slots
        await asyncio.gather(
            self._global_limiter.acquire(),
            self._get_guild_limiter(group_id).acquire(),
        )

    def _components_plain(self, obj, visited=None):
        if visited is None:
            visited = set()
        oid = id(obj)
        if oid in visited:
            return None
        visited.add(oid)
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, list):
            return [self._components_plain(x, visited) for x in obj]
        if isinstance(obj, dict):
            return {str(k): self._components_plain(v, visited) for k, v in obj.items()}
        if hasattr(obj, "__dict__"):
            data = {k: v for k, v in obj.__dict__.items() if not str(k).startswith("_")}
            # Reduce noisy attrs
            pruned = {k: self._components_plain(v, visited) for k, v in data.items()}
            pruned["__class__"] = obj.__class__.__name__
            return pruned
        return str(obj)

    def _is_message_not_found_error(self, e: Exception) -> bool:
        """Detect 404 / message deleted / NoneType from fetch_message (incl. wrapped/cause chain)."""
        seen = set()
        err = e
        while err and id(err) not in seen:
            seen.add(id(err))
            status = getattr(err, "status", None) or getattr(err, "code", None)
            if status == 404:
                return True
            msg = str(err).lower()
            if "404" in msg or "not found" in msg:
                return True
            if isinstance(err, AttributeError) and ("nonetype" in msg or "none" in msg) and "edit" in msg:
                return True
            cls = type(err).__name__
            if "NotFound" in cls or "notfound" in cls.lower():
                return True
            err = getattr(err, "__cause__", None) or getattr(err, "__context__", None)
        return False

    def _cleanup_deleted_message(
        self,
        group_id: int,
        boss_name: str,
        existing_message: GroupPersonalBestMessage,
        skip_config_update: bool = False,
    ):
        """Remove only the stale message row; keep boss configuration intact."""
        session.delete(existing_message)
        session.commit()
        self._stats_cleanups += 1
        log.warning("HOF: group %d entry %s - message deleted (404), removed GPBM only", group_id, boss_name)

    def _is_rate_limit_error(self, e: Exception) -> bool:
        """Detect 429 / rate limit errors."""
        try:
            status = getattr(e, "status", None) or getattr(e, "code", None)
            if status == 429:
                return True
            text = str(e).lower()
            if "429" in text or "ratelimit" in text or "rate limit" in text or "exceeded its ratelimit" in text:
                return True
        except Exception:
            pass
        return False

    def _is_forbidden_error(self, e: Exception) -> bool:
        try:
            status = getattr(e, "status", None) or getattr(e, "code", None)
            if status == 403:
                return True
            text = str(e).lower()
            if " 403" in text or "forbidden" in text:
                return True
        except Exception:
            pass
        return False

    def _compute_components_hash(self, components: List[BaseComponent]) -> str:
        plain = self._components_plain(components)
        # Normalize trailing whitespace for stability
        if isinstance(plain, dict):
            self._normalize_dict(plain, visited=set())
        s = json.dumps(plain, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    # Bump this when changing embed layout to force all messages to refresh
    _HASH_VERSION = 3

    def _hash_key(self, group_id: int, npc_id: int) -> str:
        return f"hof:hash:v{self._HASH_VERSION}:{group_id}:{npc_id}"

    def _get_stored_hash(self, group_id: int, npc_id: int) -> Optional[str]:
        """Return stored hash from Redis, or None if missing/error."""
        try:
            key = self._hash_key(group_id, npc_id)
            existing = redis_client.client.get(key)
            if not existing:
                return None
            if isinstance(existing, bytes):
                existing = existing.decode("utf-8")
            return existing
        except Exception as e:
            log.debug("HOF _get_stored_hash: Redis error for %d:%d: %s", group_id, npc_id, e)
            return None

    def _is_same_hash(self, group_id: int, npc_id: int, new_hash: str) -> bool:
        try:
            key = self._hash_key(group_id, npc_id)
            existing = redis_client.client.get(key)
            if not existing:
                return False
            if isinstance(existing, bytes):
                existing = existing.decode("utf-8")
            return existing == new_hash
        except Exception as e:
            log.debug("HOF _is_same_hash: Redis error for %d:%d: %s, treating as different", group_id, npc_id, e)
            return False

    def _store_components_hash(self, group_id: int, npc_id: int, new_hash: str):
        try:
            key = self._hash_key(group_id, npc_id)
            redis_client.client.set(key, new_hash, ex=7 * 24 * 3600)
        except Exception as e:
            log.warning("HOF _store_components_hash: Redis failed for %d:%d: %s", group_id, npc_id, e)


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period = period_seconds
        self.calls = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > self.period:
                self.calls.popleft()
            if len(self.calls) < self.max_calls:
                self.calls.append(now)
                return
            # Need to wait
            sleep_for = self.period - (now - self.calls[0])
            sleep_for = max(sleep_for, 0.0) + random.uniform(0.01, 0.05)
            await asyncio.sleep(sleep_for)
            # After sleeping, record the call time
            now2 = time.monotonic()
            while self.calls and now2 - self.calls[0] > self.period:
                self.calls.popleft()
            self.calls.append(now2)


@dataclass
class HOFJob:
    group_id: int
    npc_id: int
    raid_canonical: Optional[str] = None
    npc_ids: Optional[List[int]] = None
    update_directory: bool = False

