"""
    The DropTracker event system integrates the tracking functionality of the DropTracker
    with the Discord bot and XenForo website to provide a fully integrated event experience

"""

import asyncio
import os
from dotenv import load_dotenv
import interactions
from interactions import ActionRow, Button, ButtonStyle, ContainerComponent, MediaGalleryComponent, MediaGalleryItem, Message, PartialEmoji, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, UnfurledMediaItem, listen
from interactions.api.events import MessageCreate, ComponentCompletion, ButtonPressed, Startup
from db.models import GroupPersonalBestMessage, ItemList, NpcList, Player, Session, Drop, CombatAchievementEntry, PersonalBestEntry, CollectionLogEntry, XenforoSession
from db.ops import get_formatted_name
from utils.format import format_number, get_current_partition
from utils.redis import redis_client

load_dotenv()

bot = interactions.Client(token=os.getenv("BOT_TOKEN"))

@interactions.listen(MessageCreate)
async def on_message_create(event: MessageCreate):
    pass

groups_not_deleted = []

async def delete_old_hof():
    session = Session()
    items_to_remove = session.query(GroupPersonalBestMessage).where(GroupPersonalBestMessage.group_id != 2).all()
    if items_to_remove:
        for item in items_to_remove:
            message_id = item.message_id
            channel_id = item.channel_id
            try:
                channel: interactions.GuildText = await bot.fetch_channel(channel_id=channel_id)
                if type(channel) != interactions.GuildText:
                    print("Channel is not a GuildText channel, permissions issue or other problem...")
                    print(f"Removing the database entry regardless.")
                    session.delete(item)
                    if item.group_id not in groups_not_deleted:
                        groups_not_deleted.append(item.group_id)
                    continue
                message = await channel.fetch_message(message_id=message_id)
                if message:
                    await message.delete()
                    print("Deleted message for", item.boss_name, "in group", item.group_id)
                    session.delete(item)
                    await asyncio.sleep(0.2)
            except Exception as e:
                print(f"Error deleting message {message_id} in channel {channel_id}: {e}")
            finally:
                session.commit()
    print("Groups with messages that didn't get deleted:", groups_not_deleted)
    


@listen(Startup)
async def on_startup(event: Startup):
    print(f"Bot started.")
    await delete_old_hof()
    pass



if __name__ == "__main__":
    bot.start()

