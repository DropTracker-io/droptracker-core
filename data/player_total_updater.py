### This separate process is used to run player update cycles in the background, 
# as opposed to holding up the main process's ability to respond to requests, etc.

import asyncio
from datetime import datetime, timedelta
import time
import signal
import sys
import aiohttp
import quart
from quart import Quart, request
import os
from dotenv import load_dotenv
import logging
from monitor.sdnotifier import SystemdWatchdog

from sqlalchemy import func
# Do NOT import the module-global scoped `session` here: any read on it
# autobegins a transaction that this long-lived service never commits, holding
# an idle InnoDB transaction (and its metadata locks) open for the whole
# service lifetime (2026-07-16: 20h idle trx blocked an ALTER on web_events).
# Every query in this process must use a short-lived `with Session()` block.
from db.models import Group, LBUpdate, Player, User, Drop, Session
from services import redis_updates
# from db.update_player_total import update_player_in_redis
#from db.update_player_total import update_player_in_redis
from lootboard.generator import generate_server_board
from utils.github import GithubPagesUpdater

from utils.redis import redis_client

from db.app_logger import AppLogger

app_logger = AppLogger()

# Dictionary to track recently updated players: {player_id: timestamp}
recently_updated = {}
# Cooldown period in seconds (60 minutes)
UPDATE_COOLDOWN = 3600

# Configure logging
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

from utils.sentry import init_sentry
init_sentry("droptracker-player-updates")

# Create the Quart application
app = Quart(__name__)

# Global variables for systemd watchdog
watchdog = None
shutdown_event = asyncio.Event()

# Health check function for systemd watchdog
async def health_check():
    """Lightweight health check for the player update service"""
    try:
        # Basic health check - service is running if we get here
        # Don't do blocking operations in health check to avoid watchdog timeouts
        return True
    except Exception as e:
        print(f"Health check failed: {e}")
        return False

