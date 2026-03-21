# # # import asyncio

# # # from sqlalchemy import text
# # # from db import Player, session
# # # from datetime import datetime
# # # from utils.wiseoldman import check_user_by_username

# # # async def merge_duplicate_players(session, primary_player_id, duplicate_player_id):
# # #     """
# # #     Merge duplicate players by transferring all data from duplicate to primary player.
    
# # #     Args:
# # #         session: Database session
# # #         primary_player_id: ID of the player to keep
# # #         duplicate_player_id: ID of the player to merge and delete
        
# # #     Returns:
# # #         bool: True if merge was successful
# # #     """
# # #     try:
# # #         primary_player = session.query(Player).filter(Player.player_id == primary_player_id).first()
# # #         duplicate_player = session.query(Player).filter(Player.player_id == duplicate_player_id).first()
        
# # #         if not primary_player or not duplicate_player:
# # #             print(f"Could not find players: primary={primary_player_id}, duplicate={duplicate_player_id}")
# # #             return False
            
# # #         print(f"Merging player {duplicate_player_id} into {primary_player_id}")
        
# # #         # Update all foreign key references to point to primary player
# # #         tables_to_update = [
# # #             ('drops', 'player_id'),
# # #             ('personal_best', 'player_id'),
# # #             ('combat_achievement', 'player_id'),
# # #             ('collection', 'player_id'),
# # #             ('player_pets', 'player_id'),
# # #             ('notification_queue', 'player_id'),
# # #             ('notified', 'player_id'),
# # #             ('player_daily_aggregates', 'player_id'),
# # #             ('player_npc_hourly_totals', 'player_id'),
# # #             ('player_item_hourly_totals', 'player_id')
# # #         ]
        
# # #         for table_name, column_name in tables_to_update:
# # #             try:
# # #                 result = session.execute(text(f"""
# # #                     UPDATE {table_name} 
# # #                     SET {column_name} = :primary_id 
# # #                     WHERE {column_name} = :duplicate_id
# # #                 """), {"primary_id": primary_player_id, "duplicate_id": duplicate_player_id})
# # #                 print(f"Updated {result.rowcount} records in {table_name}")
# # #             except Exception as e:
# # #                 print(f"Error updating {table_name}: {e}")
        
# # #         # Update user_group_association
# # #         try:
# # #             result = session.execute(text("""
# # #                 UPDATE user_group_association 
# # #                 SET player_id = :primary_id 
# # #                 WHERE player_id = :duplicate_id
# # #             """), {"primary_id": primary_player_id, "duplicate_id": duplicate_player_id})
# # #             print(f"Updated {result.rowcount} records in user_group_association")
# # #         except Exception as e:
# # #             print(f"Error updating user_group_association: {e}")
        
# # #         # Update any other tables that might reference the player
# # #         try:
# # #             # Update any analytics tables
# # #             analytics_tables = [
# # #                 'player_item_hourly_totals',
# # #                 'player_npc_hourly_totals', 
# # #                 'player_daily_aggregates',
# # #                 'player_loot_data'
# # #             ]
            
# # #             for table in analytics_tables:
# # #                 try:
# # #                     result = session.execute(text(f"""
# # #                         UPDATE {table} 
# # #                         SET player_id = :primary_id 
# # #                         WHERE player_id = :duplicate_id
# # #                     """), {"primary_id": primary_player_id, "duplicate_id": duplicate_player_id})
# # #                     if result.rowcount > 0:
# # #                         print(f"Updated {result.rowcount} records in {table}")
# # #                 except Exception as e:
# # #                     print(f"Error updating {table}: {e}")
# # #         except Exception as e:
# # #             print(f"Error updating analytics tables: {e}")
        
# # #         # Ensure primary player has the most complete data (but don't touch account_hash to avoid unique constraint issues)
# # #         # Only update fields that don't have unique constraints
        
# # #         if not primary_player.wom_id and duplicate_player.wom_id:
# # #             primary_player.wom_id = duplicate_player.wom_id
# # #             print(f"Updated primary player wom_id to {duplicate_player.wom_id}")
        
