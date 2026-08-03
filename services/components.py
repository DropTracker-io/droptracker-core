
import subprocess
import interactions
from interactions import ComponentContext, Extension, ActionRow, Button, ButtonStyle, FileComponent, PartialEmoji, Permissions, SlashContext, UnfurledMediaItem, listen, slash_command
from interactions.api.events import Startup, Component, ComponentCompletion, ComponentError, ModalCompletion, ModalError, MessageCreate
from interactions.models import ContainerComponent, ThumbnailComponent, SeparatorComponent, UserSelectMenu, SlidingWindowSystem, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, MediaGalleryComponent, MediaGalleryItem, OverwriteType
from utils.site_urls import PREMIUM_URL, WEBSITE_URL




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



async def build_help_components(bot=None):
    """
    Build the help menu components with dynamically-resolved command IDs.

    Args:
        bot: The bot instance used for command ID resolution.  If None,
             mention links fall back to ID 0 (still renders the command name).

    Returns:
        List of components suitable for ctx.send(components=...).
    """
    from utils.format import get_command_id

    async def _cid(name: str):
        """Return the Discord command ID for *name*, or 0 as a safe fallback."""
        if bot is None:
            return 0
        result = await get_command_id(bot, name)
        if result and result != "`command not yet added`":
            return result
        return 0

    accounts_id        = await _cid("accounts")
    claim_rsn_id       = await _cid("claim-rsn")
    unclaim_rsn_id     = await _cid("unclaim-rsn")
    dm_settings_id     = await _cid("dm-settings")
    hideme_id          = await _cid("hideme")
    pingme_id          = await _cid("pingme")
    my_points_id       = await _cid("my-points")
    group_points_id    = await _cid("group-points")
    create_group_id    = await _cid("create-group")
    sync_wom_id        = await _cid("sync-wom")
    reset_points_id    = await _cid("reset-group-points")
    force_sync_id      = await _cid("force-group-sync")
    player_faq_id      = await _cid("send_player_faq")

    return [
        ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content="## Help Menu",
                    ),
                    TextDisplayComponent(
                        content="-# You are suggested to check out the [Wiki](https://www.droptracker.io/wiki) for more information.\n"
                    ),
                ],
                accessory=ThumbnailComponent(media=logo_media)
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "**User Commands**\n"
                    f"-# </accounts:{accounts_id}> - View your currently claimed in-game accounts.\n"
                    f"-# </claim-rsn:{claim_rsn_id}> - Claim an in-game character as belonging to your Discord account.\n"
                    f"-# </unclaim-rsn:{unclaim_rsn_id}> - Remove a RuneScape account from your Discord account.\n"
                    f"-# </dm-settings:{dm_settings_id}> - Configure your direct message notification preferences.\n"
                    f"-# </hideme:{hideme_id}> - Toggle whether your character(s) appear on public leaderboards/global channels.\n"
                    f"-# </pingme:{pingme_id}> - Toggle whether you get pinged when your submissions are sent to Discord.\n"
                    f"-# </my-points:{my_points_id}> - View your earned points across all groups.\n"
                    f"-# </group-points:{group_points_id}> - View this server's group point standings.\n"
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "**Group Leader / Admin Commands**\n"
                    f"-# </create-group:{create_group_id}> - Create a new group in the DropTracker database.\n"
                    f"-# </sync-wom:{sync_wom_id}> - Request an immediate WiseOldMan membership sync (1-hour cooldown).\n"
                    f"-# </reset-group-points:{reset_points_id}> - Reset your group's points back to zero.\n"
                    f"-# </force-group-sync:{force_sync_id}> - Force a WiseOldMan membership sync for your group.\n"
                    f"-# </send_player_faq:{player_faq_id}> - Post a player FAQ/setup message to a channel.\n"
                )
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
                    url="https://discord.gg/droptracker"
                ),
                Button(
                    label="GitHub",
                    style=ButtonStyle.URL,
                    url="https://github.com/DropTracker-io/"
                ),
                Button(
                    label="Support us",
                    style=ButtonStyle.URL,
                    url=PREMIUM_URL
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


async def build_player_setup(bot=None):
    """
    Build the player FAQ/setup guide with dynamically-resolved command IDs.

    Args:
        bot: The bot instance used for command ID resolution.

    Returns:
        List of components suitable for ctx.send(components=...).
    """
    from utils.format import get_command_id

    async def _cid(name: str):
        if bot is None:
            return 0
        result = await get_command_id(bot, name)
        if result and result != "`command not yet added`":
            return result
        return 0

    claim_rsn_id   = await _cid("claim-rsn")
    unclaim_rsn_id = await _cid("unclaim-rsn")
    dm_settings_id = await _cid("dm-settings")
    hideme_id      = await _cid("hideme")
    pingme_id      = await _cid("pingme")

    return [
        ContainerComponent(
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content="## Player FAQs - DropTracker.io",
            ),
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content="-# **What is the DropTracker?**\n" +
                        "-# > A community-driven, all-in-one loot and achievement tracking system built for Old School RuneScape groups.\n" +
                        "-# > We leverage the *[WiseOldMan](https://wiseoldman.net)* to manage group memberships, and provide group leaders a seamless way to configure their group's achievement notification settings.\n\n" +
                        "-# **How do I get started?**\n" +
                        "-# > 1. Install the **DropTracker** plugin on your RuneLite client, via the plugin hub.\n" +
                        "-# > 2. Visit the plugin settings panel (gear tab on RuneLite side panel) to configure which achievements you *personally* want tracked.\n" +
                        f"-# > 3. (Optionally) Claim your in-game-name using the </claim-rsn:{claim_rsn_id}> command to associate your Discord account with your character(s).\n\n" +
                        "-# **How can I get pinged when my account(s) have notifications sent?**\n" +
                        f"-# > Using the </claim-rsn:{claim_rsn_id}> command, entering your in-game-name **exactly as it appears**.\n\n" +
                        "-# **How can I prevent my submissions from being shared to the global DropTracker discord channels?**\n" +
                        f"-# > Using the </hideme:{hideme_id}> command, and selecting which account(s)/context(s) you want to be hidden from.\n\n" +
                        "-# **How can I get (or not get) pinged by the <@1172933457010245762> bot when my account(s) have notifications sent?**\n" +
                        f"-# > Using the </pingme:{pingme_id}> command, and selecting which account(s)/context(s) you do or do not want to receive pings for.\n\n" +
                        "-# **How can I remove a previously claimed account from my Discord?**\n" +
                        f"-# > Using the </unclaim-rsn:{unclaim_rsn_id}> command, and selecting the account you want to disassociate.\n\n" +
                        "-# **How do I manage direct message notifications from the bot?**\n" +
                        f"-# > Using the </dm-settings:{dm_settings_id}> command to enable or disable account-related DMs, such as name-change notifications for your claimed accounts.\n\n" +
                        "-# **What types of information does the DropTracker store about me and my account(s)?**\n" +
                        "-# 1. Your account(s) unique identifier, or 'account hash'. This is provided by Jagex, and is unique to each individual character; remaining consistent thru name changes.\n" +
                        "-# 2. Your submitted achievements/drops.\n\n" +
                        "-# 3. Your Discord ID (if you claim your account or execute commands through our bot)\n\n" +
                        "-# **What can I do to support the continued development of the DropTracker project?**\n\n" +
                        "-# This passion project began as something far more simple, and has continued to evolve into what you see before you today.\n" +
                        "-# Without the continued support of our premium groups, the development work we do would be impossible.\n" +
                        "-# If you feel as though we've provided a notable value to your OSRS experience, feel free to show support through our [Patreon](https://www.patreon.com/droptracker).\n" +
                        "-# Players who have subscribed and then upgraded their groups using that subscription are provided early access to new features, alongside a few premium-only functionalities."
                    )
                ],
                accessory=ThumbnailComponent(
                    media=logo_media
                )
            ),
            SeparatorComponent(divider=True),
        )
    ]

