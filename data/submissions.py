from db.models import CombatAchievementEntry, Drop, NotifiedSubmission, session, NpcList, Player, ItemList, PersonalBestEntry, CollectionLogEntry, User, Group, GroupConfiguration, UserConfiguration
from utils.messages import confirm_new_npc, confirm_new_item, name_change_message, new_player_message
from utils.semantic_check import get_current_ca_tier
from utils.wiseoldman import check_user_by_id, check_user_by_username, check_group_by_id, fetch_group_members
from utils.redis import RedisClient
from db.ops import DatabaseOperations, associate_player_ids
from utils.download import download_player_image
from sqlalchemy import func, text
from utils.format import get_command_id, get_extension_from_content_type, replace_placeholders, convert_from_ms
import interactions
from utils.logger import LoggerClient
from utils.semantic_check import check_drop as verify_item_real

from dotenv import load_dotenv
import os
#from xf import xenforo
# Old website api connection
#xf_api = xenforo.XenForoAPI()

from datetime import datetime, timedelta
"""

    Processes drops from the API endpoint and Discord Webhook endpoints

"""
load_dotenv()
logger = LoggerClient(token=os.getenv('LOGGER_TOKEN'))

global_footer = os.getenv('DISCORD_MESSAGE_FOOTER')
redis_client = RedisClient()
db = DatabaseOperations()

npc_list = {} # - stores a dict of the npc's and their corresponding IDs to prevent excessive querying
player_list = {} # - stores a dict of player name:ids, and their last refresh from the DB.
class RawDropData():
    def __init__(self) -> None:
        pass

def check_auth(player_name, account_hash, auth_key):
    """
    Returns true, true if there is a matching player+account_hash combo.
    Returns true, false if player exists but hash doesn't match.
    Returns false, false if player does not exist.
    """
    player = session.query(Player).filter(Player.player_name == player_name).first()
    
    if not player:
        return False, False
        
    if player.account_hash:
        if account_hash != player.account_hash:
            return True, False
        else:
            return True, True
    else:
        # Update the account hash if it's not set
        existing_player = session.query(Player).filter(Player.account_hash == account_hash).first()
        if existing_player:
            existing_player.player_name = player_name
            logger.log(f"access",f"Player {player_name} already exists with account hash {account_hash}, updating player name to {player_name}", "check_auth")
            session.commit()
        player.account_hash = account_hash
        session.commit()
        return True, True

# def check_auth(player_name, account_hash, auth_key):
#     """
#         Returns true, true if there is a 
#         matching player+user combo for the passed account hash
#         and the auth_key matches the stored.
#         Returns true, false is user exists but auth fails.
#         Returns false, false if used does not exist or player does not exist.
#     """
#     player_auth_key = f"{account_hash}:auth"
#     stored_auth = redis_client.client.get(player_auth_key)
#     if stored_auth:
#         #print("Comparing stored auth:")
#         #print(stored_auth.strip())
#         #print(auth_key.strip())
#         if stored_auth.strip().decode('utf-8') == auth_key.strip():
#             return True, True
#         else:
#             return True, False
#     else:
#         # print("Player name is", player_name)
#         player = session.query(Player).filter(Player.player_name == player_name).first()
#         if player:
#             if player.account_hash:
#                 if account_hash != player.account_hash:
#                     return True, False
#         else:
#             ## update the account hash instantly before checking for the user object
#             ## if they have a 0 stored (from claiming in discord)
#             if player:
#                 player.account_hash = account_hash
#                 session.commit()
#             else:
#                 return False, False
#         if not player or not player.user:
#             return False, False
#         else:
#             user: User = player.user
#             db_auth = user.auth_token 
#             #print("Comparing", str(db_auth).strip(), "to", str(auth_key).strip())
#             if str(db_auth).strip() != str(auth_key).strip():
#                 return True, False
#             else:
#                 redis_client.client.set(player_auth_key, db_auth, ex=3600)
#                 return True, True




