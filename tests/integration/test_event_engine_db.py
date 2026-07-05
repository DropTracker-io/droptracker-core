"""Integration test for the event completion engine (Task 17).

The scenario lives in ``event_engine_it.py`` and runs as a subprocess with the
project venv python so the unit-test conftest ``sys.modules`` stubs (db /
redis / services are MagicMocks there) never apply. It talks to the
``dt_migrate_test`` MySQL schema inside a rolled-back transaction and to real
Redis with throwaway keys.

Skips cleanly when the test DB or Redis is unavailable.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "tests", "integration", "event_engine_it.py")
VENV_PYTHON = os.path.join(ROOT, "venv", "bin", "python")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


def test_event_engine_against_dt_migrate_test():
    env = {k: v for k, v in os.environ.items()
           # Drop conftest's fake creds; the script loads real ones from .env
           if k not in ("DB_USER", "DB_PASS", "DB_HOST", "DB_NAME")}
    proc = subprocess.run(
        [PYTHON, SCRIPT], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=300,
    )
    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0 and (
            "Can't connect" in output or "Connection refused" in output
            or "Access denied" in output):
        pytest.skip(f"test DB/Redis unavailable: {output[-500:]}")
    assert proc.returncode == 0, f"integration script failed:\n{output[-4000:]}"
    assert "ALL EVENT ENGINE INTEGRATION ASSERTIONS PASSED" in output
