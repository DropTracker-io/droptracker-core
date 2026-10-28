
import subprocess
import interactions
from interactions import ComponentContext, Extension, ActionRow, Button, ButtonStyle, FileComponent, PartialEmoji, Permissions, SlashContext, UnfurledMediaItem, listen, slash_command
from interactions.api.events import Startup, Component, ComponentCompletion, ComponentError, ModalCompletion, ModalError, MessageCreate
from interactions.models import ContainerComponent, ThumbnailComponent, SeparatorComponent, UserSelectMenu, SlidingWindowSystem, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, MediaGalleryComponent, MediaGalleryItem, OverwriteType
from utils.app_emojis import partial_emoji as app_partial_emoji
from utils.site_urls import PREMIUM_URL, WEBSITE_URL
from services.guild_permissions import INVITE_PERMISSIONS

# One invite URL for every surface. Previously this file asked for
# permissions=8 (Administrator) while the website's /meta/bot-invite asked
# for none — now both derive from the same minimal bitfield.
BOT_INVITE_URL = ("https://discord.com/oauth2/authorize"
                  f"?client_id=1172933457010245762&permissions={INVITE_PERMISSIONS}"
                  "&scope=bot%20applications.commands")


logo_media = UnfurledMediaItem(
    url="https://www.droptracker.io/img/droptracker-small.gif"
)



def info_action_row() -> ActionRow:
    """The "how do I set this up" button pair.

    A function rather than a module constant because the button emoji are
    application emojis, and which application this process is is only settled
    once the bot has called ``utils.app_emojis.use_profile`` — which happens
    after imports.
    """
    return ActionRow(
        Button(
            label="View Player Setup/Info",
            style=ButtonStyle.GRAY,
            emoji=app_partial_emoji("newmember"),
            custom_id="player_setup_info"
        ),
        Button(
            label="View Clan Setup Guide",
            style=ButtonStyle.GRAY,
            emoji=app_partial_emoji("developer"),
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
    death_message_id   = await _cid("death-message")
    hideme_id          = await _cid("hideme")
    pingme_id          = await _cid("pingme")
    my_points_id       = await _cid("my-points")
    group_points_id    = await _cid("group-points")
    lookup_id          = await _cid("lookup")
    # Sub-commands are cached under their full "<base> <sub>" name, and every
    # sub-command carries the *base* command's ID — which is exactly what the
    # </submit drop:ID> mention syntax expects.
    submit_id          = await _cid("submit drop")
    sync_wom_id        = await _cid("sync-wom")
    reset_points_id    = await _cid("reset-group-points")
    force_sync_id      = await _cid("force-group-sync")
    player_faq_id      = await _cid("send_player_faq")
    split_tracking_id  = await _cid("toggle-split-tracking")
    add_points_id      = await _cid("add-group-points")
    remove_points_id   = await _cid("remove-group-points")
    points_audit_id    = await _cid("group-points-audit")
    settings_id        = await _cid("settings")
    clan_log_id        = await _cid("clan-log")
    group_setup_id     = await _cid("group-setup")

    return [
        ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[
                    TextDisplayComponent(
                        content="## Help Menu",
                    ),
                    TextDisplayComponent(
                        content=f"-# Guides for everything are in [our docs]({WEBSITE_URL}/docs).\n"
                    ),
                ],
                accessory=ThumbnailComponent(media=logo_media)
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "**User Commands**\n"
                    f"-# </settings:{settings_id}> - Your notifications and privacy, all in one panel.\n"
                    f"-# </accounts:{accounts_id}> - View your currently claimed in-game accounts.\n"
                    f"-# </claim-rsn:{claim_rsn_id}> - Claim an in-game character as belonging to your Discord account.\n"
                    f"-# </unclaim-rsn:{unclaim_rsn_id}> - Remove a RuneScape account from your Discord account.\n"
                    f"-# </dm-settings:{dm_settings_id}> - Configure your direct message notification preferences.\n"
                    f"-# </death-message:{death_message_id}> - Write what your clan sees when you die (if your clan allows members' own messages).\n"
                    f"-# </hideme:{hideme_id}> - Toggle whether your character(s) appear on public leaderboards/global channels.\n"
                    f"-# </pingme:{pingme_id}> - Toggle whether you get pinged when your submissions are sent to Discord.\n"
                    f"-# </my-points:{my_points_id}> - View your clan points: your rank, your accounts and your recent awards.\n"
                    f"-# </group-points:{group_points_id}> - Browse this server's points leaderboard, by period or season.\n"
                    f"-# </lookup:{lookup_id}> - Look up another player's clan points and DropTracker stats.\n"
                    f"-# </clan-log:{clan_log_id}> - See which boss uniques your clan has, and what's still missing.\n"
                    f"-# </submit drop:{submit_id}> - Manually submit something you received, if the plugin missed it.\n"
                    "-# > Swap `drop` for `clog`, `pb`, `ca` or `pet` to submit any other achievement type.\n"
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "**Group Leader / Admin Commands**\n"
                    f"-# </group-setup:{group_setup_id}> - Set up your clan's group, or change its settings, right here in Discord.\n"
                    f"-# </sync-wom:{sync_wom_id}> - Request an immediate WiseOldMan membership sync (1-hour cooldown).\n"
                    f"-# </force-group-sync:{force_sync_id}> - Force a WiseOldMan membership sync for your group.\n"
                    f"-# </toggle-split-tracking:{split_tracking_id}> - Enable or disable split GP tracking for your group.\n"
                    f"-# </add-group-points:{add_points_id}> - Manually add points to a member's balance.\n"
                    f"-# </remove-group-points:{remove_points_id}> - Manually remove points from a member's balance.\n"
                    f"-# </group-points-audit:{points_audit_id}> - View recent manual point adjustments for your group.\n"
                    f"-# </reset-group-points:{reset_points_id}> - Reset your group's points back to zero.\n"
                    f"-# </send_player_faq:{player_faq_id}> - Post a player FAQ/setup message to a channel.\n"
                    "-# > Right-click menus: **Modify Entry** (on a drop notification) to edit or delete it, "
                    "and **Player Lookup** (on a member) to view their tracked accounts.\n"
                )
            ),
            SeparatorComponent(divider=True),
            ActionRow(
                Button(
                    label="Docs",
                    style=ButtonStyle.URL,
                    url=f"{WEBSITE_URL}/docs"
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
            info_action_row(),
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
                url=BOT_INVITE_URL
            )
        ),
        SeparatorComponent(divider=True),
    )
]


