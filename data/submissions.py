import asyncio
import hashlib
import time
from db.models import CombatAchievementEntry, Drop, NotifiedSubmission, session, NpcList, Player, ItemList, PersonalBestEntry, CollectionLogEntry, User, Group, GroupConfiguration, UserConfiguration, NotificationQueue
from db import models
from db.update_player_total import update_player_in_redis
from db.xf.recent_submissions import create_xenforo_entry
from utils.embeds import update_boss_pb_embed
from utils.messages import confirm_new_npc, confirm_new_item, name_change_message, new_player_message
from utils.msg_logger import HighThroughputLogger
from utils.semantic_check import check_item_exists, get_current_ca_tier, get_ca_tier_progress, get_item_id, get_npc_id
from utils.wiseoldman import check_user_by_id, check_user_by_username, check_group_by_id, fetch_group_members, get_collections_logged
from utils.redis import RedisClient
from db.ops import DatabaseOperations, associate_player_ids
from utils.download import download_player_image, download_image
from sqlalchemy import func, text
from utils.format import format_number, get_command_id, get_extension_from_content_type, replace_placeholders, convert_from_ms
import interactions
from utils.logger import LoggerClient
from utils.semantic_check import check_drop as verify_item_real
from db.app_logger import AppLogger
from dotenv import load_dotenv
import os
import json
from datetime import datetime, timedelta
from sqlalchemy.engine import Row  # Add this import at the top
app_logger = AppLogger()

# last_processor_run_at = datetime.now()
# last_processor_run = None
"""

    Processes drops from the API endpoint and Discord Webhook endpoints

"""
load_dotenv()
debug_level = os.getenv('DEBUG_LEVEL', "info")

debug = debug_level != "false"

def debug_print(message, **kwargs):
    if debug:
        print(message, **kwargs)

global_footer = os.getenv('DISCORD_MESSAGE_FOOTER')
redis_client = RedisClient()
db = DatabaseOperations()

last_channels_sent = []


# @interactions.Task.create(interactions.IntervalTrigger(seconds=20))
# async def check_last_processor_run():
#     global last_processor_run_at, last_processor_run
#     if last_processor_run_at:
#         if last_processor_run_at < datetime.now() - timedelta(seconds=20):
#             notification_data = {
#                 'last_processor_run': last_processor_run,
#                 'last_processor_run_at': str(last_processor_run_at),
#                 'current_time': str(datetime.now()),
#                 'time_since_last_run': str(datetime.now() - last_processor_run_at)
#             }
#             await create_notification('processor_lag', 0, notification_data)
#             ## Force restart the bot
#             await asyncio.sleep(1)
#             await run_restart()
            
async def run_restart():
    script = "./restart.sh"
    try:
        await asyncio.create_subprocess_shell(script)
    except Exception as e:
        debug_print(f"Error running restart script: {e}")

npc_list = {} # - stores a dict of the npc's and their corresponding IDs to prevent excessive querying
player_list = {} # - stores a dict of player name:ids, and their last refresh from the DB.
class RawDropData():
    def __init__(self) -> None:
        pass

def check_auth(player_name, account_hash, auth_key, external_session=None):
    """
    Returns true, true if there is a matching player+account_hash combo.
    Returns true, false if player exists but hash doesn't match.
    Returns false, false if player does not exist.
    """
    use_external_session = external_session is not None
    if use_external_session:
        session = external_session
    else:
        session = session
    try:
        player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        
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
                app_logger.log_sync(log_type="access", data=f"Player {player_name} already exists with account hash {account_hash}, updating player name to {player_name}", app_name="core", description="check_auth")
                try:
                    session.commit()
                except Exception as e:
                    debug_print("Error committing player name change:" + str(e))
                    session.rollback()
            player.account_hash = account_hash
            try:
                session.commit()
            except Exception as e:
                debug_print("Error committing player name change:" + str(e))
                session.rollback()  
            return True, True
    except Exception as e:
        debug_print("Error checking auth:" + str(e))
        return False, False

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
            stored_auth = session.query(UserConfiguration.config_value).filter(
                UserConfiguration.user_id == user.user_id,
                UserConfiguration.config_key == 'auth'
            ).first()
            if stored_auth and stored_auth[0] and stored_auth[0] != 'false':
                return True
    return False

