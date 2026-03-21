"""
Test script for debugging drop notification placeholder replacements.

This script:
1. Uses a hardcoded bot token on startup
2. Uses a hardcoded target user ID for DMs
3. Grabs a drop notification from the database
4. Processes it through the notification logic but DMs the result to a target user

This helps debug why group/player ranks are not properly populating in embeds.

Usage:
    python test_drop_notification_dm.py                    # Use most recent drop notification
    python test_drop_notification_dm.py --id 12345         # Use specific notification ID
    python test_drop_notification_dm.py --player "Name"    # Use most recent for player
    python test_drop_notification_dm.py --group 5          # Use most recent for group
    python test_drop_notification_dm.py --debug-redis      # Extra Redis debugging
"""

import argparse
import asyncio
import json
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import interactions
from interactions import Intents
from datetime import datetime

# Database imports
from db.models import (
    NotificationQueue, ItemList, NpcList, Player, Group, GroupConfiguration,
    get_current_partition, Drop
)
from db.ops import DatabaseOperations, get_formatted_name
from db.xf.upgrades import check_active_upgrade
from api.core import get_db_session

# Utility imports
from utils.redis import redis_client
from utils.format import format_number, replace_placeholders
from services.redis_updates import get_player_list_loot_sum, loot_tracker

# ============================================================================
# CONFIGURATION - UPDATE THESE VALUES
# ============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # Set via .env
TARGET_USER_ID = 528746710042804247  # Replace with the Discord user ID to DM
# ============================================================================

# Global args for the on_startup handler
ARGS = None


