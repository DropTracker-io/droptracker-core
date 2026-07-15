import asyncio
import time
from datetime import datetime
import interactions
from sqlalchemy import text
from interactions import ActionRow, Button, ButtonStyle, ComponentContext, Embed, Extension, IntervalTrigger, OverwriteType, Permissions, Task, slash_command, slash_option, OptionType, SlashContext, listen
from interactions.api.events import MessageCreate, MessageUpdate, Component, Startup
from interactions.models import (
    ContainerComponent,
    SectionComponent,
    SeparatorComponent,
    TextDisplayComponent,
    ThumbnailComponent,
    UnfurledMediaItem,
)

from commands import try_create_user
from db.models import Drop, Group, Player, Ticket, User, Session, user_group_association
from services.ticket_transcripts import (
    backfill_open_tickets,
    close_and_archive,
    upsert_message,
)
from utils.format import format_number
from utils.redis import redis_client

SUPPORT_ROLE_ID = 1176291872143052831

_LOGO_MEDIA = UnfurledMediaItem(url="https://www.droptracker.io/img/droptracker-small.gif")

# Per-type copy for the welcome card. Unknown types fall back to _DEFAULT_TYPE_META.
TICKET_TYPE_META = {
    "players": {
        "label": "Player Support",
        "emoji": "🧍",
        "accent": 0x3498DB,
        "intro": "Something not tracking right on one of your accounts? Let's get it sorted.",
        "checklist": [
            "The **exact in-game name(s)** of the affected account(s)",
            "What you expected to happen vs. what actually happened",
            "Roughly **when** the issue occurred, so we can check the logs",
            "A screenshot of your **RuneLite DropTracker plugin settings**, if submissions aren't arriving",
        ],
    },
    "clans": {
        "label": "Clan / Group Support",
        "emoji": "🏰",
        "accent": 0x2ECC71,
        "intro": "Questions about your group's setup, configuration, or tracking? We can help.",
        "checklist": [
            "Your **group name** (or group ID, if you know it)",
            "Which feature or setting the question is about (notifications, lootboard, events, ...)",
            "Screenshots of any settings or messages involved",
            "Whether your **WiseOldMan group** is up to date, if it's a membership issue",
        ],
    },
    "support": {
        "label": "Technical Support",
        "emoji": "🛠️",
        "accent": 0xE67E22,
        "intro": "Hit a bug or something behaving strangely? Tell us about it.",
        "checklist": [
            "A description of the problem and **where it happens** (plugin, Discord bot, or website)",
            "What you expected to happen vs. what actually happened",
            "Screenshots or error messages, if you have any",
        ],
    },
    "other": {
        "label": "General Inquiry",
        "emoji": "📩",
        "accent": 0x9B59B6,
        "intro": "Whatever it is, we're listening.",
        "checklist": [
            "A description of what you need help with",
            "Any relevant screenshots, links, or account/group names",
        ],
    },
}
_DEFAULT_TYPE_META = {
    "label": "Support",
    "emoji": "🎫",
    "accent": 0x95A5A6,
    "intro": "Tell us what you need help with.",
    "checklist": [
        "A description of what you need help with",
        "Any relevant screenshots, links, or account/group names",
    ],
}