async def drop_processor(drop_data: RawDropData, external_session=None):
    """Process a drop submission and create notification entries if needed"""
    # Use provided session or create a new one
    session = models.session
    use_external_session = external_session is not None
    if use_external_session:
        session = external_session
    else:
        session = session
    try:
        npc_name = drop_data.get('source', drop_data.get('npc_name', None))
        value = drop_data['value']
        item_id = drop_data.get('item_id', drop_data.get('id', None))
        item_name = drop_data.get('item_name', drop_data.get('item', None))
        quantity = drop_data['quantity']
        auth_key = drop_data.get('auth_key', None)
        player_name = drop_data.get('player_name', drop_data.get('player', None))
        account_hash = drop_data['acc_hash']
        player_name = str(player_name).strip()
        account_hash = str(account_hash)
        downloaded = drop_data.get('downloaded', False)
        image_url = drop_data.get('image_url', None)
        
        item = session.query(ItemList).filter(ItemList.item_id == item_id).first()
        if not item:
            try:
                real_item = await check_item_exists(item_name)
                if real_item:
                    item = ItemList(item_name=item_name, item_id=item_id, noted=0, stackable=0, stacked=0)
                    session.add(item)
                    session.commit()
            except Exception as e:
                debug_print(f"Item {item_name} not found in database, aborting")
                return
        item_id = item.item_id
        
        authed = False
        player: Player = session.query(Player).filter(Player.account_hash == account_hash).first()
        if not player:
            player = await create_player(player_name, account_hash, existing_session=session)
            if not player:
                debug_print("Player not found in the database")
                return
        player_list[player_name] = player.player_id
        user_exists, authed = check_auth(player_name, account_hash, auth_key, session)
        if not user_exists or not authed:
            debug_print(player_name + " failed auth check")
            return
        
        if npc_name in npc_list:
            npc_id = npc_list[npc_name]
        else:
            npc = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc_name).first()
            if not npc:
                npc_id = None
                npc_obj = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc).first()
                if not npc_obj:
                    try:
                        npc_id = await get_npc_id(npc)
                        if npc_id:
                            npc = NpcList(npc_id=npc_id, npc_name=npc)
                            session.add(npc)
                            session.commit()
                        npc_list[npc_name] = npc.npc_id
                    except Exception as e:
                        debug_print(f"NPC {npc} not found in database, aborting")
                        return
                if npc_name not in npc_list:
                    debug_print(f"NPC {npc} not found in database")    
                    notification_data = {
                        'npc_name': npc_name,
                        'player_name': player_name,
                        'player_id': player_list[player_name]
                    }
                    await create_notification('new_npc', player_list[player_name], notification_data, existing_session=session if use_external_session else None)
                return
            else:
                npc_list[npc_name] = npc.npc_id
                npc_id = npc.npc_id
        
        player_id = player_list[player_name]
        item = redis_client.get(item_id)
        if not item:
            item = session.query(ItemList.item_id).filter(ItemList.item_id == item_id).first()
        if item:
            redis_client.set(item_id, item[0])
        else:
            # Create notification for new item
            notification_data = {
                'item_name': item_name,
                'player_name': player_name,
                'item_id': item_id,
                'npc_name': npc_name,
                'value': value
            }
            
            await create_notification('new_item', player_id, notification_data, existing_session=session if use_external_session else None)
            debug_print(f"Item not found...", item_id, item_name)
            return
        
        drop_value = int(value) * int(quantity)
        debug_print(f"Drop value: {drop_value}")
        if drop_value > 1000000:
            is_from_npc = await verify_item_real(item_name, npc_name)
            if not is_from_npc:
                return
        # Process attachment
        if drop_data.get('attachment_type', None) is not None:
            attachment_url = drop_data.get('attachment_url', None)
            attachment_type = drop_data.get('attachment_type', None)
        else:
            attachment_url = image_url
            attachment_type = None
        debug_print("Creating drop object")
        # Create the drop in database
        drop = await db.create_drop_object(
            item_id=item_id,
            player_id=player_id,
            date_received=datetime.now(),
            npc_id=npc_id,
            value=int(value),
            quantity=int(quantity),
            image_url="" if attachment_url else None,
            authed=authed,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
            existing_session=session if use_external_session else None
        )
        
        
        if not drop:
            debug_print("Failed to create drop")
            return
        try:
            debug_print("Updating player in redis")
            update_player_in_redis(player_id, session, force_update=False, batch_drops=[drop], from_submission=True)
        except Exception as e:
            debug_print(f"Error updating player in redis: {e}")
            session.rollback()
            return
        # Get player groups and check if notification is needed
        debug_print("Getting player groups")
        global_group = session.query(Group).filter(Group.group_id == 2).first()
        if global_group not in player.groups:
            player.add_group(global_group)
            session.commit()
        player_groups = session.query(Group).join(Group.players).filter(Player.player_id == player_id).all()
        debug_print(f"Player groups: {player_groups}")
        if 2 not in [group.group_id for group in player_groups]:
            global_group = session.query(Group).filter(Group.group_id == 2).first()
            player_groups.append(global_group)
        for group in player_groups:
            group_id = group.group_id
            
            # Get minimum value to notify for this group
            min_value_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'minimum_value_to_notify'
            ).first()
            
            min_value_to_notify = int(min_value_config.config_value) if min_value_config else 2500000
            
            # Check if drop value exceeds minimum for notification
            if drop_value >= min_value_to_notify:
                # Create notification entry
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
                if player:
                    if player.user:
                        user = session.query(User).filter(User.user_id == player.user_id).first()
                        if user:
                            should_dm_cfg = session.query(UserConfiguration).filter(UserConfiguration.user_id == user.user_id,
                                                                                    UserConfiguration.config_key == 'dm_drops').first()
                            if should_dm_cfg:
                                should_dm = should_dm_cfg.config_value
                                should_dm = str(should_dm).lower()
                                if should_dm == "true" or should_dm == "1":
                                    should_dm = True
                                else:
                                    should_dm = False
                                if should_dm:
                                    await create_notification('dm_drop', player_id, notification_data, group_id, existing_session=session if use_external_session else None)
                await create_xenforo_entry(drop=drop, clog=None, personal_best=None, combat_achievement=None)
                await create_notification('drop', player_id, notification_data, group_id, existing_session=session if use_external_session else None)
                debug_print(f"Drop created for {player_name} in group {group_id}")
        
        # At the end of the function, commit if we created our own session
        if not use_external_session:
            session.commit()
            
        return drop
        
    except Exception as e:
        # Roll back if we created our own session
        if not use_external_session:
            session.rollback()
        debug_print(f"Error in drop_processor: {e}")
        raise