async def drop_processor(bot: interactions.Client, drop_data: RawDropData):
    npc_name = drop_data['npc_name']
    item_name = drop_data['item_name']
    value = drop_data['value']
    item_id = drop_data['item_id']
    quantity = drop_data['quantity']
    auth_key = drop_data['auth_key']
    player_name = drop_data['player_name']
    account_hash = drop_data['account_hash']
    player_name = str(player_name).strip()
    account_hash = str(account_hash)
    image_url = None
    # print("drop_processor received account hash as", account_hash)
    authed = False
    if player_name not in player_list:
        # print("Didn't find", player_name, "in the list, checking the database...")
        player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if not player:
            #print("Player not found in the database, creating a new one...")
            await try_create_player(bot, player_name, account_hash)
        player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if player:
            player_list[player_name] = player.player_id
        else:
            return
    user_exists, authed = check_auth(player_name, account_hash, auth_key)
    # print("Exists, authed:", user_exists,authed)

    if user_exists and not authed:
        player_object = session.query(Player).filter(Player.account_hash == account_hash).first()
        if player_object:
            if player_object.user:
                print("Player has a user object attached")
                user: User = player_object.user

                send_dms = session.query(UserConfiguration).filter(UserConfiguration.user_id == user.user_id,
                                                                UserConfiguration.config_key == 'dm_me_on_submissions_without_token').first()
                if send_dms:
                    if send_dms.config_value == "true":
                        try:
                            discord_user = await bot.fetch_user(user_id=user.discord_id)
                            if discord_user:
                                await discord_user.send(f"Hey, <@{discord_user.id}>!\n" + 
                                                        f"It looks like you may have forgotten your `auth token` on the RuneLite plugin!\n\n" + 
                                                        f"In the event you forgot it, you can retrieve it again with </get-token:{await get_command_id(bot, 'get-token')}>\n\n" + 
                                                        "Does this seem like an error? You can disable these warnings on our website, or reach out for support in Discord.")
                                print("DMed the user due to a missing auth token.")
                        except Exception as e:
                            print("Couldn't DM a user due to their missing auth token:", e)
                        return
                else:
                    pass
                    # print("send_dms is false, and the user did not authenticate...")
            # print("DROP_PROCESSOR - User account exists but their auth token did not match")
            return
    if npc_name in npc_list:
        npc_id = npc_list[npc_name]
    else:
        npc = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc_name).first()
        if not npc:
            print(f"NPC {npc_name} not found in the database. Calling confirm_new_npc.")
            await confirm_new_npc(bot, npc_name, player_name, item_name, value)
            return  # Return here since the NPC creation is deferred; this drop will be ignored
        else:
            npc_list[npc_name] = npc.npc_id
            npc_id = npc.npc_id
            

    # print("Passed account hash check")
    player_id = player_list[player_name]
    item = redis_client.get(item_id)
    if not item:
        item = session.query(ItemList.item_id).filter(ItemList.item_id == item_id).first()
    if item:
        redis_client.set(item_id, item[0])
    else:
        await confirm_new_item(bot, item_name, player_name, item_id, npc_name, value)
        return
    drop_value = value * quantity
    if drop_value > 1000000: ## Anything over 1M should be verified against it's source..
        is_from_npc = verify_item_real(item_name, npc_name)
        print("Was " + item_name, "from", npc_name + "?", is_from_npc)
        #is_from_npc = check_item_against_monster(item_name, npc_name)
        if not is_from_npc:
            print("Drop has been destroyed as it exceeded 1M and was not expected from this npc:", item_name, "from", npc_name)
            return
    # Now create the drop object
    # print("Creating db object for a drop....")
    attachment_url = drop_data['attachment_url']
    attachment_type = drop_data['attachment_type']
    drop_id = await db.create_drop_object(bot,
        item_id=item_id,
        player_id=player_id,
        date_received=datetime.now(),
        npc_id=npc_id,
        value=value,
        quantity=quantity,
        image_url="" if attachment_url else None,
        authed=authed,
        attachment_url=attachment_url,
        attachment_type=attachment_type,
        add_to_queue=True
    )
    #print("Checking if there is an attachment url")
    # if drop_id and drop_data['attachment_url']:
    #     print("Drop has attachment data")
        
    #     drop = session.query(Drop).filter(Drop.drop_id == drop_id).first()
    #     drop.image_url = image_url
    #     session.commit()
    # print("Returning from drop_processor")
    return

