import json
from db.models import GroupPatreon, NotifiedSubmission, User, Group, Guild, Player, Drop, UserConfiguration, session, ItemList, GroupConfiguration, GroupEmbed, Field as EmbField, NpcList
from dotenv import load_dotenv
from sqlalchemy.dialects import mysql
from sqlalchemy import func, text
from sqlalchemy.orm import joinedload
import interactions
from interactions import Embed
import os
import asyncio
from datetime import datetime, timedelta
from db.update_player_total import add_drop_to_ignore, process_drops_batch
from utils.ranking.npc_ranker import check_npc_rank_change_from_drop
from utils.ranking.rank_checker import check_rank_change_from_drop
from utils.embeds import get_global_drop_embed
from utils.download import download_player_image
from utils.wiseoldman import fetch_group_members
from utils.redis import RedisClient, calculate_rank_amongst_groups, get_true_player_total
from utils.format import format_number, get_extension_from_content_type, parse_redis_data, parse_stored_sheet, replace_placeholders
from utils.sheets.sheet_manager import SheetManager
from utils.semantic_check import check_drop as verify_item_real
from db.item_validator import check_item_against_monster
import pymysql
from utils.logger import LoggerClient
from utils.redis import calculate_clan_overall_rank, calculate_global_overall_rank

load_dotenv()

insertion_lock = asyncio.Lock()

sheets = SheetManager()

logger = LoggerClient(token=os.getenv('LOGGER_TOKEN'))

MAX_DROP_QUEUE_LENGTH = os.getenv("QUEUE_LENGTH")

redis_client = RedisClient()

global_footer = os.getenv('DISCORD_MESSAGE_FOOTER')

# Use a dictionary for efficient lookups
player_obj_cache = {}

