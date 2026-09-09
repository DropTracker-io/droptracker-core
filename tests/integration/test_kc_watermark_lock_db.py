"""Integration test for the KC watermark lock (see kc_watermark_lock_it.py).

Runs the scenario as a subprocess with the project venv python so the unit-test
conftest ``sys.modules`` stubs never apply.

OPT-IN — set ``KC_LOCK_IT=1`` to run it. Unlike the other integration tests
here, this one cannot use the ``dt_migrate_test`` schema: it exercises
``_lock_watermark_row`` through the application's own ``db.models.base.engine``,
and the deadlock it guards against is a property of *that* engine's settings
(pymysql ``read_timeout`` vs ``innodb_lock_wait_timeout``) and of real InnoDB
row locks. So it touches the live ``data`` schema, using a salted NEGATIVE
(player_id, npc_id) that no real player can have — ``player_npc_kc`` has no
foreign keys — and deleting it afterwards. It locks only its own synthetic row,
so it cannot block live submission traffic, but a default-on test that writes to
production is not a thing to leave lying around in CI.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "tests", "integration", "kc_watermark_lock_it.py")
VENV_PYTHON = os.path.join(ROOT, "venv", "bin", "python")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


@pytest.mark.skipif(
    os.getenv("KC_LOCK_IT") != "1",
    reason="opt-in: talks to the live data schema (set KC_LOCK_IT=1)",
)
def test_kc_watermark_lock_never_blocks_the_event_loop():
    # Strip the conftest's stub DB credentials (it sets DB_USER=test_user), or
    # the subprocess inherits them and cannot reach the real schema — the same
    # reason test_player_name_norm_db.py does this.
    env = {k: v for k, v in os.environ.items()
           if k not in ("DB_USER", "DB_PASS", "DB_HOST", "DB_NAME")}
    proc = subprocess.run(
        [PYTHON, SCRIPT], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=300,
    )
    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0 and (
            "Can't connect" in output or "Connection refused" in output
            or "Access denied" in output or "Unknown database" in output):
        pytest.skip(f"database unavailable: {output[-500:]}")
    assert proc.returncode == 0, f"integration script failed:\n{output[-4000:]}"
    assert "ALL KC WATERMARK LOCK INTEGRATION ASSERTIONS PASSED" in output