# # #         if not primary_player.user_id and duplicate_player.user_id:
# # #             primary_player.user_id = duplicate_player.user_id
# # #             primary_player.user = duplicate_player.user
# # #             print(f"Updated primary player user_id to {duplicate_player.user_id}")
        
# # #         # Update other fields if primary is missing data
# # #         if not primary_player.log_slots and duplicate_player.log_slots:
# # #             primary_player.log_slots = duplicate_player.log_slots
# # #             print(f"Updated primary player log_slots to {duplicate_player.log_slots}")
        
# # #         if not primary_player.total_level and duplicate_player.total_level:
# # #             primary_player.total_level = duplicate_player.total_level
# # #             print(f"Updated primary player total_level to {duplicate_player.total_level}")
        
# # #         # Log what we're doing with account_hash without updating it
# # #         if duplicate_player.account_hash:
# # #             if primary_player.account_hash:
# # #                 print(f"Primary player already has account_hash: {primary_player.account_hash}, duplicate has: {duplicate_player.account_hash}")
# # #             else:
# # #                 print(f"Primary player has no account_hash, duplicate has: {duplicate_player.account_hash} (not updating to avoid conflicts)")
        
# # #         # Delete the duplicate player
# # #         session.delete(duplicate_player)
# # #         session.commit()
        
# # #         print(f"Successfully merged player {duplicate_player_id} into {primary_player_id}")
# # #         return True
        
# # #     except Exception as e:
# # #         session.rollback()
# # #         print(f"Error merging players: {e}")
# # #         return False

# # # async def find_and_fix_duplicate_players(session, player_name):
# # #     """
# # #     Find all players with the same name and merge them based on WOM ID validation.
    
# # #     Args:
# # #         session: Database session
# # #         player_name: Player name to check for duplicates
        
# # #     Returns:
# # #         dict: Summary of merge operations
# # #     """
# # #     # Find all players with the same name
# # #     duplicate_players = session.query(Player).filter(
# # #         Player.player_name == player_name
# # #     ).all()
    
# # #     if len(duplicate_players) <= 1:
# # #         return {"merged": 0, "errors": [], "message": "No duplicates found"}
    
# # #     print(f"Found {len(duplicate_players)} players with name '{player_name}'")
    
# # #     # Get current WOM data to determine the correct player
# # #     wom_player, current_name, wom_player_id, log_slots = await check_user_by_username(player_name)
    
# # #     if not wom_player:
# # #         print(f"Could not validate with WOM API for {player_name}")
# # #         return {"merged": 0, "errors": ["Could not validate with WOM API"], "message": "WOM validation failed"}
    
# # #     # Find the player with the correct WOM ID
# # #     correct_player = None
# # #     for player in duplicate_players:
# # #         if player.wom_id == wom_player_id:
# # #             correct_player = player
# # #             break
    
# # #     if not correct_player:
# # #         # If no player has the correct WOM ID, use the most recent one
# # #         correct_player = max(duplicate_players, key=lambda p: p.date_added)
# # #         print(f"No player has correct WOM ID, using most recent: {correct_player.player_id}")
    
# # #     print(f"Using player {correct_player.player_id} as primary")
    
# # #     # Merge all other players into the correct one
# # #     merged_count = 0
# # #     errors = []
    
# # #     for player in duplicate_players:
# # #         if player.player_id != correct_player.player_id:
# # #             print(f"Merging player {player.player_id} into {correct_player.player_id}")
# # #             success = await merge_duplicate_players(session, correct_player.player_id, player.player_id)
# # #             if success:
# # #                 merged_count += 1
# # #             else:
# # #                 errors.append(f"Failed to merge player_id {player.player_id}")
    
# # #     return {
# # #         "merged": merged_count, 
# # #         "errors": errors,
# # #         "primary_player_id": correct_player.player_id,
# # #         "message": f"Successfully merged {merged_count} duplicate players"
# # #     }

# # # async def find_account_hash_conflicts():
# # #     """
# # #     Find players that have the same account_hash but different player names.
# # #     This can help identify additional duplicate issues.
# # #     """
# # #     conflicts = session.execute(text("""
# # #         SELECT account_hash, COUNT(*) as count, GROUP_CONCAT(player_id) as player_ids, GROUP_CONCAT(player_name) as player_names
# # #         FROM players 
# # #         WHERE account_hash IS NOT NULL
# # #         GROUP BY account_hash 
# # #         HAVING COUNT(*) > 1
# # #     """)).fetchall()
    
