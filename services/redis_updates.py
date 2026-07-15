### Contains the new functional implementation for interacting with the redis dicts for players' drops
## As it relates to generating new loot leaderboards
from db import Drop, Player, session, Group, models
from utils.format import format_number
from utils.redis import redis_client
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Set, Iterable
import json
import threading
import time
from dataclasses import dataclass
from enum import Enum
from sqlalchemy import text
from sqlalchemy.orm import joinedload
from utils.format import get_current_partition

class UpdateMode(Enum):
    INCREMENTAL = "incremental"  # Add new drops to existing data
    FORCE_UPDATE = "force_update"  # Recalculate everything from database

@dataclass
class LootLeaderboardQuery:
    """Query parameters for generating loot leaderboards"""
    player_ids: Optional[List[int]] = None
    npc_ids: Optional[List[int]] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    min_item_value: Optional[int] = None  # For high-value item tracking
    partition: Optional[int] = None  # Monthly partition (YYYYMM)

@dataclass
class PlayerItemData:
    """Individual item data for a player"""
    item_id: int
    quantity: int
    total_value: int
    drop_count: int
    first_drop: datetime
    last_drop: datetime

@dataclass
class PlayerLootSummary:
    """Complete loot summary for a player"""
    player_id: int
    total_value: int
    total_drops: int
    unique_items: int
    unique_npcs: int
    items: Dict[int, PlayerItemData]
    high_value_items: List[Dict]  # Items exceeding min_item_value threshold