async def clog_processor(bot: interactions.Client,
                         player_name: str,
                         account_hash,
                         auth_key,
                         item_name: str,
                         source: str,
                         kc: int,
                         reported_slots: int,
                         attachment_url: str,
                         attachment_type: str):
    """
    Process a new collection log entry for a player.
    :param bot: interactions.Client instance
    :param player_name: Player's username
    :param item_name: The item name that the user received as a new log slot
    :param source: The NPC name or source name that they received the drop from
    :param reported_slots: The number of reported slots for this entry
    """
    # Fetch the item and NPC information
    account_hash = str(account_hash)
    
    # check_verif_user(account_hash)
    item: ItemList = session.query(ItemList.item_id).filter(ItemList.item_name == item_name.strip(), ItemList.noted == False).first()
    if not item:
        print("Couldn't find an item for this collection log entry.")
        return
    item_id = item.item_id
    npc = session.query(NpcList.npc_id).filter(NpcList.npc_name == source).first()
    if not npc:
        npc = NpcList(npc_name=source)
        session.add(npc)
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            print("Couldn't add a new NPC for this clog submission:", e)
    if player_name not in player_list:
        # print("Didn't find", player_name, "in the list, checking the database...")
        player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if not player:
            #  print("Player not found in the database, creating a new one...")
            await try_create_player(bot, player_name, account_hash)
    
    authed = False
    user_exists, authed = check_auth(player_name, account_hash, auth_key)
    if user_exists and not authed:
        print("PB_PROCESSOR - User account exists but their auth token did not match -", player_name, account_hash)
        return
    # Fetch the player
    player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
    if player and player.account_hash and player.account_hash != account_hash:
        print("Player exists but acc hash doesn't match")
        return
    if not player:
        print("CLOG_PROCESSOR - Player was not found?")
        return
    formatted_name = player_name
    if player:
        if player.user:
            formatted_name = f"<@{player.user.discord_id}>"
    else:
        print("CLOG_PROCESSOR - Player was not found?")
        return
    # Create a new collection log entry
    new_clog = CollectionLogEntry(item_id=item.item_id,
                                  npc_id=npc.npc_id,
                                  player_id=player.player_id,
                                  reported_slots=reported_slots)
    session.add(new_clog)
    session.commit()
    dl_path = None
    if new_clog.log_id and attachment_url and attachment_type:
            image_url = None
            file_extension = get_extension_from_content_type(attachment_type)
            file_name = f"{reported_slots}_slots"
            try:
                player = session.query(Player).filter(Player.player_name == player_name).first()
                if player:
                    dl_path, external = await download_player_image(submission_type="clog", 
                                                    file_name=file_name, 
                                                    player=player,
                                                        attachment_url=attachment_url,
                                                        file_extension=file_extension,
                                                        entry_id=new_clog.log_id,
                                                        entry_name=reported_slots,
                                                        npc_name=source)
                    image_url = external
                    print("Generated image url", image_url)
            except Exception as e:
                print("Couldn't download the image:", e)
            if image_url:
                clog = session.query(CollectionLogEntry).filter(CollectionLogEntry.log_id == new_clog.log_id).first()
                clog.image_url = image_url
            session.commit()
    # Process group-related logic if the player is part of any groups
    print("clog_processor - Checking if the player is in any groups.")
    player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
    player_group_ids = session.execute(text(player_group_id_query), {"player_id": player.player_id}).fetchall()
    player_group_ids = [group_id[0] for group_id in player_group_ids]
    player_group_ids.append(2)
    player_groups = session.query(Group).filter(Group.group_id.in_(player_group_ids)).all()
    print("Returned groups ids:", player_groups)
    if player_groups:
        print(player.player_name, "is in", len(player_groups), "groups")
        for group in player.groups:
            group_id = group.group_id

            # Fetch group settings
            channel_id = session.query(GroupConfiguration.config_value).filter(GroupConfiguration.config_key == 'channel_id_to_post_clog',
                                                                               GroupConfiguration.group_id == group_id).first()
            send_clogs = session.query(GroupConfiguration.config_value).filter(GroupConfiguration.config_key == 'notify_clogs',
                                                                               GroupConfiguration.group_id == group_id).first()
            
            if (
                send_clogs
                and send_clogs[0]
                and channel_id
                # and ( TODO -- decide if we want to use patreon features again in the future
                #     any(patreon.patreon_tier >= 2 for patreon in group.group_patreon)
                #     or (group.date_added >= datetime.now() - timedelta(days=7))
                # )
            ):
                if group.wom_id:
                    wom_group_id = group.wom_id
                    # Fetch player WOM IDs and associated Player IDs for the group
                    player_wom_ids = await fetch_group_members(wom_group_id)
                else:
                    # Get all player WOM IDs if no WOM group ID is available
                    player_wom_ids = [player[0] for player in session.query(Player.wom_id).all()]

                # Associate player IDs with WOM IDs
                player_ids = await associate_player_ids(player_wom_ids)

                subquery = session.query(
                    CollectionLogEntry.player_id,
                    func.max(CollectionLogEntry.reported_slots).label('max_reported_slots')
                ).group_by(CollectionLogEntry.player_id).subquery()

                # Query to get the ranked logs for the group based on the highest reported_slots
                group_stored_logs = session.query(subquery.c.player_id, subquery.c.max_reported_slots) \
                    .filter(subquery.c.player_id.in_(player_ids)) \
                    .order_by(subquery.c.max_reported_slots.desc()) \
                    .all()

                group_placement = None
                for idx, entry in enumerate(group_stored_logs, start=1):
                    if entry.player_id == player.player_id:
                        group_placement = idx
                        break

                total_group = len(group_stored_logs)

                # Query to get the ranked logs globally based on the highest reported_slots
                all_stored_logs = session.query(subquery.c.player_id, subquery.c.max_reported_slots) \
                    .order_by(subquery.c.max_reported_slots.desc()) \
                    .all()

                total_global = len(all_stored_logs)

                global_placement = None
                for idx, entry in enumerate(all_stored_logs, start=1):
                    if entry.player_id == player.player_id:
                        global_placement = idx
                        break
                if channel_id:
                    channel = await bot.fetch_channel(channel_id[0])
                else:
                    print("Channel was not found for this group:", group.group_id)
                    return
                raw_embed = await db.get_group_embed('clog', group.group_id)
                attachment = None
                if dl_path:
                    attachment = interactions.File(dl_path)
                partition = int(datetime.now().year * 100 + datetime.now().month)
                player_total_month = f"player:{player.player_id}:{partition}:total_loot"
                player_month_total = redis_client.get(player_total_month)
                if raw_embed:
                    placeholders = {"{item_name}": item_name,
                                    "{npc_name}": source,
                                    "{player_name}": player_name,
                                    "{global_rank}": global_placement,
                                    "{total_ranked_global}": total_global,
                                    "{group_rank}": group_placement,
                                    "{total_ranked_group}": total_group,
                                    "{total_tracked}": len(player_ids),
                                    "{log_slots}": reported_slots,
                                    "{player_loot_month}": player_month_total,
                                    "{item_id}": item_id,
                                    "{kc_received}": kc}
                    embed = replace_placeholders(raw_embed, placeholders)
                    if group.group_id == 2:
                        temp_embed = interactions.Embed(title=embed.title, description=embed.description, color=embed.color)
                        temp_embed.set_thumbnail(embed.thumbnail.url)
                        temp_embed.set_footer(embed.footer.text)
                        for field in embed.fields:
                            if field.name == "Group Stats":
                                continue
                            temp_embed.add_field(field.name, field.value, field.inline)
                        embed = temp_embed
                    if source.lower().strip() == "unknown":
                        embed.description = ""
                    if attachment:
                        message = await channel.send(f"{formatted_name} has received a new Collection Log slot:",
                                           embed=embed,
                                           files=attachment)
                        
                    else:
                        message = await channel.send(f"{formatted_name} has received a new Collection Log slot:",
                                           embed=embed)
                    if message:
                        await logger.log("access", f"{player_name} received a new collection log slot ({item_name}) in {group_id}", "clog_processor")
                        message_id = str(message.id)
                        clog = session.query(CollectionLogEntry).filter(CollectionLogEntry.log_id == new_clog.log_id).first()
                        notified_sub = NotifiedSubmission(channel_id=str(message.channel.id),
                                                            message_id=message_id,
                                                            group_id=group_id,
                                                            status="sent",
                                                            clog=clog)
                        session.add(notified_sub)
                        session.commit()
    else:
        print("Player is not in any groups.")