# # #     print(f"Found {len(conflicts)} account_hash conflicts:")
# # #     for conflict in conflicts:
# # #         account_hash, count, player_ids, player_names = conflict
# # #         print(f"  Account hash {account_hash}: {count} players - IDs: {player_ids}, Names: {player_names}")
    
# # #     return conflicts

# # # async def merge_account_hash_conflicts():
# # #     """
# # #     Merge players that have the same account_hash but different names.
# # #     This handles cases where the same account has multiple player entries.
# # #     """
# # #     conflicts = await find_account_hash_conflicts()
# # #     merged_count = 0
    
# # #     for conflict in conflicts:
# # #         account_hash, count, player_ids_str, player_names_str = conflict
# # #         player_ids = [int(pid) for pid in player_ids_str.split(',')]
# # #         player_names = player_names_str.split(',')
        
# # #         print(f"\nProcessing account hash conflict for hash {account_hash}")
# # #         print(f"  Players: {player_names} (IDs: {player_ids})")
        
# # #         # Get all players with this account hash
# # #         players = session.query(Player).filter(Player.player_id.in_(player_ids)).all()
        
# # #         if len(players) <= 1:
# # #             continue
            
# # #         # Use the player with the most data (most recent, or with user_id, etc.)
# # #         primary_player = max(players, key=lambda p: (
# # #             p.date_added,
# # #             p.user_id is not None,
# # #             p.wom_id is not None
# # #         ))
        
# # #         print(f"  Using player {primary_player.player_id} ({primary_player.player_name}) as primary")
        
# # #         # Merge all other players into the primary
# # #         for player in players:
# # #             if player.player_id != primary_player.player_id:
# # #                 print(f"  Merging player {player.player_id} ({player.player_name}) into {primary_player.player_id}")
# # #                 success = await merge_duplicate_players(session, primary_player.player_id, player.player_id)
# # #                 if success:
# # #                     merged_count += 1
# # #                 else:
# # #                     print(f"  Failed to merge player {player.player_id}")
    
# # #     print(f"Account hash conflicts: merged {merged_count} players")
# # #     return merged_count

# # # async def cleanup_all_duplicate_players():
# # #     """
# # #     Find and fix all duplicate players in the database.
    
# # #     Returns:
# # #         dict: Summary of all cleanup operations
# # #     """     
# # #     cleanup_summary = {
# # #         'timestamp': datetime.now().isoformat(),
# # #         'total_duplicates_found': 0,
# # #         'total_players_merged': 0,
# # #         'errors': [],
# # #         'processed_players': []
# # #     }
    
# # #     try:
# # #         # First, handle account hash conflicts
# # #         print("Checking for account hash conflicts...")
# # #         account_conflicts = await find_account_hash_conflicts()
        
# # #         if account_conflicts:
# # #             print("Merging account hash conflicts first...")
# # #             account_merged = await merge_account_hash_conflicts()
# # #             cleanup_summary['total_players_merged'] += account_merged
        
# # #         # Find all duplicate player names
# # #         duplicate_names = session.execute(text("""
# # #             SELECT player_name, COUNT(*) as count 
# # #             FROM players 
# # #             GROUP BY player_name 
# # #             HAVING COUNT(*) > 1
# # #         """)).fetchall()
        
# # #         print(f"Found {len(duplicate_names)} player names with duplicates")
# # #         cleanup_summary['total_duplicates_found'] = len(duplicate_names)
        
# # #         for player_name, count in duplicate_names:
# # #             print(f"\nProcessing duplicates for '{player_name}' ({count} players)")
# # #             result = await find_and_fix_duplicate_players(session, player_name)
            
# # #             cleanup_summary['processed_players'].append({
# # #                 'player_name': player_name,
# # #                 'count': count,
# # #                 'merged': result['merged'],
# # #                 'errors': result['errors'],
# # #                 'primary_player_id': result.get('primary_player_id')
# # #             })
            
# # #             cleanup_summary['total_players_merged'] += result['merged']
# # #             cleanup_summary['errors'].extend(result['errors'])
            
