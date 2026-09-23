"""Group-editable Hall of Fame boss messages.

The Hall of Fame posts one Components V2 message per boss (see
``services/hall_of_fame.py``). This module is the layout those messages are
built from, so a group can arrange them the way it arranges its notifications
(``services/component_layout.py``): the same ``{accent_color, blocks}``
document, the same ``text`` / ``section`` / ``separator`` / ``media`` /
``buttons`` blocks, the same placeholder rules. A group admin who has built a
notification layout already knows this one.

Two things are new, and both come from what a Hall of Fame message *is* — a
set of rankings rather than one event:

**The ``leaderboard`` block.** Picks a ranking (``pb``, ``kc``, ``loot_month``
or ``loot_all``), how many places to show, and how each line looks::

    {"type": "leaderboard", "board": "kc", "count": 3,
     "title": "**Most kills**",
     "line": "-# {medal} {player} - `{value}` kc",
     "empty": "-# Nobody has logged a kill yet."}

Personal bests come in team-size brackets, so a ``pb`` board also takes a
``bracket`` heading drawn above each one. ``count`` may be null, meaning "the
group's *Number of PBs to display* setting", which is what the default uses so
that setting keeps working.

**``each_mode``.** Raids combine their modes into one message (CoX and CM, the
three ToB modes, ...). A block with ``"each_mode": true`` is drawn once per mode
with that mode's numbers and ``{mode_name}``; every other block is drawn once
with the boss as a whole — kills and loot summed across modes. For an ordinary
boss there is one "mode", the boss itself, so the flag changes nothing and
``{mode_name}`` is empty (which drops a ``### {mode_name}`` line, as intended).

Values are always given to the renderer already formatted, as a ``HofEntry``
(built by ``services/hof_data.py``), so this module has no database, Redis or
Discord dependency and the web preview can render with the same code.

Every failure path falls back rather than breaking the channel: a layout that
no longer validates, or that cannot be shrunk under Discord's limits for some
boss, is replaced by the default for that message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from services.component_layout import (
    _PLACEHOLDER_RE,
    IS_COMPONENTS_V2,
    MAX_BLOCKS,
    MAX_BUTTONS,
    MAX_LABEL_LENGTH,
    MAX_MEDIA_ITEMS,
    MAX_TEXT_LENGTH,
    MAX_TOTAL_TEXT,
    TYPE_CONTAINER,
    TYPE_SEPARATOR,
    TYPE_TEXT_DISPLAY,
    _render_block,
    _substitute_line,
    parse_accent,
    validate_layout as _validate_component_blocks,
)

#: ``group_component_layouts.notification_type`` for a group's Hall of Fame
#: layout. The table already holds one layout document per (group, kind) with
#: an ``active`` flag, which is exactly this; no separate table needed.
LAYOUT_TYPE = "hall_of_fame"

BLOCK_TYPES = ("text", "section", "separator", "media", "buttons", "leaderboard")

# ── Discord's limits for one Hall of Fame message ────────────────────────────
# Tighter than a notification's because the rendered size depends on the data:
# an each_mode block multiplies by the number of raid modes and a leaderboard
# grows with its rows. Checked after rendering, with the rows shrunk until the
# message fits (see render_entry).
MAX_RENDERED_COMPONENTS = 38
MAX_RENDERED_TEXT = 3950

MAX_ROWS = 10
_ROW_CAPS = (None, 5, 3, 2, 1)

BOARDS: Dict[str, Dict[str, str]] = {
    "pb": {
        "label": "Personal bests",
        "help": "Fastest times, one list per team size (Solo, Duo, ...).",
        "value": "the time, e.g. 1:23.40",
    },
    "kc": {
        "label": "Kill count",
        "help": "Highest kill counts among your members. Kill counts are recorded from the plugin as members play.",
        "value": "the kill count, e.g. 1,204",
    },
    "loot_month": {
        "label": "Loot this month",
        "help": "Most loot from this boss this month.",
        "value": "the GP value, e.g. 48.20M",
    },
    "loot_all": {
        "label": "Loot, all time",
        "help": "Most loot from this boss since tracking began.",
        "value": "the GP value, e.g. 1.204B",
    },
}

#: What a leaderboard line looks like when the author leaves it blank.
DEFAULT_LINES = {
    "pb": "-# {medal} `{value}` - {player}",
    "kc": "-# {medal} {player} - `{value}` kc",
    "loot_month": "-# {medal} {player} - {coins_emoji} `{value}` gp",
    "loot_all": "-# {medal} {player} - {coins_emoji} `{value}` gp",
}
DEFAULT_BRACKET = "-# **{team_size}**"

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

#: Tokens that decorate a line rather than carry its data. A line is dropped
#: when a value on it is missing, but never because an icon is (the legacy Hall
#: of Fame bot, for one, has no emoji at all).
DECORATIVE_TOKENS = frozenset({"{boss_emoji}", "{coins_emoji}"})
_EMOJI_REF_RE = re.compile(r"\{emoji:([a-z0-9_]{2,32})\}")
_TOKEN_RE = re.compile(r"\{([a-z0-9_]+)\}")


# ── What the renderer is given ───────────────────────────────────────────────

@dataclass
class Row:
    """One place on a leaderboard, already formatted."""
    player: str
    player_plain: str
    value: str


@dataclass
class Scope:
    """The numbers for one boss, one raid mode, or a raid's modes combined.

    ``tokens`` maps ``"{token}"`` to its resolved text ("" when absent, which
    is what makes a line about it disappear). ``boards`` holds the ranked rows
    for ``kc`` / ``loot_month`` / ``loot_all``; personal bests live in
    ``pb_brackets`` as ``(team size label, rows)``.
    """
    tokens: Dict[str, str] = field(default_factory=dict)
    boards: Dict[str, List[Row]] = field(default_factory=dict)
    pb_brackets: List[Tuple[str, List[Row]]] = field(default_factory=list)


@dataclass
class HofEntry:
    """One Hall of Fame message: the boss as a whole, and its modes.

    For an ordinary boss ``modes`` is ``[scope]`` — each_mode blocks draw once.
    """
    scope: Scope
    modes: List[Scope]


# ── Token documentation (served to the editor) ───────────────────────────────
# Grouped so the editor can show them in sections an admin can scan.
TOKEN_GROUPS: List[Dict[str, Any]] = [
    {
        "label": "Boss",
        "tokens": [
            ("boss_name", "The boss's name"),
            ("boss_link", "The boss's name, linked to its DropTracker page"),
            ("boss_emoji", "The boss's own icon, when it has one"),
            ("boss_image_url", "Picture of the boss, for a thumbnail"),
            ("boss_url", "Link to the boss's DropTracker page"),
            ("mode_name", "The raid mode, in blocks set to repeat for each mode (empty otherwise)"),
        ],
    },
    {
        "label": "Top of each ranking",
        "tokens": [
            ("top_kc_player", "Member with the highest kill count"),
            ("top_kc", "Their kill count"),
            ("top_looter_month", "Member with the most loot this month"),
            ("top_loot_month", "Their loot this month, in GP"),
            ("top_looter_all", "Member with the most loot of all time"),
            ("top_loot_all", "Their loot of all time, in GP"),
            ("fastest_player", "Member with the fastest kill"),
            ("fastest_time", "The fastest kill's time"),
            ("fastest_team_size", "The fastest kill's team size"),
        ],
    },
    {
        "label": "Totals",
        "tokens": [
            ("total_pbs", "Personal bests your members have recorded here"),
            ("total_loot", "All loot your members have received from this boss, in GP"),
            ("month_name", "The current month"),
        ],
    },
    {
        "label": "Icons and links",
        "tokens": [
            ("coins_emoji", "The coins icon"),
            ("directory_url", "Link back to the Hall of Fame directory (empty when there is none)"),
            ("site_url", "The DropTracker website"),
            ("pbs_url", "The website's personal bests page"),
        ],
    },
]

#: Tokens a leaderboard line (and a pb bracket heading) may use on top of the
#: boss tokens.
ROW_TOKENS: List[Tuple[str, str]] = [
    ("medal", "🥇 🥈 🥉 for the top three, then 4., 5., ..."),
    ("rank", "The place as a number"),
    ("player", "The member, linked to their profile"),
    ("player_plain", "The member's name with no link"),
    ("value", "What they are ranked by: the time, kill count or GP"),
    ("team_size", "Personal bests only: the team size of this list"),
]


# ── Defaults ─────────────────────────────────────────────────────────────────
# Replaces the hard-coded boss message the Hall of Fame used before layouts:
# the boss and its picture, then who leads it — highest kill count and most
# loot, one line each rather than a list — and the personal-best leaderboards
# as before. Every optional figure sits on its own line, so a boss nobody has
# looted yet simply loses that line.
DEFAULT_LAYOUT: Dict[str, Any] = {
    "accent_color": None,
    "blocks": [
        {
            "type": "section",
            "content": (
                "## {boss_emoji} {boss_link} 🏆\n"
                "-# • Highest KC: `{top_kc}` kc by {top_kc_player}\n"
                "-# • Most loot this month: {coins_emoji} `{top_loot_month}` gp by {top_looter_month}\n"
                "-# • Fastest kill: `{fastest_time}` ({fastest_team_size}) by {fastest_player}\n"
                "-# • Total loot tracked: {coins_emoji} `{total_loot}` gp\n"
                "-# • Personal bests tracked: `{total_pbs}`"
            ),
            "thumbnail": "{boss_image_url}",
        },
        {"type": "separator", "divider": True},
        {"type": "text", "content": "⏳ **__Personal Best Leaderboards__**"},
        {"type": "text", "each_mode": True, "content": "### {mode_name}"},
        {
            "type": "leaderboard",
            "each_mode": True,
            "board": "pb",
            "count": None,
            "bracket": "-# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n-# **{team_size}**",
            "line": DEFAULT_LINES["pb"],
            "empty": "-# No personal bests recorded yet.",
        },
        {"type": "separator", "divider": True},
        {
            "type": "text",
            "content": (
                "-# Powered by the [DropTracker]({site_url}) • "
                "[View all Personal Bests]({pbs_url})"
            ),
        },
        {"type": "separator", "divider": True},
        {"type": "text", "content": "-# 📋 [Back to Directory]({directory_url})"},
    ],
}


def default_layout() -> Dict[str, Any]:
    return _deep_copy(DEFAULT_LAYOUT)


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


# ── What a layout needs fetched ──────────────────────────────────────────────

_BOARD_FOR_TOKEN = {
    "top_kc": "kc",
    "top_kc_player": "kc",
    "top_loot_month": "loot_month",
    "top_looter_month": "loot_month",
    "top_loot_all": "loot_all",
    "top_looter_all": "loot_all",
}


def _block_texts(block: Dict[str, Any]) -> Iterable[str]:
    for key in ("content", "thumbnail", "title", "line", "bracket", "empty"):
        value = block.get(key)
        if isinstance(value, str):
            yield value
    for url in block.get("urls") or []:
        if isinstance(url, str):
            yield url
    for button in block.get("buttons") or []:
        if isinstance(button, dict):
            for key in ("label", "url"):
                if isinstance(button.get(key), str):
                    yield button[key]


def needed_boards(layout: Dict[str, Any], pb_default_count: int) -> Dict[str, int]:
    """``{board: rows}`` the layout reads, so only those are queried.

    A ``top_*`` token needs one row of its board; a leaderboard needs its
    ``count`` (null = the group's PB setting). ``pb`` is always included: the
    personal-best total and the fastest kill come from the same rows.
    """
    needs: Dict[str, int] = {"pb": max(1, min(MAX_ROWS, pb_default_count))}
    for block in layout.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "leaderboard" and block.get("board") in BOARDS:
            count = _block_count(block, pb_default_count)
            needs[block["board"]] = max(needs.get(block["board"], 0), count)
        for text in _block_texts(block):
            for token in _TOKEN_RE.findall(text):
                board = _BOARD_FOR_TOKEN.get(token)
                if board:
                    needs[board] = max(needs.get(board, 0), 1)
    return needs


def emoji_refs(layout: Dict[str, Any]) -> List[str]:
    """Every ``{emoji:key}`` the layout places, for the caller to resolve."""
    found: List[str] = []
    for block in layout.get("blocks") or []:
        if isinstance(block, dict):
            for text in _block_texts(block):
                for key in _EMOJI_REF_RE.findall(text):
                    if key not in found:
                        found.append(key)
    return found


def _block_count(block: Dict[str, Any], pb_default_count: int) -> int:
    count = block.get("count")
    if count is None:
        count = pb_default_count
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = pb_default_count
    return max(1, min(MAX_ROWS, count))


# ── Validation ───────────────────────────────────────────────────────────────

def validate_layout(layout: Any) -> Tuple[bool, List[str]]:
    """Check a Hall of Fame layout, returning ``(ok, errors for the editor)``."""
    if not isinstance(layout, dict):
        return False, ["The layout must be an object."]
    blocks = layout.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return False, ["Add at least one block to the layout."]

    errors: List[str] = []
    if len(blocks) > MAX_BLOCKS:
        errors.append(f"Too many blocks ({len(blocks)}); the limit is {MAX_BLOCKS}.")
    if layout.get("accent_color") not in (None, "") and parse_accent(layout.get("accent_color")) is None:
        errors.append("Accent colour must be a hex value like #c8aa6e.")

    total_text = 0
    has_visible = False
    for index, block in enumerate(blocks[:MAX_BLOCKS], start=1):
        where = f"Block {index}"
        if not isinstance(block, dict):
            errors.append(f"{where} is not a valid block.")
            continue
        block_type = block.get("type")
        if block_type not in BLOCK_TYPES:
            errors.append(f"{where} has an unknown type '{block_type}'.")
            continue
        if "each_mode" in block and not isinstance(block.get("each_mode"), bool):
            errors.append(f"{where}: 'repeat for each raid mode' must be on or off.")
        if block_type != "separator":
            has_visible = True

        if block_type == "leaderboard":
            errors.extend(_validate_leaderboard(block, where))
            total_text += sum(len(t) for t in _block_texts(block))
            continue

        # The notification validator already phrases these for an editor;
        # run it on this one block and point its message at the right index.
        stripped = {k: v for k, v in block.items() if k != "each_mode"}
        ok, block_errors = _validate_component_blocks({"blocks": [stripped]})
        if not ok:
            errors.extend(e.replace("Block 1", where) for e in block_errors)
        if block_type in ("text", "section") and isinstance(block.get("content"), str):
            total_text += len(block["content"])

    if not has_visible:
        errors.append("The layout needs at least one block that is not a divider.")
    if total_text > MAX_TOTAL_TEXT:
        errors.append(
            f"The layout has {total_text} characters of text; the limit is about {MAX_TOTAL_TEXT}."
        )
    return (not errors), errors


def _validate_leaderboard(block: Dict[str, Any], where: str) -> List[str]:
    errors: List[str] = []
    if block.get("board") not in BOARDS:
        errors.append(f"{where}: choose what the leaderboard ranks.")
    count = block.get("count")
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ROWS
    ):
        errors.append(f"{where}: show between 1 and {MAX_ROWS} places.")
    for key, label in (("title", "title"), ("line", "line"), ("bracket", "team-size heading"),
                       ("empty", "empty text")):
        value = block.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{where}: the {label} must be text.")
        elif len(value) > 500:
            errors.append(f"{where}: the {label} is over 500 characters.")
    line = block.get("line")
    if isinstance(line, str) and line.strip() and not re.search(r"\{(player|player_plain|value)\}", line):
        errors.append(f"{where}: each line needs {{player}} or {{value}}, or every place looks the same.")
    return errors


# ── Rendering ────────────────────────────────────────────────────────────────

def _medal(rank: int) -> str:
    return _MEDALS.get(rank, f"{rank}.")


def _resolve_emoji_refs(text: str, emojis: Dict[str, str]) -> str:
    """``{emoji:npc_zulrah}`` → the glyph, or nothing when it is not available.

    Always replaced, never left in place: the notification token pattern does
    not match the colon form, so an unresolved one would otherwise be sent as
    literal text.
    """
    if not text or "{emoji:" not in text:
        return text
    return _EMOJI_REF_RE.sub(lambda m: emojis.get(m.group(1), ""), text)


def _with_emojis(block: Dict[str, Any], emojis: Dict[str, str]) -> Dict[str, Any]:
    out = dict(block)
    for key in ("content", "title", "line", "bracket", "empty"):
        if isinstance(out.get(key), str):
            out[key] = _resolve_emoji_refs(out[key], emojis)
    if isinstance(out.get("buttons"), list):
        out["buttons"] = [
            {**b, "label": _resolve_emoji_refs(b.get("label") or "", emojis)}
            if isinstance(b, dict) else b
            for b in out["buttons"]
        ]
    return out


def _substitute(text: str, tokens: Dict[str, str]) -> str:
    """Resolve tokens line by line, dropping any line that is missing a value.

    Stricter than the notification rule (which drops a line only when *every*
    value on it is blank) because a Hall of Fame line usually pairs two values —
    "`{top_kc}` kc by {top_kc_player}" — and an icon beside them would keep a
    line whose data is gone. Icons never count as data (DECORATIVE_TOKENS), and
    a token nobody defines drops its line as it does everywhere else.
    """
    if not text:
        return ""
    kept: List[str] = []
    for line in text.split("\n"):
        missing = False
        for token in _PLACEHOLDER_RE.findall(line):
            if token not in tokens:
                missing = True
                break
            if token not in DECORATIVE_TOKENS and not str(tokens[token]).strip():
                missing = True
                break
        if missing:
            continue
        resolved = _substitute_line(line, tokens)
        if line.strip() and not resolved.strip():
            continue
        kept.append(resolved)
    return "\n".join(kept).strip()


def _row_lines(line_template: str, tokens: Dict[str, str], rows: List[Row],
               extra: Optional[Dict[str, str]] = None) -> List[str]:
    lines: List[str] = []
    for index, row in enumerate(rows, start=1):
        values = dict(tokens)
        if extra:
            values.update(extra)
        values.update({
            "{rank}": str(index),
            "{medal}": _medal(index),
            "{player}": row.player,
            "{player_plain}": row.player_plain,
            "{value}": row.value,
        })
        rendered = _substitute(line_template, values)
        if rendered:
            lines.append(rendered)
    return lines


def _render_leaderboard(block: Dict[str, Any], scope: Scope, tokens: Dict[str, str],
                        pb_default_count: int, row_cap: Optional[int]) -> Optional[Dict[str, Any]]:
    board = block.get("board")
    if board not in BOARDS:
        return None
    count = _block_count(block, pb_default_count)
    if row_cap is not None:
        count = min(count, row_cap)
    line = block.get("line") if isinstance(block.get("line"), str) and block["line"].strip() else DEFAULT_LINES[board]

    body: List[str] = []
    if board == "pb":
        bracket = block.get("bracket")
        if not isinstance(bracket, str):
            bracket = DEFAULT_BRACKET
        for label, rows in scope.pb_brackets:
            if not rows:
                continue
            extra = {"{team_size}": label}
            heading = _substitute(bracket, {**tokens, **extra}) if bracket.strip() else ""
            lines = _row_lines(line, tokens, rows[:count], extra)
            if lines:
                if heading:
                    body.append(heading)
                body.extend(lines)
    else:
        body = _row_lines(line, tokens, (scope.boards.get(board) or [])[:count])

    title = _substitute(block.get("title") or "", tokens)
    if not body:
        body = [_substitute(block.get("empty") or "", tokens)]
        if not body[0]:
            return None
    content = "\n".join(part for part in [title, *body] if part).strip()
    if not content:
        return None
    return {"type": TYPE_TEXT_DISPLAY, "content": content[:MAX_TEXT_LENGTH]}


def _render_one(block: Dict[str, Any], scope: Scope, tokens: Dict[str, str],
                pb_default_count: int, row_cap: Optional[int]) -> Optional[Dict[str, Any]]:
    block_type = block.get("type")
    if block_type == "leaderboard":
        return _render_leaderboard(block, scope, tokens, pb_default_count, row_cap)
    if block_type in ("text", "section"):
        # Text goes through the Hall of Fame line rule first; what is left has
        # no tokens, so the shared block renderer keeps it as-is and only
        # resolves the thumbnail.
        content = _substitute(block.get("content") or "", tokens)
        return _render_block({**block, "content": content}, tokens) if content else None
    return _render_block(block, tokens)


def render_layout(layout: Dict[str, Any], entry: HofEntry, common: Dict[str, str],
                  pb_default_count: int, emojis: Optional[Dict[str, str]] = None,
                  row_cap: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Resolve a layout for one boss into a Components V2 payload (plain dicts).

    ``common`` holds the tokens that do not depend on the boss (site links,
    directory link, coins icon, month). ``emojis`` maps ``{emoji:key}`` keys to
    glyphs. ``row_cap`` shortens every leaderboard; ``render_entry`` uses it to
    shrink a message that came out too large.
    """
    blocks = layout.get("blocks") if isinstance(layout, dict) else None
    if not isinstance(blocks, list):
        return None
    emojis = emojis or {}

    # Consecutive each_mode blocks repeat *together*, mode by mode: a mode
    # heading followed by that mode's leaderboard, then the next mode.
    runs: List[Tuple[bool, List[Dict[str, Any]]]] = []
    for raw in blocks[:MAX_BLOCKS]:
        if not isinstance(raw, dict):
            continue
        repeat = bool(raw.get("each_mode"))
        if runs and runs[-1][0] == repeat and repeat:
            runs[-1][1].append(raw)
        else:
            runs.append((repeat, [raw]))

    children: List[Dict[str, Any]] = []
    for repeat, run in runs:
        for scope in (entry.modes if repeat else [entry.scope]):
            tokens = {**common, **scope.tokens}
            for raw in run:
                rendered = _render_one(_with_emojis(raw, emojis), scope, tokens,
                                       pb_default_count, row_cap)
                if rendered is not None:
                    children.append(rendered)

    if not any(c.get("type") != TYPE_SEPARATOR for c in children):
        return None
    # Collapse runs of separators (an each_mode block that rendered nothing
    # leaves its neighbours' dividers touching), then trim the ends.
    tidy: List[Dict[str, Any]] = []
    for child in children:
        if child.get("type") == TYPE_SEPARATOR and tidy and tidy[-1].get("type") == TYPE_SEPARATOR:
            continue
        tidy.append(child)
    while tidy and tidy[0].get("type") == TYPE_SEPARATOR:
        tidy.pop(0)
    while tidy and tidy[-1].get("type") == TYPE_SEPARATOR:
        tidy.pop()

    container: Dict[str, Any] = {"type": TYPE_CONTAINER, "components": tidy}
    accent = parse_accent(layout.get("accent_color"))
    if accent is not None:
        container["accent_color"] = accent
    return {"flags": IS_COMPONENTS_V2, "components": [container]}


def count_components(obj) -> int:
    """Components in a rendered payload, the way Discord counts them."""
    if isinstance(obj, list):
        return sum(count_components(item) for item in obj)
    if isinstance(obj, dict):
        count = 1 if "type" in obj else 0
        for key in ("components", "accessory"):
            child = obj.get(key)
            if child is not None:
                count += count_components(child)
        return count
    return 0


def total_text(obj) -> int:
    """Characters of displayed text in a rendered payload."""
    if isinstance(obj, list):
        return sum(total_text(item) for item in obj)
    if isinstance(obj, dict):
        total = len(obj["content"]) if isinstance(obj.get("content"), str) else 0
        for value in obj.values():
            if isinstance(value, (list, dict)):
                total += total_text(value)
        return total
    return 0


def within_limits(payload: Dict[str, Any]) -> bool:
    components = payload.get("components") or []
    return (
        count_components(components) <= MAX_RENDERED_COMPONENTS
        and total_text(components) <= MAX_RENDERED_TEXT
    )


def render_entry(layout: Optional[Dict[str, Any]], entry: HofEntry, common: Dict[str, str],
                 pb_default_count: int, emojis: Optional[Dict[str, str]] = None,
                 log: Optional[Callable[[str], None]] = None) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Render with the group's layout, shrinking and falling back as needed.

    Returns ``(payload, used_default)``. Tries the layout at full size, then
    with every leaderboard shortened; if it still does not fit (or renders to
    nothing) the default layout is tried the same way. ``(None, True)`` means
    even the default could not fit, which the caller logs and skips.
    """
    attempts: List[Tuple[Dict[str, Any], bool]] = []
    if layout is not None:
        attempts.append((layout, False))
    attempts.append((DEFAULT_LAYOUT, True))
    for candidate, is_default in attempts:
        for cap in _ROW_CAPS:
            payload = render_layout(candidate, entry, common, pb_default_count, emojis, row_cap=cap)
            if payload is None:
                break
            if within_limits(payload):
                return payload, is_default
        if log and not is_default:
            log("custom Hall of Fame layout could not be fitted; using the default")
    return None, True


def meta() -> Dict[str, Any]:
    """What the editor needs to know about this DSL (served by the web API)."""
    return {
        "block_types": list(BLOCK_TYPES),
        "boards": [{"key": k, **v, "default_line": DEFAULT_LINES[k]} for k, v in BOARDS.items()],
        "token_groups": [
            {"label": g["label"], "tokens": [{"token": t, "help": h} for t, h in g["tokens"]]}
            for g in TOKEN_GROUPS
        ],
        "row_tokens": [{"token": t, "help": h} for t, h in ROW_TOKENS],
        "default_bracket": DEFAULT_BRACKET,
        "limits": {
            "max_blocks": MAX_BLOCKS,
            "max_rows": MAX_ROWS,
            "max_text_len": MAX_TEXT_LENGTH,
            "max_total_text": MAX_TOTAL_TEXT,
            "max_media_items": MAX_MEDIA_ITEMS,
            "max_buttons": MAX_BUTTONS,
            "max_label_len": MAX_LABEL_LENGTH,
        },
    }


def parse_stored(raw: Any) -> Optional[Dict[str, Any]]:
    """A stored layout document, if it still parses and validates."""
    import json

    try:
        layout = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(layout, dict):
        return None
    ok, _ = validate_layout(layout)
    return layout if ok else None


def load_active_layout(session, group_id: int) -> Optional[Dict[str, Any]]:
    """The group's live Hall of Fame layout, or None to use the default.

    Any failure — no row, saved as a draft, stops validating — answers None:
    a broken layout costs the customisation, never the Hall of Fame.
    """
    try:
        from db.models import GroupComponentLayout

        row = (
            session.query(GroupComponentLayout)
            .filter(
                GroupComponentLayout.group_id == group_id,
                GroupComponentLayout.notification_type == LAYOUT_TYPE,
            )
            .first()
        )
    except Exception:
        return None
    if row is None or not row.active:
        return None
    return parse_stored(row.layout)
