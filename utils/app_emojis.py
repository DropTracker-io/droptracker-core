"""The bot's own emoji, owned by the application instead of by a guild.

Every custom emoji the bot posted used to be a **guild** emoji uploaded to one
of the servers the bot happens to be in. Posting one anywhere else costs the
sending bot ``USE_EXTERNAL_EMOJIS`` in the destination channel — a permission
plenty of clans do not grant, and one that @everyone can lose to a single role
override nobody remembers making. Where it is missing the emoji does not
degrade: the raw ``<:supporter:1263827303712948304>`` shows up in the message.

**Application** emojis have no such requirement. They belong to the app rather
than to a guild, work in every channel the bot can already speak in, and cap at
2000 per app instead of 250 per server. ``scripts/seed_app_emojis.py`` uploads
them and writes :data:`MAP_PATH`; this module is the read side.

Two things this has to get right that the rank set (``utils/rank_emojis.py``,
same pattern, its own map) does not:

* **An app emoji only renders for the app that owns it.** The DropTracker core
  bot and the Hall of Fame bot are separate Discord applications, and
  ``services/hall_of_fame.py`` is loaded by *both* processes — so the same line
  of code has to resolve to a different id depending on which one is running
  it. Hence the map is keyed by profile and every process declares its own via
  :func:`use_profile` at startup.
* **A missing emoji must not become a missing message.** Every key carries a
  Unicode fallback, so code that has been switched over renders sensibly before
  the seeder has ever run, on a profile that was never seeded, and for the one
  key whose original art is already gone from the CDN (``join``). Nothing here
  raises.

Deliberately pure: stdlib only at import time, no DB and no network, so the
bots, the notification service and the tests can all import it.
"""

from __future__ import annotations

import json
import os
import re
from typing import NamedTuple, Optional

#: Written by scripts/seed_app_emojis.py: {profile: {key: "<:name:id>"}}.
MAP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "app_emojis.json"
)

#: Profile -> the .env key holding that application's bot token. The seeder
#: reads it to know which app to upload to; keeping the table here is what
#: stops the two sides drifting into different profile spellings.
PROFILE_TOKENS = {
    "core": "BOT_TOKEN",       # bots/main.py — slash commands, notifications, panels
    "core-dev": "DEV_TOKEN",   # the same code with STATE=dev, a separate Discord app
    "hof": "HALL_OF_FAME_BOT_TOKEN",  # bots/hall_of_fame.py
}

DEFAULT_PROFILE = "core"

#: Discord's app emoji names are ``[a-zA-Z0-9_]{2,32}``.
_VALID_NAME = re.compile(r"^[A-Za-z0-9_]{2,32}$")


class Spec(NamedTuple):
    """One emoji: what to call it, what to show without it, where its art is."""

    #: Emoji name on the application. Also the registry key, lowercased.
    name: str
    #: Unicode stand-in used whenever the app emoji is unavailable.
    fallback: str
    #: Animated art has to be uploaded (and fetched) as a GIF.
    animated: bool
    #: The guild emoji this was migrated from — the seeder pulls the art from
    #: ``cdn.discordapp.com/emojis/{legacy_id}``. None once no source survives.
    legacy_id: Optional[int]
    #: What it marks, for the seeder's listing and for whoever adds the next one.
    purpose: str