# # #             print(f"Result: {result['message']}")
        
# # #         print(f"\nCleanup completed:")
# # #         print(f"Total duplicate names: {cleanup_summary['total_duplicates_found']}")
# # #         print(f"Total players merged: {cleanup_summary['total_players_merged']}")
# # #         print(f"Total errors: {len(cleanup_summary['errors'])}")
        
# # #         return cleanup_summary
        
# # #     except Exception as e:
# # #         print(f"Error during cleanup: {e}")
# # #         cleanup_summary['errors'].append(f"Cleanup error: {str(e)}")
# # #         return cleanup_summary
# # #     finally:
# # #         session.close()

# # # if __name__ == "__main__":
# # #     # First run account hash conflict resolution
# # #     print("=== STEP 1: Resolving Account Hash Conflicts ===")
# # #     asyncio.run(merge_account_hash_conflicts())
    
# # #     print("\n=== STEP 2: Resolving Duplicate Player Names ===")
# # #     asyncio.run(cleanup_all_duplicate_players())


# # import json
# # from db import Player, session
# # from utils.wiseoldman import check_user_by_username
# # import sys
# # import asyncio # Added for running async code

# # async def main(): # Wrapped the entire logic in an async function
# #     # --- Configuration & Data Preparation ---

# #     # Initialize lists to store IDs of players with issues
# #     no_hashes = []
# #     wrong_ids = []
# #     names = []

# #     # 1. Read player names from the names.txt file
# #     try:
# #         with open("names.txt", "r") as f:
# #             # Read lines, strip whitespace (including newline characters)
# #             names = [line.strip() for line in f if line.strip()]
# #     except FileNotFoundError:
# #         print("Error: names.txt not found. Please ensure the file exists.")
# #         # Using return instead of sys.exit(1) since we are inside an async function
# #         return


# #     # 2. Iterate through names, check database and external API
# #     print(f"Starting audit for {len(names)} players...")

# #     for name in names:
# #         # Query database for player (SQLAlchemy is synchronous)
# #         player = session.query(Player).filter(Player.player_name == name).first()

# #         if not player:
# #             print(f"Warning: Player '{name}' not found in local database.")
# #             continue

# #         # Check for missing account hash
# #         if player.account_hash is None:
# #             print(f"No hash still: {player.player_name}")
# #             no_hashes.append(player.player_id)

# #         # Check external Wise Old Man (WOM) ID
# #         try:
# #             # Attempt to fetch external data (NOW using 'await')
# #             player_obj, player_name, wom_id, display_name = await check_user_by_username(player.player_name)

# #             if wom_id is not None and wom_id != player.wom_id:
# #                 print(f"Wrong ID: {player.player_name} (DB ID: {player.wom_id}, WOM ID: {wom_id})")
# #                 wrong_ids.append(player.player_id)

# #         except ValueError:
# #             # Catch error if check_user_by_username doesn't return 4 values or raises an error
# #             print(f"Error processing WOM check for {player.player_name}: check_user_by_username failed or returned unexpected format.")
# #             # If the external check fails, we cannot verify the ID, so we skip the check for this player.
# #         except Exception as e:
# #             # Catch other potential errors (e.g., API request failure)
# #             print(f"Unexpected error during WOM check for {player.player_name}: {e}")
            
# #         # Note: If you need to commit changes to the database, you would do it here:
# #         # session.commit()


# #     # 3. Write results to output.json
# #     output_data = {
# #         "players_without_account_hash": no_hashes,
# #         "players_with_mismatched_wom_id": wrong_ids
# #     }

# #     print("\n--- Summary ---")
# #     print(f"Players without account hash: {len(no_hashes)}")
# #     print(f"Players with mismatched WOM ID: {len(wrong_ids)}")

# #     try:
# #         with open("output.json", "w") as jsonfile:
# #             json.dump(output_data, jsonfile, indent=4)
# #         print("Successfully wrote results to output.json")
# #     except IOError as e:
# #         print(f"Error writing to output.json: {e}")

# #     # Close the database session
# #     session.close()

