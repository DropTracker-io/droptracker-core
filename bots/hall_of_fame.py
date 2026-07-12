import interactions
import os
import json
import signal
import sys
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from interactions.api.events import MessageCreate, Startup
from interactions import Embed, Intents, Message, ChannelType, OptionType, slash_command, Permissions, slash_option
from db.entitlements import resolve_group_entitlements
from db.models import Group, ItemList, PersonalBestEntry, PlayerPet, Session, Player, User, GroupConfiguration
from utils.format import convert_to_ms, get_true_boss_name
from services import hall_of_fame
from monitor.sdnotifier import SystemdWatchdog
import time


load_dotenv()

bot = interactions.Client(token=os.getenv("HALL_OF_FAME_BOT_TOKEN"), intents=Intents.ALL)

# The global/template group is always active and exempt from the premium gate
# (mirrors services.hall_of_fame._GLOBAL_GROUP_ID).
GLOBAL_GROUP_ID = 2

# Global variables for systemd watchdog
watchdog = None
shutdown_event = asyncio.Event()
# Allow the gateway a grace period to connect before the health check can report
# "unhealthy" (otherwise it flaps on every startup and spams the journal). The
# watchdog heartbeat is sent regardless, so this only affects the log line.
_STARTUP_GRACE_SECONDS = 120
_process_started_at = time.monotonic()

# Health check function for systemd watchdog
async def health_check():
    """Comprehensive health check for the hall of fame bot"""
    try:
        # Check if bot is ready and connected
        if not bot.is_ready:
            # Still coming up — don't report unhealthy during the grace window.
            return (time.monotonic() - _process_started_at) < _STARTUP_GRACE_SECONDS

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


@interactions.listen(Startup)
async def on_startup(event: Startup):
    print("Hall of Fame bot started.")
    total_groups = 0
    try:
        local_session = Session()
        # Count exactly the groups the reconciliation loop will actually process:
        # create_pb_embeds enabled AND (global group OR holds the hall_of_fame
        # entitlement). Uses the same resolver as services.hall_of_fame so the
        # presence count can't drift from reality.
        group_ids = [
            row.group_id
            for row in local_session.query(GroupConfiguration.group_id).filter(
                GroupConfiguration.config_key == "create_pb_embeds",
                GroupConfiguration.config_value == "1",
            ).all()
        ]
        for group_id in group_ids:
            if group_id == GLOBAL_GROUP_ID:
                total_groups += 1
                continue
            try:
                if resolve_group_entitlements(local_session, group_id).get("hall_of_fame"):
                    total_groups += 1
            except Exception:
                local_session.rollback()
    except Exception as e:
        # Presence count is cosmetic — never let it stop the service from loading.
        print("Error getting groups to update:", e)
    bot.load_extension("services.hall_of_fame")
    try:
        await bot.change_presence(status=interactions.Status.ONLINE,
                                  activity=interactions.Activity(name=f"{total_groups} Halls of Fame", type=interactions.ActivityType.WATCHING))
    except Exception as e:
        print("Error setting presence:", e)

async def main():
    """Main function with systemd watchdog integration"""
    global watchdog
    
    # Setup signal handlers
    setup_signal_handlers()
    
    # Initialize systemd watchdog
    watchdog = SystemdWatchdog()
    watchdog.set_health_check(health_check)
    
    try:
        async with watchdog:
            # Notify systemd that we're ready
            await watchdog.notify_ready()
            print("Systemd watchdog initialized and ready notification sent")

            shutdown_task = asyncio.create_task(shutdown_event.wait())

            while not shutdown_event.is_set():
                # Start the bot
                bot_task = asyncio.create_task(bot.astart(token=os.getenv("HALL_OF_FAME_BOT_TOKEN")))

                # Wait for either bot to complete or shutdown signal
                done, pending = await asyncio.wait(
                    [bot_task, shutdown_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # If shutdown was requested, cancel the bot task
                if shutdown_event.is_set():
                    print("Shutdown requested, stopping bot...")
                    if not bot_task.done():
                        bot_task.cancel()
                        try:
                            await bot_task
                        except asyncio.CancelledError:
                            pass
                    break

                # Bot stopped unexpectedly; log exception if present and restart
                if bot_task.done():
                    exc = bot_task.exception()
                    if exc:
                        print(f"Hall of Fame bot task crashed: {exc}")
                    else:
                        print("Hall of Fame bot task stopped unexpectedly")
                    await asyncio.sleep(5)

            print("Hall of Fame bot shutting down gracefully...")
            
    except KeyboardInterrupt:
        print("Received keyboard interrupt")
    except Exception as e:
        print(f"Fatal error in main: {e}")
        raise
    finally:
        print("Hall of Fame bot cleanup completed")

if __name__ == "__main__":
    asyncio.run(main())