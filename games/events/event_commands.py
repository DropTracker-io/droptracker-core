from datetime import datetime
import traceback
from interactions import AutocompleteContext, Extension, slash_command, SlashContext, slash_option, OptionType, Embed, Button, ButtonStyle, SlashCommandChoice
from sqlalchemy.exc import SQLAlchemyError
from db.eventmodels import EventModel as EventModel, EventTeamModel, session, EventTask as TaskModel
from games.events.BoardGame import BoardGame
from games.events.utils.classes.base import EventType, Task as EventTask
from games.events.EventFactory import get_event_by_id, get_event_by_uid
from db.models import User, Group
import logging

from games.events.event import Event
from games.events.utils.shared import find_group_by_guild_id

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("events.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("events")

class EventCommands(Extension):
    @slash_command(name="create_team",
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
    async def team_create_cmd(self, 
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
            existing_team = session.query(EventTeamModel).filter(
                EventTeamModel.event_id == event_id,
                EventTeamModel.name == name
            ).first()
            
            if existing_team:
                await ctx.send(f"Team '{name}' already exists for this event.", ephemeral=True)
                return
                
            # Create the team
            team = EventTeamModel(
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
            game = get_event_by_id(event_id)
                
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



    @slash_command(name="event", description="Event management commands")
    async def event_command(self, ctx: SlashContext):
        """Base command for event management"""
        # This is just a command group, subcommands will handle functionality
        pass

    @slash_command(name="create_event",
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
            SlashCommandChoice(name="Board Game", value="BoardGame")
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
    async def create_event(self, 
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
            if type not in [entry.value for entry in EventType]:
                await ctx.send(":warning: Invalid event type. Please use 'BoardGame'.", ephemeral=True)
                return
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
            
            event = Event(group_id=group.group_id,
                        notification_channel_id=ctx.channel_id,
                        bot=ctx.bot)
            
            
            
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
    async def event_config(self, 
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
                            channel = await ctx.bot.fetch_channel(processed_value)
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
                            guild = await ctx.bot.fetch_guild(guild_id)
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
    async def event_config_autocomplete(self, ctx: AutocompleteContext):
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


        
    @slash_command(name="points",
        description="View the point awards for various point-related tasks in the Gielinor Race."
    )
    @slash_option(
        name="type",
        description="Type of point-related task to view",
        required=True,
        autocomplete=True,
        opt_type=OptionType.STRING
    )
    async def points(self, 
        ctx: SlashContext,
        type: str
    ):
        """View the point awards for various point-related tasks in the Gielinor Race."""
        try:
            game = get_event_by_uid(ctx.author.id)
            if not game:
                await ctx.send("You are not participating in any events.\n" + 
                            "You are viewing the default point awards for this task.", ephemeral=True)
                point_awards = game.get_default_point_awards(type)
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
                task: EventTask = game.get_default_task_by_name(type)
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
    async def points_autocomplete(self, ctx: AutocompleteContext):
        """Autocomplete for points"""
        game = get_event_by_uid(ctx.author.id)
        if not game:
            raw_tasks = session.query(TaskModel).where(TaskModel.type == "point_collection").all()
            tasks = [task.name for task in raw_tasks]
        else:
            if len(game.tasks) < 1:
                game._load_tasks()
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

async def is_droptracker_admin(self, ctx: SlashContext):
    if ctx.author.id in [528746710042804247, 232236164776460288]:
        return True
    return False