def build_ticket_welcome(ticket_type: str, ticket_id: int, author_mention: str, opened_ts: int):
    """Components-V2 welcome card posted as the first message in a new ticket."""
    meta = TICKET_TYPE_META.get(ticket_type, _DEFAULT_TYPE_META)
    checklist = "\n".join(f"> {i + 1}. {item}" for i, item in enumerate(meta["checklist"]))
    return [
        ContainerComponent(
            SectionComponent(
                components=[
                    TextDisplayComponent(content=f"## {meta['emoji']} {meta['label']} — Ticket #{ticket_id}"),
                    TextDisplayComponent(content=f"-# Opened by {author_mention} • <t:{opened_ts}:f>"),
                ],
                accessory=ThumbnailComponent(media=_LOGO_MEDIA),
            ),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    f"Hey {author_mention} — thanks for reaching out! "
                    f"The <@&{SUPPORT_ROLE_ID}> team has been notified and will be with you shortly.\n\n"
                    f"{meta['intro']}"
                )
            ),
            TextDisplayComponent(content=f"**While you wait, it helps a lot if you post:**\n{checklist}"),
            SeparatorComponent(divider=True),
            TextDisplayComponent(
                content=(
                    "-# Your account details are being pulled up below so staff can assist faster. "
                    "This conversation is archived to your [ticket history](https://www.droptracker.io/tickets) on the website."
                )
            ),
            ActionRow(
                Button(label="Close Ticket", style=ButtonStyle.DANGER, custom_id="close_ticket"),
                Button(label="Docs", style=ButtonStyle.URL, url="https://www.droptracker.io/docs"),
                Button(label="My Tickets", style=ButtonStyle.URL, url="https://www.droptracker.io/tickets"),
            ),
            accent_color=meta["accent"],
        )
    ]


def build_ticket_loading():
    """Placeholder card shown while the account snapshot loads."""
    return [
        ContainerComponent(
            TextDisplayComponent(content="### ⏳ Pulling up your account details..."),
            TextDisplayComponent(content="-# Checking claimed accounts, groups, and recent activity — one moment."),
            accent_color=0x3498DB,
        )
    ]


def build_ticket_snapshot(data: dict | None):
    """Components-V2 account snapshot for staff: user/player/group names with IDs.

    ``data`` is the dict produced by :func:`_get_data_for_ticket_sync`
    (plain values only — safe to use after the DB session is closed), or
    None when the Discord user has no DropTracker user row at all.
    """
    if not data:
        return [
            ContainerComponent(
                TextDisplayComponent(content="### ❌ No DropTracker account found"),
                TextDisplayComponent(
                    content=(
                        "-# This Discord account isn't registered with the DropTracker yet.\n"
                        "-# If your issue is about tracking, install the **DropTracker plugin** from the RuneLite "
                        "plugin hub, then use `/claim-rsn` to link your in-game account(s) to Discord."
                    )
                ),
                accent_color=0xE74C3C,
            )
        ]

    user = data["user"]
    players = data["players"]

    user_lines = [
        f"**Discord:** <@{user['discord_id']}> (`{user['discord_id']}`)",
        f"**DropTracker User ID:** `{user['user_id']}`" + (f" • **Username:** {user['username']}" if user.get("username") else ""),
    ]
    if user.get("registered_ts"):
        user_lines.append(f"**Registered:** <t:{user['registered_ts']}:D>")
    user_lines.append(f"**Claimed accounts:** {len(players)}")

    blocks = [
        SectionComponent(
            components=[
                TextDisplayComponent(content="## 📋 Account Snapshot"),
                TextDisplayComponent(content="-# Loaded automatically so staff can look things up faster."),
            ],
            accessory=ThumbnailComponent(media=_LOGO_MEDIA),
        ),
        SeparatorComponent(divider=True),
        TextDisplayComponent(content="\n".join(user_lines)),
    ]

    if not players:
        blocks.append(SeparatorComponent(divider=True))
        blocks.append(
            TextDisplayComponent(
                content=(
                    "-# No in-game accounts are claimed by this Discord account yet. "
                    "Use `/claim-rsn` to link one — it makes player-related issues much faster to investigate."
                )
            )
        )
    for p in players:
        wom_part = f"[`{p['wom_id']}`](https://wiseoldman.net/players/{p['wom_id']})" if p.get("wom_id") else "`—`"
        detail_lines = [
            f"### 🧍 [{p['player_name']}](https://www.droptracker.io/players/{p['player_id']})",
            f"-# Player ID `{p['player_id']}` • WOM ID {wom_part}" + (f" • Total level `{p['total_level']}`" if p.get("total_level") else ""),
        ]
        if p.get("account_hash"):
            detail_lines.append(f"-# Account hash `{p['account_hash']}`")
        if p.get("last_drop_ts"):
            detail_lines.append(f"**Last drop:** <t:{p['last_drop_ts']}:R> (<t:{p['last_drop_ts']}:f>)")
        else:
            detail_lines.append("**Last drop:** No drops recorded yet")
        detail_lines.append(f"**Loot this month:** {format_number(p['month_total'])} gp")
        if p["groups"]:
            group_bits = ", ".join(
                f"[{name}](https://www.droptracker.io/groups/{gid}) (`{gid}`)" for gid, name in p["groups"]
            )
            detail_lines.append(f"**Groups:** {group_bits}")
        else:
            detail_lines.append("**Groups:** Not in any groups")
        blocks.append(SeparatorComponent(divider=True))
        blocks.append(TextDisplayComponent(content="\n".join(detail_lines)))

    return [ContainerComponent(*blocks, accent_color=0x2ECC71)]