async def ca_processor(bot: interactions.Client,
                       account_hash,
                       auth_key,
                       task_name,
                       points_awarded,
                       points_total,
                       completed_tier,
                       attachment_url,
                       attachment_type):
    
    dl_path = None
    player = session.query(Player).filter(Player.account_hash == account_hash).first()
    if not player:
        return
    player_name = player.player_name
    player_list[player_name] = player.player_id
    authed = False
    user_exists, authed = check_auth(player_name, account_hash, auth_key)
    if user_exists and not authed:
        return
    existing_entry = session.query(CombatAchievementEntry).filter(CombatAchievementEntry.player_id == player.player_id,
                                                                  CombatAchievementEntry.task_name == task_name).first()
    if existing_entry:
        ## duplicate CA
        return
    new_ca = CombatAchievementEntry(player_id=player.player_id,
                                        task_name=task_name)
    try:
        session.add(new_ca)
    except Exception as e:
        print("Couldn't add the new CA:", e)
        session.rollback()
    print("ca_processor - Checking if the player is in any groups.")
    player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
    player_group_ids = session.execute(text(player_group_id_query), {"player_id": player.player_id}).fetchall()
    
    player_group_ids = [group_id[0] for group_id in player_group_ids]
    player_group_ids.append(2)
    player_groups = session.query(Group).filter(Group.group_id.in_(player_group_ids)).all()
    print("Returned groups:", player_groups)
    if player_groups:
        print(player.player_name, "is in", len(player_groups), "groups")
        for group in player.groups:
            group: Group = group
            group_id = group.group_id
            send_cas = session.query(GroupConfiguration.config_value).filter(GroupConfiguration.config_key == 'notify_cas',
                                                                                        GroupConfiguration.group_id == group_id).first()
            formatted_name = player.player_name
            channel_id = session.query(GroupConfiguration.config_value).filter(GroupConfiguration.config_key == 'channel_id_to_post_ca',
                                                                                       GroupConfiguration.group_id == group_id).first()
            if channel_id:
                channel_id = channel_id[0]
            if player.user:
                user: User = player.user
                formatted_name = f"<@{user.discord_id}>"
            if (
                send_cas
                and send_cas[0]
                and channel_id
                # and ( TODO -- decide if we want to use patreon features again in the future
                #     any(patreon.patreon_tier >= 2 for patreon in group.group_patreon)
                #     or (group.date_added >= datetime.now() - timedelta(days=7))
                # )
            ):
                if new_ca.id and attachment_url and attachment_type:
                    file_extension = get_extension_from_content_type(attachment_type)
                    try:
                        player = session.query(Player).filter(Player.player_name == player_name).first()
                        if player:
                            dl_path, external = await download_player_image(submission_type="ca", 
                                                            file_name=task_name, 
                                                            player=player,
                                                                attachment_url=attachment_url,
                                                                file_extension=file_extension,
                                                                entry_id=new_ca.id,
                                                                entry_name=task_name,
                                                                npc_name="all")
                            image_url = external
                            print("Generated image url", image_url)
                    except Exception as e:
                        print("Couldn't download the image:", e)
                    if image_url:
                        ca = session.query(CombatAchievementEntry).filter(CombatAchievementEntry.id == new_ca.id).first()
                        ca.image_url = image_url
                        session.commit()
                ## Qualifies for a discord message
                channel = await bot.fetch_channel(channel_id=channel_id)
                if channel:
                    embed_template = await db.get_group_embed('ca', group_id)
                    actual_tier = get_current_ca_tier(points_total)
                    if embed_template:
                        value_dict = {
                            "{player_name}": formatted_name,
                            "{task_name}": task_name,
                            "{current_tier}": actual_tier,
                            "{points_awarded}": points_awarded,
                            "{total_points}": points_total
                        }
                        embed: interactions.Embed = replace_placeholders(embed_template, value_dict)
                        if group.group_id == 2:
                            temp_embed = interactions.Embed(title=embed.title, description=embed.description, color=embed.color)
                            temp_embed.set_thumbnail(embed.thumbnail.url)
                            temp_embed.set_footer(embed.footer.text)
                            for field in embed.fields:
                                if field.name == "Group Stats":
                                    continue
                                temp_embed.add_field(field.name, field.value, field.inline)
                            embed = temp_embed
                        embed.set_author("New Combat Achievement:")
                        if dl_path:
                            attachment = interactions.File(dl_path)
                            message = await channel.send(f"{formatted_name} has completed a new Combat Achievement:",embed=embed,
                                files=attachment)
                        else:
                            message = await channel.send(f"{formatted_name} has completed a new Combat Achievement:",embed=embed)
                        if message:
                            message_id = str(message.id)
                            await logger.log("access", f"{player_name} completed a new Combat Achievement ({task_name}) in {group_id}", "ca_processor")
                            ca = session.query(CombatAchievementEntry).filter(CombatAchievementEntry.id == new_ca.id).first()
                            notified_sub = NotifiedSubmission(channel_id=str(message.channel.id),
                                                                message_id=message_id,
                                                                group_id=group_id,
                                                                status="sent",
                                                                ca=ca)
                            session.add(notified_sub)
                            session.commit()
    else:
        print("No groups found for the player...")