async def build_clan_setup(bot=None):
    """
    Build the clan setup guide with dynamically-resolved command IDs.

    Args:
        bot: The bot instance used for command ID resolution.

    Returns:
        List of components suitable for ctx.send(components=...).
    """
    from utils.format import get_command_id

    async def _cid(name: str):
        if bot is None:
            return 0
        result = await get_command_id(bot, name)
        if result and result != "`command not yet added`":
            return result
        return 0

    create_group_id   = await _cid("create-group")
    sync_wom_id       = await _cid("sync-wom")
    reset_points_id   = await _cid("reset-group-points")
    force_sync_id     = await _cid("force-group-sync")
    player_faq_id     = await _cid("send_player_faq")

    return [
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
                        content=f"-# If you have all of these, grab your **WiseOldMan Group ID** (3-6 digits maximum, with no hyphens), and use </create-group:{create_group_id}> in your group's Discord server to get started.\n" +
                        "-# Once you create a group, you should be DMed with a welcome message; and a link to configure your group settings.\n\n" +
                        f"-# After creating a group, you can also [click here]({WEBSITE_URL}/dashboard), then click your group name to find your group config page."
                    )
                ],
                accessory=ThumbnailComponent(
                    media=UnfurledMediaItem(
                        url="https://www.droptracker.io/img/wom-example.png"
                    )
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "**Additional Admin Commands**\n"
                    f"-# </sync-wom:{sync_wom_id}> - Request an immediate WiseOldMan membership sync for your group (1-hour cooldown).\n"
                    f"-# </force-group-sync:{force_sync_id}> - Force a full WiseOldMan membership sync bypassing the cooldown (admin only).\n"
                    f"-# </reset-group-points:{reset_points_id}> - Reset all group points back to zero (irreversible).\n"
                    f"-# </send_player_faq:{player_faq_id}> - Post a player FAQ/setup message to a channel in your server.\n"
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
                        content=f"-# We also offer some premium features for groups when they [upgrade their account]({PREMIUM_URL}).\n" +
                        "-# Please consider subscribing to support the development of the project.",
                    ),
                ],
                accessory=Button(
                    label="Upgrade",
                    style=ButtonStyle.LINK,
                    emoji=PartialEmoji(name="supporter", id=1263827303712948304),
                    url=PREMIUM_URL
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
        print(f"Components service initialized.")


    @listen(Component)
    async def on_component(self, event: Component):

        if event.ctx.custom_id == "clan_setup_info":
            await self.send_clan_setup_info(event.ctx)
        elif event.ctx.custom_id == "player_setup_info":
            await self.send_player_setup_info(event.ctx)




    async def send_player_setup_info(self, ctx: ComponentContext):
        components = await build_player_setup(self.bot)
        await ctx.send(components=components, ephemeral=True)



    async def send_clan_setup_info(self, ctx: ComponentContext):
        components = await build_clan_setup(self.bot)
        await ctx.send(components=components, ephemeral=True)


    
