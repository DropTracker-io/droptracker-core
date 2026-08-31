"""Things worth asserting on that a fresh checkout does not contain.

A few of our best checks have a subject that lives outside the repository: the
item and NPC icons under ``static/assets/img`` are gitignored, so are the
migration files in ``alembic/versions``, and the shared TypeScript registries
belong to the *web* repository next door. CI clones this repository and nothing
else, so it has none of them — and neither does a contributor on their first
day.

Such a check is still worth keeping; it just has to distinguish "this is
wrong" from "I cannot see it from here", and report the second as a skip. The
rule it exists to protect: **``tests/unit`` passes on a clean checkout.** A
suite that cannot is a suite whose red is uninformative, and a red nobody can
act on is a red everybody learns to scroll past.

Reach for ``skip_without`` when the missing thing makes the assertion
unanswerable. Prefer, where you can, to split the check instead: assert the
part that is answerable anywhere (``validate_manifest(check_art=False)`` is the
worked example) and let only the environment-dependent remainder skip.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Repository root — this file is ``<root>/tests/local_artifacts.py``.
REPO_ROOT = Path(__file__).resolve().parents[1]


def skip_without(present: bool, what: str, hint: str = "") -> None:
    """Skip the calling test when ``what`` is not available in this checkout.

    ``present`` is the caller's own existence check, so the reason can name the
    artifact in the caller's terms rather than a path this module guessed at.
    """
    __tracebackhide__ = True  # report the skip against the test, not this helper
    if present:
        return
    reason = f"{what} is not part of a clean checkout, so this cannot be checked here"
    pytest.skip(f"{reason}. {hint}" if hint else reason)


def web_repo_root() -> Path | None:
    """The sibling web repository, or ``None`` if it is not beside this one.

    The two repositories are developed side by side (``droptracker/disc`` and
    ``droptracker/web``), which is the layout the parity checks assume. It used
    to be written as one absolute path, which meant the checks ran on exactly
    one machine and quietly skipped on every other — including every
    contributor's. ``DROPTRACKER_WEB_ROOT`` overrides for anyone who keeps it
    somewhere else.
    """
    override = os.environ.get("DROPTRACKER_WEB_ROOT")
    candidates = [Path(override)] if override else []
    candidates += [REPO_ROOT.parent / "web", REPO_ROOT.parent / "droptracker-web"]
    for candidate in candidates:
        if (candidate / "packages" / "api-types").is_dir():
            return candidate
    return None


def ts_registry(relative: str) -> Path | None:
    """A TypeScript source file inside the sibling web repo's shared types."""
    root = web_repo_root()
    if root is None:
        return None
    path = root / "packages" / "api-types" / "src" / relative
    return path if path.is_file() else None


def plugin_repo_root() -> Path | None:
    """The sibling RuneLite plugin repository, or ``None`` if not beside us.

    Same side-by-side layout as :func:`web_repo_root` (``droptracker/disc`` and
    ``droptracker/plugin``). Used by the checks that guard a deliberate
    duplication — region data and the safe/dangerous region sets exist in both
    repositories, and the point of the copy is that they agree.
    ``DROPTRACKER_PLUGIN_ROOT`` overrides.
    """
    override = os.environ.get("DROPTRACKER_PLUGIN_ROOT")
    candidates = [Path(override)] if override else []
    candidates += [REPO_ROOT.parent / "plugin", REPO_ROOT.parent / "droptracker-plugin"]
    for candidate in candidates:
        if (candidate / "src" / "main" / "java" / "io" / "droptracker").is_dir():
            return candidate
    return None
