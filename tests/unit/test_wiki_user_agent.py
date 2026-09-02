"""One identity for the OSRS Wiki, and never a blocklisted one.

We have been blocklisted twice. Both times a *second copy* of the User-Agent
string is what made it expensive: the 2026-08-20 block was fixed in
osrs_api/client.py while utils/ge_value.py kept its own literal, and that copy
was still being sent when prices.runescape.wiki blocklisted it on 2026-08-28 —
five days of every drop valued from the client instead of the GE, and 128
override-priced drops stored at 0.

So this module fails the build if either mistake reappears in the tree.
"""
import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Directories with no bearing on what we send to the wiki at runtime.
_SKIP_DIRS = {
    ".git", ".claude", "venv", ".venv", "__pycache__", "node_modules",
    "logs", "static", "docs", "alembic",
}

# This test names the blocklisted strings, so it must not match itself, and
# utils/wiki_ua.py records them deliberately as the blocklist.
_ALLOWED = {
    os.path.join("tests", "unit", "test_wiki_user_agent.py"),
    os.path.join("utils", "wiki_ua.py"),
}


def _python_files():
    for root, dirs, files in os.walk(_REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, _REPO_ROOT)
            if rel in _ALLOWED:
                continue
            yield rel, path


def test_blocklisted_user_agents_are_not_used_anywhere():
    from utils.wiki_ua import BLOCKLISTED_USER_AGENTS

    offenders = []
    for rel, path in _python_files():
        try:
            source = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for blocked in BLOCKLISTED_USER_AGENTS:
            if blocked in source:
                offenders.append(f"{rel}: {blocked!r}")

    assert not offenders, (
        "Blocklisted OSRS Wiki User-Agent(s) found — the wiki refuses these at "
        "the edge and the 403 is easy to misread as 'this item has no price'. "
        "Import utils.wiki_ua.USER_AGENT instead:\n  " + "\n  ".join(offenders)
    )


def test_wiki_callers_import_the_shared_user_agent():
    """No module may spell its own UA for a runescape.wiki host.

    A private copy is exactly the drift that left ge_value.py blocklisted for
    five days after the "fix" landed next door.
    """
    ua_literal = re.compile(r"""User-Agent['"]\s*:\s*['"]""")
    offenders = []
    for rel, path in _python_files():
        try:
            source = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if "runescape.wiki" not in source:
            continue
        if ua_literal.search(source):
            offenders.append(rel)

    assert not offenders, (
        "Module(s) talking to a runescape.wiki host with a hardcoded "
        "User-Agent literal; use utils.wiki_ua.USER_AGENT:\n  "
        + "\n  ".join(offenders)
    )


def test_current_user_agent_is_not_duplicated():
    """Even a *correct* copy is a liability.

    The next block lands on the identity, not on one module's spelling of it;
    a second copy is a module that won't be fixed when the first one is.
    """
    from utils.wiki_ua import USER_AGENT

    offenders = [
        rel for rel, path in _python_files()
        if USER_AGENT in open(path, encoding="utf-8", errors="ignore").read()
    ]
    assert not offenders, (
        "The shared User-Agent string is copied into:\n  " + "\n  ".join(offenders)
        + "\nImport utils.wiki_ua.USER_AGENT instead of repeating the literal."
    )


def test_shared_user_agent_is_descriptive_with_contact():
    """The wiki's API etiquette policy asks for identification + a contact."""
    from utils.wiki_ua import USER_AGENT

    assert "droptracker.io" in USER_AGENT.lower()
    assert "contact" in USER_AGENT.lower()
