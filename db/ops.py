import json
from db.models import (
    GroupPatreon, NotifiedSubmission, Session, User, Group, Guild, Player, Drop, 
    UserConfiguration, session, XenforoSession, ItemList, GroupConfiguration, 
    GroupEmbed, Field as EmbField, NpcList, NotificationQueue, user_group_association
)
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
from db import models
from db.xf.recent_submissions import create_xenforo_entry
from utils.ranking.npc_ranker import check_npc_rank_change_from_drop
from utils.ranking.rank_checker import check_rank_change_from_drop
from utils.embeds import get_global_drop_embed
from utils.download import download_player_image
from utils.wiseoldman import fetch_group_members, check_user_by_id, check_user_by_username
from utils.redis import RedisClient, calculate_rank_amongst_groups, get_true_player_total
from utils.format import format_number, get_extension_from_content_type, parse_redis_data, parse_stored_sheet, replace_placeholders
from utils.sheets.sheet_manager import SheetManager
from utils.semantic_check import check_drop as verify_item_real
from db.item_validator import check_item_against_monster
import pymysql
from utils.redis import calculate_clan_overall_rank, calculate_global_overall_rank
from db.app_logger import AppLogger

load_dotenv()

insertion_lock = asyncio.Lock()

sheets = SheetManager()

