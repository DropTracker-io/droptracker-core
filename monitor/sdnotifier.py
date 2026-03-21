"""
Shared systemd watchdog utility for multiple applications
"""
import asyncio
import os
import logging
import inspect
import threading
from typing import Optional, Callable
from concurrent.futures import TimeoutError as FutureTimeout
import sdnotify

logger = logging.getLogger(__name__)

class SystemdWatchdog:
    def __init__(self, heartbeat_interval: Optional[float] = None):
        """
        Initialize the systemd watchdog handler.
        
        Args:
            heartbeat_interval: Interval between watchdog notifications in seconds.
                               If None, will auto-calculate from WATCHDOG_USEC.
        """
        self.notifier = sdnotify.SystemdNotifier()
        self.is_running = False
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.health_check_func: Optional[Callable] = None
        
        # Calculate heartbeat interval
        if heartbeat_interval is None:
            self.heartbeat_interval = self._calculate_heartbeat_interval()
        else:
            self.heartbeat_interval = heartbeat_interval
            
        logger.info(f"SystemdWatchdog initialized with {self.heartbeat_interval}s interval")
    
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
    
    def _heartbeat_loop(self) -> None:
        """Internal heartbeat loop running in a background thread."""
        while not self.stop_event.is_set():
            try:
                healthy = True
                if self.health_check_func:
                    try:
                        check_result = self.health_check_func()
                        if inspect.isawaitable(check_result):
                            if not self.loop:
                                raise RuntimeError("Async health check requires an event loop")
                            future = asyncio.run_coroutine_threadsafe(check_result, self.loop)
                            healthy = future.result(timeout=max(self.heartbeat_interval / 2, 1))
                        else:
                            healthy = bool(check_result)
                    except FutureTimeout:
                        logger.warning("Health check timed out")
                        healthy = False
                    except Exception as e:
                        logger.error(f"Health check raised an error: {e}")
                        healthy = False

                if not healthy:
                    logger.warning("Health check reported unhealthy state")

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