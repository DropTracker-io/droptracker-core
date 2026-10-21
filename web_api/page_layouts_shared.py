"""Page layouts (web137a): the block vocabulary, limits and validator.

Pure (no Quart, no DB) so it unit-tests directly. Routes call
``validate_layout(raw, scope)`` and turn ``LayoutError`` into a 422.

A layout is::

    {"version": 1,
     "theme":  {...page-level look, see THEME_SPEC...},
     "blocks": [{"id", "type", "width", "surface", "title", "hide_on", ...props}]}

The validator NORMALIZES: unknown keys are dropped, enums fall back to their
default, ints are clamped. Only free text and links are rejected outright,
because silently truncating something a person typed is worse than telling
them. A block that is merely unfinished (no image chosen yet, no boss picked)
is valid: drafts autosave while someone is mid-edit, and the public renderer
skips a block that has nothing to show. The page header (icon, name, description) is not a block: it always
renders first, so every page keeps the site's identity in the same place.

The frontend mirrors this vocabulary in
``packages/api-types/src/page-layouts.ts``. A new block type or prop needs
both, plus a renderer, an inspector form and a library entry.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from web_api.sites_shared import ALLOWED_PALETTE_KEYS, palette_value_ok

LAYOUT_VERSION = 1
MAX_BLOCKS = 40
MAX_LAYOUT_JSON_BYTES = 256 * 1024
MAX_PALETTE_KEYS = 24

WIDTHS = ("full", "two_thirds", "half", "third")
# OSRS-styled containers a block can sit in. "plain" draws nothing.
SURFACES = ("plain", "card", "stone", "window", "parchment", "scroll", "panel")
HIDE_ON = ("none", "mobile", "desktop")

# Built-in links a group page can show (rendered only when the group actually
# has the thing: Discord set, points on, a recap frozen, ...).
GROUP_LINK_KEYS = (
    "lootboard", "clan_log", "personal_bests", "pvp", "points",
    "recaps", "events", "discord",
)
PLAYER_LINK_KEYS = ("collection_log", "achievements", "recap")
LINK_STYLES = ("outline", "solid", "osrs")
ALIGNS = ("left", "center", "right")

THEME_PRESETS = ("default", "dusk", "parchment", "wilderness")
BACKGROUNDS = ("default", "plain", "stone", "parchment", "starry")
BANNER_HEIGHTS = ("sm", "md", "lg")
HEADER_STYLES = ("standard", "centered", "banner")

_IMG_PATH_RE = re.compile(r"^/img/[A-Za-z0-9_./-]{1,250}$")


class LayoutError(ValueError):
    """A layout a person submitted that cannot be saved. The message is shown
    to them as-is, so it says what to fix."""


# --- prop specs ---------------------------------------------------------------
# Each spec is a callable (value, where) -> normalized value. ``where`` is a
# human label for error messages ("Block 3 (Text)").

Spec = Callable[[Any, str], Any]


def int_range(lo: int, hi: int, default: int) -> Spec:
    def spec(v: Any, where: str) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))
    return spec


def enum(choices: tuple, default: str) -> Spec:
    def spec(v: Any, where: str) -> str:
        return v if v in choices else default
    return spec


def boolean(default: bool) -> Spec:
    def spec(v: Any, where: str) -> bool:
        return v if isinstance(v, bool) else default
    return spec


def text(max_len: int, *, required: bool = False, default: str = "") -> Spec:
    def spec(v: Any, where: str) -> str:
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise LayoutError(f"{where}: expected text.")
        v = v.strip()
        if len(v) > max_len:
            raise LayoutError(f"{where}: keep it under {max_len} characters ({len(v)} now).")
        if required and not v:
            raise LayoutError(f"{where}: this can't be empty.")
        return v or default
    return spec


def link_url(v: Any, where: str) -> str:
    """https:// or a site-relative path. Everything else (javascript:, data:,
    protocol-relative //evil) is refused."""
    if not isinstance(v, str) or not v.strip():
        raise LayoutError(f"{where}: a link is required.")
    v = v.strip()
    if len(v) > 300:
        raise LayoutError(f"{where}: links can be at most 300 characters.")
    if v.startswith("/") and not v.startswith("//") and not v.startswith("/\\"):
        return v
    if v.lower().startswith("https://") and len(v) > len("https://"):
        return v
    raise LayoutError(f"{where}: links must start with https:// (or / for a page on this site).")


