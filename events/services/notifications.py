import asyncio
import json
import os
from datetime import datetime, timedelta
import random
import interactions
from sqlalchemy import text
from events.models import EventModel, EventNotification, EventConfigModel, EventTeamModel
from db.models import ItemList, NotificationQueue, NpcList, PersonalBestEntry, User, UserConfiguration, get_current_partition, session, Player, Group, GroupConfiguration
from db.ops import DatabaseOperations, get_formatted_name
from db.xf.upgrades import check_active_upgrade
from utils.redis import redis_client
from utils.embeds import update_boss_pb_embed
from utils.messages import confirm_new_npc, confirm_new_item, name_change_message, new_player_message
from utils.format import format_number, replace_placeholders, convert_from_ms
from utils.download import download_player_image
from db.app_logger import AppLogger
from utils.semantic_check import get_ca_tier_progress, get_current_ca_tier
from interactions import ButtonStyle, ContainerComponent, Button, SectionComponent, TextDisplayComponent, ActionRow, SeparatorComponent, ThumbnailComponent, UnfurledMediaItem, MediaGalleryComponent, MediaGalleryItem


app_logger = AppLogger()
global_footer = os.getenv('DISCORD_MESSAGE_FOOTER')
db = DatabaseOperations()


sent_drops = {}
sent_pbs = {}
sent_cas = {}
sent_clogs = {}

class NotificationService:
    def __init__(self, bot: interactions.Client, db_ops: DatabaseOperations):
        self.bot = bot
        self.db_ops = db_ops
        self.notified_users = []
        self.running = False
        self._processing_lock = asyncio.Lock()
    
    @interactions.Task.create(interactions.IntervalTrigger(seconds=5))
    async def start(self):
        """Start the notification service"""
        if self.running:
            return
            
        self.running = True
        asyncio.create_task(self.process_notifications_loop())
    
    async def stop(self):
        """Stop the notification service"""
        self.running = False
    
    
    async def process_notifications_loop(self):
        """Main loop to process notifications"""
        cleanup_counter = 0
        while self.running:
            try:
                async with self._processing_lock:
                    await self.process_pending_notifications()
                    
            except Exception as e:
                app_logger.log(log_type="error", data=f"Error processing notifications: {e}", app_name="notification_service", description="process_notifications_loop")
            finally:
                await asyncio.sleep(5)
    
    async def process_pending_notifications(self):
        """Process pending notifications"""
        try:
            # Use SELECT FOR UPDATE to lock the rows
            notifications = session.query(EventNotification).filter(
                EventNotification.status == 'pending'
            ).with_for_update().order_by(EventNotification.created_at.asc()).limit(10).all()
            
            og_length = len(notifications)
            if og_length > 0:
                print(f"Processing {og_length} pending notifications...")
            
            for notification in notifications:
                try:
                    # Double check status after lock to ensure it's still pending
                    if notification.status != 'pending':
                        continue
                        
                    # Mark as processing
                    notification.status = 'processing'
                    session.commit()
                    
                    await self.process_notification(notification)
                    
                except Exception as e:
                    notification.status = 'failed'
                    notification.error_message = str(e)
                    session.commit()
                    app_logger.log(log_type="error", data=f"Error processing notification {notification.id}: {e}", app_name="notification_service", description="process_pending_notifications")
            
            if og_length > 0:
                print("Finished processing pending notification data.")
            
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error in process_pending_notifications: {e}", app_name="notification_service", description="process_pending_notifications")

    async def process_notification(self, notification: EventNotification):
        """Process a single notification based on its type"""
        try:
            app_logger.log(log_type="info", data=f"Processing notification {notification.id} of type {notification.notification_type}", app_name="notification_service", description="process_notification")
            
            if notification.data is not None:
                data = json.loads(notification.data)
            else:
                data = {}
            notification_type = notification.notification_type
            
            
            # Process the notification based on its type
            if notification_type == 'player_invite_message':
                await self.send_player_invite_message(notification)
            else:
                notification.status = 'failed'
                notification.error_message = f"Unknown notification type: {notification_type}"
                session.commit()
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error processing notification {notification.id}: {e}", app_name="notification_service", description="process_notification")
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    
    async def send_player_invite_message(self, notification: EventNotification):
        """Send notification about new NPC"""
        try:
            event_id = notification.event_id
            event = session.query(EventModel).filter(EventModel.id == event_id).first()
            group_id = event.group_id
            group = session.query(Group).filter(Group.group_id == group_id).first()
            configured_channel = session.query(EventConfigModel).filter(EventConfigModel.event_id == event_id, EventConfigModel.config_key == "event_notice_channel_id").first()
            if configured_channel and configured_channel.config_value != "1":
                channel = await self.bot.fetch_channel(configured_channel.config_value)
                team_method = session.query(EventConfigModel).filter(EventConfigModel.event_id == event_id, EventConfigModel.config_key == "team_selection_method").first()
                can_choose_team = team_method.config_value == 'players'
                event_teams = session.query(EventTeamModel).filter(EventTeamModel.event_id == event_id).all()
                if can_choose_team and event_teams:
                    action_row_component = ActionRow(
                        Button(
                            style=random.choice([ButtonStyle.PRIMARY, ButtonStyle.SECONDARY, ButtonStyle.SUCCESS, ButtonStyle.DANGER]),
                            label=f"{team.name}",
                            custom_id=f"join_event_{event_id}_team_{team.id}"
                        ) for team in event_teams
                    )
                else:
                    action_row_component = ActionRow(
                        Button(
                            style=ButtonStyle.SUCCESS,
                            label="Sign up",
                            custom_id=f"join_event_{event_id}"
                        )
                    )
                if event.start_date and event.start_date is not None:
                    event_start_timestamp = f"<t:{int(event.start_date.timestamp())}:R>"
                else:
                    event_start_timestamp = "N/A"
                if event.end_date and event.end_date is not None:
                    event_end_timestamp = f"<t:{int(event.end_date.timestamp())}:R>"
                else:
                    event_end_timestamp = "N/A"
                components = [
                    ContainerComponent(
                        SeparatorComponent(divider=True),
                        SectionComponent(
                            components=[
                                TextDisplayComponent(
                                    content=("### A new event has been created for this group:\n" +
                                    f"-# Event Type: {event.event_type}\n" +
                                    f"-# Starts: {event_start_timestamp}\n" +
                                    f"-# Ends: {event_end_timestamp}\n" +
                                    f"-# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=\n\n" +
                                    f"# of teams: `{len(event.teams)}`\n" +
                                    f"Team size: `{event.team_size}`\n" +
                                    f"Max participants: `{event.max_participants}`\n" +
                                    f"Slots available: `{event.max_participants - len(event.participants)}`\n" +
                                    f"**Click the button below to join the event!**\n")),
                            ],
                            accessory=ThumbnailComponent(
                                UnfurledMediaItem(
                                    url=event.banner_image
                                )
                            )
                        ),
                        SeparatorComponent(divider=True),
                        action_row_component
                        ,TextDisplayComponent(
                            content=("-# Powered by the DropTracker | https://www.droptracker.io")
                        ),
                        SeparatorComponent(divider=True),
                    )
                ]
                await channel.send(
                    components=components
                )
            else:
                await self.bot.send_message(group.group_id, f"Admin sent player invite button to join {event.id}")
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
