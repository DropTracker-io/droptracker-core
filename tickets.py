import asyncio
import time
from datetime import datetime
import interactions
from sqlalchemy import text
from interactions import Button, ButtonStyle, ComponentContext, Embed, Extension, OverwriteType, Permissions, slash_command, slash_option, OptionType, SlashContext, listen
from interactions.api.events import MessageCreate, Component

from commands import try_create_user
from db.models import Drop, Group, Player, Ticket, User, session, user_group_association
from utils.redis import redis_client

class Tickets(Extension):
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
        ticket = session.query(Ticket).filter_by(channel_id=ctx.channel.id).first()
        if not ticket:
            embed = Embed(description=":warning: This is not a ticket channel owned by the DropTracker ticket system.")
            await ctx.send(embeds=[embed])
            return
        ticket.status = "closed"
        ticket.last_reply_uid = author.id
        session.commit()
        await ctx.send(f"Ticket #{ticket.ticket_id} closed...")
        await asyncio.sleep(5)
        await ctx.channel.delete()

    @listen(Component)
    async def on_component(self, event: Component):
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
                ticket = session.query(Ticket).filter_by(channel_id=event.ctx.channel.id).first()
                if ticket:
                    user = session.query(User).filter_by(user_id=str(ticket.created_by)).first()
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
                await event.ctx.send(f"Closing ticket #{ticket.ticket_id}...")
                await asyncio.sleep(5)
                ticket.status = "closed"
                ticket.last_reply_uid = author.id
                session.commit()
                await event.ctx.channel.delete()
                
        except Exception as e:
            print(f"Error in ticket component handler: {e}")
            try:
                await event.ctx.send("An error occurred processing your request. Please try again.", ephemeral=True)
            except:
                pass  # Interaction might have already been responded to

    async def create_ticket(self, ctx: ComponentContext, ticket_type: str):
        # Defer the interaction IMMEDIATELY to prevent timeout
        await ctx.defer(ephemeral=True)
        
        # Check if user already has an open ticket to prevent duplicates
        dt_user = session.query(User).filter_by(discord_id=str(ctx.author.id)).first()
        if not dt_user:
            await try_create_user(discord_id=str(ctx.author.id), username=ctx.author.username)
            dt_user = session.query(User).filter_by(discord_id=str(ctx.author.id)).first()
        
        # Check for existing open tickets
        existing_ticket = session.query(Ticket).filter_by(
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
        session.add(ticket)
        session.commit()
        
        # Respond to the interaction immediately to prevent timeout
        await ctx.send(
            f"✅ Your `{ticket_type}` ticket has been created: {ticket_channel.mention}\n"
            f"Please wait while we set up your ticket details...", 
            ephemeral=True
        )
        
        # Now do the heavy lifting asynchronously
        try:
            ticket_buttons = [
                Button(label="Close Ticket", style=ButtonStyle.DANGER, custom_id="close_ticket")
            ]
            
            # First message with initial embed and ping
            initial_embed = Embed(
                title=f"{ticket_type.capitalize()} Ticket", 
                description=f"Thanks for reaching out. We'll get back to you ASAP!\n\n**__Meanwhile__, if you have any relevant screenshots or information to provide that may help with your ticket, please post them below.__**"
            )
            
            await ticket_channel.send(
                content=f"Hey, {ctx.author.mention}! The <@&1176291872143052831> team will be with you shortly!", 
                embed=initial_embed, 
                components=ticket_buttons
            )
            
            # Load player data asynchronously (this is the slow part)
            player_data = await get_data_for_ticket(ctx.author.id)
            if player_data:
                player_embed = Embed(title="Player Information", description="Account details below")
                player_names = [player_info['player'].player_name for player_info in player_data]
                if player_names:
                    player_embed.add_field(name="Accounts:", value=f"{', '.join(player_names)}")
                
                for player_info in player_data:
                    player = player_info['player']
                    groups = player_info['groups']
                    time_since_last_drop = player_info['time_since_last_drop']
                    last_drop = player_info['last_drop']
                    month_total = player_info['month_total']
                    
                    player_embed.add_field(
                        name="Player Details", 
                        value=f"**{player.player_name}**\n" + 
                              f"WiseOldMan ID: {player.wom_id}\n" + 
                              f"Account Hash: {player.account_hash}\n",
                        inline=False
                    )
                    
                    if time_since_last_drop:
                        player_embed.add_field(name="Time Since Last Drop:", value=f"{time_since_last_drop}", inline=False)
                    else:
                        player_embed.add_field(name="Time Since Last Drop:", value="No drops recorded", inline=False)
                        
                    if last_drop:
                        player_embed.add_field(name="Last Drop:", value=f"{last_drop}", inline=False)
                    else:
                        player_embed.add_field(name="Last Drop:", value="No drops recorded", inline=False)
                        
                    player_embed.add_field(name="Total Loot This Month:", value=f"{month_total}", inline=False)
                    
                    if groups:
                        group_names = [group.group_name for group in groups]
                        player_embed.add_field(name="Groups:", value=f"{', '.join(group_names)}", inline=False)
                    else:
                        player_embed.add_field(name="Groups:", value="Not in any groups", inline=False)
                
                await ticket_channel.send(embed=player_embed)
            else:
                no_player_embed = Embed(description="No player information was found for this user.")
                await ticket_channel.send(embed=no_player_embed)
                
        except Exception as e:
            print(f"Error setting up ticket details: {e}")
            # Even if this fails, the ticket channel was created successfully
            await ticket_channel.send(
                "⚠️ There was an issue loading your player information, but your ticket is ready!\n"
                "Please describe your issue and our team will help you shortly."
            )

async def get_data_for_ticket(discord_id: str):
    """Optimized function to get player data for tickets"""
    try:
        user = session.query(User).filter_by(discord_id=str(discord_id)).first()
        if not user:
            return None
            
        # Get players with basic info only
        players = session.query(Player).filter_by(user_id=user.user_id).limit(5).all()  # Limit to prevent abuse
        if not players:
            return None
            
        players_data = []
        for player in players:
            if not player.player_id:
                continue
                
            # Get player's groups efficiently using joins
            groups = session.query(Group).join(
                user_group_association, Group.group_id == user_group_association.c.group_id
            ).filter(
                user_group_association.c.player_id == player.player_id,
                Group.group_id != 2  # Exclude global group
            ).limit(3).all()  # Limit groups shown

            # Get last drop info with limit to avoid scanning large tables
            last_drop_record = session.query(Drop).filter_by(
                player_id=player.player_id
            ).order_by(Drop.date_added.desc()).limit(1).first()
            
            time_since_last_drop = None
            last_drop = None
            if last_drop_record:
                last_drop = last_drop_record.date_added
                time_delta = datetime.now() - last_drop
                seconds = time_delta.total_seconds()
                if seconds < 60 * 60 * 24:
                    time_since_last_drop = f"{seconds / 60 / 60:.1f} hours"
                else:
                    time_since_last_drop = f"{seconds / 60 / 60 / 24:.1f} days"

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
                "player": player,
                "groups": groups,
                "user": user,
                "discord_id": discord_id,
                "time_since_last_drop": time_since_last_drop,
                "last_drop": last_drop,
                "month_total": month_total
            })

        return players_data
        
    except Exception as e:
        print(f"Error in get_data_for_ticket: {e}")
        return None