async def _command_mentions(bot, names):
    """{name: "</name:id>"} for the given commands; plain `/name` when the id
    can't be resolved (bot not connected, command not registered yet)."""
    from utils.format import get_command_id

    out = {}
    for name in names:
        cid = 0
        if bot is not None:
            try:
                result = await get_command_id(bot, name)
                if result and result != "`command not yet added`":
                    cid = result
            except Exception:
                cid = 0
        out[name] = f"</{name}:{cid}>" if cid else f"`/{name}`"
    return out


async def build_player_setup(bot=None, group_name=None):
    """The members' getting-started guide (``/send_player_faq`` and the
    "player setup" button), worded by ``utils.member_guide`` so it matches
    the copy the website posts.

    Returns:
        List of components suitable for ctx.send(components=...).
    """
    from utils import member_guide

    mentions = await _command_mentions(
        bot, ["claim-rsn", "pingme", "hideme", "submit drop", "my-points", "settings"])
    body = "\n\n".join(f"**{h}**\n{b}" for h, b in member_guide.sections(lambda n: mentions[n]))
    return [
        ContainerComponent(
            SectionComponent(
                components=[
                    TextDisplayComponent(content=f"## {member_guide.TITLE}"),
                    TextDisplayComponent(content=member_guide.intro(group_name)),
                ],
                accessory=ThumbnailComponent(media=logo_media),
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(content=body),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "-# **What do you store about me?** Your account's Jagex account hash (it stays the same "
                    "through name changes), what you submit, and your Discord id once you link. "
                    f"[Privacy policy]({WEBSITE_URL}/privacy)"
                )
            ),
            ActionRow(
                Button(label="Get the plugin", style=ButtonStyle.LINK, url=member_guide.PLUGIN_URL),
                Button(label="Link your account", style=ButtonStyle.LINK, url=member_guide.REGISTER_URL),
                Button(label="Docs", style=ButtonStyle.LINK, url=member_guide.DOCS_URL),
            ),
        )
    ]


def _free_feature_lines() -> list:
    from db.group_features import _FALLBACK_FREE_FEATURES, free_tier_features
    from db.models import Session

    s = Session()
    try:
        return free_tier_features(s)
    except Exception:
        return list(_FALLBACK_FREE_FEATURES)
    finally:
        s.close()


async def build_clan_setup(bot=None):
    """"What is DropTracker?": what a clan gets (free), and how to start.

    The feature list is the Free plan's own list (``subscription_tiers``,
    the same lines as the /premium Free card), so it can't drift from the
    website. Points to ``/group-setup`` and the website wizard, the two
    current ways in.

    Returns:
        List of components suitable for ctx.send(components=...).
    """
    import asyncio

    lines = await asyncio.to_thread(_free_feature_lines)
    mentions = await _command_mentions(bot, ["group-setup", "help"])
    free = "\n".join(f"- {line}" for line in lines)
    return [
        ContainerComponent(
            SectionComponent(
                components=[
                    TextDisplayComponent(content="## What is The DropTracker?"),
                    TextDisplayComponent(
                        content=(
                            "Loot and achievement tracking for Old School RuneScape clans. Members install "
                            "one RuneLite plugin, and their drops, personal bests, collection logs, pets "
                            "and more show up here in Discord and on the website."
                        )
                    ),
                ],
                accessory=ThumbnailComponent(media=logo_media),
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(content=f"**Free for every clan**\n{free}"),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "**Get started in about two minutes**\n"
                    "You need a [Wise Old Man group](https://wiseoldman.net/groups) for your clan "
                    "(we sync members from it) and Manage Server in this server.\n"
                    f"- Run {mentions['group-setup']} right here, or\n"
                    f"- use the [setup wizard on the website]({WEBSITE_URL}/groups/new)."
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    f"-# Paid plans add a Hall of Fame, a clan website and faster lootboards. "
                    f"[Compare plans]({PREMIUM_URL}). Every command: {mentions['help']}. "
                    "Questions? [Join our Discord](https://discord.gg/droptracker)."
                )
            ),
            ActionRow(
                Button(label="Set up on the website", style=ButtonStyle.LINK, url=f"{WEBSITE_URL}/groups/new"),
                Button(label="Docs", style=ButtonStyle.LINK, url=f"{WEBSITE_URL}/docs/create-group"),
                Button(label="Invite the bot", style=ButtonStyle.LINK, url=BOT_INVITE_URL),
            ),
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


    
