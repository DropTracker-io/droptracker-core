"""Keeps the advertised plugin version in step with the Plugin Hub by itself.

The server tells every client the latest plugin version (``/plugin_version``,
row ``plugin_latest_version`` on group 2), and an older client nags its
player to update. Until 2026-11-19 a person edited that row after each
release, so for days after a release nobody was told about it.

A release has three states, and only the last may be advertised:

1. merged on our ``master``;
2. **approved**: the Plugin Hub's ``plugins/droptracker`` pins the commit;
3. **released**: the hub has built and published it, which can trail the pin
   by days.

Advertising at state 2 tells players to fetch a build that does not exist.
So two signals are required: the hub pins a commit whose version is newer
than the advertised one, AND enough real players are already submitting
with that version (``player_plugin_versions``), which only a published build
can produce in numbers. Testers on dev builds report new versions early; the
threshold is far above how many of them there are.

It only ever moves the version forward, and only to exactly what the hub pins.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional, Tuple

HUB_PIN_URL = "https://raw.githubusercontent.com/runelite/plugin-hub/master/plugins/droptracker"
BUILD_FILE_URL = "https://raw.githubusercontent.com/{repo}/{commit}/build.gradle"
OUR_REPOSITORY = "joelhalen/droptracker-plugin"

GLOBAL_GROUP_ID = 2
CONFIG_KEY = "plugin_latest_version"
# Distinct players on the pinned version, seen recently, before it counts as
# released. Dev and tester builds account for about a dozen.
MIN_PLAYERS = int(os.getenv("PLUGIN_RELEASE_MIN_PLAYERS", "50"))
SEEN_WITHIN_HOURS = 24

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_GRADLE_VERSION = re.compile(r"^\s*version\s*=\s*['\"]([0-9]+(?:\.[0-9]+){1,2})['\"]\s*$", re.M)
_REPO = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def parse_pin(text_: Any) -> Optional[Tuple[str, str]]:
    """``(owner/repo, commit)`` from the hub's plugin file, or None if it is
    not a well-formed pin of a GitHub repository."""
    if not isinstance(text_, str):
        return None
    fields = {}
    for line in text_.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    repo = _REPO.search(fields.get("repository", ""))
    commit = fields.get("commit", "")
    if repo is None or not _COMMIT.match(commit):
        return None
    return repo.group(1), commit


def parse_gradle_version(text_: Any) -> Optional[str]:
    """The ``version = '6.1.29'`` a build.gradle declares, or None."""
    if not isinstance(text_, str):
        return None
    match = _GRADLE_VERSION.search(text_)
    return match.group(1) if match else None


def decide(advertised: Optional[str], pinned: Optional[str], players_on_pinned: int,
           min_players: int = MIN_PLAYERS) -> Tuple[Optional[str], str]:
    """``(version to advertise or None, why)``."""
    from utils.plugin_versions import version_tuple

    pinned_tuple = version_tuple(pinned)
    if pinned_tuple is None:
        return None, "could not read the version the Plugin Hub pins"
    advertised_tuple = version_tuple(advertised)
    if advertised_tuple is not None and pinned_tuple <= advertised_tuple:
        return None, f"already advertising {advertised}; the hub pins {pinned}"
    if players_on_pinned < min_players:
        return None, (f"the hub pins {pinned} but only {players_on_pinned} players are on it "
                      f"(need {min_players}): approved, not yet released")
    return pinned, (f"the hub pins {pinned} and {players_on_pinned} players are already on it; "
                    f"was advertising {advertised or 'nothing'}")


# --------------------------------------------------------------------------- #
# The parts that touch the outside world
# --------------------------------------------------------------------------- #

def _fetch(url: str) -> str:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "droptracker-plugin-release-check"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def pinned_version(fetch=_fetch) -> Tuple[Optional[str], Optional[str]]:
    """``(version, commit)`` the Plugin Hub pins for us right now.

    ``(None, None)`` if the hub file cannot be read or does not point at our
    repository (the automation must never follow a pin to somebody else's).
    """
    pin = parse_pin(fetch(HUB_PIN_URL))
    if pin is None or pin[0].lower() != OUR_REPOSITORY:
        return None, None
    repo, commit = pin
    return parse_gradle_version(fetch(BUILD_FILE_URL.format(repo=repo, commit=commit))), commit


def players_on(db_session, version: str) -> int:
    from sqlalchemy import text

    return int(db_session.execute(text(
        "SELECT COUNT(DISTINCT player_id) FROM player_plugin_versions "
        "WHERE version = :v AND last_seen >= UTC_TIMESTAMP() - INTERVAL :h HOUR"
    ), {"v": version, "h": SEEN_WITHIN_HOURS}).scalar() or 0)


def advertised_row(db_session):
    """The row the endpoint reads. Rows for one key are not unique in this
    table, and the endpoint takes whichever comes last; so does this."""
    from db.models import GroupConfiguration

    rows = (
        db_session.query(GroupConfiguration)
        .filter(GroupConfiguration.group_id == GLOBAL_GROUP_ID,
                GroupConfiguration.config_key == CONFIG_KEY)
        .all()
    )
    filled = [row for row in rows if (row.config_value or "").strip()]
    return filled[-1] if filled else None


def sync(db_session, *, apply: bool, fetch=_fetch, min_players: int = MIN_PLAYERS) -> dict:
    """One check. Returns ``{"changed", "advertised", "pinned", "commit",
    "players", "reason"}``. The caller commits when ``changed``."""
    from db.models import GroupConfiguration

    row = advertised_row(db_session)
    advertised = (row.config_value or "").strip() if row is not None else None
    version, commit = pinned_version(fetch)
    players = players_on(db_session, version) if version else 0
    new, reason = decide(advertised, version, players, min_players)
    result = {"changed": False, "advertised": advertised, "pinned": version, "commit": commit,
              "players": players, "reason": reason}
    if new is None or not apply:
        result["would_change"] = new is not None
        return result
    if row is None:
        db_session.add(GroupConfiguration(group_id=GLOBAL_GROUP_ID, config_key=CONFIG_KEY,
                                          config_value=new))
    else:
        row.config_value = new
    db_session.flush()
    result["changed"] = True
    return result