async def create_player(player_name, account_hash, existing_session=None):
    
    """Create a player without Discord-specific functionality"""
    use_existing_session = existing_session is not None
    session = models.session
    if use_existing_session:
        session = existing_session
    else:
        session = session
    account_hash = str(account_hash)
    if not account_hash or len(account_hash) < 5:
        debug_print("Account hash is too short, aborting")
        return False
    
    player = session.query(Player).filter(Player.player_name == player_name).first()
    
    if not player:
        wom_player, player_name, wom_player_id, log_slots = await check_user_by_username(player_name)
        account_hash = str(account_hash)
        
        if not wom_player:
            return None
        
        player: Player = session.query(Player).filter(Player.wom_id == wom_player_id).first()
        if not player:
            debug_print("Still no player after wom_id check")
            player: Player = session.query(Player).filter(Player.account_hash == account_hash).first()
        
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
                if player:
                    if player.user:
                        user = session.query(User).filter(User.user_id == player.user_id).first()
                        if user:
                            should_dm_cfg = session.query(UserConfiguration).filter(UserConfiguration.user_id == user.user_id,
                                                                                    UserConfiguration.config_key == 'dm_account_changes').first()
                            if should_dm_cfg:
                                should_dm = should_dm_cfg.config_value
                                should_dm = str(should_dm).lower()
                                if should_dm == "true" or should_dm == "1":
                                    should_dm = True
                                else:
                                    should_dm = False
                                if should_dm:
                                    await create_notification('dm_name_change', player.player_id, notification_data, existing_session=session if use_existing_session else None)
                
                await create_notification('name_change', player.player_id, notification_data, existing_session=session if use_existing_session else None)
        else:
            debug_print(f"We found the player, updating their data")
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
            
            player_list[player_name] = new_player.player_id
            app_logger.log(log_type="access", data=f"{player_name} has been created with ID {new_player.player_id} (hash: {account_hash}) ", app_name="core", description="create_player")
            
            # Create new player notification
            notification_data = {
                'player_name': player_name
            }
            await create_notification('new_player', new_player.player_id, notification_data, existing_session=session if use_existing_session else None)
            
            return new_player
    else:
        stored_account_hash = player.account_hash
        if str(stored_account_hash) != account_hash:
            debug_print("Potential fake submission from" + player_name + " with a changed account hash!!")
        player_list[player_name] = player.player_id
    
    return player