class RedisLootTracker:
    """
    Functional implementation for Redis-based loot tracking and leaderboard generation.
    Handles both incremental updates and force updates with concurrency safety.
    """
    
    def __init__(self):
        self._lock = threading.RLock()  # Reentrant lock for thread safety
        self._processing_players: Set[int] = set()  # Track players being processed
        
    def _get_partition(self, dt: datetime = None) -> int:
        return get_current_partition()  

    def _coerce_drop_datetime(self, drop_date) -> datetime:
        """Best-effort normalization for drop timestamp values."""
        if isinstance(drop_date, datetime):
            return drop_date
        if isinstance(drop_date, str):
            try:
                return datetime.fromisoformat(drop_date)
            except Exception:
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(drop_date, fmt)
                except Exception:
                    continue
        return datetime.now()
    
    def _get_redis_keys(self, player_id: int, partition: int, drop_date: datetime = None, world_type: str = "main") -> Dict[str, str]:
        """Generate Redis keys for a player and partition.

        Seasonal submissions are stored under a separate key namespace
        (``seasonal:player:...``) to keep them completely isolated from
        main-world data.
        """
        ns = "seasonal:player" if world_type == "seasonal" else "player"
        base_keys = {
            'total_items': f"{ns}:{player_id}:{partition}:total_items",
            'total_loot': f"{ns}:{player_id}:{partition}:total_loot",
            'recent_items': f"{ns}:{player_id}:{partition}:recent_items",
            'drop_history': f"{ns}:{player_id}:{partition}:drop_history",
            'high_value_items': f"{ns}:{player_id}:{partition}:high_value_items",
            'all_time_total_items': f"{ns}:{player_id}:all:total_items",
            'all_time_total_loot': f"{ns}:{player_id}:all:total_loot",
            'all_time_recent_items': f"{ns}:{player_id}:all:recent_items",
            'all_time_high_value_items': f"{ns}:{player_id}:all:high_value_items"
        }

        # Add daily keys if drop_date is provided
        if drop_date:
            daily_partition = drop_date.strftime('%Y%m%d')  # YYYYMMDD format
            base_keys.update({
                'daily_total_items': f"{ns}:{player_id}:daily:{daily_partition}:total_items",
                'daily_total_loot': f"{ns}:{player_id}:daily:{daily_partition}:total_loot",
                'daily_recent_items': f"{ns}:{player_id}:daily:{daily_partition}:recent_items",
                'daily_drop_history': f"{ns}:{player_id}:daily:{daily_partition}:drop_history",
                'daily_high_value_items': f"{ns}:{player_id}:daily:{daily_partition}:high_value_items"
            })

        return base_keys
    
    def _atomic_hash_update_script(self) -> str:
        """Lua script for atomic hash updates"""
        return """
        local key = KEYS[1]
        local item_id = ARGV[1]
        local qty_delta = tonumber(ARGV[2])
        local value_delta = tonumber(ARGV[3])
        local force_update = ARGV[4] == "true"
        local drop_count_delta = tonumber(ARGV[5])
        local first_drop = ARGV[6]
        local last_drop = ARGV[7]
        
        local current = redis.call('HGET', key, item_id)
        local new_qty, new_value, new_drop_count, new_first_drop, new_last_drop
        
        if current and not force_update then
            local parts = {}
            for part in string.gmatch(current, "[^,]+") do
                table.insert(parts, part)
            end
            
            if #parts >= 5 then
                local existing_qty = tonumber(parts[1])
                local existing_value = tonumber(parts[2])
                local existing_drop_count = tonumber(parts[3])
                local existing_first_drop = parts[4]
                local existing_last_drop = parts[5]
                
                new_qty = existing_qty + qty_delta
                new_value = existing_value + value_delta
                new_drop_count = existing_drop_count + drop_count_delta
                new_first_drop = existing_first_drop
                new_last_drop = last_drop  -- Always update to latest
            else
                new_qty = qty_delta
                new_value = value_delta
                new_drop_count = drop_count_delta
                new_first_drop = first_drop
                new_last_drop = last_drop
            end
        else
            new_qty = qty_delta
            new_value = value_delta
            new_drop_count = drop_count_delta
            new_first_drop = first_drop
            new_last_drop = last_drop
        end
        
        local result = new_qty .. "," .. new_value .. "," .. new_drop_count .. "," .. new_first_drop .. "," .. new_last_drop
        redis.call('HSET', key, item_id, result)
        return result
        """
    
    def add_to_player(
        self,
        player: Player,
        drop,
        world_type: str = "main",
        item_name: str | None = None,
        npc_name: str | None = None,
        exclude_group_ids: set | None = None,
    ) -> bool:
        """
        Add a single drop to a player's Redis cache (incremental update).
        Thread-safe and atomic. Also updates leaderboards.

        Pass ``world_type="seasonal"`` to write into the seasonal key namespace
        so that seasonal and main-world data are stored completely separately.

        ``item_name``/``npc_name`` are optional display strings the caller has
        already resolved (e.g. ``drop_processor``) — passed through only for the
        realtime drop-feed publish so it can show text without this hot path
        doing its own DB lookup.

        ``exclude_group_ids``: groups whose boards this drop must NOT count
        toward (manual_submission_policy, suggestion #45) — filtered out of
        both the leaderboard increments and the realtime group publishes.
        """
        with self._lock:
            if player.player_id in self._processing_players:
                # Player is being force-updated, skip incremental update
                return False

            try:
                result = self._add_drop_incremental(player.player_id, drop, world_type=world_type)
                if result:
                    # Update leaderboards after successful drop addition
                    partition = self._get_partition(drop.date_added)
                    total_value = drop.value * drop.quantity

                    # Get player's group IDs for group leaderboards
                    player_group_ids = [group.group_id for group in player.groups] if player.groups else []
                    if exclude_group_ids:
                        player_group_ids = [g for g in player_group_ids if g not in exclude_group_ids]

                    # Update leaderboards incrementally using ZINCRBY instead of full recalculation.
                    # The drop's own timestamp decides which weekly/daily boards it lands on,
                    # so late-processed drops around a day/week boundary stay on the right board.
                    drop_dt = self._coerce_drop_datetime(drop.date_added)
                    self._increment_leaderboards(player.player_id, total_value, partition, player_group_ids,
                                                 world_type=world_type, drop_dt=drop_dt,
                                                 npc_id=getattr(drop, "npc_id", None))

                    # Additive: publish a realtime leaderboard_delta (Task 07 C).
                    # Never affects intake — publish failures are swallowed.
                    try:
                        from services.realtime import publish_drop
                        publish_drop(
                            player, drop, total_value, partition, player_group_ids,
                            world_type=world_type, item_name=item_name, npc_name=npc_name,
                        )
                    except Exception:
                        pass

                return result
            except Exception as e:
                print(f"Error adding drop {getattr(drop, 'drop_id', '?')} to player {player.player_id}: {e}")
                return False
    
    def _increment_leaderboards(self, player_id: int, value_delta: int,
                                partition: Optional[int] = None, group_ids: Optional[List[int]] = None,
                                world_type: str = "main", drop_dt: Optional[datetime] = None,
                                npc_id: Optional[int] = None):
        """
        Incrementally update leaderboards by adding value_delta to player's score.
        More efficient than full recalculation for individual drop additions.

        Seasonal leaderboard keys are prefixed with ``seasonal:`` to keep them
        separate from main-world rankings.
        """
        if partition is None:
            partition = self._get_partition()

        prefix = "seasonal:" if world_type == "seasonal" else ""
        pipeline = redis_client.client.pipeline(transaction=True)

        # Update global leaderboard
        global_key = f"{prefix}leaderboard:{partition}"
        pipeline.zincrby(global_key, value_delta, player_id)

        # Update group leaderboards
        if group_ids:
            for group_id in group_ids:
                group_key = f"{prefix}leaderboard:{partition}:group:{group_id}"
                pipeline.zincrby(group_key, value_delta, player_id)

        pipeline.execute()

        # --- Additive: weekly / daily / all-time partitions (Task 07 Part A) ---
        # The monthly board above is unchanged. These extra sorted sets are
        # populated going forward so the Web API's period=day|week|all filters
        # have real data. Failures here never affect the monthly board or the
        # drop itself.
        try:
            self._increment_extra_partitions(player_id, value_delta, group_ids, prefix, drop_dt)
        except Exception as e:
            print(f"[redis_updates] extra-partition update skipped: {e}")

        # --- Additive: per-NPC loot boards ---
        # Powers the Hall of Fame "Most Loot" section and the website's NPC
        # leaderboards, which read these keys but previously had no writer.
        # Best-effort: never affects the boards above or the drop itself.
        try:
            self._increment_npc_leaderboards(player_id, value_delta, npc_id, partition,
                                             group_ids, prefix)
        except Exception as e:
            print(f"[redis_updates] npc-leaderboard update skipped: {e}")

    # Monthly per-NPC boards get a TTL (many NPCs × groups × months would grow
    # unbounded otherwise); the all-time boards persist like leaderboard:all.
    _NPC_MONTH_TTL = 400 * 24 * 3600   # ~13 months

    def _increment_npc_leaderboards(self, player_id: int, value_delta: int,
                                    npc_id: Optional[int], partition: int,
                                    group_ids: Optional[List[int]], prefix: str):
        """Maintain per-NPC loot sorted sets (player_id -> total GP from this NPC).

        Keys (mirroring web_api.common.npc_leaderboard_key and the reads in
        services/hall_of_fame.py):
          {prefix}leaderboard:npc:{npc_id}[:{partition}]                 (global)
          {prefix}leaderboard:group:{gid}:npc:{npc_id}[:{partition}]     (per group)
        """
        try:
            npc_id = int(npc_id) if npc_id is not None else 0
        except (TypeError, ValueError):
            npc_id = 0
        if npc_id <= 0 or not value_delta:
            return

        pipeline = redis_client.client.pipeline(transaction=True)

        # Global (used by the website and by the global/template group's HOF).
        month_key = f"{prefix}leaderboard:npc:{npc_id}:{partition}"
        all_key = f"{prefix}leaderboard:npc:{npc_id}"
        pipeline.zincrby(month_key, value_delta, player_id)
        pipeline.expire(month_key, self._NPC_MONTH_TTL)
        pipeline.zincrby(all_key, value_delta, player_id)

        # Per-group.
        if group_ids:
            for group_id in group_ids:
                g_month = f"{prefix}leaderboard:group:{group_id}:npc:{npc_id}:{partition}"
                g_all = f"{prefix}leaderboard:group:{group_id}:npc:{npc_id}"
                pipeline.zincrby(g_month, value_delta, player_id)
                pipeline.expire(g_month, self._NPC_MONTH_TTL)
                pipeline.zincrby(g_all, value_delta, player_id)

        pipeline.execute()

    # Retention for the additive weekly/daily boards (bounds Redis memory).
    _WEEKLY_TTL = 400 * 24 * 3600   # ~13 months
    _DAILY_TTL = 90 * 24 * 3600     # 90 days (matches player daily key retention)

    def _increment_extra_partitions(self, player_id: int, value_delta: int,
                                    group_ids: Optional[List[int]], prefix: str,
                                    drop_dt: Optional[datetime] = None):
        """Maintain weekly / daily / all-time global + per-group player boards.

        Strictly additive to the monthly board. Weekly and daily sets get a TTL
        so they don't grow unbounded; all-time and monthly persist. ``drop_dt``
        (the drop's own timestamp) picks the week/day tokens; wall clock is only
        the fallback.
        """
        from utils.partitions import week_token, day_token, ALL

        now = drop_dt or datetime.now()
        wk, day = week_token(now), day_token(now)
        # (token, ttl) — None ttl => persist.
        tokens = [(wk, self._WEEKLY_TTL), (day, self._DAILY_TTL), (ALL, None)]

        pipeline = redis_client.client.pipeline(transaction=True)
        for token, ttl in tokens:
            gkey = f"{prefix}leaderboard:{token}"
            pipeline.zincrby(gkey, value_delta, player_id)
            if ttl:
                pipeline.expire(gkey, ttl)
            if group_ids:
                for group_id in group_ids:
                    grpkey = f"{prefix}leaderboard:{token}:group:{group_id}"
                    pipeline.zincrby(grpkey, value_delta, player_id)
                    if ttl:
                        pipeline.expire(grpkey, ttl)
        pipeline.execute()
    
    def _add_drop_incremental(self, player_id: int, drop, world_type: str = "main") -> bool:
        """Internal method for incremental drop addition"""
        drop_date = self._coerce_drop_datetime(drop.date_added)
        partition = self._get_partition(drop_date)
        keys = self._get_redis_keys(player_id, partition, drop_date, world_type=world_type)
        
        # Calculate drop values
        total_value = drop.value * drop.quantity
        drop_timestamp = drop_date.strftime('%Y-%m-%d %H:%M:%S')
        
        # Use pipeline for atomic operations
        pipeline = redis_client.client.pipeline(transaction=True)
        
        # Update monthly item totals
        pipeline.eval(
            self._atomic_hash_update_script(),
            1,
            keys['total_items'],
            str(drop.item_id),
            str(drop.quantity),
            str(total_value),
            "false",  # Not force update
            "1",  # Drop count delta
            drop_timestamp,
            drop_timestamp
        )
        
        # Update all-time item totals
        pipeline.eval(
            self._atomic_hash_update_script(),
            1,
            keys['all_time_total_items'],
            str(drop.item_id),
            str(drop.quantity),
            str(total_value),
            "false",
            "1",
            drop_timestamp,
            drop_timestamp
        )
        
        # Update daily item totals
        pipeline.eval(
            self._atomic_hash_update_script(),
            1,
            keys['daily_total_items'],
            str(drop.item_id),
            str(drop.quantity),
            str(total_value),
            "false",
            "1",
            drop_timestamp,
            drop_timestamp
        )
        
        # Update total loot for all granularities
        pipeline.incrbyfloat(keys['total_loot'], total_value)  # Monthly
        pipeline.incrbyfloat(keys['all_time_total_loot'], total_value)  # All-time
        pipeline.incrbyfloat(keys['daily_total_loot'], total_value)  # Daily

        # Daily per-player keys must expire: this hot path is what creates
        # them, and before these EXPIREs it never set a TTL — ~100k daily
        # hashes (~200MB) accumulated permanently between 2025-11 and 2026-07.
        # 90 days matches _rebuild_daily_data's retention.
        pipeline.expire(keys['daily_total_items'], self._DAILY_TTL)
        pipeline.expire(keys['daily_total_loot'], self._DAILY_TTL)

        if int(drop.value * drop.quantity) > 1000000:
            # Add to recent items
            recent_item_data = {
                'drop_id': drop.drop_id,
                'item_id': drop.item_id,
                'npc_id': drop.npc_id,
                'value': drop.value,
                'quantity': drop.quantity,
                'total_value': total_value,
                'date_added': drop_timestamp,
                'partition': partition
            }
            
            # Add to all granularities
            pipeline.lpush(keys['recent_items'], json.dumps(recent_item_data))  # Monthly
            pipeline.lpush(keys['all_time_recent_items'], json.dumps(recent_item_data))  # All-time
            pipeline.lpush(keys['daily_recent_items'], json.dumps(recent_item_data))  # Daily
            
            # Trim recent items lists
            pipeline.ltrim(keys['recent_items'], 0, 49)  # Keep last 50 items (monthly)
            pipeline.ltrim(keys['all_time_recent_items'], 0, 99)  # Keep last 100 items (all-time)
            pipeline.ltrim(keys['daily_recent_items'], 0, 24)  # Keep last 25 items (daily)
            pipeline.expire(keys['daily_recent_items'], self._DAILY_TTL)
        
        # Execute all operations atomically
        try:
            pipeline.execute()
            return True
        except Exception as e:
            print(f"Pipeline execution failed: {e}")
            return False
    
    def force_update_player(self, player_id: int, session_to_use=None) -> bool:
        """
        Force update a player's Redis cache by recalculating from database.
        This removes all existing Redis data and rebuilds from scratch.
        Thread-safe with concurrency protection.
        """
        with self._lock:
            if player_id in self._processing_players:
                return False  # Already being processed
            
            self._processing_players.add(player_id)
        
        try:
            return self._force_update_player_internal(player_id, session_to_use)
        finally:
            with self._lock:
                self._processing_players.discard(player_id)
    
    def _force_update_player_internal(self, player_id: int, session_to_use=None) -> bool:
        """Internal force update implementation"""
        if session_to_use is None:
            session_to_use = session
        
        try:
            # Get player with groups
            player = session_to_use.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.groups)).first()
            if not player:
                print(f"Player {player_id} not found")
                return False
            
            # Get player's group IDs
            player_group_ids = [group.group_id for group in player.groups]
            print(f"Player {player_id} belongs to groups: {player_group_ids}")
            
            # Get all visible drops for the player (exclude hidden)
            player_drops = session_to_use.query(Drop).filter(
                Drop.player_id == player_id,
                Drop.hidden != True,
            ).order_by(Drop.date_added.asc()).all()
            
            if not player_drops:
                # No drops, clear Redis data and remove from leaderboards.
                # Still advance date_updated — otherwise drop-less players sit
                # at the head of the stale-player queue forever and starve it.
                self._clear_player_redis_data(player_id)
                self._remove_from_leaderboards(player_id, player_group_ids)
                player.date_updated = datetime.now()
                session_to_use.commit()
                return True
            
            # Group drops by partition (monthly) and by day
            partition_drops = {}  # monthly partitions
            daily_drops = {}      # daily partitions
            
            for drop in player_drops:
                # Monthly partition
                partition = drop.partition
                if partition not in partition_drops:
                    partition_drops[partition] = []
                partition_drops[partition].append(drop)
                
                # Daily partition
                daily_partition = drop.date_added.strftime('%Y%m%d')
                if daily_partition not in daily_drops:
                    daily_drops[daily_partition] = []
                daily_drops[daily_partition].append(drop)
            
            # Clear existing Redis data
            self._clear_player_redis_data(player_id)
            self._remove_from_leaderboards(player_id, player_group_ids)

            # Per-group manual-submission exclusions (drop_group_moderation):
            # subtract from the affected GROUP boards only — the intake path
            # never counted these drops there, so the rebuild must not either.
            from services.drop_moderation import player_exclusion_totals
            try:
                excl_monthly, excl_daily = player_exclusion_totals(session_to_use, player_id)
            except Exception as e:
                print(f"Couldn't load manual-policy exclusions for player {player_id}: {e}")
                excl_monthly, excl_daily = {}, {}

            # Rebuild Redis data for each monthly partition and update leaderboards
            all_time_total = 0
            for partition, drops in partition_drops.items():
                total_loot = self._rebuild_partition_data(player_id, partition, drops)
                all_time_total += total_loot
                # Update leaderboards for this partition
                deductions = {
                    gid: amt for (gid, part), amt in excl_monthly.items() if part == partition
                }
                self.update_leaderboards(player_id, total_loot, partition, player_group_ids,
                                         group_deductions=deductions or None)
                print(f"Updated leaderboards for player {player_id} in partition {partition}")

            # Re-establish the all-time global board + total from the accumulated
            # per-partition totals. `_remove_from_leaderboards` cleared the stale
            # all-time score above, so this ZADD (absolute) is authoritative and
            # keeps `leaderboard:all` correct after drops are hidden/edited —
            # something the incremental ZINCRBY path alone cannot do.
            self._rebuild_all_time_total(player_id, all_time_total)

            # Rebuild Redis data for each daily partition
            for daily_partition, drops in daily_drops.items():
                self._rebuild_daily_data(player_id, daily_partition, drops)
                print(f"Updated daily data for player {player_id} on {daily_partition}")

            # Re-write this player's daily + weekly leaderboard scores from the
            # same per-day grouping. Without this, the incremental
            # ZINCRBY-maintained boards keep stale scores forever after drops
            # are hidden/edited — the monthly and all-time boards were repaired
            # above, but day/week were not.
            self._rebuild_period_leaderboards(player_id, daily_drops, player_group_ids,
                                              group_day_deductions=excl_daily or None)

            # Re-apply split GP credits earned as a participant in other players' drops.
            # These are not in the player's own Drop rows, so they must be reconstructed
            # from drop_splits separately.
            self._apply_split_credits(player_id, session_to_use)

            # Update player's last update timestamp
            player.date_updated = datetime.now()
            session_to_use.commit()

            return True
            
        except Exception as e:
            print(f"Force update failed for player {player_id}: {e}")
            # Roll the session back before returning. The background updater
            # reuses one session across a batch of players; if a query here dies
            # mid-transaction (e.g. the drops SELECT hits pymysql read_timeout)
            # and we DON'T roll back, the very next player on that session fails
            # instantly with "Can't reconnect until invalid transaction is rolled
            # back. Please rollback() fully before proceeding." — the chronic
            # error pair seen in droptracker-player-updates.
            try:
                session_to_use.rollback()
            except Exception as rollback_error:
                print(f"Rollback after force-update failure also failed for player {player_id}: {rollback_error}")
            return False
    
    def _apply_split_credits(self, player_id: int, session_to_use) -> None:
        """
        After a full Redis rebuild, re-apply group leaderboard credits that come
        from drop_splits rows (i.e. drops the player participated in but did not
        receive).  Only rows for groups that currently have split_gp_tracking
        enabled are processed so that stale rows from before the flag was enabled
        are silently ignored.
        """
        try:
            from db.models.drop_split import DropSplit
            from db.models import GroupConfiguration, Drop

            split_rows = (
                session_to_use.query(DropSplit)
                .filter(DropSplit.player_id == player_id)
                .all()
            )
            if not split_rows:
                return

            # Build a set of group_ids that have split_gp_tracking enabled
            unique_group_ids = {row.group_id for row in split_rows}
            enabled_configs = (
                session_to_use.query(GroupConfiguration.group_id)
                .filter(
                    GroupConfiguration.group_id.in_(unique_group_ids),
                    GroupConfiguration.config_key == "split_gp_tracking",
                    GroupConfiguration.config_value == "1",
                )
                .all()
            )
            enabled_groups = {row.group_id for row in enabled_configs}

            for row in split_rows:
                if row.group_id not in enabled_groups:
                    continue
                # Determine the partition from the parent drop
                drop = session_to_use.query(Drop).filter(Drop.drop_id == row.drop_id).first()
                if not drop or drop.hidden:
                    continue
                partition = drop.partition
                self.add_split_credit(player_id, row.split_value, partition, row.group_id)
        except Exception as e:
            print(f"[RedisLootTracker] _apply_split_credits failed for player {player_id}: {e}")

    def _clear_player_redis_data(self, player_id: int):
        """Clear all Redis data for a player.

        Uses SCAN, not KEYS: KEYS walks the entire keyspace (~1M keys) while
        holding the Redis event loop, stalling every other client for
        hundreds of ms per call (it dominated the slowlog).
        """
        pattern = f"player:{player_id}:*"
        batch = []
        for key in redis_client.client.scan_iter(match=pattern, count=1000):
            batch.append(key)
            if len(batch) >= 500:
                redis_client.client.delete(*batch)
                batch = []
        if batch:
            redis_client.client.delete(*batch)
    
    def _remove_from_leaderboards(self, player_id: int, group_ids: List[int]):
        """Remove player from the current-month + all-time global leaderboards.

        The all-time board (``leaderboard:all``) must be cleared here too, so that
        a subsequent rebuild (``force_update_player``) re-adds an accurate score
        instead of layering on top of a stale one. Historical monthly boards and
        per-group all-time boards are intentionally left untouched (force_update
        re-adds each monthly partition; per-group all-time is maintained purely
        incrementally and is not read by any user-facing surface).
        """
        from utils.partitions import week_token, day_token

        current_partition = self._get_partition()

        pipeline = redis_client.client.pipeline(transaction=True)

        # Remove from global leaderboard (current month + all-time + current
        # week/day). The week/day boards are re-added with absolute scores by
        # `_rebuild_period_leaderboards`, so clearing here is what makes a
        # fully-hidden player actually disappear from them.
        global_key = f"leaderboard:{current_partition}"
        pipeline.zrem(global_key, player_id)
        pipeline.zrem("leaderboard:all", player_id)
        period_tokens = (week_token(), day_token())
        for token in period_tokens:
            pipeline.zrem(f"leaderboard:{token}", player_id)

        # Remove from group leaderboards
        for group_id in group_ids:
            group_key = f"leaderboard:{current_partition}:group:{group_id}"
            pipeline.zrem(group_key, player_id)
            for token in period_tokens:
                pipeline.zrem(f"leaderboard:{token}:group:{group_id}", player_id)

        pipeline.execute()
    
    def _rebuild_partition_data(self, player_id: int, partition: int, drops: List[Drop]) -> int:
        """Rebuild Redis data for a specific partition. Returns total loot value."""
        keys = self._get_redis_keys(player_id, partition)
        
        # Aggregate data
        item_data = {}  # item_id -> (quantity, total_value, drop_count, first_drop, last_drop)
        total_loot = 0
        recent_items_raw = []
        
        for drop in drops:
            total_value = drop.value * drop.quantity
            total_loot += total_value
            drop_timestamp = drop.date_added.strftime('%Y-%m-%d %H:%M:%S')
            
            # Aggregate item data
            if drop.item_id not in item_data:
                item_data[drop.item_id] = (0, 0, 0, drop_timestamp, drop_timestamp)
            
            qty, val, count, first, _ = item_data[drop.item_id]
            item_data[drop.item_id] = (qty + drop.quantity, val + total_value, count + 1, first, drop_timestamp)
            if int(drop.value * drop.quantity) > 1000000:
                # Add to recent items
                recent_item_data = {
                    'drop_id': drop.drop_id,
                    'item_id': drop.item_id,
                    'npc_id': drop.npc_id,
                    'value': drop.value,
                    'quantity': drop.quantity,
                    'total_value': total_value,
                    'date_added': drop_timestamp,
                    'partition': partition }
                recent_items_raw.append(recent_item_data)
        
        # Use pipeline for atomic updates
        pipeline = redis_client.client.pipeline(transaction=True)
        
        # Set this partition's monthly total loot. The all-time total loot is
        # NOT set here: this method is called once per monthly partition, so
        # setting `all_time_total_loot` to a single partition's total would
        # leave it equal to whichever partition was rebuilt last. The correct
        # all-time sum is written once by `_rebuild_all_time_total` after the
        # per-partition loop in `_force_update_player_internal`.
        pipeline.set(keys['total_loot'], total_loot)

        recent_items_raw.sort(key=lambda x: x['date_added'])
        recent_items = [json.dumps(item) for item in recent_items_raw]
        
        # Set item data
        for item_id, (qty, val, count, first, last) in item_data.items():
            item_value = f"{qty},{val},{count},{first},{last}"
            pipeline.hset(keys['total_items'], item_id, item_value)
            pipeline.hset(keys['all_time_total_items'], item_id, item_value)
        
        # Set recent items
        if recent_items_raw:
            pipeline.delete(keys['recent_items'])
            pipeline.delete(keys['all_time_recent_items'])
            pipeline.lpush(keys['recent_items'], *recent_items)  # Use recent_items, not recent_items_raw
            pipeline.lpush(keys['all_time_recent_items'], *recent_items)  # Use recent_items, not recent_items_raw
        
        # Execute all operations
        pipeline.execute()

        return total_loot

    def _rebuild_period_leaderboards(self, player_id: int, daily_drops: Dict[str, List[Drop]],
                                     group_ids: List[int],
                                     group_day_deductions: Optional[Dict[tuple, int]] = None) -> None:
        """Set the player's authoritative daily + weekly board scores after a
        full rebuild.

        ``daily_drops`` is the ``{YYYYMMDD: [drops]}`` grouping already built by
        ``_force_update_player_internal`` (visible drops only). Scores are
        written with absolute ZADD — overwriting whatever the incremental
        ZINCRBY path accumulated — for every day within the daily retention
        window and every ISO week within the weekly retention window.
        ``_remove_from_leaderboards`` clears the current day/week entries first,
        so a player whose current drops were all hidden drops off those boards.

        ``group_day_deductions``: ``(group_id, 'YYYYMMDD') -> GP`` to subtract
        from that group's day (and containing week) board — the per-group
        manual_submission_policy exclusions (drop_group_moderation). Global
        boards always get the full totals.
        """
        from utils.partitions import week_token

        now = datetime.now()
        daily_cutoff = now - timedelta(seconds=self._DAILY_TTL)
        weekly_cutoff = now - timedelta(seconds=self._WEEKLY_TTL)
        deductions = group_day_deductions or {}

        weekly_totals: Dict[str, int] = {}
        weekly_group_deductions: Dict[tuple, int] = {}
        pipeline = redis_client.client.pipeline(transaction=True)

        for daily_partition, drops in daily_drops.items():
            try:
                day_dt = datetime.strptime(daily_partition, '%Y%m%d')
            except ValueError:
                continue
            day_total = sum(d.value * d.quantity for d in drops)

            if day_dt >= weekly_cutoff:
                wk = week_token(day_dt)
                weekly_totals[wk] = weekly_totals.get(wk, 0) + day_total
                for group_id in group_ids:
                    ded = deductions.get((group_id, daily_partition), 0)
                    if ded:
                        key = (group_id, wk)
                        weekly_group_deductions[key] = weekly_group_deductions.get(key, 0) + ded

            if day_dt < daily_cutoff:
                continue
            day_key = f"leaderboard:{daily_partition}"
            pipeline.zadd(day_key, {player_id: day_total})
            pipeline.expire(day_key, self._DAILY_TTL)
            for group_id in group_ids:
                gkey = f"leaderboard:{daily_partition}:group:{group_id}"
                group_day_total = max(day_total - deductions.get((group_id, daily_partition), 0), 0)
                pipeline.zadd(gkey, {player_id: group_day_total})
                pipeline.expire(gkey, self._DAILY_TTL)

        for wk, week_total in weekly_totals.items():
            week_key = f"leaderboard:{wk}"
            pipeline.zadd(week_key, {player_id: week_total})
            pipeline.expire(week_key, self._WEEKLY_TTL)
            for group_id in group_ids:
                gkey = f"leaderboard:{wk}:group:{group_id}"
                group_week_total = max(week_total - weekly_group_deductions.get((group_id, wk), 0), 0)
                pipeline.zadd(gkey, {player_id: group_week_total})
                pipeline.expire(gkey, self._WEEKLY_TTL)

        pipeline.execute()

    def _rebuild_all_time_total(self, player_id: int, all_time_total: int) -> None:
        """Set the player's authoritative all-time score/total after a full rebuild.

        `all_time_total` is the sum of every monthly partition's total loot (all
        non-hidden drops). Writing an absolute value here — rather than relying on
        the incremental ZINCRBY in `_increment_extra_partitions` — is what keeps
        `leaderboard:all` correct when drops are hidden, edited, or the board is
        rebuilt from scratch.
        """
        pipeline = redis_client.client.pipeline(transaction=True)
        pipeline.set(f"player:{player_id}:all:total_loot", all_time_total)
        if all_time_total > 0:
            pipeline.zadd("leaderboard:all", {player_id: all_time_total})
        else:
            pipeline.zrem("leaderboard:all", player_id)
        pipeline.execute()

    def _rebuild_daily_data(self, player_id: int, daily_partition: str, drops: List[Drop]) -> int:
        """Rebuild Redis data for a specific daily partition. Returns total loot value."""
        # Generate daily keys
        daily_keys = {
            'daily_total_items': f"player:{player_id}:daily:{daily_partition}:total_items",
            'daily_total_loot': f"player:{player_id}:daily:{daily_partition}:total_loot",
            'daily_recent_items': f"player:{player_id}:daily:{daily_partition}:recent_items",
            'daily_drop_history': f"player:{player_id}:daily:{daily_partition}:drop_history",
            'daily_high_value_items': f"player:{player_id}:daily:{daily_partition}:high_value_items"
        }
        
        # Aggregate data for this day
        item_data = {}  # item_id -> (quantity, total_value, drop_count, first_drop, last_drop)
        total_loot = 0
        recent_items_raw = []
        
        for drop in drops:
            total_value = drop.value * drop.quantity
            total_loot += total_value
            drop_timestamp = drop.date_added.strftime('%Y-%m-%d %H:%M:%S')
            
            # Aggregate item data
            if drop.item_id not in item_data:
                item_data[drop.item_id] = (0, 0, 0, drop_timestamp, drop_timestamp)
            
            qty, val, count, first, _ = item_data[drop.item_id]
            item_data[drop.item_id] = (qty + drop.quantity, val + total_value, count + 1, first, drop_timestamp)
            
            if int(drop.value * drop.quantity) > 1000000:
                # Add to recent items
                recent_item_data = {
                    'drop_id': drop.drop_id,
                    'item_id': drop.item_id,
                    'npc_id': drop.npc_id,
                    'value': drop.value,
                    'quantity': drop.quantity,
                    'total_value': total_value,
                    'date_added': drop_timestamp,
                    'daily_partition': daily_partition
                }
                recent_items_raw.append(recent_item_data)
        
        # Use pipeline for atomic updates
        pipeline = redis_client.client.pipeline(transaction=True)
        
        # Set daily total loot
        pipeline.set(daily_keys['daily_total_loot'], total_loot)
        
        # Sort recent items by time
        recent_items_raw.sort(key=lambda x: x['date_added'])
        recent_items = [json.dumps(item) for item in recent_items_raw]
        
        # Set daily item data
        for item_id, (qty, val, count, first, last) in item_data.items():
            item_value = f"{qty},{val},{count},{first},{last}"
            pipeline.hset(daily_keys['daily_total_items'], item_id, item_value)
        
        # Set daily recent items
        if recent_items_raw:
            pipeline.delete(daily_keys['daily_recent_items'])
            pipeline.lpush(daily_keys['daily_recent_items'], *recent_items)
        
        # Set expiration for daily keys (optional - expire after 90 days to save memory)
        expiration_days = 90
        expiration_seconds = expiration_days * 24 * 60 * 60
        for key in daily_keys.values():
            pipeline.expire(key, expiration_seconds)
        
        # Execute all operations
        pipeline.execute()
        
        return total_loot
    
    def generate_loot_leaderboard(self, query: LootLeaderboardQuery) -> Dict:
        """
        Generate a comprehensive loot leaderboard based on query parameters.
        Returns aggregated data for all matching players.
        """
        result = {
            'players': [],
            'total_players': 0,
            'total_value': 0,
            'high_value_items': [],
            'generated_at': datetime.now().isoformat()
        }
        
        # Get player IDs to process
        if query.player_ids:
            player_ids = query.player_ids
        else:
            # Get all players from database
            players = session.query(Player.player_id).all()
            player_ids = [p[0] for p in players]
        
        # Process each player
        for player_id in player_ids:
            player_summary = self._get_player_loot_summary(
                player_id, query.npc_ids, query.start_time, 
                query.end_time, query.min_item_value, query.partition
            )
            
            if player_summary:
                result['players'].append({
                    'player_id': player_summary.player_id,
                    'total_value': player_summary.total_value,
                    'total_drops': player_summary.total_drops,
                    'unique_items': player_summary.unique_items,
                    'unique_npcs': player_summary.unique_npcs,
                    'items': len(player_summary.items),
                    'high_value_items': len(player_summary.high_value_items)
                })
                
                result['total_value'] += player_summary.total_value
                result['high_value_items'].extend(player_summary.high_value_items)
        
        # Sort players by total value
        result['players'].sort(key=lambda x: x['total_value'], reverse=True)
        result['total_players'] = len(result['players'])
        
        # Sort high value items by value
        result['high_value_items'].sort(key=lambda x: x['total_value'], reverse=True)
        
        return result
    
    def _get_player_loot_summary(self, player_id: int, npc_ids: Optional[List[int]] = None,
                                start_time: Optional[datetime] = None, 
                                end_time: Optional[datetime] = None,
                                min_item_value: Optional[int] = None,
                                partition: Optional[int] = None) -> Optional[PlayerLootSummary]:
        """Get comprehensive loot summary for a player"""
        
        if partition is None:
            partition = self._get_partition()
        
        keys = self._get_redis_keys(player_id, partition)
        
        # Get total loot
        total_loot_str = redis_client.get(keys['total_loot'])
        if not total_loot_str:
            return None
        
        total_loot = int(float(total_loot_str))
        
        # Get item data
        items_data = redis_client.client.hgetall(keys['total_items'])
        items = {}
        total_drops = 0
        unique_npcs = set()
        high_value_items = []
        
        for item_id_bytes, item_data_bytes in items_data.items():
            item_id = int(item_id_bytes.decode('utf-8'))
            item_data = item_data_bytes.decode('utf-8').split(',')
            
            if len(item_data) >= 5:
                quantity = int(item_data[0])
                total_value = int(item_data[1])
                drop_count = int(item_data[2])
                first_drop = datetime.strptime(item_data[3], '%Y-%m-%d %H:%M:%S')
                last_drop = datetime.strptime(item_data[4], '%Y-%m-%d %H:%M:%S')
                
                # Apply filters
                if start_time and last_drop < start_time:
                    continue
                if end_time and first_drop > end_time:
                    continue
                
                # Check for high value items
                if min_item_value and total_value >= min_item_value:
                    high_value_items.append({
                        'item_id': item_id,
                        'quantity': quantity,
                        'total_value': total_value,
                        'drop_count': drop_count,
                        'first_drop': first_drop.isoformat(),
                        'last_drop': last_drop.isoformat()
                    })
                
                items[item_id] = PlayerItemData(
                    item_id=item_id,
                    quantity=quantity,
                    total_value=total_value,
                    drop_count=drop_count,
                    first_drop=first_drop,
                    last_drop=last_drop
                )
                
                total_drops += drop_count
        
        return PlayerLootSummary(
            player_id=player_id,
            total_value=total_loot,
            total_drops=total_drops,
            unique_items=len(items),
            unique_npcs=len(unique_npcs),
            items=items,
            high_value_items=high_value_items
        )
    
    def get_player_rank(self, player_id: int, group_id: Optional[int] = None, 
                       partition: Optional[int] = None) -> Optional[Tuple[int, int]]:
        """
        Get a player's rank and total players in ranking.
        Returns (rank, total_players) or None if not found.
        """
        if partition is None:
            partition = self._get_partition()
        
        # Get leaderboard key
        if group_id:
            rank_key = f"leaderboard:{partition}:group:{group_id}"
        else:
            rank_key = f"leaderboard:{partition}"
        
        # Get player's score and rank
        score = redis_client.client.zscore(rank_key, player_id)
        if score is None:
            return None
        
        rank = redis_client.client.zrevrank(rank_key, player_id)
        total_players = redis_client.client.zcard(rank_key)
        
        if rank is None:
            return None
        
        return (int(rank) + 1, total_players)  # Redis ranks are 0-based
    
    def update_leaderboards(self, player_id: int, total_value: int,
                           partition: Optional[int] = None, group_ids: Optional[List[int]] = None,
                           group_deductions: Optional[Dict[int, int]] = None):
        """Update leaderboards for a player.

        ``group_deductions``: per-group GP to subtract from ``total_value`` on
        THAT group's board (manual_submission_policy exclusions,
        drop_group_moderation) — the global board always gets the full total.
        """
        if partition is None:
            partition = self._get_partition()

        pipeline = redis_client.client.pipeline(transaction=True)

        # Update global leaderboard
        global_key = f"leaderboard:{partition}"
        pipeline.zadd(global_key, {player_id: total_value})

        # Update group leaderboards
        if group_ids:
            for group_id in group_ids:
                group_key = f"leaderboard:{partition}:group:{group_id}"
                group_value = max(total_value - (group_deductions or {}).get(group_id, 0), 0)
                pipeline.zadd(group_key, {player_id: group_value})
                print(f"Updated group leaderboard {group_id} for player {player_id} with value {group_value:,}")

        pipeline.execute()

    def add_split_credit(self, player_id: int, split_value: int, partition: int,
                         group_id: int, world_type: str = "main") -> None:
        """
        Atomically adjust a player's score in a single group leaderboard by split_value.

        Pass a positive delta to credit a split participant, or a negative delta
        (split_value - full_value) to bring the drop receiver's group score down
        from the full drop value to their equal share.

        Only the group leaderboard sorted set is touched — global leaderboard and
        individual player:*:total_loot keys are never modified.
        """
        prefix = "seasonal:" if world_type == "seasonal" else ""
        key = f"{prefix}leaderboard:{partition}:group:{group_id}"
        redis_client.client.zincrby(key, split_value, player_id)