async def pb_processor(bot: interactions.Client,
                       player_name, 
                       account_hash,
                       auth_key,
                       boss_name, 
                       current_time_ms, 
                       personal_best_ms,
                       team_size, 
                       is_new_pb,
                       attachment_url,
                       attachment_type):
    player_name = str(player_name).strip()
    if player_name not in player_list:
        new_player = await try_create_player(bot, player_name, account_hash)
        if new_player:
            player_list[player_name] = new_player.player_id
    authed = False
    user_exists, authed = check_auth(player_name, account_hash, auth_key)
    if user_exists and not authed:
        print("PB_PROCESSOR - User account exists but their auth token did not match -", player_name, account_hash)
        return
    if player_name in player_list:
        player_id = player_list[player_name]
    else:
        return
    if not player_id:
        return
    if boss_name == "Whisperer":
        boss_name = "The Whisperer"
    elif boss_name == "Hueycoatl":
        boss_name = "The Hueycoatl"
    npc_id = session.query(NpcList.npc_id).filter(NpcList.npc_name == boss_name).first()
    existing_pb = None
    if not npc_id:
        await confirm_new_npc(bot, boss_name, player_name, "none", 1)
    else:
        try:
            tnpc_id = npc_id[0]
        except Exception as e:
            print("Couldn't get the npc id:", e)
            return
        existing_pb: PersonalBestEntry = session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id == player_id,
                                                            PersonalBestEntry.npc_id == tnpc_id,
                                                            PersonalBestEntry.team_size == team_size).first()
    
    if existing_pb and not is_new_pb:
        # already stored player's PB for this boss/team size & this isn't a new PB
        return
    elif not existing_pb:
        new_entry = PersonalBestEntry(player_id=player_id,
                                  npc_id=npc_id[0],
                                  kill_time=current_time_ms,
                                  personal_best=personal_best_ms,
                                  team_size=team_size,
                                  new_pb=is_new_pb)
        try:
            session.add(new_entry)
            session.commit()
        except Exception as e:
            print("Couldn't save the user's personal best to the database", e)
            session.rollback()
    else:
        existing_pb.personal_best = personal_best_ms
        session.commit()
        print("Updated a new PB for the user.")
    if not is_new_pb:
        return ## don't send for non-pbs, even if it's their first entry for this npc
    ## now we can handle logic for patreon groups' notifications
    dl_path, external, image_url = None, None, None
    if is_new_pb:
        if existing_pb:
            new_entry = existing_pb
        if new_entry.id and attachment_url and attachment_type:
            file_extension = get_extension_from_content_type(attachment_type)
            file_name = f"{current_time_ms}"
            try:
                player = session.query(Player).filter(Player.player_name == player_name).first()
                if player:
                    dl_path, external = await download_player_image(submission_type="pb", 
                                                    file_name=file_name, 
                                                    player=player,
                                                        attachment_url=attachment_url,
                                                        file_extension=file_extension,
                                                        entry_id=new_entry.id,
                                                        entry_name=current_time_ms,
                                                        npc_name=boss_name)
                    image_url = external
                    print("Generated image url", image_url)
            except Exception as e:
                print("Couldn't download the image:", e)
            if image_url:
                pb = session.query(PersonalBestEntry).filter(PersonalBestEntry.id == new_entry.id).first()
                pb.image_url = image_url
            session.commit()
        player: Player = session.query(Player).filter(Player.player_id == player_id).first()
        print("pb_processor - Checking if the player is in any groups.")
        player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
        player_group_ids = session.execute(text(player_group_id_query), {"player_id": player.player_id}).fetchall()
        player_group_ids = [group_id[0] for group_id in player_group_ids]
        player_group_ids.append(2)
        player_groups = session.query(Group).filter(Group.group_id.in_(player_group_ids)).all()
        print("Returned groups:", player_groups)
        if player_groups:
            print(player.player_name, "is in", len(player_groups), "groups")
            for group in player_groups:
                group: Group = group
                print("Checking group:", group.group_name)
                group_id = group.group_id
                send_pbs = session.query(GroupConfiguration.config_value).filter(GroupConfiguration.config_key == 'notify_pbs',
                                                                                    GroupConfiguration.group_id == group_id).first()
                formatted_name = None
                if type(player) == Player:
                    if player.user_id:
                        user: User = session.query(User).filter(User.user_id == player.user_id).first()
                        formatted_name = f"<@{user.discord_id}>"
                if (
                    send_pbs
                    and send_pbs[0]
                    # and ( TODO -- decide if we want to use patreon features again in the future
                    #     any(patreon.patreon_tier >= 2 for patreon in group.group_patreon)
                    #     or (group.date_added >= datetime.now() - timedelta(days=7))
                    # )
                ):
                    channel_id = session.query(GroupConfiguration.config_value).filter(GroupConfiguration.config_key == 'channel_id_to_post_pb',
                                                                                       GroupConfiguration.group_id == group_id).first()
                    print("Got the channel id:", channel_id)
                    channel_id = channel_id[0]
                    if group.wom_id:
                        wom_group_id = group.wom_id
                        # Fetch player WOM IDs and associated Player IDs
                        player_wom_ids = await fetch_group_members(wom_group_id)
                    else:
                        player_wom_ids = []
                        raw_wom_ids = session.query(Player.wom_id).all() ## get all users if no wom_group_id is found
                        for player in raw_wom_ids:
                            player_wom_ids.append(player[0])

                    player_ids = await associate_player_ids(player_wom_ids)
                    total_tracked = len(player_ids)
                    if type(npc_id) == int:
                        print(f"NPC ID is already an int: {npc_id}")
                    else:
                        try:
                            npc_id = npc_id[0]
                        except Exception as e:
                            print("Couldn't get the subscripted npc id:", e)
                    group_ranks = session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id.in_(player_ids), PersonalBestEntry.npc_id == int(npc_id),
                                                                          PersonalBestEntry.team_size == team_size).order_by(PersonalBestEntry.personal_best.asc()).all()
                    all_ranks = session.query(PersonalBestEntry).filter(PersonalBestEntry.npc_id == int(npc_id),
                                                                        PersonalBestEntry.team_size == team_size).order_by(PersonalBestEntry.personal_best.asc()).all()
                    print("Group ranks:",group_ranks)
                    print("All ranks:",all_ranks)
                    total_ranked_group = len(group_ranks)
                    total_ranked_global = len(all_ranks)
                    current_user_best_ms = personal_best_ms
                    ## player's rank in group
                    group_placement = None
                    global_placement = None
                    print("Assembling rankings....")
                    for idx, entry in enumerate(group_ranks, start=1): 
                        if entry.personal_best == current_user_best_ms:
                            group_placement = idx
                            break
                    ## player's rank globally
                    for idx, entry in enumerate(all_ranks, start=1):
                        if entry.personal_best == current_user_best_ms:
                            global_placement = idx
                            break
                    try:
                        channel = await bot.fetch_channel(channel_id=channel_id)
                        attachment = None
                        if dl_path:
                            attachment = interactions.File(dl_path)
                        values = {"{personal_best}": convert_from_ms(personal_best_ms),
                                "{kill_time}": convert_from_ms(current_time_ms),
                                "{is_new_pb}": "True" if is_new_pb else "False",
                                "{npc_name}": boss_name,
                                "{group_rank}": group_placement,
                                "{player_name}": formatted_name if formatted_name else player_name,
                                "{total_ranked_group}": total_ranked_group,
                                "{total_ranked_global}": total_ranked_global,
                                "{global_rank}": global_placement,
                                "{image_url}": image_url,
                                "{team_size}": team_size,
                                "{tracked_members}": total_tracked,
                                "{npc_id}": npc_id}
                        embed_template = await db.get_group_embed('pb', group_id)
                        if embed_template:
                            embed: interactions.Embed = replace_placeholders(embed_template,
                                                        values)
                            if group.group_id == 2:
                                temp_embed = interactions.Embed(title=embed.title, description=embed.description, color=embed.color)
                                temp_embed.set_thumbnail(embed.thumbnail.url)
                                temp_embed.set_footer(embed.footer.text)
                                for field in embed.fields:
                                    if field.name == "Group Stats":
                                        continue
                                    temp_embed.add_field(field.name, field.value, field.inline)
                                embed = temp_embed
                            embed.set_author(name="New Personal Best:")
                            if attachment:
                                message = await channel.send(f"{formatted_name if formatted_name else player_name} achieved a new PB:",
                                                    embed=embed,
                                                    files=attachment)
                            else:
                                message = await channel.send(f"{formatted_name if formatted_name else player_name} achieved a new PB:",
                                                    embed=embed)
                            if message:
                                message_id = str(message.id)
                                await logger.log("access", f"{player_name} achieved a new PB ({convert_from_ms(current_time_ms)} at {boss_name}) in {group_id}", "pb_processor")
                                pb = session.query(PersonalBestEntry).filter(PersonalBestEntry.id == new_entry.id).first()
                                notified_sub = NotifiedSubmission(channel_id=str(message.channel.id),
                                                                    message_id=message_id,
                                                                    group_id=group_id,
                                                                    status="sent",
                                                                    pb=pb)
                                session.add(notified_sub)
                                session.commit()
                        else:
                            print("No embed exists for the group with this type: 'pb'")
                    except Exception as e:
                        print("Couldn't send the personal best embed:", e)
        else:
            print("No groups found for the player...")                    


