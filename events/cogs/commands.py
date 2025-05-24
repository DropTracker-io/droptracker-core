from datetime import datetime, timedelta
import interactions
from interactions import Extension, slash_command

from db.eventmodels import BoardGameModel, EventModel



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
        await self.bot.db.add(event)
        await self.bot.db.commit()

        await ctx.send("Event created successfully!", ephemeral=True)