# Global instance
loot_tracker = RedisLootTracker()


def get_player_current_month_total(player_id: int) -> int:
    """Fetch the player's monthly total loot from Redis computed by redis_updates."""
    try:
        now = datetime.now()
        partition = now.year * 100 + now.month
        key = f"player:{player_id}:{partition}:total_loot"
        total_str = redis_client.get(key)
        if total_str is None:
            # Fallback to global leaderboard score if key missing
            score = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
            return int(float(score)) if score is not None else 0
        return int(float(total_str))
    except Exception:
        return 0
    
def get_player_list_loot_sum(player_ids: List[int]):
    try:
        group_total = 0
        for player_id in player_ids:
            group_total += get_player_current_month_total(player_id)
        return group_total
    except Exception:
        return 0

# Convenience functions for backward compatibility
def add_to_player(
    player: Player,
    drop: Drop,
    world_type: str = "main",
    item_name: str | None = None,
    npc_name: str | None = None,
    exclude_group_ids: set | None = None,
) -> bool:
    """Add a drop to a player's Redis cache"""
    return loot_tracker.add_to_player(
        player, drop, world_type=world_type, item_name=item_name, npc_name=npc_name,
        exclude_group_ids=exclude_group_ids,
    )


def add_split_credit(player_id: int, split_value: int, partition: int,
                     group_id: int, world_type: str = "main") -> None:
    """Adjust a player's score in a single group leaderboard by split_value."""
    return loot_tracker.add_split_credit(player_id, split_value, partition, group_id, world_type)

