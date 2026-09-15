"""
Shared systemd watchdog utility for multiple applications
"""
import asyncio
import faulthandler
import os
import logging
import inspect
import threading
import time
from typing import Optional, Callable, Tuple
from concurrent.futures import TimeoutError as FutureTimeout
import sdnotify

logger = logging.getLogger(__name__)

# With restart_after_stalls set, WATCHDOG=1 is never withheld before the
# watchdog has run this long, so watchdog restarts are at least this far apart.
# Discord bots identify on every start and a token that goes past 1000
# identifies in 24h is reset; 300s (+30s WatchdogSec, +5s RestartSec) caps a
# restart loop at ~260 a day.
DEFAULT_RESTART_MIN_UPTIME = 300.0


class SystemdWatchdog:
    def __init__(self, heartbeat_interval: Optional[float] = None, *,
                 restart_after_stalls: Optional[int] = None,
                 restart_min_uptime: float = DEFAULT_RESTART_MIN_UPTIME):
        """
        Initialize the systemd watchdog handler.
        
        Args:
            heartbeat_interval: Interval between watchdog notifications in seconds.
                               If None, will auto-calculate from WATCHDOG_USEC.
            restart_after_stalls: Opt-in. Once the event loop has stalled on this
                               many consecutive checks, stop sending WATCHDOG=1 so
                               WatchdogSec expires and Restart= starts a fresh
                               process. A stall is the loop not running a no-op
                               callback within the check timeout. A health check
                               that runs and returns False never withholds: it
                               is logged, as it is when this is None (the default).
            restart_min_uptime: Don't withhold before the watchdog has run this
                               long, which spaces watchdog restarts at least
                               this far apart.
        """
        if restart_after_stalls is not None and restart_after_stalls < 1:
            raise ValueError("restart_after_stalls must be at least 1")
        self.notifier = sdnotify.SystemdNotifier()
        self.is_running = False
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.health_check_func: Optional[Callable] = None
        self.restart_after_stalls = restart_after_stalls
        self.restart_min_uptime = restart_min_uptime
        self.consecutive_stalls = 0
        self.withholding = False
        self.started_at = time.monotonic()
        
        # Calculate heartbeat interval
        if heartbeat_interval is None:
            self.heartbeat_interval = self._calculate_heartbeat_interval()
        else:
            self.heartbeat_interval = heartbeat_interval
            
        logger.info(f"SystemdWatchdog initialized with {self.heartbeat_interval}s interval")
        if restart_after_stalls is not None:
            logger.info(f"Watchdog restarts after {restart_after_stalls} consecutive event loop stalls "
                        f"(not before {restart_min_uptime:.0f}s of uptime)")
    
    def _calculate_heartbeat_interval(self) -> float:
        """Calculate appropriate heartbeat interval from systemd's WATCHDOG_USEC."""
        watchdog_usec = os.environ.get('WATCHDOG_USEC')
        if watchdog_usec:
            # Convert microseconds to seconds and use half the timeout as interval
            watchdog_sec = int(watchdog_usec) / 1_000_000
            interval = watchdog_sec / 2
            logger.info(f"Auto-calculated heartbeat interval: {interval}s (watchdog timeout: {watchdog_sec}s)")
            return interval
        else:
            # Default fallback if no watchdog configured
            logger.warning("No WATCHDOG_USEC found, using default 15s interval")
            return 15.0
    
    def set_health_check(self, func: Callable) -> None:
        """
        Set a custom health check function.
        The function should return True if the service is healthy, False otherwise.
        Can be async or sync.
        """
        self.health_check_func = func
    
    async def notify_ready(self) -> None:
        """Notify systemd that the service is ready."""
        self.notifier.notify("READY=1")
        logger.info("Notified systemd: service ready")
    
    async def notify_stopping(self) -> None:
        """Notify systemd that the service is stopping."""
        self.notifier.notify("STOPPING=1")
        logger.info("Notified systemd: service stopping")
    
    def _run_health_check(self) -> Tuple[bool, bool]:
        """Run the health check once. Returns (healthy, loop_stalled); a stall
        is only measured when restart_after_stalls is set."""
        timeout = max(self.heartbeat_interval / 2, 1)
        deadline = time.monotonic() + timeout
        loop_ran = None
        if self.restart_after_stalls is not None and self.loop is not None:
            loop_ran = threading.Event()
            try:
                # Queued ahead of an async check, so a check that finished means this ran.
                self.loop.call_soon_threadsafe(loop_ran.set)
            except RuntimeError:  # loop already closed: the process is exiting
                loop_ran = None

        healthy = True
        if self.health_check_func:
            try:
                check_result = self.health_check_func()
                if inspect.isawaitable(check_result):
                    if not self.loop:
                        raise RuntimeError("Async health check requires an event loop")
                    future = asyncio.run_coroutine_threadsafe(check_result, self.loop)
                    healthy = future.result(timeout=timeout)
                else:
                    healthy = bool(check_result)
            except FutureTimeout:
                logger.warning("Health check timed out")
                healthy = False
            except Exception as e:
                logger.error(f"Health check raised an error: {e}")
                healthy = False

        stalled = loop_ran is not None and not loop_ran.wait(max(deadline - time.monotonic(), 0))
        return bool(healthy), stalled

    def _should_notify(self, stalled: bool, now: float) -> bool:
        """Track consecutive stalls; False means withhold WATCHDOG=1 so systemd restarts the unit."""
        if self.restart_after_stalls is None:
            return True
        if not stalled:
            if self.withholding:
                logger.warning("Event loop recovered; resuming watchdog heartbeats")
            self.consecutive_stalls = 0
            self.withholding = False
            return True

        self.consecutive_stalls += 1
        if (self.consecutive_stalls < self.restart_after_stalls
                or now - self.started_at < self.restart_min_uptime):
            logger.warning(f"Event loop stalled on {self.consecutive_stalls} consecutive checks "
                           f"(watchdog restart after {self.restart_after_stalls})")
            return True
        if not self.withholding:
            self.withholding = True
            logger.error(f"Event loop stalled on {self.consecutive_stalls} consecutive checks; "
                         "withholding WATCHDOG=1 so systemd restarts the unit")
            try:
                # The restart destroys the evidence, so record where the loop is stuck.
                faulthandler.dump_traceback(all_threads=True)
            except Exception:
                pass
        return False

    def _heartbeat_loop(self) -> None:
        """Internal heartbeat loop running in a background thread."""
        while not self.stop_event.is_set():
            try:
                healthy, stalled = self._run_health_check()
                if not healthy:
                    logger.warning("Health check reported unhealthy state")

                if self._should_notify(stalled, time.monotonic()):
                    try:
                        self.notifier.notify("WATCHDOG=1")
                        logger.debug("Sent watchdog heartbeat")
                    except Exception as notify_error:
                        logger.error(f"Failed to notify watchdog: {notify_error}")
            except Exception as loop_error:
                logger.error(f"Error in watchdog heartbeat thread: {loop_error}")

            self.stop_event.wait(timeout=self.heartbeat_interval)
    
    async def start(self) -> None:
        """Start the watchdog heartbeat."""
        if self.is_running:
            logger.warning("Watchdog already running")
            return
        
        # Only start if systemd watchdog is actually configured
        if not os.environ.get('WATCHDOG_USEC'):
            logger.info("No systemd watchdog configured, skipping watchdog start")
            return
        
        self.is_running = True
        self.loop = asyncio.get_running_loop()
        self.started_at = time.monotonic()
        self.consecutive_stalls = 0
        self.withholding = False
        self.stop_event.clear()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        logger.info("Started systemd watchdog heartbeat")
    
    async def stop(self) -> None:
        """Stop the watchdog heartbeat."""
        if not self.is_running:
            return
        
        self.is_running = False
        self.stop_event.set()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=self.heartbeat_interval)
            self.heartbeat_thread = None
        
        await self.notify_stopping()
        logger.info("Stopped systemd watchdog heartbeat")
    
    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()


# Convenience function for simple use cases
async def run_with_systemd_watchdog(main_func: Callable, 
                                   health_check_func: Optional[Callable] = None,
                                   heartbeat_interval: Optional[float] = None):
    """
    Run a main function with systemd watchdog support.
    
    Args:
        main_func: The main application function to run (should be async)
        health_check_func: Optional health check function
        heartbeat_interval: Optional custom heartbeat interval
    """
    watchdog = SystemdWatchdog(heartbeat_interval)
    if health_check_func:
        watchdog.set_health_check(health_check_func)
    
    try:
        async with watchdog:
            await watchdog.notify_ready()
            await main_func()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error(f"Application error: {e}")
        raise
    finally:
        logger.info("Application shutting down")