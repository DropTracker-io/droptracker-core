#!/usr/bin/env python3
"""
Submission Manager CLI

A standalone command-line tool for managing DropTracker submissions.
Allows re-processing existing entries to queue notifications, and
creating new entries manually.

Usage:
    python scripts/submission_manager.py

Author: DropTracker Team
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

# Database imports
from db import (
    session, Session,
    Player, Drop, CollectionLogEntry, PersonalBestEntry, CombatAchievementEntry,
    NpcList, ItemList, Group, GroupConfiguration, NotificationQueue
)
from sqlalchemy import text
from sqlalchemy.orm import joinedload


class Colors:
    """ANSI color codes for terminal output"""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_header(text: str):
    """Print a styled header"""
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{text:^60}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}\n")


def print_success(text: str):
    """Print success message"""
    print(f"{Colors.GREEN}✓ {text}{Colors.ENDC}")


def print_error(text: str):
    """Print error message"""
    print(f"{Colors.RED}✗ {text}{Colors.ENDC}")


def print_info(text: str):
    """Print info message"""
    print(f"{Colors.CYAN}ℹ {text}{Colors.ENDC}")


def print_warning(text: str):
    """Print warning message"""
    print(f"{Colors.YELLOW}⚠ {text}{Colors.ENDC}")


def get_input(prompt: str, default: str = None) -> str:
    """Get user input with optional default value"""
    if default:
        result = input(f"{Colors.BLUE}{prompt} [{default}]: {Colors.ENDC}").strip()
        return result if result else default
    return input(f"{Colors.BLUE}{prompt}: {Colors.ENDC}").strip()


def get_int_input(prompt: str, default: int = None) -> Optional[int]:
    """Get integer input with optional default"""
    try:
        value = get_input(prompt, str(default) if default else None)
        return int(value) if value else default
    except ValueError:
        print_error("Invalid number")
        return None


def confirm(prompt: str, default: bool = True) -> bool:
    """Get yes/no confirmation"""
    default_str = "Y/n" if default else "y/N"
    result = get_input(f"{prompt} ({default_str})", "").lower()
    if not result:
        return default
    return result in ('y', 'yes')


def get_player_groups(db_session, player: Player) -> List[Group]:
    """Get all groups a player belongs to"""
    player_gids = db_session.execute(
        text("SELECT group_id FROM user_group_association WHERE player_id = :player_id"),
        {"player_id": player.player_id}
    ).all()
    
    groups = []
    for gid in player_gids:
        group = db_session.query(Group).filter(Group.group_id == gid[0]).first()
        if group:
            groups.append(group)
    
    # Ensure global group is included
    global_group = db_session.query(Group).filter(Group.group_id == 2).first()
    if global_group and global_group not in groups:
        groups.append(global_group)
    
    return groups


class SubmissionManager:
    """Main CLI application for managing submissions"""
    
    def __init__(self):
        self.running = True
    
    def run(self):
        """Main application loop"""
        print_header("DropTracker Submission Manager")
        
        while self.running:
            self.show_main_menu()
    
    def show_main_menu(self):
        """Display main menu and handle selection"""
        print(f"\n{Colors.BOLD}Main Menu:{Colors.ENDC}")
        print("  1. Re-process existing entries (queue notifications)")
        print("  2. Create new Drop entry")
        print("  3. Create new Collection Log entry")
        print("  4. Create new Personal Best entry")
        print("  5. Create new Combat Achievement entry")
        print("  6. Lookup entries by player")
        print("  7. Exit")
        
        choice = get_input("\nSelect option (1-7)")
        
        if choice == "1":
            self.reprocess_menu()
        elif choice == "2":
            self.create_drop()
        elif choice == "3":
            self.create_clog()
        elif choice == "4":
            self.create_pb()
        elif choice == "5":
            self.create_ca()
        elif choice == "6":
            self.lookup_entries()
        elif choice == "7":
            self.running = False
            print_info("Goodbye!")
        else:
            print_error("Invalid option")
    
    # ==================== RE-PROCESS ====================
    
    def reprocess_menu(self):
        """Re-process existing entries to queue notifications"""
        print_header("Re-Process Entries")
        
        print("Entry types:")
        print("  1. Drop")
        print("  2. Collection Log")
        print("  3. Personal Best")
        print("  4. Combat Achievement")
        
        type_choice = get_input("Select type (1-4)")
        type_map = {"1": "drop", "2": "clog", "3": "pb", "4": "ca"}
        
        if type_choice not in type_map:
            print_error("Invalid type")
            return
        
        entry_type = type_map[type_choice]
        
        ids_str = get_input("Enter entry IDs (comma-separated)")
        if not ids_str:
            print_error("No IDs provided")
            return
        
        try:
            ids = [int(id.strip()) for id in ids_str.split(',') if id.strip()]
        except ValueError:
            print_error("Invalid ID format")
            return
        
        include_global = confirm("Include global group (ID 2)?", True)
        
        print_info(f"Re-processing {len(ids)} {entry_type} entries...")
        
        asyncio.run(self._reprocess_entries(entry_type, ids, include_global))
    
    async def _reprocess_entries(self, entry_type: str, ids: List[int], include_global: bool):
        """Async implementation of entry re-processing"""
        db_session = Session()
        
        try:
            for entry_id in ids:
                await self._reprocess_single_entry(db_session, entry_type, entry_id, include_global)
            db_session.commit()
            print_success("Re-processing complete!")
        except Exception as e:
            db_session.rollback()
            print_error(f"Error: {str(e)}")
        finally:
            db_session.close()
    
    async def _reprocess_single_entry(self, db_session, entry_type: str, entry_id: int, include_global: bool):
        """Re-process a single entry"""
        # Fetch the entry based on type
        if entry_type == "drop":
            entry = db_session.query(Drop).options(
                joinedload(Drop.player)
            ).filter(Drop.drop_id == entry_id).first()
            if not entry:
                print_warning(f"Drop ID {entry_id} not found")
                return
            player = entry.player
            notification_type = "drop"
            
            item = db_session.query(ItemList).filter(ItemList.item_id == entry.item_id).first()
            npc = db_session.query(NpcList).filter(NpcList.npc_id == entry.npc_id).first()
            
            notification_data = {
                "drop_id": entry.drop_id,
                "item_name": item.item_name if item else f"Item {entry.item_id}",
                "npc_name": npc.npc_name if npc else f"NPC {entry.npc_id}",
                "value": entry.value,
                "quantity": entry.quantity,
                "total_value": entry.value * entry.quantity,
                "player_name": player.player_name,
                "player_id": player.player_id,
                "image_url": entry.image_url,
            }
            details = f"{entry.quantity}x {notification_data['item_name']} from {notification_data['npc_name']}"
            
        elif entry_type == "clog":
            entry = db_session.query(CollectionLogEntry).options(
                joinedload(CollectionLogEntry.player)
            ).filter(CollectionLogEntry.log_id == entry_id).first()
            if not entry:
                print_warning(f"Collection Log ID {entry_id} not found")
                return
            player = entry.player
            notification_type = "clog"
            
            item = db_session.query(ItemList).filter(ItemList.item_id == entry.item_id).first()
            npc = db_session.query(NpcList).filter(NpcList.npc_id == entry.npc_id).first()
            
            notification_data = {
                "clog_id": entry.log_id,
                "item_name": item.item_name if item else f"Item {entry.item_id}",
                "npc_name": npc.npc_name if npc else f"NPC {entry.npc_id}",
                "player_name": player.player_name,
                "player_id": player.player_id,
                "image_url": entry.image_url,
                "reported_slots": entry.reported_slots,
            }
            details = f"{notification_data['item_name']} from {notification_data['npc_name']}"
            
        elif entry_type == "pb":
            entry = db_session.query(PersonalBestEntry).options(
                joinedload(PersonalBestEntry.player)
            ).filter(PersonalBestEntry.id == entry_id).first()
            if not entry:
                print_warning(f"Personal Best ID {entry_id} not found")
                return
            player = entry.player
            notification_type = "pb"
            
            npc = db_session.query(NpcList).filter(NpcList.npc_id == entry.npc_id).first()
            time_str = f"{entry.kill_time // 60000}:{(entry.kill_time // 1000) % 60:02d}.{entry.kill_time % 1000 // 10:02d}"
            
            notification_data = {
                "pb_id": entry.id,
                "player_name": player.player_name,
                "player_id": player.player_id,
                "npc_id": entry.npc_id,
                "boss_name": npc.npc_name if npc else f"NPC {entry.npc_id}",
                "time_ms": entry.kill_time,
                "personal_best": entry.personal_best,
                "team_size": entry.team_size,
                "image_url": entry.image_url,
            }
            details = f"{time_str} at {notification_data['boss_name']}"
            
        elif entry_type == "ca":
            entry = db_session.query(CombatAchievementEntry).options(
                joinedload(CombatAchievementEntry.player)
            ).filter(CombatAchievementEntry.id == entry_id).first()
            if not entry:
                print_warning(f"Combat Achievement ID {entry_id} not found")
                return
            player = entry.player
            notification_type = "ca"
            
            notification_data = {
                "ca_id": entry.id,
                "player_name": player.player_name,
                "player_id": player.player_id,
                "task_name": entry.task_name,
                "image_url": entry.image_url,
            }
            details = entry.task_name
        else:
            print_error(f"Unknown entry type: {entry_type}")
            return
        
        print_info(f"Found {entry_type} ID {entry_id}: {details} for {player.player_name}")
        
        # Get player groups
        player_groups = get_player_groups(db_session, player)
        
        if not include_global:
            player_groups = [g for g in player_groups if g.group_id != 2]
        
        print(f"  Player is in {len(player_groups)} groups: {[g.group_name for g in player_groups]}")
        
        # Create notifications for each group
        notifications_created = 0
        for group in player_groups:
            notification = NotificationQueue(
                notification_type=notification_type,
                player_id=player.player_id,
                data=json.dumps(notification_data),
                group_id=group.group_id,
                status="pending"
            )
            db_session.add(notification)
            notifications_created += 1
            print(f"    → Created notification for: {group.group_name} (ID: {group.group_id})")
        
        print_success(f"  Created {notifications_created} notifications")
    
    # ==================== CREATE DROP ====================
    
    def create_drop(self):
        """Create a new drop entry"""
        print_header("Create Drop Entry")
        
        player_name = get_input("Player name")
        if not player_name:
            print_error("Player name required")
            return
        
        item_input = get_input("Item name or ID")
        if not item_input:
            print_error("Item required")
            return
        
        npc_input = get_input("NPC name or ID")
        if not npc_input:
            print_error("NPC required")
            return
        
        value = get_int_input("Value (GP)")
        if not value:
            print_error("Value required")
            return
        
        quantity = get_int_input("Quantity", 1)
        image_url = get_input("Image URL (optional)", "") or None
        create_notifications = confirm("Create notifications?", True)
        
        asyncio.run(self._create_drop(
            player_name, item_input, npc_input, value, quantity, image_url, create_notifications
        ))
    
    async def _create_drop(self, player_name: str, item_input: str, 
                           npc_input: str, value: int, quantity: int,
                           image_url: Optional[str], create_notifications: bool):
        """Async implementation of drop creation"""
        db_session = Session()
        
        try:
            # Find player
            player = db_session.query(Player).filter(
                Player.player_name.ilike(player_name)
            ).first()
            if not player:
                print_error(f"Player '{player_name}' not found")
                return
            
            # Find item
            if item_input.isdigit():
                item = db_session.query(ItemList).filter(ItemList.item_id == int(item_input)).first()
            else:
                item = db_session.query(ItemList).filter(ItemList.item_name.ilike(item_input)).first()
            if not item:
                print_error(f"Item '{item_input}' not found")
                return
            
            # Find NPC
            if npc_input.isdigit():
                npc = db_session.query(NpcList).filter(NpcList.npc_id == int(npc_input)).first()
            else:
                npc = db_session.query(NpcList).filter(NpcList.npc_name.ilike(npc_input)).first()
            if not npc:
                print_error(f"NPC '{npc_input}' not found")
                return
            
            # Create drop
            from db.models.drop import get_current_partition
            drop = Drop(
                item_id=item.item_id,
                player_id=player.player_id,
                npc_id=npc.npc_id,
                value=value,
                quantity=quantity,
                image_url=image_url,
                authed=True,
                used_api=False,
                partition=get_current_partition()
            )
            db_session.add(drop)
            db_session.flush()
            
            print_success(f"Created drop ID {drop.drop_id}: {quantity}x {item.item_name} from {npc.npc_name}")
            
            if create_notifications:
                notification_data = {
                    "drop_id": drop.drop_id,
                    "item_name": item.item_name,
                    "npc_name": npc.npc_name,
                    "value": value,
                    "quantity": quantity,
                    "total_value": value * quantity,
                    "player_name": player.player_name,
                    "player_id": player.player_id,
                    "image_url": image_url,
                }
                
                groups = get_player_groups(db_session, player)
                for group in groups:
                    notification = NotificationQueue(
                        notification_type="drop",
                        player_id=player.player_id,
                        data=json.dumps(notification_data),
                        group_id=group.group_id,
                        status="pending"
                    )
                    db_session.add(notification)
                    print(f"  → Notification queued for: {group.group_name}")
            
            db_session.commit()
            print_success("Drop creation complete!")
            
        except Exception as e:
            db_session.rollback()
            print_error(f"Error: {str(e)}")
        finally:
            db_session.close()
    
    # ==================== CREATE CLOG ====================
    
    def create_clog(self):
        """Create a new collection log entry"""
        print_header("Create Collection Log Entry")
        
        player_name = get_input("Player name")
        if not player_name:
            print_error("Player name required")
            return
        
        item_input = get_input("Item name or ID")
        if not item_input:
            print_error("Item required")
            return
        
        npc_input = get_input("NPC/Source name or ID")
        if not npc_input:
            print_error("NPC/Source required")
            return
        
        slots = get_int_input("Reported slots (optional)", None)
        image_url = get_input("Image URL (optional)", "") or None
        create_notifications = confirm("Create notifications?", True)
        
        asyncio.run(self._create_clog(
            player_name, item_input, npc_input, slots, image_url, create_notifications
        ))
    
    async def _create_clog(self, player_name: str, item_input: str,
                           npc_input: str, slots: Optional[int],
                           image_url: Optional[str], create_notifications: bool):
        """Async implementation of clog creation"""
        db_session = Session()
        
        try:
            player = db_session.query(Player).filter(
                Player.player_name.ilike(player_name)
            ).first()
            if not player:
                print_error(f"Player '{player_name}' not found")
                return
            
            if item_input.isdigit():
                item = db_session.query(ItemList).filter(ItemList.item_id == int(item_input)).first()
            else:
                item = db_session.query(ItemList).filter(ItemList.item_name.ilike(item_input)).first()
            if not item:
                print_error(f"Item '{item_input}' not found")
                return
            
            if npc_input.isdigit():
                npc = db_session.query(NpcList).filter(NpcList.npc_id == int(npc_input)).first()
            else:
                npc = db_session.query(NpcList).filter(NpcList.npc_name.ilike(npc_input)).first()
            if not npc:
                print_error(f"NPC '{npc_input}' not found")
                return
            
            clog = CollectionLogEntry(
                item_id=item.item_id,
                player_id=player.player_id,
                npc_id=npc.npc_id,
                reported_slots=slots,
                image_url=image_url,
                used_api=False
            )
            db_session.add(clog)
            db_session.flush()
            
            print_success(f"Created clog ID {clog.log_id}: {item.item_name} from {npc.npc_name}")
            
            if create_notifications:
                notification_data = {
                    "clog_id": clog.log_id,
                    "item_name": item.item_name,
                    "npc_name": npc.npc_name,
                    "player_name": player.player_name,
                    "player_id": player.player_id,
                    "image_url": image_url,
                    "reported_slots": slots,
                }
                
                groups = get_player_groups(db_session, player)
                for group in groups:
                    notification = NotificationQueue(
                        notification_type="clog",
                        player_id=player.player_id,
                        data=json.dumps(notification_data),
                        group_id=group.group_id,
                        status="pending"
                    )
                    db_session.add(notification)
                    print(f"  → Notification queued for: {group.group_name}")
            
            db_session.commit()
            print_success("Collection log creation complete!")
            
        except Exception as e:
            db_session.rollback()
            print_error(f"Error: {str(e)}")
        finally:
            db_session.close()
    
    # ==================== CREATE PB ====================
    
    def create_pb(self):
        """Create a new personal best entry"""
        print_header("Create Personal Best Entry")
        
        player_name = get_input("Player name")
        if not player_name:
            print_error("Player name required")
            return
        
        npc_input = get_input("Boss name or NPC ID")
        if not npc_input:
            print_error("Boss required")
            return
        
        kill_time = get_int_input("Kill time (milliseconds)")
        if not kill_time:
            print_error("Kill time required")
            return
        
        personal_best = get_int_input("Personal best time (milliseconds)")
        if not personal_best:
            print_error("Personal best required")
            return
        
        print("Team sizes: Solo, Duo, Trio, 4-man, 5-man, 6-man, 7-man, 8-man")
        team_size = get_input("Team size", "Solo")
        image_url = get_input("Image URL (optional)", "") or None
        is_new_pb = confirm("Is this a new personal best?", True)
        create_notifications = confirm("Create notifications?", True)
        
        asyncio.run(self._create_pb(
            player_name, npc_input, kill_time, personal_best, team_size,
            image_url, is_new_pb, create_notifications
        ))
    
    async def _create_pb(self, player_name: str, npc_input: str,
                         kill_time: int, personal_best: int,
                         team_size: str, image_url: Optional[str],
                         is_new_pb: bool, create_notifications: bool):
        """Async implementation of PB creation"""
        db_session = Session()
        
        try:
            player = db_session.query(Player).filter(
                Player.player_name.ilike(player_name)
            ).first()
            if not player:
                print_error(f"Player '{player_name}' not found")
                return
            
            if npc_input.isdigit():
                npc = db_session.query(NpcList).filter(NpcList.npc_id == int(npc_input)).first()
            else:
                npc = db_session.query(NpcList).filter(NpcList.npc_name.ilike(npc_input)).first()
            if not npc:
                print_error(f"NPC '{npc_input}' not found")
                return
            
            pb = PersonalBestEntry(
                player_id=player.player_id,
                npc_id=npc.npc_id,
                kill_time=kill_time,
                personal_best=personal_best,
                team_size=team_size,
                new_pb=is_new_pb,
                image_url=image_url,
                used_api=False
            )
            db_session.add(pb)
            db_session.flush()
            
            time_str = f"{kill_time // 60000}:{(kill_time // 1000) % 60:02d}.{kill_time % 1000 // 10:02d}"
            print_success(f"Created PB ID {pb.id}: {time_str} at {npc.npc_name} ({team_size})")
            
            if create_notifications:
                notification_data = {
                    "pb_id": pb.id,
                    "player_name": player.player_name,
                    "player_id": player.player_id,
                    "npc_id": npc.npc_id,
                    "boss_name": npc.npc_name,
                    "time_ms": kill_time,
                    "personal_best": personal_best,
                    "team_size": team_size,
                    "image_url": image_url,
                }
                
                groups = get_player_groups(db_session, player)
                for group in groups:
                    notification = NotificationQueue(
                        notification_type="pb",
                        player_id=player.player_id,
                        data=json.dumps(notification_data),
                        group_id=group.group_id,
                        status="pending"
                    )
                    db_session.add(notification)
                    print(f"  → Notification queued for: {group.group_name}")
            
            db_session.commit()
            print_success("Personal best creation complete!")
            
        except Exception as e:
            db_session.rollback()
            print_error(f"Error: {str(e)}")
        finally:
            db_session.close()
    
    # ==================== CREATE CA ====================
    
    def create_ca(self):
        """Create a new combat achievement entry"""
        print_header("Create Combat Achievement Entry")
        
        player_name = get_input("Player name")
        if not player_name:
            print_error("Player name required")
            return
        
        task_name = get_input("Task name")
        if not task_name:
            print_error("Task name required")
            return
        
        image_url = get_input("Image URL (optional)", "") or None
        create_notifications = confirm("Create notifications?", True)
        
        asyncio.run(self._create_ca(
            player_name, task_name, image_url, create_notifications
        ))
    
    async def _create_ca(self, player_name: str, task_name: str,
                         image_url: Optional[str], create_notifications: bool):
        """Async implementation of CA creation"""
        db_session = Session()
        
        try:
            player = db_session.query(Player).filter(
                Player.player_name.ilike(player_name)
            ).first()
            if not player:
                print_error(f"Player '{player_name}' not found")
                return
            
            ca = CombatAchievementEntry(
                player_id=player.player_id,
                task_name=task_name,
                image_url=image_url,
                used_api=False
            )
            db_session.add(ca)
            db_session.flush()
            
            print_success(f"Created CA ID {ca.id}: '{task_name}'")
            
            if create_notifications:
                notification_data = {
                    "ca_id": ca.id,
                    "player_name": player.player_name,
                    "player_id": player.player_id,
                    "task_name": task_name,
                    "image_url": image_url,
                }
                
                groups = get_player_groups(db_session, player)
                for group in groups:
                    notification = NotificationQueue(
                        notification_type="ca",
                        player_id=player.player_id,
                        data=json.dumps(notification_data),
                        group_id=group.group_id,
                        status="pending"
                    )
                    db_session.add(notification)
                    print(f"  → Notification queued for: {group.group_name}")
            
            db_session.commit()
            print_success("Combat achievement creation complete!")
            
        except Exception as e:
            db_session.rollback()
            print_error(f"Error: {str(e)}")
        finally:
            db_session.close()
    
    # ==================== LOOKUP ====================
    
    def lookup_entries(self):
        """Look up entries for a player"""
        print_header("Lookup Entries")
        
        player_name = get_input("Player name")
        if not player_name:
            print_error("Player name required")
            return
        
        print("Entry types: 1=Drop, 2=CLog, 3=PB, 4=CA")
        type_choice = get_input("Select type (1-4)", "1")
        type_map = {"1": "drop", "2": "clog", "3": "pb", "4": "ca"}
        
        if type_choice not in type_map:
            print_error("Invalid type")
            return
        
        entry_type = type_map[type_choice]
        limit = get_int_input("Limit", 20)
        
        db_session = Session()
        
        try:
            player = db_session.query(Player).filter(
                Player.player_name.ilike(player_name)
            ).first()
            
            if not player:
                print_error(f"Player '{player_name}' not found")
                return
            
            print(f"\n{Colors.BOLD}Results for {player.player_name}:{Colors.ENDC}\n")
            print(f"{'ID':<10} {'Details':<50} {'Date':<20} {'Notified':<10}")
            print("-" * 90)
            
            if entry_type == "drop":
                entries = db_session.query(Drop).filter(
                    Drop.player_id == player.player_id
                ).order_by(Drop.date_added.desc()).limit(limit).all()
                
                for entry in entries:
                    item = db_session.query(ItemList).filter(ItemList.item_id == entry.item_id).first()
                    npc = db_session.query(NpcList).filter(NpcList.npc_id == entry.npc_id).first()
                    has_notif = len(entry.notified_drops) > 0
                    
                    details = f"{entry.quantity}x {item.item_name if item else 'Unknown'} from {npc.npc_name if npc else 'Unknown'}"
                    date_str = entry.date_added.strftime("%Y-%m-%d %H:%M") if entry.date_added else "N/A"
                    
                    print(f"{entry.drop_id:<10} {details[:50]:<50} {date_str:<20} {'Yes' if has_notif else 'No':<10}")
                    
            elif entry_type == "clog":
                entries = db_session.query(CollectionLogEntry).filter(
                    CollectionLogEntry.player_id == player.player_id
                ).order_by(CollectionLogEntry.date_added.desc()).limit(limit).all()
                
                for entry in entries:
                    item = db_session.query(ItemList).filter(ItemList.item_id == entry.item_id).first()
                    npc = db_session.query(NpcList).filter(NpcList.npc_id == entry.npc_id).first()
                    has_notif = len(entry.notified_clog) > 0
                    
                    details = f"{item.item_name if item else 'Unknown'} from {npc.npc_name if npc else 'Unknown'}"
                    date_str = entry.date_added.strftime("%Y-%m-%d %H:%M") if entry.date_added else "N/A"
                    
                    print(f"{entry.log_id:<10} {details[:50]:<50} {date_str:<20} {'Yes' if has_notif else 'No':<10}")
                    
            elif entry_type == "pb":
                entries = db_session.query(PersonalBestEntry).filter(
                    PersonalBestEntry.player_id == player.player_id
                ).order_by(PersonalBestEntry.date_added.desc()).limit(limit).all()
                
                for entry in entries:
                    npc = db_session.query(NpcList).filter(NpcList.npc_id == entry.npc_id).first()
                    has_notif = len(entry.notified_pb) > 0
                    time_str = f"{entry.kill_time // 60000}:{(entry.kill_time // 1000) % 60:02d}"
                    
                    details = f"{time_str} at {npc.npc_name if npc else 'Unknown'} ({entry.team_size})"
                    date_str = entry.date_added.strftime("%Y-%m-%d %H:%M") if entry.date_added else "N/A"
                    
                    print(f"{entry.id:<10} {details[:50]:<50} {date_str:<20} {'Yes' if has_notif else 'No':<10}")
                    
            elif entry_type == "ca":
                entries = db_session.query(CombatAchievementEntry).filter(
                    CombatAchievementEntry.player_id == player.player_id
                ).order_by(CombatAchievementEntry.date_added.desc()).limit(limit).all()
                
                for entry in entries:
                    has_notif = len(entry.notified_ca) > 0
                    date_str = entry.date_added.strftime("%Y-%m-%d %H:%M") if entry.date_added else "N/A"
                    
                    print(f"{entry.id:<10} {entry.task_name[:50]:<50} {date_str:<20} {'Yes' if has_notif else 'No':<10}")
            
            print(f"\n{Colors.CYAN}Found {len(entries)} entries{Colors.ENDC}")
            
        except Exception as e:
            print_error(f"Error: {str(e)}")
        finally:
            db_session.close()


def main():
    """Main entry point"""
    try:
        manager = SubmissionManager()
        manager.run()
    except KeyboardInterrupt:
        print("\n")
        print_info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
