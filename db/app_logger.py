"""Structured app logger — routes through Python ``logging`` (audit fix).

Historic shape: ``log()`` printed to stdout and RPUSH'd rows onto a Redis
``log_queue`` whose batch-inserter body had long been commented out — a
drain-to-nowhere worker thread. Because large subsystems (the notification
send path above all) log EXCLUSIVELY through this module, none of their
errors ever reached Python ``logging`` — which means none reached journald
log levels or the Sentry logging integration. That made every silent-failure
finding in the 2026-07-20 event-system audit invisible in production.

Now ``log()`` maps ``log_type`` onto real logging levels on a per-app child
logger (``app.<app_name>``): ERROR-level calls become Sentry events, WARNING
becomes breadcrumbs, and journald keeps the same one-line console shape as
the old ``print``. The public interface (``AppLogger().log(log_type=, data=,
app_name=, description=)``) is unchanged.
"""
import logging

_LEVELS = {
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

# Processes that already call logging.basicConfig (the workers) keep their
# config. Anything that never configured logging (the core bot boots straight
# into interactions.py) still gets timestamped stdout lines matching the old
# print() behaviour instead of losing sub-WARNING output to the last-resort
# handler.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
    )


class AppLogger:
    def log(self, log_type, data, app_name, description):
        logger = logging.getLogger(f"app.{app_name or 'app'}")
        level = _LEVELS.get(str(log_type).lower(), logging.INFO)
        logger.log(level, "%s (%s)", data, description)
