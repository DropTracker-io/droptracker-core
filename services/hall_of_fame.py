import datetime
from typing import List
from interactions import BaseComponent, Extension, listen
import interactions
from interactions import ComponentContext, Extension, ActionRow, Button, ButtonStyle, FileComponent, PartialEmoji, Permissions, SlashContext, UnfurledMediaItem, listen, slash_command
from interactions.api.events import Startup, Component, ComponentCompletion, ComponentError, ModalCompletion, ModalError, MessageCreate
from interactions.models import ContainerComponent, ThumbnailComponent, SeparatorComponent, UserSelectMenu, SlidingWindowSystem, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, MediaGalleryComponent, MediaGalleryItem, OverwriteType
from db.models import GroupConfiguration, GroupPersonalBestMessage, get_current_partition, session, Group, NpcList, User, Player, user_group_association, PersonalBestEntry
from sqlalchemy import select, func, text
from sqlalchemy.orm import aliased
from db.ops import get_formatted_name
from utils.format import convert_from_ms, format_number, get_npc_image_url
import asyncio
from utils.redis import redis_client

class HallOfFame(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        asyncio.create_task(self.update_hall_of_fame())
        print("Hall of Fame service initialized.")
    

    def _is_in_development(self):
        cfg = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == 2,
                                                GroupConfiguration.config_key == "is_in_development").first()
        if cfg and cfg.config_value == "1":
            return True
        return False
    

    async def update_hall_of_fame(self):
        while True:
            print("Update hall of fame called")
            groups_to_update = session.query(GroupConfiguration.group_id).filter(GroupConfiguration.config_key == "create_pb_embeds",
                                                                                 GroupConfiguration.config_value == "1").all()
            for group in groups_to_update:
                await self._update_group_hof(group)
            await asyncio.sleep(360)

    async def _update_group_hof(self, group: Group):
        if self._is_in_development() and group.group_id != 2:
            return
        group_bosses = []
        required_bosses: GroupConfiguration = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group.group_id, 
                                                                                GroupConfiguration.config_key == "personal_best_embed_boss_list").first()
        boss_list = required_bosses.config_value
        if boss_list == "" or len(str(boss_list)) < 10:
            boss_list = required_bosses.long_value
        if boss_list == "" or len(str(boss_list)) < 10:
            ## Neither field has entries, so we skip this group
            return
        bosses_to_update = boss_list.replace("[", "").replace("]", "").split(",")
        bosses_to_update = [boss.strip() for boss in bosses_to_update]
        for boss in bosses_to_update:
            if boss not in group_bosses:
                group_bosses.append(boss)
        for boss in group_bosses:
            print(f"Updating boss: {boss}")
            boss = boss.replace('"', '')
            npc = session.query(NpcList).filter(NpcList.npc_name == boss).first()
            if npc:
                components = await self._finalize_boss_components(npc, group)
                await self._send_boss_components(group.group_id, npc, components)
            else:
                print(f"NPC not found for {boss}")

    async def _should_send_hof(self, group_id: int, npc: NpcList):
        required_bosses: GroupConfiguration = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id, 
                                                                                GroupConfiguration.config_key == "personal_best_embed_boss_list").first()
        if required_bosses and required_bosses.config_value:
            boss_list = required_bosses.config_value
            if boss_list == "" or len(str(boss_list)) < 10:
                boss_list = required_bosses.long_value
            if boss_list == "" or len(str(boss_list)) < 10:
                ## Neither field has entries, so we skip this group
                return False
            bosses_to_update = boss_list.replace("[", "").replace("]", "").split(",")
            bosses_to_update = [boss.strip() for boss in bosses_to_update]
            if npc.npc_name in bosses_to_update:
                return True
        return False
    
    async def _update_boss_component(self, group_id: int, npc: NpcList):
        if await self._should_send_hof(group_id, npc):
            group = session.query(Group).filter(Group.group_id == group_id).first()
            components = await self._finalize_boss_components(npc, group)
            await self._send_boss_components(group_id, npc, components)
        else:
            print(f"No need to update boss component for {npc.npc_name}")

    async def _send_boss_components(self, group_id: int, npc: NpcList, components: List[BaseComponent]):
        group = session.query(Group).filter(Group.group_id == group_id).first()
        if group:
            channel_cfg = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id, GroupConfiguration.config_key == "channel_id_to_send_pb_embeds").first()
            existing_message = session.query(GroupPersonalBestMessage).filter(GroupPersonalBestMessage.group_id == group_id,
                                                                              GroupPersonalBestMessage.boss_name == npc.npc_name).first()
            if existing_message:
                message_id = existing_message.message_id
                channel_id = existing_message.channel_id
                if message_id and message_id != "":
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                        if channel:
                            message = await channel.fetch_message(message_id)
                            await message.edit(components=components)
                            existing_message.date_updated = datetime.datetime.now()
                            print(f"Message edited for {npc.npc_name}")
                            await asyncio.sleep(5)
                            return True
                        else:
                            print(f"Channel not found for {channel_id}")
                    except Exception as e:
                        print(f"Error editing message: {e}")
                        print(f"Error type: {type(e)}")
                        import traceback
                        print(f"Traceback: {traceback.format_exc()}")
                        return False
            elif channel_cfg and channel_cfg.config_value:
                channel_id = channel_cfg.config_value
                if channel_id != "":
                    channel = await self.bot.fetch_channel(channel_id)
                    if channel:
                        message = await channel.send(components=components)
                        print(f"Message sent to channel for {npc.npc_name}")
                        await asyncio.sleep(5)
                        session.add(GroupPersonalBestMessage(group_id=group_id, message_id=message.id, channel_id=channel_id, boss_name=npc.npc_name))
                        session.commit()
                        return True
                    else:
                        print(f"Channel not found for {channel_id}")
                        return False
            else:
                print(f"Channel not configured for group {group_id}")
                return False
        else:
            print(f"Group not found for {group_id}")
            return False

    async def _finalize_boss_components(self, npc: NpcList, group: Group):
        # Create components matching message_handler.py structure
        pb_components, summary_content = self._create_pb_components(group.group_id, npc)
        # print(f"PB components returned: {pb_components}")
        # print(f"PB component types: {[type(c) for c in pb_components]}")

        container = ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content=f"## 🏆 {self._get_linked_name(npc)} - Hall of Fame 🏆\n" + 
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
        #print(f"Got PBs: {pbs}")
        fastest_kill_part = ""
        total_pbs = 0
        for team_size, entries in pbs.items():
            #print(f"Team size: {team_size}")
            for pb in entries:
                #print(f"PB: {pb}")
                total_pbs += 1
                if fastest_kill is None or pb.personal_best < fastest_kill[0]:
                    fastest_kill = [pb.personal_best, team_size, pb.player_id, None]
        #print(f"Fastest kill: {fastest_kill}")
        if total_pbs > 0:
            if fastest_kill:
                fastest_kill[3] = session.query(Player).filter(Player.player_id == fastest_kill[2]).first()
                fastest_kill_part = f"-# • Fastest kill: `{convert_from_ms(fastest_kill[0])}` ({self._get_team_size_string(fastest_kill[1])})\n"
            else:
                fastest_kill = [0, 0, 0, "No data"]
        partition = get_current_partition()
        if group_id != 2:
            key = f"leaderboard:group:{group_id}:npc:{npc.npc_id}:{partition}"
            all_key = f"leaderboard:group:{group_id}:npc:{npc.npc_id}"
        else:
            key = f"leaderboard:npc:{npc.npc_id}:{partition}"
            all_key = f"leaderboard:npc:{npc.npc_id}"
        #print(f"Using key: {key}")
        most_loot_month = redis_client.client.zrevrange(key, 0, 4, withscores=True)
        most_loot_part = ""
        total_loot_part = ""
        if len(most_loot_month) > 1:
       
            month_looters = []
            for loot in most_loot_month:
                player = session.query(Player).filter(Player.player_id == loot[0]).first()
                month_looters.append([loot[0], 1, loot[1], player])
            most_loot = month_looters[0]
            #print(f"Most loot: {most_loot}")
            
            most_loot_alltime = redis_client.client.zrevrange(all_key, 0, 4, withscores=True)
            if len(most_loot_alltime) > 1:
                most_loot_alltime = most_loot_alltime[0]
                alltime_most_loot = [most_loot_alltime[0], 1, most_loot_alltime[1], None]
            else:
                alltime_most_loot = [0, 0, 0, "No data"]
            #print(f"All-time most loot: {alltime_most_loot}")
            alltime_most_loot[3] = session.query(Player).filter(Player.player_id == alltime_most_loot[0]).first()
            #print(f"All-time most loot player: {alltime_most_loot[3]}")
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
            f"-# ↳ by {get_formatted_name(fastest_kill[3].player_name, group_id, session)}" +
            f"{most_loot_part}"
        )
        #print(f"Summary content: {summary_content}")
        
        # summary_component = TextDisplayComponent(content=summary_content)
        # print(f"Summary component type: {type(summary_component)}")
        # components.append(summary_component)
        if len(most_loot_month) > 1:
            loot_str = ""
            for i in range(len(most_loot_month)):
                loot_str += f"-# {i + 1}. {get_formatted_name(month_looters[i][3].player_name, group_id, session)} - `{format_number(month_looters[i][2])}` gp\n"
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
            #print(f"Team size component type: {type(team_size_component)}")
            components.append(team_size_component)
            pb_text = ""
            for i, pb in enumerate(entries):
                if i >= 5:
                    break
                pb: PersonalBestEntry = pb
                pb_text += f"-# {i + 1} - `{convert_from_ms(pb.personal_best)}` - {get_formatted_name(pb.player.player_name, group_id, session)}\n"
            pb_component = TextDisplayComponent(content=pb_text)
            components.append(pb_component)
        #print(f"Final components list: {components}")
        #print(f"Component types: {[type(c) for c in components]}")
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
        player_ids = session.query(text("player_id FROM user_group_association WHERE group_id = :group_id")).params(group_id=group_id).all()
        player_ids = [player_id[0] for player_id in player_ids]
        ## Remove duplicates
        player_ids = list(set(player_ids))
        pbs = session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id.in_(player_ids), PersonalBestEntry.npc_id.in_(npc_ids)).all()
        personal_bests = {}
        print(f"Got {len(pbs)} pbs")
        unique_team_sizes = set()
        for pb in pbs:
            if pb.team_size not in unique_team_sizes:
                unique_team_sizes.add(pb.team_size)
        print(f"Unique team sizes: {unique_team_sizes}")
        if len(unique_team_sizes) > 5:
            ## Remove the largest team sizes if there are more than 5
            pbs = [pb for pb in pbs if pb.team_size in ["Solo", "2", 2, "3", 3, "4", 4, "5", 5]]
        for pb in pbs:
            if pb.team_size not in personal_bests:
                print(f"Adding team size: {pb.team_size}")
                personal_bests[pb.team_size] = []
            personal_bests[pb.team_size].append(pb)
        for team_size in personal_bests:
            ## Sort the entries by the lowest personal best
            personal_bests[team_size].sort(key=lambda x: x.personal_best)
        return personal_bests

    def _get_linked_name(self, npc: NpcList):
        return f"[{npc.npc_name}]({self._get_npc_url(npc)})"

    def _get_npc_img_url(self, npc: NpcList):
        return f"https://www.droptracker.io/img/npcdb/{npc.npc_id}.png"
    
    def _get_npc_url(self, npc: NpcList):
        npc_name = npc.npc_name.replace(" ", "-")
        return f"https://www.droptracker.io/npcs/{npc_name}.{npc.npc_id}/view"

    
    
