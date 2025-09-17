

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
        self.lootboard_updates.start()
        #self.lootboard_updates()

    async def _update_and_refresh_group(self, session: Session, group_id: int, group_cfg: dict, force: bool = False):
        """Update Discord message for a group. Only generate new board if force=True."""
        if force:
            # Only generate new board image when forced (e.g., new submission)
            try:
                from lootboards import update_specific_board
                await update_specific_board(group_id, force=force)
            except Exception as e:
                print(f"Error generating board for group {group_id}: {e}")
                # Continue to attempt message update if an older image exists

        try:
            channel: interactions.Channel = await self.bot.fetch_channel(channel_id=group_cfg['channel'])
            if not channel:
                return
            if channel.type == interactions.BaseChannel:
                print("Channel not found for this group.")
                return
            group_obj = session.query(Group).filter(Group.group_id == group_id).first()

            # Determine behavior for repost vs edit-in-place
            should_repost_value = group_cfg['repost'] if group_cfg['repost'] else "false"
            repost_enabled = str(should_repost_value).lower() in ['true', '1', 'yes', 'on']

            message = None
            if repost_enabled:
                # Remove old message if configured
                if group_cfg['message'] and str(group_cfg['message']) not in ("", "0"):
                    try:
                        old_message = await channel.fetch_message(message_id=group_cfg['message'])
                        await old_message.delete()
                        print(f"Deleted previous lootboard message for group {group_id} ({group_obj.group_name})")
                    except Exception as e:
                        print(f"Couldn't delete previous message for group {group_id} ({group_obj.group_name}): {e}")
                # Post a brand new message
                try:
                    message = await channel.send("<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                    configured_message = session.query(GroupConfiguration).filter(
                        GroupConfiguration.group_id == group_id,
                        GroupConfiguration.config_key == 'lootboard_message_id'
                    ).first()
                    if configured_message:
                        configured_message.config_value = str(message.id)
                        session.commit()
                    print(f"Posted new lootboard message for group {group_id} ({group_obj.group_name}) with ID: {message.id}")
                except Exception as e:
                    print(f"Couldn't send a new message to the channel: {e}")
                    return
            else:
                # Try to fetch and edit existing message
                if group_cfg['message'] and str(group_cfg['message']) not in ("", "0"):
                    try:
                        message = await channel.fetch_message(message_id=group_cfg['message'])
                    except Exception:
                        message = None
                # If none exists, create one and store the ID
                if not message:
                    print(f"No message ID found for group {group_id} ({group_obj.group_name}). Creating a new one...")
                    try:
                        new_board = await channel.send("This loot leaderboard is being initialized.... Please wait a few moments.")
                        new_board_msg_id = new_board.id
                        configured_message = session.query(GroupConfiguration).filter(
                            GroupConfiguration.group_id == group_id,
                            GroupConfiguration.config_key == 'lootboard_message_id'
                        ).first()
                        if configured_message:
                            configured_message.config_value = str(new_board_msg_id)
                            session.commit()
                        message = new_board
                    except Exception as e:
                        print(f"Couldn't send a message to the channel: {e}")
                        return

            # Resolve image path
            image_path = f"/store/droptracker/disc/static/assets/img/clans/{group_id}/lb/lootboard.png"
            if not os.path.exists(image_path):
                #print(f"Lootboard image not found for group {group_id} ({group_obj.group_name}).")
                pass
                return

            # Build embed from template
            try:
                embed_template = await self.db.get_group_embed('lb', group_id)
            except Exception as e:
                print("Unable to obtain embed_template for group", group_obj.group_name, "e:", e)
                return

            if group_id != 2:
                total_tracked = group_obj.get_player_count()
            else:
                total_tracked = session.query(Player.wom_id).count()

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
                return

            try:
                lootboard = interactions.File(image_path)
                await message.edit(content="", embed=embed, files=lootboard)
            except Exception as e:
                print("Unable to edit the message for group", group_obj.group_name, "e:", e)
                return

        except Exception as e:
            print(f"Exception in _update_and_refresh_group for group {group_id}: {e}")

    async def update_group_now(self, group_id: int, force: bool = False):
        """Public method to generate and refresh a single group's lootboard message now.
        Usage: ext = bot.get_ext("services.lootboard_updater"); await ext.update_group_now(group_id, force=True)
        """
        if hasattr(self, "_processing_lock") and self._processing_lock.locked():
            #print("Lootboard updater is busy; queuing single-group update after current run.")
            pass
        async with self._processing_lock:
            session = None
            try:
                session = Session()
                configured_channel = session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'lootboard_channel_id'
                ).first()
                configured_message = session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'lootboard_message_id'
                ).first()
                should_repost = session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'repost_lootboard'
                ).first()
                if not configured_channel or not configured_channel.config_value:
                    print(f"No lootboard channel configured for group {group_id}.")
                    return
                group_cfg = {
                    "channel": configured_channel.config_value,
                    "message": configured_message.config_value if configured_message else "",
                    "repost": should_repost.config_value if should_repost else ""
                }
                await self._update_and_refresh_group(session, group_id, group_cfg, force=force)
            except Exception as e:
                print(f"Error updating group {group_id} now: {e}")
            finally:
                if session:
                    try:
                        session.close()
                    except:
                        pass

    @Task.create(IntervalTrigger(seconds=540))
    async def lootboard_updates(self):
        try:
            if hasattr(self, "_processing_lock") and self._processing_lock.locked():
                #print("Lootboard update still running, skipping this interval.")
                return
            if hasattr(self, "_processing_lock"):
                await self._processing_lock.acquire()
            #print("Updating loot leaderboards...")
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
                        await self._update_and_refresh_group(session, group_id, group, force=False)
                    except Exception as e:
                        configured_style = None
                        try:
                            configured_style = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'loot_board_type').first()
                        except:
                            pass
                            
                        group_name = None
                        try:
                            group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                            group_name = group_obj.group_name if group_obj else str(group_id)
                        except:
                            group_name = str(group_id)
                        if configured_style:
                            # app_logger.log(log_type="error", data=f"Loot leaderboards -- Couldn't create/send {group_name} (#{group_id})'s embed: {e}\n" + 
                            #                  "Board style is:" + configured_style.config_value, app_name="core", description="update_loot_leaderboards")
                            #print("Exception occurred while updating the loot leaderboard for group", group_name, "e:", e, "type:", type(e))
                            pass
                        else:
                            #print("Exception occurred while updating the loot leaderboard for group", group_name, "e:", e, "type:", type(e))
                            pass
                    # Wait 1 second before processing the next group
                    await asyncio.sleep(1)
            except Exception as e:
                #print(f"Major error in loot leaderboard update: {e}")
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
            
            #       print("Completed loot leaderboard update. Waiting 5 minutes before the next update.")
        except Exception as e:
            #print(f"Critical error in loot leaderboard update loop: {e}")
            pass
        finally:
            if hasattr(self, "_processing_lock") and self._processing_lock.locked():
                self._processing_lock.release()
        await asyncio.sleep(540) ## wait 9 minutes before the next loop
        

async def instantly_update_board(group_id: int, force: bool = False):
    """Generate a new board for a specific group. This should only be called when new submissions come in."""
    # This function should be called from submissions.py when new data comes in
    # It will generate a new board image
    from lootboards import update_specific_board
    await update_specific_board(group_id, force=force)

