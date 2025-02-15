import json
from db.models import GroupPatreon, NotifiedSubmission, User, Group, Guild, Player, Drop, UserConfiguration, session, ItemList, GroupConfiguration, GroupEmbed, Field as EmbField, NpcList
from dotenv import load_dotenv
from sqlalchemy.dialects import mysql
from sqlalchemy import func
from sqlalchemy.orm import joinedload
import interactions
from interactions import Embed
import os
import asyncio
from datetime import datetime, timedelta
from db.update_player_total import process_drops_batch
from utils.download import download_player_image
from utils.wiseoldman import fetch_group_members
from utils.redis import RedisClient, calculate_rank_amongst_groups
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
        

        #print("Checking drop for player_id", player_id)
        if player_id:
            player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.user)).first()
            if player:  
                user: User = player.user
                if user:
                    patreon_status: GroupPatreon = session.query(GroupPatreon).filter(GroupPatreon.user_id == user.user_id).first()
                    patreon_trial = False
                    has_patreon = False
                    if patreon_status:
                        has_patreon = True if patreon_status.patreon_tier >= 1 else False
                    if not has_patreon:
                        if user.date_added >= datetime.now() - timedelta(days=7):
                            has_patreon = True
        if player_id and int(drop_data.quantity) == 1:
            player_groups_key = f"player_groups:{player_id}"
            player_groups_js = redis_client.get(player_groups_key)

            if player_groups_js:
                player_groups = json.loads(player_groups_js)
            else:
                # player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.user)).first()
                if player and player.groups:
                    player_groups = [group.group_id for group in player.groups]  # Get group IDs
                    if 2 not in player_groups:
                        player_groups.append(2) ## Add the user to the global group
                    player_groups_json = json.dumps(player_groups)
                    redis_client.client.set(player_groups_key, player_groups_json, ex=3600)
                else:
                    player_groups = []

            if not player_groups:
                # print("Player is not in any groups.")
                return
            
            for group_id in player_groups:
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
                        process_drops_batch([drop_data], session)
                    except Exception as e:
                        print("Couldn't manually process this drop data...", e)
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
                        for key, value in total_items.items():
                            key = key.decode('utf-8')
                            value = value.decode('utf-8')
                            try:
                                quantity, total_value = map(int, value.split(','))
                            except ValueError:
                                #print(f"Error processing item {key} for player {player_id}: {value}")
                                continue
                            player_total += total_value
                        player_month_total = player_total
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
                        print(f"Image downloaded and URL set: {external_url}")

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
    
        
async def update_group_members():
    print("Updating group member association tables...")
    group_ids = session.query(Group.wom_id).all()
    for wom_id in group_ids:
        wom_id = wom_id[0]
        group: Group = session.query(Group).filter(Group.wom_id == wom_id).first()
        if group:
            group_wom_ids = await fetch_group_members(wom_id)
            group_members = session.query(Player).filter(Player.wom_id.in_(group_wom_ids)).all()
            for member in group_members:
                if member.user:
                    member.user.add_group(group)
                member.add_group(group)
            group.date_updated = func.now()
            try:
                session.commit()
                #await logger.log("access", f"Successfully updated {len(group_members)} group assocations for {group.group_name} (#{group.group_id})", "update_group_members")
            except Exception as e:
                await logger.log("error", f"Couldn't update group member associations for{group.group_name} (#{group.group_id}) e: {e}", "update_group_members")
        else:
            print("Group not found for wom_id", wom_id)


async def associate_player_ids(player_wom_ids):
    # Query the database for all players' WOM IDs and Player IDs
    all_players = session.query(Player.wom_id, Player.player_id).all()
    if player_wom_ids is None:
        return []
    # Create a mapping of WOM ID to Player ID
    db_wom_to_ids = [{"wom": player.wom_id, "id": player.player_id} for player in all_players]
    
    # Filter out the Player IDs where the WOM ID matches any of the given `player_wom_ids`
    matched_ids = [player['id'] for player in db_wom_to_ids if player['wom'] in player_wom_ids]
    
    return matched_ids

