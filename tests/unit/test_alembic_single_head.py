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

``alembic/versions/`` has been tracked since 2026-09-16, so CI sees the whole
graph and this runs everywhere. (Before that the directory was gitignored and
only a handful of revisions were force-added, so CI skipped it.)

A commit can still carry an incomplete graph — a revision whose parent was
never committed. Alembic cannot build a graph whose ancestors are missing (it
raises ``KeyError`` on the absent revision before it can count heads), so that
is checked first and fails with the names of the missing revisions.
"""
from __future__ import annotations

import os
import re

import pytest

#: ``revision`` / ``down_revision`` assignments, module level. Three shapes
#: coexist in versions/ — bare (``revision = 'x'``), annotated
#: (``revision: str = 'x'``) and, on merge revisions, a tuple of parents
#: (``down_revision = ("a", "b")``). The right-hand side is captured whole and
#: its quoted ids pulled out, so all three parse and ``None`` yields nothing.
_ASSIGNMENT_RE = re.compile(
    r"^(revision|down_revision)\s*(?::[^=\n]+)?=\s*(?P<value>.+)$", re.MULTILINE
)
_QUOTED_RE = re.compile(r"[\"\']([^\"\']+)[\"\']")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ALEMBIC_DIR = os.path.join(_REPO_ROOT, "alembic")
_VERSIONS_DIR = os.path.join(_ALEMBIC_DIR, "versions")


def _migration_files() -> list:
    if not os.path.isdir(_VERSIONS_DIR):
        return []
    return [f for f in os.listdir(_VERSIONS_DIR) if f.endswith(".py")]


def _missing_ancestors() -> set:
    """Revisions referenced as a ``down_revision`` but absent from the checkout.

    Parsed rather than loaded through alembic: alembic raises on the first gap,
    and we want to report every one of them in the failure message.
    """
    present, referenced = set(), set()
    for name in _migration_files():
        with open(os.path.join(_VERSIONS_DIR, name), encoding="utf-8") as fh:
            source = fh.read()
        for match in _ASSIGNMENT_RE.finditer(source):
            ids = _QUOTED_RE.findall(match.group("value"))
            (present if match.group(1) == "revision" else referenced).update(ids)
    return referenced - present


def test_migration_graph_has_a_single_head():
    pytest.importorskip("alembic", reason="alembic isn't installed in this environment")
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not _migration_files():
        pytest.skip("no migrations in this checkout")

    # The revisions are tracked, so a gap is a real mistake: a migration was
    # committed while its parent stayed on someone's machine, and every deploy
    # of this commit would fail to upgrade.
    missing = _missing_ancestors()
    assert not missing, (
        "These revisions are named as a down_revision but are not in "
        f"alembic/versions/: {', '.join(sorted(missing))}. Commit them."
    )

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
