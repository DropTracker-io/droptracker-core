"""Integration test for the event sign-up pool (services/event_signup.py).

Runs ``event_signup_it.py`` as a subprocess with the project venv python so the
unit-test conftest ``sys.modules`` stubs never apply. Talks to the
``dt_migrate_test`` MySQL schema inside a rolled-back transaction. Skips
cleanly when the test DB is unavailable.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "tests", "integration", "event_signup_it.py")
VENV_PYTHON = os.path.join(ROOT, "venv", "bin", "python")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


def test_event_signup_against_dt_migrate_test():
    env = {k: v for k, v in os.environ.items()
           if k not in ("DB_USER", "DB_PASS", "DB_HOST", "DB_NAME")}
    proc = subprocess.run(
        [PYTHON, SCRIPT], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=300,
    )
    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0 and (
            "Can't connect" in output or "Connection refused" in output
            or "Access denied" in output):
        pytest.skip(f"test DB unavailable: {output[-500:]}")
    assert proc.returncode == 0, f"integration script failed:\n{output[-4000:]}"
    assert "ALL EVENT SIGNUP INTEGRATION ASSERTIONS PASSED" in output