stored_notifications = []

async def create_notification(notification_type, player_id, data, group_id=None, existing_session=None):
    """Create a notification queue entry"""
    global stored_notifications
    if len(stored_notifications) > 100:
        while len(stored_notifications) > 100:
            stored_notifications.pop()
    use_existing_session = existing_session is not None
    session = models.session
    if use_existing_session:
        session = existing_session
    else:
        session = session
    hashed_data = hashlib.sha256(json.dumps(data).encode()).hexdigest()
    if hashed_data in stored_notifications:
        ## This group notification already got created ...
        return
    stored_notifications.append(hashed_data)
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

async def clog_processor(clog_data, external_session=None):
    print("clog_processor")
    """Process a collection log submission and create notification entries if needed"""
    player_name = clog_data.get('player_name', clog_data.get('player', None))
    session = models.session
    use_external_session = external_session is not None
    if use_external_session:
        session = external_session
    else:
        session = session
    if not player_name:
        print("No player name found, aborting")
        return
    has_xf_entry = False

    account_hash = clog_data['acc_hash']
    item_name = clog_data.get('item_name', clog_data.get('item', None))
    if not item_name:
        print("No item name found, aborting")
        return
    auth_key = clog_data.get('auth_key', '')
    attachment_url = clog_data.get('attachment_url', None)
    attachment_type = clog_data.get('attachment_type', None)
    reported_slots = clog_data.get('reported_slots', None)
    downloaded = clog_data.get('downloaded', False)
    image_url = clog_data.get('image_url', None)

    killcount = clog_data.get('kc', None)       
    item = session.query(ItemList).filter(ItemList.item_name == item_name).first()
    if not item:
        try:
            item_id = await get_item_id(item_name)
            if item_id:
                item = ItemList(item_name=item_name, item_id=item_id, noted=0, stackable=0, stacked=0)
                session.add(item)
                session.commit()
        except Exception as e:
            print(f"Item {item_name} not found in database, aborting")
            return
    item_id = item.item_id
    npc_name = clog_data.get('source', None)
    npc = npc_name
    print(f"NPC: {npc}")
    npc_id = None
    if player_name not in player_list:
        player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if not player:
            # Create player without Discord-specific code
            player = await create_player(player_name, account_hash, existing_session=session)
            if not player:
                print(f"Player does not exist, and creating failed")
                return
            player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if player:
            player_list[player_name] = player.player_id
        else:
            return
    player_id = player_list[player_name]
    try:
        if npc:
            npc_obj = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc).first()
            if not npc_obj:
                try:
                    npc_id = await get_npc_id(npc)
                    if npc_id:
                        npc = NpcList(npc_id=npc_id, npc_name=npc)
                        session.add(npc)
                        session.commit()
                    npc_list[npc_name] = npc.npc_id
                except Exception as e:
                    print(f"NPC {npc} not found in database, aborting")
                    return
            if npc_name not in npc_list:
                print(f"NPC {npc} not found in database")    
                notification_data = {
                    'npc_name': npc_name,
                    'player_name': player_name,
                    'player_id': player_list[player_name]
                }
                await create_notification('new_npc', player_list[player_name], notification_data, existing_session=session if use_external_session else None)
        npc_id = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc).first()
        npc_id = npc_id.npc_id if npc_id else None
        if npc_id is None:
            print(f"NPC not able to be found in the database.")
        
    except Exception as e:
        print(f"Error processing clog: {e}")
        session.rollback()
        return
    # Validate player
    
    
    # Get the player object for image download
    player = session.query(Player).filter(Player.player_id == player_id).first()
    if not player:
        print("Player not found in database, aborting")
        return
        
    user_exists, authed = check_auth(player_name, account_hash, auth_key, session)
    if not user_exists or not authed:
        print("user failed auth check")
        return
        
    # Check if collection log entry already exists
    clog_entry = session.query(CollectionLogEntry).filter(
        CollectionLogEntry.player_id == player_id,
        CollectionLogEntry.item_id == item_id
    ).first()
    
    is_new_clog = False
    if npc_id is None:
        print(f"We did not find an npc for {npc_name}, aborting")
        return
    if not clog_entry:
        # Create new collection log entry
        clog_entry = CollectionLogEntry(
            player_id=player_id,
            reported_slots=reported_slots,
            item_id=item_id,
            npc_id=npc_id,
            date_added=datetime.now(),
            image_url=""
        )
        session.add(clog_entry)
        session.commit()  # Commit to get the log_id
        
        # Process image if available
        dl_path = ""
        if attachment_url and not downloaded:
            try:
                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"clog_{player_id}_{item_name.replace(' ', '_')}_{int(time.time())}"
                
                dl_path, external_url = await download_player_image(
                    submission_type="clog",
                    file_name=file_name,
                    player=player,  # Now player is defined
                    attachment_url=attachment_url,
                    file_extension=file_extension,
                    entry_id=clog_entry.log_id,
                    entry_name=item_name
                )
                
                # Update the image URL
                clog_entry.image_url = dl_path if dl_path else ""
            except Exception as e:
                app_logger.log(log_type="error", data=f"Couldn't download collection log image: {e}", app_name="core", description="clog_processor")
        elif downloaded:
                clog_entry.image_url = image_url
        
        is_new_clog = True
        print("Added clog to session")
    
    print("Committing session")
    session.commit()
    
    # Create notification if it's a new collection log entry
    if is_new_clog:
        print("New collection log -- Creating notification")
        # Get player groups
        global_group = session.query(Group).filter(Group.group_id == 2).first()
        if global_group not in player.groups:
            player.add_group(global_group)
            session.commit()
        player_groups = session.query(Group).join(Group.players).filter(Player.player_id == player_id).all()
        for group in player_groups:
            group_id = group.group_id
            
            
            # Check if group has collection log notifications enabled
            clog_notify_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'notify_clogs'
            ).first()
            
            if clog_notify_config and clog_notify_config.config_value.lower() == 'true' or int(clog_notify_config.config_value) == 1 or group_id == 2:
                notification_data = {
                    'player_name': player_name,
                    'player_id': player_id,
                    'item_name': item_name,
                    'npc_name': npc,
                    'image_url': clog_entry.image_url,
                    'kc_received': killcount,
                    'item_id': item_id
                }
                print("Creating notification")
                if not has_xf_entry:
                    await create_xenforo_entry(drop=None, clog=clog_entry, personal_best=None, combat_achievement=None)
                    has_xf_entry = True
                await create_notification('clog', player_id, notification_data, group_id, existing_session=session if use_external_session else None)
        if player:
            if player.user:
                user = session.query(User).filter(User.user_id == player.user_id).first()
                if user:
                    should_dm_cfg = session.query(UserConfiguration).filter(UserConfiguration.user_id == user.user_id,
                                                                            UserConfiguration.config_key == 'dm_clogs').first()
                    if should_dm_cfg:
                        should_dm = should_dm_cfg.config_value
                        should_dm = str(should_dm).lower()
                        if should_dm == "true" or should_dm == "1":
                            should_dm = True
                        else:
                            should_dm = False
                        if should_dm:
                            await create_notification('dm_clog', player_id, notification_data, group_id, existing_session=session if use_external_session else None)
                
    print("Returning clog entry") 
    
    return clog_entry

