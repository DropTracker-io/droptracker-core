"""Integration scenario for the ``players.player_name_norm`` generated column.

Run standalone (NOT collected by pytest — see test_player_name_norm_db.py,
which invokes it as a subprocess so the unit-test sys.modules stubs never
apply):

    venv/bin/python tests/integration/player_name_norm_it.py

``db.ops.resolve_player_for_display`` matches a submitted RSN against
``player_name_norm``, a MariaDB VIRTUAL generated column (web100a), using a key
computed in Python by ``normalize_player_display_equivalence``. The two
expressions live in different languages and different files, so nothing in the
type system stops them drifting — and if they drift, the fallback silently
stops resolving names instead of failing loudly. That is exactly the failure
mode this branch exists to prevent.

The unit tests cannot cover this: they run on sqlite, which has no
REGEXP_REPLACE, so their fake column is filled from the Python side and would
agree with itself no matter what the DDL says.

So this asserts the one thing that matters: for every input, what MariaDB
computes from a stored name equals what Python computes from the same string.
The inputs are the adversarial ones — the separator/whitespace/case cases the
old SQL got wrong (it trimmed before replacing, and never collapsed runs), plus
the live spellings from the reported split-credit bugs.
"""
import configparser
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402
_env = dotenv_values(os.path.join(ROOT, ".env"))
for _k in ("DB_USER", "DB_PASS"):
    if _env.get(_k):
        os.environ[_k] = _env[_k]

from sqlalchemy import create_engine, text  # noqa: E402

# utils.format is imported inside main(): importing it first pulls db.ops ->
# utils.embeds -> utils.format and dies on the partially-initialised module.

TEST_DB = "dt_migrate_test"

# Every one of these is a real shape, not a synthetic edge case:
#   - the plugin spellings from the split-credit bug (X-tra, Tzuk-Kal-Lag)
#   - the WOM-canonical stored forms
#   - the six live names the OLD expression could never match, because it
#     trimmed before replacing and never collapsed whitespace runs
NAMES = [
    "Beast_Owned", "Beast-Owned", "Beast Owned", "beast owned",
    "X-tra", "X_tra", "x tra",
    "Tzuk-Kal-Lag", "NoX-EvilAce", "Itz_Baal", "Blip-A",
    "Solo", "zero acc", "WI Beer Guy",
    # the six that the pre-web100a SQL stranded
    "-NoDashes", "the  worst", "I         q", "bandit  nick",
    "X     X333", "R  3d   0wnz",
    # leading/trailing separators and mixed runs
    "_Lead", "Trail_", "-Lead", "Trail-", " Spaced ", "a_-_b", "A - B",
]


def _test_engine():
    ini = configparser.ConfigParser()
    ini.read(os.path.join(ROOT, "alembic.ini"))
    base, _, _db = ini.get("alembic", "sqlalchemy.url").rpartition("/")
    assert TEST_DB != "data"
    return create_engine(f"{base}/{TEST_DB}")


def main():
    engine = _test_engine()
    assert engine.url.database == TEST_DB, f"refusing to run against {engine.url.database}"

    salt = os.getpid()
    table = f"player_name_norm_it_{salt}"

    from db.models.player import Player
    from utils.format import normalize_player_display_equivalence

    expr = Player.__table__.c.player_name_norm.computed.sqltext
    # Build the scratch table from the MODEL's expression, so a change to the
    # model that the migration did not follow is caught here too.
    ddl = (
        f"CREATE TABLE {table} ("
        " player_id INT PRIMARY KEY,"
        " player_name VARCHAR(20) COLLATE utf8mb4_general_ci,"
        " player_name_norm VARCHAR(20) COLLATE utf8mb4_general_ci"
        f" AS ({expr}) VIRTUAL,"
        " KEY ix_norm (player_name_norm)"
        ") ENGINE=InnoDB"
    )

    failures = []
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        conn.execute(text(ddl))
    try:
        with engine.begin() as conn:
            for i, name in enumerate(NAMES):
                conn.execute(
                    text(f"INSERT INTO {table} (player_id, player_name) VALUES (:i, :n)"),
                    {"i": i, "n": name},
                )
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT player_name, player_name_norm FROM {table}")
            ).all()
            assert len(rows) == len(NAMES), f"expected {len(NAMES)} rows, got {len(rows)}"
            for stored, sql_norm in rows:
                py_norm = normalize_player_display_equivalence(stored)
                if sql_norm != py_norm:
                    failures.append((stored, sql_norm, py_norm))

            # The column is only worth having if the resolver's lookup is a
            # seek. A plan change back to a scan would restore the 15ms cost
            # without failing any correctness assertion.
            plan = conn.execute(text(
                f"EXPLAIN SELECT player_id FROM {table} "
                "WHERE player_name_norm = 'x tra' LIMIT 1"
            )).all()
            access_type, key = plan[0][3], plan[0][5]
            assert access_type == "ref" and key == "ix_norm", (
                f"lookup no longer uses the index: type={access_type!r} key={key!r}"
            )
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))

    if failures:
        for stored, sql_norm, py_norm in failures:
            print(f"MISMATCH stored={stored!r} sql={sql_norm!r} python={py_norm!r}")
        raise AssertionError(
            f"{len(failures)}/{len(NAMES)} names normalize differently in MariaDB "
            "and Python — resolve_player_for_display would silently stop matching them"
        )

    print(f"checked {len(NAMES)} names; MariaDB and Python agree on every one")
    print("ALL PLAYER NAME NORM INTEGRATION ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
