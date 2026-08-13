from datetime import datetime, timedelta

from sqlalchemy import text
from lootboard import generator
from db.models import Group, Session, XenforoSession
import asyncio
import os
import time

last_board_updates = {}

# Non-premium boards refresh at most once per this many minutes. The state
# lives in the mtime of the generated lootboard.png — this module runs in a
# fresh subprocess every cycle, so in-memory dicts reset between runs.
NON_PREMIUM_REFRESH_MINUTES = 59
# Cap non-premium regenerations per run so the hourly refresh is spread
# across 2-minute cycles instead of one long run that risks the supervisor
# timeout (30 runs/hour x 25 covers far more than the ~220 tracked groups).
NON_PREMIUM_PER_RUN = 25


def board_age_seconds(group_id: int) -> float:
    """Seconds since this group's current board was last generated."""
    path = f"/store/droptracker/disc/static/assets/img/clans/{group_id}/lb/lootboard.png"
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return float("inf")

async def lootboard_update_loop():
    print("Starting lootboard update loop")
    try:
        await update_boards()
    except Exception as e:
        print(f"Exception in lootboard_update_loop: {e}")
    # Wait 2 minutes before the next iteration
    return True

def get_fresh_session():
    """Create a new database session - no global session management"""
    return Session()

def get_fresh_xenforo_session():
    """Create a new XenForo database session - no global session management"""
    return XenforoSession()




async def update_specific_board(group_id: int, force: bool = False):
    try:
        # Fetch the group with a short-lived session
        group_data = None
        with Session() as group_session:
            group = group_session.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                print(f"Group {group_id} not found")
                return
            if not group.guild_id or group.guild_id == 0:
                print(f"Group {group_id} has no valid guild_id")
                return
            # Store group data outside the session
            group_data = {
                'group_name': group.group_name,
                'wom_id': group.wom_id
            }

        # Determine premium status with a separate short-lived session
        is_premium = False
        if group_id == 2:
            is_premium = True
        else:
            with get_fresh_xenforo_session() as xf_session:
                premium_status = xf_session.execute(
                    text("SELECT * FROM xf_user_upgrade_active WHERE group_id = :group_id"),
                    {"group_id": group_id}
                ).first()
                if premium_status:
                    is_premium = True

        # Non-premium throttling unless forced
        if not is_premium and not force:
            if group_id not in last_board_updates:
                last_board_updates[group_id] = datetime.now() - timedelta(days=7)
            if last_board_updates[group_id] > datetime.now() - timedelta(minutes=59):
                print(f"Skipping group {group_id}: within 60-minute window for non-premium")
                return

        # Ensure destination directory exists
        save_dir = f"/store/droptracker/disc/static/assets/img/clans/{group_id}/lb"
        if not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)

        # Generate using a fresh session that will be closed quickly
        print("Generating board for group:", group_id, "using a fresh session...")
        try:
            with Session() as gen_session:
                new_path = await generator.generate_server_board_temporary(
                    group_id=group_id,
                    wom_group_id=group_data['wom_id'],
                    session_to_use=gen_session
                )
            print(f"Board generated for {group_data['group_name']}")
            print(f"Board path: {new_path}")
            if not is_premium:
                last_board_updates[group_id] = datetime.now()
        except Exception as e:
            print(f"Error generating board for group {group_id}: {e}")
    except Exception as e:
        print(f"Exception in update_specific_board({group_id}): {e}")
    finally:
        print("Finished cycle and closed sessions.")