async def ca_processor(ca_data, external_session=None):
    debug_print("ca_processor")
    """Process a combat achievement submission and create notification entries if needed"""
    # global last_processor_run_at, last_processor_run
    has_xf_entry = False
    session = models.session
    use_external_session = external_session is not None
    if use_external_session:
        session = external_session
    else:
        session = session
    player_name = ca_data['player_name']
    account_hash = ca_data['acc_hash']
    points_awarded = ca_data['points']
    points_total = ca_data['total_points']
    completed_tier = ca_data['completed']
    task_name = ca_data['task']
    tier = ca_data['tier']
    auth_key = ca_data.get('auth_key', '')
    attachment_url = ca_data.get('attachment_url', None)
    attachment_type = ca_data.get('attachment_type', None)
    downloaded = ca_data.get('downloaded', False)
    image_url = ca_data.get('image_url', None)
    # Validate player
    if player_name not in player_list:
        player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if not player:
            # Create player without Discord-specific code
            player = await create_player(player_name, account_hash, existing_session=session)
            if not player:
                debug_print("Player still not found in the database, aborting")
                return
            player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if player:
            player_list[player_name] = player.player_id
        else:
            debug_print("Player still not found in the database, aborting")
            return
    
    player_id = player_list[player_name]
    user_exists, authed = check_auth(player_name, account_hash, auth_key, session)
    if not user_exists or not authed:
        debug_print("User failed auth check")
        return
    # Check if CA entry already exists
    ca_entry = session.query(CombatAchievementEntry).filter(
        CombatAchievementEntry.player_id == player_id,
        CombatAchievementEntry.task_name == task_name
    ).first()
    
    is_new_ca = False
    
    if not ca_entry:
        
        debug_print("CA entry not found in the database, creating new entry - Task tier: " + str(tier))
        dl_path = ""
        ca_entry = CombatAchievementEntry(
            player_id=player_id,
            task_name=task_name,
            date_added=datetime.now(),
            image_url=dl_path
        )
        session.add(ca_entry)
        is_new_ca = True
        # Process image if available
        if attachment_url and not downloaded:
            try:
                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"ca_{player_id}_{task_name.replace(' ', '_')}_{int(time.time())}"
                player = session.query(Player).filter(Player.player_id == player_id).first()
                if not player:
                    debug_print("Player not found in database, aborting")
                    return
                dl_path, external_url = await download_player_image(
                    submission_type="ca",
                    file_name=file_name,
                    player=player,
                    attachment_url=attachment_url,
                    file_extension=file_extension,
                    entry_id=ca_entry.id,
                    entry_name=task_name
                )
                
                if dl_path:
                    ca_entry.image_url = dl_path
            except Exception as e:
                app_logger.log(log_type="error", data=f"Couldn't download CA image: {e}", app_name="core", description="ca_processor")
        
    session.commit()
    debug_print("Committed a new CA entry")
    # Create notification if it's a new CA
    if is_new_ca:
        debug_print("New CA entry, creating notification")
        # Get player groups
        global_group = session.query(Group).filter(Group.group_id == 2).first()
        if global_group not in player.groups:
            player.add_group(global_group)
            session.commit()
        player_groups = session.query(Group).join(Group.players).filter(Player.player_id == player_id).all()
        debug_print("Player groups: " + str(player_groups))
        for group in player_groups:
            debug_print("Checking group: " + str(group))
            group_id = group.group_id
            
            
            # Check if group has CA notifications enabled
            ca_notify_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'notify_cas'
            ).first()
            debug_print("CA notify config: " + str(ca_notify_config.config_value))
            if ca_notify_config and ca_notify_config.config_value.lower() == 'true' or ca_notify_config.config_value == '1':
                # Check if tier meets minimum notification tier
                min_tier = session.query(GroupConfiguration.config_value).filter(GroupConfiguration.config_key == 'min_ca_tier_to_notify',
                                                                            GroupConfiguration.group_id == group_id).first()
                tier_order = ['easy', 'medium', 'hard', 'elite', 'master', 'grandmaster']
                if min_tier != "disabled" or group_id == 2:
                    if (min_tier and min_tier[0].lower() in tier_order) or group_id == 2:
                        min_tier_value = min_tier[0].lower()
                        min_tier_index = tier_order.index(min_tier_value)
                        
                        # Check if the current task's tier meets the minimum requirement
                        task_tier_index = tier_order.index(tier.lower()) if tier.lower() in tier_order else -1
                        
                        if task_tier_index < min_tier_index:
                            # Task tier is below the minimum required tier, skip processing
                            debug_print(f"Skipping {task_name} ({tier}) as it's below minimum tier {min_tier_value} for group {group_id}")
                            continue
                        else:
                            debug_print("Tier meets minimum notification tier")
                            notification_data = {
                                'player_name': player_name,
                                'player_id': player_id,
                                'task_name': task_name,
                                'tier': tier,
                                'points_awarded': points_awarded,
                                'points_total': points_total,
                                'completed_tier': completed_tier,
                                'image_url': ca_entry.image_url
                            }
                            if not has_xf_entry:
                                try:
                                    await create_xenforo_entry(drop=None, clog=None, personal_best=None, combat_achievement=ca_entry)
                                    has_xf_entry = True
                                except Exception as e:
                                    debug_print(f"Couldn't add CA to XenForo: {e}")
                                    app_logger.log(log_type="error", data=f"Couldn't add CA to XenForo: {e}", app_name="core", description="ca_processor")
                            if player:
                                if player.user:
                                    user = session.query(User).filter(User.user_id == player.user_id).first()
                                    if user:
                                        should_dm_cfg = session.query(UserConfiguration).filter(UserConfiguration.user_id == user.user_id,
                                                                                                UserConfiguration.config_key == 'dm_cas').first()
                                        if should_dm_cfg:
                                            should_dm = should_dm_cfg.config_value
                                            should_dm = str(should_dm).lower()
                                            if should_dm == "true" or should_dm == "1":
                                                should_dm = True
                                            else:
                                                should_dm = False
                                            if should_dm:
                                                await create_notification('dm_ca', player_id, notification_data, group_id, existing_session=session if use_external_session else None)
                            await create_notification('ca', player_id, notification_data, group_id, existing_session=session if use_external_session else None)
    
    return ca_entry

