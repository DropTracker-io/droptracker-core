import os
import asyncio

from api import create_app
from api.lifecycle import setup_signal_handlers, cleanup_port, register_app_lifecycle, shutdown_event
from monitor.sdnotifier import SystemdWatchdog
from api.health_utils import health_check


async def main():
    app = create_app()
    register_app_lifecycle(app)

    print("Starting DropTracker API server...")
    setup_signal_handlers()
    print("Signal handlers setup complete")

    watchdog = SystemdWatchdog()

    async def watchdog_health_check():
        try:
            return await health_check(app, include_request_test=False)
        except Exception as exc:
            print(f"Watchdog health check encountered an error: {exc}")
            return False

    watchdog.set_health_check(watchdog_health_check)
    print("Systemd watchdog initialized")

    try:
        async with watchdog:
            port = int(os.environ.get("API_PORT", 31323))
            print(f"Checking for existing processes on port {port}...")
            port_available = await cleanup_port(port)
            if not port_available:
                print(f"Desired port {port} unavailable after cleanup attempts; exiting.")
                raise SystemExit(1)

            from hypercorn.config import Config
            from hypercorn.asyncio import serve

            # Note: Programmatic serve() ignores workers/worker_class (single event loop).
            # For production multi-worker, run: hypercorn --workers 6 ... "api:create_app()"
            config = Config()
            config.bind = [f"127.0.0.1:{port}"]
            config.graceful_timeout = 10
            config.keep_alive_timeout = 10

            print(f"Starting Hypercorn on port {port}...")
            serve_task = asyncio.create_task(
                serve(app, config, shutdown_trigger=shutdown_event.wait)
            )

            await watchdog.notify_ready()
            print("Systemd watchdog initialized and ready notification sent")

            await serve_task
            print("API server shutting down gracefully...")
    finally:
        print("API server cleanup completed")


if __name__ == "__main__":
    asyncio.run(main())
