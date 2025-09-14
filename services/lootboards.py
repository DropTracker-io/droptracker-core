

from interactions import Extension, Client, IntervalTrigger, Task
from db.models import Group, GroupConfiguration, Player, Session
import asyncio
import os
import interactions
from datetime import datetime, timedelta
import time
from utils.format import replace_placeholders
from db.ops import DatabaseOperations

class LootboardServices(Extension):
    def __init__(self, bot: Client):
        self.bot = bot
        print("Lootboard services initialized.")
        self.db = DatabaseOperations()
        self._processing_lock = asyncio.Lock()
        asyncio.create_task(self.lootboard_updates())

    async def lootboard_updates(self):
        try:
            if hasattr(self, "_processing_lock") and self._processing_lock.locked():
                print("Lootboard update still running, skipping this interval.")
                return
            if hasattr(self, "_processing_lock"):
                await self._processing_lock.acquire()
            print("Updating loot leaderboards...")
            session = None
            try:
                session = Session()
                all_groups = session.query(Group).all()
                groups_to_update = {}
                for group in all_groups:
                    group_id = group.group_id
                    configured_channel = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'lootboard_channel_id').first()
                    configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'lootboard_message_id').first()
                    should_repost = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'repost_lootboard').first()
                    if configured_channel and configured_message:
                        if configured_channel.config_value:
                            groups_to_update[group_id] = {"wom_id": group.wom_id,
                                                        "channel": configured_channel.config_value,
                                                        "message": configured_message.config_value,
                                                        "repost": should_repost.config_value}
                
                for group_id, group in groups_to_update.items():
                    try:
                        channel: interactions.Channel = await self.bot.fetch_channel(channel_id=group['channel'])
                        if not channel:
                            #print(f"Channel with id {group['channel']} not found on discord for group {group_id} ({group_obj.group_name}).")
                            continue
                        message_to_update = None
                        group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                        
                        # Check if we should repost (create new message) or edit existing
                        should_repost_value = group['repost'] if group['repost'] else "false"
                        repost_enabled = should_repost_value.lower() in ['true', '1', 'yes', 'on']
                        
                        if repost_enabled:
                            # Check if there's an existing message to delete first
                            if group['message'] and group['message'] != '' and group['message'] != "0" and group['message'] != 0:
                                try:
                                    old_message = await channel.fetch_message(message_id=group['message'])
                                    await old_message.delete()
                                    print(f"Deleted previous lootboard message for group {group_id} ({group_obj.group_name})")
                                except Exception as e:
                                    print(f"Couldn't delete previous message for group {group_id} ({group_obj.group_name}): {e}")
                                    # Continue anyway, we'll try to post a new message
                            
                            # Always create a new message when repost is enabled
                            try:
                                message = await channel.send(f"<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                                configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                            GroupConfiguration.config_key == 'lootboard_message_id').first()
                                configured_message.config_value = str(message.id)
                                session.commit()
                                print(f"Posted new lootboard message for group {group_id} ({group_obj.group_name}) with ID: {message.id}")
                            except Exception as e:
                                print(f"Couldn't send a new message to the channel: {e}")
                                continue
                        else:
                            # Use existing logic to find and edit existing message
                            if group['message'] != '' and group['message'] != "0" and group['message'] != 0:
                                try:
                                    message = await channel.fetch_message(message_id=group['message'])
                                except Exception as e:
                                    #print("Couldn't fetch the message for this lootboard...:", e)
                                    continue
                                    
                            else:
                                print(f"No message ID found for group {group_id} ({group_obj.group_name}). We would have sent a new one right now...")
                                try:
                                    new_board = await channel.send(f"This loot leaderboard is being initialized.... Please wait a few moments.")
                                    new_board_msg_id = new_board.id
                                    configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                                GroupConfiguration.config_key == 'lootboard_message_id').first()
                                    configured_message.config_value = str(new_board_msg_id)
                                    session.commit()
                                except Exception as e:
                                    print(f"Couldn't send a message to the channel: {e}")
                                    continue
                                #staffchat = await bot.fetch_channel(channel_id=1210765308239945729)

                                group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                                configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                        GroupConfiguration.config_key == 'lootboard_message_id').first()
                                
                                # else: ## found previous message from the bot
                                #     message = message_to_update
                                #     configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                #                                                                     GroupConfiguration.config_key == 'lootboard_message_id').first()
                                #     if configured_message.config_value != str(message.id):  
                                #         configured_message.config_value = str(message.id)
                                #         session.commit()
                                #     if not message:
                                #         message = await channel.send(f"<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                                #         print(f"No message ID found for group {group_id} ({group_obj.group_name}). Creating a new one...")
                                #         try:
                                #             configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                #                                                                     GroupConfiguration.config_key == 'lootboard_message_id').first()
                                #             configured_message.config_value = str(message.id)
                                #             session.commit()
                                #         except Exception as e:
                                #             print(f"Couldn't update the lootboard message ID with a new one... e: {e}")
                            
                            if not message:
                                print(f"Couldn't get the message to update the loot leaderboard with...")
                                try:
                                    message = await channel.send(f"<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                                    configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                                GroupConfiguration.config_key == 'lootboard_message_id').first()
                                    configured_message.config_value = str(message.id)
                                    session.commit()
                                except Exception as e:
                                    print(f"Couldn't send a new message to the channel: {e}")
                                continue
                        
                        wom_id = group['wom_id']
                        if not wom_id:
                            wom_id = 0
                            # Use the direct URL and call the updates in our external process.
                        image_path = f"/store/droptracker/disc/static/assets/img/clans/{group_id}/lb/lootboard.png"
                        if not os.path.exists(image_path):
                            print(f"Lootboard image not found for group {group_id} ({group_obj.group_name}).")
                            continue
                        
                        try:
                            embed_template = await self.db.get_group_embed('lb', group_id)
                        except Exception as e:
                            print("Unable to obtain embed_template for group", group_obj.group_name, "e:", e)
                            continue
                        if group_id != 2:
                            total_tracked = group_obj.get_player_count()
                        else:
                            total_tracked = session.query(Player.wom_id).count()
                        # with get_fresh_xenforo_session() as xenforo_session:
                        #     # Fix: Use execute() instead of query() when using text() with parameters
                        #     premium_status = xenforo_session.execute(
                        #         text("SELECT * FROM xf_user_upgrade_active WHERE group_id = :group_id"), 
                        #         {"group_id": group_id}
                        #     ).first()
                        #     if not premium_status:
                        #         group_patreon = session.query(GroupPatreon).filter(GroupPatreon.group_id == group_id).first()
                        #         next_update = datetime.now() + timedelta(seconds=615)
                        #     else:
                        #         next_update = datetime.now() + timedelta(seconds=615)
                        next_update = datetime.now() + timedelta(seconds=615)
                        future_timestamp = int(time.mktime(next_update.timetuple()))
                        value_dict = {
                            "{next_refresh}": f"<t:{future_timestamp}:R>",
                            "{tracked_members}": total_tracked
                        }
                        try:
                            embed = replace_placeholders(embed_template, value_dict)
                        except Exception as e:
                            print("Unable to replace placeholders for group", group_obj.group_name, "e:", e)
                            continue
                        try:
                            lootboard = interactions.File(image_path)
                            await message.edit(content="",embed=embed,files=lootboard)
                            #print("Updated the loot leaderboard for group", group_obj.group_name)
                        except Exception as e:
                            print("Unable to edit the message for group", group_obj.group_name, "e:", e)
                            continue
                    except Exception as e:
                        configured_style = None
                        try:
                            configured_style = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'loot_board_type').first()
                        except:
                            pass
                            
                        if configured_style:
                            # app_logger.log(log_type="error", data=f"Loot leaderboards -- Couldn't create/send {group_obj.group_name} (#{group_id})'s embed: {e}\n" + 
                            #                  "Board style is:" + configured_style.config_value, app_name="core", description="update_loot_leaderboards")
                            print("Exception occurred while updating the loot leaderboard for group", group_obj.group_name, "e:", e, "type:", type(e))
                        else:
                            print("Exception occurred while updating the loot leaderboard for group", group_obj.group_name, "e:", e, "type:", type(e))
                    # Wait 1 second before processing the next group
                    await asyncio.sleep(1)
            except Exception as e:
                print(f"Major error in loot leaderboard update: {e}")
                if session:
                    try:
                        session.rollback()
                    except:
                        pass
            finally:
                if session:
                    try:
                        session.close()
                    except:
                        pass
            
            print("Completed loot leaderboard update. Waiting 5 minutes before the next update.")
        except Exception as e:
            print(f"Critical error in loot leaderboard update loop: {e}")
        finally:
            if hasattr(self, "_processing_lock") and self._processing_lock.locked():
                self._processing_lock.release()
        await asyncio.sleep(540) ## wait 9 minutes before the next loop
        

async def instantly_update_board(group_id: int, force: bool = False):
    # Local import to avoid circular import during module initialization
    from lootboards import update_specific_board
    await update_specific_board(group_id, force=force)

