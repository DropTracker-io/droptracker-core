from datetime import datetime
import os
import interactions
from interactions import ChannelType, ContextMenuContext, Extension, listen, Message, message_context_menu
from interactions.api.events import MessageCreate, Component
from db.models import Group, GroupPatreon, PlayerPet, session, Player, ItemList, PersonalBestEntry
from db.xf.recent_submissions import create_xenforo_entry
from services.components import info_action_row
from utils.embeds import update_boss_pb_embed
# Removed circular import - these will be imported lazily inside functions if needed
from utils.msg_logger import HighThroughputLogger
from data.submissions import clog_processor, ca_processor, pb_processor, drop_processor
from utils.format import convert_to_ms, convert_from_ms, get_true_boss_name
from utils.redis import redis_client
from utils.app_emojis import emoji as app_emoji, partial_emoji as app_partial_emoji
from utils.site_urls import PREMIUM_URL
from db.app_logger import AppLogger
from interactions import AutocompleteContext, BaseContext, GuildText, Permissions, SlashCommand, UnfurledMediaItem, PartialEmoji, ActionRow, Button, ButtonStyle, SlashCommandOption, check, is_owner, Extension, slash_command, slash_option, SlashContext, Embed, OptionType, GuildChannel, SlashCommandChoice
from interactions.api.events import Startup, Component, ComponentCompletion, ComponentError, ModalCompletion, ModalError, MessageCreate
from interactions.models import ContainerComponent, ThumbnailComponent, SeparatorComponent, UserSelectMenu, SlidingWindowSystem, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, MediaGalleryComponent, MediaGalleryItem, OverwriteType



app_logger = AppLogger()
bot_token = os.getenv("DISCORD_TOKEN")
ignored_list = []
last_xf_transfer = datetime.now()


