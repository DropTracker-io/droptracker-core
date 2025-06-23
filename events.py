"""
    The DropTracker event system integrates the tracking functionality of the DropTracker
    with the Discord bot and XenForo website to provide a fully integrated event experience

"""

import asyncio
import os
from dotenv import load_dotenv
import interactions
from interactions import ActionRow, Button, ButtonStyle, ContainerComponent, MediaGalleryComponent, MediaGalleryItem, Message, PartialEmoji, SectionComponent, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, UnfurledMediaItem, listen, Task, IntervalTrigger
from interactions.api.events import MessageCreate, ComponentCompletion, ButtonPressed, Startup
from db.models import GroupPersonalBestMessage, ItemList, NpcList, Player, Session, Drop, CombatAchievementEntry, PersonalBestEntry, CollectionLogEntry, XenforoSession, session
from db.ops import get_formatted_name
from events.models import *
from events.models.tasks import TaskType, lifecycle
from utils.format import format_number, get_current_partition
from utils.redis import redis_client
from events.cogs import *
from events.services.notifications import NotificationService
from db.ops import DatabaseOperations
from utils.wiseoldman import get_player_metric, get_player_total_kills  

load_dotenv()


bot = interactions.Client(token=os.getenv("EVENT_BOT_TOKEN"))
bot.load_extension("events.cogs.event_commands")

@interactions.listen(MessageCreate)
async def on_message_create(event: MessageCreate):
    pass

notification_service = None

@listen(Startup)
async def on_startup(event: Startup):
    global notification_service
    db = DatabaseOperations()
    notification_service = NotificationService(bot, db)
    
    print(f"Bot started.")
    await create_tasks()


async def create_tasks():
    notification_sync.start()
    assigned_task_check.start()
    print("Starting notification sync...")

