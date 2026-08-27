"""Dedicated DB engine for the data API.

Separate from the shared ``db.models.base`` engine on purpose:

* **Credentials** — ``DATA_API_DB_USER``/``DATA_API_DB_PASS`` select a
  SELECT-only MariaDB account (``dataapi_ro``) whose ``MAX_USER_CONNECTIONS``
  caps this service at the server even if the pool math here is wrong.
  Key mint/update writes go through the *web_api* (shared engine), not here.
  Falls back to the shared credentials when unset so a dev checkout works
  before the grant exists.
* **Statement ceiling** — every connection starts with
  ``max_statement_time = 10`` so a runaway query is killed server-side and
  surfaces as a clean 503 (:func:`is_statement_timeout`), well inside the 30s
  client read_timeout and nginx's 90s. The export API sets this per-block;
  here it is unconditional because *no* endpoint of this service has a
  legitimate >10s query — that is what the rollup/caching rules are for.
* **Pool** — small (5+10). This service is supposed to shed load, not queue
  it; the shared 500-connection budget already feeds ~15 processes.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Production runs `hypercorn "data_api:create_app()"` — nothing on that path
# reads .env, so do it where the credentials are consumed (the
# db/models/base.py convention).
load_dotenv()

STATEMENT_TIMEOUT_SECONDS = 10

#: MariaDB "statement exceeded max_statement_time" + client-side query
#: interrupted / server-gone codes seen when the ceiling fires mid-read.
_TIMEOUT_ERROR_CODES = {1969, 3024, 2013}


def _database_url() -> str:
    user = os.getenv("DATA_API_DB_USER") or os.getenv("DB_USER")
    password = os.getenv("DATA_API_DB_PASS") or os.getenv("DB_PASS")
    host = os.getenv("DB_HOST", "localhost")
    name = os.getenv("DB_NAME", "data")
    return f"mysql+pymysql://{user}:{password}@{host}:3306/{name}"


engine = create_engine(
    _database_url(),
    pool_size=int(os.getenv("DATA_API_DB_POOL_SIZE", "5")),
    max_overflow=int(os.getenv("DATA_API_DB_MAX_OVERFLOW", "10")),
    pool_timeout=10,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_reset_on_return="rollback",
    # Same defence as the shared engine (see db/models/base.py): a leaked
    # session under READ COMMITTED releases its read view per statement
    # instead of pinning InnoDB purge fleet-wide.
    isolation_level="READ COMMITTED",
    connect_args={
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
        "charset": "utf8mb4",
        "init_command": f"SET SESSION max_statement_time = {STATEMENT_TIMEOUT_SECONDS}",
    },
)

SessionLocal = sessionmaker(bind=engine)


def is_statement_timeout(error: Exception) -> bool:
    """Whether ``error`` is the statement ceiling firing (→ respond 503)."""
    cause = getattr(error, "orig", error)
    args = getattr(cause, "args", ())
    return bool(args) and args[0] in _TIMEOUT_ERROR_CODES
