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
import sys

logger = logging.getLogger("droptracker.sentry")

_initialized = False


def _environment_name() -> str:
    """Sentry environment for this process, from the fleet's STATE switch.

    This used to be hardcoded to ``"production"``, so a dev box pointed at the
    same DSN filed its errors indistinguishably from the live fleet. The
    quote-stripping matches ``web_api/billing.py`` because the shipped .env
    writes it as ``STATE="live"``.
    """
    state = (os.getenv("STATE") or "").strip().strip('"').lower()
    return "production" if state == "live" else (state or "unknown")


def init_sentry(service_name: str) -> bool:
    """Initialise Sentry for this process. Returns True when enabled.

    Safe to call more than once; only the first successful call initialises.
    """
    global _initialized
    if _initialized:
        return True

    # Never report from a test run. The unit suite deliberately drives error
    # branches (dead-lettering, requeue failure, unhandled-request paths), and
    # because init runs at import time a single `import workers.webhook_consumer`
    # in one test module would otherwise arm Sentry for the whole pytest
    # process and ship those green-suite log.error()s to production.
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        logger.debug("pytest detected — Sentry disabled for %s", service_name)
        return False

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

    environment = _environment_name()

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            traces_sample_rate=0,  # errors only — no performance tracing
        )
        sentry_sdk.set_tag("service", service_name)
    except Exception as exc:  # never let telemetry break a service
        logger.warning("Sentry initialisation failed for %s: %s", service_name, exc)
        return False

    _initialized = True
    logger.info("Sentry initialised for %s (environment=%s)", service_name, environment)
    return True