def image_url(v: Any, where: str) -> str:
    """https:// images, or our own /img/ asset tree."""
    if not isinstance(v, str) or not v.strip():
        raise LayoutError(f"{where}: an image address is required.")
    v = v.strip()
    if len(v) > 300:
        raise LayoutError(f"{where}: image addresses can be at most 300 characters.")
    if _IMG_PATH_RE.match(v) or (v.lower().startswith("https://") and len(v) > 8):
        return v
    raise LayoutError(f"{where}: images must be an https:// address.")


def optional(spec: Spec) -> Spec:
    def wrapped(v: Any, where: str) -> Any:
        if v is None or v == "":
            return None
        return spec(v, where)
    return wrapped


def key_list(choices: tuple, default: tuple, max_items: int) -> Spec:
    """Ordered, de-duplicated subset of ``choices``."""
    def spec(v: Any, where: str) -> list:
        if not isinstance(v, list):
            return list(default)
        out: list = []
        for item in v:
            if item in choices and item not in out:
                out.append(item)
        return out[:max_items]
    return spec


def _button_items(v: Any, where: str) -> list:
    if not isinstance(v, list):
        return []
    if len(v) > 8:
        raise LayoutError(f"{where}: at most 8 buttons.")
    out = []
    for n, item in enumerate(v, 1):
        if not isinstance(item, dict):
            raise LayoutError(f"{where}: button {n} is malformed.")
        out.append({
            "label": text(40, required=True)(item.get("label"), f"{where}, button {n} label"),
            "href": link_url(item.get("href"), f"{where}, button {n} link"),
        })
    return out


def _pb_bosses(v: Any, where: str) -> list:
    if not isinstance(v, list):
        return []
    out = []
    for item in v[:8]:
        if not isinstance(item, dict):
            continue
        try:
            npc_id = int(item.get("npc_id"))
        except (TypeError, ValueError):
            continue
        sizes = item.get("team_sizes")
        sizes = [s for s in sizes if isinstance(s, str) and len(s) <= 8][:8] \
            if isinstance(sizes, list) else []
        out.append({"npc_id": npc_id, "team_sizes": sizes})
    return out


