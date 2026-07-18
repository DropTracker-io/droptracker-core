"""
News / update-channel opt-in.

Members opt into three private update channels (plugin / website / discord) by
pressing a single button, which toggles the ``Follows Updates`` role. That role
is granted view access to all three channels, so one role toggle unlocks (or
hides) the whole set.

Why a role and not per-user channel adds? Two reasons:
  * Discord caps permission overwrites at 500 per channel; this guild already
    has ~570 members, so per-user "add to channel" would break at scale.
  * The earlier design added users to forum *threads*, which makes Discord post
    an "X added Y" system message that is **undeletable** (API error 50021 —
    "Cannot execute action on a system message"). A role produces no such
    clutter.

Setup is idempotent and performed by the bot itself — on startup (so a restart
applies it) and whenever the owner runs ``/post-update-optin``: create the role
if missing, grant it view access on each channel, and seed any initial
followers. The ``@listen(Component)`` handler is persistent (matches on
custom_id) so the posted button keeps working across restarts.

Author: joelhalen
"""

import asyncio

import interactions
from interactions import (
    ActionRow, Button, ButtonStyle, ComponentContext, Extension, OverwriteType,
    Permissions, SlashContext, check, is_owner, listen, slash_command,
)
from interactions.api.events import Component
from interactions.client import errors as ix_errors
from interactions.models import (
    ContainerComponent, SeparatorComponent, TextDisplayComponent,
)
from db.app_logger import AppLogger

app_logger = AppLogger()

# --- Configuration -------------------------------------------------------
# Guild the update channels live in (used by the startup setup pass, which has
# no interaction context to infer it from).
GUILD_ID = 1172737525069135962

# The opt-in role. Resolved/created by name, so no id needs hardcoding.
FOLLOW_ROLE_NAME = "Follows Updates"

# The three private update channels the role unlocks.
# (emoji, label, one-line description, channel_id)
UPDATE_CHANNELS = [
    ("🔌", "Plugin updates",  "RuneLite plugin releases & fixes", 1528029600104583208),  # forum
    ("🌐", "Website updates", "website & dashboard changes",      1528029728483704873),  # text
    ("💬", "Discord updates", "server & bot changes",             1528029693079588924),  # text
]

# Public channel anyone can rely on without opting in.
NEWS_CHANNEL_ID = 1527845346582073426

# One-time migration seed: members from the old update threads who should keep
# access on the new channels. Granted the role during setup.
INITIAL_FOLLOWER_IDS = [431415094627270656]

# Permissions the role gets on each update channel (read-only "follow").
_FOLLOW_ALLOW = [Permissions.VIEW_CHANNEL, Permissions.READ_MESSAGE_HISTORY]

# Stable custom_id linking the button to the handler below.
OPTIN_BUTTON_ID = "news_optin_all"


def _channel_mentions():
    return ", ".join(f"<#{cid}>" for (_e, _l, _d, cid) in UPDATE_CHANNELS if cid)


def build_optin_components():
    """Build the components-v2 message: explanation + single follow button."""
    bullets = "\n".join(
        f"-# {emoji} <#{cid}> — {desc}"
        for (emoji, label, desc, cid) in UPDATE_CHANNELS
    )
    return [
        ContainerComponent(
            TextDisplayComponent(
                content="Hey, <@&1279163761218949204>! :wave:"
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "## Staying in the loop\n"
                    "We're aiming to reduce the amount of clutter sent when we make "
                    "changes to our app and services. To continue staying updated, "
                    "you have two options:"
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "### 1. Follow the update channels\n"
                    "-# Press the button below and I'll unlock these three channels "
                    "for you:\n"
                    f"{bullets}\n"
                    "-# Press it again any time to unfollow and hide them."
                )
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "### 2. Or just stay put\n"
                    "-# Prefer not to? No action needed — any *important* news will "
                    f"still go out in the public <#{NEWS_CHANNEL_ID}> channel regardless."
                )
            ),
            ActionRow(
                Button(
                    label="🔔 Follow the update channels",
                    style=ButtonStyle.SUCCESS,
                    custom_id=OPTIN_BUTTON_ID,
                )
            ),
            SeparatorComponent(divider=True),
        )
    ]