async def update_boards():
    try:
        # Get all groups with a dedicated session that's immediately closed
        original_groups = []
        with Session() as temp_session:
            try:
                original_groups = temp_session.query(Group).all()
            except Exception as e:
                temp_session.rollback()
                try:
                    original_groups = temp_session.query(Group).all()
                except Exception as e:
                    print(f"Error getting groups: {e}")
                    return
            
        # Every group id that still exists, for the stale-entry prune below
        # (independent of the guild_id filter — an unlinked group still exists).
        db_group_ids = {g.group_id for g in original_groups}

        # Create a clean list of groups outside the session
        groups = []
        for g in original_groups:
            if g.guild_id and g.guild_id != 0:
                temp_group = Group(group_name=g.group_name, guild_id=g.guild_id, wom_id=g.wom_id)
                temp_group.group_id = g.group_id
                groups.append(temp_group)
                
        # Premium = live paid subscription pool (group_subscriptions is the
        # canonical source post-XenForo-cutover). Group 2 is the global board.
        premium_ids = {2}
        try:
            from db.entitlements import effective_group_tiers
            with Session() as ent_session:
                premium_ids |= {
                    int(gid) for gid in
                    effective_group_tiers(ent_session, [g.group_id for g in groups])
                }
        except Exception as e:
            print(f"Error resolving premium tiers (non-premium cadence for all): {e}")

        # Premium groups regenerate every run; non-premium at most hourly,
        # most-stale first and capped per run so a killed/slow run can never
        # starve the same groups twice.
        premium_groups = [g for g in groups if g.group_id in premium_ids]
        stale = [
            (g, board_age_seconds(g.group_id))
            for g in groups if g.group_id not in premium_ids
        ]
        stale = [(g, age) for g, age in stale if age >= NON_PREMIUM_REFRESH_MINUTES * 60]
        stale.sort(key=lambda pair: pair[1], reverse=True)
        deferred = max(0, len(stale) - NON_PREMIUM_PER_RUN)
        todo = premium_groups + [g for g, _ in stale[:NON_PREMIUM_PER_RUN]]
        print(
            f"Found {len(groups)} groups: {len(premium_groups)} premium, "
            f"{len(stale)} stale non-premium (processing {len(todo)}, deferring {deferred})"
        )
        for group in todo:
            try:
                if not os.path.exists(f"/store/droptracker/disc/static/assets/img/clans/{group.group_id}/lb"):
                    os.makedirs(f"/store/droptracker/disc/static/assets/img/clans/{group.group_id}/lb")
                
                # Create a completely new session for each group that will be closed quickly
                print("Generating board for group:", group.group_id, "using a fresh session...")
                try:
                    with Session() as group_session:
                        new_path = await generator.generate_server_board_temporary(group_id=group.group_id, wom_group_id=group.wom_id, session_to_use=group_session)
                    print(f"Board generated for {group.group_name}")
                    print(f"Board path: {new_path}")
                except Exception as e:
                    print(f"Error generating board for group {group.group_id}: {e}")
                    # No need to explicitly rollback - the context manager will handle it
            except Exception as e:
                print(f"Error in group processing for {group.group_id}: {e}")
                continue
        
        # Prune deleted groups from the precomputed group leaderboard. The
        # per-group zadds during generation never remove members, so a group
        # deleted from the DB would otherwise stay on the website's group
        # leaderboard forever (web_api reads gleaderboard:{partition} first).
        try:
            from utils.redis import RedisClient
            partition = datetime.now().year * 100 + datetime.now().month
            key = f"gleaderboard:{partition}"
            redis_client = RedisClient()
            members = redis_client.client.zrange(key, 0, -1)
            stale = [m for m in members if int(m) not in db_group_ids]
            if stale:
                redis_client.client.zrem(key, *stale)
                print(f"Pruned deleted groups from {key}: {[int(m) for m in stale]}")
        except Exception as e:
            print(f"Error pruning deleted groups from gleaderboard: {e}")

    except Exception as e:
        print(f"Error updating boards: {e}")
    finally:
        print("Finished cycle and closed sessions.")
    
    print("Completed lootboard update loop. Waiting 2 minutes to continue")

async def update_event_team_boards():
    """Per-team event lootboards (lootboard/team_boards.py), piggy-backing on
    this subprocess's 2-minute cadence.

    Gated on the EVENT_TEAM_LOOTBOARDS env flag (off by default) and fully
    isolated: any failure in here must never affect the group boards above,
    which have already been written by the time this runs."""
    try:
        from lootboard.team_boards import feature_enabled, sweep_team_boards

        if not feature_enabled():
            return
        written = await sweep_team_boards()
        print(f"Generated {len(written)} event team board(s)")
    except Exception as e:
        print(f"Error updating event team boards: {e}")


async def startup():
    print("Starting lootboard update loop")
    await lootboard_update_loop()
    await update_event_team_boards()

if __name__ == "__main__":
    asyncio.run(startup())
    exit()
