"""OSRS clan rank → Discord app emoji, for the clan chat bridge.

Discord has no inline-image primitive. An embed author icon, a Components V2
thumbnail (``type 11``, "only usable as an accessory in a section") and a media
gallery all sit *beside* a line, never inside it — the only glyph that flows
within a text run is an emoji. So rendering the mirror channel as
``:rank: Player: message`` means uploading the rank set as real custom emoji.

They are **application** emojis: 2000 per app, no guild required and no
``USE_EXTERNAL_EMOJIS`` permission, so the 250-per-server cap that would
otherwise force an emoji-server farm for ~270 ranks never comes into play.
``scripts/seed_rank_emojis.py`` uploads them and writes :data:`MAP_PATH`.

Wiki file casing is irregular — ``Deputy_owner`` but ``Gnome_Child``,
``Record-chaser`` but ``Speed-Runner`` — so no file name is ever rebuilt at
runtime. The seeder resolves real names from the live wiki listing; both sides
meet on :func:`normalize_rank`, which is also what makes WOM's ``deputy_owner``
and the game's ``"Deputy Owner"`` the same key.
"""

import json
import os
import re

#: Written by scripts/seed_rank_emojis.py: {normalized rank: "<:name:id>"}.
MAP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "static", "rank_emojis.json")

#: Discord app emoji names are ``[a-zA-Z0-9_]{2,32}``; the prefix keeps the
#: rank set identifiable among any other emoji the app may own later.
EMOJI_PREFIX = "rank_"

_NON_KEY_RE = re.compile(r"[^a-z0-9]+")

_map_cache = {"mtime": None, "map": {}}


def normalize_rank(rank) -> str:
    """Rank label → the one key every source agrees on.

    WOM reports ``deputy_owner``, the game reports ``"Deputy Owner"`` and the
    wiki file is ``Deputy_owner`` — all three land on ``deputy_owner``.
    Anything that isn't alphanumeric collapses to a single underscore, so
    ``Record-chaser`` and ``record chaser`` agree too."""
    key = _NON_KEY_RE.sub("_", str(rank or "").strip().lower())
    return key.strip("_")


def emoji_name(rank) -> str:
    """Normalized rank → the Discord app emoji name the seeder uploads."""
    return f"{EMOJI_PREFIX}{normalize_rank(rank)}"


def load_map(path: str = None) -> dict:
    """``{normalized rank: "<:name:id>"}``, memoized on the file's mtime.

    A missing or unreadable map is not an error — it just means the seeder
    hasn't run against this app yet, and every lookup returns None so the
    bridge falls back to plain ``**Name**: message`` lines."""
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
            _map_cache["map"] = {normalize_rank(k): v for k, v in (data or {}).items() if v}
        except (OSError, ValueError):
            _map_cache["map"] = {}
        _map_cache["mtime"] = mtime
    return _map_cache["map"]


def emoji_for_rank(rank, emoji_map: dict = None) -> str:
    """``"<:rank_deputy_owner:123>"`` for a rank label, or None.

    None is the normal path for ranks with no wiki icon ("Not Ranked", WOM's
    default ``member``, a clan using a title the wiki doesn't have) — callers
    render the line without a glyph rather than a broken ``<::>``."""
    key = normalize_rank(rank)
    if not key:
        return None
    source = load_map() if emoji_map is None else emoji_map
    return source.get(key) or None
