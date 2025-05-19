
import interactions
from interactions import ComponentContext, Extension, ActionRow, Button, ButtonStyle, FileComponent, PartialEmoji, Permissions, SlashContext, UnfurledMediaItem, listen, slash_command
from interactions.api.events import Startup, Component, ComponentCompletion, ComponentError, ModalCompletion, ModalError, MessageCreate
from interactions.models import ContainerComponent, ThumbnailComponent, SeparatorComponent, UserSelectMenu, SlidingWindowSystem, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, MediaGalleryComponent, MediaGalleryItem, OverwriteType

class Components(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        print(f"Components service initialized.")


    @listen(Component)
    async def on_component(self, event: Component):

        if event.ctx.custom_id == "clan_setup_info":
            await self.send_clan_setup_info(event.ctx)
        elif event.ctx.custom_id == "player_setup_info":
            await self.send_player_setup_info(event.ctx)


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



    logo_media = UnfurledMediaItem(
        url="https://www.droptracker.io/img/droptracker-small.gif"
    )

    player_setup = [
        ContainerComponent(
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content="## Player Setup - DropTracker.io",
            ),
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content="-# This section will be added soon. For now, all you need to do is install our plugin and ensure your group is configured properly.\n" +
                        "-# Feel free to visit the [Wiki](https://www.droptracker.io/wiki) for more information."
                    )
                ],
                accessory=ThumbnailComponent(
                    media=logo_media
                )
            ),
            SeparatorComponent(divider=True),
        )
    ]

    clan_setup = [
        ContainerComponent(
            TextDisplayComponent(
                content="## Clan Setup - DropTracker.io",
            ),
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content="-# There are a few pre-requisites to setting up a DropTracker group:\n"
                        "-# 1. You must have a [WiseOldMan group](https://wiseoldman.net/groups) - if you don't have one, you can [create one here](https://wiseoldman.net/groups/create)\n"
                        "-# 2. A Discord server where you are either the owner, or have the owner's permissions to set up our bot\n"
                        "-# 3. Our Discord Bot invited to your server\n"),
                ],
                accessory=ThumbnailComponent(
                    media=logo_media
                )
            ),
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content="-# If you have all of these, grab your **WiseOldMan Group ID** (3-6 digits maximum, with no hyphens), and use </create-group:1369493380358209543> in your group's Discord server to get started.\n" +
                        "-# Once you create a group, you should be DMed with a welcome message; and a link to configure your group settings.\n\n" +
                        "-# After creating a group, you can also [click here](https://www.droptracker.io/account/players), then click your group name in the side navigation to find your group config page."
                    )
                ],
                accessory=ThumbnailComponent(
                    media=UnfurledMediaItem(
                        url="https://www.droptracker.io/img/wom-example.png"
                    )
                )
                    
                
            ),
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content="### Need more help?"
                    ),
                    TextDisplayComponent(
                        content="-# You could:" + "\n" +
                        "-# - Open a ticket in <#1210765301042380820>\n" +
                        "-# - Check out the [Wiki on our website](https://www.droptracker.io/wiki)\n" +
                        "-# - Send us a message in <#1374155512660103273>\n"
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
                        content="-# We also offer some premium features for groups when they [upgrade their account](https://www.droptracker.io/account/upgrades).\n" +
                        "-# Please consider subscribing to support the development of the project.",
                    ),
                ],
                accessory=Button(
                    label="Upgrade",
                    style=ButtonStyle.LINK,
                    emoji=PartialEmoji(name="supporter", id=1263827303712948304),
                    url="https://www.droptracker.io/account/upgrades"
                )
            ),
            SeparatorComponent(divider=True),
            ActionRow(
                Button(
                    label="Invite our Discord bot",
                    style=ButtonStyle.LINK,
                    url="https://discord.com/oauth2/authorize?client_id=1172933457010245762&permissions=8&scope=bot"
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content="-# Powered by the [DropTracker](https://www.droptracker.io) - a project by <@528746710042804247>"
            ),
            SeparatorComponent(divider=True),
            
        )
    ]



    async def send_player_setup_info(self, ctx: ComponentContext):
        components = self.player_setup
        await ctx.send(components=components, ephemeral=True)



    async def send_clan_setup_info(self, ctx: ComponentContext):
        components = self.clan_setup
        await ctx.send(components=components, ephemeral=True)


    
