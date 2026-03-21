import os
from datetime import timedelta

from dotenv import load_dotenv

from api.services.metrics import MetricsTracker
from utils.logger import LoggerClient
from utils.redis import RedisClient
from services import redis_updates
from db import Session, session, engine
from threading import Lock

_pool_status_lock = Lock()


def log_pool_status(prefix: str) -> None:
    """Emit the current SQLAlchemy pool status for diagnostics."""
    try:
        with _pool_status_lock:
            status = engine.pool.status()
        print(f"[DB] {prefix}: {status}")
    except Exception as exc:
        print(f"[DB] Failed to log pool status ({prefix}): {exc}")


# Load environment variables as early as possible
load_dotenv()


# Core singletons shared across blueprints
logger = LoggerClient(token=os.getenv("LOGGER_TOKEN"))
metrics = MetricsTracker()
redis_client = RedisClient()
redis_tracker = redis_updates.RedisLootTracker()


def get_db_session(log_label: str = None):
    """Return a new SQLAlchemy session from the shared session factory."""
    db_session = Session()
    if log_label:
        log_pool_status(f"{log_label} checkout")
    return db_session


def reset_db_connections():
    """Dispose of the current scoped session to avoid stale connections."""
    try:
        session.remove()
    except Exception:
        # Be lenient here; this is best-effort cleanup
        pass


__all__ = [
    "logger",
    "metrics",
    "redis_client",
    "redis_tracker",
    "log_pool_status",
    "get_db_session",
    "reset_db_connections",
]