class DropNotificationTester:
    """Test class for debugging drop notification placeholder replacements."""
    
    def __init__(self, bot: interactions.Client, debug_redis: bool = False):
        self.bot = bot
        self.db_ops = DatabaseOperations()
        self.debug_redis = debug_redis
    
    async def get_pending_drop_notification(self, db_session, notification_id: int = None, 
                                             player_name: str = None, group_id: int = None):
        """Fetch a drop notification from the database based on criteria."""
        
        # If specific ID provided, get that one
        if notification_id:
            notification = db_session.query(NotificationQueue).filter(
                NotificationQueue.id == notification_id,
                NotificationQueue.notification_type == 'drop'
            ).first()
            return notification
        
        # Build query based on filters
        query = db_session.query(NotificationQueue).filter(
            NotificationQueue.notification_type == 'drop'
        )
        
        if player_name:
            player = db_session.query(Player).filter(Player.player_name == player_name).first()
            if player:
                query = query.filter(NotificationQueue.player_id == player.player_id)
        
        if group_id:
            query = query.filter(NotificationQueue.group_id == group_id)
        
        # Prefer pending, but fall back to any
        notification = query.filter(
            NotificationQueue.status == 'pending'
        ).order_by(NotificationQueue.created_at.desc()).first()
        
        if not notification:
            notification = query.order_by(NotificationQueue.created_at.desc()).first()
        
        return notification
    
    async def debug_redis_state(self, player_id: int, group_id: int, partition: int):
        """Print detailed Redis state for debugging."""
        print("\n" + "="*80)
        print("DETAILED REDIS STATE DEBUG")
        print("="*80)
        
        # Check global leaderboard
        global_key = f"leaderboard:{partition}"
        print(f"\n[REDIS] Global Leaderboard Key: {global_key}")
        
        exists = redis_client.client.exists(global_key)
        print(f"  Key Exists: {exists}")
        
        if exists:
            total_members = redis_client.client.zcard(global_key)
            print(f"  Total Members: {total_members}")
            
            player_score = redis_client.client.zscore(global_key, player_id)
            print(f"  Player {player_id} Score: {player_score}")
            
            if player_score is not None:
                player_rank = redis_client.client.zrevrank(global_key, player_id)
                print(f"  Player {player_id} Rank (0-based): {player_rank}")
                print(f"  Player {player_id} Rank (1-based): {player_rank + 1 if player_rank is not None else None}")
            
            # Show top 5 in leaderboard
            top_5 = redis_client.client.zrevrange(global_key, 0, 4, withscores=True)
            print(f"  Top 5 Players:")
            for i, (pid, score) in enumerate(top_5, 1):
                pid_str = pid.decode() if isinstance(pid, bytes) else str(pid)
                print(f"    {i}. Player {pid_str}: {score}")
        
        # Check group leaderboard
        group_key = f"leaderboard:{partition}:group:{group_id}"
        print(f"\n[REDIS] Group Leaderboard Key: {group_key}")
        
        exists = redis_client.client.exists(group_key)
        print(f"  Key Exists: {exists}")
        
        if exists:
            total_members = redis_client.client.zcard(group_key)
            print(f"  Total Members: {total_members}")
            
            player_score = redis_client.client.zscore(group_key, player_id)
            print(f"  Player {player_id} Score: {player_score}")
            
            if player_score is not None:
                player_rank = redis_client.client.zrevrank(group_key, player_id)
                print(f"  Player {player_id} Rank (0-based): {player_rank}")
                print(f"  Player {player_id} Rank (1-based): {player_rank + 1 if player_rank is not None else None}")
            
            # Show top 5 in group leaderboard
            top_5 = redis_client.client.zrevrange(group_key, 0, 4, withscores=True)
            print(f"  Top 5 in Group:")
            for i, (pid, score) in enumerate(top_5, 1):
                pid_str = pid.decode() if isinstance(pid, bytes) else str(pid)
                print(f"    {i}. Player {pid_str}: {score}")
        else:
            print(f"  WARNING: Group leaderboard does not exist!")
            
            # Check what group keys do exist
            pattern = f"leaderboard:{partition}:group:*"
            matching_keys = list(redis_client.client.scan_iter(match=pattern, count=100))[:10]
            print(f"  Existing group leaderboard keys (first 10):")
            for key in matching_keys:
                key_str = key.decode() if isinstance(key, bytes) else str(key)
                print(f"    - {key_str}")
        
        # Check player-specific keys
        print(f"\n[REDIS] Player-Specific Keys for player {player_id}:")
        
        total_loot_key = f"player:{player_id}:{partition}:total_loot"
        total_loot = redis_client.get(total_loot_key)
        print(f"  {total_loot_key}: {total_loot}")
        
        # Check for any player keys
        player_pattern = f"player:{player_id}:*"
        player_keys = list(redis_client.client.scan_iter(match=player_pattern, count=50))
        print(f"  All player keys (pattern: {player_pattern}):")
        for key in player_keys[:20]:  # Limit to 20
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            val = redis_client.client.get(key)
            val_str = val.decode() if isinstance(val, bytes) else str(val) if val else "None"
            print(f"    - {key_str}: {val_str[:50]}...")
    
    async def process_and_dm_drop_notification(self, notification: NotificationQueue, target_user_id: int, db_session):
        """
        Process a drop notification similar to send_drop_notification,
        but send it as a DM to the target user instead.
        
        This includes extensive debug output to identify placeholder issues.
        """
        print("\n" + "="*80)
        print("PROCESSING DROP NOTIFICATION")
        print("="*80)
        
        # Parse notification data
        data = json.loads(notification.data)
        group_id = notification.group_id
        player_id = notification.player_id
        drop_id = data.get('drop_id')
        
        print(f"\n[INFO] Notification ID: {notification.id}")
        print(f"[INFO] Group ID: {group_id}")
        print(f"[INFO] Player ID: {player_id}")
        print(f"[INFO] Drop ID: {drop_id}")
        print(f"[INFO] Status: {notification.status}")
        print(f"[INFO] Created At: {notification.created_at}")
        print(f"\n[DATA] Raw notification data:")
        print(json.dumps(data, indent=2))
        
        # Get player name
        player_name = data.get('player_name')
        item_name = data.get('item_name')
        kill_count = data.get('kill_count', None)
        
        print(f"\n[PLAYER] Player Name: {player_name}")
        print(f"[ITEM] Item Name: {item_name}")
        print(f"[KC] Kill Count: {kill_count}")
        
        # Get item ID
        item_id_record = db_session.query(ItemList).filter(ItemList.item_name == item_name).first()
        if item_id_record:
            item_id = item_id_record.item_id
            print(f"[ITEM] Item ID found: {item_id}")
        else:
            item_id = 1
            print(f"[ITEM] Item not found in database, using default ID: {item_id}")
        
        # Get NPC ID
        npc_name = data.get('npc_name', None)
        print(f"[NPC] NPC Name: {npc_name}")
        
        if npc_name:
            npc_id_record = db_session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
            if npc_id_record:
                npc_id = npc_id_record.npc_id
                print(f"[NPC] NPC ID found: {npc_id}")
            else:
                npc_id = 1
                print(f"[NPC] NPC not found in database, using default ID: {npc_id}")
        else:
            npc_id = 0
        
        # Get values
        value = data.get('value')
        quantity = data.get('quantity')
        total_value = data.get('total_value')
        image_url = data.get('image_url', None)
        
        print(f"\n[VALUE] Value: {value}")
        print(f"[VALUE] Quantity: {quantity}")
        print(f"[VALUE] Total Value: {total_value}")
        print(f"[IMAGE] Image URL: {image_url}")
        
        # Get embed template
        upgrade_active = check_active_upgrade(group_id)
        print(f"\n[UPGRADE] Upgrade Active for Group {group_id}: {upgrade_active}")
        
        if upgrade_active:
            embed_template = await self.db_ops.get_group_embed('drop', group_id)
            print(f"[EMBED] Using custom embed template for group {group_id}")
        else:
            embed_template = await self.db_ops.get_group_embed('drop', 1)
            print(f"[EMBED] Using default embed template (group 1)")
        
        if not embed_template:
            print("[ERROR] No embed template found!")
            return
        
        # Get player info
        player = None
        if not player_id:
            player = db_session.query(Player).filter(Player.player_name == player_name).first()
            if player:
                player_id = player.player_id
                print(f"[PLAYER] Found player by name, ID: {player_id}")
        
        # Calculate partition and totals
        partition = get_current_partition()
        print(f"\n[PARTITION] Current Partition: {partition}")
        
        # Get monthly total
        month_total_int = self._get_player_month_total(player_id, partition)
        player_month_total = format_number(month_total_int)
        print(f"[LOOT] Player Month Total (raw): {month_total_int}")
        print(f"[LOOT] Player Month Total (formatted): {player_month_total}")
        
        # Get group players and total
        players_in_group = db_session.query(Player.player_id).join(Player.groups).filter(
            Group.group_id == group_id
        ).all()
        print(f"[GROUP] Players in Group {group_id}: {len(players_in_group)}")
        
        group_month_total = format_number(get_player_list_loot_sum([p.player_id for p in players_in_group]))
        print(f"[GROUP] Group Month Total: {group_month_total}")
        
        # ========================================================================
        # RANK CALCULATIONS - This is where issues might occur
        # ========================================================================
        print("\n" + "-"*40)
        print("RANK CALCULATIONS")
        print("-"*40)
        
        # Run detailed Redis debug if requested
        if self.debug_redis:
            await self.debug_redis_state(player_id, group_id, partition)
        
        # Global rank
        global_rank_data = loot_tracker.get_player_rank(player_id, None, partition)
        print(f"[RANK] Global Rank Data (raw): {global_rank_data}")
        
        # Group rank
        group_rank_data = loot_tracker.get_player_rank(player_id, group_id, partition)
        print(f"[RANK] Group Rank Data (raw): {group_rank_data}")
        
        # Parse group rank
        if group_rank_data:
            group_rank, user_count = group_rank_data
            print(f"[RANK] Group Rank: {group_rank} / {user_count}")
        else:
            group_rank = None
            user_count = redis_client.client.zcard(f"leaderboard:{partition}:group:{group_id}")
            print(f"[RANK] Group Rank: None (fallback user_count: {user_count})")
        
        # Parse global rank
        if global_rank_data:
            global_rank, total_global_players = global_rank_data
            print(f"[RANK] Global Rank: {global_rank} / {total_global_players}")
        else:
            global_rank = None
            total_global_players = redis_client.client.zcard(f"leaderboard:{partition}")
            print(f"[RANK] Global Rank: None (fallback total: {total_global_players})")
        
        # Group-to-group ranking
        all_groups = db_session.query(Group.group_id).filter(Group.group_id != 2).all()
        total_groups = len(all_groups) - 1
        print(f"[RANK] Total Groups (excluding global): {total_groups}")
        
        group_totals = []
        for group in all_groups:
            group_total = redis_client.zsum(f"leaderboard:{partition}:group:{group.group_id}")
            group_totals.append({'id': group.group_id, 'total': group_total})
        
        sorted_groups = sorted(group_totals, key=lambda x: x['total'], reverse=True)
        group_to_group_rank = str(next((i for i, g in enumerate(sorted_groups) if g['id'] == group_id), 0) + 1)
        print(f"[RANK] Group-to-Group Rank: {group_to_group_rank} / {total_groups}")
        
        # Debug: Check Redis keys directly
        print("\n" + "-"*40)
        print("REDIS KEY DEBUG")
        print("-"*40)
        
        global_leaderboard_key = f"leaderboard:{partition}"
        group_leaderboard_key = f"leaderboard:{partition}:group:{group_id}"
        
        print(f"[REDIS] Global Leaderboard Key: {global_leaderboard_key}")
        print(f"[REDIS] Group Leaderboard Key: {group_leaderboard_key}")
        
        # Check if player exists in leaderboards
        global_score = redis_client.client.zscore(global_leaderboard_key, player_id)
        group_score = redis_client.client.zscore(group_leaderboard_key, player_id)
        
        print(f"[REDIS] Player Score in Global Leaderboard: {global_score}")
        print(f"[REDIS] Player Score in Group Leaderboard: {group_score}")
        
        # Check total members in each leaderboard
        global_members = redis_client.client.zcard(global_leaderboard_key)
        group_members = redis_client.client.zcard(group_leaderboard_key)
        
        print(f"[REDIS] Global Leaderboard Total Members: {global_members}")
        print(f"[REDIS] Group Leaderboard Total Members: {group_members}")
        
        # Build rank strings
        if global_rank is not None and total_global_players is not None:
            global_rank_str = "`" + str(global_rank) + "`" + "/" + "`" + str(total_global_players) + "`"
        else:
            global_rank_str = "`?`"
        
        if group_rank is not None and user_count is not None:
            group_rank_str = "`" + str(group_rank) + "`" + "/" + "`" + str(user_count) + "`"
        else:
            group_rank_str = "`?`"
        
        print(f"\n[STRING] Global Rank String: {global_rank_str}")
        print(f"[STRING] Group Rank String: {group_rank_str}")
        
        # Get formatted player name
        formatted_name = get_formatted_name(player_name, group_id, db_session)
        print(f"[NAME] Formatted Player Name: {formatted_name}")
        
        # Build placeholder values
        values = {
            "{item_name}": item_name,
            "{month_name}": datetime.now().strftime("%B"),
            "{player_total_month}": "`" + player_month_total + "`",
            "{global_rank}": global_rank_str,
            "{group_rank}": group_rank_str,
            "{group_total}": "`" + str(group_month_total) + "`",
            "{user_count}": "`" + str(user_count) + "`",
            "{group_total_month}": "`" + group_month_total + "`",
            "{group_to_group_rank}": "`" + str(group_to_group_rank) + "`" + "/" + "`" + str(total_groups) + "`",
            "{item_id}": str(item_id),
            "{npc_id}": str(npc_id),
            "{npc_name}": npc_name,
            "{kill_count}": str(kill_count),
            "{item_value}": "`" + format_number(total_value) + "`",
            "{quantity}": "`" + str(quantity) + "`",
            "{total_value}": "`" + str(total_value) + "`",
            "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{player_name}.{player_id}/view)",
            "{image_url}": image_url or ""
        }
        
        print("\n" + "-"*40)
        print("PLACEHOLDER VALUES")
        print("-"*40)
        for key, val in values.items():
            print(f"  {key}: {val}")
        
        # Replace placeholders
        embed = replace_placeholders(embed_template, values)
        
        if group_id == 2:
            embed = await self.remove_group_field(embed)
        if kill_count is None or int(kill_count) < 1:
            embed = await self.remove_kc_field(embed)
        
        print("\n" + "-"*40)
        print("FINAL EMBED")
        print("-"*40)
        print(f"  Title: {embed.title}")
        print(f"  Description: {embed.description}")
        if embed.fields:
            for i, field in enumerate(embed.fields):
                print(f"  Field {i}: {field.name} = {field.value}")
        
        # Get attachment if available
        attachment = None
        if image_url and "droptracker.io" in image_url:
            local_url = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
            if os.path.exists(local_url):
                attachment = interactions.File(local_url)
                print(f"\n[ATTACH] Found local image: {local_url}")
            else:
                print(f"\n[ATTACH] Image not found locally: {local_url}")
        
        # Send DM to target user
        print("\n" + "="*80)
        print("SENDING DM TO TARGET USER")
        print("="*80)
        
        try:
            target_user = await self.bot.fetch_user(target_user_id)
            print(f"[DM] Target User: {target_user.username} ({target_user_id})")
            
            debug_info = f"**Debug Info for Notification #{notification.id}**\n"
            debug_info += f"Group ID: {group_id}\n"
            debug_info += f"Player ID: {player_id}\n"
            debug_info += f"Global Rank Raw: {global_rank_data}\n"
            debug_info += f"Group Rank Raw: {group_rank_data}\n"
            
            await target_user.send(debug_info)
            
            if attachment:
                await target_user.send(f"{formatted_name} received a drop:", embed=embed, files=attachment)
            else:
                await target_user.send(f"{formatted_name} received a drop:", embed=embed)
            
            print("[SUCCESS] DM sent successfully!")
            
        except Exception as e:
            print(f"[ERROR] Failed to send DM: {e}")
            raise
    
    def _get_player_month_total(self, player_id: int, partition: int = None) -> int:
        """Fetch the player's monthly total loot from Redis."""
        try:
            if partition is None:
                partition = get_current_partition()
            key = f"player:{player_id}:{partition}:total_loot"
            total_str = redis_client.get(key)
            if total_str is None:
                score = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
                return int(float(score)) if score is not None else 0
            return int(float(total_str))
        except Exception:
            return 0
    
    async def remove_group_field(self, embed: interactions.Embed):
        """Removes the Group field from the embed."""
        if embed.fields:
            embed.fields = [field for field in embed.fields if "Group" not in field.name]
        return embed
    
    async def remove_kc_field(self, embed: interactions.Embed):
        """Removes the Kills field from the embed."""
        if embed.fields:
            embed.fields = [field for field in embed.fields if "Source:" not in field.name]
        return embed