async def pb_processor(pb_data, external_session=None):
    debug_print("pb_processor")
    """Process a personal best submission and create notification entries if needed"""
    session = models.session
    use_external_session = external_session is not None
    if use_external_session:
        session = external_session
    else:
        session = session
    player_name = pb_data['player_name']
    account_hash = pb_data['acc_hash']
    boss_name = pb_data['npc_name']
    current_ms = pb_data.get('current_time_ms', 0)
    pb_ms = pb_data.get('personal_best_ms', 0)
    team_size = pb_data.get('team_size', 1)
    is_personal_best = pb_data.get('is_new_pb', False)
    time_ms = current_ms if current_ms < pb_ms and current_ms != 0 else (pb_ms if pb_ms != 0 else current_ms)
    auth_key = pb_data.get('auth_key', '')
    attachment_url = pb_data.get('attachment_url', None)
    attachment_type = pb_data.get('attachment_type', None)
    downloaded = pb_data.get('downloaded', False)
    image_url = pb_data.get('image_url', None)
    player = None
    has_xf_entry = False
    print("Raw pb data: " + str(pb_data))
    dl_path = None
    npc: NpcList = session.query(NpcList.npc_id).filter(NpcList.npc_name == boss_name).first()
    npc_name = boss_name
    if npc_name in npc_list:
        npc_id = npc_list[npc_name]
    else:
        npc = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc_name).first()
        if not npc:
            npc_id = None
            npc_obj = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc).first()
            if not npc_obj:
                try:
                    npc_id = await get_npc_id(npc)
                    if npc_id:
                        npc = NpcList(npc_id=npc_id, npc_name=npc)
                        session.add(npc)
                        session.commit()
                    npc_list[npc_name] = npc.npc_id
                except Exception as e:
                    debug_print(f"NPC {npc} not found in database, aborting")
                    return
            if npc_name not in npc_list:
                debug_print(f"NPC {npc} not found in database")    
                notification_data = {
                    'npc_name': npc_name,
                    'player_name': player_name,
                    'player_id': player_list[player_name]
                }
                await create_notification('new_npc', player_list[player_name], notification_data, existing_session=session if use_external_session else None)
            return
        else:
            npc_list[npc_name] = npc.npc_id
            npc_id = npc.npc_id
    # Validate player
    if player_name not in player_list:
        player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if not player:
            # Create player without Discord-specific code
            player = await create_player(player_name, account_hash, existing_session=session)
            if not player:
                return
            player: Player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
        if player:
            player_list[player_name] = player.player_id
        else:
            return
    
    player_id = player_list[player_name]
    user_exists, authed = check_auth(player_name, account_hash, auth_key, session)
    
    # Create or update PB entry
    pb_entry = session.query(PersonalBestEntry).filter(
        PersonalBestEntry.player_id == player_id,
        PersonalBestEntry.npc_id == npc_id,
        PersonalBestEntry.team_size == team_size
    ).first()
    
    is_new_pb = False
    old_time = None
    
    
    
    # Process image if available
    if is_personal_best:
        print("Is personal best, processing image")
        if attachment_url and not downloaded:
            try:
                file_extension = get_extension_from_content_type(attachment_type)
                file_name = f"pb_{player_id}_{boss_name.replace(' ', '_')}_{int(time.time())}"
                
                dl_path, external_url = await download_player_image(
                    submission_type="pb",
                    file_name=file_name,
                    player=player,
                    attachment_url=attachment_url,
                    file_extension=file_extension,
                    entry_id=pb_entry.id,
                    entry_name=boss_name
                )
                
                if dl_path:
                    pb_entry.image_url = dl_path
                    session.commit()
            except Exception as e:
                print(f"Couldn't download PB image: {e}")
                app_logger.log(log_type="error", data=f"Couldn't download PB image: {e}", app_name="core", description="pb_processor")
        elif downloaded:
            dl_path = image_url
    if pb_entry:
        print("PB entry found, processing")
        if pb_entry.personal_best < time_ms:
            old_time = pb_entry.personal_best
            pb_entry.personal_best = time_ms  
            pb_entry.new_pb=is_personal_best
            pb_entry.kill_time = current_ms
            pb_entry.date_added = datetime.now()
            pb_entry.image_url = dl_path if dl_path else ""
            is_new_pb = True
    else:
        print("PB entry not found, creating new entry")
        pb_entry = PersonalBestEntry(
            player_id=player_id,
            npc_id=npc_id,
            team_size=team_size,
            new_pb=is_personal_best,
            personal_best=time_ms,
            kill_time=current_ms,
            date_added=datetime.now(),
            image_url=dl_path if dl_path else ""
        )
        session.add(pb_entry)
    
    session.commit()
    print("Committed PB entry - personal best: " + str(is_personal_best))
    # Create notification if it's a new PB
    if is_personal_best:
        print("Is personal best, creating notification")
        # Get player groups
        global_group = session.query(Group).filter(Group.group_id == 2).first()
        if player is None or not player:
            player = session.query(Player).filter(Player.account_hash == account_hash).first()
        if not player:
            return
        if global_group not in player.groups:
            player.add_group(global_group)
            session.commit()
        player_groups = session.query(Group).join(Group.players).filter(Player.player_id == player_id).all()
        for group in player_groups:
            group_id = group.group_id
            print("Checking group: " + str(group))
            
            # Check if group has PB notifications enabled
            pb_notify_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'notify_pbs'
            ).first()
            print("PB notify config: " + str(pb_notify_config))
            if pb_notify_config and pb_notify_config.config_value.lower() == 'true' or int(pb_notify_config.config_value) == 1:
                notification_data = {
                    'player_name': player_name,
                    'player_id': player_id,
                    'npc_id': npc_id,
                    'boss_name': boss_name,
                    'time_ms': time_ms,
                    'old_time_ms': old_time,
                    'team_size': team_size,
                    'kill_time_ms': current_ms,
                    'image_url': pb_entry.image_url
                }
                print("Creating notification")
                ## Check if we should send a notification for this npc
                await create_notification('pb', player_id, notification_data, group_id, existing_session=session if use_external_session else None  )
                if not has_xf_entry:
                    await create_xenforo_entry(drop=None, clog=None, personal_best=pb_entry, combat_achievement=None)
                    has_xf_entry = True
                if player:
                    if player.user:
                        user = session.query(User).filter(User.user_id == player.user_id).first()
                        if user:
                            should_dm_cfg = session.query(UserConfiguration).filter(UserConfiguration.user_id == user.user_id,
                                                                                    UserConfiguration.config_key == 'dm_pbs').first()
                            if should_dm_cfg:
                                should_dm = should_dm_cfg.config_value
                                should_dm = str(should_dm).lower()
                                if should_dm == "true" or should_dm == "1":
                                    should_dm = True
                                else:
                                    should_dm = False
                                if should_dm:
                                    await create_notification('dm_pb', player_id, notification_data, group_id, existing_session=session if use_external_session else None)
                
    
    return pb_entry

