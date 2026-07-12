"""Optional Sentry error reporting for all DropTracker services.

Usage — once, at process startup (every systemd unit's entry point):

    from utils.sentry import init_sentry
    init_sentry("droptracker-api")

This is a strict no-op unless BOTH of the following hold:
  * the ``sentry-sdk`` package is installed, and
  * ``SENTRY_DSN`` is set in the environment (``.env``),
so calling it can never take a service down. Only errors are reported
(``traces_sample_rate=0`` — no performance tracing). The service name is
attached as a ``service`` tag so one DSN can cover the whole fleet.
"""

import logging
import os

logger = logging.getLogger("droptracker.sentry")

_initialized = False


def init_sentry(service_name: str) -> bool:
    """Initialise Sentry for this process. Returns True when enabled.

    Safe to call more than once; only the first successful call initialises.
    """
    global _initialized
    if _initialized:
        return True

    # Entry points that don't call load_dotenv() before us still get the DSN.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        logger.info("SENTRY_DSN not set — Sentry disabled for %s", service_name)
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning("sentry-sdk not installed — Sentry disabled for %s", service_name)
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment="production",
            traces_sample_rate=0,  # errors only — no performance tracing
        )
        sentry_sdk.set_tag("service", service_name)
    except Exception as exc:  # never let telemetry break a service
        logger.warning("Sentry initialisation failed for %s: %s", service_name, exc)
        return False

    _initialized = True
    logger.info("Sentry initialised for %s", service_name)
    return True
