from db.models import Drop, Player, Group, session
from utils.redis import RedisClient
from datetime import datetime, timedelta
from sqlalchemy.sql import text

redis_client = RedisClient()
class NPCRankChecker:
    def __init__(self):
        self.redis_client = redis_client
        self.session = session
    
    async def get_player_npc_totals(self, player_id, npc_id, start_time=None, end_time=None):
        """Get a player's total loot value for a specific NPC within a timeframe"""
        if start_time is None:
            # Default to current month instead of all-time
            current_date = datetime.now()
            current_partition = current_date.year * 100 + current_date.month
            
            npc_key = f"player:{player_id}:{current_partition}:npc_totals"
            npc_value = self.redis_client.client.hget(npc_key, str(npc_id))
            if npc_value:
                return int(npc_value.decode('utf-8'))
            return 0
        
        # Generate time partitions based on start and end time
        time_diff = end_time - start_time
        
        if time_diff.days > 30:
            granularity = "monthly"
        elif time_diff.days > 1:
            granularity = "daily"
        elif time_diff.seconds > 3600:
            granularity = "hourly"
        else:
            granularity = "minute"
        
        time_partitions = generate_time_partitions(start_time, end_time, granularity)
        
        # Determine Redis key prefix based on granularity
        if granularity == "monthly":
            prefix = ""  # Monthly has no prefix
        elif granularity == "daily":
            prefix = "daily"
        elif granularity == "hourly":
            prefix = "hourly"
        else:
            prefix = "minute"
        
        total_value = 0
        
        for partition in time_partitions:
            if prefix:
                npc_key = f"player:{player_id}:{prefix}:{partition}:npcs"
            else:
                npc_key = f"player:{player_id}:{partition}:npc_totals"
            
            npc_value = self.redis_client.client.hget(npc_key, str(npc_id))
            if npc_value:
                total_value += int(npc_value.decode('utf-8'))
        
        return total_value
    
    async def get_all_players_npc_totals(self, npc_id, start_time=None, end_time=None):
        """Get all players' total loot values for a specific NPC within a timeframe"""
        all_players = self.session.query(Player).all()
        player_totals = {}
        
        for player in all_players:
            player_total = await self.get_player_npc_totals(player.player_id, npc_id, start_time, end_time)
            if player_total > 0:
                player_totals[player.player_id] = player_total
        
        return player_totals
    
    async def get_group_players_npc_totals(self, group_id, npc_id, start_time=None, end_time=None):
        """Get all players' total loot values for a specific NPC within a group"""
        group = self.session.query(Group).filter(Group.group_id == group_id).first()
        if not group:
            return {}
        
        player_totals = {}
        
        for player in group.players:
            player_total = await self.get_player_npc_totals(player.player_id, npc_id, start_time, end_time)
            if player_total > 0:
                player_totals[player.player_id] = player_total
        
        return player_totals
    
    async def get_player_npc_rank(self, player_id, npc_id, start_time=None, end_time=None, group_id=None):
        """
        Get a player's rank for a specific NPC, both globally and within their group
        
        Returns a tuple: (global_rank, total_players, group_rank, total_group_players, player_total)
        """
        player_total = await self.get_player_npc_totals(player_id, npc_id, start_time, end_time)
        
        if player_total == 0:
            return (None, 0, None, 0, 0)
        
        # Get global rankings
        if group_id:
            all_totals = await self.get_group_players_npc_totals(group_id, npc_id, start_time, end_time)
        else:
            all_totals = await self.get_all_players_npc_totals(npc_id, start_time, end_time)
        
        # Sort players by total value
        sorted_players = sorted(all_totals.items(), key=lambda x: x[1], reverse=True)
        
        # Find player's global rank
        global_rank = None
        for i, (pid, total) in enumerate(sorted_players):
            if pid == player_id:
                global_rank = i + 1
                break
        
        total_players = len(sorted_players)
        
        # Get group rankings if player is in a group
        group_rank = None
        total_group_players = 0
        
        if group_id:
            group_totals = await self.get_group_players_npc_totals(group_id, npc_id, start_time, end_time)
            sorted_group = sorted(group_totals.items(), key=lambda x: x[1], reverse=True)
            
            for i, (pid, total) in enumerate(sorted_group):
                if pid == player_id:
                    group_rank = i + 1
                    break
            
            total_group_players = len(sorted_group)
        
        return (global_rank, total_players, group_rank, total_group_players, player_total)
    
    async def simulate_npc_drop_rank_change(self, player_id, npc_id, drop_value, start_time=None, end_time=None, group_id=None):
        """
        Simulate how a new drop would affect a player's rank for a specific NPC
        
        Returns a dictionary with structured information about rank changes
        """
        result = {
            "player_global": {},
            "player_in_group": {},
            "group": {}
        }
        
        # Get current ranks
        current_ranks = await self.get_player_npc_rank(player_id, npc_id, start_time, end_time, group_id)
        original_global_rank, total_players, original_group_rank, total_group_players, original_total = current_ranks
        
        # Calculate new total with the simulated drop
        new_total = original_total + drop_value
        
        # Get all player totals
        all_totals = await self.get_all_players_npc_totals(npc_id, start_time, end_time)
        
        # Create a copy with the simulated new total
        simulated_totals = all_totals.copy()
        simulated_totals[player_id] = new_total
        
        # Sort players by total value
        sorted_players = sorted(simulated_totals.items(), key=lambda x: x[1], reverse=True)
        
        # Find player's new global rank
        new_global_rank = next((i+1 for i, (pid, _) in enumerate(sorted_players) if pid == player_id), 0)
        
        # Calculate global rank change
        global_rank_change = 0
        if original_global_rank and new_global_rank:
            global_rank_change = original_global_rank - new_global_rank
        
        # Populate player_global result
        result["player_global"] = {
            "original_rank": original_global_rank,
            "original_total": original_total,
            "new_rank": new_global_rank,
            "new_total": new_total,
            "rank_change": global_rank_change,
            "improved": global_rank_change > 0
        }
        
        # Get player's groups if not specified
        player_group_ids = []
        if not group_id:
            player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
            player_group_ids_result = self.session.execute(text(player_group_id_query), {"player_id": player_id}).fetchall()
            player_group_ids = [g[0] for g in player_group_ids_result if g[0] != 2]  # Exclude group ID 2
        else:
            player_group_ids = [group_id]
        
        # Process each group the player is in
        for g_id in player_group_ids:
            # Get group players' totals for this NPC
            group_totals = await self.get_group_players_npc_totals(g_id, npc_id, start_time, end_time)
            
            if not group_totals:
                continue
            
            # Get player's current rank in this group
            group_sorted = sorted(group_totals.items(), key=lambda x: x[1], reverse=True)
            current_group_rank = next((i+1 for i, (pid, _) in enumerate(group_sorted) if pid == player_id), 0)
            
            # Create a copy with the simulated new total
            simulated_group_totals = group_totals.copy()
            simulated_group_totals[player_id] = new_total
            
            # Sort players in group by total value
            sorted_group_players = sorted(simulated_group_totals.items(), key=lambda x: x[1], reverse=True)
            
            # Find player's new rank in group
            new_group_rank = next((i+1 for i, (pid, _) in enumerate(sorted_group_players) if pid == player_id), 0)
            
            # Calculate group rank change
            group_rank_change = 0
            if current_group_rank and new_group_rank:
                group_rank_change = current_group_rank - new_group_rank
            
            # Populate player_in_group result
            result["player_in_group"][g_id] = {
                "original_rank": current_group_rank,
                "new_rank": new_group_rank,
                "rank_change": group_rank_change,
                "improved": group_rank_change > 0
            }
            
            # Now handle group-to-group comparison
            # Get all groups' totals for this NPC
            all_groups_query = """SELECT DISTINCT group_id FROM user_group_association WHERE group_id != 2"""
            all_group_ids = [g[0] for g in self.session.execute(text(all_groups_query)).fetchall()]
            
            all_group_totals = {}
            for group_id in all_group_ids:
                group_players = await self.get_group_players_npc_totals(group_id, npc_id, start_time, end_time)
                if group_players:
                    all_group_totals[group_id] = sum(group_players.values())
            
            # Get current group rank
            sorted_groups = sorted(all_group_totals.items(), key=lambda x: x[1], reverse=True)
            current_group_global_rank = next((i+1 for i, (gid, _) in enumerate(sorted_groups) if gid == g_id), 0)
            
            # Calculate new group total
            original_group_total = all_group_totals.get(g_id, 0)
            new_group_total = original_group_total - original_total + new_total
            
            # Create simulated group totals
            simulated_group_global_totals = all_group_totals.copy()
            simulated_group_global_totals[g_id] = new_group_total
            
            # Sort groups by total value
            sorted_simulated_groups = sorted(simulated_group_global_totals.items(), key=lambda x: x[1], reverse=True)
            
            # Find group's new global rank
            new_group_global_rank = next((i+1 for i, (gid, _) in enumerate(sorted_simulated_groups) if gid == g_id), 0)
            
            # Calculate group global rank change
            group_global_rank_change = 0
            if current_group_global_rank and new_group_global_rank:
                group_global_rank_change = current_group_global_rank - new_group_global_rank
            
            # Populate group result
            result["group"][g_id] = {
                "original_rank": current_group_global_rank,
                "original_total": original_group_total,
                "new_rank": new_group_global_rank,
                "new_total": new_group_total,
                "rank_change": group_global_rank_change,
                "improved": group_global_rank_change > 0
            }
        
        return result
    
    async def simulate_group_npc_rank_change(self, group_id, player_id, npc_id, drop_value, start_time=None, end_time=None):
        """Helper method to simulate how a drop affects a group's ranking for a specific NPC"""
        # This would require implementing group-to-group comparison for specific NPCs
        # For now, return a placeholder or None
        return None

