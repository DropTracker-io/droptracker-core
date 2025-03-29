import interactions
import aiohttp
import aiofiles
from interactions import Embed, Client, listen, ChannelType, Button, ButtonStyle
from interactions.api.events import MessageCreate, Component
from db.models import User, Group, Guild, Player, Drop, session, ItemList, Webhook, NpcList, GroupConfiguration
from utils.wiseoldman import check_user_by_id, check_user_by_username, check_group_by_id
import re
import os
#from utils.zohomail import send_email
from dotenv import load_dotenv

from utils.format import format_time_since_update, format_number, get_command_id, get_extension_from_content_type

from datetime import datetime, timedelta

from utils.redis import RedisClient, calculate_global_overall_rank, calculate_rank_amongst_groups
from db.ops import DatabaseOperations

load_dotenv()
db = DatabaseOperations()
redis_client = RedisClient()

ignored_list = [] # temporary implementation of a source blacklist

global_footer = os.getenv('DISCORD_MESSAGE_FOOTER')

async def send_update_message(bot: interactions.Client, total_added, current_id):
    channel = await bot.fetch_channel(channel_id=1281734796116099155)
    update_embed = interactions.Embed(title="New drops added",
                                      description="A cycle of the redis cache update has completed.")
    update_embed.set_thumbnail(url="https://www.droptracker.io/img/droptracker-small.gif")
    update_embed.add_field(name=f"Added a total of {total_added} new entries.",
                           value=f"Currently up to id #{current_id}")
    await channel.send(embed=update_embed)

bot = None

async def message_processor(disc_bot: interactions.Client, event: interactions.events.MessageCreate):
    pass
    
async def new_patreon_sub(disc_bot: interactions.Client, user_id, sub_type, group: Group = None):
    channel_id = 1210765296055623681
    try:
        channel = await disc_bot.fetch_channel(channel_id=channel_id)
        if sub_type == 1:
            sub_str = "[Supporter]"
        elif sub_type == 2:
            sub_str = "[Supporter] (Group)"
        elif sub_type > 2:
            sub_str = "[Supporter+]*"
        else:
            sub_str = "Supporter"
        patreon_sub=Embed(title="New Patreon",
                          description=f"<@{user_id}> just joined our Patreon at the **{sub_str}** tier!",
                          color=0x00ff00)
        patreon_sub.set_footer("Thank you for your support in helping keep the DropTracker's lights on!")
        await channel.send(embed=patreon_sub)
    except Exception as e:
        print("Couldn't send message:", e)
    

async def new_patreon_update(user: User, status: str):
    embed = Embed(title="New Patron",
                  description=f"<@{user.discord_id}> has subscribed via Patreon!")
    channel_id = 1210765296055623681
    channel = await bot.fetch_channel(channel_id = channel_id)
    discord_user = await bot.fetch_user(user_id=user.discord_id)
    await channel.send(embeds=embed)
    try:
        user_embed = Embed(title="Your Patreon status has been updated",
                        description=f"Thank you for your contribution to keeping the **DropTracker** online!",
                        color=0x00ff00)
        user_embed.add_field(name=f"You now have the ability to configure `google sheets`, `custom webhooks` and more!",
                            value=f"Just use </user-settings:{await get_command_id(bot, 'user-settings')}>")
        user_embed.set_footer(global_footer)
        await discord_user.send(f"Hey, <@{discord_user.id}>!",
                                embeds=user_embed)
    except Exception as e:
        ## probably a lack of permissions, or the user's privacy settings        pass
        pass 

async def new_player_message(bot: interactions.Client, player_name):
    channel_id = 1281734796116099155
    channel = await bot.fetch_channel(channel_id=1281734796116099155)
    await channel.send(embeds=Embed(title="New player added",
                                    description=f"{player_name} has made their first appearance in the database.",
                                    color=0x00ff00, footer=global_footer))
    
async def name_change_message(bot, new_name, player_id, old_name):
    channel_id = 1281734796116099155
    channel = await bot.fetch_channel(channel_id=1281734796116099155)
    await channel.send(embeds=Embed(title="Name changed",
                                    description=f"[{player_id}] `{old_name}` -> `{new_name}`",
                                    color=0x00ff00, footer=global_footer))
                                
