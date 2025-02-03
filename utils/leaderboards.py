from datetime import datetime
from typing import Optional, List, Dict
from utils.redis import RedisClient

class LeaderboardManager:
    def __init__(self):
        self.redis = RedisClient()
        
    def get_current_partition(self) -> int:
        now = datetime.now()
        return now.year * 100 + now.month
        
    def update_player_leaderboard_score(self, player_id: int, partition: Optional[int] = None):
        """Update a player's score in various leaderboards"""
        if partition is None:
            partition = self.get_current_partition()
            
        # Get player's total for the partition
        total = self.redis.client.get(f"player:{player_id}:{partition}:total_loot")
        if not total:
            return
            
        # Update monthly leaderboard
        self.redis.client.zadd(
            f"leaderboard:monthly:{partition}",
            {str(player_id): int(total)}
        )
        
        # Update all-time leaderboard
        all_time_total = self.redis.client.get(f"player:{player_id}:all:total_loot")
        if all_time_total:
            self.redis.client.zadd(
                "leaderboard:all_time",
                {str(player_id): int(all_time_total)}
            )
            
    def get_top_players(self, 
                       leaderboard_key: str, 
                       start: int = 0, 
                       end: int = 9, 
                       with_scores: bool = True) -> List[Dict]:
        """Get top players from a specific leaderboard with their ranks"""
        results = self.redis.client.zrevrange(
            leaderboard_key,
            start,
            end,
            withscores=True
        )
        
        if not results:
            return []
            
        players = []
        for rank, (player_id, score) in enumerate(results, start=start+1):
            players.append({
                "rank": rank,
                "player_id": int(player_id),
                "score": int(score)
            })
            
        return players
        
    def get_player_rank(self, player_id: str, leaderboard_key: str) -> Optional[Dict]:
        """Get a specific player's rank and score"""
        rank = self.redis.client.zrevrank(leaderboard_key, str(player_id))
        if rank is None:
            return None
            
        score = self.redis.client.zscore(leaderboard_key, str(player_id))
        return {
            "rank": rank + 1,
            "score": int(score)
        }
