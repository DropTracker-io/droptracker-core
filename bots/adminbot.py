"""DropTracker Admin / Knowledgebase bot (owner-only, standalone process).

Mirrors the process skeleton of ``bots/hall_of_fame.py`` (load_dotenv → Sentry →
Client → Startup listener → systemd watchdog + health-check + graceful shutdown /
restart loop) but is a completely separate Discord application:

  * Its token is ``ADMIN_BOT_TOKEN``. Until the owner creates the Discord app and
    fills it in ``.env`` that var is empty, so this module prints a one-line
    explanation and exits 78 (EX_CONFIG). The systemd unit sets
    ``RestartPreventExitStatus=78`` so an unconfigured token never crash-loops.
  * It requests ``Intents.ALL`` — the owner has enabled all three privileged
    toggles on the application (Message Content is what ``/kb-sync`` history
    mining actually needs).
  * The only extension it loads is ``commands.adminbot_cmds`` — the owner-only KB
    admin commands. It deliberately does NOT load the ``commands`` package the
    public bot uses.
"""

import interactions
import os
import signal
import sys
import asyncio
from dotenv import load_dotenv
from interactions.api.events import Startup
from interactions import Intents
from monitor.sdnotifier import SystemdWatchdog
import time


load_dotenv()

from utils.sentry import init_sentry
init_sentry("droptracker-adminbot")

# Before constructing anything Discord: refuse to start without a token. The unit
# has RestartPreventExitStatus=78 so systemd won't crash-loop on this.
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
if not ADMIN_BOT_TOKEN or not ADMIN_BOT_TOKEN.strip():
    print("ADMIN_BOT_TOKEN is not set — create the admin Discord application and fill it in .env")
    sys.exit(78)

# All intents — the owner has enabled all three privileged toggles on this
# application (Message Content is the load-bearing one for /kb-sync mining).
bot = interactions.Client(token=ADMIN_BOT_TOKEN, intents=Intents.ALL)

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
    """Comprehensive health check for the admin/KB bot"""
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
    print("Admin/KB bot started.")
    # Owner-only KB admin commands live in their own extension, loaded ONLY here —
    # never via the public bot's `load_extension("commands")`. Startup fires again
    # on every in-process reconnect (main()'s astart retry loop), so tolerate the
    # extension already being loaded instead of aborting the listener.
    try:
        bot.load_extension("commands.adminbot_cmds")
    except Exception as e:
        if "already loaded" in str(e):
            print("adminbot_cmds already loaded (reconnect) — continuing")
        else:
            raise
    try:
        await bot.change_presence(
            status=interactions.Status.ONLINE,
            activity=interactions.Activity(name="the database", type=interactions.ActivityType.WATCHING),
        )
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
                bot_task = asyncio.create_task(bot.astart(token=ADMIN_BOT_TOKEN))

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
                        print(f"Admin/KB bot task crashed: {exc}")
                    else:
                        print("Admin/KB bot task stopped unexpectedly")
                    await asyncio.sleep(5)

            print("Admin/KB bot shutting down gracefully...")

    except KeyboardInterrupt:
        print("Received keyboard interrupt")
    except Exception as e:
        print(f"Fatal error in main: {e}")
        raise
    finally:
        print("Admin/KB bot cleanup completed")


if __name__ == "__main__":
    asyncio.run(main())
