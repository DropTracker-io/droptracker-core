import asyncio
from datetime import datetime
import interactions
from sqlalchemy import text
from interactions import Button, ButtonStyle, ComponentContext, Embed, Extension, OverwriteType, Permissions, slash_command, slash_option, OptionType, SlashContext, listen
from interactions.api.events import MessageCreate, Component

from commands import try_create_user
from db.models import Drop, Group, Player, Ticket, User, session
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
        if "create_ticket_" in event.ctx.custom_id:
            ticket_type = event.ctx.custom_id.split("_")[2]
            await self.create_ticket(event.ctx, ticket_type)

        if "close_ticket" in event.ctx.custom_id:
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

    async def create_ticket(self, ctx: ComponentContext, ticket_type: str):
        # Defer the interaction to prevent timeout
        
        
        bot: interactions.Client = self.bot
        ticket_category = bot.get_channel(1210785948892274698)
        total_tickets = session.query(Ticket).count()
        if not ticket_category:
            return await ctx.send("Ticket category not found.")
        
        author_user: User = await bot.fetch_user(ctx.author.id)
        author_name = author_user.username
        ticket_channel = await ticket_category.create_text_channel(name=f"{author_name}-{ticket_type}-{total_tickets+1}")
        await ticket_channel.add_permission(target=ctx.author, 
                                            type=OverwriteType.MEMBER, 
                                            allow=[Permissions.VIEW_CHANNEL, Permissions.SEND_MESSAGES, Permissions.READ_MESSAGE_HISTORY])
        await ctx.defer(ephemeral=True)
        await asyncio.sleep(1)
        ticket_buttons = [
            Button(label="Close Ticket", style=ButtonStyle.DANGER, custom_id="close_ticket")
        ]
        dt_user = session.query(User).filter_by(user_id=str(ctx.author.id)).first()
        if not dt_user:
            await try_create_user(discord_id=str(ctx.author.id), username=ctx.author.username)
        dt_user = session.query(User).filter_by(discord_id=str(ctx.author.id)).first()
        ticket = Ticket(type=ticket_type, channel_id=ticket_channel.id, created_by=dt_user.user_id, date_added=datetime.now(), status="open")
        session.add(ticket)
        session.commit()
        
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
        
        # Second message with player details
        player_data = await get_data_for_ticket(ctx.author.id)
        if player_data:
            player_embed = Embed(title="Player Information", description="Account details below")
            player_names = [player_info['player'].player_name for player_info in player_data]
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
        
        # Respond to the original interaction
        await ctx.send(f"Your `{ticket_type}` ticket has been created: {ticket_channel.mention}\n", ephemeral=True)

async def get_data_for_ticket(discord_id: str):
    user = session.query(User).filter_by(discord_id=str(discord_id)).first()
    if not user:
        return None
    players = session.query(Player).filter_by(user_id=user.user_id).all()
    players_data = []
    for player in players:
        if not (player.player_id and player.account_hash):
            continue
            
        # Get player's groups
        groups = []
        group_sql = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
        group_ids = session.execute(text(group_sql), {"player_id": player.player_id}).fetchall()
        for (group_id,) in group_ids:
            if group_id == 2:
                continue
            group = session.query(Group).filter_by(group_id=group_id).first()
            if group:
                groups.append(group)

        # Get last drop info
        last_drop_record = session.query(Drop).filter_by(player_id=player.player_id).order_by(Drop.date_added.desc()).first()
        
        time_since_last_drop = None
        last_drop = None
        if last_drop_record:
            last_drop = last_drop_record.date_added
            time_delta = datetime.now() - last_drop
            seconds = time_delta.total_seconds()
            if seconds < 60 * 60 * 24:
                time_since_last_drop = f"{seconds / 60 / 60:.2f} hours"
            else:
                time_since_last_drop = f"{seconds / 60 / 60 / 24:.2f} days"

        # Get monthly total
        partition = datetime.now().year * 100 + datetime.now().month
        player_total_key = f"player:{player.player_id}:{partition}:total_loot"
        month_total = redis_client.get(player_total_key)
        month_total = int(month_total or 0)

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