class NewsOptin(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        # Extensions load from main.py's Startup handler, so a `@listen(Startup)`
        # here would register too late to ever fire. But __init__ runs inside
        # that same (already-connected, loop-active) window, so schedule the
        # idempotent setup as a background task — this is what makes a plain
        # restart provision the role, channel access, and migrated followers.
        try:
            self._setup_task = asyncio.create_task(self._startup_setup())
        except RuntimeError:
            # No running loop (e.g. imported outside the bot) — the owner
            # command performs setup instead.
            self._setup_task = None

    async def _startup_setup(self):
        """One-shot setup a few seconds after load, once the gateway settles."""
        await asyncio.sleep(5)
        try:
            guild = await self.bot.fetch_guild(GUILD_ID)
            if guild:
                await self._ensure_setup(guild)
        except Exception as e:
            app_logger.log(
                log_type="warning",
                data=f"news opt-in: startup setup failed: {e}",
                app_name="core",
                description="news_optin",
            )

    # --- role resolution / setup ----------------------------------------
    def _find_role(self, guild):
        """Return the Follows-Updates role from the guild's roles, or None."""
        for r in guild.roles:
            if r.name == FOLLOW_ROLE_NAME:
                return r
        return None

    async def _ensure_setup(self, guild, *, apply_perms: bool = False):
        """Idempotently ensure the role exists, has view access on each update
        channel, and that the initial followers hold it. Returns the role.

        Channel overwrites are (re)applied when the role is first created, or
        when *apply_perms* is set (the owner command passes it, to self-heal a
        removed overwrite). Follower seeding runs every call — it's cheap and
        keeps the migrated members' access from drifting."""
        role = self._find_role(guild)
        if role is None:
            role = await guild.create_role(
                name=FOLLOW_ROLE_NAME,
                hoist=False,
                mentionable=False,
                reason="Opt-in role for update channels",
            )
            apply_perms = True  # brand-new role → must grant channel access

        if apply_perms:
            for (_e, label, _d, cid) in UPDATE_CHANNELS:
                channel = guild.get_channel(cid) or await self.bot.fetch_channel(cid)
                await channel.add_permission(
                    role,
                    type=OverwriteType.ROLE,
                    allow=_FOLLOW_ALLOW,
                    reason="Grant Follows Updates access to update channel",
                )

        for uid in INITIAL_FOLLOWER_IDS:
            try:
                member = await guild.fetch_member(uid)
                if member and not member.has_role(role):
                    await member.add_role(role, reason="Migrate existing follower")
            except Exception as e:
                app_logger.log(
                    log_type="warning",
                    data=f"news opt-in: couldn't seed follower {uid}: {e}",
                    app_name="core",
                    description="news_optin",
                )
        return role

    # --- button ---------------------------------------------------------
    @listen(Component)
    async def on_component(self, event: Component):
        if event.ctx.custom_id == OPTIN_BUTTON_ID:
            await self._handle_optin(event.ctx)

    async def _handle_optin(self, ctx: ComponentContext):
        """Toggle the Follows-Updates role for the presser."""
        await ctx.defer(ephemeral=True)

        guild = ctx.guild or await self.bot.fetch_guild(ctx.guild_id)
        member = ctx.author
        if guild is None or member is None:
            await ctx.send("Please press this from within the server.", ephemeral=True)
            return

        role = self._find_role(guild)
        if role is None:
            await ctx.send(
                "⚠️ Update following isn't set up yet — please let an admin know.",
                ephemeral=True,
            )
            return

        chans = _channel_mentions()
        try:
            if member.has_role(role):
                await member.remove_role(role, reason="Opted out of updates")
                await ctx.send(
                    f"🔕 You've unfollowed updates — {chans} are hidden again.\n"
                    "Press the button any time to re-follow.",
                    ephemeral=True,
                )
            else:
                await member.add_role(role, reason="Opted in to updates")
                await ctx.send(
                    f"✅ You're now following updates! You can see {chans}.\n"
                    "Press the button again any time to unfollow.",
                    ephemeral=True,
                )
        except ix_errors.Forbidden:
            await ctx.send(
                "❌ I don't have permission to change your roles — please let an admin know.",
                ephemeral=True,
            )
        except Exception as e:
            app_logger.log(
                log_type="error",
                data=f"news opt-in: toggle failed for {getattr(member, 'id', '?')}: {e}",
                app_name="core",
                description="news_optin",
            )
            await ctx.send(
                "❌ Something went wrong — please let an admin know.",
                ephemeral=True,
            )

    # --- owner command --------------------------------------------------
    @slash_command(
        name="post-update-optin",
        description="Set up the Follows-Updates role + access, then post the opt-in message here.",
        default_member_permissions=Permissions.ADMINISTRATOR,
    )
    @check(is_owner())
    async def post_update_optin_cmd(self, ctx: SlashContext):
        """Ensure setup (role, channel access, seed followers) then post the
        opt-in message. Bot-owner only."""
        await ctx.defer(ephemeral=True)
        try:
            await self._ensure_setup(ctx.guild, apply_perms=True)
        except ix_errors.Forbidden:
            await ctx.send(
                "❌ I couldn't create the role / set channel permissions — "
                "check I have **Manage Roles** and **Manage Channels**.",
                ephemeral=True,
            )
            return
        except Exception as e:
            app_logger.log(
                log_type="error",
                data=f"news opt-in: setup via command failed: {e}",
                app_name="core",
                description="news_optin",
            )
            await ctx.send("❌ Setup failed — check the logs.", ephemeral=True)
            return
        await ctx.channel.send(components=build_optin_components())
        await ctx.send("Setup complete and opt-in message posted. ✅", ephemeral=True)
