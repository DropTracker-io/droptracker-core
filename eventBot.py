### events.py
### Hosts the core logic for running events through the DropTracker's infrastructure, using a separate Discord bot.

import os
import logging
import traceback
from typing import Dict, Optional, List, Tuple
import asyncio
import json
from datetime import datetime, timedelta

import interactions
from interactions import AutocompleteContext, Button, ButtonStyle, IntervalTrigger, Message, SlashCommandChoice
from interactions import Task, listen, Client, Intents, slash_command, SlashContext, check, is_owner
from interactions import OptionType, slash_option, Embed, ComponentContext, Extension
from interactions.api.events import GuildJoin, GuildLeft, MessageCreate, Component, Startup

from sqlalchemy import Text, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound

from db.base import session
from db.models import Player, User, Group, Drop, CombatAchievementEntry
from db.eventmodels import EventModel as EventModel, EventConfigModel as EventConfigModel, EventTask as TaskModel, EventTeamModel, EventParticipant, EventShopItem

from games.events.BoardGame import BoardGame, TaskItem, TileType
from games.events.EventFactory import get_event_by_id, get_event_by_uid
from games.events.utils.classes.base import Task as EventTask, Team as EventTeam
from games.events.utils.event_config import EventConfig
from games.events.event_commands import EventCommands

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("events.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("events")

# Load token from environment variable for security
BOT_TOKEN = os.getenv("EVENT_BOT_TOKEN")
global_footer = os.getenv("DISCORD_MESSAGE_FOOTER")
if not BOT_TOKEN:
    logger.critical("No bot token found in environment variables. Please set EVENT_BOT_TOKEN.")
    exit(1)

# Dictionary to store active game instances
active_games: Dict[int, BoardGame] = {}

# Initialize bot with required intents
bot = interactions.Client(intents=Intents.DEFAULT | Intents.GUILD_MESSAGES | Intents.MESSAGE_CONTENT)

@listen(Startup)
async def on_startup():
    """Handle bot startup events"""
    logger.info("Bot is starting up...")
    
    try:
        # Set bot presence
        activity = interactions.Activity(
            name="Events",
            type=interactions.ActivityType.PLAYING,
            assets=
                interactions.ActivityAssets(
                    large_image="board",
                    large_text="DropTracker1",
                    small_image="chest",
                    small_text="DropTracker2"
                )
            ,
            url="https://droptracker.io/discord",
            buttons=[
                interactions.Button(
                    style=interactions.ButtonStyle.LINK,
                    label="Join our Discord",
                    url="https://droptracker.io/discord"
                )
            ]
        )

        await bot.change_presence(
            status=interactions.Status.ONLINE,
            activity=activity
        )
        bot.load_extension("games.events.event_commands")
        
        # Start the game recovery task
        check_active_games.start()
        logger.info("Game recovery task started")
        await check_active_games()
        
        logger.info("Bot is ready!")
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        logger.error(traceback.format_exc())

@Task.create(IntervalTrigger(seconds=5))
async def check_active_games():
    """
    Periodically check for active games in the database and recover any that aren't in memory
    """
    try:
        # Query active events from database
        games = session.query(EventModel).filter(EventModel.status == "active").all()
        # logger.info(f"Found {len(games)} active games in database")
        
        for game_model in games:
            if game_model.id not in active_games:
                logger.info(f"Recovering game {game_model.id} ({game_model.name})...")
                
                try:
                    # Find notification channel
                    notification_config = session.query(EventConfigModel).filter(
                        EventConfigModel.event_id == game_model.id,
                        EventConfigModel.config_key == "notification_channel"
                    ).first()
                    
                    notification_channel_id = None
                    if notification_config:
                        notification_channel_id = int(notification_config.config_value)
                    
                    # Create new game instance
                    game = BoardGame(
                        group_id=game_model.group_id,
                        id=game_model.id,
                        notification_channel_id=notification_channel_id,
                        bot=bot
                    )
                    
                    # Load game state
                    success = game.load_game_state()
                    if success:
                        active_games[game_model.id] = game
                        logger.info(f"Successfully recovered game {game_model.id}")
                        
                        # Notify channel that game was recovered
                        if notification_channel_id:
                            try:
                                channel = await bot.fetch_channel(notification_channel_id)
                                await channel.send(
                                    embed=Embed(
                                        title="Game Recovered",
                                        description=f"The game '{game_model.name}' has been recovered after a bot restart.",
                                        color=0x00FF00
                                    )
                                )
                            except Exception as e:
                                logger.error(f"Failed to send recovery notification: {e}")
                    else:
                        logger.error(f"Failed to load game state for game {game_model.id}")
                except Exception as e:
                    logger.error(f"Error recovering game {game_model.id}: {e}")
                    logger.error(traceback.format_exc())
            else:
                logger.debug(f"Game {game_model.id} is already active")
    except SQLAlchemyError as e:
        logger.error(f"Database error in check_active_games: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in check_active_games: {e}")
        logger.error(traceback.format_exc())





## TODO -- send this request through a Component
async def move_player_to_team(
    ctx: SlashContext,
    event_id: int,
    team_name: str,
    player_id: int = None
):
    """Join a team in an event"""
    try:
        # Check if event exists
        event = session.query(EventModel).get(event_id)
        if not event:
            await ctx.send(f"Event with ID {event_id} not found.", ephemeral=True)
            return
        
        # Get the user
        discord_id = ctx.author.id
        user = session.query(User).filter(User.discord_id == str(discord_id)).first()
        if not user:
            await ctx.send("You must be registered in the system to join events.", ephemeral=True)
            return
            
        # Check if already in a team for this event
        existing = session.query(EventParticipant).filter(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user.user_id
        ).first()
        
        if existing:
            existing_team = session.query(EventTeamModel).get(existing.team_id)
            await ctx.send(
                f"You are already in team '{existing_team.name}' for this event. ",
                ephemeral=True
            )
            return
            
        # Find the team
        team = session.query(EventTeamModel).filter(
            EventTeamModel.event_id == event_id,
            EventTeamModel.name == team_name
        ).first()
        
        if not team:
            await ctx.send(f"Team '{team_name}' not found for this event.", ephemeral=True)
            return
            
        # Get player
        if player_id:
            player = session.query(Player).filter(
                Player.player_id == player_id,
                Player.user_id == user.user_id
            ).first()
            
            if not player:
                await ctx.send(f"Player with ID {player_id} not found or doesn't belong to you.", ephemeral=True)
                return
        else:
            # Get default player
            player = session.query(Player).filter(
                Player.user_id == user.user_id
            ).first()
            
            if not player:
                # Get any player
                player = session.query(Player).filter(
                    Player.user_id == user.user_id
                ).first()
                
            if not player:
                await ctx.send("You don't have any players. Please create a player first.", ephemeral=True)
                return
                
        # Add to team
        participant = EventParticipant(
            event_id=event_id,
            team_id=team.id,
            user_id=user.user_id,
            player_id=player.player_id
        )
        
        session.add(participant)
        session.commit()
        
        # Update game state
        if event_id in active_games:
            game = active_games[event_id]
            game.add_player_to_team(team_name, player.player_id)
            game.save_game_state()
        
        # Send confirmation
        embed = Embed(
            title="Team Joined",
            description=f"You have joined team '{team_name}' in event '{event.name}'.",
            color=0x00FF00
        )
        
        embed.add_field(name="Player", value=player.player_name)
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in move_player_to_team: {e}")
        await ctx.send("A database error occurred while moving the player to the team.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in move_player_to_team: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)




async def roll_dice(
    ctx: SlashContext
    ## TODO -- send this request through a Component
):
    """Roll dice and move your team"""
    try:
        # Check if user is registered
        user = session.query(User).filter(User.discord_id == str(ctx.author.id)).first()
        if not user:
            await ctx.send("You must be registered in the system to roll dice.", ephemeral=True)
            return
        
        # Get all events the user is participating in
        event_participants = session.query(EventParticipant).filter(EventParticipant.user_id == user.user_id).all()
        if not event_participants:
            await ctx.send("You are not participating in any events.", ephemeral=True)
            return
        
        # Find active events the user is participating in
        active_event = None
        active_event_id = None
        
        for event_participant in event_participants:
            event_id = event_participant.event_id
            event = session.query(EventModel).get(event_id)
            
            if event and event.status == "active":
                active_event = event
                active_event_id = event_id
                break  # Found an active event, no need to continue
        
        # Check if we found an active event
        if not active_event:
            await ctx.send("You are not participating in any active events.", ephemeral=True)
            return
        
        # Get the user's team
        discord_id = ctx.author.id
        
        if not team_obj or not team_name:
            await ctx.send("You are not part of any team in this event.", ephemeral=True)
            return
        
        # Get or load game
        if active_event_id in active_games:
            game = active_games[active_event_id]
            team_obj, team_name = await game.get_user_team(discord_id)
        
        else:
            # Create new game instance
            game = BoardGame(
                event_id=active_event_id,
                notification_channel_id=ctx.channel_id,
                bot=bot
            )
            success = game.load_game_state()
            if not success:
                await ctx.send("Failed to load game state.", ephemeral=True)
                return
            active_games[active_event_id] = game
            team_obj, team_name = await game.get_user_team(discord_id)
        
        # Get the team
        if not team_obj or not team_name:
            team: EventTeam = game.get_team(team_name)
        else:
            team = team_obj
        if not team:
            print("Team not found")
            return
        # Check if team has a pending task
        db_team = session.query(EventTeamModel).filter(EventTeamModel.name == team_name,
                                                  EventTeamModel.event_id == active_event_id).first()
        if db_team.current_task > 0:
            print("Team's current task ID: ", db_team.current_task)
            task: EventTask = game._load_task_by_id(db_team.current_task)
            print("Loaded task: ", task)
            team.current_task = task
            team.task_progress = db_team.task_progress
            # If the current_task has data stored; they are currently still assigned to a task and cannot roll
            if team.current_task:
                print("Team's current task: ", team.current_task)
                if team.current_task.is_assembly:
                    # TODO -- implement a function to return the required list of items for an assembly-based task
                    embed = Embed(
                        title=f"Your task is not complete yet!",
                        description=f"You must complete `{team.current_task.name}` before you can roll again.",
                        color=0x00FF00
                    )
                    embed.add_field(name="TODO -- assembly task thing", value=f"{team.current_task.required_items}",inline=False)
                    # Add mercy rule info if available
                else:
                    embed = Embed(
                        title=f"Your task is not complete yet!",
                        description=f"You must complete your `{team.current_task.name}` task before you can roll again.",
                        color=0x00FF00
                    )
                    print("Current task: ", team.current_task)
                    if len(team.current_task.required_items) == 1:
                        embed.add_field(name="Progress:", value=f"{team.task_progress} / 1 `{team.current_task.required_items[0].name}`")
                    else:
                        progress_string = ", ".join([f"1 x `{item}`\n" for item in team.current_task.required_items])
                        embed.add_field(name="You still need to collect:", value=f"{progress_string}", inline=False)
            
            embed.set_footer(text=global_footer, icon_url="https://www.droptracker.io/img/droptracker-small.gif")
            print("Current task difficulty: ", team.current_task.difficulty)
            print("Current location: ", team.position)
            embed.add_field(name="Current Location:", value=f"Tile #{team.position} ({game.get_tile_emoji(tile_type=team.current_task.difficulty)})",inline=False)
            # Add mercy rule info if available
            if hasattr(team, 'mercy_rule') and team.mercy_rule:
                embed.add_field(name="Mercy Rule", value=f"Your task will auto-complete at {team.mercy_rule}", inline=False)
            
            await ctx.send(embeds=[embed])
            return
        else:
            # Roll dice and move
            roll, tile, task = game.roll_and_move(team_name)
            
            # Update team's task
            if task:
                team.current_task = task
                
                # Set mercy rule if applicable
                if hasattr(team, 'mercy_rule') and not team.mercy_rule:
                    # Set mercy rule to 24 hours from now
                    team.mercy_rule = datetime.now() + timedelta(days=1)
            
            # Create embed
            embed = Embed(
                title=f"Team {team_name} Rolled {roll}",
                description=f"Moved to position {team.position}",
                color=0x00FF00
            )
            
            # Add tile info
            embed.add_field(name="Tile Type", value=tile.type.value.capitalize() if tile.type else "Unknown")
            
            # Add task info if available
            if task:
                embed.add_field(name="Task", value=task.name)
                
                # Format required items
                if isinstance(task.required_items, str):
                    required = task.required_items
                elif isinstance(task.required_items, list):
                    if task.required_items:  # Check if list is not empty
                        if hasattr(task.required_items[0], 'name'):
                            # If items are objects with a name attribute
                            required = ", ".join([item.name for item in task.required_items])
                        elif isinstance(task.required_items[0], dict) and 'item_id' in task.required_items[0]:
                            # If items are dictionaries with an item_id key
                            required = ", ".join([f"{item.get('quantity', 1)}x {item['item_id']}" for item in task.required_items])
                        else:
                            # Fallback to string representation
                            required = ", ".join([str(item) for item in task.required_items])
                    else:
                        required = "None"
                else:
                    required = str(task.required_items) if task.required_items else "None"
                
                # Make sure required is not empty
                if not required or required.strip() == "":
                    required = "None"
                
                embed.add_field(name="Required Items", value=required)
                embed.add_field(name="Points", value=str(task.points))
                embed.add_field(name="Assembly Required", value="Yes" if task.is_assembly else "No")
            
            # Send to notification channel if configured
            notification_channel_id = getattr(game.config, 'general_notification_channel_id', None)
            if notification_channel_id:
                notification_channel = bot.get_channel(notification_channel_id)
                if notification_channel:
                    await notification_channel.send(embeds=[embed])
                else:
                    logger.warning(f"Notification channel {notification_channel_id} not found")
            
            # Always respond to the user
            await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in roll_dice: {e}")
        await ctx.send("A database error occurred.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in roll_dice: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)


@listen(Component)
async def on_component(event: Component):
    """Handle button interactions"""
    try:
        ctx = event.ctx
        custom_id = ctx.custom_id
        
        # Handle event start button
        if custom_id.startswith("event_start_"):
            event_id = int(custom_id.split("_")[2])
            await handle_event_start(ctx, event_id)
        
        # Handle event cancel button
        elif custom_id.startswith("event_cancel_"):
            event_id = int(custom_id.split("_")[2])
            await handle_event_cancel(ctx, event_id)
            
        # Handle event initialize button
        elif custom_id.startswith("event_init_"):
            event_id = int(custom_id.split("_")[2])
            await handle_event_init(ctx, event_id)
            
    except Exception as e:
        logger.error(f"Error in on_component: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

async def handle_event_start(ctx: ComponentContext, event_id: int):
    """Handle event start button click"""
    try:
        # Check if event exists
        event = session.get(EventModel, event_id)
        if not event:
            await ctx.send(f"Event with ID {event_id} not found.", ephemeral=True)
            return
        
        # Check if event is in setup status
        if event.status != "setup":
            await ctx.send(f"Event is already in {event.status} status.", ephemeral=True)
            return
        
        # Update event status first
        event.status = "active"
        session.commit()
        
        # Create new game instance
        game = BoardGame(
            event_id=event_id,
            notification_channel_id=ctx.channel_id,
            bot=bot
        )
        
        # Initialize the game (this will now respect the active status)
        success = game.load_game_state()
        if not success:
            # Revert status if initialization failed
            event.status = "setup"
            session.commit()
            await ctx.send("Failed to initialize the game.", ephemeral=True)
            return
            
        # Store in active games
        active_games[event_id] = game
        
        # Save notification channel
        config = EventConfigModel(
            event_id=event_id,
            config_key="notification_channel",
            config_value=str(ctx.channel_id)
        )
        session.add(config)
        session.commit()
        
        # Send confirmation
        embed = Embed(
            title="Event Started",
            description=f"Event '{event.name}' has been started!",
            color=0x00FF00
        )
        
        # Add game info
        embed.add_field(
            name="Teams",
            value=str(len(game.teams)) if game.teams else "0"
        )
        
        embed.add_field(
            name="Board Size",
            value=str(len(game.tiles)) if game.tiles else "0"
        )
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in handle_event_start: {e}")
        await ctx.send("A database error occurred while starting the event.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in handle_event_start: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

async def handle_event_cancel(ctx: ComponentContext, event_id: int):
    """Handle event cancel button click"""
    try:
        # Check if event exists
        event = session.query(EventModel).get(event_id)
        if not event:
            await ctx.send(f"Event with ID {event_id} not found.", ephemeral=True)
            return
        
        # Update event status
        event.status = "cancelled"
        session.commit()
        
        # Remove from active games if present
        if event_id in active_games:
            del active_games[event_id]
        
        # Send confirmation
        embed = Embed(
            title="Event Cancelled",
            description=f"Event '{event.name}' has been cancelled.",
            color=0xFF0000
        )
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in handle_event_cancel: {e}")
        await ctx.send("A database error occurred while cancelling the event.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in handle_event_cancel: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)



async def handle_event_init(ctx: ComponentContext, event_id: int):
    """Handle event initialize button click"""
    try:
        # Check if event exists and is active
        event = session.get(EventModel, event_id)
        if not event:
            await ctx.send(f"Event with ID {event_id} not found.", ephemeral=True)
            return
            
        if event.status != "active":
            await ctx.send(f"Event must be active to initialize (current status: {event.status}).", ephemeral=True)
            return
            
        # Create new game instance
        game = BoardGame(
            event_id=event_id,
            notification_channel_id=ctx.channel_id,
            bot=bot
        )
        config: EventConfig = game.config
        config.general_notification_channel_id = ctx.channel_id
        
        # Initialize the game
        success = game.load_game_state()
        if not success:
            await ctx.send("Failed to initialize the game.", ephemeral=True)
            return
            
        # Store in active games
        active_games[event_id] = game
        
        # Save notification channel if not already set
        existing_config = session.query(EventConfigModel).filter(
            EventConfigModel.event_id == event_id,
            EventConfigModel.config_key == "notification_channel"
        ).first()
        
        if not existing_config:
            config = EventConfigModel(
                event_id=event_id,
                config_key="notification_channel",
                config_value=str(ctx.channel_id)
            )
            session.add(config)
            session.commit()
        
        # Send confirmation
        embed = Embed(
            title="Game Initialized",
            description=f"Game for event '{event.name}' has been initialized!",
            color=0x00FF00
        )
        
        # Add game info
        embed.add_field(
            name="Teams",
            value=str(len(game.teams)) if game.teams else "0"
        )
        
        embed.add_field(
            name="Board Size",
            value=str(len(game.tiles)) if game.tiles else "0"
        )
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in handle_event_init: {e}")
        await ctx.send("A database error occurred while initializing the game.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in handle_event_init: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)



def get_default_point_awards(type: str) -> dict:
    """Get the default point awards for a given task name"""
    with open("games/events/task_store/default.json", "r") as f:
        default_tasks = json.load(f)
    point_awards = {}
    for task in default_tasks["tasks"]:
        if task["name"] == type:    
            for item in task["required_items"]:
                item: TaskItem = item
                point_awards[item.name] = item.points
            return point_awards
    return None





async def main():
    """Main entry point for the bot"""
    if not BOT_TOKEN:
        logger.critical("No bot token provided. Please set the EVENT_BOT_TOKEN environment variable.")
        return
    
    try:
        logger.info("Starting bot...")
        await bot.astart(BOT_TOKEN)
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
        logger.critical(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(main())