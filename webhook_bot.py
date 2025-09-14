import interactions
import os
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from interactions.api.events import MessageCreate, Startup
from interactions import Embed, Intents, Message, ChannelType, OptionType, slash_command, Permissions, slash_option
from db.models import Group, ItemList, PersonalBestEntry, PlayerPet, Session, Player, User, UserConfiguration
from data.submissions import adventure_log_processor, clog_processor, ca_processor, pb_processor, drop_processor, pet_processor
from utils.format import convert_to_ms, get_true_boss_name
from services.updates import Updates
from services.ticket_system import Tickets
from sqlalchemy.exc import OperationalError, DisconnectionError
import time

channel_id_to_use = 1210765287591256084

load_dotenv()

bot = interactions.Client(token=os.getenv("WEBHOOK_TOKEN"), intents=Intents.ALL)


# Add a test to verify the event listener is registered
@bot.event
async def on_ready():
    print("=== BOT IS READY ===")
    print(f"Bot logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")
    for guild in bot.guilds:
        print(f"  - {guild.name} (ID: {guild.id})")
    print("=== END READY ===")

# Add retry decorator for database operations
def retry_on_database_error(max_retries=3, delay=1):
    """Decorator to retry database operations on connection failures"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (OperationalError, DisconnectionError) as e:
                    last_exception = e
                    if "server has gone away" in str(e).lower() or "connection reset" in str(e).lower():
                        print(f"Database connection lost on attempt {attempt + 1}, retrying in {delay}s...")
                        if attempt < max_retries - 1:  # Don't sleep on the last attempt
                            await asyncio.sleep(delay)
                        continue
                    else:
                        raise  # Re-raise if it's not a connection issue
                except Exception as e:
                    # For non-database errors, don't retry
                    raise
            
            # If we get here, all retries failed
            print(f"All {max_retries} database retry attempts failed")
            raise last_exception
        return wrapper
    return decorator



@retry_on_database_error(max_retries=3, delay=1)
async def process_submission_with_session(submission_type, embed_data):
    """Process a submission with a fresh database session"""
    session = Session()
    print(f"Processing submission with session: {submission_type}")
    try:
        if submission_type == "collection_log":
            result = await clog_processor(embed_data, external_session=session)
        elif submission_type == "combat_achievement":
            result = await ca_processor(embed_data, external_session=session)
        elif submission_type == "personal_best":
            result = await pb_processor(embed_data, external_session=session)
        elif submission_type == "drop":
            result = await drop_processor(embed_data, external_session=session)
        elif submission_type == "pet":
            result = await pet_processor(embed_data, external_session=session)
        elif submission_type == "adventure_log":
            result = await adventure_log_processor(embed_data, external_session=session)
        else:
            result = None
        
        # Commit the session if everything succeeded
        session.commit()
        return result
        
    except Exception as e:
        # Rollback on any error
        session.rollback()
        print(f"Error processing {submission_type}: {e}")
        raise
    finally:
        # Always close the session
        session.close()

@interactions.listen(MessageCreate)
async def on_message_create(event: MessageCreate):
    def embed_to_dict(embed: Embed):
        if embed.fields:
            return {f.name: f.value for f in embed.fields}
        return {}
    
    print("Got a message...")
    bot: interactions.Client = event.bot
    if bot.is_closed:
        await bot.astart(token=os.getenv("WEBHOOK_TOKEN"))
    await bot.wait_until_ready()
    
    if isinstance(event, Message):
        message = event
    else:
        message = event.message
        
    if message.author.system:  # or message.author.bot:
        return
    if message.author.id == bot.user.id:
        return
    if message.channel.type == ChannelType.DM or message.channel.type == ChannelType.GROUP_DM:
        return
    channel_id = message.channel.id
    target_guilds = ["1172737525069135962",
                    "900855778095800380",
                    "597397938989432842",
                    "702992720909828168",
                    "1120606216972947468"]
                    
    if str(message.guild.id) in target_guilds:
        for embed in message.embeds:
            embed_data = embed_to_dict(embed)
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.url:
                        embed_data['attachment_url'] = attachment.url
                        embed_data['attachment_type'] = attachment.content_type
                        
            field_names = [field.name for field in embed.fields]
            if embed_data:
                field_values = [field.value.lower().strip() for field in embed.fields]
                if "source_type" in field_names and "loot chest" in field_values:
                    ## Skip pvp
                    continue
                    
                embed_data['used_api'] = False
                
                try:
                    print(f"Processing submission: {embed_data}")
                    if "collection_log" in field_values:
                        await process_submission_with_session("collection_log", embed_data)
                        continue
                    elif "combat_achievement" in field_values:
                        await process_submission_with_session("combat_achievement", embed_data)
                        continue
                    elif "npc_kill" in field_values or "kill_time" in field_values:
                        await process_submission_with_session("personal_best", embed_data)
                        continue
                    elif embed.title and "received some drops" in embed.title or "drop" in field_values:
                        await process_submission_with_session("drop", embed_data)
                        continue
                    elif "experience_update" in field_values or "experience_milestone" in field_values or "level_up" in field_values:
                        # await experience_processor(embed_data)
                        continue
                    elif "quest_completion" in field_values:
                        # await quest_processor(embed_data)
                        continue
                    elif "pet" in field_values and "pet_name" in field_names:
                        await process_submission_with_session("pet", embed_data)
                        continue
                    elif "adventure_log" in field_values:
                        await process_submission_with_session("adventure_log", embed_data)
                        continue
                        
                except Exception as e:
                    print(f"Failed to process submission after retries: {e}")
                    # Continue processing other embeds even if one fails
    else:
        print(f"Message is not in the target guilds: {message.guild.id}")

        
@interactions.listen(Startup)
async def on_startup(event: Startup):
    
    # Load extensions first (they don't require database)
    try:
        bot.load_extension("services.updates")
        bot.load_extension("services.ticket_system")
    except Exception as e:
        print(f"Error loading extensions: {e}")
    
    # Then handle database operations with proper session management
    player_count = 0
    local_session = Session()
    try:
        player_count = local_session.query(Player.player_id).count()
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name=f" ~{player_count} players", type=interactions.ActivityType.WATCHING))
    except (OperationalError, DisconnectionError) as e:
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name="DropTracker Bot", type=interactions.ActivityType.WATCHING))
    except Exception as e:
        print(f"Unexpected error during startup: {e}")
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name="DropTracker Bot", type=interactions.ActivityType.WATCHING))
    finally:
        local_session.close()




if __name__ == "__main__":
    bot.start()