class DatabaseOperations:
    def __init__(self) -> None:
        self.drop_queue = []
        pass

    async def check_drop(self, bot: interactions.Client, drop_data: Drop):
        
        player_id = drop_data.player_id
        npc_name = session.query(NpcList.npc_name).filter(NpcList.npc_id == drop_data.npc_id).first()
        npc_name = npc_name[0] if npc_name else None
        item_name = session.query(ItemList.item_name).filter(ItemList.item_id == drop_data.item_id).first()
        item_name = item_name[0] if item_name else "Unknown"
        drop_value = drop_data.value * drop_data.quantity
        has_processed = {}

        # Check if player is in cache
        if player_id not in player_obj_cache:
            # If not in cache, query and add to cache
            player = session.query(Player).filter(Player.player_id == player_id).first()
            if player:
                player_obj_cache[player_id] = player
        else:
            # If in cache, retrieve from cache
            player = player_obj_cache[player_id]
        if player_id:
            player_groups_key = f"player_groups:{player_id}"
            player_groups_js = redis_client.get(player_groups_key)

            if player_groups_js:
                player_groups = json.loads(player_groups_js)
            else:
                player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.user)).first()
                global_group = session.query(Group).filter(Group.group_id == 2).first()
                if not player.groups:
                    player.add_group(global_group)
                    print(f"{player.player_name} has been added to the global group")
                if player and player.groups:
                    player_groups = [group.group_id for group in player.groups]  # Get group IDs
                    if 2 not in player_groups:
                        player.add_group(global_group)
                        print(f"{player.player_name} has been added to the global group")
                    player_groups_json = json.dumps(player_groups)
                    redis_client.client.set(player_groups_key, player_groups_json, ex=3600)
                else:
                    player_groups = []

            if not player_groups:
                # print("Player is not in any groups.")
                return
            
            for group_id in player_groups:
                ## Perform ranking checks for changes
                
                if player_id == 1:
                    # print("Checking this drop for a rank change notification -- from @joelhalen")
                    results = await check_rank_change_from_drop(player_id=player_id, drop_data=drop_data, specific_group_id=group_id)
                    npc_results = await check_npc_rank_change_from_drop(player_id, drop_data, group_id)
                    try:
                        # print("Got results:", results)
                        
                        # Check if player improved globally or in any group
                        player_improved_globally = results["player_global"]["rank_change"] > 0
                        
                        # Check if player improved in the specific group
                        player_improved_in_group = False
                        if group_id in results["player_in_group"]:
                            player_improved_in_group = results["player_in_group"][group_id]["rank_change"] > 0
                        
                        if player_improved_globally or player_improved_in_group:
                            if player.user_id != None:
                                print("Player has a user ID")
                                dm_setting = session.query(UserConfiguration).filter_by(user_id=player.user_id,
                                                                                        config_key="dm_on_global_rank_change").first()
                                player_min_rank_setting = session.query(UserConfiguration).filter_by(user_id=player.user_id,
                                                                                        config_key="rank_change_dm_minimum_rank").first()
                                player_min_changed_setting = session.query(UserConfiguration).filter_by(user_id=player.user_id,
                                                                                        config_key="rank_change_dm_minimum_rank_change").first()
                                print("Min rank setting:", player_min_rank_setting.config_value)
                                print("Min rank change setting:", player_min_changed_setting.config_value)
                                print("DM setting:", dm_setting.config_value)
                                
                                if dm_setting:
                                    dm_setting = dm_setting.config_value
                                    if str(dm_setting) == "1":
                                        print("Checking if we should send a DM")
                                        player_min_rank = int(player_min_rank_setting.config_value) if player_min_rank_setting else 0
                                        player_min_change = int(player_min_changed_setting.config_value) if player_min_changed_setting else 0
                                        
                                        # Check if the rank change meets the minimum requirements
                                        global_rank_meets_criteria = results["player_global"]["new_rank"] <= player_min_rank
                                        global_change_meets_criteria = results["player_global"]["rank_change"] >= player_min_change
                                        
                                        
                                        if global_rank_meets_criteria or global_change_meets_criteria:
                                            print("Meets criteria for a DM")
                                            user = session.query(User).filter_by(user_id=player.user_id).first()
                                            user = await bot.fetch_user(user_id=user.discord_id)
                                            
                                            # Create a rich embed for the notification
                                            embed = Embed(
                                                title="🏆 Rank Improvement!",
                                                description=f"Your recent drop has improved your position on the leaderboard!",
                                                color=0x00FF00  # Green color for positive news
                                            )
                                            
                                            # Add fields with detailed information
                                            embed.add_field(
                                                name="📈 Global Rank Change",
                                                value=f"You climbed **{results['player_global']['rank_change']}** places!",
                                                inline=False
                                            )
                                            
                                            embed.add_field(
                                                name="🔢 New Ranking",
                                                value=f"Your new position: **#{results['player_global']['new_rank']}**\n"
                                                    f"Previous position: #{results['player_global']['original_rank']}",
                                                inline=True
                                            )
                                            
                                            embed.add_field(
                                                name="💧 Drop Value",
                                                value=f"**{format_number(drop_value)}**",
                                                inline=True
                                            )
                                            
                                            # Add a footer with timestamp
                                            embed.set_footer(text="Keep up the great work!")
                                            embed.timestamp = datetime.now()
                                            
                                            # Send the embed
                                            print("Sending DM to user:", user.username)
                                            await user.send(embed=embed)
                            else:
                                print("Player " + player.player_name + " does not have a user ID.")
                        # Check if group improved
                        group_improved = False
                        if group_id in results["group"]:
                            group_improved = results["group"][group_id]["rank_change"] > 0
                            
                            if group_improved:
                                enabled_notifications = session.query(GroupConfiguration).filter_by(group_id=group_id,
                                                                                            config_key="send_rank_notifications").first()
                                if enabled_notifications and enabled_notifications.config_value == "1":
                                    group_min_rank_setting = session.query(GroupConfiguration).filter_by(group_id=group_id,
                                                                                                    config_key="rank_change_notification_minimum_rank").first()
                                    group_min_changed_setting = session.query(GroupConfiguration).filter_by(group_id=group_id,
                                                                                                config_key="rank_change_notification_minimum_rank_change").first()
                                    
                                    group_min_rank = int(group_min_rank_setting.config_value) if group_min_rank_setting else 999999
                                    group_min_change = int(group_min_changed_setting.config_value) if group_min_changed_setting else 1
                                    
                                    # Get the group's rank change and new rank
                                    group_rank_change = results["group"][group_id]["rank_change"]
                                    group_new_rank = results["group"][group_id]["new_rank"]
                                    
                                    if group_rank_change >= group_min_change and group_new_rank <= group_min_rank:
                                        channel_id = session.query(GroupConfiguration).filter_by(group_id=group_id,
                                                                                            config_key="channel_id_to_send_rank_notifications").first()
                                        if channel_id:
                                            channel_id = channel_id.config_value
                                            try:
                                                channel = await bot.fetch_channel(channel_id=channel_id)
                                                if channel:
                                                    if player.user_id:
                                                        user = session.query(User).filter_by(user_id=player.user_id).first()
                                                        player_string = f"<@{user.discord_id}>"
                                                    else:
                                                        player_string = f"{player.player_name}"
                                                    
                                                    # Create a rich embed for the notification
                                                    embed = Embed(
                                                        title="🏆 Rank Improvement!",
                                                        description=f"{player_string}'s `{item_name}` drop just increased your group's rank to **#{group_new_rank}**!",
                                                        color=0x00FF00  # Green color for positive news
                                                    )
                                                    
                                                    # Add fields with detailed information
                                                    embed.add_field(
                                                        name="📈 Group Rank Change",
                                                        value=f"You climbed **{group_rank_change}** places!",
                                                        inline=False
                                                    )
                                                    
                                                    group_name = session.query(Group.group_name).filter_by(group_id=group_id).first()
                                                    group_name = group_name[0] if group_name else "Unknown"
                                                    
                                                    # Get player's rank in group
                                                    player_in_group_rank = "N/A"
                                                    if group_id in results["player_in_group"]:
                                                        player_in_group_rank = results["player_in_group"][group_id]["new_rank"]
                                                    
                                                    embed.add_field(
                                                        name="🔢 Player Rank Change",
                                                        value=f"Globally: **#{results['player_global']['new_rank']}**\n"
                                                            f"In {group_name}: **#{player_in_group_rank}**",
                                                        inline=True
                                                    )
                                                    
                                                    embed.add_field(
                                                        name="<:GP:1159872376956272640> Drop Value <:GP:1159872376956272640>",
                                                        value=f"**{format_number(drop_value)}**",
                                                        inline=True
                                                    )
                                                    
                                                    # Add a footer with timestamp
                                                    embed.set_footer(text=global_footer)
                                                    
                                                    # Send the embed
                                                    await channel.send(embed=embed)
                                            except Exception as e:
                                                print("Error fetching channel:", e)
                    except Exception as e:
                        print("Error checking rank change:", e)
                group_config_key = f"group_config:{group_id}"
                group_config = redis_client.client.hgetall(group_config_key)
                if group_config:
                    group_config = parse_redis_data(group_config)
                if not group_config:
                    # If group config not in Redis, query from the database
                    configs = session.query(GroupConfiguration).filter_by(group_id=group_id).all()
                    group_config = {config.config_key: config.config_value for config in configs}
                    
                    # Only cache the configuration in Redis if it's not empty

                    if group_config:
                        redis_client.client.hset(group_config_key, mapping=group_config)  # Use hset instead of hmset
                        redis_client.client.expire(group_config_key, 1)
                    else:
                        print(f"Group config for group_id {group_id} is empty or does not exist.")
                        return
                #print("Retrieved group_config:", group_config)
                # Get minimum value to notify (default to 2.5M if not found)
                # print("Group config:", group_config)
                min_value = int(group_config.get('minimum_value_to_notify', 2500000))
                #print("Min value returned as", min_value)
                send_stacks = group_config.get('send_stacks_of_items', False)
                if int(drop_data.value) > min_value or (send_stacks and (int(drop_data.value) * int(drop_data.quantity)) > min_value):
                    ## Manually process this instantly in the redis cache
                    try:
                        if drop_data.drop_id not in has_processed:
                            process_drops_batch([drop_data], session, from_submission=True)
                            has_processed[drop_data.drop_id] = True
                    except Exception as e:
                        print("Couldn't manually process this drop data...", e)
                    add_drop_to_ignore(drop_data.drop_id)
                    # Get channel ID from group config
                    channel_id = group_config.get('channel_id_to_post_loot')
                    if channel_id:
                        #print("Fetching channel from the discord api")
                        channel = await bot.fetch_channel(channel_id=channel_id)
                        
                        #print("Got channel", channel.name)
                        raw_embed = await self.get_group_embed(embed_type="drop", group_id=group_id)
                        item_name = session.query(ItemList.item_name).filter(ItemList.item_id == drop_data.item_id).first()
                        npc_name = session.query(NpcList.npc_name).filter(NpcList.npc_id == drop_data.npc_id).first()
                        #print("replacing embed with values from dict")
                        partition = int(datetime.now().year * 100 + datetime.now().month)
                        # player_total_month = f"player:{player_id}:{partition}:total_loot"
                        # player_month_total = redis_client.get(player_total_month)
                        # if player_month_total is None:
                        #     print(f"Warning: Redis key {player_total_month} returned None")
                        total_items_key = f"player:{player_id}:{partition}:total_items"

                        # Get total items
                        total_items = redis_client.client.hgetall(total_items_key)
                        #print("redis update total items stored:", total_items)
                        player_total = 0
                        player_month_total = get_true_player_total(player_id)
                        month_name = datetime.now().strftime("%B")
                        wom_member_list = None
                        group_wom_id = session.query(Group.wom_id).filter(Group.group_id == group_id).first()
                        try:    
                            if group_wom_id:
                                group_wom_id = group_wom_id[0]
                            if group_wom_id:
                                print("Finding group members?")
                                wom_member_list = await fetch_group_members(wom_group_id=int(group_wom_id))
                        except Exception as e:
                            print("Couldn't get the member list", e)
                            return
                        #print("Got wom member list")
                        player_ids = await associate_player_ids(wom_member_list)
                        #print("Associated player_ids")
                        clan_player_ids = wom_member_list if wom_member_list else []
                        # print("clan_player_ids:", clan_player_ids)
                        group_rank, ranked_in_group, group_total_month = calculate_clan_overall_rank(player_id, player_ids)
                        # print("Calculated group rank and group totals")
                        global_rank, ranked_global = calculate_global_overall_rank(player_id)
                        # print("Calculated total group/clan members")
                        total_tracked = len(player_ids)
                        group_to_group_rank, total_groups = calculate_rank_amongst_groups(group_id, player_ids)
                        #print("Sending value dict")
                        try:
                            # Use fallback values for None types to avoid concatenation errors
                            # print("Creating value dict with drop data:", drop_data)
                            # print("Month name:", month_name)
                            # print("P total / G total:", player_month_total, group_total_month)
                            # print("Group-to-group:", group_to_group_rank, total_groups)
                            # print("Global rank", global_rank, ranked_global)
                            # print("User count", total_tracked)
                            # print("Item value (q*v)", format_number(int(drop_data.value) * int(drop_data.quantity)))
                            # print("Item id, npc name", drop_data.item_id, npc_name[0] if npc_name else "Unknown")
                            value_dict = {
                                "{item_name}": item_name[0] if item_name else "Unknown",
                                "{month_name}": month_name,
                                "{player_total_month}": format_number(player_month_total) if player_month_total is not None else "0",
                                "{group_total_month}": format_number(group_total_month) if group_total_month is not None else "0",
                                "{group_to_group_rank}": f"{group_to_group_rank if group_to_group_rank else '0'}/{total_groups if total_groups else '1'}",
                                "{group_rank}": f"{group_rank or '0'}/{ranked_in_group or '0'}",  # Handle None with '0'
                                "{global_rank}": f"{global_rank or '0'}/{ranked_global or '0'}",  # Handle None with '0'
                                "{user_count}": total_tracked if total_tracked is not None else "0",
                                "{item_value}": format_number(int(drop_data.value) * int(drop_data.quantity)),
                                "{item_id}": drop_data.item_id if drop_data.item_id else 995,
                                "{npc_name}": npc_name[0] if npc_name else "Unknown"
                            }
                            # print("Passing value dict", value_dict)
                            if group_id == 2:
                                try:
                                    embed: Embed = await get_global_drop_embed(item_name[0] if item_name else "Unknown", drop_data.item_id, player_id, drop_data.quantity, drop_data.value, drop_data.npc_id)
                                except Exception as e:
                                    print("Error getting global drop embed:", e)
                                    embed = replace_placeholders(raw_embed, value_dict)
                            else:
                                embed: Embed = replace_placeholders(raw_embed, value_dict)
                            
                        except Exception as e:
                            print("Exception creating embed:", e)

                        image_link = ""
                        if drop_data.image_url:
                            image_url = drop_data.image_url
                            if len(image_url) > 5:
                                image_link = f"\n" + image_url
                            else:
                                pass
                        player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.user)).first()
                        user = player.user
                        if user:
                            if group_id == 2 and not user.global_ping:
                                str_name = f"{player.player_name}"
                            elif not user.group_ping:
                                str_name = f"{player.player_name}"
                            else:
                                if user.username != player.player_name:
                                    str_name = f"<@{user.discord_id}> (`{player.player_name}`)"
                                else:
                                    str_name = f"<@{user.discord_id}>"
                        else:
                            str_name = f"{player.player_name}"
                        embed.set_author(name=player.player_name,icon_url="https://www.droptracker.io/img/droptracker-small.gif")
                        try:
                            if drop_data.image_url:
                                url = drop_data.image_url
                                local_path = url.replace("https://www.droptracker.io/", "/store/droptracker/disc/static/assets/")
                                attachment = interactions.File(local_path)
                                message = await channel.send(f"{str_name} has received a drop:",
                                                    embed=embed,
                                                    files=attachment)
                            else:
                                message = await channel.send(f"{str_name} has received a drop:",
                                                    embed=embed)
                            if message:
                                message_id = str(message.id)
                                drop = session.query(Drop).filter(Drop.drop_id == drop_data.drop_id).first()
                                notified_sub = NotifiedSubmission(channel_id=str(message.channel.id),
                                                                  message_id=message_id,
                                                                  group_id=group_id,
                                                                  status="sent",
                                                                  drop=drop)
                                session.add(notified_sub)
                                session.commit()
                        except Exception as e:
                            print("Couldn't send the message for a drop:",e)
                        #print(f"{drop_data.item_id} - This drop should have a message sent to channel {channel_id}...")
                    else:
                        pass
                        #print(f"{drop_data.item_id} - Channel ID not found, but drop qualifies for notification.")
                else:
                    pass
                    #print(f"{drop_data.item_id} - This drop does not meet the minimum value ({min_value}) to send a message.")
        else:
            if not player_id:
                print("Player id not found for drop:", drop_data)
            else:
                pass

        

        
    async def create_drop_object(self, bot: interactions.Client, item_id, player_id, date_received, npc_id, value, quantity, image_url: str = "", authed: bool = False,
                                attachment_url: str = "", attachment_type: str = "", add_to_queue: bool = True):
        """
        Create a drop and add it to the queue for inserting to the database.
        """
        if isinstance(date_received, datetime):
            # Convert to string in the required format without timezone and microseconds
            date_received_str = date_received.strftime('%Y-%m-%d %H:%M:%S')
        else:
            date_received_str = date_received  # Assuming it's already a string in the correct format
        

        # Initialize image URL with the provided one
        image_url = image_url or ""

        # Create the drop object
        newdrop = Drop(item_id=item_id,
                    player_id=player_id,
                    date_added=date_received_str,
                    date_updated=date_received_str,
                    npc_id=npc_id,
                    value=value,
                    quantity=quantity,
                    authed=authed,
                    image_url=image_url)

        try:
            # Add the drop to the session and commit to generate the drop_id
            session.add(newdrop)
            session.commit()
        except Exception as e:
            session.rollback()
            print(f"Error committing new drop to the database: {e}")
            return None

        # Attempt to download the image and update the drop entry
        if attachment_url and attachment_type:
            try:
                # Set up the file extension and file name
                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"{item_id}_{npc_id}_{newdrop.drop_id}"  # Create a unique file name

                player = session.query(Player).filter(Player.player_id == player_id).first()
                if player:
                    # Download the image and update the drop with the image URL
                    if (value * quantity) > 50000:
                        dl_path, external_url = await download_player_image(
                            submission_type="drop",
                            file_name=str(file_name),
                            player=player,
                            attachment_url=str(attachment_url),
                            file_extension=str(file_extension),
                            entry_id=str(newdrop.drop_id),
                            entry_name=str(item_id),
                            npc_name=str(npc_id)
                        )
                    
                        # Update the image URL in the drop entry
                        newdrop.image_url = external_url
                        #print(f"Image downloaded and URL set: {external_url}")

                        # Commit the updated drop to the database
                        session.commit()
                else:
                    print("Player not found.")
            except Exception as e:
                print(f"Couldn't download the image: {e}")
                session.rollback()

        # Check if the drop meets the criteria for sending a message
        await self.check_drop(bot, newdrop)

        return newdrop.drop_id

    async def create_user(self, auth_token, discord_id: str, username: str, ctx = None) -> User:
        """ 
            Creates a new 'user' in the database
        """
        new_user = User(discord_id=str(discord_id), auth_token=str(auth_token), username=str(username))
        try:
            session.add(new_user)
            session.commit()
            await logger.log("access", f"{username} has been created with Discord ID {discord_id}", "create_user")
            if ctx:
                return await ctx.send(f"Your Discord account has been successfully registered in the DropTracker database!\n" +
                                "You must now use `/claim-rsn` in order to claim ownership of your accounts.")
            return new_user
        except Exception as e:
            session.rollback()
            await logger.log("error", f"Couldn't create a new user with Discord ID {discord_id}: {e}", "create_user")
            if ctx:
                return await ctx.send(f"`You don't have a valid account registered, " +
                            "and an error occurred trying to create one. \n" +
                            "Try again later, perhaps.`:" + e, ephemeral=True)
            if new_user:
                await logger.log("access", f"{new_user.username} has been created with Discord ID {discord_id}", "create_user")
                return new_user
            else:
                return None

    async def assign_rsn(user: User, player: Player):
        """ 
        :param: user: User object
        :param: player: Player object
            Assigns a 'player' to the specified 'user' object in the database
            :return: True/False if successful 
        """
        try:
            if not player.wom_id:
                return
            if player.user and player.user != user:
                """ 
                    Only allow the change if the player isn't already claimed.
                """
                await logger.log("error", f"{user.username} tried to claim a rs account that was already associated with {player.user.username}'s account", "assign_rsn")
                return False
            else:
                player.user = user
                session.commit()
                await logger.log("access", f"{player.player_name} has been associated with {user.discord_id}", "assign_rsn")
        except Exception as e:
            session.rollback()
            await logger.log("error", f"Couldn't associate {player.player_name} with {user.discord_id}: {e}", "assign_rsn")
            return False
        finally:
            return True
        
    async def get_group_embed(self, embed_type: str, group_id: int):
        """
        :param: embed_type: "lb", "drop", "ca", "clog", "pb"
        :param: group_id: int-representation of the DropTracker-based Group ID
            Returns an interactions.Embed object constructed with the data
            stored for this group_id and embed_type
        """
        try:
            stored_embed = session.query(GroupEmbed).filter(GroupEmbed.group_id == group_id, 
                                                            GroupEmbed.embed_type == embed_type).first()
            if not stored_embed:
                stored_embed = session.query(GroupEmbed).filter(GroupEmbed.group_id == 1,
                                                                GroupEmbed.embed_type == embed_type).first()
            if stored_embed:
                embed = Embed(title=stored_embed.title, 
                              description=stored_embed.description,
                              color=stored_embed.color)
                current_time = datetime.now()
                if stored_embed.timestamp:
                    embed.timestamp = current_time.timestamp()
                
                embed.set_thumbnail(url=stored_embed.thumbnail)
                embed.set_footer(global_footer)
                fields = session.query(EmbField).filter(EmbField.embed_id == stored_embed.embed_id).all()
                current_time = datetime.now()
                refresh_time = current_time + timedelta(minutes=10)
                refresh_unix = int(refresh_time.timestamp())
                if fields:
                    for field in fields:
                        field_name = str(field.field_name)
                        field_value = str(field.field_value)
                        field_name.replace("{next_refresh}", f"<t:{refresh_unix}:R>")
                        field_value.replace("{next_refresh}", f"<t:{refresh_unix}:R>")
                        embed.add_field(name=field_name,
                                        value=field.field_value,
                                        inline=field.inline)
                return embed
            else:
                print("No embed found")
                return None
        except Exception as e:
            await logger.log("error", f"An error occurred trying to create a {embed_type} embed for group {group_id}: {e}", "get_group_embed")
    
        
