"""Canonical read-only SQL guard for the admin/KB surfaces.

Single source of truth used by BOTH:
- the admin bot's ``/sql`` command (owner-typed raw SQL, gated by KB_ALLOW_SQL), and
- ``services.kb.investigator`` (model-generated investigation SELECTs).

Deliberately dependency-light (stdlib + sqlalchemy + db.models.base only) so it
is always safe to import at module top, unlike the optional-dep KB modules.

Guarantees: single statement; SELECT/SHOW/EXPLAIN/DESCRIBE only; forbidden
keywords scanned with string literals stripped (so ``WHERE name='update'``
passes); LIMIT 50 auto-appended to un-LIMITed SELECTs; execution runs on an
AUTOCOMMIT connection with ``max_statement_time=10`` and the session forced
READ ONLY — belt and suspenders on top of the keyword validation.
"""

import re

from db.models.base import engine

_SQL_START = re.compile(r"^(select|show|explain|describe|desc)\b", re.I)
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|call|"
    r"load|outfile|dumpfile|infile|lock|unlock|set|use|handler|do|execute|prepare|"
    r"deallocate|kill|shutdown|install|uninstall|rename|optimize|repair|analyze|flush|"
    r"reset|purge|change|start|stop|begin|commit|rollback|savepoint|xa|sleep|benchmark)\b",
    re.I,
)


def validate_readonly_sql(raw: str) -> tuple[bool, str]:
    """(ok, sanitized-or-error). Single read-only statement; forbidden keywords
    scanned with string literals stripped (so WHERE name='update' passes);
    LIMIT 50 auto-appended to un-LIMITed SELECTs."""
    s = re.sub(r"/\*.*?\*/", " ", raw, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"#[^\n]*", " ", s).strip().rstrip(";").strip()
    if not s:
        return False, "Empty query."
    if ";" in s:
        return False, "Multiple statements are not allowed."
    m = _SQL_START.match(s)
    if not m:
        return False, "Only SELECT / SHOW / EXPLAIN / DESCRIBE queries are allowed."
    scan = re.sub(r"'(?:[^'\\]|\\.)*'", "''", s)
    scan = re.sub(r'"(?:[^"\\]|\\.)*"', '""', scan)
    if _SQL_FORBIDDEN.search(scan):
        return False, "Query contains a forbidden keyword."
    if m.group(1).lower() == "select" and not re.search(r"\blimit\s+\d+", s, re.I):
        s += " LIMIT 50"
    return True, s


def run_readonly_sql(q: str):
    """Execute a validated read-only query. Returns (columns, rows[<=50])."""
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.exec_driver_sql("SET SESSION max_statement_time=10")
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        res = conn.exec_driver_sql(q)
        cols = list(res.keys())
        rows = res.fetchmany(50)
        return cols, rows