async def try_create_player(bot: interactions.Client, player_name, account_hash):
        account_hash = str(account_hash)
        if not account_hash or len(account_hash) < 5:
            return False # abort if no account hash was passed immediately
        player_name = player_name.replace("-", " ")
        player = session.query(Player).filter(Player.player_name == player_name).first()
        
        if not player:
            #print("Player not found in database, checking WOM...")
            wom_player, player_name, wom_player_id = await check_user_by_username(player_name)
            account_hash = str(account_hash)
            if not wom_player:
                pass
                #print("WOM player doesn't exist, and we can't update them/create them:", {player_name})
            elif not wom_player.latest_snapshot:
                #print(f"Failed to find or create player via WOM: {player_name}. Aborting.")
                return 
            player: Player = session.query(Player).filter(Player.wom_id == wom_player_id).first()
            if not player:
                #print("Player not found in database, checking account hash...")
                player: Player = session.query(Player).filter(Player.account_hash == account_hash).first()
            if player is not None:
                print("Player found in database:", player.player_name)
                if player_name != player.player_name:
                    old_name = player.player_name
                    player.player_name = player_name
                    session.commit()
                    if player.user:
                        user: User = player.user
                        user_discord_id = user.discord_id
                        if user_discord_id:
                            try:
                                user = await bot.fetch_user(user_id=user_discord_id)
                                if user:
                                    embed = interactions.Embed(title=f"Name change detected:",
                                                            description=f"Your account, {old_name}, has changed names to {player_name}.",
                                                            color="#00f0f0")
                                    embed.add_field(name=f"Is this a mistake?",
                                                    value=f"Reach out in [our discord](https://www.droptracker.io/discord)")
                                    embed.set_footer(global_footer)
                                    await user.send(f"Hey, <@{user.discord_id}>", embed=embed)
                            except Exception as e:
                                print("Couldn't DM the user on a name change:", e)
                    await name_change_message(bot, player_name, player.player_id, old_name)
            else:
                print("Player not found in database, creating new player...")
                try:
                    overall = wom_player.latest_snapshot.data.skills.get('overall')
                    total_level = overall.level
                except Exception as e:
                    print("Failed to get total level for player:", e)
                    total_level = 0
                new_player = Player(wom_id=wom_player_id, 
                                    player_name=player_name, 
                                    account_hash=account_hash, 
                                    total_level=total_level)
                session.add(new_player)
                await new_player_message(bot, player_name)
                session.commit()
                player_list[player_name] = new_player.player_id
                await logger.log("access", f"{player_name} has been created with ID {new_player.player_id} (hash: {account_hash}) ", "try_create_player")
                # await xf_api.try_create_xf_player(player_id=new_player.player_id,
                #                                   wom_id=new_player.wom_id,
                #                                   player_name=new_player.player_name,
                #                                   user_id=new_player.user_id,
                #                                   log_slots=0,
                #                                   total_level=total_level,
                #                                   xf_user_id=new_player.user.xf_user_id if new_player.user else None)
                return new_player
        else:
            stored_account_hash = player.account_hash
            if str(stored_account_hash) != account_hash:
                print("Potential fake submission from", player_name + " with a changed account hash!!")
            player_list[player_name] = player.player_id


def check_verif_user(account_hash: str):
    """
        Checks if the user has an account in the database.
        In the case they do, it ignores their drops if 
        they have an 'auth' key that is valid in the user configs.
    """
    player = session.query(Player).filter(Player.account_hash == account_hash).first()
    if player:
        if player.user:
            user: User = player.user
            stored_auth = session.query(UserConfiguration.config_value).filter(UserConfiguration.user_id == user.user_id,
                                                                            UserConfiguration.config_key == 'auth').first()
            if stored_auth[0] and stored_auth[0] != 'false':
                return True
    return False