def build_ticket_snapshot_error():
    return [
        ContainerComponent(
            TextDisplayComponent(content="### ⚠️ Couldn't load account details"),
            TextDisplayComponent(
                content=(
                    "-# There was an issue loading your player information, but your ticket is ready.\n"
                    "-# Please describe your issue and our team will help you shortly."
                )
            ),
            accent_color=0xF39C12,
        )
    ]

class Tickets(Extension):
    def __init__(self, bot):
        # channel_id (str) -> ticket_id for open tickets; kept warm by the
        # 15s task so the MessageCreate hot path never queries the DB for
        # non-ticket channels.
        self.ticket_channel_cache: dict[str, int] = {}
        self._started = False
        # webhook_bot loads this extension INSIDE its own Startup handler, so
        # a @listen(Startup) here would register after the event already
        # fired and never run. Instead defer via a task that waits for ready
        # (returns immediately when the bot is already up).
        asyncio.create_task(self._deferred_start())

    async def _deferred_start(self):
        try:
            await self.bot.wait_until_ready()
        except Exception as e:
            print(f"[tickets] wait_until_ready failed: {e}")
        if self._started:
            return
        self._started = True
        self._refresh_ticket_cache()
        self.process_ticket_maintenance.start()
        print(f"[tickets] extension started; tracking {len(self.ticket_channel_cache)} open ticket channels")
        # Sync history for all open tickets (idempotent) — heals any gap from
        # downtime and seeds transcripts for tickets that predate archiving.
        asyncio.create_task(backfill_open_tickets(self.bot))

    @listen(Startup)
    async def _tickets_startup(self, event: Startup):
        # Only fires if the extension is ever loaded before Startup; the
        # deferred-start guard makes double-fire harmless.
        await self._deferred_start()

    def _refresh_ticket_cache(self):
        local_session = Session()
        try:
            rows = (
                local_session.query(Ticket.channel_id, Ticket.ticket_id)
                .filter(Ticket.status.in_(["open", "close_requested"]))
                .all()
            )
            self.ticket_channel_cache = {str(c): t for c, t in rows}
        except Exception as e:
            print(f"Error refreshing ticket cache: {e}")
        finally:
            local_session.close()

    @Task.create(IntervalTrigger(seconds=15))
    async def process_ticket_maintenance(self):
        """Refresh the channel cache and act on web-requested closes.

        The admin dashboard (web_api PATCH /admin/tickets/{id}) flips a
        ticket to status='close_requested'; this loop archives the channel,
        marks it closed, and deletes the channel.
        """
        self._refresh_ticket_cache()
        local_session = Session()
        try:
            pending = (
                local_session.query(Ticket)
                .filter(Ticket.status == "close_requested")
                .all()
            )
            for t in pending:
                local_session.expunge(t)
        except Exception as e:
            print(f"Error reading close_requested tickets: {e}")
            pending = []
        finally:
            local_session.close()
        for ticket in pending:
            closer_name = "site staff"
            if ticket.closed_by is not None:
                s = Session()
                try:
                    closer = s.query(User).filter(User.user_id == ticket.closed_by).first()
                    if closer is not None and closer.username:
                        closer_name = closer.username
                finally:
                    s.close()
            await close_and_archive(
                self.bot,
                ticket.ticket_id,
                closed_by_user_id=ticket.closed_by,
                closed_by_name=closer_name,
                reason="closed from the web dashboard",
            )

    @listen(MessageCreate)
    async def _mirror_ticket_message(self, event: MessageCreate):
        await self._mirror(event.message)

    @listen(MessageUpdate)
    async def _mirror_ticket_edit(self, event: MessageUpdate):
        if event.after is not None:
            await self._mirror(event.after)

    async def _mirror(self, message):
        if message is None or getattr(message, "channel", None) is None:
            return
        ticket_id = self.ticket_channel_cache.get(str(message.channel.id))
        if ticket_id is None:
            return
        local_session = Session()
        try:
            ticket = local_session.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if ticket is None:
                return
            local_session.expunge(ticket)
        finally:
            local_session.close()
        try:
            await upsert_message(ticket, message)
        except Exception as e:
            print(f"Error mirroring ticket message: {e}")
    @slash_command(name="close",
                   description="Close a ticket")
    async def close_ticket(self, ctx: SlashContext):
        author = ctx.author
        author_roles = author.roles
        can_close = False
        if 1342871954885050379 in [role.id for role in author_roles]:
            can_close = True
        if 1176291872143052831 in [role.id for role in author_roles]:
            can_close = True
        if not can_close:
            embed = Embed(description=":warning: You do not have permission to use this command.")
            await ctx.send(embeds=[embed])
            return
        
        # Use local session to avoid conflicts
        local_session = Session()
        try:
            ticket = local_session.query(Ticket).filter_by(channel_id=ctx.channel.id).first()
            if not ticket:
                embed = Embed(description=":warning: This is not a ticket channel owned by the DropTracker ticket system.")
                await ctx.send(embeds=[embed])
                return
            ticket_id = ticket.ticket_id
            closer = local_session.query(User).filter_by(discord_id=str(author.id)).first()
            closer_user_id = closer.user_id if closer else None
        except Exception as e:
            local_session.rollback()
            print(f"Error closing ticket: {e}")
            raise
        finally:
            local_session.close()
        await ctx.send(f"Ticket #{ticket_id} is being archived and closed...")
        ok = await close_and_archive(
            self.bot,
            ticket_id,
            closed_by_user_id=closer_user_id,
            closed_by_name=author.display_name or author.username,
        )
        if not ok:
            await ctx.channel.send(
                ":warning: Archiving the conversation failed, so the ticket was **not** closed. Please try again."
            )

    @listen(Component)
    async def on_component(self, event: Component):
        print("On component called")
        try:
            custom_id = event.ctx.custom_id
            
            if "create_ticket_" in custom_id:
                ticket_type = custom_id.split("_")[2]
                await self.create_ticket(event.ctx, ticket_type)
                return  # Exit early to prevent further processing

            if "close_ticket" in custom_id:
                author = event.ctx.author
                author_roles = author.roles
                can_close = False
                if 1342871954885050379 in [role.id for role in author_roles]:
                    can_close = True
                if 1176291872143052831 in [role.id for role in author_roles]:
                    can_close = True
                
                # Use local session for this operation
                local_session = Session()
                try:
                    ticket = local_session.query(Ticket).filter_by(channel_id=event.ctx.channel.id).first()
                    if ticket:
                        user = local_session.query(User).filter_by(user_id=str(ticket.created_by)).first()
                        if user:
                            discord_id = user.discord_id
                        else:
                            discord_id = None
                        if str(discord_id) == str(author.id):
                            can_close = True
                    if not can_close:
                        embed = Embed(description=":warning: You do not have permission to use this command.")
                        await event.ctx.send(embeds=[embed])
                        return
                    if not ticket:
                        embed = Embed(description=":warning: This is not a ticket channel owned by the DropTracker ticket system.")
                        await event.ctx.send(embeds=[embed])
                        return
                    ticket_id = ticket.ticket_id
                    closer = local_session.query(User).filter_by(discord_id=str(author.id)).first()
                    closer_user_id = closer.user_id if closer else None
                except Exception as e:
                    local_session.rollback()
                    print(f"Error in close ticket component: {e}")
                    raise
                finally:
                    local_session.close()
                await event.ctx.send(f"Ticket #{ticket_id} is being archived and closed...")
                ok = await close_and_archive(
                    self.bot,
                    ticket_id,
                    closed_by_user_id=closer_user_id,
                    closed_by_name=author.display_name or author.username,
                )
                if not ok:
                    await event.ctx.channel.send(
                        ":warning: Archiving the conversation failed, so the ticket was **not** closed. Please try again."
                    )
                
        except Exception as e:
            print(f"Error in ticket component handler: {e}")
            try:
                await event.ctx.send("An error occurred processing your request. Please try again.", ephemeral=True)
            except:
                pass  # Interaction might have already been responded to

    async def create_ticket(self, ctx: ComponentContext, ticket_type: str):
        # Defer the interaction IMMEDIATELY to prevent timeout
        await ctx.defer(ephemeral=True)
        
        # Use local session for ticket creation
        local_session = Session()
        try:
            # Check if user already has an open ticket to prevent duplicates
            dt_user = local_session.query(User).filter_by(discord_id=str(ctx.author.id)).first()
            if not dt_user:
                await try_create_user(discord_id=str(ctx.author.id), username=ctx.author.username)
                dt_user = local_session.query(User).filter_by(discord_id=str(ctx.author.id)).first()
            
            # Check for existing open tickets
            existing_ticket = local_session.query(Ticket).filter_by(
                created_by=dt_user.user_id, 
                status="open"
            ).first()
            
            if existing_ticket:
                return await ctx.send(
                    f"You already have an open ticket: <#{existing_ticket.channel_id}>\n"
                    f"Please use your existing ticket or close it before creating a new one.",
                    ephemeral=True
                )
            
            bot: interactions.Client = self.bot
            ticket_category = bot.get_channel(1210785948892274698)
            if not ticket_category:
                return await ctx.send("Ticket category not found. Please contact an administrator.", ephemeral=True)
            
            # Use a more efficient way to get ticket count for naming
            try:
                author_name = ctx.author.username
                # Use timestamp instead of total count for uniqueness and speed
                ticket_number = int(time.time()) % 10000
                ticket_channel = await ticket_category.create_text_channel(
                    name=f"{author_name}-{ticket_type}-{ticket_number}"
                )
                await ticket_channel.add_permission(
                    target=ctx.author, 
                    type=OverwriteType.MEMBER, 
                    allow=[Permissions.VIEW_CHANNEL, Permissions.SEND_MESSAGES, Permissions.READ_MESSAGE_HISTORY]
                )
            except Exception as e:
                print(f"Error creating ticket channel: {e}")
                return await ctx.send("Failed to create ticket channel. Please try again later.", ephemeral=True)
            
            # Create and save the ticket to database immediately
            ticket = Ticket(
                type=ticket_type, 
                channel_id=ticket_channel.id, 
                created_by=dt_user.user_id, 
                date_added=datetime.now(), 
                status="open"
            )
            local_session.add(ticket)
            local_session.commit()
            # Mirror messages in this channel immediately (don't wait for the
            # 15s cache refresh).
            self.ticket_channel_cache[str(ticket_channel.id)] = ticket.ticket_id
            
            # Respond to the interaction immediately to prevent timeout
            await ctx.send(
                f"✅ Your `{ticket_type}` ticket has been created: {ticket_channel.mention}\n"
                f"Please wait while we set up your ticket details...", 
                ephemeral=True
            )
            
        except Exception as e:
            local_session.rollback()
            print(f"Error creating ticket: {e}")
            await ctx.send("Failed to create ticket. Please try again later.", ephemeral=True)
            return
        finally:
            local_session.close()
        
        # Now do the heavy lifting asynchronously
        try:
            # Welcome card: type-aware guidance + close button. Mentions live
            # inside the TextDisplay components (V2 messages can't carry
            # content=), and still ping normally.
            welcome = build_ticket_welcome(
                ticket_type=ticket_type,
                ticket_id=ticket.ticket_id,
                author_mention=ctx.author.mention,
                opened_ts=int(time.time()),
            )
            await ticket_channel.send(components=welcome)

            # Account snapshot: placeholder card first, filled in by a
            # background task so channel setup never blocks on the DB.
            loading_message = await ticket_channel.send(components=build_ticket_loading())
            asyncio.create_task(load_and_update_player_data(ctx.author.id, ticket_channel, loading_message))
                
        except Exception as e:
            print(f"Error setting up ticket details: {e}")
            # Even if this fails, the ticket channel was created successfully
            await ticket_channel.send(
                "⚠️ There was an issue loading your player information, but your ticket is ready!\n"
                "Please describe your issue and our team will help you shortly."
            )