# Create bot instance
bot = interactions.Client(
    intents=Intents.DIRECT_MESSAGES | Intents.GUILD_INTEGRATIONS,
    send_command_traceback=False
)


@interactions.listen()
async def on_startup():
    """Called when the bot is ready."""
    global ARGS
    
    print("\n" + "="*80)
    print("BOT STARTED - RUNNING DROP NOTIFICATION TEST")
    print("="*80)
    print(f"Bot User: {bot.user.username} ({bot.user.id})")
    
    debug_redis = ARGS.debug_redis if ARGS else False
    tester = DropNotificationTester(bot, debug_redis=debug_redis)
    
    with get_db_session() as db_session:
        # Get a drop notification based on arguments
        notification_id = ARGS.id if ARGS else None
        player_name = ARGS.player if ARGS else None
        group_id = ARGS.group if ARGS else None
        
        notification = await tester.get_pending_drop_notification(
            db_session, 
            notification_id=notification_id,
            player_name=player_name,
            group_id=group_id
        )
        
        if notification:
            print(f"\n[FOUND] Drop notification found: ID {notification.id}")
            await tester.process_and_dm_drop_notification(notification, TARGET_USER_ID, db_session)
        else:
            print("\n[ERROR] No drop notifications found matching criteria!")
            print(f"  Notification ID: {notification_id}")
            print(f"  Player Name: {player_name}")
            print(f"  Group ID: {group_id}")
            
            # Try to DM the user about the error
            try:
                target_user = await bot.fetch_user(TARGET_USER_ID)
                await target_user.send("No drop notifications found in the database matching your criteria!")
            except Exception as e:
                print(f"[ERROR] Could not DM user: {e}")
    
    print("\n" + "="*80)
    print("TEST COMPLETE - STOPPING BOT")
    print("="*80)
    
    # Stop the bot after testing
    await asyncio.sleep(2)
    await bot.stop()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Test drop notification placeholder replacements by DMing a test user"
    )
    parser.add_argument(
        "--id", type=int, default=None,
        help="Specific notification ID to test"
    )
    parser.add_argument(
        "--player", type=str, default=None,
        help="Player name to filter notifications by"
    )
    parser.add_argument(
        "--group", type=int, default=None,
        help="Group ID to filter notifications by"
    )
    parser.add_argument(
        "--debug-redis", action="store_true",
        help="Enable detailed Redis state debugging"
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Bot token (overrides hardcoded value)"
    )
    parser.add_argument(
        "--user", type=int, default=None,
        help="Target user ID to DM (overrides hardcoded value)"
    )
    return parser.parse_args()


