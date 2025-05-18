import asyncio
import json
import os
from datetime import datetime, timedelta
import interactions
from sqlalchemy import text
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

app_logger = AppLogger()
global_footer = os.getenv('DISCORD_MESSAGE_FOOTER')
db = DatabaseOperations()

class NotificationService:
    def __init__(self, bot: interactions.Client, db_ops: DatabaseOperations):
        self.bot = bot
        self.db_ops = db_ops
        self.notified_users = []
        self.running = False
    
    @interactions.Task.create(interactions.IntervalTrigger(seconds=5))
    async def start(self):
        """Start the notification service"""
        self.running = True
        asyncio.create_task(self.process_notifications_loop())
    
    async def stop(self):
        """Stop the notification service"""
        self.running = False
    
    
    async def process_notifications_loop(self):
        """Main loop to process notifications"""
        while self.running:
            try:
                await self.process_pending_notifications()
            except Exception as e:
                app_logger.log(log_type="error", data=f"Error processing notifications: {e}", app_name="notification_service", description="process_notifications_loop")
            
    
    async def process_pending_notifications(self):
        """Process pending notifications"""
        notifications = session.query(NotificationQueue).filter(
            NotificationQueue.status == 'pending'
        ).order_by(NotificationQueue.created_at.asc()).limit(10).all()
        og_length = len(notifications)
        ## Also get pending notifications that have been waiting for more than 20 minutes
        if og_length < 2:
            lost_pending_notifications = session.query(NotificationQueue).filter(
                NotificationQueue.status == 'processing',
                NotificationQueue.created_at < datetime.now() - timedelta(minutes=20)
            ).order_by(NotificationQueue.created_at.asc()).limit(2 - og_length).all()
            notifications.extend(lost_pending_notifications)
        if og_length > 0:
            print(f"Processing {og_length} pending notifications...")
        for notification in notifications:
            try:
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

    async def process_notification(self, notification):
        """Process a single notification based on its type"""
        try:
            data = json.loads(notification.data)
            notification_type = notification.notification_type
            
            if notification_type == 'drop':
                await self.send_drop_notification(notification, data)
            elif notification_type == 'pb':
                await self.send_pb_notification(notification, data)
            elif notification_type == 'ca':
                await self.send_ca_notification(notification, data)
            elif notification_type == 'clog':
                await self.send_clog_notification(notification, data)
            elif notification_type == 'new_npc':
                await self.send_new_npc_notification(notification, data)
            elif notification_type == 'new_item':
                await self.send_new_item_notification(notification, data)
            elif notification_type == 'name_change':
                await self.send_name_change_notification(notification, data)
            elif notification_type == 'new_player':
                await self.send_new_player_notification(notification, data)
            elif notification_type == 'user_upgrade':
                await self.send_user_upgrade_notification(notification, data)
            elif notification_type == 'group_upgrade':
                await self.send_group_upgrade_notification(notification, data)
            else:
                notification.status = 'failed'
                notification.error_message = f"Unknown notification type: {notification_type}"
                session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_group_upgrade_notification(self, notification: NotificationQueue, data: dict):
        """Send a group upgrade notification to Discord"""
        notification.status = 'processing'
        session.commit()
        try:
            group_id = data.get('group_id')
            group = session.query(Group).filter(Group.group_id == group_id).first()
            user_id = data.get('dt_id')
            user = session.query(User).filter(User.user_id == user_id).first()
            status = data.get('status') 
            if user.players:
                players = [player for player in user.players]
                player_name = players[0].player_name
            else:
                player_name = None
            global_embed = None
            group_embed = None
            global_channel = None
            channel = None
            if group and user:
                bot: interactions.Client = self.bot
                guild_id = group.guild_id
                guild = await bot.fetch_guild(guild_id)
                if guild:
                    channel = guild.public_updates_channel
                global_channel = await bot.fetch_channel(1373331322709479485)
                if channel:
                    match status:
                        case 'added':
                            group_embed = interactions.Embed(
                                title=f"<:supporter:1263827303712948304> Your group has been upgraded!",
                                description=f"<@{user.discord_id}> has upgraded {group.group_name} to unlock premium features, such as customizable embeds!",
                                color="#00f0f0"
                            )
                            group_embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            group_embed.add_field(
                                name="Thank you for your support!",
                                value="Developing and maintaining a project like this takes lots of time and effort. We're extremely grateful for your continued support!"
                            )
                            group_embed.set_footer(global_footer)
                            global_embed = interactions.Embed(
                                title=f"<:supporter:1263827303712948304> `{user.username}` just upgraded {group.group_name}!",
                                description=f"{player_name if player_name else f'<@{user.discord_id}>'} just used their [account upgrade benefits](https://www.droptracker.io/account/upgrades) to unlock premium features for [{group.group_name}](https://www.droptracker.io/groups/{group.group_name}.{group.group_id}/view)",
                                color="#00f0f0"
                            )
                            global_embed.add_field(
                                name="Thank you for your support!",
                                value="Contributions like this keep us motivated to continue maintaining the project."
                            )
                            global_embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            global_embed.set_footer(global_footer)
                            global_guild = await bot.fetch_guild(1172737525069135962)
                            guild_member = await global_guild.fetch_member(user.discord_id)
                            if guild_member:
                                premium_role = global_guild.get_role(role_id=1210765189625151592)
                                await guild_member.add_role(role=premium_role)
                        case 'expired':
                            group_embed = interactions.Embed(
                                title=f"<:supporter:1263827303712948304> Your group has been downgraded!",
                                description=f"Your group upgrade has now expired.",
                                color="#f00000"
                            )
                            group_embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            group_embed.add_field(
                                name="Thank you for your support!",
                                value="Developing and maintaining a project like this takes lots of time and effort. We're extremely grateful for any support you provided."
                            )
                            group_embed.set_footer(global_footer)
                            global_guild = await bot.fetch_guild(1172737525069135962)
                            guild_member = await global_guild.fetch_member(user.discord_id)
                            if guild_member:
                                premium_role = global_guild.get_role(role_id=1210765189625151592)
                                if premium_role in guild_member.roles:
                                    await guild_member.remove_role(role=premium_role)
                    if channel and group_embed:
                        try:
                            await channel.send(embed=group_embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                        except Exception as e:
                            if group.configurations:
                                for config in group.configurations:
                                    if config.config_key == 'authed_users':
                                        authed_users = config.config_value
                                        authed_users = authed_users.replace('[','').replace(']','').replace('"','').replace(' ', '').split(',')
                                        for user_id in authed_users:
                                            user_id = int(user_id)
                                            try:
                                                authed_user = await bot.fetch_user(user_id)
                                                if authed_user:
                                                    if status == 'expired':
                                                        group_embed.add_field(
                                                        name=f"Original Supporter:",
                                                        value=f"<@{user.discord_id}>",
                                                        inline=False
                                                    )
                                                    if user_id in self.notified_users:
                                                        ## Don't notify the same user twice in quick succession.
                                                        return
                                                    self.notified_users.append(user_id)
                                                    await authed_user.send(embed=group_embed)
                                                    await asyncio.sleep(0.2)
                                            except Exception as e:
                                                app_logger.log(log_type="error", data=f"Error sending group embed to authed user {user_id}: {e}", app_name="notification_service", description="send_group_upgrade_notification")
                            app_logger.log(log_type="error", data=f"Error sending group embed: {e}", app_name="notification_service", description="send_group_upgrade_notification")
                    else:
                        app_logger.log(log_type="error", data=f"Channel or group embed not found", app_name="notification_service", description="send_group_upgrade_notification")
                    if global_channel and global_embed:
                        try:
                            await global_channel.send(embed=global_embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                            session.commit()
                        except Exception as e:
                            app_logger.log(log_type="error", data=f"Error sending global embed: {e}", app_name="notification_service", description="send_group_upgrade_notification")
                    else:
                        app_logger.log(log_type="error", data=f"Global channel or global embed not found", app_name="notification_service", description="send_group_upgrade_notification")
                    
                    session.commit()
                else:
                    notification.status = 'failed'
                    notification.error_message = f"Channel not found"
                    session.commit()
            else:
                notification.status = 'failed'
                notification.error_message = f"Group not found"
                session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise

    async def send_user_upgrade_notification(self, notification: NotificationQueue, data: dict):
        """Send a user upgrade notification to Discord"""
        notification.status = 'processing'
        session.commit()
        try:
            user_id = data.get('dt_id')
            status = data.get('status')
            db_user = session.query(User).filter(User.user_id == user_id).first()
            if user_id in self.notified_users:
                ## Don't notify the same user twice in quick succession.
                return
            self.notified_users.append(user_id)
            if db_user:
                bot: interactions.Client = self.bot
                user = await bot.fetch_user(db_user.discord_id)
                if user:
                    match status:
                        case 'added':
                            embed = interactions.Embed(
                                title="<a:droptracker:1346787143778963497> Thank you for your support!",
                                description=f"Your account upgrade has been successfully processed.",
                                color="#00f0f0"
                            )
                            embed.add_field(
                                name="What's next?",
                                value="You can now [select a group](https://www.droptracker.io/account/premium)" + 
                                " to use your premium features on.\n\n" + 
                                "If you have any questions, [feel free to reach out in our Discord](https://www.droptracker.io/discord)"
                            )
                            embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            embed.set_footer(global_footer)
                            await user.send(embed=embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                            session.commit()
                            return
                        case 'expired':
                            embed = interactions.Embed(
                                title="We're sorry to see you go!",
                                description=f"Your account upgrade has expired.\n" +
                                "Please consider [re-upgrading your account](https://www.droptracker.io/account/upgrades) to continue supporting the project," + 
                                " and to retain access to your group's premium features.",
                                color="#f00000"
                            )
                            embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            
                            embed.set_footer(global_footer)
                            await user.send(embed=embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                            session.commit()
                            return
            else:
                notification.status = 'failed'
                notification.error_message = f"User not found"
                session.commit()
                return
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            app_logger.log(log_type="error", data=f"Error sending user upgrade notification: {e}", app_name="notification_service", description="send_user_upgrade_notification")
            raise
                
            
            
            

    async def send_drop_notification(self, notification: NotificationQueue, data: dict):
        """Send a drop notification to Discord"""
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            print(f"Got raw drop notification data: {data}")
            
            
            # Get channel ID for this group
            channel_id_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
            ).first()
            
            if not channel_id_config:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                session.commit()
                return
            
            channel_id = channel_id_config.config_value
            if channel_id != "":
                channel = await self.bot.fetch_channel(channel_id=channel_id)
            else:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                session.commit()
                return
            
            # Get player name
            player_name = data.get('player_name')
            item_name = data.get('item_name')
            item_id = session.query(ItemList).filter(ItemList.item_name == item_name).first()
            if item_id:
                item_id = item_id.item_id
            else:
                item_id = 1
            npc_name = data.get('npc_name', None)
            if npc_name:
                npc_id = session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
            else:
                npc_id = 0
            if npc_id:
                npc_id = npc_id.npc_id
            else:
                npc_id = 1
            value = data.get('value')
            quantity = data.get('quantity')
            total_value = data.get('total_value')
            image_url = data.get('image_url', None)
            if image_url is None or image_url == "":
                try:
                    drop = session.query(Drop).filter(Drop.drop_id == data.get('drop_id')).first()
                    if drop:
                        image_url = drop.image_url
                except Exception as e:
                    image_url = None
            print(f"Debug - image_url: {image_url}, type: {type(image_url)}")
            if not image_url or "droptracker.io" not in image_url:
                image_url = ""
            
            # Get embed template
            upgrade_active = check_active_upgrade(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('drop', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('drop', 1)
            print(f"Debug - embed_template: {embed_template}")
            
            if not embed_template:
                notification.status = 'failed'
                notification.error_message = f"No embed template for group {group_id}"
                session.commit()
                return
            
            # Download image if available
            attachment = None
            if image_url:
                try:
                    local_path = image_url.replace("https://www.droptracker.io/img", "/store/droptracker/disc/static/assets/img")
                    print(f"Debug - local_path: {local_path}")
                    attachment = interactions.File(local_path)
                except Exception as e:
                    print(f"Debug - Couldn't get attachment from path: {e}")
                    attachment = None
                    pass
            
            # Replace placeholders in embed
            player = None
            if not player_id:
                player = session.query(Player).filter(Player.player_name == player_name).first()
                if player:
                    player_id = player.player_id
            
            partition = get_current_partition()
            player_total_raw = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
            player_month_total = format_number(player_total_raw)
            group_month_total = format_number(redis_client.zsum(f"leaderboard:group:{group_id}:{partition}"))
            group_rank = redis_client.client.zrank(f"leaderboard:group:{group_id}:{partition}", player_id) + 1 if redis_client.client.zrank(f"leaderboard:group:{group_id}:{partition}", player_id) is not None else None
            global_rank = redis_client.client.zrank(f"leaderboard:{partition}", player_id) + 1 if redis_client.client.zrank(f"leaderboard:{partition}", player_id) is not None else None
            total_global_players = redis_client.client.zcard(f"leaderboard:{partition}")
            group_total = redis_client.zsum(f"leaderboard:group:{group_id}:{partition}")
            user_count = redis_client.client.zcard(f"leaderboard:group:{group_id}:{partition}")
            total_members = redis_client.client.zcard(f"leaderboard:group:{group_id}:{partition}")

            if group_rank is not None:
                group_rank = total_members - group_rank
            else:
                group_rank = None
            if global_rank is not None:
                global_rank = total_global_players - global_rank
            else:
                global_rank = None
            # get all group ranks
            all_groups = session.query(Group.group_id).filter(Group.group_id != 2).all()
            total_groups = len(all_groups) - 1
            group_totals = []
            for group in all_groups:
                group_total = redis_client.zsum(f"leaderboard:group:{group.group_id}:{partition}")
                group_totals.append({'id': group.group_id,
                                   'total': group_total})
            sorted_groups = sorted(group_totals, key=lambda x: x['total'], reverse=True)
            group_to_group_rank = str(next((i for i, g in enumerate(sorted_groups) if g['id'] == group_id), 0) + 1)
            formatted_name = get_formatted_name(player_name, group_id, session)
            values = {
                "{item_name}": item_name,
                "{month_name}": datetime.now().strftime("%B"),
                "{player_total_month}": "`" + player_month_total + "`",
                "{global_rank}": "`" + str(global_rank) + "`" + "/" + "`" + str(total_global_players) + "`",
                "{group_rank}": "`" + str(group_rank) + "`" + "/" + "`" + str(user_count) + "`",
                "{group_total}": "`" + str(group_total) + "`",
                "{user_count}": "`" + str(user_count) + "`",
                "{group_total_month}": "`" + group_month_total + "`",
                "{group_to_group_rank}": "`" + str(group_to_group_rank) + "`" + "/" + "`" + str(total_groups) + "`",
                "{item_id}": str(item_id),
                "{npc_id}": str(npc_id),
                "{npc_name}": npc_name,
                "{item_value}": "`" + format_number(int(value) * int(quantity)) + "`",
                "{quantity}": "`" + str(quantity) + "`",
                "{total_value}": "`" + str(total_value) + "`",
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{player_name}.{player_id}/view)",
                "{image_url}": image_url or ""
            }
            print("Sending to replace_placeholders")
            
            embed = replace_placeholders(embed_template, values)
            if group_id == 2:
                embed = await self.remove_group_field(embed)
            image_url = data.get('image_url', None)
            if image_url and "cdn.discordapp.com" in image_url:
                try:
                    drop = session.query(Drop).filter(Drop.drop_id == data.get('drop_id')).first()
                    if drop:
                        image_url = drop.image_url
                except Exception as e:
                    image_url = None
            if image_url:
                try:
                    local_url = image_url.replace("https://www.droptracker.io/", "/store/droptracker/disc/static/assets/")
                    attachment = interactions.File(local_url)
                except Exception as e:
                    print(f"Debug - Couldn't get attachment from path: {e}")
                    attachment = None
                    pass
            print("Got the embed...")
            # Send message
            if attachment:
                message = await channel.send(f"{formatted_name} received a drop:", embed=embed, files=attachment)
            else:
                message = await channel.send(f"{formatted_name} received a drop:", embed=embed)
            
            # Mark as sent
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            
            # Create NotifiedSubmission entry
            from db.models import NotifiedSubmission, Drop
            drop = session.query(Drop).filter(Drop.drop_id == data.get('drop_id')).first()
            
            if drop and message:
                notified_sub = NotifiedSubmission(
                    channel_id=str(message.channel.id),
                    player_id=player_id,
                    message_id=str(message.id),
                    group_id=group_id,
                    status="sent",
                    drop=drop
                )
                session.add(notified_sub)
            
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_new_npc_notification(self, notification: NotificationQueue, data: dict):
        """Send notification about new NPC"""
        try:
            npc_name = data.get('npc_name')
            player_name = data.get('player_name')
            item_name = data.get('item_name')
            value = data.get('value')
            
            await confirm_new_npc(self.bot, npc_name, player_name, item_name, value)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_new_item_notification(self, notification: NotificationQueue, data: dict):
        """Send notification about new item"""
        try:
            item_name = data.get('item_name')
            player_name = data.get('player_name')
            item_id = data.get('item_id')
            npc_name = data.get('npc_name')
            value = data.get('value')
            
            await confirm_new_item(self.bot, item_name, player_name, item_id, npc_name, value)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_name_change_notification(self, notification: NotificationQueue, data: dict):
        """Send notification about player name change"""
        try:
            player_name = data.get('player_name')
            player_id = data.get('player_id')
            old_name = data.get('old_name')
            
            await name_change_message(self.bot, player_name, player_id, old_name)
            
            # Also send DM to user if they have Discord ID
            player = session.query(Player).filter(Player.player_id == player_id).first()
            if player and player.user:
                user_discord_id = player.user.discord_id
                if user_discord_id:
                    try:
                        user = await self.bot.fetch_user(user_id=user_discord_id)
                        if user:
                            embed = interactions.Embed(
                                title=f"Name change detected:",
                                description=f"Your account, {old_name}, has changed names to {player_name}.",
                                color="#00f0f0"
                            )
                            embed.add_field(
                                name=f"Is this a mistake?",
                                value=f"Reach out in [our discord](https://www.droptracker.io/discord)"
                            )
                            embed.set_footer(global_footer)
                            await user.send(f"Hey, <@{user.discord_id}>", embed=embed)
                    except Exception as e:
                        app_logger.log(log_type="error", data=f"Couldn't DM user about name change: {e}", app_name="notification_service", description="send_name_change_notification")
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_new_player_notification(self, notification: NotificationQueue, data: dict):
        """Send notification about new player"""
        try:
            player_name = data.get('player_name')
            
            await new_player_message(self.bot, player_name)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_pb_notification(self, notification: NotificationQueue, data: dict):
        """Send a personal best notification to Discord"""
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            
            # Get channel ID for this group
            channel_id_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_pb'
            ).first()
            
            if not channel_id_config:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                session.commit()
                return
            
            channel_id = channel_id_config.config_value
            if channel_id != "":
                channel = await self.bot.fetch_channel(channel_id=channel_id)
            else:
                channel_id_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
                if channel_id_config:
                    channel_id = channel_id_config.config_value
                    channel = await self.bot.fetch_channel(channel_id=channel_id)
                else:
                    notification.status = 'failed'
                    notification.error_message = f"No channel configured for group {group_id}"
                    session.commit()
                    return
            
            # Get data
            player_name = data.get('player_name')
            boss_name = data.get('boss_name')
            time_ms = data.get('time_ms')
            old_time_ms = data.get('old_time_ms')
            kill_time_ms = data.get('kill_time_ms')
            image_url = data.get('image_url')
            team_size = data.get('team_size')
            npc_id = data.get('npc_id')
            # Format times
            time_formatted = convert_from_ms(time_ms)
            old_time_formatted = convert_from_ms(old_time_ms) if old_time_ms else None
            
            # Get embed template
            upgrade_active = check_active_upgrade(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('pb', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('pb', 1)
            if group_id == 2:
                embed_template = await self.remove_group_field(embed_template)
            
            print(f"Debug - embed_template: {embed_template}")
            partition = get_current_partition()
            player_total_raw = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
            player_ids = session.query(text("player_id")).from_statement(
                text("SELECT DISTINCT player_id FROM user_group_association WHERE group_id = :group_id")
            ).params(group_id=group_id).all()
            
            group_ranks = session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id.in_(player_ids), PersonalBestEntry.npc_id == int(npc_id),
                                                                        PersonalBestEntry.team_size == team_size).order_by(PersonalBestEntry.personal_best.asc()).all()
            all_ranks = session.query(PersonalBestEntry).filter(PersonalBestEntry.npc_id == int(npc_id),
                                                                    PersonalBestEntry.team_size == team_size).order_by(PersonalBestEntry.personal_best.asc()).all()
                #print("Group ranks:",group_ranks)
                #print("All ranks:",all_ranks)
            total_ranked_group = len(group_ranks)
            total_ranked_global = len(all_ranks)
            current_user_best_ms = time_ms
                ## player's rank in group
            group_placement = None
            global_placement = None
            #print("Assembling rankings....")
            for idx, entry in enumerate(group_ranks, start=1): 
                if entry.personal_best == current_user_best_ms:
                    group_placement = idx
                    break
            if global_placement is None:
                global_placement = "`?`"
            ## player's rank globally
            for idx, entry in enumerate(all_ranks, start=1):
                if entry.personal_best == current_user_best_ms:
                    global_placement = idx
                    break
            if group_placement is None:
                group_placement = "`?`"
                # Replace placeholders
            formatted_name = get_formatted_name(player_name, group_id, session)
            
            replacements = {
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{player_name}.{player_id}/view)",
                "{global_rank}": str(global_placement),
                "{total_ranked_global}": str(total_ranked_global),
                "{group_rank}": str(group_placement),
                "{total_ranked_group}": str(total_ranked_group),
                "{npc_name}": boss_name,
                "{npc_id}": str(npc_id),
                "{team_size}": team_size,
                "{personal_best}": time_formatted,
            }
            
            embed = replace_placeholders(embed_template, replacements)
            
            # Send message
            if image_url:
                local_path = image_url.replace("https://www.droptracker.io/", "/store/droptracker/disc/static/assets/")
                attachment = interactions.File(local_path)
                message = await channel.send(f"{formatted_name} has achieved a new personal best:", embed=embed, files=attachment)
            else:
                message = await channel.send(f"{formatted_name} has achieved a new personal best:", embed=embed)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_ca_notification(self, notification: NotificationQueue, data: dict):
        """Send a combat achievement notification to Discord"""
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            print("Got raw CA data:", data)
            
            # Get channel ID for this group
            channel_id_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_ca'
            ).first()
            
            if not channel_id_config:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                session.commit()
                return
            
            channel_id = channel_id_config.config_value
            if channel_id != "":
                channel = await self.bot.fetch_channel(channel_id=channel_id)
            else:
                channel_id_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
                if channel_id_config:
                    channel_id = channel_id_config.config_value
                    channel = await self.bot.fetch_channel(channel_id=channel_id)
                else:
                    notification.status = 'failed'
                    notification.error_message = f"No channel configured for group {group_id}"
                    session.commit()
            # Get data
            player_name = data.get('player_name')
            task_name = data.get('task_name')
            task_tier = data.get('tier')
            image_url = data.get('image_url')
            points_awarded = data.get('points_awarded')
            points_total = data.get('points_total')
            
            # Map tier to color and name
            tier_colors = {
                "1": 0x00ff00,  # Easy - Green
                "2": 0x0000ff,  # Medium - Blue
                "3": 0xff0000,  # Hard - Red
                "4": 0xffff00,  # Elite - Yellow
                "5": 0xff00ff,  # Master - Purple
                "6": 0x00ffff   # Grandmaster - Cyan
            }
            
            tier_names = {
                "1": "Easy",
                "2": "Medium",
                "3": "Hard",
                "4": "Elite",
                "5": "Master",
                "6": "Grandmaster"
            }
            
            # Get embed template
            upgrade_active = check_active_upgrade(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('ca', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('ca', 1)
            
            actual_tier = await get_current_ca_tier(points_total)
            tier_order = ['Grandmaster', 'Master', 'Elite', 'Hard', 'Medium', 'Easy']
            if actual_tier is None:
                next_tier = "Easy"
            else:
                next_tier = tier_order[tier_order.index(actual_tier) - 1]
            progress, next_tier_points = await get_ca_tier_progress(points_total)
            formatted_task_name = task_name.replace(" ", "_").replace("?", "%3F")
            wiki_url = f"https://oldschool.runescape.wiki/w/{formatted_task_name}"
            formatted_task_name = f"[{task_name}]({wiki_url})"
            if embed_template:
                value_dict = {
                    "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{player_name}.{player_id}/view)",
                    "{task_name}": formatted_task_name,
                    "{current_tier}": actual_tier,
                    "{progress}": progress,
                    "{points_awarded}": points_awarded,
                    "{total_points}": points_total,
                    "{next_tier}": next_tier,
                    "{task_tier}": task_tier,
                    "{next_tier_points}": next_tier_points,
                    "{points_left}": int(next_tier_points) - int(points_total)
                }
            
            embed = replace_placeholders(embed_template, value_dict)
            
            # Send message
            formatted_name = get_formatted_name(player_name, group_id, session)
            
            if image_url:
                local_path = image_url.replace("https://www.droptracker.io/", "/store/droptracker/disc/static/assets/")
                attachment = interactions.File(local_path)
                message = await channel.send(f"{formatted_name} has completed a combat achievement!", embed=embed, files=attachment)
            else:
                message = await channel.send(f"{formatted_name} has completed a combat achievement!", embed=embed)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    
    async def send_clog_notification(self, notification: NotificationQueue, data: dict):
        """Send a collection log notification to Discord"""
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            print(f"Found a collection log notification to send in {group_id}")
            
            # Get channel ID for this group
            channel_id_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_clog'
            ).first()
            print(f"Found a channel id config for {group_id}")
            if not channel_id_config or not channel_id_config.config_value:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                session.commit()
                return
            
            channel_id = channel_id_config.config_value
            if channel_id and channel_id != "" and len(str(channel_id)) > 10:
                channel = await self.bot.fetch_channel(channel_id=channel_id)
            else:
                channel_id_config = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
                if channel_id_config:
                    channel_id = channel_id_config.config_value
                    channel = await self.bot.fetch_channel(channel_id=channel_id)
                else:
                    print(f"Invalid channel id: {channel_id}")
                    notification.status = 'failed'
                    notification.error_message = f"Invalid channel id: {channel_id}"
                    session.commit()
                    return
            
            # Get data
            player_name = data.get('player_name')
            item_name = data.get('item_name')
            collection_name = data.get('collection_name')
            image_url = data.get('image_url')
            item_id = data.get('item_id')
            kc = data.get('kc_received')
            npc_name = data.get('npc_name')
            partition = get_current_partition()
            player_total_raw = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
            player_month_total = format_number(player_total_raw)
            
            # Get embed template
            upgrade_active = check_active_upgrade(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('clog', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('clog', 1)
            
            if group_id == 2:
                embed_template = await self.remove_group_field(embed_template)

            user_count = format_number(redis_client.client.zcard(f"leaderboard:group:{group_id}:{partition}"))
            # Replace placeholders
            replacements = {
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{player_name}.{player_id}/view)",
                "{player_loot_month}": player_month_total,
                "{kc_received}": kc,
                "{item_name}": item_name,
                "{collection_name}": collection_name,
                "{item_id}": item_id,
                "{npc_name}": npc_name,
                "{total_tracked}": user_count
            }
            
            embed = replace_placeholders(embed_template, replacements)
            
            # Send message
            formatted_name = get_formatted_name(player_name, group_id, session)
            
            if image_url:
                local_path = image_url.replace("https://www.droptracker.io/", "/store/droptracker/disc/static/assets/")
                attachment = interactions.File(local_path)
                message = await channel.send(f"{formatted_name} has added an item to their collection log:", embed=embed, files=attachment)
            else:
                message = await channel.send(f"{formatted_name} has added an item to their collection log!", embed=embed)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            session.commit()
            raise
    

    async def remove_group_field(self, embed: interactions.Embed):
        if embed.fields:
            embed.fields = [field for field in embed.fields if "Group" not in field.name]
        return embed
    # Add other notification handlers as needed for PBs, collection log entries, etc. 