# Signal handlers for graceful shutdown
def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown"""
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)

def delete_player_keys(player_id, batch_size=100):
    """
    Delete player keys in batches to avoid blocking.
    """
    pattern = f"player:{player_id}:*"
    keys = []
    for key in redis_client.client.scan_iter(pattern, count=batch_size):
        keys.append(key)
        if len(keys) >= batch_size:
            redis_client.client.delete(*keys)
            keys = []
    if keys:
        redis_client.client.delete(*keys)   

async def send_watchdog_heartbeat():
    """Send a manual watchdog heartbeat"""
    global watchdog
    if watchdog and watchdog.notifier:
        try:
            watchdog.notifier.notify("WATCHDOG=1")
            print("Sent manual watchdog heartbeat")
        except Exception as e:
            print(f"Failed to send watchdog heartbeat: {e}")

# Define routes
@app.route('/')
async def index():
    return "Player Update Service is running!"

@app.route('/health')
async def health_check_route():
    return {"status": "healthy"}

def _requeue_failed_player(player_id: int) -> None:
    """Advance a player's date_updated after a failed force-update so it rotates
    to the BACK of the stale-player queue.

    force_update_player() only advances date_updated on success, so a player
    whose update keeps failing (e.g. one whose drops query times out) stays at
    the head of the "date_updated oldest" queue and is retried every ~30s
    forever — starving the other ~11k stale players and logging a timeout every
    cycle. Bumping the timestamp here lets the queue make forward progress; the
    player is retried on the normal 14-day cadence. Uses its own short-lived
    session so it never touches the (possibly poisoned) update session.
    """
    try:
        with Session() as s:
            s.query(Player).filter(Player.player_id == player_id).update(
                {Player.date_updated: datetime.now()}, synchronize_session=False
            )
            s.commit()
    except Exception as e:
        print(f"Failed to requeue player {player_id} after update failure: {e}")


async def update_players():
    """Enhanced update_players with watchdog notifications"""
    global watchdog

    while not shutdown_event.is_set():
        print("Player update loop beginning...")
        cycle_start_time = time.time()

        try:
            # Send watchdog heartbeat at start of cycle
            await send_watchdog_heartbeat()

            # Select only the stalest handful of player IDs at the DB (ORDER BY
            # + LIMIT), instead of materialising every stale Player and slicing
            # in Python. Ordering by date_updated makes "oldest first" explicit
            # and, combined with _requeue_failed_player, guarantees the queue
            # advances even when a given player can't be updated.
            with Session() as list_session:
                stale_rows = (
                    list_session.query(Player.player_id)
                    .filter(Player.date_updated < datetime.now() - timedelta(days=14))
                    .order_by(Player.date_updated.asc())
                    .limit(2)
                    .all()
                )
            player_ids = [row[0] for row in stale_rows]

            print(f"Selected {len(player_ids)} stalest player(s) to update this iteration...")

            if not player_ids:
                print("No players to update")
                await asyncio.sleep(30)
                continue

            for i, player_id in enumerate(player_ids):
                try:
                    player_start_time = time.time()
                    print(f"Updating player {player_id} ({i+1}/{len(player_ids)})")

                    # Send watchdog heartbeat before starting player update
                    await send_watchdog_heartbeat()

                    # Run the player update in a thread to avoid blocking, with a
                    # FRESH session created inside that thread. A session must not
                    # be shared across executor threads (see npc_totals_loop), and
                    # a per-player session means one player's failure/timeout can
                    # never leave a poisoned transaction for the next player.
                    def update_player_sync(pid=player_id):
                        with Session() as player_session:
                            try:
                                return redis_updates.force_update_player(
                                    player_id=pid,
                                    session_to_use=player_session,
                                )
                            except Exception:
                                player_session.rollback()
                                raise

                    # Execute the blocking operation in a thread
                    loop = asyncio.get_event_loop()
                    update_result = await loop.run_in_executor(None, update_player_sync)

                    player_elapsed = time.time() - player_start_time
                    print(f"Updated player {player_id} in {player_elapsed:.2f}s - Result: {update_result}")

                    # A False/failed result means date_updated was NOT advanced;
                    # rotate the player to the back of the queue so it can't clog
                    # the head forever. Done off-thread to avoid blocking the loop.
                    if not update_result:
                        await loop.run_in_executor(None, _requeue_failed_player, player_id)

                    # Send another watchdog heartbeat after player update
                    await send_watchdog_heartbeat()

                    # Small delay between players to allow other operations
                    if i < len(player_ids) - 1:
                        await asyncio.sleep(1)

                except Exception as e:
                    print(f"Error updating player {player_id}: {e}")
                    # Still requeue so a persistently-erroring player doesn't stall the queue.
                    await asyncio.get_event_loop().run_in_executor(None, _requeue_failed_player, player_id)
                    app_logger.log(
                        log_type="error",
                        data=f"Error updating player {player_id}: {e}",
                        app_name="player_updates",
                        description="update_players"
                    )
                    continue

        except Exception as e:
            print(f"Error updating players: {e}")
            app_logger.log(
                log_type="error",
                data=f"Error updating players: {e}",
                app_name="player_updates",
                description="update_players"
            )

        cycle_elapsed = time.time() - cycle_start_time
        print(f"Player update loop finished in {cycle_elapsed:.2f}s")

        # Send final watchdog heartbeat for this cycle
        await send_watchdog_heartbeat()

        # Wait with periodic heartbeats during sleep
        await sleep_with_watchdog_heartbeats(30)

async def sleep_with_watchdog_heartbeats(sleep_duration: int):
    """
    Sleep for the specified duration while sending periodic watchdog heartbeats
    and checking for shutdown signals.
    """
    heartbeat_interval = 10  # Send heartbeat every 10 seconds during sleep
    elapsed = 0
    
    while elapsed < sleep_duration and not shutdown_event.is_set():
        sleep_time = min(heartbeat_interval, sleep_duration - elapsed)
        await asyncio.sleep(sleep_time)
        elapsed += sleep_time
        
        # Send heartbeat if we're still waiting
        if elapsed < sleep_duration and not shutdown_event.is_set():
            await send_watchdog_heartbeat()

@app.route('/update', methods=['POST'])
async def update():
    data = await request.get_json()
    player_id = data.get('player_id')
    force_update = True
    print(f"Received update request for player {player_id}. Force update: {force_update}")
    
    # Send watchdog heartbeat at start of manual update
    await send_watchdog_heartbeat()
    
    # Check if player was recently updated
    current_time = time.time()
    if player_id in recently_updated:
        time_since_update = current_time - recently_updated[player_id]
        if time_since_update < UPDATE_COOLDOWN:
            # Within cooldown: a full Redis rebuild ran <1h ago (recorded in the
            # in-memory recently_updated map), so skip the redundant rebuild.
            #
            # Do NOT bump player.date_updated here. That column is the stale-player
            # sweep's "oldest first" ordering key (see update_players / ORDER BY
            # date_updated ASC), and only a real rebuild may advance it — on success
            # below, or via _requeue_failed_player after a failure. Bumping it on a
            # skip would push the player to the back of the sweep queue on every
            # /update call, so an intake-time Redis-write miss (drop in MySQL but
            # never folded into Redis) would never self-heal via the periodic
            # refresh. The cooldown itself lives entirely in recently_updated, so
            # skipping the bump leaves it fully intact.
            minutes_ago = int(time_since_update / 60)
            return {"status": "skipped", "reason": f"Updated {minutes_ago} minutes ago"}
    
    with Session() as session:
        try:
            print("Attempting to get player...")
            player = session.query(Player).filter(Player.player_id == player_id).first()
            if player:
                print("Player found, attempting to update using optimized method...")

                # Send heartbeat before starting update
                await send_watchdog_heartbeat()

                # Run the update in a thread to avoid blocking, with a FRESH
                # session created inside that thread. Never share this handler's
                # session with the worker: for a very large account the rebuild
                # takes minutes, and if the HTTP client cancels (times out), the
                # async side would close the session while the worker is still
                # inside session.commit() -> IllegalStateChangeError. A private
                # worker session makes cancellation harmless.
                def update_player_sync():
                    with Session() as worker_session:
                        try:
                            return redis_updates.force_update_player(player_id, worker_session)
                        except Exception:
                            worker_session.rollback()
                            raise

                loop = asyncio.get_event_loop()
                updated = await loop.run_in_executor(None, update_player_sync)

                # Send heartbeat after update
                await send_watchdog_heartbeat()

                print("Returned:", updated)
                if updated and updated == True:
                    # Record the update time
                    recently_updated[player_id] = current_time
                    player.date_updated = datetime.now()
                    session.commit()
                    print("Updated player properly.")
                    return {"status": "updated"}
                else:
                    print("Didn't update player properly.")
                    return {"status": "failed"}
            else:
                print("Player not found.")
                return {"status": "player not found"}
        except Exception as e:
            print(f"Error in manual update: {e}")
            session.rollback()
            return {"status": "failed", "error": str(e)}

async def _run_periodic_refold(module, label: str) -> None:
    """Day-chunk ABSOLUTE re-fold of the current partition(s), capped at the
    tailer pointer, to heal drops the additive tailer skipped (auto-increment
    commit-order gaps — see <module>.refold_day).

    Runs in the SAME loop as the tailer (never concurrently with it), so the
    rollup has a single writer and the pointer is stable for the whole pass.
    Drives one day per executor call, sending watchdog heartbeats *while* each
    day's statement runs so a heavy day can't trip WatchdogSec (30s)."""
    cap = module.get_pointer()
    if not cap:
        return  # tailer not seeded yet; nothing folded, nothing to re-fold
    loop = asyncio.get_event_loop()
    touched = 0

    for partition, day_start, day_end in module.refold_plan():
        def _do(p=partition, a=day_start, b=day_end):
            with Session() as s:
                try:
                    return module.refold_day(s, p, a, b, cap)
                except Exception:
                    s.rollback()
                    raise

        for attempt in range(3):
            fut = loop.run_in_executor(None, _do)
            try:
                # Keep the watchdog fed while the day's INSERT..SELECT runs:
                # wait returns the instant the day finishes, else every 10s
                # (well inside WatchdogSec=30) so a heavy day can't trip it.
                while True:
                    done, _ = await asyncio.wait({fut}, timeout=10)
                    await send_watchdog_heartbeat()
                    if done:
                        break
                touched += await fut
                break
            except Exception as e:
                if attempt < 2 and any(tok in str(e) for tok in ("1205", "Lock wait", "Deadlock")):
                    print(f"{label}: re-fold {day_start} lock contention, retry {attempt + 1}")
                    await sleep_with_watchdog_heartbeats(10)
                    continue
                # Don't abort the whole pass (or the tailer) for one bad day;
                # the next scheduled re-fold recomputes it anyway (idempotent).
                print(f"{label}: re-fold {day_start} failed: {e}")
                break
        await send_watchdog_heartbeat()
        await asyncio.sleep(module.REFOLD_PACE_SEC)

    print(f"{label}: periodic re-fold touched {touched} rollup rows")


async def npc_totals_loop():
    """Keep the player_npc_hourly_totals rollup current (powers the profile
    'top bosses' blocks). Tails the drops table by drop_id every 60s, and
    periodically re-folds the current partition to heal commit-order gaps."""
    from services import npc_totals

    def run_once():
        # Fresh session per iteration: the module-global session is not
        # thread-safe across executor threads, and a single MySQL timeout
        # left it permanently in "invalid transaction" state.
        with Session() as s:
            try:
                return npc_totals.process_new_drops(session=s)
            except Exception:
                s.rollback()
                raise

    last_refold = 0.0  # 0 => re-fold on first pass (heals gaps right after a restart)
    while not shutdown_event.is_set():
        try:
            await send_watchdog_heartbeat()
            loop = asyncio.get_event_loop()
            scanned = await loop.run_in_executor(None, run_once)
            if scanned:
                print(f"npc_totals: folded {scanned} new drops into hourly rollup")

            if time.time() - last_refold >= npc_totals.REFOLD_INTERVAL_SEC:
                last_refold = time.time()  # set before running so a failure can't retry-storm
                await _run_periodic_refold(npc_totals, "npc_totals")
        except Exception as e:
            print(f"Error in npc totals loop: {e}")
            app_logger.log(
                log_type="error",
                data=f"Error in npc totals loop: {e}",
                app_name="player_updates",
                description="npc_totals_loop",
            )
        await sleep_with_watchdog_heartbeats(60)


async def item_totals_loop():
    """Keep the player_item_hourly_totals rollup current (powers the item
    pages' receive totals + search ranking). Tails the drops table by drop_id
    every 60s, and periodically re-folds the current partition to heal
    commit-order gaps. Same pattern as npc_totals_loop."""
    from services import item_totals

    def run_once():
        # Fresh session per iteration — same rationale as npc_totals_loop.
        with Session() as s:
            try:
                return item_totals.process_new_drops(session=s)
            except Exception:
                s.rollback()
                raise

    last_refold = 0.0  # 0 => re-fold on first pass (heals gaps right after a restart)
    while not shutdown_event.is_set():
        try:
            await send_watchdog_heartbeat()
            loop = asyncio.get_event_loop()
            scanned = await loop.run_in_executor(None, run_once)
            if scanned:
                print(f"item_totals: folded {scanned} new drops into hourly rollup")

            if time.time() - last_refold >= item_totals.REFOLD_INTERVAL_SEC:
                last_refold = time.time()  # set before running so a failure can't retry-storm
                await _run_periodic_refold(item_totals, "item_totals")
        except Exception as e:
            print(f"Error in item totals loop: {e}")
            app_logger.log(
                log_type="error",
                data=f"Error in item totals loop: {e}",
                app_name="player_updates",
                description="item_totals_loop",
            )
        await sleep_with_watchdog_heartbeats(60)


async def github_update_loop():
    """Enhanced github_update_loop with watchdog notifications"""
    if os.getenv("STATUS") == "dev" or os.getenv("STATE") == "dev":
        print("Skipping GitHub update loop on dev instance")
        ## Do not perform github updates on dev instances
        return
    updater = GithubPagesUpdater()
    app_logger.log(log_type="access", data=f"Started GitHub update loop", app_name="player_updates", description="github_update_loop")

    while not shutdown_event.is_set():
        last_update = redis_client.client.get("github_update_last_timestamp")
        last_update_dt = None
        if last_update:
            try:
                # redis_client.client is not decode_responses, so .get() returns bytes;
                # the timestamp was stored as an ISO string, so decode + parse before comparing.
                last_update_dt = datetime.fromisoformat(
                    last_update.decode() if isinstance(last_update, bytes) else last_update
                )
            except (ValueError, TypeError):
                # Malformed/blank value: treat as "no recent update".
                last_update_dt = None

        if last_update_dt is not None and last_update_dt > datetime.now() - timedelta(minutes=30):
            ## Ensure we are only updating once per 30 minutes, at minimum. Sleep out the
            ## remainder of the window and re-check, rather than ending the task entirely.
            remaining = (last_update_dt + timedelta(minutes=30)) - datetime.now()
            await sleep_with_watchdog_heartbeats(int(remaining.total_seconds()) + 1)
            continue

        redis_client.client.set("github_update_last_timestamp", datetime.now().isoformat())
        try:
            # Send watchdog heartbeat before GitHub update
            await send_watchdog_heartbeat()

            # Pass watchdog instance to prevent timeout during webhook checking
            changes = await updater.update_github_pages(watchdog)

            # Send watchdog heartbeat after GitHub update
            await send_watchdog_heartbeat()

            # Best-effort automation-channel report (internally bounded, never
            # raises — the watchdog cadence is preserved).
            from services.automation_updates import report_run
            await report_run("github_pages", ok=True, changes=changes or [])

        except Exception as e:
            print(f"Error in GitHub update loop: {e}")
            try:
                from services.automation_updates import report_run
                await report_run("github_pages", ok=False, changes=[], error=str(e)[:400])
            except Exception:
                pass

        # Re-check every 10 minutes; the 30-minute Redis gate above sets the
        # effective publish cadence. Runs are cheap now — the updater commits
        # only when the published content actually changed (this loop is the
        # ONLY publisher; heartbeat's competing 15-minute task was removed).
        await sleep_with_watchdog_heartbeats(600)

# Background task for player updates
@app.before_serving
async def setup_background_tasks():
    app.update_task = asyncio.create_task(update_players())
    app.github_task = asyncio.create_task(github_update_loop())
    app.npc_totals_task = asyncio.create_task(npc_totals_loop())
    app.item_totals_task = asyncio.create_task(item_totals_loop())
    app_logger.log(log_type="access", data=f"Started background tasks", app_name="player_updates", description="setup_background_tasks")

async def get_all_groups(session_to_use = None):
    if session_to_use is not None:
        return session_to_use.query(Group).all()
    with Session() as s:
        return s.query(Group).all()

@app.after_serving
async def cleanup_background_tasks():
    """Enhanced cleanup with proper task cancellation"""
    print("Shutting down background tasks...")
    
    # Signal shutdown to all loops
    shutdown_event.set()
    
    # Cancel tasks
    if hasattr(app, 'update_task'):
        app.update_task.cancel()
        try:
            await app.update_task
        except asyncio.CancelledError:
            pass
    
    if hasattr(app, 'github_task'):
        app.github_task.cancel()
        try:
            await app.github_task
        except asyncio.CancelledError:
            pass

    if hasattr(app, 'npc_totals_task'):
        app.npc_totals_task.cancel()
        try:
            await app.npc_totals_task
        except asyncio.CancelledError:
            pass

    if hasattr(app, 'item_totals_task'):
        app.item_totals_task.cancel()
        try:
            await app.item_totals_task
        except asyncio.CancelledError:
            pass

    app_logger.log(log_type="access", data=f"Background tasks were cancelled", app_name="player_updates", description="cleanup_background_tasks")

async def main():
    """Main function with systemd watchdog integration"""
    global watchdog
    
    # Setup signal handlers
    setup_signal_handlers()
    
    # Initialize systemd watchdog
    watchdog = SystemdWatchdog()
    watchdog.set_health_check(health_check)
    
    # Get port from environment variable or use default
    port = int(os.getenv("PLAYER_UPDATE_PORT", 21475))
    
    try:
        async with watchdog:
            # Notify systemd that we're ready
            await watchdog.notify_ready()
            print("Systemd watchdog initialized and ready notification sent")
            app_logger.log(log_type="access", data=f"Starting Player Update Service on port {port}", app_name="player_updates", description="main")
            
            # Start the Quart app
            app_task = asyncio.create_task(app.run_task(host='0.0.0.0', port=port))
            
            # Wait for either app to complete or shutdown signal
            done, pending = await asyncio.wait(
                [app_task, asyncio.create_task(shutdown_event.wait())],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # If shutdown was requested, cancel the app task
            if shutdown_event.is_set():
                print("Shutdown requested, stopping Player Update Service...")
                if not app_task.done():
                    app_task.cancel()
                    try:
                        await app_task
                    except asyncio.CancelledError:
                        pass
            
            print("Player Update Service shutting down gracefully...")
            
    except KeyboardInterrupt:
        print("Received keyboard interrupt")
    except Exception as e:
        print(f"Fatal error in main: {e}")
        app_logger.log(log_type="error", data=f"Fatal error in main: {e}", app_name="player_updates", description="main")
        raise
    finally:
        app_logger.log(log_type="access", data=f"Player Update Service cleanup completed", app_name="player_updates", description="main")
        print("Player Update Service cleanup completed")

if __name__ == '__main__':
    asyncio.run(main())