# # if __name__ == "__main__":
# #     try:
# #         asyncio.run(main()) # Entry point to run the async function
# #     except KeyboardInterrupt:
# #         print("\nAudit interrupted by user.")
# #     except Exception as e:
# #         print(f"An unexpected error occurred during execution: {e}")
# #         sys.exit(1)


# import asyncio
# import interactions
# from interactions import Intents, listen
# from interactions.api.events import Startup
# from datetime import datetime, timedelta
# from lootboard import generator

# # Initialize bot
# bot = interactions.Client(
#     intents=Intents.DIRECT_MESSAGES | Intents.GUILD_INTEGRATIONS,
#     send_command_traceback=False,
#     owner_ids=[528746710042804247, 232236164776460288]
# )

# @listen(Startup)
# async def startup_meth():
#     # Define your time window
#     target_time = datetime(2026, 1, 5, 7, 34, 10)
#     start_time = target_time - timedelta(days=14)  # 30 seconds before
#     end_time = target_time + timedelta(days=14)    # 30 seconds after
    
#     # Or use explicit times:
#     # start_time = datetime(2026, 1, 5, 7, 33, 40)
#     # end_time = datetime(2026, 1, 5, 7, 34, 40)
    
#     print(f"Generating timeframe board from {start_time} to {end_time}")
#     await generator.generate_timeframe_board(
#         bot, 
#         group_id=7,
#         wom_group_id=0,
#         start_time=start_time,
#         end_time=end_time
#     )
#     print("Done!")
#     await bot.stop()

# # Start the bot
# bot.start(os.getenv("BOT_TOKEN"))


from scripts.user_dmer import send_bulk_dms
from interactions import ComponentType, ContainerComponent, Embed, SeparatorComponent, TextDisplayComponent, ThumbnailComponent, UnfurledMediaItem
from interactions.models import SectionComponent
import asyncio

logo_media = UnfurledMediaItem(
    url="https://www.droptracker.io/img/droptracker-small.gif"
)

# Use <@user> as placeholder - it will be replaced with actual mention for each user
feedback_components = [
    ContainerComponent(
        SeparatorComponent(divider=True),
        TextDisplayComponent(
            content="## Hey, <@user>!\n We Need Your Input! 🎯",
        ),
        SeparatorComponent(divider=True),
        SectionComponent(
            components=[
                TextDisplayComponent(
                    content="you are"
                    # "-# **Hello!**\n\n" +
                    # "As DropTracker continues to evolve, we're dedicating the next few weeks to platform enhancements based on **your** feedback.\n\n" +
                    # "**Current Development Focus:**\n" +
                    # "-# • Website improvements and bug fixes\n" +
                    # "-# • Implementing remaining notification types (quests, diaries, levels)\n" +
                    # "-# • Enhancing overall user experience\n\n" +
                    # "**How You Can Help:**\n" +
                    # "-# 1. **Share feedback** - What's working well? What needs improvement?\n" +
                    # "-# 2. **Suggest features** - What would make DropTracker better for your group?\n" +
                    # "-# 3. **Contribute to documentation** - Interested in helping with our wiki/docs? Reach out!\n\n" +
                    # "-# 4. **Support the project** - Consider supporting via an [Account Upgrade](https://www.droptracker.io/groups/upgrades) if you find value in what we're building.\n\n" +
                    # "-# Your input directly shapes our development roadmap and helps us prioritize what matters most to the community.\n\n" +
                    # "-# **Reply to this message, reach out via support ticket, or join our Discord** to share your thoughts.\n\n" +
                    # "-# Thank you for being part of the DropTracker community. Your continued support makes this project possible! 🎉\n\n"
                )
            ],
            accessory=ThumbnailComponent(
                media=logo_media
            )
        ),
        SeparatorComponent(divider=True),
    )
]


async def main():
    # No manual replacement needed - send_bulk_dms handles <@user> automatically!
    results = await send_bulk_dms(
        user_ids=[1298847468146004028],  # Replace with your target group leader IDs
        content="",
        components=feedback_components,
        # Uses DM_BOT_TOKEN or BOT_TOKEN from env by default
        # personalize=True is the default - automatically replaces <@user> with actual mention
    )
    print(results.summary())
    print(f"DMs disabled: {results.get_forbidden_user_ids()}")
    
asyncio.run(main())