async def check_npc_rank_change_from_drop(player_id: int, drop_data: Drop, specific_group_id: int = None):
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
        npc_ranker = NPCRankChecker()
        npc_results = await npc_ranker.simulate_npc_drop_rank_change(player_id, drop_data.npc_id, drop_value)
        #print("Got NPC results:", npc_results)
        
        # If specific_group_id is provided, filter the results
        if specific_group_id is not None:
            # Check if the player is actually in this group
            player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id AND group_id = :group_id"""
            is_in_group = npc_ranker.session.execute(
                text(player_group_id_query), 
                {"player_id": player_id, "group_id": specific_group_id}
            ).fetchone() is not None
            
            if not is_in_group:
                print(f"Warning: Player {player_id} is not in group {specific_group_id} for NPC ranking")
            
            filtered_result = {
                "player_global": npc_results.get("player_global", {}),
                "player_in_group": {},
                "group": {}
            }
            
            # Only include the specific group if the player is in it
            if is_in_group:
                if specific_group_id in npc_results.get("player_in_group", {}):
                    filtered_result["player_in_group"][specific_group_id] = npc_results["player_in_group"][specific_group_id]
                
                if specific_group_id in npc_results.get("group", {}):
                    filtered_result["group"][specific_group_id] = npc_results["group"][specific_group_id]
            
            #print(f"Filtered NPC result for group {specific_group_id}: {filtered_result}")
            return filtered_result
        
        # Make sure we're returning a complete result with all sections
        complete_result = {
            "player_global": npc_results.get("player_global", {}),
            "player_in_group": npc_results.get("player_in_group", {}),
            "group": npc_results.get("group", {})
        }
        
        print(f"Final NPC result from check_npc_rank_change_from_drop: {complete_result}")
        return complete_result
    except Exception as e:
        #print(f"Error checking NPC rank change: {e}")
        return {
            "player_global": {},
            "player_in_group": {},
            "group": {}
        }
    


def generate_time_partitions(start_time, end_time, granularity):
    """
    Generate a list of time partition strings between start_time and end_time
    based on the specified granularity.
    """
    partitions = []
    current_time = start_time
    
    if granularity == "monthly":
        # Generate monthly partitions (YYYYMM)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next month
            if current_time.month == 12:
                current_time = current_time.replace(year=current_time.year + 1, month=1)
            else:
                current_time = current_time.replace(month=current_time.month + 1)
    
    elif granularity == "daily":
        # Generate daily partitions (YYYYMMDD)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m%d')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next day
            current_time = current_time + timedelta(days=1)
    
    elif granularity == "hourly":
        # Generate hourly partitions (YYYYMMDDHH)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m%d%H')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next hour
            current_time = current_time + timedelta(hours=1)
    
    else:  # minute
        # Generate minute partitions (YYYYMMDDHHMM)
        while current_time <= end_time:
            partition = current_time.strftime('%Y%m%d%H%M')
            if partition not in partitions:
                partitions.append(partition)
            # Move to next minute
            current_time = current_time + timedelta(minutes=1)
    
    return partitions