class MessageHandler(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot


    @listen(Component)
    async def on_component(self, event: Component):
        ctx = event.ctx
        custom_id = ctx.custom_id
        if custom_id.startswith("patreon_group_"):
            group_id = int(custom_id.split("_")[2])
            valid_patreon = session.query(GroupPatreon).filter(GroupPatreon.user_id == ctx.user.id).first()
            if valid_patreon and valid_patreon.group_id == None:
                valid_patreon.group_id = group_id
                group = session.query(Group).filter(Group.group_id == group_id).first()
                await ctx.send(f"You have assigned your DropTracker Patreon subscription perks to {group.group_name}!")
                try:
                    await ctx.message.delete()
                except Exception as e:
                    print("Couldn't delete the message:", e)
                return
            else:
                await ctx.send("You don't have a valid Patreon subscription, or you already have a group assigned to it.")
                return

        if custom_id == "nitro_pick":
            # Clan-picker select from a boost confirmation DM: set the booster's
            # group designation (mirrors the patreon_group_ picker above).
            from db.models import Session as _Session
            from services import nitro_attribution
            try:
                gid = int(ctx.values[0])
            except (IndexError, ValueError, TypeError):
                return
            name = None
            _s = _Session()
            try:
                name = nitro_attribution.designate_group_for_discord_user(_s, str(ctx.author.id), gid)
                if name:
                    _s.commit()
            except Exception as e:
                print(f"[nitro] pick handler error: {e}")
            finally:
                _s.close()
            msg = (
                f"✓ Your boost now supports **{name}**. Change it any time at "
                f"https://www.droptracker.io/settings."
                if name
                else "Couldn't update your choice — you may not be in that clan anymore."
            )
            await ctx.edit_origin(content=msg, components=[])
            return
            
    async def send_invite_page(self, message: Message):
        channel = await self.bot.fetch_channel(message.channel.id)
        invite_components = [
            ContainerComponent(
                SeparatorComponent(divider=True),
                SectionComponent(
                    components=[
                        TextDisplayComponent(
                            content="## Invite me to your Discord Server",
                        ),
                    ],
                    accessory=Button(
                        label="Invite the DropTracker.io Bot",
                        style=ButtonStyle.LINK,
                        url="https://discord.com/oauth2/authorize?client_id=1172933457010245762&permissions=8&scope=bot"
                    )
                ),
                SeparatorComponent(divider=True),
            )
        ]
        components = invite_components
        await channel.send(components=components)


    async def send_runelite_logs_guide(self, message: Message):
        channel = await self.bot.fetch_channel(message.channel.id)
        log_components = [
            ContainerComponent(
                SeparatorComponent(divider=True),
                TextDisplayComponent(
                            content="### Finding your RuneLite client logs for debugging purposes:\n" +
                            "-# There are two primary ways to locate your `client.log` file:\n" +
                            f"-# By right-clicking the {app_emoji('screenshot')} screenshot icon in the top-right corner of the RuneLite client\n" +
                            "-# or, navigate to:\n" +
                            "-# Windows: `%userprofile%.runelite\logs`\n" +
                            "-# Linux/MacOS: $HOME/.runelite/logs\n\n"
                            "-# You should see a file named `client.log` in this folder. Please drag and drop it here.",
                        ),
                SeparatorComponent(divider=True),
            )
        ]
        components = log_components
        await channel.send(components=components)
            

    async def send_welcome_page(self, message: Message):
        channel = await self.bot.fetch_channel(message.channel.id)
        logo_media = UnfurledMediaItem(
            url="https://www.droptracker.io/img/droptracker-small.gif"
        )
        welcome_page = [
            ContainerComponent(
                SeparatorComponent(divider=True),
                TextDisplayComponent(
                    content="# Welcome to the DropTracker.io Discord Server"
                ),
                SeparatorComponent(divider=True),
                SectionComponent(
                    components=[
                        TextDisplayComponent(
                            content="-# The DropTracker is an all-in-one loot and achievement" + 
                            "tracking system, built for Old School RuneScape players & groups.\n" +
                            "-# Our small team of developers work hard to provide a fun extension to the game:\n\n" + 
                            "-# <@528746710042804247> - Primary Development\n" +
                            "-# <@232236164776460288> - RuneLite plugin / community help\n" +
                            "-# <@230848731614806017> - Developer\n\n" +
                            "-# We are always looking for more help!\n" + 
                            "-# If you are interested in joining the team, please reach out.",
                        ),
                    ],
                    accessory=ThumbnailComponent(
                        media=logo_media
                    )
                ),
                SeparatorComponent(divider=True),
                SectionComponent(
                    components=[
                        TextDisplayComponent(
                            content="### How do I use this app?",
                        ),
                        TextDisplayComponent(
                            content="-# We strive to provide the most simple integration for players and groups alike.\n" +
                            "-# All you *need* to do is install our [RuneLite plugin](https://www.droptracker.io/runelite), and your drops & achievements should automatically be tracked by the <@1172933457010245762> Discord bot.\n" +
                            "\n-# It is highly recommended, however, to enable **API connections** in our plugin configuration to ensure the most reliable tracking functionality."
                        )
                        
                    ],
                    accessory=ThumbnailComponent(
                        media=UnfurledMediaItem(
                            url="https://cdn2.steamgriddb.com/icon/1071f2d716fafebd789062219cec9c83/32/128x128.png"
                        )
                    )
                ),
                SeparatorComponent(divider=True),
                SectionComponent(
                    components=[
                        TextDisplayComponent(
                            content="### How does it work?",
                        ),
                        TextDisplayComponent(
                            content=(
                                "-# The DropTracker has two methods of tracking players using our plugin:\n" + 
                                "-# - Discord Webhooks\n" + 
                                "-# - API Connections (*preferred*)\n" +
                                "-# When you receive a drop or complete an achievement, your client will auto" +
                                "matically communicate this information with our system through whichever method you choose (by default, using Discord Webhooks).\n\n" +
                                "-# Once it arrives on our server, we determine based on WiseOldMan and our registered group listings whether or not it qualifies to have a notification sent via Discord.\n"
                            )
                        ),
                    ],
                    accessory=Button(
                            label="Read the Wiki",
                            style=ButtonStyle.LINK,
                            url="https://www.droptracker.io/wiki"
                        ),
                ),
                SeparatorComponent(divider=True),

                SectionComponent(
                    components=[
                        TextDisplayComponent(
                            content=f"-# We also offer some additional features for [players who upgrade their accounts]({PREMIUM_URL}).\n" +
                            "-# Please consider subscribing to support the continued development of the project.",
                        ),
                    ],
                    accessory=Button(
                        label="Upgrade",
                        style=ButtonStyle.LINK,
                        emoji=app_partial_emoji("supporter"),
                        url=PREMIUM_URL
                    )
                ),
                SeparatorComponent(divider=True),
                info_action_row(),
                SeparatorComponent(divider=True)
            ),
            
        ]

        components = welcome_page
        await channel.send(components=components)

