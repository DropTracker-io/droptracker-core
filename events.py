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
from db.models import GroupPersonalBestMessage, ItemList, NpcList, Player, Session, Drop, CombatAchievementEntry, PersonalBestEntry, CollectionLogEntry, XenforoSession, session
from db.ops import get_formatted_name
from events.models import *
from utils.format import format_number, get_current_partition
from utils.redis import redis_client

load_dotenv()


bot = interactions.Client(token=os.getenv("BOT_TOKEN"))

@interactions.listen(MessageCreate)
async def on_message_create(event: MessageCreate):
    pass



@listen(Startup)
async def on_startup(event: Startup):
    bot.load_extension("events.cogs.event_commands")
    print(f"Bot started.")



if __name__ == "__main__":
    bot.start()

