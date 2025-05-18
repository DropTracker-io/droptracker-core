### Class to help determine if a group or player has climbed a rank and whether a notification should be sent as a result if so

from datetime import datetime
from typing import List, Dict
import time
from functools import lru_cache
from sqlalchemy import text
from db.models import Drop, Player, Group, session
from utils.ranking.npc_ranker import NPCRankChecker
from utils.wiseoldman import fetch_group_members # Assuming these functions exist

dev = True

# Cache for rankings data
class RankingsCache:
    def __init__(self, refresh_interval=300):  # 5 minutes default refresh interval
        self.refresh_interval = refresh_interval
        self.last_refresh = 0
        self.player_rankings = {}  # {player_id: rank}
        self.group_rankings = {}   # {group_id: rank}
        self.group_player_rankings = {}  # {group_id: {player_id: rank}}
        self.refresh()
    
    def refresh(self):
        """Refresh all cached ranking data"""
        current_time = time.time()
        if current_time - self.last_refresh < self.refresh_interval:
            return  # Skip refresh if not enough time has passed
        
        
        # Get all players and their totals directly from Redis/DB
        players = session.query(Player).all()
        player_totals = {}
        for player in players:
            try:
                # Get the latest total from Redis
                player_totals[player.player_id] = player.get_current_total()
            except Exception as e:
                print(f"Error getting player total for player {player.player_id}: {e}")
        
        # Calculate player rankings
        sorted_players = sorted(player_totals.items(), key=lambda x: x[1], reverse=True)
        self.player_rankings = {pid: i+1 for i, (pid, _) in enumerate(sorted_players)}
        
        # Get all groups and their totals directly from Redis/DB
        groups = session.query(Group).all()
        group_totals = {}
        for group in groups:
            if group.group_id == 2:  # Skip specific group
                continue
            try:
                # Get the latest total from Redis
                group_totals[group.group_id] = group.get_current_total()
            except Exception as e:
                print(f"Error getting group total for group {group.group_id}: {e}")
        
        # Calculate group rankings
        sorted_groups = sorted(group_totals.items(), key=lambda x: x[1], reverse=True)
        self.group_rankings = {gid: i+1 for i, (gid, _) in enumerate(sorted_groups)}
        
        # Calculate player rankings within each group
        self.group_player_rankings = {}
        
        for group_id in group_totals.keys():
            # Get players in this group
            player_ids_query = """SELECT player_id FROM user_group_association WHERE group_id = :group_id"""
            player_ids_from_group = session.execute(text(player_ids_query), {"group_id": group_id}).fetchall()
            player_ids_from_group = [pid[0] for pid in player_ids_from_group if pid[0] is not None]
            
            # Get totals for players in this group
            group_player_totals = {}
            for pid in player_ids_from_group:
                if pid in player_totals:
                    group_player_totals[pid] = player_totals[pid]
            
            # Sort and calculate rankings
            sorted_group_players = sorted(group_player_totals.items(), key=lambda x: x[1], reverse=True)
            group_player_rankings = {pid: i+1 for i, (pid, _) in enumerate(sorted_group_players)}
            
            self.group_player_rankings[group_id] = group_player_rankings
        
        self.last_refresh = current_time

    def get_player_rank(self, player_id):
        """Get a player's current global rank"""
        self.refresh()
        return self.player_rankings.get(player_id, 0)
    
    def get_group_rank(self, group_id):
        """Get a group's current rank"""
        self.refresh()
        return self.group_rankings.get(group_id, 0)
    
    def get_player_rank_in_group(self, player_id, group_id):
        """Get a player's rank within a specific group"""
        self.refresh()
        if group_id in self.group_player_rankings:
            return self.group_player_rankings[group_id].get(player_id, 0)
        return 0
    
    def simulate_drop_effect(self, player_id, drop_value):
        """Simulate the effect of a drop on rankings"""
        self.refresh()
        
        # Get player's groups
        player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
        player_group_ids = session.execute(text(player_group_id_query), {"player_id": player_id}).fetchall()
        player_group_ids = [group_id[0] for group_id in player_group_ids if group_id[0] != 2]
        
        
        result = {
            "player_global": {},
            "player_in_group": {},
            "group": {}
        }
        
        # Get current player total from Redis
        player = session.query(Player).filter_by(player_id=player_id).first()
        original_total = player.get_current_total()
        original_rank = self.get_player_rank(player_id)
        
        # Get all player totals for simulation
        players = session.query(Player).all()
        player_totals = {}
        for p in players:
            try:
                player_totals[p.player_id] = p.get_current_total()
            except Exception as e:
                print(f"Error getting player total for player {p.player_id}: {e}")
        
        # Create a copy and modify for simulation
        simulated_player_totals = player_totals.copy()
        simulated_player_totals[player_id] = original_total + drop_value
        
        # Calculate new rank
        sorted_simulated_players = sorted(simulated_player_totals.items(), key=lambda x: x[1], reverse=True)
        new_rank = next((i+1 for i, (pid, _) in enumerate(sorted_simulated_players) if pid == player_id), 0)
        
        result["player_global"] = {
            "original_rank": original_rank,
            "original_total": original_total,
            "new_rank": new_rank,
            "new_total": simulated_player_totals[player_id],
            "rank_change": original_rank - new_rank,
            "improved": original_rank - new_rank > 0
        }
        
        # Process each group the player is in
        for group_id in player_group_ids:
            try:
                # Get players in this group
                player_ids_query = """SELECT player_id FROM user_group_association WHERE group_id = :group_id"""
                player_ids_from_group = session.execute(text(player_ids_query), {"group_id": group_id}).fetchall()
                player_ids_from_group = [pid[0] for pid in player_ids_from_group if pid[0] is not None]
                
                # Get totals for players in this group
                group_player_totals = {}
                for pid in player_ids_from_group:
                    if pid in player_totals:
                        group_player_totals[pid] = player_totals[pid]
                
                if not group_player_totals:
                    continue
                    
                if player_id not in group_player_totals:
                    continue
                    
                # Get player's current rank in this group
                group_sorted = sorted(group_player_totals.items(), key=lambda x: x[1], reverse=True)
                original_player_group_rank = next((i+1 for i, (pid, _) in enumerate(group_sorted) if pid == player_id), 0)
                
                # Create a copy and modify for simulation
                simulated_group_player_totals = group_player_totals.copy()
                simulated_group_player_totals[player_id] = original_total + drop_value
                
                # Calculate new player rank in group
                sorted_simulated_group_players = sorted(simulated_group_player_totals.items(), key=lambda x: x[1], reverse=True)
                new_player_group_rank = next((i+1 for i, (pid, _) in enumerate(sorted_simulated_group_players) if pid == player_id), 0)
                
                # Add player's rank in group to results
                result["player_in_group"][group_id] = {
                    "original_rank": original_player_group_rank,
                    "new_rank": new_player_group_rank,
                    "rank_change": original_player_group_rank - new_player_group_rank,
                    "improved": original_player_group_rank - new_player_group_rank > 0
                }
                
                # Now handle group-to-group comparison
                # Get current group total
                group = session.query(Group).filter_by(group_id=group_id).first()
                if not group:
                    continue
                    
                original_group_total = group.get_current_total()
                original_group_rank = self.get_group_rank(group_id)
                
                # Get all group totals for simulation
                all_groups_query = """SELECT DISTINCT group_id FROM user_group_association WHERE group_id != 2"""
                all_group_ids = [g[0] for g in session.execute(text(all_groups_query)).fetchall()]
                
                group_totals = {}
                for gid in all_group_ids:
                    g = session.query(Group).filter_by(group_id=gid).first()
                    if g:
                        try:
                            group_totals[gid] = g.get_current_total()
                        except Exception as e:
                            print(f"Error getting group total for group {gid}: {e}")
                
                # Create a copy and modify for simulation
                simulated_group_totals = group_totals.copy()
                simulated_group_totals[group_id] = original_group_total + drop_value
                
                # Calculate new group rank
                sorted_simulated_groups = sorted(simulated_group_totals.items(), key=lambda x: x[1], reverse=True)
                new_group_rank = next((i+1 for i, (gid, _) in enumerate(sorted_simulated_groups) if gid == group_id), 0)
                
                # Add group's global rank to results
                result["group"][group_id] = {
                    "original_rank": original_group_rank,
                    "original_total": original_group_total,
                    "new_rank": new_group_rank,
                    "new_total": simulated_group_totals[group_id],
                    "rank_change": original_group_rank - new_group_rank,
                    "improved": original_group_rank - new_group_rank > 0
                }
            except Exception as e:
                print(f"Error processing group {group_id}: {e}")
        
        # Ensure the result is properly returned with all data
        
        return result

    def force_refresh(self):
        """Force an immediate refresh of the cache regardless of the timer"""
        self.last_refresh = 0
        self.refresh()
        return True