async def notify_group(bot: interactions.Client, type: str, group: Group, member: Player):
    configured_channel = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group.group_id, GroupConfiguration.config_key == "channel_id_to_send_logs").first()
    if configured_channel and configured_channel.config_value and configured_channel.config_value != "":
        channel_id = configured_channel.config_value
    else:
        return
    try:
        channel = await bot.fetch_channel(channel_id=channel_id)
    except Exception as e:
        print(f"Channel not found for ID: {channel_id}")
        return
    if type == "player_removed":
        if channel:
            if member.user:
                uid = f"<@{member.user.discord_id}>"
            else:
                uid = f"ID: `{member.player_id}`"
            embed = Embed(title=f"<:leave:1213802516882530375> Member Removed",
                          description=f"{member.player_name} ({uid}) has been removed from your group during to a WiseOldMan refresh.",
                          color=0x00ff00)
            embed.add_field(name="Total members:", value=f"{len(group.players)}", inline=True)
            embed.set_footer(global_footer)
            await channel.send(embed=embed)
        else:
            print(f"Channel not found for ID: {channel_id}")
    elif type == "player_added":
        if channel:
            if member.user:
                uid = f"<@{member.user.discord_id}>"
            else:
                uid = f"ID: `{member.player_id}`"   
            embed = Embed(title=f"<:join:1213802515834204200> Member Added",
                          description=f"{member.player_name} ({uid}) has been added to your group during a WiseOldMan refresh.",
                          color=0x00ff00)
            query = """SELECT COUNT(*) FROM user_group_association WHERE group_id = :group_id"""
            total_members = session.execute(text(query), {"group_id": group.group_id}).fetchone()
            total_members = total_members[0] if total_members else 0
            embed.add_field(name="Total members:", value=f"{total_members}", inline=True)
            embed.set_footer(global_footer)
            await channel.send(embed=embed)
        else:
            print(f"Channel not found for ID: {channel_id}")

