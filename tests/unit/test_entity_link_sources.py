"""No Discord-facing link builds a site URL out of a display name.

The bug this exists to stop shipping again: ``/claim-rsn``'s success embed
built its "your profile" link as
``https://www.droptracker.io/players/{result.get('player_name')}``. OSRS names
carry spaces (``Beast Owned``), and Discord's ``[label](url)`` parser ends the
URL at the first space — so the embed rendered a dead half-link followed by
loose text for exactly the players whose names had a space in them.

``utils/site_urls.py`` is the answer (id-based, one place to change), but a
one-off f-string bypasses it silently and reads fine in review. So this scans
the tracked source instead of any single call site: a name-shaped expression
inside an entity URL path is a finding, wherever it is written.
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Entity routes whose segment is an id. ``/wiki/``, ``/img/...`` and the
#: search route legitimately take names and are not listed.
ENTITY_ROUTES = ("players", "groups", "npcs", "items", "events")

#: ``.../players/{...}`` — capture the interpolated expression.
_URL_PLACEHOLDER = re.compile(
    r"droptracker\.io/(?:%s)/\{([^{}]+)\}" % "|".join(ENTITY_ROUTES)
)

#: An expression that reads like a display name rather than an id.
#: ``player_name`` and ``npc_name`` are the real offenders. Anything id-shaped
#: is checked first and wins, so a hybrid like ``group_name_id`` is not a hit.
_NAME_ISH = re.compile(r"name|rsn|title", re.I)
_ID_ISH = re.compile(r"_id\b|\bid\b", re.I)


def _tracked_python_files():
    """Every .py file git tracks.

    Deliberately git, not a filesystem walk: the working tree carries ``.bak``
    copies, stale ``.claude/worktrees/`` clones and ``__pycache__`` that would
    make this fail on code nobody ships.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def _findings():
    hits = []
    for path in _tracked_python_files():
        if "tests" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for expression in _URL_PLACEHOLDER.findall(line):
                if _ID_ISH.search(expression):
                    continue
                if _NAME_ISH.search(expression):
                    rel = path.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{number}: {expression.strip()}")
    return hits


def test_no_entity_url_is_built_from_a_name():
    hits = _findings()
    assert not hits, (
        "These build a site entity URL from a display name — a name with a "
        "space breaks the Discord link. Use utils/site_urls.py with the id:\n  "
        + "\n  ".join(hits)
    )


def test_the_scanner_catches_the_shape_it_is_meant_to_catch():
    # A guard with a typo'd regex passes forever and protects nothing.
    offender = "url = f\"https://www.droptracker.io/players/{result.get('player_name')}\""
    assert _URL_PLACEHOLDER.findall(offender), "scanner no longer sees the original bug"
    expression = _URL_PLACEHOLDER.findall(offender)[0]
    assert _NAME_ISH.search(expression) and not _ID_ISH.search(expression)


@pytest.mark.parametrize(
    "line",
    [
        'f"https://www.droptracker.io/players/{player_id}"',
        'f"https://www.droptracker.io/groups/{group.group_id}/subscription"',
        'f"https://www.droptracker.io/npcs/{npc_id}"',
        'f"https://www.droptracker.io/img/npcdb/{npc_name}.png"',
    ],
)
def test_the_scanner_does_not_fire_on_id_routes_or_asset_paths(line):
    for expression in _URL_PLACEHOLDER.findall(line):
        assert _ID_ISH.search(expression) or not _NAME_ISH.search(expression)