@Task.create(IntervalTrigger(seconds=5))
async def assigned_task_check():
    try:
        ## Determine if any new AssignedTask objects need to be properly assigned a TrackedTaskData object to determine completion
        assigned_tasks = session.query(AssignedTask).filter(AssignedTask.status == "created").all()
        if not assigned_tasks:
            return

        print(f"Found {len(assigned_tasks)} tasks to process")
        for assigned_task in assigned_tasks:
            try:
                print(f"Processing assigned task: {assigned_task.id}")
                team: EventTeamModel = session.query(EventTeamModel).filter(EventTeamModel.id == assigned_task.team_id).first()
                if not team:
                    print(f"No team found for task {assigned_task.id}")
                    continue

                print(f"Team found: {team.id}")
                event = session.query(EventModel).filter(EventModel.id == team.event_id).first()
                if not event:
                    print(f"No event found for team {team.id}")
                    continue

                print(f"Event found: {event.id}")
                if event.status != "draft":  # Skip non-draft events
                    print(f"Event {event.id} is not in draft status, skipping")
                    continue

                print(f"Processing event {event.id} in draft status")
                if not team.members:
                    print(f"No members found in team {team.id}")
                    continue

                print(f"Found {len(team.members)} members in team {team.id}")
                for player in team.members:
                    try:
                        print(f"Processing player: {player.id}")
                        participant: EventParticipant = player
                        player: Player = participant.player
                        event_task = session.query(EventTask).filter(EventTask.event_id == event.id, EventTask.id == assigned_task.task_id).first()
                        if not event_task:
                            print(f"No event task found for task {assigned_task.id}")
                            continue

                        task_type = event_task.task_type
                        print(f"Processing task type: {task_type}")
                        
                        # Process the task based on type
                        try:
                            match task_type:
                                case TaskType.KC_TARGET:
                                    event_task = session.query(EventTask).filter(EventTask.event_id == event.id, EventTask.id == assigned_task.task_id).first()
                                    if event_task:
                                        config = event_task.task_config
                                        if config:
                                            if config['source_npcs'] != "any":
                                                for npc in config['source_npcs']:
                                                    npc_data = await get_player_metric(player.player_name, f"{npc}")
                                                    if npc_data and isinstance(npc_data, dict) and 'kills' in npc_data:
                                                        participant.tracked_data.append(TrackedTaskData(
                                                            event_id=event.id,
                                                            team_id=team.id, 
                                                            assigned_task_id=assigned_task.id,
                                                            player_id=participant.id,
                                                            status=event.status,
                                                            is_active=True,
                                                            type=task_type,
                                                            key=npc,    
                                                            value=str(npc_data['kills'])
                                                        ))
                                                    else:
                                                        total_kills = await get_player_total_kills(player.player_id)
                                                        participant.tracked_data.append(TrackedTaskData(
                                                            event_id=event.id,
                                                            team_id=team.id,    
                                                            assigned_task_id=assigned_task.id,
                                                            player_id=participant.id,
                                                            status=event.status,
                                                            is_active=True,
                                                            type=task_type,
                                                            key=npc,
                                                            value=str(total_kills)
                                                        ))
                                case TaskType.XP_TARGET:
                                    event_task = session.query(EventTask).filter(EventTask.event_id == event.id, EventTask.id == assigned_task.task_id).first()
                                    if event_task:
                                        config = event_task.task_config
                                        if config:
                                            target_skill = config.get('skill_name', config.get('requires', 'any'))
                                            if target_skill != "any":
                                                skill_data = await get_player_metric(player.player_name, f"{target_skill}")
                                                if skill_data and isinstance(skill_data, dict) and 'experience' in skill_data:
                                                    session.add(TrackedTaskData(
                                                        event_id=event.id,
                                                        team_id=team.id,
                                                        assigned_task_id=assigned_task.id,
                                                        player_id=participant.id,
                                                        status=event.status,
                                                        is_active=True, 
                                                        type=task_type,
                                                        key=target_skill,
                                                        value=str(skill_data['experience'])
                                                    ))
                                                else:
                                                    player_exp = await get_player_metric(player.player_name, "exp")
                                                    if player_exp and isinstance(player_exp, dict) and 'exp' in player_exp:
                                                        session.add(TrackedTaskData(
                                                            event_id=event.id,
                                                            team_id=team.id,
                                                            assigned_task_id=assigned_task.id,
                                                            player_id=participant.id,
                                                            status=event.status,
                                                            is_active=True,
                                                            type=task_type,
                                                            key=target_skill,
                                                            value=str(player_exp['exp'])
                                                        ))
                                case TaskType.EHP_TARGET:
                                    event_task = session.query(EventTask).filter(EventTask.event_id == event.id, EventTask.id == assigned_task.task_id).first()
                                    if event_task:
                                        player_ehp = await get_player_metric(player.player_name, "ehp")
                                        session.add(TrackedTaskData(
                                            event_id=event.id,
                                            team_id=team.id,
                                            assigned_task_id=assigned_task.id,
                                            player_id=participant.id,
                                            status=event.status,
                                            is_active=True,
                                            type=task_type,
                                            key="start_ehp",
                                            value=str(player_ehp['ehp'])
                                        ))
                                case TaskType.EHB_TARGET:
                                    event_task = session.query(EventTask).filter(EventTask.event_id == event.id, EventTask.id == assigned_task.task_id).first()
                                    if event_task:
                                        player_ehb = await get_player_metric(player.player_name, "ehb")
                                        session.add(TrackedTaskData(
                                            event_id=event.id,
                                            team_id=team.id,
                                            assigned_task_id=assigned_task.id,
                                            player_id=participant.id,
                                            status=event.status,        
                                            is_active=True,
                                            type=task_type,
                                            key="start_ehb",
                                            value=str(player_ehb['ehb'])
                                        ))
                                case TaskType.LOOT_VALUE:
                                    event_task = session.query(EventTask).filter(EventTask.event_id == event.id, EventTask.id == assigned_task.task_id).first()
                                    if event_task:
                                        config = event_task.task_config
                                        if config:
                                            source_npcs = config.get('source_npcs', "any")
                                            if source_npcs != "any":
                                                for npc in config['source_npcs']:
                                                    session.add(TrackedTaskData(
                                                        event_id=event.id,
                                                        team_id=team.id, 
                                                        assigned_task_id=assigned_task.id,
                                                        player_id=participant.id,
                                                        status=event.status,
                                                        is_active=True,
                                                        type=task_type,
                                                        key=npc,    
                                                        value=0
                                                    ))
                                            else:
                                                session.add(TrackedTaskData(
                                                    event_id=event.id,
                                                    team_id=team.id,    
                                                    assigned_task_id=assigned_task.id,
                                                    player_id=participant.id,
                                                    status=event.status,
                                                    is_active=True,
                                                    type=task_type,
                                                    key="",
                                                    value=0
                                                ))
                                case TaskType.ITEM_COLLECTION:
                                    event_task = session.query(EventTask).filter(EventTask.event_id == event.id, EventTask.id == assigned_task.task_id).first()
                                    if event_task:
                                        config = event_task.task_config
                                        ## Initialized empty as no items have been collected yet on creation
                                        session.add(TrackedTaskData(
                                            event_id=event.id,
                                            team_id=team.id,
                                            assigned_task_id=assigned_task.id,
                                            player_id=participant.id,
                                            status=event.status,
                                            is_active=True,
                                            type=task_type,
                                            key="",
                                            value=0
                                        ))
                                case TaskType.KILL_TIME:
                                    ## We don't need to store anything for this task type
                                    pass
                                case _:
                                    print(f"Unknown task type: {task_type}")
                                    continue

                            # Update task status after successful processing
                            print(f"Updating task {assigned_task.id} status to waiting")
                            assigned_task.status = "waiting"
                            session.commit()
                            print(f"Successfully updated task {assigned_task.id} status to waiting")
                        except Exception as e:
                            print(f"Error processing task type {task_type} for task {assigned_task.id}: {str(e)}")
                            session.rollback()
                            continue

                    except Exception as e:
                        print(f"Error processing player {player.id} for task {assigned_task.id}: {str(e)}")
                        session.rollback()
                        continue

            except Exception as e:
                print(f"Error processing task {assigned_task.id}: {str(e)}")
                session.rollback()
                continue

    except Exception as e:
        print(f"Error in assigned_task_check: {str(e)}")
        session.rollback()

@Task.create(IntervalTrigger(seconds=5))
async def notification_sync():
    await notification_service.process_pending_notifications()      

if __name__ == "__main__":
    bot.start()

