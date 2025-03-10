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
from interactions import OptionType, slash_option, Embed, ComponentContext
from interactions.api.events import GuildJoin, GuildLeft, MessageCreate, Component, Startup

from sqlalchemy import Text, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound

from db.base import session
from db.models import Player, User, Group, Drop, CombatAchievementEntry
from db.eventmodels import Event as EventModel, EventConfig, EventTask as TaskModel, EventTeam, EventParticipant, EventItems

from games.events.BoardGame import BoardGame, TaskItem, TileType, Task as EventTask, get_default_task_by_name
from games.events.utils.bg_config import BoardGameConfig

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
                    notification_config = session.query(EventConfig).filter(
                        EventConfig.event_id == game_model.id,
                        EventConfig.config_key == "notification_channel"
                    ).first()
                    
                    notification_channel_id = None
                    if notification_config:
                        notification_channel_id = int(notification_config.config_value)
                    
                    # Create new game instance
                    game = BoardGame(
                        event_id=game_model.id,
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

async def get_user_team(event_id: int, user_id: int) -> Tuple[Optional[EventTeam], Optional[str]]:
    """
    Get the team a user belongs to in an event
    
    Args:
        event_id: The event ID
        user_id: The Discord user ID
        
    Returns:
        Tuple of (EventTeam object, team_name) or (None, None) if not found
    """
    try:
        # Find the user in the database
        user = session.query(User).filter(User.discord_id == str(user_id)).first()
        if not user:
            return None, None
            
        # Find the participant entry
        participant = session.query(EventParticipant).filter(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user.user_id
        ).first()
        #print("Found event participant: ", participant)
        if not participant:
            return None, None
            
        # Get the team
        team: EventTeam = session.query(EventTeam).get(participant.team_id)
        #print("Found event team: ", team)
        if not team:
            return None, None
            
        return team, team.name
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_user_team: {e}")
        return None, None

@slash_command(name="event", description="Event management commands")
async def event_command(ctx: SlashContext):
    """Base command for event management"""
    # This is just a command group, subcommands will handle functionality
    pass

@slash_command(
    name="create_event",
    description="Create a new event"
)
@slash_option(
    name="name",
    description="Name of the event",
    required=True,
    opt_type=OptionType.STRING
)
@slash_option(
    name="type",
    description="Type of event",
    required=True,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="Board Game", value="board_game")
    ]
)
@slash_option(
    name="description",
    description="Description of the event",
    required=False,
    opt_type=OptionType.STRING
)
@slash_option(
    name="start_date",
    description="Start date of the event (YYYY-MM-DD)",
    required=False,
    opt_type=OptionType.STRING
)
async def create_event(
    ctx: SlashContext,
    name: str,
    type: str,
    description: str = None,
    start_date: str = None
):
    """Create a new event"""
    if not is_droptracker_admin(ctx):
        embed = Embed(description=":warning: You do not have permission to use this command.")
        return await ctx.send(embeds=[embed],ephemeral=True)
    try:
        # Get the guild
        guild_id = ctx.guild_id
        if not guild_id:
            await ctx.send("This command must be used in a server.", ephemeral=True)
            return
            
        # Find the group
        try:
            group = await find_group_by_guild_id(guild_id)
        except ValueError as e:
            await ctx.send(str(e), ephemeral=True)
            return
            
        # Get the author
        author_discord_id = ctx.author.id
        author = session.query(User).filter(User.discord_id == str(author_discord_id)).first()
        if not author:
            await ctx.send("You must be registered in the system to create events.", ephemeral=True)
            return
            
        # Parse start date
        if start_date:
            try:
                start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                await ctx.send("Invalid date format. Please use YYYY-MM-DD.", ephemeral=True)
                return
        else:
            start_date_obj = datetime.now()
            
        # Create the event
        event = EventModel(
            name=name,
            type=type,
            description=description,
            start_date=start_date_obj,
            status="setup",
            author_id=author.user_id,
            group_id=group.group_id,
            update_number=0
        )
        
        session.add(event)
        session.commit()
        
        # Save notification channel
        config = EventConfig(
            event_id=event.id,
            config_key="notification_channel",
            config_value=str(ctx.channel_id),
            update_number=0
        )
        
        session.add(config)
        session.commit()
        
        # Create confirmation embed
        embed = Embed(
            title="Event Created",
            description=f"Event '{name}' has been created!",
            color=0x00FF00
        )
        
        embed.add_field(name="ID", value=str(event.id))
        embed.add_field(name="Type", value=type)
        embed.add_field(name="Status", value="Setup")
        
        if description:
            embed.add_field(name="Description", value=description, inline=False)
            
        embed.add_field(name="Start Date", value=start_date_obj.strftime("%Y-%m-%d"), inline=False)
        
        # Add buttons
        start_button = Button(
            style=ButtonStyle.SUCCESS,
            label="Start Event",
            custom_id=f"event_start_{event.id}"
        )
        
        cancel_button = Button(
            style=ButtonStyle.DANGER,
            label="Cancel Event",
            custom_id=f"event_cancel_{event.id}"
        )
        
        await ctx.send(embeds=[embed], components=[start_button, cancel_button])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in create_event: {e}")
        await ctx.send("A database error occurred while creating the event.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in create_event: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

@slash_command(
    name="create_team",
    description="Create a new team for an event"
)
@slash_option(
    name="event_id",
    description="ID of the event",
    required=True,
    opt_type=OptionType.INTEGER
)
@slash_option(
    name="name",
    description="Name of the team",
    required=True,
    opt_type=OptionType.STRING
)
async def team_create_cmd(
    ctx: SlashContext,
    event_id: int,
    name: str
):
    """Create a new team for an event"""
    if not is_droptracker_admin(ctx):
        embed = Embed(description=":warning: You do not have permission to use `/create_team`.")
        return await ctx.send(embeds=[embed],ephemeral=True)
    try:
        # Check if event exists
        event = session.get(EventModel, event_id)
        if not event:
            await ctx.send(f"Event with ID {event_id} not found.", ephemeral=True)
            return
            
        # Check if event is in setup or active status
        if event.status not in ["setup", "active"]:
            await ctx.send(f"Cannot create teams for events in '{event.status}' status.", ephemeral=True)
            return
            
        # Check if team name already exists
        existing_team = session.query(EventTeam).filter(
            EventTeam.event_id == event_id,
            EventTeam.name == name
        ).first()
        
        if existing_team:
            await ctx.send(f"Team '{name}' already exists for this event.", ephemeral=True)
            return
            
        # Create the team
        team = EventTeam(
            event_id=event_id,
            name=name,
            current_location="0",
            previous_location="0",
            points=0,
            gold=5  # Starting gold
        )
        
        session.add(team)
        session.commit()
        
        # Get or create game instance
        if event_id in active_games:
            game = active_games[event_id]
        else:
            # Create new game instance
            game = BoardGame(
                event_id=event_id,
                notification_channel_id=ctx.channel_id,
                bot=bot
            )
            active_games[event_id] = game
            
        # Add team to game
        game.create_team(name, team_id=team.id)
        game.save_game_state()
        
        # Send confirmation
        embed = Embed(
            title="Team Added",
            description=f"Team '{name}' has been added to event '{event.name}'.",
            color=0x00FF00
        )
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in add_team: {e}")
        await ctx.send("A database error occurred while adding the team.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in add_team: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

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
            existing_team = session.query(EventTeam).get(existing.team_id)
            await ctx.send(
                f"You are already in team '{existing_team.name}' for this event. ",
                ephemeral=True
            )
            return
            
        # Find the team
        team = session.query(EventTeam).filter(
            EventTeam.event_id == event_id,
            EventTeam.name == team_name
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
        team_obj, team_name = await get_user_team(active_event_id, discord_id)
        
        if not team_obj or not team_name:
            await ctx.send("You are not part of any team in this event.", ephemeral=True)
            return
        
        # Get or load game
        if active_event_id in active_games:
            game = active_games[active_event_id]
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
        
        # Get the team
        team: EventTeam = game.get_team(team_name)
        if not team:
            await ctx.send(f"Team '{team_name}' not found in game state.", ephemeral=True)
            return
        print("Team's current task: ", team.current_task)
        # Check if team has a pending task
        db_team = session.query(EventTeam).filter(EventTeam.name == team_name,
                                                  EventTeam.event_id == active_event_id).first()
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

@slash_command(
    name="task",
    description="Views your team's currently assigned task, if you have one."
)
async def task_cmd(
    ctx: SlashContext
):
    board_game = get_event_by_uid(ctx.author.id)
    if not board_game:
        await ctx.send(":warning: You are not part of any event.", ephemeral=True)
        return
    team_obj, team_name = await get_user_team(board_game.event_id, ctx.author.id)
    if not team_obj or not team_name:
        await ctx.send(":warning: You are not part of any team in this event.", ephemeral=True)
        return
    """Check if your team's task is completed"""
    team_task = team_obj.current_task
    game_team = board_game.get_team(team_name)
    team_embed, components = board_game.create_team_embed("task", game_team)
    # if not team_task:
    #     await ctx.send(":warning: You don't have a task assigned to you.", ephemeral=True)
    #     return
    return await ctx.send(embeds=[team_embed], components=components,ephemeral=True)
    
    

@slash_command(
    name="status",
    description="View the current event status."
)
async def event_status_cmd(
    ctx: SlashContext
):
    """View your team's status"""
    try:
        board_game = get_event_by_uid(ctx.author.id)
        if not board_game:
            await ctx.send(":warning: You are not part of any event.", ephemeral=True)
            return
        # Check if event exists
        event = session.query(EventModel).get(board_game.event_id)
        if not event:
            await ctx.send(":warning: Event not found.", ephemeral=True)
            return
            
        # Get the user's team
        discord_id = ctx.author.id
        team_obj, team_name = await get_user_team(board_game.event_id, discord_id)
        
        if not team_obj or not team_name:
            await ctx.send("You are not part of any team in this event.", ephemeral=True)
            return
        event_id = board_game.event_id
        # Get or load game
        if event_id in active_games:
            game = active_games[event_id]
        else:
            # Create new game instance
            game = BoardGame(
                event_id=event_id,
                notification_channel_id=ctx.channel_id,
                bot=bot
            )
            success = game.load_game_state()
            if not success:
                await ctx.send("Failed to load game state.", ephemeral=True)
                return
            active_games[event_id] = game
        
        # Get the team
        team = game.get_team(team_name)
        if not team:
            await ctx.send(f"Team '{team_name}' not found in game state.", ephemeral=True)
            return
        
        # Create status embed
        embed = Embed(
            title=f"Team {team_name} Status",
            description=f"Current position: {team.position}",
            color=0x3498DB
        )
        
        # Add team info
        embed.add_field(name="Points", value=str(team.points))
        embed.add_field(name="Gold", value=str(team.gold))
        
        # Add current task if any
        if team.current_task:
            embed.add_field(name="Current Task", value=team.current_task.name, inline=False)
            
            # Format required items
            task = team.current_task
            if isinstance(task.required_items, str):
                required = task.required_items
            elif isinstance(task.required_items, list):
                required = ", ".join([item.name for item in task.required_items])
            else:
                required = task.required_items.name
                
            embed.add_field(name="Required Items", value=required)
            embed.add_field(name="Points", value=str(task.points))
            
            if task.is_assembly:
                assembled = ", ".join(team.assembled_items) if team.assembled_items else "None"
                embed.add_field(name="Assembled Items", value=assembled)
        else:
            embed.add_field(name="Current Task", value="None - Ready to roll!", inline=False)
        
        # Add inventory
        if team._get_inventory():
            inventory = ", ".join([item.name for item in team._get_inventory()])
            embed.add_field(name="Inventory", value=inventory, inline=False)
        else:
            embed.add_field(name="Inventory", value="Empty", inline=False)
        
        # Add active effects
        if team.active_effects:
            effects = "\n".join([f"{effect}: {duration} turns" for effect, duration in team.active_effects.items()])
            embed.add_field(name="Active Effects", value=effects, inline=False)
        
        # Add team members
        if team._get_players():
            members = ", ".join([player.player_name for player in team._get_players()])
            embed.add_field(name="Team Members", value=members, inline=False)
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in team_status: {e}")
        await ctx.send("A database error occurred.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in team_status: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

@event_command.subcommand(
    sub_cmd_name="leaderboard",
    sub_cmd_description="View the event leaderboard"
)
async def leaderboard(
    ctx: SlashContext,
    event_id: int
):
    """View the event leaderboard"""
    board_game = get_event_by_uid(ctx.author.id)
    if not board_game:
        await ctx.send(":warning: You are not part of any event.", ephemeral=True)
        return
    try:
        # Check if event exists
        event = session.query(EventModel).get(board_game.event_id)
        if not event:
            await ctx.send(":warning: Event not found.", ephemeral=True)
            return
            
        # Get or load game
        if board_game.event_id in active_games:
            game = active_games[board_game.event_id]
        else:
            # Create new game instance
            game = BoardGame(
                event_id=board_game.event_id,
                notification_channel_id=ctx.channel_id,
                bot=bot
            )
            success = game.load_game_state()
            if not success:
                await ctx.send("Failed to load game state.", ephemeral=True)
                return
            active_games[event_id] = game
        
        # Sort teams by points
        sorted_teams = sorted(game.teams, key=lambda t: t.points, reverse=True)
        
        # Create leaderboard embed
        embed = Embed(
            title=f"Leaderboard: {event.name}",
            description="Current team standings",
            color=0xFFD700
        )
        
        # Add teams to leaderboard
        for i, team in enumerate(sorted_teams):
            position_emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i+1}."
            embed.add_field(
                name=f"{position_emoji} {team.name}",
                value=f"Points: {team.points} | Position: {team.position} ({game.get_tile_emoji(tile_num=team.position if team.position else team.current_location)})",
                inline=False
            )
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in leaderboard: {e}")
        await ctx.send("A database error occurred.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in leaderboard: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

@slash_command(
    name="shop",
    description="View some information about the event shop"
)
async def shop(
    ctx: SlashContext
):
    """View the event shop"""
    try:
        # Check if event exists
        board_game = get_event_by_uid(ctx.author.id)
        if not board_game:
            await ctx.send(":warning: You are not part of any event.", ephemeral=True)
            return
        event = session.query(EventModel).get(board_game.event_id)
        if not event:
            await ctx.send(":warning: Event not found.", ephemeral=True)
            return
            
        # Get or load game
        if board_game.event_id in active_games:
            game = active_games[board_game.event_id]
        else:
            # Create new game instance
            game = BoardGame(
                event_id=board_game.event_id,
                notification_channel_id=ctx.channel_id,
                bot=bot
            )
            success = game.load_game_state()
            if not success:
                await ctx.send("Failed to load game state.", ephemeral=True)
                return
            active_games[board_game.event_id] = game
        
        # Create shop embed
        embed = Embed(
            title=f"Event Shop: {event.name}",
            description="Items available for purchase",
            color=0x9B59B6
        )
        
        # Add items to shop
        for item in game.shop_items:
            print("Shop item: ", item)
            embed.add_field(
                name=f"{item.emoji} {item.name} - {item.cost} gold",
                value=f"Effect: {item.effect}\nType: {item.item_type.value}\nCooldown: {item.cooldown} turns",
                inline=False
            )
        
        await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in shop: {e}")
        await ctx.send("A database error occurred.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in shop: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

## TODO -- Use Components for this functionality
async def buy_item(
    ctx: SlashContext,
    event_id: int,
    item_name: str
):
    """Buy an item from the shop"""
    try:
        # Check if event exists and is active
        event = session.query(EventModel).get(event_id)
        if not event:
            await ctx.send(f"Event with ID {event_id} not found.", ephemeral=True)
            return
            
        if event.status != "active":
            await ctx.send(f"Event is not active (current status: {event.status}).", ephemeral=True)
            return
            
        # Get the user's team
        discord_id = ctx.author.id
        team_obj, team_name = await get_user_team(event_id, discord_id)
        
        if not team_obj or not team_name:
            await ctx.send("You are not part of any team in this event.", ephemeral=True)
            return
            
        # Get or load game
        if event_id in active_games:
            game = active_games[event_id]
        else:
            # Create new game instance
            game = BoardGame(
                event_id=event_id,
                notification_channel_id=ctx.channel_id,
                bot=bot
            )
            success = game.load_game_state()
            if not success:
                await ctx.send("Failed to load game state.", ephemeral=True)
                return
            active_games[event_id] = game
        
        # Buy the item
        success, cost, item = game.buy_item(team_name, item_name)
        
        if success:
            # Create success embed
            embed = Embed(
                title="Item Purchased",
                description=f"Your team has purchased {item.name} for {cost} gold.",
                color=0x00FF00
            )
            
            embed.add_field(name="Effect", value=item.effect)
            embed.add_field(name="Remaining Gold", value=str(game.get_team(team_name).gold))
            
            await ctx.send(embeds=[embed])
        else:
            # Create failure embed
            embed = Embed(
                title="Purchase Failed",
                description=f"Failed to purchase {item_name}.",
                color=0xFF0000
            )
            
            if not item:
                embed.add_field(name="Reason", value="Item not found in shop")
            elif cost > game.get_team(team_name).gold:
                embed.add_field(name="Reason", value=f"Not enough gold. Cost: {cost}, Your gold: {game.get_team(team_name).gold}")
            else:
                embed.add_field(name="Reason", value="Unknown error")
            
            await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in buy_item: {e}")
        await ctx.send("A database error occurred.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in buy_item: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

## TODO -- Use Components for this functionality
async def use_item(
    ctx: SlashContext,
    event_id: int,
    item_name: str,
    target_team: Optional[str] = None
):
    """Use an item from your inventory"""
    try:
        # Check if event exists and is active
        event = session.query(EventModel).get(event_id)
        if not event:
            await ctx.send(f"Event with ID {event_id} not found.", ephemeral=True)
            return
            
        if event.status != "active":
            await ctx.send(f"Event is not active (current status: {event.status}).", ephemeral=True)
            return
            
        # Get the user's team
        discord_id = ctx.author.id
        team_obj, team_name = await get_user_team(event_id, discord_id)
        
        if not team_obj or not team_name:
            await ctx.send("You are not part of any team in this event.", ephemeral=True)
            return
            
        # Get or load game
        if event_id in active_games:
            game = active_games[event_id]
        else:
            # Create new game instance
            game = BoardGame(
                event_id=event_id,
                notification_channel_id=ctx.channel_id,
                bot=bot
            )
            success = game.load_game_state()
            if not success:
                await ctx.send("Failed to load game state.", ephemeral=True)
                return
            active_games[event_id] = game
        
        # Use the item
        success, effect, item = game.use_item(team_name, item_name, target_team)
        
        if success:
            # Create success embed
            embed = Embed(
                title="Item Used",
                description=f"Your team has used {item.name}.",
                color=0x00FF00
            )
            
            embed.add_field(name="Effect", value=effect)
            
            if target_team:
                embed.add_field(name="Target", value=target_team)
            
            await ctx.send(embeds=[embed])
        else:
            # Create failure embed
            embed = Embed(
                title="Item Use Failed",
                description=f"Failed to use {item_name}.",
                color=0xFF0000
            )
            
            if not item:
                embed.add_field(name="Reason", value="Item not found in your inventory")
            elif effect:
                embed.add_field(name="Reason", value=effect)
            else:
                embed.add_field(name="Reason", value="Unknown error")
            
            await ctx.send(embeds=[embed])
        
    except SQLAlchemyError as e:
        logger.error(f"Database error in use_item: {e}")
        await ctx.send("A database error occurred.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in use_item: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

@event_command.subcommand(
    sub_cmd_name="config",
    sub_cmd_description="View and modify event configuration"
)
@slash_option(
    name="config_key",
    description="Key to modify",
    required=True,
    autocomplete=True,
    opt_type=OptionType.STRING
)
@slash_option(
    name="config_value",
    description="Value to set",
    required=True,
    opt_type=OptionType.STRING
)
async def event_config(
    ctx: SlashContext,
    config_key: str,
    config_value: str
):
    """View and modify event configuration"""
    try:
        # Get the event
        event = get_event_by_uid(ctx.author.id)
        if not event:
            await ctx.send(":warning: You do not have permissions to use this command, or not part of any event.", ephemeral=True)
            return
        
        # Get the config
        config = event.config
        
        # Define mapping of user-friendly names to actual config attributes
        config_mapping = {
            "Public notifications": {
                "attr": "general_notification_channel_id",
                "type": "channel",
                "description": "Channel for public game notifications"
            },
            "Admin notifications": {
                "attr": "admin_notification_channel_id",
                "type": "channel",
                "description": "Channel for admin notifications"
            },
            "Shop channel": {
                "attr": "shop_channel_id",
                "type": "channel",
                "description": "Channel for the shop"
            },
            "Game board channel": {
                "attr": "game_board_channel_id",
                "type": "channel",
                "description": "Channel for the game board"
            },
            "Team category": {
                "attr": "team_category_id",
                "type": "category",
                "description": "Category for team channels"
            },
            "Team role (1)": {
                "attr": "team_role_id_1",
                "type": "role",
                "description": "Role for team 1 members"
            },
            "Team role (2)": {
                "attr": "team_role_id_2",
                "type": "role",
                "description": "Role for team 2 members"
            },
            "Team role (3)": {
                "attr": "team_role_id_3",
                "type": "role",
                "description": "Role for team 3 members"
            },
            "Team role (4)": {
                "attr": "team_role_id_4",
                "type": "role",
                "description": "Role for team 4 members"
            },
            "Die sides": {
                "attr": "die_sides",
                "type": "int",
                "description": "Number of sides on the dice"
            },
            "Number of dice": {
                "attr": "number_of_dice",
                "type": "int",
                "description": "Number of dice to roll"
            },
            "Items enabled": {
                "attr": "items_enabled",
                "type": "bool",
                "description": "Whether items are enabled"
            },
            "Shop enabled": {
                "attr": "shop_enabled",
                "type": "bool",
                "description": "Whether the shop is enabled"
            },
            "Starting gold": {
                "attr": "starting_gold",
                "type": "int",
                "description": "Starting gold for teams"
            }
        }
        
        # Check if the config key is valid
        if config_key not in config_mapping:
            await ctx.send(f":warning: Invalid configuration key: {config_key}", ephemeral=True)
            return
        
        # Get the config attribute and type
        config_attr = config_mapping[config_key]["attr"]
        config_type = config_mapping[config_key]["type"]
        
        # Process the value based on type
        processed_value = None
        
        if config_type == "channel" or config_type == "category":
            # Extract channel ID from mention or ID
            if config_value.startswith("<#") and config_value.endswith(">"):
                # It's a channel mention
                channel_id = config_value[2:-1]
                processed_value = int(channel_id)
            else:
                try:
                    # Try to convert to int
                    processed_value = int(config_value)
                    # Verify the channel exists
                    try:
                        channel = await bot.fetch_channel(processed_value)
                        if not channel:
                            await ctx.send(f":warning: Channel with ID {processed_value} not found", ephemeral=True)
                            return
                    except Exception as e:
                        logger.warning(f"Error fetching channel {processed_value}: {e}")
                        # Continue anyway, as the ID might be valid but not accessible
                except ValueError:
                    await ctx.send(f":warning: Invalid channel ID or mention: {config_value}", ephemeral=True)
                    return
        
        elif config_type == "role":
            # Extract role ID from mention or ID
            if config_value.startswith("<@&") and config_value.endswith(">"):
                # It's a role mention
                role_id = config_value[3:-1]
                processed_value = int(role_id)
            else:
                try:
                    # Try to convert to int
                    processed_value = int(config_value)
                    # We don't verify the role exists because it might be in a different guild
                except ValueError:
                    # Get the guild ID for this event
                    event_model = session.query(EventModel).filter(EventModel.id == event.event_id).first()
                    if not event_model:
                        await ctx.send(f":warning: Event not found in database", ephemeral=True)
                        return
                        
                    group_id = event_model.group_id
                    guild_id_result = session.query(Group.guild_id).filter(Group.id == group_id).first()
                    if not guild_id_result:
                        await ctx.send(f":warning: Group not found in database", ephemeral=True)
                        return
                        
                    guild_id = guild_id_result[0]
                    
                    try:
                        # Try to fetch the guild and role
                        guild = await bot.fetch_guild(guild_id)
                        roles = await guild.fetch_roles()
                        
                        # Find role by name
                        role = next((r for r in roles if r.name.lower() == config_value.lower()), None)
                        
                        if role:
                            processed_value = role.id
                        else:
                            await ctx.send(f":warning: Role '{config_value}' not found in guild", ephemeral=True)
                            return
                    except Exception as e:
                        logger.error(f"Error fetching guild or roles: {e}")
                        await ctx.send(f":warning: Error fetching guild or roles: {str(e)}", ephemeral=True)
                        return
        
        elif config_type == "int":
            try:
                processed_value = int(config_value)
            except ValueError:
                await ctx.send(f":warning: Invalid integer value: {config_value}", ephemeral=True)
                return
        
        elif config_type == "bool":
            if config_value.lower() in ["true", "yes", "y", "1", "on", "enable", "enabled"]:
                processed_value = True
            elif config_value.lower() in ["false", "no", "n", "0", "off", "disable", "disabled"]:
                processed_value = False
            else:
                await ctx.send(f":warning: Invalid boolean value: {config_value}. Use 'true' or 'false'.", ephemeral=True)
                return
        
        else:
            # Default to string
            processed_value = config_value
        
        # Update the config
        setattr(config, config_attr, processed_value)
        
        # Save the config to the database
        try:
            success = event.save_game_state()
            session.commit()
            await ctx.send(f":white_check_mark: Successfully updated {config_key} to {config_value}", ephemeral=True)
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving configuration: {e}")
            await ctx.send(f":warning: Failed to save configuration: {str(e)}", ephemeral=True)
    
    except Exception as e:
        logger.error(f"Error in event_config: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f":warning: An error occurred: {str(e)}", ephemeral=True)


@event_config.autocomplete(
    option_name="config_key"
)
async def event_config_autocomplete(ctx: AutocompleteContext):
    config_options = [
        "Public notifications",
        "Admin notifications",
        "Shop channel",
        "Game board channel",
        "Team category",
        "Team role (1)",
        "Team role (2)",
        "Team role (3)",
        "Team role (4)",
        "Die sides",
        "Number of dice",
        "Items enabled",
        "Shop enabled",
        "Starting gold"
    ]
    
    choices = [
        {
            "name": option,
            "value": option,
        }
        for option in config_options
    ]
    
    await ctx.send(choices=choices)

    
@slash_command(
    name="points",
    description="View the point awards for various point-related tasks in the Gielinor Race."
)
@slash_option(
    name="type",
    description="Type of point-related task to view",
    required=True,
    autocomplete=True,
    opt_type=OptionType.STRING
)
async def points(
    ctx: SlashContext,
    type: str
):
    """View the point awards for various point-related tasks in the Gielinor Race."""
    try:
        game = get_event_by_uid(ctx.author.id)
        if not game:
            await ctx.send("You are not participating in any events.\n" + 
                           "You are viewing the default point awards for this task.", ephemeral=True)
            point_awards = get_default_point_awards(type)
            print("Point awards: ", point_awards)
        # Find active events the user is participating in
        else:
            point_awards = game.get_points_awards(type)
            print("Point awards: ", point_awards)
        if not point_awards:
            await ctx.send("No point awards found for this task.", ephemeral=True)
            return
        if game:
            for task in game.tasks:
                if task.name == type:
                    task = task
                    break
        if not task:
            task: EventTask = get_default_task_by_name(type)
            task = TaskModel(
                        event_id=game.event_id,
                        type=task["type"],
                        name=task["name"],
                        description=task["description"],
                        difficulty=task["difficulty"],
                        points=task["points"],
                        required_items=[event_item["name"] + "," for event_item in task["required_items"]],
                        is_assembly=task["is_assembly"]
            )
            session.add(task)
            session.commit()
        embed = Embed(
            title=f"{type}",
            description=f"{task.description}",
            color=0x00FF00
        )
        max_field_length = 1024
        point_string = ""
        for item_name, item_points in point_awards.items():
            point_string += f"{item_name}: {item_points} points\n"
            if len(point_string) >= max_field_length:
                embed.add_field(name="Points are awarded for the following drops:", value=point_string, inline=False)
                point_string = ""
        if point_string:
            embed.add_field(name="...", value=point_string,inline=False)
        await ctx.send(embeds=[embed])
    except Exception as e:
        logger.error(f"Error in points: {e}")
        logger.error(traceback.format_exc())
        await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

@points.autocomplete(
    option_name="type"
)
async def points_autocomplete(ctx: AutocompleteContext):
    """Autocomplete for points"""
    game = get_event_by_uid(ctx.author.id)
    if not game:
        print("No game found")
        raw_tasks = session.query(TaskModel).where(TaskModel.type == "point_collection").all()
        tasks = [task.name for task in raw_tasks]
    else:
        print("Game found")
        print("Task types: ", [task.type for task in game.tasks])
        tasks = [task.name for task in game.tasks if task.type == "point_collection"]
    print(f"Found {len(tasks)} tasks")
    choices = []
    for task in tasks:
        choices.append(
            {
                "name": f"{task}",
                "value": f"{task}",
            }
        )
    await ctx.send(
        choices=choices
    )

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
        config = EventConfig(
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
        config: BoardGameConfig = game.config
        config.general_notification_channel_id = ctx.channel_id
        
        # Initialize the game
        success = game.load_game_state()
        if not success:
            await ctx.send("Failed to initialize the game.", ephemeral=True)
            return
            
        # Store in active games
        active_games[event_id] = game
        
        # Save notification channel if not already set
        existing_config = session.query(EventConfig).filter(
            EventConfig.event_id == event_id,
            EventConfig.config_key == "notification_channel"
        ).first()
        
        if not existing_config:
            config = EventConfig(
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

def get_event_by_uid(discord_id):
    # Get user
    user = session.query(User).filter(User.discord_id == str(discord_id)).first()
    if not user:
        return None
    
    # Get user's groups
    stmt = text("SELECT group_id FROM user_group_association WHERE user_id = :user_id")
    groups = session.execute(stmt, {"user_id": user.user_id}).fetchall()
    print(f"Found {len(groups)} groups for user: {user.user_id}")
    for group in groups:
        group_id = group[0]
        group: Group = session.query(Group).filter(Group.group_id == group_id).first()
        event = session.query(EventModel).filter(EventModel.group_id == group.group_id).first()
        if event and event.status == "active":
            for game_id, game in active_games.items():
                print(f"Game found: {game}")
                if game.event_id == event.id:
                    return game
        else:
            print("No event found that is active for this user in group: ", group.group_id)
    return None

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

async def find_group_by_guild_id(guild_id: int) -> Group:
    """Find a group by Discord guild ID"""
    try:
        group = session.query(Group).filter(Group.guild_id == guild_id).first()
        if not group:
            raise ValueError(f"Group with guild_id {guild_id} not found")
        return group
    except SQLAlchemyError as e:
        logger.error(f"Database error in find_group_by_guild_id: {e}")
        raise ValueError("A database error occurred while finding the group")

async def is_droptracker_admin(ctx: SlashContext):
    if ctx.author.id in [528746710042804247, 232236164776460288]:
        return True
    return False

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