def force_update_player(player_id: int, session_to_use=None) -> bool:
    """Force update a player's Redis cache from database"""
    return loot_tracker.force_update_player(player_id, session_to_use)

def generate_loot_leaderboard(query: LootLeaderboardQuery) -> Dict:
    """Generate loot leaderboard based on query parameters"""
    return loot_tracker.generate_loot_leaderboard(query)


def sync_leaderboards_from_redis(partition: Optional[int] = None, session_to_use=None) -> int:
    """
    Sync leaderboards from existing Redis total_loot data.
    
    This is useful when:
    - Leaderboards are empty but player total_loot keys exist
    - After Redis restart where only some keys were persisted
    - Initial migration after adding the leaderboard update fix
    
    Returns the number of players synced.
    """
    if partition is None:
        now = datetime.now()
        partition = now.year * 100 + now.month
    
    if session_to_use is None:
        session_to_use = session
    
    synced_count = 0
    
    # Get all players with their groups
    players = session_to_use.query(Player).options(joinedload(Player.groups)).all()
    
    print(f"Syncing leaderboards for {len(players)} players in partition {partition}...")
    
    pipeline = redis_client.client.pipeline(transaction=True)
    batch_count = 0
    
    for player in players:
        # Get player's total loot from Redis
        total_loot_key = f"player:{player.player_id}:{partition}:total_loot"
        total_loot = redis_client.get(total_loot_key)
        
        if total_loot is None:
            continue
        
        try:
            total_loot_value = int(float(total_loot))
        except (ValueError, TypeError):
            continue
        
        if total_loot_value <= 0:
            continue
        
        # Update global leaderboard
        global_key = f"leaderboard:{partition}"
        pipeline.zadd(global_key, {player.player_id: total_loot_value})
        
        # Update group leaderboards
        for group in player.groups:
            group_key = f"leaderboard:{partition}:group:{group.group_id}"
            pipeline.zadd(group_key, {player.player_id: total_loot_value})
        
        synced_count += 1
        batch_count += 1
        
        # Execute in batches of 100 to avoid huge pipelines
        if batch_count >= 100:
            pipeline.execute()
            pipeline = redis_client.client.pipeline(transaction=True)
            batch_count = 0
            print(f"Synced {synced_count} players...")
    
    # Execute remaining
    if batch_count > 0:
        pipeline.execute()
    
    print(f"Leaderboard sync complete. Synced {synced_count} players.")
    return synced_count


