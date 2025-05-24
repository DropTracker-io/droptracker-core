from datetime import datetime, timedelta
import interactions
from interactions import Extension, slash_command

from db.models import session
from utils.redis import redis_client
from events.models import *


class Commands(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot

    @slash_command(
        name="event",
        description="Create a new event",
        default_member_permissions=interactions.Permissions.ADMINISTRATOR
    )
    async def create_event(self, ctx: interactions.CommandContext):
        await ctx.send("Creating a new event...", ephemeral=True)
        await ctx.defer()
    
        # Create a new event
        event = EventModel(
            title="A Testing Event",
            description="This is a testing event",
            start_date=datetime.now(),
            end_date=datetime.now() + timedelta(days=1)
        )
        board_game = BoardGameModel(
            title="A Testing Board Game",
            description="This is a testing board game",
            max_players=2,
            min_players=1,
            max_turns=100,
            min_turns=1
        )
        # Save the event to the database
        session.add(event)
        session.add(board_game)
        session.commit()

        await ctx.send("Event created successfully!", ephemeral=True)

async def create_event_task(ctx: interactions.SlashContext, *args, **kwargs):

    ## Create a new event task

    from db.models import User
    user = session.query(User).filter(User.discord_id == str(ctx.author.id)).first()
    if not user:
        return await ctx.send("You must be registered to use this command. Please use `/register` first.", ephemeral=True)
