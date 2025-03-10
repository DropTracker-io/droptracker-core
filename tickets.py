
import asyncio
from datetime import datetime
import interactions
from interactions import Button, ButtonStyle, ComponentContext, Embed, Extension, OverwriteType, Permissions, slash_command, slash_option, OptionType, SlashContext, listen
from interactions.api.events import MessageCreate, Component

from commands import try_create_user
from db.models import Ticket, User, session

class Tickets(Extension):
    @slash_command(name="create_embed", description="Create ticket embed")
    async def create_ticket_embed(self, ctx: SlashContext):
        author = ctx.author
        author_roles = author.roles
        bot: interactions.Client = self.bot
        is_admin = False
        if 1342871954885050379 in [role.id for role in author_roles]:
            is_admin = True
        if 1176291872143052831 in [role.id for role in author_roles]:
            is_admin = True
        if not is_admin:
            embed = Embed(description=":warning: You do not have permission to use this command.")
            await ctx.send(embeds=[embed])
            return
        await ctx.defer()
        target_channel_id = ctx.channel_id
        target_channel = await bot.fetch_channel(target_channel_id)
        embed = Embed(title=f"DropTracker Support", description="Need a hand setting up or having some problems?")
        embed.add_field(name="---", value="Please use the buttons below to open a ticket for what you need help with:")
        embed.set_thumbnail(url="https://www.droptracker.io/img/droptracker-small.gif")
        embed.set_footer(text="Powered by the DropTracker | https://www.droptracker.io/")
        buttons = [
            Button(label="Clans / Setting Up", style=ButtonStyle.SUCCESS, custom_id="create_ticket_clans"),
            Button(label="Players / Tracking", style=ButtonStyle.SUCCESS, custom_id="create_ticket_players"),
            Button(label="Supporting the Project", style=ButtonStyle.SUCCESS, custom_id="create_ticket_support"),
            Button(label="Other", style=ButtonStyle.SUCCESS, custom_id="create_ticket_other")
        ]
        await target_channel.send(embed=embed, components=buttons)

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
                if str(ticket.created_by) == str(author.id):
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
        bot: interactions.Client = self.bot
        ticket_category = bot.get_channel(1210785948892274698)
        total_tickets = session.query(Ticket).count()
        if not ticket_category:
            return await ctx.send("Ticket category not found.")
        ticket_channel = await ticket_category.create_text_channel(name=f"{total_tickets+1}-{ticket_type}")
        await ticket_channel.add_permission(target=ctx.author, 
                                            type=OverwriteType.MEMBER, 
                                            allow=[Permissions.VIEW_CHANNEL, Permissions.SEND_MESSAGES, Permissions.READ_MESSAGE_HISTORY])
        await asyncio.sleep(1)
        ticket_embed = Embed(title=f"{ticket_type.capitalize()} Ticket", description=f"Thanks for reaching out. We'll get back to you ASAP!")
        ticket_embed.add_field(name="\n", value="**__Meanwhile__, if you have any relevant screenshots or information to provide that may help with your ticket, please post them below.__**")
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
        await ticket_channel.send(content=f"Hey, {ctx.author.mention}! the <@&1176291872143052831> team will be with you shortly!", embed=ticket_embed, components=ticket_buttons)
        await ctx.send(f"Your `{ticket_type}` ticket has been created: {ticket_channel.mention}\n",ephemeral=True)