app_logger = AppLogger()

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
                            print("Sending to batch proccessor...")
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
                        ## Add this to the XenForo table for recent submissions if it exceeds 5M gp
                        if int(drop_data.value) * int(drop_data.quantity) > 5000000:
                            ## Create a session for the operation
                            try:
                                await create_xenforo_entry(drop=drop_data)
                            except Exception as e:
                                print("Couldn't add the submission to XenForo:", e)
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
                                                                  drop=drop,
                                                                  player_id=player_id)
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

    async def create_drop_object(self, item_id, player_id, date_received, npc_id, value, quantity, image_url: str = "", authed: bool = False,
                                attachment_url: str = "", attachment_type: str = "", add_to_queue: bool = True, existing_session=None):
        """
        Create a drop and add it to the queue for inserting to the database.
        """
        session = models.session
        use_external_session = existing_session is not None
        if use_external_session:
            session = existing_session
        #print("Create_drop_object called")
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

        # Create notification entries for this drop
        drop_value = value * quantity
        
        # Get player groups and check if notification is needed
        player_groups = session.query(Group).join(Group.players).filter(Player.player_id == player_id).all()
        
        # Get item and npc names for notifications
        item_name = session.query(ItemList.item_name).filter(ItemList.item_id == item_id).first()
        item_name = item_name[0] if item_name else "Unknown"
        
        npc_name_obj = session.query(NpcList.npc_name).filter(NpcList.npc_id == npc_id).first()
        npc_name = npc_name_obj[0] if npc_name_obj else "Unknown"
        
        player = session.query(Player).filter(Player.player_id == player_id).first()
        player_name = player.player_name if player else "Unknown"
        
        for group in player_groups:
            group_id = group.group_id
            
            # Skip global group (ID 2) for notifications
            if group_id == 2:
                continue
                
            # Get minimum value to notify for this group
            min_value_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'min_value_to_notify'
            ).first()
            
            min_value_to_notify = int(min_value_config.config_value) if min_value_config else 2500000
            
            # Check if drop value exceeds minimum for notification
            if drop_value >= min_value_to_notify:
                # Create notification entry
                notification_data = {
                    'drop_id': newdrop.drop_id,
                    'item_name': item_name,
                    'npc_name': npc_name,
                    'value': value,
                    'quantity': quantity,
                    'total_value': drop_value,
                    'player_name': player_name,
                    'player_id': player_id,
                    'image_url': newdrop.image_url,
                    'attachment_type': attachment_type
                }
                
                notification = NotificationQueue(
                    notification_type='drop',
                    player_id=player_id,
                    group_id=group_id,
                    data=json.dumps(notification_data),
                    status='pending',
                    created_at=datetime.now()
                )
                session.add(notification)
                session.commit()

        return newdrop

    async def create_user(self, auth_token, discord_id: str, username: str, ctx = None) -> User:
        """ 
            Creates a new 'user' in the database
        """
        new_user = User(discord_id=str(discord_id), auth_token=str(auth_token), username=str(username))
        try:
            session.add(new_user)
            session.commit()
            app_logger.log(log_type="new", data=f"{username} has been created with Discord ID {discord_id}", app_name="core", description="create_user")
            if ctx:
                return await ctx.send(f"Your Discord account has been successfully registered in the DropTracker database!\n" +
                                "You must now use `/claim-rsn` in order to claim ownership of your accounts.")
            default_config = session.query(UserConfiguration).filter(UserConfiguration.user_id == 1).all()
    ## grab the default configuration options from the database
            if new_user:
                user = new_user
            if not user:
                user = session.query(User).filter(User.discord_id == discord_id).first()

            new_config = []
            for option in default_config:
                option_value = option.config_value
                default_option = UserConfiguration(
                    user_id=user.user_id,
                    config_key=option.config_key,
                    config_value=option_value,
                    updated_at=datetime.now()
                )
                new_config.append(default_option)
            try:
                session.add_all(new_config)
                session.commit()
                return new_user
            except Exception as e:
                session.rollback()
            try:
                droptracker_guild: interactions.Guild = await ctx.bot.fetch_guild(guild_id=1172737525069135962)
                dt_member = droptracker_guild.get_member(member_id=discord_id)
                if dt_member:
                    registered_role = droptracker_guild.get_role(role_id=1210978844190711889)
                    await dt_member.add_role(role=registered_role)
            except Exception as e:
                print("Couldn't add the user to the registered role:", e)
            # xf_user = await xf_api.try_create_xf_user(discord_id=str(discord_id),
            #                                 username=username,
            #                                 auth_key=str(auth_token))
            # if xf_user:
            #     user.xf_user_id = xf_user['user_id']
        except Exception as e:
            session.rollback()
            app_logger.log(log_type="error", data=f"Couldn't create a new user with Discord ID {discord_id}: {e}", app_name="core", description="create_user")
            if ctx:
                return await ctx.send(f"`You don't have a valid account registered, " +
                            "and an error occurred trying to create one. \n" +
                            "Try again later, perhaps.`:" + e, ephemeral=True)
            if new_user:
                app_logger.log(log_type="new", data=f"{new_user.username} has been created with Discord ID {discord_id}", app_name="core", description="create_user")
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
                app_logger.log(log_type="error", data=f"{user.username} tried to claim a rs account that was already associated with {player.user.username}'s account", app_name="core", description="assign_rsn")
                return False
            else:
                player.user = user
                session.commit()
                app_logger.log(log_type="access", data=f"{player.player_name} has been associated with {user.discord_id}", app_name="core", description="assign_rsn")
        except Exception as e:
            session.rollback()
            app_logger.log(log_type="error", data=f"Couldn't associate {player.player_name} with {user.discord_id}: {e}", app_name="core", description="assign_rsn")
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
            app_logger.log(log_type="error", data=f"An error occurred trying to create a {embed_type} embed for group {group_id}: {e}", app_name="core", description="get_group_embed")
    
    async def create_notification(self, notification_type, player_id, data, group_id=None):
        """Create a notification queue entry"""
        notification = NotificationQueue(
            notification_type=notification_type,
            player_id=player_id,
            data=json.dumps(data),
            group_id=group_id,
            status='pending'
        )
        session.add(notification)
        session.commit()
        return notification.id
    
    async def create_player(self, player_name, account_hash):
        """Create a player without Discord-specific functionality"""
        account_hash = str(account_hash)
        
        try:
            # Check if player exists in WiseOldMan
            wom_player, player_name, wom_player_id, log_slots = await check_user_by_username(player_name)
            
            if not wom_player or not wom_player.latest_snapshot:
                return None
            
            player = session.query(Player).filter(Player.wom_id == wom_player_id).first()
            if not player:
                player = session.query(Player).filter(Player.account_hash == account_hash).first()
            
            if player is not None:
                if player_name != player.player_name:
                    old_name = player.player_name
                    player.player_name = player_name
                    player.log_slots = log_slots
                    session.commit()
                    
                    # Create name change notification
                    notification_data = {
                        'player_name': player_name,
                        'player_id': player.player_id,
                        'old_name': old_name
                    }
                    await self.create_notification('name_change', player.player_id, notification_data)
            else:
                try:
                    overall = wom_player.latest_snapshot.data.skills.get('overall')
                    total_level = overall.level
                except Exception as e:
                    total_level = 0
                
                new_player = Player(
                    wom_id=wom_player_id, 
                    player_name=player_name, 
                    account_hash=account_hash, 
                    total_level=total_level,
                    log_slots=log_slots
                )
                session.add(new_player)
                session.commit()
                
                app_logger.log(log_type="access", data=f"{player_name} has been created with ID {new_player.player_id} (hash: {account_hash}) ", app_name="core", description="create_player")
                
                # Create new player notification
                notification_data = {
                    'player_name': player_name
                }
                await self.create_notification('new_player', new_player.player_id, notification_data)
                
                return new_player
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error creating player: {e}", app_name="core", description="create_player")
            return None
        
        return player
    
    async def process_drop(self, drop_data, message_id=None, message_logger=None):
        """Process a drop submission and create notification entries if needed"""
        npc_name = drop_data.get('npc_name')
        item_name = drop_data.get('item_name')
        value = drop_data.get('value')
        item_id = drop_data.get('item_id')
        quantity = drop_data.get('quantity')
        auth_key = drop_data.get('auth_key')
        player_name = drop_data.get('player_name')
        account_hash = drop_data.get('account_hash')
        attachment_url = drop_data.get('attachment_url')
        attachment_type = drop_data.get('attachment_type')
        
        player_name = str(player_name).strip()
        account_hash = str(account_hash)
        
        # Get or create player
        player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if not player:
            player = await self.create_player(player_name, account_hash)
            if not player:
                return None
        
        player_id = player.player_id
        
        # Check NPC exists
        npc = session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
        if not npc:
            # Create notification for new NPC
            notification_data = {
                'npc_name': npc_name,
                'player_name': player_name,
                'item_name': item_name,
                'value': value
            }
            
            await self.create_notification('new_npc', player_id, notification_data)
            return None
        
        npc_id = npc.npc_id
        
        # Check item exists
        item = session.query(ItemList).filter(ItemList.item_id == item_id).first()
        if not item:
            # Create notification for new item
            notification_data = {
                'item_name': item_name,
                'player_name': player_name,
                'item_id': item_id,
                'npc_name': npc_name,
                'value': value
            }
            
            await self.create_notification('new_item', player_id, notification_data)
            return None
        
        # Create the drop entry
        drop_value = int(value) * int(quantity)
        
        # Create the drop in the database
        drop = Drop(
            player_id=player_id,
            npc_id=npc_id,
            npc_name=npc_name,
            item_id=item_id,
            item_name=item_name,
            value=value,
            quantity=quantity,
            date_added=datetime.now()
        )
        
        session.add(drop)
        session.commit()
        
        # Process image if provided
        if attachment_url and attachment_type:
            try:
                # Set up the file extension and file name
                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"{item_id}_{npc_id}_{drop.drop_id}"
                
                if (value * quantity) > 50000:
                    dl_path, external_url = await download_player_image(
                        submission_type="drop",
                        file_name=str(file_name),
                        player=player,
                        attachment_url=str(attachment_url),
                        file_extension=str(file_extension),
                        entry_id=str(drop.drop_id),
                        entry_name=str(item_id),
                        npc_name=str(npc_id)
                    )
                
                    # Update the image URL in the drop entry
                    drop.image_url = external_url
                    session.commit()
            except Exception as e:
                app_logger.log(log_type="error", data=f"Couldn't download image: {e}", app_name="core", description="process_drop")
        
        # Get player groups
        player_groups = session.query(Group).join(
            user_group_association,
            (user_group_association.c.group_id == Group.group_id) &
            (user_group_association.c.player_id == player_id)
        ).all()
        
        # Create notifications for each group if the drop meets criteria
        for group in player_groups:
            group_id = group.group_id
            
            # Skip global group (ID 2)
            if group_id == 2:
                continue
                
            # Check if drop meets minimum value for notification
            min_value_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'min_value_to_notify'
            ).first()
            
            min_value_to_notify = 1000000  # Default
            if min_value_config:
                min_value_to_notify = int(min_value_config.config_value)
            
            if drop_value >= min_value_to_notify:
                notification_data = {
                    'drop_id': drop.drop_id,
                    'item_name': item_name,
                    'npc_name': npc_name,
                    'value': value,
                    'quantity': quantity,
                    'total_value': drop_value,
                    'player_name': player_name,
                    'player_id': player_id,
                    'image_url': drop.image_url,
                    'attachment_type': attachment_type
                }
                
                await self.create_notification('drop', player_id, notification_data, group_id)
        
        return drop