async def load_and_update_player_data(discord_id: str, ticket_channel, loading_message):
    """Background task to load account data and swap the placeholder card."""
    try:
        data = await get_data_for_ticket(discord_id)
        await loading_message.edit(components=build_ticket_snapshot(data))
    except Exception as e:
        print(f"Error loading player data in background: {e}")
        try:
            await loading_message.edit(components=build_ticket_snapshot_error())
        except Exception as edit_error:
            print(f"Failed to update loading message: {edit_error}")


def _get_data_for_ticket_sync(discord_id: str):
    """Fetch the account snapshot (plain values only) — runs in a thread pool.

    Returns None when the Discord user has no DropTracker user row;
    otherwise {"user": {...}, "players": [...]} where players may be empty.
    Everything is extracted to primitives before the session closes so the
    async renderer never touches detached ORM objects.
    """
    local_session = Session()
    try:
        user = local_session.query(User).filter_by(discord_id=str(discord_id)).first()
        if not user:
            return None

        user_data = {
            "user_id": user.user_id,
            "discord_id": user.discord_id,
            "username": user.username,
            "registered_ts": int(user.date_added.timestamp()) if user.date_added else None,
        }

        # Limit to prevent abuse
        players = local_session.query(Player).filter_by(user_id=user.user_id).limit(5).all()

        players_data = []
        for player in players:
            if not player.player_id:
                continue

            # Get player's groups efficiently using joins
            groups = local_session.query(Group.group_id, Group.group_name).join(
                user_group_association, Group.group_id == user_group_association.c.group_id
            ).filter(
                user_group_association.c.player_id == player.player_id,
                Group.group_id != 2  # Exclude global group
            ).limit(3).all()  # Limit groups shown

            # Get last drop info with limit to avoid scanning large tables
            last_drop_record = local_session.query(Drop.date_added).filter_by(
                player_id=player.player_id
            ).order_by(Drop.date_added.desc()).limit(1).first()

            # Get monthly total from Redis (fast)
            month_total = 0
            try:
                partition = datetime.now().year * 100 + datetime.now().month
                player_total_key = f"player:{player.player_id}:{partition}:total_loot"
                month_total = redis_client.get(player_total_key)
                month_total = int(month_total or 0)
            except Exception as e:
                print(f"Redis error for player {player.player_id}: {e}")

            players_data.append({
                "player_id": player.player_id,
                "player_name": player.player_name,
                "wom_id": player.wom_id,
                "account_hash": player.account_hash,
                "total_level": player.total_level,
                "last_drop_ts": int(last_drop_record.date_added.timestamp()) if last_drop_record and last_drop_record.date_added else None,
                "month_total": month_total,
                "groups": [(gid, gname) for gid, gname in groups],
            })

        return {"user": user_data, "players": players_data}

    except Exception as e:
        print(f"Error in get_data_for_ticket: {e}")
        return None
    finally:
        local_session.close()


async def get_data_for_ticket(discord_id: str):
    """Async wrapper that runs database operations in thread pool to avoid blocking"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_data_for_ticket_sync, discord_id)