async def main():
    """Main entry point."""
    global ARGS, BOT_TOKEN, TARGET_USER_ID
    
    ARGS = parse_args()
    
    # Override config from command line if provided
    token = ARGS.token if ARGS.token else BOT_TOKEN
    target_user = ARGS.user if ARGS.user else TARGET_USER_ID
    
    # Update globals for the on_startup handler
    if ARGS.user:
        TARGET_USER_ID = ARGS.user
    
    if token == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please set your bot token!")
        print("  Either edit the BOT_TOKEN variable in the script")
        print("  Or pass --token YOUR_TOKEN on the command line")
        return
    
    if target_user == 123456789012345678:
        print("ERROR: Please set your target user ID!")
        print("  Either edit the TARGET_USER_ID variable in the script")
        print("  Or pass --user YOUR_USER_ID on the command line")
        return
    
    print("="*80)
    print("DROP NOTIFICATION TEST SCRIPT")
    print("="*80)
    print(f"Target User ID: {TARGET_USER_ID}")
    print(f"Notification ID Filter: {ARGS.id}")
    print(f"Player Name Filter: {ARGS.player}")
    print(f"Group ID Filter: {ARGS.group}")
    print(f"Debug Redis: {ARGS.debug_redis}")
    print("="*80)
    print("\nStarting bot...")
    
    await bot.astart(token)


if __name__ == "__main__":
    asyncio.run(main())
