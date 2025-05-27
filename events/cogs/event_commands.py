from datetime import datetime, timedelta
import interactions
from interactions import ButtonStyle, Extension, slash_command
from interactions import SlashContext, ContainerComponent, Button, SectionComponent, TextDisplayComponent, ActionRow, SeparatorComponent, ThumbnailComponent, UnfurledMediaItem, MediaGalleryComponent, MediaGalleryItem

from db.models import GroupConfiguration, session
from events.generators import BingoBoardGen
from utils.redis import redis_client
from events.models import *

from events.manager import EventManager
event_manager = EventManager()

class Commands(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot

    @slash_command(
        name="event",
        description="Create a new event",
        default_member_permissions=interactions.Permissions.ADMINISTRATOR
    )
    async def create_event(self, ctx: SlashContext):
        await ctx.send("Creating a new Bingo event...", ephemeral=True)
        await ctx.defer()
    
        try:
            # Create a new event with proper timestamp conversion
            start_time = datetime.now()
            end_time = start_time + timedelta(days=1)
            group_id = 2
            
            event = EventModel(
                group_id=group_id,
                author_id=int(ctx.author.id),
                event_type="bingo",
                status="pending",
                banner_image="https://cdn.discordapp.com/attachments/1369611010000000000/1369611010000000000/image.png",
                title="test",
                description="test",
                start_date=int(start_time.timestamp()),  # Convert to timestamp
                end_date=int(end_time.timestamp()),      # Convert to timestamp
                max_participants=2,
                team_size=4
            )
            
            # Add and commit event first to get ID
            session.add(event)
            session.commit()
            
            # Create bingo game configuration
            bingo_game = BingoGameModel(
                event_id=event.id,  # Now event has an ID
                individual_boards=False,
                board_size=5,
                win_condition="x_pattern",
                allow_diagonal=True,
                center_free=True,
                max_boards_per_team=1
            )
            session.add(bingo_game)
            session.commit()
            
            # Create teams
            team_count = 3
            teams = []
            for team_idx in range(team_count):
                team = EventTeamModel(
                    event_id=event.id,
                    name=f"Team {team_idx + 1}",
                    points=0,
                    gold=100,  # Default starting gold
                    current_task=None,
                    task_progress=None,
                    turn_number=1,  # Start at turn 1
                    mercy_count=0,
                )
                session.add(team)
                teams.append(team)
            
            # Commit teams to get their IDs
            session.commit()
            
            # Generate tasks and create boards
            board_tasks = {}  # For shared boards: position -> AssignedTask
            
            for team in teams:
                # Create a board for this team (even if shared, each team gets a board record)
                team_id = team.id if bingo_game.individual_boards else None
                board = BingoBoardModel(
                    event_id=event.id,
                    team_id=team_id
                )
                session.add(board)
                session.commit()  # Commit to get board ID
                
                # Create tiles for the board
                for y in range(bingo_game.board_size):
                    for x in range(bingo_game.board_size):
                        position_key = f"{x},{y}"
                        
                        if not bingo_game.individual_boards:
                            # Shared boards: reuse tasks across teams for same positions
                            if position_key not in board_tasks:
                                assigned_task = event_manager.generate_task(event, team)
                                if assigned_task is None:
                                    print(f"No task found for position {x},{y}")
                                    continue
                                board_tasks[position_key] = assigned_task
                            else:
                                assigned_task = board_tasks[position_key]
                        else:
                            # Individual boards: each team gets unique tasks
                            assigned_task = event_manager.generate_task(event, team)
                            if assigned_task is None:
                                print(f"No task found for team {team.id}, position {x},{y}")
                                continue
                        
                        # Create the tile
                        tile = BingoBoardTile(
                            board_id=board.board_id,
                            task_id=assigned_task.id,  # Reference to AssignedTask
                            position_x=x,
                            position_y=y,
                            status="pending",
                            completed_by_team_id=None,
                            date_completed=None
                        )
                        session.add(tile)
            
            # Generate visual boards after all data is committed
            await self._generate_bingo_images(event, bingo_game, teams)
            
            # Final commit for all tiles
            session.commit()
            
            # Send notifications
            await self._send_event_notifications(ctx, event, group_id)
            
            await ctx.send("Event created successfully!", ephemeral=True)
            
        except Exception as e:
            # Rollback on error
            session.rollback()
            await ctx.send(f"Error creating event: {str(e)}", ephemeral=True)
            print(f"Event creation error: {e}")
    
    async def _generate_bingo_images(self, event, bingo_game, teams):
        """Generate visual board images for the event."""
        if bingo_game.individual_boards:
            # Generate individual boards for each team
            for team in teams:
                board = session.query(BingoBoardModel).filter(
                    BingoBoardModel.event_id == event.id, 
                    BingoBoardModel.team_id == team.id
                ).first()
                
                if board:
                    try:
                        # Generate the visual board
                        board_gen = board.generate_board_image(
                            cell_size=100,
                            save_path=f"static/assets/img/clans/events/{event.type}.{event.id}/{team.id}.png"
                        )
                        
                        # Save the board image
                        board_gen.save(f"static/assets/img/clans/events/{event.type}.{event.id}/{team.id}.png")
                        print(f"Generated board for team {team.id}")
                        
                    except Exception as e:
                        print(f"Error generating board for team {team.id}: {e}")
                else:
                    print(f"No board found for team {team.id}")
        else:
            # Generate shared board (use first team's board as the shared one)
            shared_board = session.query(BingoBoardModel).filter(
                BingoBoardModel.event_id == event.id,
                BingoBoardModel.team_id.is_(None)  # Shared boards have team_id = NULL
            ).first()
            
            if shared_board:
                try:
                    # Generate the visual board
                    board_gen = shared_board.generate_board_image(
                        cell_size=120,  # Larger cells for shared boards
                        save_path=f"static/assets/img/clans/events/{event.type}.{event.id}/shared.png"
                    )
                    
                    # Save the board image
                    board_gen.save(f"static/assets/img/clans/events/{event.type}.{event.id}/shared.png")
                    print(f"Generated shared board for event {event.id}")
                    
                except Exception as e:
                    print(f"Error generating shared board: {e}")
            else:
                print("No shared board found")
    
    async def _send_event_notifications(self, ctx, event, group_id):
        """Send event notifications to the configured channel."""
        channel_to_notify = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id, 
            GroupConfiguration.config_key == "event_notice_channel_id"
        ).first()
        
        ping_to_use = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id, 
            GroupConfiguration.config_key == "event_notice_ping"
        ).first()
        
        ping_str = ping_to_use.config_value if ping_to_use else ""
        
        if channel_to_notify:
            channel_id = channel_to_notify.config_value
            channel = self.bot.get_channel(int(channel_id))
            
            if channel:
                components = [
                    ContainerComponent(
                        SeparatorComponent(divider=True),
                        SectionComponent(
                            components=[
                                TextDisplayComponent(
                                    content=("### A new event has been created!\n" + 
                                    f"-# Created by: {ctx.author.mention}\n" +
                                    f"-# Event Type: {event.event_type}\n" +
                                    f"-# Starts: <t:{event.start_date}:R>\n" +
                                    f"-# Ends: <t:{event.end_date}:R>\n" +
                                    f"-# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=\n\n" +
                                    f"Teams: {len(event.teams)}\n" +
                                    f"Team size: {event.team_size}\n" +
                                    f"Max participants: {event.max_participants}\n" +
                                    f"Players needed: {event.max_participants - len(event.teams)}\n"))
                            ],
                            accessory=ThumbnailComponent(
                                UnfurledMediaItem(
                                    url="https://www.droptracker.io/img/droptracker-small.gif"
                                )
                            )
                        ),
                        SeparatorComponent(divider=True),
                        MediaGalleryComponent(
                            MediaGalleryItem(
                                media=UnfurledMediaItem(
                                    url=event.banner_image
                                ),
                                description=event.description
                            )
                        ),
                        ActionRow(
                            Button(
                                style=ButtonStyle.SUCCESS,
                                label="Join Event",
                                custom_id=f"join_event_{event.id}"
                            ),
                            Button(
                                style=ButtonStyle.URL,
                                label="More Information",
                                url=f"https://www.droptracker.io/events/{event.id}"
                            )
                        )
                    )
                ]
                
                message = f"Event created successfully! {event.title} has started. Use `/join` to join the event."
                if ping_str:
                    message = f"{ping_str} {message}"
                
                await channel.send(message, components=components)

