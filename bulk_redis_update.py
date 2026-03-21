#!/usr/bin/env python3
"""
Bulk Redis Update Script

This script provides a command-line interface for performing bulk Redis updates
on players who have received drops in the current calendar month.

Usage:
    python bulk_redis_update.py [--dry-run] [--batch-size N] [--players ID1,ID2,...]

Examples:
    # Update all players with current month drops
    python bulk_redis_update.py
    
    # Dry run to see how many players would be updated
    python bulk_redis_update.py --dry-run
    
    # Update specific players
    python bulk_redis_update.py --players 795,123,456
    
    # Use custom batch size
    python bulk_redis_update.py --batch-size 25
"""

import argparse
import sys
from datetime import datetime
from services.redis_updates import BulkRedisUpdater
from db.models.base import get_fresh_session


def progress_callback(progress):
    """Progress callback function for bulk updates"""
    percent = (progress['completed_players'] / progress['total_players']) * 100
    print(f"Progress: {progress['completed_players']}/{progress['total_players']} "
          f"({percent:.1f}%) - Batch {progress['batch']}/{progress['total_batches']} "
          f"- Success: {progress['successful']}, Failed: {progress['failed']}")


def main():
    parser = argparse.ArgumentParser(
        description="Bulk Redis update for DropTracker players",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        '--dry-run', 
        action='store_true',
        help='Show how many players would be updated without actually updating'
    )
    
    parser.add_argument(
        '--batch-size',
        type=int,
        default=50,
        help='Number of players to process in each batch (default: 50)'
    )
    
    parser.add_argument(
        '--fetch-chunk-size',
        type=int,
        default=50000,
        help='Number of player IDs to fetch per DB chunk when scanning current month drops (default: 50000)'
    )
    
    parser.add_argument(
        '--players',
        type=str,
        help='Comma-separated list of specific player IDs to update'
    )
    
    args = parser.parse_args()
    
    if args.fetch_chunk_size <= 0:
        parser.error("--fetch-chunk-size must be greater than zero")
    
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than zero")
    
    # Create a fresh database session only for dry-run queries
    session = None
    if args.dry_run:
        session = get_fresh_session()
    
    bulk_updater = BulkRedisUpdater(
        batch_size=args.batch_size,
        player_fetch_chunk_size=args.fetch_chunk_size
    )
    
    try:
        if args.players:
            # Update specific players
            try:
                player_ids = [int(pid.strip()) for pid in args.players.split(',')]
            except ValueError:
                print("Error: Invalid player IDs. Please provide comma-separated integers.")
                return 1
            
            if args.dry_run:
                print(f"DRY RUN: Would update {len(player_ids)} specific players:")
                for pid in player_ids:
                    print(f"  - Player ID: {pid}")
                return 0
            
            print(f"Starting bulk update for {len(player_ids)} specific players...")
            
            result = bulk_updater.force_update_specific_players(
                player_ids, 
                progress_callback=progress_callback
            )
            
        else:
            # Update all players with current month drops
            if args.dry_run:
                current_month_players = bulk_updater.get_players_with_current_month_drops(session)
                print(f"DRY RUN: Would update {len(current_month_players)} players with current month drops")
                print(f"Current month partition: {datetime.now().year * 100 + datetime.now().month}")
                return 0
            
            print(f"Starting bulk update for players with current month drops...")
            
            result = bulk_updater.force_update_all_current_month_players(
                progress_callback=progress_callback
            )
        
        # Print final results
        print("\n" + "="*60)
        print("BULK UPDATE SUMMARY")
        print("="*60)
        print(f"Started at: {result['started_at']}")
        print(f"Completed at: {result['completed_at']}")
        print(f"Duration: {result['duration_seconds']:.2f} seconds")
        print(f"Total players: {result['total_players']}")
        print(f"Successful updates: {result['successful_updates']}")
        print(f"Failed updates: {result['failed_updates']}")
        
        if result['errors']:
            print(f"\nErrors ({len(result['errors'])}):")
            for i, error in enumerate(result['errors'][:10], 1):
                print(f"  {i}. {error}")
            if len(result['errors']) > 10:
                print(f"  ... and {len(result['errors']) - 10} more errors")
        
        success_rate = (result['successful_updates'] / result['total_players']) * 100 if result['total_players'] > 0 else 0
        print(f"\nSuccess rate: {success_rate:.1f}%")
        
        # Return appropriate exit code
        return 0 if result['failed_updates'] == 0 else 1
        
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        return 1
    except Exception as e:
        print(f"Fatal error: {e}")
        return 1
    finally:
        if session:
            session.close()


if __name__ == "__main__":
    sys.exit(main())