sent_npc_email_list = []
async def confirm_new_npc(bot: interactions.Client, npc_name, player_name, item_name, value):
    if npc_name == "Loot Chest":
        return
    else:
        channel_id = 1350412061141762110
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel:
            embed = Embed(title="New NPC Detected",
                          description=f"Player: `{player_name}`\n" + 
                          f"Item: `{item_name}`\n" + 
                          f"**Unknown NPC:** `{npc_name}`\n" + 
                          f"Value: `{value}`")
            await channel.send(f"@everyone\nAn NPC has arrived thru a submission that we are not yet tracking:", embeds=embed)
        


sent_item_email_list = []
async def confirm_new_item(bot: interactions.Client, item_name, player_name, item_id, npc_name, value):
    if item_name not in sent_item_email_list:
        channel_id = 1350412061141762110
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel:
            embed = Embed(title="New item Detected",
                          description=f"Player: `{player_name}`\n" + 
                          f"**Unknown Item:** `{item_name}`\n" + 
                          f"Item ID: `{item_id}`\n" + 
                          f"NPC: `{npc_name}`\n" + 
                          f"Value: `{value}`")
            
            await channel.send(f"@everyone\nAn NPC has arrived thru a submission that we are not yet tracking:", embeds=embed)
        sent_item_email_list.append(item_name)
    else:
        return

async def joined_guild_msg(bot: interactions.Client, guild: interactions.Guild):
    try:
        owner_id = guild._owner_id
        user = await bot.fetch_user(user_id=owner_id)
        welcome_embed = Embed(title="Thanks for the invite!",
                              description="> *What do I do next?*")
        welcome_embed.add_field(name="Are you trying to create a `group`?",
                                value=f"First, make sure you have a [wise old man](http://www.wiseoldman.net/groups/create) group created," +
                                "\n containing the members you want to track.\n" + 
                                f"Then, use the </create-group:{await get_command_id(bot, 'create-group')}> command " + 
                                "**in *your group's* discord server** to register it in our database.",
                                inline=False)
        welcome_embed.add_field(name="Need some assistance?",
                                value=f"Check out </help:{await get_command_id(bot, 'help')}> & our [docs](https://www.droptracker.io/docs).\nFeel free to join our [discord server](https://www.droptracker.io/discord) if you still need help.")
        welcome_embed.set_footer(global_footer)
        
        await user.send(f"Hey, <@{owner_id}>!",embeds=[welcome_embed])
    except Exception as e:
        print("Couldn't DM the server owner when we joined a guild...")

        
async def send_lootboard_message(bot: interactions.Client, group_id, channel_id, message_id: str = None):
    embed = interactions.Embed(title="Loot Leaderboard",
                                        description=f"Powered by [DropTracker.io](https://www.droptracker.io/)",
                                        color=0x00ff00)
    embed.add_field(name="Track your drops automatically!",
                    value="Download the [DropTracker RuneLite plugin]" + 
                    "(https://www.droptracker.io/runelite)",
                    inline=True)
    embed.add_field(name=f"Need some help?",
                    value=f"Use </help:{await get_command_id(bot, 'help')}>",
                    inline=True)
    embed.add_field(name=f"Links",
                    value=f"[Docs](https://www.droptracker.io/docs)\n" + 
                            "[Website](https://www.droptracker.io/)\n" + 
                            f"[Discord](https://www.droptracker.io/discord)")
    embed.set_footer(global_footer)
    channel_object = await bot.fetch_channel(channel_id=channel_id)
    if not message_id:
        try:
            
            if channel_object:
                lb_msg = await channel_object.send(embeds=embed)
                new_msg_id = str(lb_msg.id)
                cfg = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id).first()

                if cfg:
                    # Find the entry where config_key is 'lootboard_message_id'
                    if cfg.config_key == 'lootboard_message_id':
                        # Update the config_value with the new message ID
                        cfg.config_value = new_msg_id
                        
                        # Commit the changes to the database
                        session.commit()
                        print(f'Updated lootboard_message_id to {new_msg_id}')
                    else:
                        print('config_key for lootboard_message_id not found')
        except Exception as e:
            print("Couldn't properly configure a new lootboard channel ID for the group: ", e)
    else:
        message = await channel_object.get_message(message_id=message_id)
        try:
            await message.edit(embeds=embed)
        except Exception as e:
            pass