def get_formatted_name(player_name:str, group_id: int, existing_session = None):
    use_existing_session = existing_session is not None
    if use_existing_session:
        session = existing_session
    else:
        session = session
    player = session.query(Player).filter(Player.player_name == player_name).first()
    formatted_name = f"[{player.player_name}](https://www.droptracker.io/players/{player.player_id}/view)"
    if player.user:
        user: User = session.query(User).filter(User.user_id == player.user.user_id).first()
        if user:
            if group_id == 2 and user.global_ping:
                formatted_name = f"<@{user.discord_id}> (`{player.player_name}`)"
            elif user.group_ping:
                formatted_name = f"<@{user.discord_id}> (`{player.player_name}`)"
    return formatted_name
    

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
            query = """SELECT COUNT(*) FROM user_group_association WHERE group_id = :group_id"""
            total_players = session.execute(text(query), {"group_id": group.group_id}).fetchone()
            total_players = total_players[0] if total_players else 0
            embed.add_field(name="Total members:", value=f"{total_players}", inline=True)
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
    app_logger.log(log_type="access", data="Updating group member association tables...", app_name="core", description="update_group_members")
    if forced_id:
        group_ids = [forced_id]
    else:
        # Use scalar_subquery to get just the values
        group_ids = session.scalars(session.query(Group.wom_id)).all()
    total_updated = 0
    for wom_id in group_ids:
        # wom_id should now be a simple integer
        #app_logger.log(log_type="access", data=f"Processing WOM ID: {wom_id}", app_name="core", description="update_group_members")
        try:
            wom_id = int(wom_id)
        except (ValueError, TypeError) as e:
            #app_logger.log(log_type="error", data=f"Error converting WOM ID to int: {e} - womid: {wom_id} (type: {type(wom_id)})", app_name="core", description="update_group_members")
            continue
        group: Group = session.query(Group).filter(Group.wom_id == wom_id).first()
        if group:
            group_wom_ids = await fetch_group_members(wom_id)
            #app_logger.log(log_type="ex_info", data=f"Group WOM IDs: {group_wom_ids}", app_name="core", description="update_group_members")
                
            # Only proceed with member updates if we successfully got the member list
            if group_wom_ids:
                # Get current group members from database
                group_members = session.query(Player).filter(Player.wom_id.in_(group_wom_ids)).all()
                # Remove members no longer in the group
                app_logger.log(log_type="info", data=f"Found {len(group_members)} from our database in {group.group_name}", app_name="core", description="update_group_members")
                for member in group.players:
                    if member.wom_id and member.wom_id not in group_wom_ids:
                        member = session.query(Player).filter(Player.player_id == member.player_id).first()
                        app_logger.log(log_type="access", data=f"{member.player_name} has been removed from {group.group_name}", app_name="core", description="update_group_members")
                        member.remove_group(group)
                        try:
                            await notify_group(bot, "player_removed", group, member)
                        except Exception as e:
                            app_logger.log(log_type="error", data=f"Couldn't notify {group.group_name} that {member.player_name} has been removed: {e}", app_name="core", description="update_group_members")
                
                # Add new members to the group
                for member in group_members:
                    if member not in group.players:
                        if member.user:
                            member.user.add_group(group)
                        member.add_group(group)
                        member = session.query(Player).filter(Player.player_id == member.player_id).first()
                        try:
                            await notify_group(bot, "player_added", group, member)
                        except Exception as e:
                            pass
                group.date_updated = func.now()
                try:
                    session.commit()
                except Exception as e:
                    session.rollback()
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

async def associate_player_ids(player_wom_ids, before_date: datetime = None, session_to_use = None):
    # Query the database for all players' WOM IDs and Player IDs
    if session_to_use is not None:
        session = session_to_use
    else:
        session = models.session
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
