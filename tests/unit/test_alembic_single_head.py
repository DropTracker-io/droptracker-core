"""The migration graph must have exactly one head.

Two heads is not a loud failure — nothing breaks until the next deploy, when
``alembic upgrade head`` refuses with "Multiple head revisions are present" and
whoever is deploying has to name a revision by hand. That workaround then hides
the split: naming one head silently leaves the other branch unapplied. It
happened for real (``kb01a_knowledgebase_tables`` was authored off
``web67a_ticket_inactivity`` and the ``web*`` line ran on to web75a without
rejoining), so this asserts the invariant instead of waiting for a deploy to
discover it.

The fix when this fails is one command, from the repo root::

    ./venv/bin/alembic merge -m "<why these two lines diverged>" heads

``alembic/versions/`` is **gitignored** (see CONTRIBUTING.md), so a fresh clone
and CI have no migration files at all — the test skips there rather than
failing on an empty graph. It is a guard for the boxes that actually hold the
migrations, which is where the mistake gets made.
"""
from __future__ import annotations

import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ALEMBIC_DIR = os.path.join(_REPO_ROOT, "alembic")
_VERSIONS_DIR = os.path.join(_ALEMBIC_DIR, "versions")


def _migration_files() -> list:
    if not os.path.isdir(_VERSIONS_DIR):
        return []
    return [f for f in os.listdir(_VERSIONS_DIR) if f.endswith(".py")]


def test_migration_graph_has_a_single_head():
    pytest.importorskip("alembic", reason="alembic isn't installed in this environment")
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not _migration_files():
        pytest.skip("alembic/versions/ is gitignored — no migrations in this checkout")

    # Built from the script directory alone, never alembic.ini: the ini carries
    # DB credentials and is itself untracked (alembic.ini.template is what ships),
    # and reading the graph needs no database connection.
    config = Config()
    config.set_main_option("script_location", _ALEMBIC_DIR)
    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, (
        "The migration graph has diverged into "
        f"{len(heads)} heads: {sorted(heads)}. `alembic upgrade head` cannot run "
        "until they are merged — from the repo root:\n"
        '    ./venv/bin/alembic merge -m "<why these lines diverged>" heads'
    )
