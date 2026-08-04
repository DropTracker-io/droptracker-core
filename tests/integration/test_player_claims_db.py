"""Integration test for the shared RSN claim service (db/player_claims.py).

Runs ``player_claims_it.py`` as a subprocess with the project venv python so
the unit-test conftest ``sys.modules`` stubs never apply. Talks to the
``dt_migrate_test`` MySQL schema (the script rebinds the global scoped session
there and cleans up after itself). Skips cleanly when the test DB is
unavailable.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "tests", "integration", "player_claims_it.py")
VENV_PYTHON = os.path.join(ROOT, "venv", "bin", "python")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


def test_player_claims_against_dt_migrate_test():
    env = {k: v for k, v in os.environ.items()
           if k not in ("DB_USER", "DB_PASS", "DB_HOST", "DB_NAME")}
    proc = subprocess.run(
        [PYTHON, SCRIPT], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=300,
    )
    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0 and (
            "Can't connect" in output or "Connection refused" in output
            or "Access denied" in output
            # alembic.ini is gitignored, so a fresh clone or a git worktree has
            # no test-DB URL at all; configparser reports the absent file as a
            # missing section rather than raising on read.
            or "No section: 'alembic'" in output):
        pytest.skip(f"test DB unavailable: {output[-500:]}")
    assert proc.returncode == 0, f"integration script failed:\n{output[-4000:]}"
    assert "ALL PLAYER CLAIM INTEGRATION ASSERTIONS PASSED" in output