async def update_group_members(bot: interactions.Client, forced_id: int = None):
    print("Updating group member association tables...")
    if forced_id:
        group_ids = [forced_id]
    else:
        group_ids = session.query(Group.wom_id).all()
    total_updated = 0
    for wom_id in group_ids:
        if type(wom_id) == list:
            wom_id = wom_id[0]
        group: Group = session.query(Group).filter(Group.wom_id == wom_id).first()
        if group:
            group_wom_ids = await fetch_group_members(wom_id)
            
            # Only proceed with member updates if we successfully got the member list
            if group_wom_ids:
                # Get current group members from database
                group_members = session.query(Player).filter(Player.wom_id.in_(group_wom_ids)).all()
                
                # Remove members no longer in the group
                for member in group.players:
                    if member.wom_id and member.wom_id not in group_wom_ids:
                        member = session.query(Player).filter(Player.player_id == member.player_id).first()
                        await notify_group(bot, "player_removed", group, member)
                        print(f"Removing {member.player_name} from {group.group_name}")
                        member.remove_group(group)
                
                # Add new members to the group
                for member in group_members:
                    if member not in group.players:
                        print(f"Adding {member.player_name} to {group.group_name}")
                        if member.user:
                            member.user.add_group(group)
                        member.add_group(group)
                        member = session.query(Player).filter(Player.player_id == member.player_id).first()
                        await notify_group(bot, "player_added", group, member)
                group.date_updated = func.now()
                try:
                    session.commit()
                    #await logger.log("access", f"Successfully updated {len(group_members)} group associations for {group.group_name} (#{group.group_id})", "update_group_members")
                except Exception as e:
                    session.rollback()
                    await logger.log("error", f"Couldn't update group member associations for {group.group_name} (#{group.group_id}): {e}", "update_group_members")
            else:
                print(f"Failed to fetch member list for group {group.group_name} (WOM ID: {wom_id})")
        else:
            print("Group not found for wom_id", wom_id)

    ## Update the global group
    player_ids = session.query(Player.player_id).all()
    for player_id in player_ids:
        player = session.query(Player).filter(Player.player_id == player_id).first()
        if player:
            if 2 not in [group.group_id for group in player.groups]:
                player.add_group(session.query(Group).filter(Group.group_id == 2).first())
                session.commit()

async def associate_player_ids(player_wom_ids, before_date: datetime = None):
    # Query the database for all players' WOM IDs and Player IDs
    if before_date:
        all_players = session.query(Player.wom_id, Player.player_id).filter(Player.date_added < before_date).all()
    else:
        all_players = session.query(Player.wom_id, Player.player_id).all()
    if player_wom_ids is None:
        return []
    all_players = [player for player in all_players if player.player_id != None and player.wom_id != None]
    # Create a mapping of WOM ID to Player ID
    db_wom_to_ids = [{"wom": player.wom_id, "id": player.player_id} for player in all_players]
    
    # Filter out the Player IDs where the WOM ID matches any of the given `player_wom_ids`
    matched_ids = [player['id'] for player in db_wom_to_ids if player['wom'] in player_wom_ids]
    
    return matched_ids