# Create a global instance of the cache
rankings_cache = RankingsCache()
npc_ranker = NPCRankChecker()

async def check_rank_change_from_drop(player_id: int, drop_data: Drop, specific_group_id: int = None):
    """
    Check if a player or a group has managed to climb a rank due to a drop
    
    Args:
        player_id: The ID of the player to check.
        drop_data: A dictionary containing the drop data.
        specific_group_id: Optional specific group ID to check. If provided, only this group will be checked.

    Returns:
        A dictionary containing detailed rank information
    """
    try:
        drop_value = drop_data.value * drop_data.quantity
    except Exception as e:
        
        drop_value = 0
    
    # Use the cache to simulate the drop effect
    result = rankings_cache.simulate_drop_effect(player_id, drop_value)
    
    
    # If specific_group_id is provided, filter the results
    if specific_group_id is not None:
        # Check if the player is actually in this group
        player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id AND group_id = :group_id"""
        is_in_group = session.execute(
            text(player_group_id_query), 
            {"player_id": player_id, "group_id": specific_group_id}
        ).fetchone() is not None
        
        if not is_in_group:
            print(f"Warning: Player {player_id} is not in group {specific_group_id}")
            return None
        
        filtered_result = {
            "player_global": result["player_global"],
            "player_in_group": {},
            "group": {}
        }
        
        # Only include the specific group if the player is in it
        if is_in_group:
            # Get the player's groups
            player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
            player_group_ids = session.execute(text(player_group_id_query), {"player_id": player_id}).fetchall()
            player_group_ids = [group_id[0] for group_id in player_group_ids if group_id[0] != 2]
            
            # If the player is in this group, include it in the result
            if specific_group_id in player_group_ids:
                # We need to recalculate the player's rank in this specific group
                # This is similar to what's done in simulate_drop_effect
                try:
                    # Get players in this group
                    player_ids_query = """SELECT player_id FROM user_group_association WHERE group_id = :group_id"""
                    player_ids_from_group = session.execute(text(player_ids_query), {"group_id": specific_group_id}).fetchall()
                    player_ids_from_group = [pid[0] for pid in player_ids_from_group if pid[0] is not None]
                    
                    # Get player totals
                    player_totals = {}
                    for pid in player_ids_from_group:
                        p = session.query(Player).filter_by(player_id=pid).first()
                        if p:
                            try:
                                player_totals[pid] = p.get_current_total()
                            except Exception as e:
                                print(f"Error getting player total for player {pid}: {e}")
                    
                    # Get player's current rank in this group
                    group_sorted = sorted(player_totals.items(), key=lambda x: x[1], reverse=True)
                    original_player_group_rank = next((i+1 for i, (pid, _) in enumerate(group_sorted) if pid == player_id), 0)
                    
                    # Create a copy and modify for simulation
                    simulated_group_player_totals = player_totals.copy()
                    simulated_group_player_totals[player_id] = player_totals.get(player_id, 0) + drop_value
                    
                    # Calculate new player rank in group
                    sorted_simulated_group_players = sorted(simulated_group_player_totals.items(), key=lambda x: x[1], reverse=True)
                    new_player_group_rank = next((i+1 for i, (pid, _) in enumerate(sorted_simulated_group_players) if pid == player_id), 0)
                    
                    # Add player's rank in group to results
                    filtered_result["player_in_group"][specific_group_id] = {
                        "original_rank": original_player_group_rank,
                        "new_rank": new_player_group_rank,
                        "rank_change": original_player_group_rank - new_player_group_rank,
                        "improved": original_player_group_rank - new_player_group_rank > 0
                    }
                    
                    # Now handle group-to-group comparison
                    # Get current group total
                    group = session.query(Group).filter_by(group_id=specific_group_id).first()
                    if group:
                        original_group_total = group.get_current_total()
                        original_group_rank = rankings_cache.get_group_rank(specific_group_id)
                        
                        # Get all group totals for simulation
                        all_groups_query = """SELECT DISTINCT group_id FROM user_group_association WHERE group_id != 2"""
                        all_group_ids = [g[0] for g in session.execute(text(all_groups_query)).fetchall()]
                        
                        group_totals = {}
                        for gid in all_group_ids:
                            g = session.query(Group).filter_by(group_id=gid).first()
                            if g:
                                try:
                                    group_totals[gid] = g.get_current_total()
                                except Exception as e:
                                    print(f"Error getting group total for group {gid}: {e}")
                        
                        # Create a copy and modify for simulation
                        simulated_group_totals = group_totals.copy()
                        simulated_group_totals[specific_group_id] = original_group_total + drop_value
                        
                        # Calculate new group rank
                        sorted_simulated_groups = sorted(simulated_group_totals.items(), key=lambda x: x[1], reverse=True)
                        new_group_rank = next((i+1 for i, (gid, _) in enumerate(sorted_simulated_groups) if gid == specific_group_id), 0)
                        
                        # Add group's global rank to results
                        filtered_result["group"][specific_group_id] = {
                            "original_rank": original_group_rank,
                            "original_total": original_group_total,
                            "new_rank": new_group_rank,
                            "new_total": simulated_group_totals[specific_group_id],
                            "rank_change": original_group_rank - new_group_rank,
                            "improved": original_group_rank - new_group_rank > 0
                        }
                except Exception as e:
                    print(f"Error calculating group ranks for group {specific_group_id}: {e}")
        
        print(f"Filtered result for group {specific_group_id}: {filtered_result}")
        return filtered_result
    
    # Make sure we're returning a complete result with all sections
    complete_result = {
        "player_global": result.get("player_global", {}),
        "player_in_group": result.get("player_in_group", {}),
        "group": result.get("group", {})
    }
    
    print(f"Final result from check_rank_change_from_drop: {complete_result}")
    return complete_result

async def get_current_rank(player_id: int):
    """Get a player's current global rank using the cache"""
    return rankings_cache.get_player_rank(player_id)

# Force a refresh of the cache
def refresh_rankings_cache():
    """Force a refresh of the rankings cache"""
    return rankings_cache.force_refresh()

