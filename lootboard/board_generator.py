"""One redraw pass over the group lootboards.

Run as a fresh subprocess by ``lootboard/_board_generator.py`` (every minute,
or sooner when an instant redraw is waiting). Which boards are due, and why,
is decided by ``lootboard/schedule.py``: each group's tier sets a scheduled
interval, and Sponsor/Patron-style tiers also redraw after a drop
notification. The core bot posts whichever boards come out newer than the
ones it last posted, so drawing a board here is what updates Discord.
"""
from datetime import datetime
import asyncio
import time

from lootboard import generator
from lootboard import schedule
from db.models import Group, Session

# Bound one pass so it stays well inside the driver's 10-minute timeout.
# Instant redraws always go first and are never deferred; scheduled ones are
# taken most-overdue first, and anything past the cap waits for the next pass
# (a minute later). ~300 free boards every 30 minutes is ~10 a minute.
SCHEDULED_PER_RUN = 40


async def update_specific_board(group_id: int, force: bool = False):
    """Redraw one group's board now if it is due (or unconditionally with
    ``force``)."""
    try:
        with Session() as group_session:
            group = group_session.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                print(f"Group {group_id} not found")
                return
            if not group.guild_id or group.guild_id == 0:
                print(f"Group {group_id} has no valid guild_id")
                return
            group_data = {'group_name': group.group_name, 'wom_id': group.wom_id}
            policy = schedule.load_policies(group_session, [group_id])[group_id]

        if not force and not schedule.scheduled_due(policy, schedule.board_mtime(group_id), time.time()):
            print(f"Skipping group {group_id}: not due (every {policy.refresh_minutes} min)")
            return
        await _draw(group_id, group_data['group_name'], group_data['wom_id'])
    except Exception as e:
        print(f"Exception in update_specific_board({group_id}): {e}")


async def _draw(group_id: int, group_name: str, wom_id) -> bool:
    try:
        with Session() as gen_session:
            new_path = await generator.generate_server_board_temporary(
                group_id=group_id, wom_group_id=wom_id, session_to_use=gen_session
            )
        print(f"Board generated for {group_name}: {new_path}")
        return True
    except Exception as e:
        print(f"Error generating board for group {group_id}: {e}")
        return False


async def update_boards():
    try:
        with Session() as session:
            rows = session.query(Group.group_id, Group.group_name, Group.guild_id, Group.wom_id).all()
            # Every group id that still exists, for the stale-entry prune below
            # (independent of the guild_id filter: an unlinked group still exists).
            db_group_ids = {int(r.group_id) for r in rows}
            groups = {
                int(r.group_id): (r.group_name, r.wom_id)
                for r in rows if r.guild_id and r.guild_id != 0
            }
            policies = schedule.load_policies(session, groups.keys())

        now = time.time()
        mtimes = {g: schedule.board_mtime(g) for g in groups}
        try:
            redis = schedule._redis()
            dirty_ready = schedule.ready_dirty_group_ids(redis)
        except Exception as e:
            print(f"Error reading instant lootboard flags (scheduled redraws only): {e}")
            redis, dirty_ready = None, set()

        ids = sorted(groups)
        instant, scheduled, deferred = schedule.plan_pass(
            ids, policies, dirty_ready, now, mtimes, SCHEDULED_PER_RUN
        )
        print(
            f"Found {len(groups)} groups: {len(instant)} instant, {len(scheduled)} scheduled "
            f"due (deferring {deferred})"
        )

        for group_id in instant:
            # The cooldown claim is the de-duplication: if another pass got
            # here first, skip.
            try:
                if not schedule.claim_instant(group_id, redis):
                    continue
            except Exception as e:
                print(f"Error claiming instant redraw for group {group_id}: {e}")
                continue
            name, wom_id = groups[group_id]
            await _draw(group_id, name, wom_id)

        for group_id in scheduled:
            name, wom_id = groups[group_id]
            if redis is not None and policies[group_id].instant:
                try:
                    schedule.consume_for_scheduled(group_id, redis)
                except Exception:
                    pass
            await _draw(group_id, name, wom_id)

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
        print("Finished lootboard pass.")


async def update_event_team_boards():
    """Per-team event lootboards (lootboard/team_boards.py), piggy-backing on
    this subprocess's pass. They throttle themselves by PNG mtime.

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
    print("Starting lootboard pass")
    await update_boards()
    await update_event_team_boards()

if __name__ == "__main__":
    asyncio.run(startup())
    exit()
