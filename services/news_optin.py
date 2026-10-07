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

Clan leaders follow by default (``services/leader_updates.py``): a sweep hands
the role to every group owner/admin in the server, and the button is their way
out. Pressing it stores the choice, so a leader who turned it off is never
given the role back. Until ``LEADER_UPDATES_LIVE`` is on, the sweep only
touches the pilot accounts.

Author: joelhalen
"""

import asyncio

import interactions
from interactions import (
    ActionRow, Button, ButtonStyle, ComponentContext, Extension, IntervalTrigger,
    OverwriteType, Permissions, SlashContext, Task, check, is_owner, listen,
    slash_command,
)
from interactions.api.events import Component
from interactions.client import errors as ix_errors
from interactions.models import (
    ContainerComponent, SeparatorComponent, TextDisplayComponent,
)
from db.app_logger import AppLogger
from services import leader_updates

app_logger = AppLogger()

# --- Configuration -------------------------------------------------------
# Guild the update channels live in (used by the startup setup pass, which has
# no interaction context to infer it from).
GUILD_ID = leader_updates.HQ_GUILD_ID

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
NEWS_CHANNEL_ID = leader_updates.NEWS_CHANNEL_ID

# One-time migration seed: members from the old update threads who should keep
# access on the new channels. Granted the role during setup.
INITIAL_FOLLOWER_IDS = [431415094627270656]

# Permissions the role gets on each update channel (read-only "follow").
_FOLLOW_ALLOW = [Permissions.VIEW_CHANNEL, Permissions.READ_MESSAGE_HISTORY]

# Stable custom_id linking the button to the handler below.
OPTIN_BUTTON_ID = "news_optin_all"

# How often the leader sweep runs. Cheap when there is nothing new: one query
# plus a Redis set read; Discord is only asked about leaders not yet handled.
LEADER_SWEEP_MINUTES = 10
# Spacing between member lookups, to stay well inside the rate limit.
LEADER_LOOKUP_SPACING_SECONDS = 1.0


def _redis_conn():
    try:
        from utils.redis import redis_client

        return redis_client.client
    except Exception:
        return None


def _load_pending_grants() -> list:
    """Thread-side: the next leaders the sweep should look up."""
    from db.models.base import Session

    session = Session()
    try:
        return leader_updates.pending_role_grants(session, _redis_conn())
    finally:
        session.rollback()
        session.close()


def _save_follow_pref(discord_id, following: bool) -> None:
    """Thread-side: remember a button press so the sweep respects it."""
    from db.models.base import Session

    session = Session()
    try:
        leader_updates.set_follow_pref(session, discord_id, following)
        conn = _redis_conn()
        if conn is not None:
            leader_updates.mark_handled(conn, discord_id)
    finally:
        session.close()


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
        # Started here, not in __init__: Task.start needs the running loop.
        self.leader_role_sweep.start()

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

    # --- leader sweep ---------------------------------------------------
    @Task.create(IntervalTrigger(minutes=LEADER_SWEEP_MINUTES))
    async def leader_role_sweep(self):
        try:
            await self._leader_role_sweep()
        except Exception as e:
            app_logger.log(
                log_type="warning",
                data=f"news opt-in: leader sweep failed: {e}",
                app_name="core",
                description="news_optin",
            )

    async def _leader_role_sweep(self) -> dict:
        """Give the Follows Updates role to clan leaders who don't have it yet.

        Pilot accounts only until LEADER_UPDATES_LIVE. Returns counts for logs."""
        todo = await asyncio.to_thread(_load_pending_grants)
        counts = {"granted": 0, "had_role": 0, "absent": 0, "errors": 0}
        if not todo:
            return counts
        guild = await self.bot.fetch_guild(GUILD_ID)
        role = self._find_role(guild) if guild else None
        if role is None:
            return counts
        conn = _redis_conn()
        if conn is None:
            return counts

        for discord_id in todo:
            try:
                member = await guild.fetch_member(int(discord_id), force=True)
                if member is None:
                    leader_updates.mark_absent(conn, discord_id)
                    counts["absent"] += 1
                elif member.has_role(role):
                    leader_updates.mark_handled(conn, discord_id)
                    counts["had_role"] += 1
                else:
                    await member.add_role(role, reason="Clan leader: follows updates by default")
                    leader_updates.mark_handled(conn, discord_id)
                    counts["granted"] += 1
            except Exception as e:
                # Transient: left unmarked, so the next sweep tries again.
                counts["errors"] += 1
                app_logger.log(
                    log_type="warning",
                    data=f"news opt-in: leader {discord_id} not handled: {e}",
                    app_name="core",
                    description="news_optin",
                )
            await asyncio.sleep(LEADER_LOOKUP_SPACING_SECONDS)

        app_logger.log(
            log_type="info",
            data=f"news opt-in: leader sweep {counts} (live={leader_updates.is_live()})",
            app_name="core",
            description="news_optin",
        )
        return counts

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
            following = not member.has_role(role)
            if following:
                await member.add_role(role, reason="Opted in to updates")
            else:
                await member.remove_role(role, reason="Opted out of updates")
            try:
                await asyncio.to_thread(_save_follow_pref, member.id, following)
            except Exception as e:
                app_logger.log(
                    log_type="warning",
                    data=f"news opt-in: couldn't save choice for {member.id}: {e}",
                    app_name="core",
                    description="news_optin",
                )
            if following:
                await ctx.send(
                    f"✅ You're now following updates! You can see {chans}.\n"
                    "Press the button again any time to unfollow.",
                    ephemeral=True,
                )
            else:
                await ctx.send(
                    f"🔕 You've unfollowed updates. {chans} are hidden again, "
                    "and they'll stay off until you press the button again.",
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