def _npc_id(v: Any, where: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise LayoutError(f"{where}: pick a boss.")
    if n <= 0:
        raise LayoutError(f"{where}: pick a boss.")
    return n


GROUP_STATS = (
    "members", "rank", "monthly_loot", "top_player", "top_boss", "records_held",
    "clan_log", "pvp_kills", "pvp_loot", "points_leader", "bank_balance", "events_running",
)
PLAYER_STATS = (
    "monthly_loot", "rank", "clan_points", "top_npc", "last_month_loot", "clog_slots",
    "ca_tasks", "quests", "combat_level", "pbs_count", "badges_count", "pvp_kills",
)
# What the stats block shows when someone adds it: the classic four.
GROUP_STATS_DEFAULT = ("members", "rank", "monthly_loot", "top_player")
PLAYER_STATS_DEFAULT = ("monthly_loot", "rank", "clan_points", "top_npc")
STAT_COLUMNS = ("auto", "2", "3", "4")
# What the highlights feed can filter on (routes/page_layouts.py serves it).
HIGHLIGHT_TYPES = ("drop", "pet", "clog", "pb")

# --- block catalog --------------------------------------------------------------
# type -> (label, scopes, default surface, props)
# scopes: "group" pages, "player" profiles (member_default uses "player").

_SHARED: dict[str, tuple[str, tuple, str, dict[str, Spec]]] = {
    "markdown": ("Text", ("group", "player"), "plain", {
        "body": text(8000),
        "align": enum(ALIGNS, "left"),
    }),
    "image": ("Image", ("group", "player"), "plain", {
        "url": optional(image_url),
        "alt": text(200),
        "caption": text(300),
        "fit": enum(("cover", "contain"), "cover"),
        "height": enum(("sm", "md", "lg", "auto"), "md"),
        "link": optional(link_url),
    }),
    "buttons": ("Buttons", ("group", "player"), "plain", {
        "items": _button_items,
        "style": enum(LINK_STYLES, "solid"),
        "align": enum(ALIGNS, "left"),
    }),
    "divider": ("Divider", ("group", "player"), "plain", {
        "size": enum(("sm", "md", "lg"), "md"),
        "rule": boolean(True),
    }),
}

_GROUP: dict[str, tuple[str, tuple, str, dict[str, Spec]]] = {
    "group_links": ("Clan links", ("group",), "plain", {
        "items": key_list(GROUP_LINK_KEYS, GROUP_LINK_KEYS, len(GROUP_LINK_KEYS)),
        "style": enum(LINK_STYLES, "outline"),
        "align": enum(ALIGNS, "left"),
    }),
    "support_card": ("Support this clan", ("group",), "plain", {}),
    "stats_row": ("Stats", ("group",), "plain", {
        "stats": key_list(GROUP_STATS, GROUP_STATS_DEFAULT, 8),
        "columns": enum(STAT_COLUMNS, "auto"),
    }),
    "bank": ("Clan bank", ("group",), "plain", {}),
    "records": ("Clan records", ("group",), "plain", {
        "limit": int_range(1, 24, 8),
    }),
    "top_players": ("Top players", ("group",), "card", {
        "limit": int_range(1, 25, 10),
    }),
    "boss_activity": ("Most active bosses", ("group",), "card", {
        "limit": int_range(1, 25, 10),
    }),
    "recent_drops": ("Recent drops", ("group",), "plain", {
        "limit": int_range(1, 25, 10),
    }),
    "lootboard": ("Lootboard", ("group",), "card", {
        "period": enum(("month", "week", "day", "all"), "month"),
    }),
    "pb_board": ("Personal best board", ("group",), "card", {
        "bosses": _pb_bosses,
        "rows": int_range(3, 25, 5),
    }),
    "leaderboard": ("Loot leaderboard", ("group",), "card", {
        "limit": int_range(3, 50, 10),
        "period": enum(("month", "week", "day", "all"), "month"),
    }),
    "clan_log": ("Clan Log", ("group",), "card", {
        "view": enum(("summary", "board"), "summary"),
        "show_categories": boolean(True),
        "recent": int_range(0, 20, 6),
    }),
    "points_leaderboard": ("Clan points", ("group",), "card", {
        "limit": int_range(3, 50, 10),
    }),
    "pvp_summary": ("PvP", ("group",), "card", {
        "period": enum(("month", "all"), "month"),
        "limit": int_range(3, 25, 5),
        "show_recent": boolean(True),
    }),
    "events_list": ("Events", ("group",), "card", {
        "show": enum(("active", "active_and_past"), "active_and_past"),
        "limit": int_range(1, 10, 4),
    }),
    "highlights": ("Highlights", ("group",), "card", {
        "types": key_list(HIGHLIGHT_TYPES, ("pet", "clog", "pb"), len(HIGHLIGHT_TYPES)),
        "limit": int_range(1, 25, 8),
    }),
    "npc_board": ("Boss leaderboard", ("group",), "card", {
        "npc_id": optional(_npc_id),
        "npc_name": text(80),
        "period": enum(("month", "week", "day", "all"), "month"),
        "limit": int_range(3, 50, 10),
    }),
    "recap": ("Recap card", ("group",), "plain", {
        "period": enum(("month", "year"), "month"),
    }),
    "announcements": ("Announcements", ("group",), "card", {
        "limit": int_range(1, 10, 3),
    }),
    "event_standings": ("Event standings", ("group",), "card", {
        "event_id": optional(int_range(1, 2**31 - 1, 0)),
    }),
    "wom_achievements": ("Recent achievements", ("group",), "card", {
        "limit": int_range(1, 25, 10),
    }),
    "member_roster": ("Member list", ("group",), "card", {
        "limit": int_range(5, 200, 50),
        "sort": enum(("monthly", "all_time", "name"), "monthly"),
        "layout": enum(("cards", "table"), "cards"),
        "show_rank": boolean(True),
        "sortable": boolean(True),
    }),
    "live_ticker": ("Live drop feed", ("group",), "plain", {}),
}

_PLAYER: dict[str, tuple[str, tuple, str, dict[str, Spec]]] = {
    "player_links": ("Profile links", ("player",), "plain", {
        "items": key_list(PLAYER_LINK_KEYS, PLAYER_LINK_KEYS, len(PLAYER_LINK_KEYS)),
        "style": enum(LINK_STYLES, "outline"),
        "align": enum(ALIGNS, "left"),
    }),
    "player_showcase": ("Character and account", ("player",), "plain", {}),
    "player_stats": ("Stats", ("player",), "plain", {
        "stats": key_list(PLAYER_STATS, PLAYER_STATS_DEFAULT, 8),
        "columns": enum(STAT_COLUMNS, "auto"),
    }),
    "player_clog": ("Collection log", ("player",), "card", {
        "view": enum(("summary", "browser"), "summary"),
    }),
    "player_ca": ("Combat achievements", ("player",), "card", {
        "view": enum(("summary", "browser"), "summary"),
    }),
    "player_quests": ("Quests and diaries", ("player",), "card", {}),
    "player_highlights": ("Highlights", ("player",), "card", {
        "types": key_list(HIGHLIGHT_TYPES, ("pet", "clog", "pb"), len(HIGHLIGHT_TYPES)),
        "limit": int_range(1, 25, 8),
    }),
    "personal_bests": ("Personal bests", ("player",), "plain", {
        "limit": int_range(1, 60, 12),
    }),
    "top_bosses": ("Top bosses", ("player",), "card", {
        "limit": int_range(1, 25, 10),
    }),
    "player_groups": ("Groups", ("player",), "plain", {}),
    "player_pvp": ("PvP record", ("player",), "plain", {}),
    "player_recent_drops": ("Recent drops", ("player",), "card", {
        "limit": int_range(1, 25, 10),
    }),
    "player_badges": ("Badges", ("player",), "plain", {}),
}

BLOCK_CATALOG: dict[str, tuple[str, tuple, str, dict[str, Spec]]] = {
    **_SHARED, **_GROUP, **_PLAYER,
}


def block_types_for(scope: str) -> tuple[str, ...]:
    """Block types a layout of ``scope`` may hold. member_default layouts are
    player profiles, so they take player blocks."""
    kind = "group" if scope == "group" else "player"
    return tuple(t for t, (_l, scopes, _s, _p) in BLOCK_CATALOG.items() if kind in scopes)


THEME_SPEC: dict[str, Spec] = {
    "preset": enum(THEME_PRESETS, "default"),
    "background": enum(BACKGROUNDS, "default"),
    "header_style": enum(HEADER_STYLES, "standard"),
    "banner_url": optional(image_url),
    "banner_height": enum(BANNER_HEIGHTS, "md"),
    "show_description": boolean(True),
}

_BLOCK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _validate_palette(v: Any) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise LayoutError("Colours: expected a set of colour values.")
    if len(v) > MAX_PALETTE_KEYS:
        raise LayoutError(f"Colours: at most {MAX_PALETTE_KEYS} values.")
    out = {}
    for key, val in v.items():
        if key not in ALLOWED_PALETTE_KEYS:
            continue
        if not isinstance(val, str) or not palette_value_ok(val):
            raise LayoutError(f"Colours: '{val}' isn't a colour we can use.")
        out[key] = val.strip()
    return out


def validate_theme(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    theme = {k: spec(raw.get(k), f"Page style ({k})") for k, spec in THEME_SPEC.items()}
    theme["palette"] = _validate_palette(raw.get("palette"))
    return theme


def validate_block(raw: Any, index: int, allowed: tuple[str, ...]) -> dict:
    if not isinstance(raw, dict):
        raise LayoutError(f"Block {index + 1} is malformed.")
    btype = raw.get("type")
    if btype not in allowed:
        raise LayoutError(f"Block {index + 1}: '{btype}' can't be used on this page.")
    label, _scopes, default_surface, props = BLOCK_CATALOG[btype]
    where = f"Block {index + 1} ({label})"
    bid = raw.get("id")
    if not isinstance(bid, str) or not _BLOCK_ID_RE.match(bid):
        raise LayoutError(f"{where}: missing a valid id.")
    out: dict[str, Any] = {
        "id": bid,
        "type": btype,
        "width": enum(WIDTHS, "full")(raw.get("width"), where),
        "surface": enum(SURFACES, default_surface)(raw.get("surface"), where),
        "title": text(80)(raw.get("title"), f"{where} title") or None,
        "hide_on": enum(HIDE_ON, "none")(raw.get("hide_on"), where),
    }
    for key, spec in props.items():
        out[key] = spec(raw.get(key), where)
    return out


def validate_layout(raw: Any, scope: str) -> dict:
    """Return the normalized layout, or raise LayoutError."""
    if not isinstance(raw, dict):
        raise LayoutError("The layout is malformed.")
    blocks = raw.get("blocks")
    if not isinstance(blocks, list):
        raise LayoutError("The layout has no block list.")
    if len(blocks) > MAX_BLOCKS:
        raise LayoutError(f"A page can hold at most {MAX_BLOCKS} blocks ({len(blocks)} now).")
    allowed = block_types_for(scope)
    seen: set[str] = set()
    out_blocks = []
    for i, b in enumerate(blocks):
        block = validate_block(b, i, allowed)
        if block["id"] in seen:
            raise LayoutError(f"Block {i + 1}: duplicate id '{block['id']}'.")
        seen.add(block["id"])
        out_blocks.append(block)
    return {"version": LAYOUT_VERSION, "theme": validate_theme(raw.get("theme")), "blocks": out_blocks}
