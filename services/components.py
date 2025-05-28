
import subprocess
import interactions
from interactions import ComponentContext, Extension, ActionRow, Button, ButtonStyle, FileComponent, PartialEmoji, Permissions, SlashContext, UnfurledMediaItem, listen, slash_command
from interactions.api.events import Startup, Component, ComponentCompletion, ComponentError, ModalCompletion, ModalError, MessageCreate
from interactions.models import ContainerComponent, ThumbnailComponent, SeparatorComponent, UserSelectMenu, SlidingWindowSystem, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, MediaGalleryComponent, MediaGalleryItem, OverwriteType




logo_media = UnfurledMediaItem(
    url="https://www.droptracker.io/img/droptracker-small.gif"
)



InfoActionRow = ActionRow(
    Button(
        label="View Player Setup/Info",
        style=ButtonStyle.GRAY,
        emoji=PartialEmoji(name="newmember", id=1263916335184744620),
        custom_id="player_setup_info"
    ),
    Button(
        label="View Clan Setup Guide",
        style=ButtonStyle.GRAY,
        emoji=PartialEmoji(name="developer", id=1263916346954088558),
        custom_id="clan_setup_info"
    ),
)




async def get_external_latency():
        host = "amazon.com"
        ping_command = ["ping", "-c", "1", host]

        try:
            output = subprocess.check_output(ping_command, stderr=subprocess.STDOUT, universal_newlines=True)
            if "time=" in output:
                ext_latency_ms = output.split("time=")[-1].split(" ")[0]
                return ext_latency_ms
        except subprocess.CalledProcessError:
            return "N/A"  

        return "N/A"



help_components = [
    ContainerComponent(
        SeparatorComponent(divider=True),
        SectionComponent(
            components=[
                TextDisplayComponent(
                    content="## Help Menu",
                ),
                TextDisplayComponent(content="-# You are suggested to check out the [Wiki](https://www.droptracker.io/wiki) for more information.\n"),
            ],
            accessory=ThumbnailComponent(
                media=logo_media
            )
        ),
        SeparatorComponent(divider=True),
        TextDisplayComponent(
                    content=(f"**User Commands**\n" +
                    "-# </accounts:1369493380534636594> - View which in-game accounts you currently have associated to your Discord account.\n" +
                    "-# </claim-rsn:1369493380358209537> - Claim an in-game character as belonging to your Discord account.\n" +
                    "-# </hideme:1369493380358209544> - Toggle whether you want your character(s) to be listed on publicly on leaderboards/global channels.\n" +
                    "-# </pingme:1369493380358209541> - Toggle whether you want to be pinged when the DropTracker sends messages for your account(s).\n"),
                ),
        SeparatorComponent(divider=True),
        TextDisplayComponent(
            content=(f"**Group Leader Commands**\n" +
                    "-# </create-group:1369493380358209543> - Create a new group in the DropTracker database.\n")
        ),
        SeparatorComponent(divider=True),
        ActionRow(
            Button(
                label="Wiki",
                style=ButtonStyle.URL,
                url="https://www.droptracker.io/wiki"
            ),
            Button(
                label="Join our Discord",
                style=ButtonStyle.URL,
                url="https://www.droptracker.io/discord"
            ),
            Button(
                label="GitHub",
                style=ButtonStyle.URL,
                url="https://github.com/DropTracker-io/"
            ),
            Button(
                label="Support us",
                style=ButtonStyle.URL,
                url="https://www.droptracker.io/account/upgrades"
            )
        ),
        SeparatorComponent(divider=True),
        InfoActionRow,
        SeparatorComponent(divider=True),
    )
]


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



class Components(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        self.player_setup = player_setup
        self.clan_setup = clan_setup
        print(f"Components service initialized.")


    @listen(Component)
    async def on_component(self, event: Component):

        if event.ctx.custom_id == "clan_setup_info":
            await self.send_clan_setup_info(event.ctx)
        elif event.ctx.custom_id == "player_setup_info":
            await self.send_player_setup_info(event.ctx)




    async def send_player_setup_info(self, ctx: ComponentContext):
        components = self.player_setup
        await ctx.send(components=components, ephemeral=True)



    async def send_clan_setup_info(self, ctx: ComponentContext):
        components = self.clan_setup
        await ctx.send(components=components, ephemeral=True)


    