SPECS = {
    "construction": Spec(
        "construction", "🚧", False, 1533062962418417704,
        "POH Construction level requirement (claim-rsn, Hall of Fame sync note)",
    ),
    "loading": Spec(
        "loading", "⏳", True, 1180923500836421715,
        "placeholder while a lootboard or a new group initialises",
    ),
    "supporter": Spec(
        "supporter", "⭐", False, 1263827303712948304,
        "premium / upgrade prompts and upgrade announcements",
    ),
    "droptracker": Spec(
        "droptracker", "📊", True, 1346787143778963497,
        "brand mark on account-upgrade thank-you messages",
    ),
    "screenshot": Spec(
        "screenshot", "📸", False, 1380839233123651695,
        "RuneLite's screenshot button, in the client-log walkthrough",
    ),
    "join": Spec(
        # The guild emoji this came from is gone (its CDN entry 404s), so there
        # is no art to migrate and the fallback is the live rendering until
        # someone drops a PNG in static/emoji/join.png for the seeder to use.
        "join", "📥", False, None,
        "member added to a group by a WiseOldMan refresh",
    ),
    "leave": Spec(
        "leave", "📤", False, 1213802516882530375,
        "member removed from a group by a WiseOldMan refresh",
    ),
    "newmember": Spec(
        "newmember", "🆕", False, 1263916335184744620,
        "\"player setup\" button on the help/info panels",
    ),
    "developer": Spec(
        "developer", "🛠️", False, 1263916346954088558,
        "\"clan setup\" button on the help/info panels",
    ),
}

_state = {"profile": os.getenv("DROPTRACKER_EMOJI_PROFILE") or DEFAULT_PROFILE}
_map_cache = {"mtime": None, "map": {}}


def use_profile(profile: str) -> None:
    """Declare which application this process is running as.

    Called once by each bot before it connects. ``services/hall_of_fame.py``
    runs inside two different applications, so without this the HOF process
    would post ids that only the core app owns — which renders as raw
    ``<:construction:...>`` text, not as nothing.
    """
    _state["profile"] = str(profile or DEFAULT_PROFILE)


def current_profile() -> str:
    """The profile this process resolves against."""
    return _state["profile"]


def load_map(path: str = None) -> dict:
    """``{profile: {key: "<:name:id>"}}``, memoized on the file's mtime.

    A missing or unreadable map is not an error — it means the seeder has not
    run yet, and every lookup falls back to Unicode.
    """
    path = path or MAP_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _map_cache["mtime"], _map_cache["map"] = None, {}
        return {}
    if _map_cache["mtime"] != mtime:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            _map_cache["map"] = {
                str(profile): {str(k): v for k, v in (entries or {}).items() if v}
                for profile, entries in (data or {}).items()
                if isinstance(entries, dict)
            }
        except (OSError, ValueError):
            _map_cache["map"] = {}
        _map_cache["mtime"] = mtime
    return _map_cache["map"]


def emoji(key: str, profile: str = None) -> str:
    """``"<:construction:123>"`` for ``key``, or its Unicode fallback.

    Never raises and never returns empty: an unknown key yields ``""`` only if
    it is genuinely not in :data:`SPECS`, which a unit test forbids.
    """
    spec = SPECS.get(key)
    if spec is None:
        return ""
    entry = load_map().get(profile or current_profile(), {}).get(key)
    return entry or spec.fallback


def partial_emoji(key: str, profile: str = None):
    """:func:`emoji` as a ``PartialEmoji``, for buttons and select options.

    ``PartialEmoji.from_str`` reads both forms, so the Unicode fallback makes a
    valid button emoji too. Imported lazily to keep this module importable
    without a Discord library present.
    """
    from interactions import PartialEmoji

    return PartialEmoji.from_str(emoji(key, profile))


def emoji_names() -> dict:
    """``{key: app emoji name}`` — what the seeder should own on every app."""
    return {key: spec.name for key, spec in SPECS.items()}


def validate_specs() -> list:
    """Reasons the registry could not be seeded as written (empty when fine)."""
    problems = []
    for key, spec in SPECS.items():
        if not _VALID_NAME.match(spec.name):
            problems.append(f"{key}: {spec.name!r} is not a valid Discord emoji name")
        if not spec.fallback:
            problems.append(f"{key}: no Unicode fallback")
    names = [spec.name for spec in SPECS.values()]
    for name in sorted(set(n for n in names if names.count(n) > 1)):
        problems.append(f"{name!r} is claimed by more than one key")
    return problems
