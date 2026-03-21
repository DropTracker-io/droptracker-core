from db.base import Base, Session, sessionmaker, engine
from sqlalchemy import text
import time
import logging
import json
from datetime import datetime
from sqlalchemy.exc import OperationalError
import signal

from db.models import NotifiedSubmission, NotificationQueue

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def execute_with_timeout(session, query, params=None, timeout=30):
    """Execute query with timeout"""
    try:
        # Set a very short timeout for individual queries
        result = session.execute(text(query), params or {})
        return result
    except OperationalError as e:
        if "timed out" in str(e).lower():
            logger.warning(f"Query timed out after {timeout} seconds")
        raise

def cleanup_duplicate_drops(dry_run=True, days_back=7):
    """
    Remove duplicate drops based on unique_id for the past N days.
    Uses efficient batch processing to avoid timeouts.
    
    Args:
        dry_run (bool): If True, only show what would be deleted without actually deleting
        days_back (int): Number of days back to look for duplicates
    
    Returns:
        dict: Summary of cleanup operation including affected player IDs
    """
    session = None
    affected_players = set()
    cleanup_summary = {
        'timestamp': datetime.now().isoformat(),
        'dry_run': dry_run,
        'days_back': days_back,
        'total_duplicates_found': 0,
        'total_drops_deleted': 0,
        'unique_ids_processed': 0,
        'affected_player_ids': [],
        'errors': []
    }
    
    try:
        Session = sessionmaker(bind=engine)
        session = Session()
        # Scan the entire database from starting point in batches
        logger.info("Scanning entire database for duplicates...")
        
        start_drop_id = 60000000
        batch_size = 1000
        seen_unique_ids = {}  # This will persist across all batches
        duplicate_drops = []
        total_scanned = 0
        current_drop_id = start_drop_id
        
        while True:
            logger.info(f"Scanning batch starting from drop_id {current_drop_id}...")
            
            # Get next batch of drops ordered by drop_id
            batch_sql = f"""
            SELECT drop_id, unique_id, player_id, item_id, quantity, value, date_added
        FROM drops
            WHERE unique_id IS NOT NULL
            AND date_added > NOW() - INTERVAL {days_back} DAY
            AND drop_id >= {current_drop_id}
            ORDER BY drop_id ASC
            LIMIT {batch_size}
            """
            
            result = session.execute(text(batch_sql))
            batch_drops = result.fetchall()
            
            if not batch_drops:
                logger.info("No more drops found. Finished scanning.")
                break
            
            logger.info(f"Processing batch of {len(batch_drops)} drops...")
            total_scanned += len(batch_drops)
            
            # Find duplicates in this batch (checking against ALL previously seen unique_ids)
            batch_duplicates = 0
            for drop in batch_drops:
                drop_id, unique_id, player_id, item_id, quantity, value, date_added = drop
                
                # Debug logging for specific drop_ids we're looking for
                if drop_id in [67469606, 67469621]:
                    logger.info(f"PROCESSING TARGET DROP: drop_id={drop_id}, unique_id={unique_id[:50]}...")
                
                # Skip the first drop if it's the same as current_drop_id (to avoid processing the same drop twice)
                # BUT be more careful about this logic to avoid skipping legitimate first occurrences
                if drop_id == current_drop_id and current_drop_id != start_drop_id:
                    # Only skip if we're confident this was processed in the previous batch
                    # For now, let's be more conservative and not skip unless we're sure
                    if drop_id in [67469606, 67469621]:
                        logger.info(f"SKIPPING DROP due to current_drop_id logic: drop_id={drop_id}, current_drop_id={current_drop_id}")
                    # Let's comment out the continue for now to see if this fixes the issue
                    # continue
                
                # Debug logging for our target drops
                if drop_id in [67469606, 67469621]:
                    logger.info(f"Checking if unique_id {unique_id[:30]}... is in seen_unique_ids: {unique_id in seen_unique_ids}")
                    if unique_id in seen_unique_ids:
                        logger.info(f"Found in seen_unique_ids! Original drop_id: {seen_unique_ids[unique_id][0]}")
                
                if unique_id in seen_unique_ids:
                    # Found a duplicate! Compare with the original to see which is older
                    original_drop = seen_unique_ids[unique_id]
                    original_drop_id = original_drop[0]
                    
                    # Keep the older drop (lower drop_id), delete the newer one
                    if drop_id < original_drop_id:
                        # Current drop is older, so delete the original and keep this one
                        duplicate_drops.append(original_drop)
                        seen_unique_ids[unique_id] = drop  # Replace with older drop
                        logger.info(f"DUPLICATE FOUND: drop_id {drop_id} is OLDER than {original_drop_id}, will keep {drop_id}")
                        logger.info(f"  Unique ID: {unique_id[:50]}...")
                        logger.info(f"  Player ID: {player_id}, Item ID: {item_id}")
                    else:
                        # Current drop is newer, so delete it
                        duplicate_drops.append(drop)
                        logger.info(f"DUPLICATE FOUND: drop_id {drop_id} is NEWER than {original_drop_id}, will delete {drop_id}")
                        logger.info(f"  Unique ID: {unique_id[:50]}...")
                        logger.info(f"  Player ID: {player_id}, Item ID: {item_id}")
                    
                    batch_duplicates += 1
                else:
                    # First time seeing this unique_id, store it
                    seen_unique_ids[unique_id] = drop
                    if drop_id in [67469606, 67469621]:
                        logger.info(f"STORING FIRST OCCURRENCE: drop_id={drop_id}, unique_id={unique_id[:30]}...")
                
                # Update current position for next batch
                current_drop_id = drop_id + 1
            
            logger.info(f"Batch complete: {batch_duplicates} duplicates found (Total scanned: {total_scanned}, Unique IDs tracked: {len(seen_unique_ids)})")
            
            # If we got fewer drops than batch_size, we've reached the end
            if len(batch_drops) < batch_size:
                logger.info("Reached end of database.")
                break
        
        if not duplicate_drops:
            logger.info(f"No duplicates found in {total_scanned} drops scanned. Exiting.")
            return
        
        logger.info(f"Scan complete: Found {len(duplicate_drops)} duplicate drops in {total_scanned} total drops scanned")
        cleanup_summary['total_duplicates_found'] = len(duplicate_drops)
        
        if dry_run:
            # Show what would be deleted
            logger.info("DUPLICATE DROPS THAT WOULD BE DELETED (DRY RUN):")
            logger.info("drop_id | player_id | item_id | unique_id | qty | value | total_value | date_added")
            logger.info("-" * 80)
            
            for drop in duplicate_drops:
                drop_id, unique_id, player_id, item_id, quantity, value, date_added = drop
                total_value = quantity * value
                logger.info(f"{drop_id:8} | {player_id:9} | {item_id:7} | {unique_id[:15]:15} | {quantity:3} | {value:5} | {total_value:11} | {date_added}")
                
                # Track affected players
                affected_players.add(player_id)
            
            cleanup_summary['affected_player_ids'] = list(affected_players)
            
            logger.info(f"\nDRY RUN: Would delete {len(duplicate_drops)} duplicate drops")
            logger.info(f"Affected {len(affected_players)} unique players")
            logger.info(f"Lowest ID: {duplicate_drops[0][0]}")
            logger.info(f"Highest ID: {duplicate_drops[-1][0]}")
            
            return cleanup_summary
        
        # Actual deletion - delete the duplicate drops directly
        deleted_total = 0
        
        for i, drop in enumerate(duplicate_drops):
            cleanup_summary['unique_ids_processed'] += 1
            drop_id, unique_id, player_id, item_id, quantity, value, date_added = drop
            
            logger.info(f"Processing {i+1}/{len(duplicate_drops)}: Deleting drop_id {drop_id} (unique_id {unique_id[:20]}...)")
            
            try:
                # Create a fresh session for each deletion
                fresh_session = Session()
                
                # Track affected players
                affected_players.add(player_id)
                
                # First, delete any related records in notified table (foreign key constraint)
                notified_submissions = fresh_session.query(NotifiedSubmission).filter(NotifiedSubmission.drop_id == drop_id).all()
                if notified_submissions:
                    logger.info(f"Deleting {len(notified_submissions)} notified submissions for drop_id {drop_id}")
                    for notified_submission in notified_submissions:
                        fresh_session.delete(notified_submission)
                    fresh_session.commit()  # Commit the deletion of notified submissions first
                
                # Now delete the drop
                delete_sql = """
                DELETE FROM drops
                WHERE drop_id = :drop_id
                """
                
                result = fresh_session.execute(text(delete_sql), {
                    'drop_id': drop_id
                })
                
                deleted_count = result.rowcount
                deleted_total += deleted_count
                
                if deleted_count > 0:
                    logger.info(f"Deleted drop_id {drop_id} (Total: {deleted_total})")
                else:
                    logger.warning(f"Drop {drop_id} was not found or already deleted")
                
                # Commit the drop deletion
                fresh_session.commit()
                fresh_session.close()
                
            except Exception as e:
                logger.error(f"Error deleting drop_id {drop_id}: {e}")
                cleanup_summary['errors'].append(f"Deletion error for drop_id {drop_id}: {str(e)}")
                if 'fresh_session' in locals():
                    try:
                        fresh_session.rollback()
                        fresh_session.close()
                    except:
                        pass
                continue
            
            # Small delay between deletions
            time.sleep(0.05)
        
        # Final commit
        session.commit()
        
        cleanup_summary['total_drops_deleted'] = deleted_total
        cleanup_summary['affected_player_ids'] = list(affected_players)
        
        logger.info(f"Cleanup completed. Total deleted: {deleted_total} duplicate drops")
        logger.info(f"Affected {len(affected_players)} unique players")
        
        return cleanup_summary
        
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        if session:
            try:
                session.rollback()
            except:
                pass
        raise
    finally:
        if session:
            try:
                session.close()
            except:
                pass

def save_cleanup_results(results, filename_prefix="cleanup_results"):
    """Save cleanup results to JSON file"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{timestamp}.json"
    
    with open(filename, 'w') as f:
        json.dump(results, f, indent=2)
    
    logger.info(f"Results saved to {filename}")
    return filename

if __name__ == "__main__":
    # Uncomment the line you want to run:
    
    
    #Actual cleanup - only run this when you're ready to delete duplicates
    results = cleanup_duplicate_drops(dry_run=False, days_back=7)
    if results:
        save_cleanup_results(results, "actual_cleanup_results")