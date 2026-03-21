#!/usr/bin/env python3
"""
CLI script for generating timeframe-based loot boards.

This script is called by the API endpoint as a subprocess to avoid
blocking the main event loop during CPU-intensive image generation.

Usage:
    python timeframe_board_cli.py --group-id 123 [options]

Options:
    --group-id      Required. The DropTracker group ID.
    --wom-group-id  Optional. Wise Old Man group ID.
    --start-time    Optional. Start datetime in ISO format (e.g., 2024-01-15T00:00:00).
    --end-time      Optional. End datetime in ISO format (e.g., 2024-01-15T23:59:59).
    --npc-id        Optional. Filter drops to a specific NPC.

Output:
    On success: Prints the image path to stdout
    On failure: Prints error to stderr and exits with non-zero code
"""

import argparse
import asyncio
import sys
from datetime import datetime
from typing import Optional


def parse_datetime(dt_str: str) -> Optional[datetime]:
    """Parse a datetime string in various ISO formats."""
    if not dt_str:
        return None
    
    dt_str = dt_str.strip()
    
    # Remove timezone info if present
    if '+' in dt_str:
        dt_str = dt_str.split('+')[0]
    if 'Z' in dt_str:
        dt_str = dt_str.replace('Z', '')
    
    # Try parsing with different formats
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    
    raise ValueError(f"Unable to parse datetime: {dt_str}")


async def generate_board(
    group_id: int,
    wom_group_id: int = 0,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    npc_id: Optional[int] = None
) -> str:
    """
    Generate a timeframe board and return the image path.
    
    Returns:
        str: Path to the generated image file
        
    Raises:
        Exception: If board generation fails
    """
    from lootboard import generator
    
    image_path = await generator.generate_timeframe_board(
        group_id=group_id,
        wom_group_id=wom_group_id,
        start_time=start_time,
        end_time=end_time,
        npc_id=npc_id
    )
    
    if not image_path:
        raise RuntimeError("Board generation returned no image path")
    
    return image_path


def main(argv: Optional[list[str]] = None) -> int:
    """
    Main entry point for the CLI.
    
    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    parser = argparse.ArgumentParser(
        description="Generate a timeframe-based loot board."
    )
    parser.add_argument(
        "--group-id",
        type=int,
        required=True,
        help="Target group ID for the board."
    )
    parser.add_argument(
        "--wom-group-id",
        type=int,
        default=0,
        help="Wise Old Man group ID (optional)."
    )
    parser.add_argument(
        "--start-time",
        type=str,
        default=None,
        help="Start datetime in ISO format (e.g., 2024-01-15T00:00:00)."
    )
    parser.add_argument(
        "--end-time",
        type=str,
        default=None,
        help="End datetime in ISO format (e.g., 2024-01-15T23:59:59)."
    )
    parser.add_argument(
        "--npc-id",
        type=int,
        default=None,
        help="Filter drops to a specific NPC ID."
    )
    
    args = parser.parse_args(argv)
    
    try:
        # Parse datetime strings
        start_time = None
        end_time = None
        
        if args.start_time:
            try:
                start_time = parse_datetime(args.start_time)
            except ValueError as e:
                print(f"Error parsing start-time: {e}", file=sys.stderr)
                return 1
        
        if args.end_time:
            try:
                end_time = parse_datetime(args.end_time)
            except ValueError as e:
                print(f"Error parsing end-time: {e}", file=sys.stderr)
                return 1
        
        # Handle npc_id
        npc_id = args.npc_id
        if npc_id is not None and npc_id <= 0:
            npc_id = None
        
        # Run the async board generation
        image_path = asyncio.run(generate_board(
            group_id=args.group_id,
            wom_group_id=args.wom_group_id,
            start_time=start_time,
            end_time=end_time,
            npc_id=npc_id
        ))
        
        # Output the image path to stdout for the parent process
        print(image_path)
        return 0
        
    except Exception as e:
        print(f"Error generating board: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