async def try_create_player(bot: interactions.Client, player_name, account_hash):
        account_hash = str(account_hash)
        if not account_hash or len(account_hash) < 5:
            return False # abort if no account hash was passed immediately
        #player_name = player_name.replace("-", " ")
        player = session.query(Player).filter(Player.player_name == player_name).first()
        
        if not player:
            #print("Player not found in database, checking WOM...")
            wom_player, player_name, wom_player_id, log_slots = await check_user_by_username(player_name)
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
                if player_name != player.player_name:
                    old_name = player.player_name
                    player.player_name = player_name
                    player.log_slots = log_slots
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
                                debug_print("Couldn't DM the user on a name change:" + str(e))
                    await name_change_message(bot, player_name, player.player_id, old_name)
            else:
                debug_print("Player not found in database, creating new player..." + str(e))
                try:
                    overall = wom_player.latest_snapshot.data.skills.get('overall')
                    total_level = overall.level
                except Exception as e:
                    #print("Failed to get total level for player:", e)
                    total_level = 0
                new_player = Player(wom_id=wom_player_id, 
                                    player_name=player_name, 
                                    account_hash=account_hash, 
                                    total_level=total_level,
                                    log_slots=log_slots)
                session.add(new_player)
                await new_player_message(bot, player_name)
                session.commit()
                player_list[player_name] = new_player.player_id
                app_logger.log(log_type="access", data=f"{player_name} has been created with ID {new_player.player_id} (hash: {account_hash}) ", app_name="core", description="try_create_player")
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
                debug_print("Potential fake submission from " + player_name + " with a changed account hash!!")
            player_list[player_name] = player.player_id