class BulkRedisUpdater:
    """
    Class for performing bulk Redis operations on multiple players.
    Handles batch processing with progress tracking and error handling.
    """
    
    def __init__(self, batch_size: int = 50, max_workers: int = 5, player_fetch_chunk_size: int = 50000):
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        if player_fetch_chunk_size <= 0:
            raise ValueError("player_fetch_chunk_size must be greater than zero")

        self.batch_size = batch_size
        self.max_workers = max_workers
        self.player_fetch_chunk_size = player_fetch_chunk_size
        self.loot_tracker = loot_tracker

    def _stream_player_ids_for_partition(
        self,
        session_to_use,
        partition: int,
        chunk_size: int
    ) -> Iterable[int]:
        """
        Stream unique player IDs for a given partition in chunks to avoid long-running queries.
        """
        last_player_id = -1
        prev_emitted = None

        stmt = text(
            """
            SELECT drops.player_id
            FROM drops
            WHERE drops.`partition` = :partition_value
              AND drops.player_id > :last_player_id
            ORDER BY drops.player_id
            LIMIT :chunk_size
            """
        )

        while True:
            result = session_to_use.execute(
                stmt,
                {
                    "partition_value": partition,
                    "last_player_id": last_player_id,
                    "chunk_size": chunk_size,
                },
            )
            rows = result.fetchall()
            result.close()

            if not rows:
                break

            last_player_id = rows[-1][0]

            for row in rows:
                player_id = row[0]
                if prev_emitted == player_id:
                    continue
                prev_emitted = player_id
                yield player_id
        
    def iter_players_with_current_month_drops(
        self,
        session_to_use=None,
        chunk_size: Optional[int] = None
    ) -> Iterable[int]:
        """
        Stream player IDs who have received drops in the current calendar month.
        """
        if session_to_use is None:
            session_to_use = session

        effective_chunk_size = chunk_size or self.player_fetch_chunk_size

        try:
            current_partition = self.loot_tracker._get_partition()
            yield from self._stream_player_ids_for_partition(
                session_to_use,
                current_partition,
                effective_chunk_size
            )
        except Exception as e:
            print(f"Error streaming players with current month drops: {e}")
            return

    def get_players_with_current_month_drops(self, session_to_use=None, chunk_size: Optional[int] = None) -> List[int]:
        """
        Get all player IDs who have received drops in the current calendar month.
        
        Args:
            session_to_use: Optional database session to use
            
        Returns:
            List[int]: List of player IDs with drops in current month
        """
        if session_to_use is None:
            session_to_use = session

        effective_chunk_size = chunk_size or self.player_fetch_chunk_size

        try:
            return list(
                self.iter_players_with_current_month_drops(
                    session_to_use=session_to_use,
                    chunk_size=effective_chunk_size
                )
            )
        except Exception as e:
            print(f"Error getting players with current month drops: {e}")
            return []
    
    def force_update_all_current_month_players(self, session_to_use=None, 
                                             progress_callback=None) -> Dict:
        """
        Force update Redis cache for all players who have drops in the current month.
        
        Args:
            session_to_use: Optional database session to use for getting player list only
            progress_callback: Optional callback function for progress updates
            
        Returns:
            Dict: Summary of the bulk update operation
        """
        start_time = datetime.now()
        result = {
            'started_at': start_time.isoformat(),
            'total_players': 0,
            'successful_updates': 0,
            'failed_updates': 0,
            'errors': [],
            'completed_at': None,
            'duration_seconds': 0
        }
        
        # Use a fresh session just for getting the player list
        list_session = None
        try:
            if session_to_use is None:
                from db.models.base import get_fresh_session
                list_session = get_fresh_session()
                session_for_list = list_session
            else:
                session_for_list = session_to_use
            
            # Get all players with current month drops
            player_ids = self.get_players_with_current_month_drops(session_for_list)
            result['total_players'] = len(player_ids)
            
            if not player_ids:
                print("No players found with drops in current month")
                result['completed_at'] = datetime.now().isoformat()
                result['duration_seconds'] = 0
                return result
            
            print(f"Starting bulk Redis update for {len(player_ids)} players with current month drops")
            
            # Process players in batches
            for i in range(0, len(player_ids), self.batch_size):
                batch = player_ids[i:i + self.batch_size]
                batch_num = (i // self.batch_size) + 1
                total_batches = (len(player_ids) + self.batch_size - 1) // self.batch_size
                
                print(f"Processing batch {batch_num}/{total_batches} ({len(batch)} players)")
                
                # Process each player in the batch with its own fresh session
                for player_id in batch:
                    player_session = None
                    try:
                        # Create a fresh session for each player to avoid transaction issues
                        from db.models.base import get_fresh_session
                        player_session = get_fresh_session()
                        
                        success = self.loot_tracker.force_update_player(player_id, player_session)
                        if success:
                            result['successful_updates'] += 1
                            print(f"✓ Updated player {player_id}")
                        else:
                            result['failed_updates'] += 1
                            error_msg = f"Force update returned False for player {player_id}"
                            result['errors'].append(error_msg)
                            print(f"✗ Failed to update player {player_id}")
                            
                    except Exception as e:
                        result['failed_updates'] += 1
                        error_msg = f"Exception updating player {player_id}: {str(e)}"
                        result['errors'].append(error_msg)
                        print(f"✗ Error updating player {player_id}: {e}")
                    finally:
                        # Always close the player session
                        if player_session:
                            try:
                                player_session.close()
                            except Exception as e:
                                print(f"Warning: Error closing session for player {player_id}: {e}")
                
                # Progress callback
                if progress_callback:
                    progress = {
                        'batch': batch_num,
                        'total_batches': total_batches,
                        'completed_players': result['successful_updates'] + result['failed_updates'],
                        'total_players': result['total_players'],
                        'successful': result['successful_updates'],
                        'failed': result['failed_updates']
                    }
                    progress_callback(progress)
                
                # Small delay between batches to avoid overwhelming the system
                if batch_num < total_batches:
                    time.sleep(0.5)
            
            end_time = datetime.now()
            result['completed_at'] = end_time.isoformat()
            result['duration_seconds'] = (end_time - start_time).total_seconds()
            
            print(f"\nBulk update completed:")
            print(f"  Total players: {result['total_players']}")
            print(f"  Successful: {result['successful_updates']}")
            print(f"  Failed: {result['failed_updates']}")
            print(f"  Duration: {result['duration_seconds']:.2f} seconds")
            
            if result['errors']:
                print(f"  Errors encountered: {len(result['errors'])}")
                for error in result['errors'][:5]:  # Show first 5 errors
                    print(f"    - {error}")
                if len(result['errors']) > 5:
                    print(f"    ... and {len(result['errors']) - 5} more errors")
            
            return result
            
        except Exception as e:
            end_time = datetime.now()
            result['completed_at'] = end_time.isoformat()
            result['duration_seconds'] = (end_time - start_time).total_seconds()
            error_msg = f"Fatal error in bulk update: {str(e)}"
            result['errors'].append(error_msg)
            print(f"Fatal error in bulk update: {e}")
            return result
        finally:
            # Close the list session if we created it
            if list_session:
                try:
                    list_session.close()
                except Exception as e:
                    print(f"Warning: Error closing list session: {e}")
    
    def force_update_specific_players(self, player_ids: List[int], 
                                    session_to_use=None, progress_callback=None) -> Dict:
        """
        Force update Redis cache for specific players.
        
        Args:
            player_ids: List of player IDs to update
            session_to_use: Optional database session to use (ignored, fresh sessions created per player)
            progress_callback: Optional callback function for progress updates
            
        Returns:
            Dict: Summary of the bulk update operation
        """
        start_time = datetime.now()
        result = {
            'started_at': start_time.isoformat(),
            'total_players': len(player_ids),
            'successful_updates': 0,
            'failed_updates': 0,
            'errors': [],
            'completed_at': None,
            'duration_seconds': 0
        }
        
        try:
            print(f"Starting bulk Redis update for {len(player_ids)} specific players")
            
            # Process players in batches
            for i in range(0, len(player_ids), self.batch_size):
                batch = player_ids[i:i + self.batch_size]
                batch_num = (i // self.batch_size) + 1
                total_batches = (len(player_ids) + self.batch_size - 1) // self.batch_size
                
                print(f"Processing batch {batch_num}/{total_batches} ({len(batch)} players)")
                
                # Process each player in the batch with its own fresh session
                for player_id in batch:
                    player_session = None
                    try:
                        # Create a fresh session for each player to avoid transaction issues
                        from db.models.base import get_fresh_session
                        player_session = get_fresh_session()
                        
                        success = self.loot_tracker.force_update_player(player_id, player_session)
                        if success:
                            result['successful_updates'] += 1
                            print(f"✓ Updated player {player_id}")
                        else:
                            result['failed_updates'] += 1
                            error_msg = f"Force update returned False for player {player_id}"
                            result['errors'].append(error_msg)
                            print(f"✗ Failed to update player {player_id}")
                            
                    except Exception as e:
                        result['failed_updates'] += 1
                        error_msg = f"Exception updating player {player_id}: {str(e)}"
                        result['errors'].append(error_msg)
                        print(f"✗ Error updating player {player_id}: {e}")
                    finally:
                        # Always close the player session
                        if player_session:
                            try:
                                player_session.close()
                            except Exception as e:
                                print(f"Warning: Error closing session for player {player_id}: {e}")
                
                # Progress callback
                if progress_callback:
                    progress = {
                        'batch': batch_num,
                        'total_batches': total_batches,
                        'completed_players': result['successful_updates'] + result['failed_updates'],
                        'total_players': result['total_players'],
                        'successful': result['successful_updates'],
                        'failed': result['failed_updates']
                    }
                    progress_callback(progress)
                
                # Small delay between batches
                if batch_num < total_batches:
                    time.sleep(0.5)
            
            end_time = datetime.now()
            result['completed_at'] = end_time.isoformat()
            result['duration_seconds'] = (end_time - start_time).total_seconds()
            
            print(f"\nBulk update completed:")
            print(f"  Total players: {result['total_players']}")
            print(f"  Successful: {result['successful_updates']}")
            print(f"  Failed: {result['failed_updates']}")
            print(f"  Duration: {result['duration_seconds']:.2f} seconds")
            
            return result
            
        except Exception as e:
            end_time = datetime.now()
            result['completed_at'] = end_time.isoformat()
            result['duration_seconds'] = (end_time - start_time).total_seconds()
            error_msg = f"Fatal error in bulk update: {str(e)}"
            result['errors'].append(error_msg)
            print(f"Fatal error in bulk update: {e}")
            return result

# Global instance for bulk operations
bulk_updater = BulkRedisUpdater()

# Convenience functions for bulk operations
def force_update_all_current_month_players(session_to_use=None, progress_callback=None) -> Dict:
    """Force update all players with drops in current month"""
    return bulk_updater.force_update_all_current_month_players(session_to_use, progress_callback)

def force_update_specific_players(player_ids: List[int], session_to_use=None, progress_callback=None) -> Dict:
    """Force update specific players"""
    return bulk_updater.force_update_specific_players(player_ids, session_to_use, progress_callback)

def get_players_with_current_month_drops(session_to_use=None) -> List[int]:
    """Get all player IDs with drops in current month"""
    return bulk_updater.get_players_with_current_month_drops(session_to_use)


if __name__ == "__main__":
    # Example usage
    print("Testing bulk Redis updater...")
    
    # Get players with current month drops
    current_month_players = get_players_with_current_month_drops()
    print(f"Found {len(current_month_players)} players with current month drops")
    
    # Example: Update all current month players
    # result = force_update_all_current_month_players()
    
    # Example: Update specific players
    # result = force_update_specific_players([795, 123, 456])