"""Group-editable message text and footers for embed notifications (web140a).

Two things a group controls around its embed designs:

* **Message text** — the line sent above the embed ("X received a drop:").
  Stored on the design itself (``group_embeds.content``). NULL means the
  built-in line: the template group's (group 1) text when staff set one, else
  ``DEFAULT_CONTENT``. "" sends no text. ``{player_mention}`` is the only
  place a player can be pinged — a mention inside an embed never pings — so it
  carries ``db.ops.get_formatted_name``, which honours the player's own ping
  settings. Own text needs ``custom_embeds``, like the rest of the design.

* **Footer** — ``group_notification_footers``. Everything carries the
  DropTracker footer unless the group holds ``custom_footer`` (every paid
  plan) and has chosen otherwise, for every type ("" row) or one type.

Both are read on every notification, so both are cached per process for
``_CACHE_TTL`` seconds; an edit reaches the bots within a minute. Group
messages only: DMs and DropTracker's own announcements keep our footer.
"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, Optional, Tuple

TEMPLATE_GROUP_ID = 1
CONTENT_MAX = 2000
FOOTER_MAX = 2048
FOOTER_ENTITLEMENT = "custom_footer"

# The built-in line per embed type, exactly as the senders always wrote it.
# lb is absent: the lootboard edits one standing message and sends no text.
DEFAULT_CONTENT: Dict[str, str] = {
    "drop": "{player_mention} received a drop:",
    "clog": "{player_mention} has added an item to their collection log!",
    "pb": "{player_mention} has achieved a new personal best:",
    "ca": "{player_mention} has completed a combat achievement!",
    "pet": "{player_mention} has acquired a new pet!",
    "pet_dupe": "{player_mention} has received a duplicate pet!",
    "level_up": "{player_mention} levelled-up:",
    "quest": "{player_mention} completed a quest!",
    "death": "{player_mention} has died!",
    "diary": "{player_mention} completed an achievement diary!",
    "slayer": "{player_mention} completed a slayer task!",
    "pvp_kill": "{player_mention} got a PvP kill!",
    "pvp_death": "{player_mention} was killed in PvP!",
}
CONTENT_TYPES = tuple(DEFAULT_CONTENT)

# Types a footer can be chosen for. The embed types, plus the milestone
# messages that have no design of their own (xp milestones use the level-up
# design; kc and rank milestones are built in code).
FOOTER_TYPES = (
    "drop", "clog", "pb", "ca", "pet", "pet_dupe", "level_up", "xp_milestone",
    "kc", "rank", "quest", "death", "diary", "slayer", "pvp_kill", "pvp_death", "lb",
)
FOOTER_MODES = ("default", "none", "custom")
# A type that has no footer row of its own follows this one before the
# group-wide choice (a duplicate pet is a pet message to most groups).
FOOTER_FALLBACK_TYPES = {"pet_dupe": "pet", "xp_milestone": "level_up"}

PLAYER_MENTION = "{player_mention}"

_MASS_MENTION = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
_CACHE_TTL = 60.0

_content_cache: Dict[Tuple[int, str], Tuple[float, Optional[str]]] = {}
_footer_cache: Dict[int, Tuple[float, Dict[str, Tuple[str, Optional[str]]]]] = {}


def global_footer() -> Optional[str]:
    return os.getenv("DISCORD_MESSAGE_FOOTER") or None


# --- validation (web_api) ---------------------------------------------------

def validate_content(value) -> Optional[str]:
    """Normalise a submitted message text. None = built-in, "" = no text.

    Raises ValueError with a user-facing reason.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Message text must be text.")
    text = value.strip()
    if len(text) > CONTENT_MAX:
        raise ValueError(f"Message text can be at most {CONTENT_MAX} characters.")
    if _MASS_MENTION.search(text):
        raise ValueError("Message text can't ping @everyone or @here. Mention a role instead.")
    return text


def validate_footer(mode, text) -> Tuple[str, Optional[str]]:
    """Normalise a submitted footer choice. Raises ValueError."""
    if mode not in FOOTER_MODES:
        raise ValueError(f"Footer must be one of: {', '.join(FOOTER_MODES)}.")
    if mode != "custom":
        return mode, None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Enter the footer text, or choose no footer.")
    text = text.strip()
    if len(text) > FOOTER_MAX:
        raise ValueError(f"Footer text can be at most {FOOTER_MAX} characters.")
    return mode, text


# --- rendering ---------------------------------------------------------------

def neutralize_mass_mentions(text: str) -> str:
    """Belt to the editor's braces: an @everyone that got in anyway is inert."""
    return _MASS_MENTION.sub(lambda m: "@​" + m.group(1), text)


def render_text(template: Optional[str], values: dict) -> str:
    """Fill a message-text or footer template from a sender's value map.

    The same rules as an embed description: a line that references a value
    this notification lacks is dropped ("KC: None" is worse than no line), and
    a documented token nobody filled renders as nothing.
    """
    from utils.format import (
        missing_tokens,
        replace_placeholders_in_text,
        strip_known_tokens,
    )
    from utils.game_emojis import with_derived_tokens

    if not template:
        return ""
    values = with_derived_tokens(values or {})
    missing = missing_tokens(values)
    text = template
    if missing:
        text = "\n".join(
            line for line in text.split("\n") if not any(t in line for t in missing)
        )
    text = replace_placeholders_in_text(text, {k: v for k, v in values.items() if k not in missing})
    text = strip_known_tokens(text)
    # Group points tokens are exempt from strip_known_tokens (their leftover
    # is a signal elsewhere); here a leftover is just unavailable data.
    text = re.sub(r"\{group_points_[a-z_]+\}", "", text)
    return text.strip()


def render_content(template: str, values: dict, player_mention: str) -> Optional[str]:
    """The message text to send, or None for none."""
    text = render_text(template, {**(values or {}), PLAYER_MENTION: player_mention})
    if not text:
        return None
    return neutralize_mass_mentions(text)[:CONTENT_MAX]


def default_content(embed_type: str, player_mention: str) -> str:
    """The built-in line, rendered. What the senders send with no override."""
    return DEFAULT_CONTENT[embed_type].replace(PLAYER_MENTION, player_mention)


# --- message text lookup -----------------------------------------------------

def _row_content(group_id: int, embed_type: str) -> Tuple[bool, Optional[str]]:
    """``(row_exists, content)`` for one group's design of one type."""
    from db.models import GroupEmbed, Session

    with Session() as s:
        row = (
            s.query(GroupEmbed.content)
            .filter(GroupEmbed.group_id == group_id, GroupEmbed.embed_type == embed_type)
            .order_by(GroupEmbed.embed_id)
            .first()
        )
    if row is None:
        return False, None
    content = row[0]
    return True, content if isinstance(content, str) else None


def _cached_row_content(group_id: int, embed_type: str) -> Optional[str]:
    key = (group_id, embed_type)
    now = time.monotonic()
    hit = _content_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    _, content = _row_content(group_id, embed_type)
    _content_cache[key] = (now + _CACHE_TTL, content)
    return content


def content_template(group_id: Optional[int], embed_type: str,
                     custom_embeds: bool, own_type: Optional[str] = None) -> Optional[str]:
    """The stored message text that applies, or None for the sender's own line.

    The group's text when it may use its own designs and set one, else the
    template group's when staff set one. ``own_type`` names the group design
    actually used when it differs from ``embed_type`` (a duplicate pet sent
    with the pet design takes the pet design's text). Lookup errors fall back
    to the built-in line: the text is never worth the notification.
    """
    try:
        if custom_embeds and group_id and group_id != TEMPLATE_GROUP_ID:
            own = _cached_row_content(group_id, own_type or embed_type)
            if own is not None:
                return own
        return _cached_row_content(TEMPLATE_GROUP_ID, embed_type)
    except Exception as exc:
        print(f"Couldn't load the {embed_type} message text for group {group_id}: {exc}")
        return None


# --- footer lookup -----------------------------------------------------------

def _load_footer_rows(group_id: int) -> Dict[str, Tuple[str, Optional[str]]]:
    from db.models import GroupNotificationFooter, Session

    with Session() as s:
        rows = (
            s.query(GroupNotificationFooter)
            .filter(GroupNotificationFooter.group_id == group_id)
            .all()
        )
        return {
            (r.notification_type or ""): (r.mode, r.text)
            for r in rows
            if r.mode in FOOTER_MODES
        }


def _footer_rows(group_id: int) -> Dict[str, Tuple[str, Optional[str]]]:
    now = time.monotonic()
    hit = _footer_cache.get(group_id)
    if hit and hit[0] > now:
        return hit[1]
    rows = _load_footer_rows(group_id)
    _footer_cache[group_id] = (now + _CACHE_TTL, rows)
    return rows


def pick_footer(rows: Dict[str, Tuple[str, Optional[str]]], notification_type: str,
                default: Optional[str]) -> Optional[str]:
    """Pure resolution over a group's rows: type, its fallback type, group-wide."""
    for key in (notification_type, FOOTER_FALLBACK_TYPES.get(notification_type), ""):
        if key is None or key not in rows:
            continue
        mode, text = rows[key]
        if mode == "none":
            return None
        if mode == "custom":
            return text or None
        return default
    return default


def footer_template(group_id: Optional[int], notification_type: str) -> Optional[str]:
    """The footer (still holding placeholders) for one group message, or None.

    The DropTracker footer unless the group holds ``custom_footer`` and chose
    otherwise. An entitlement or lookup error keeps the DropTracker footer.
    """
    default = global_footer()
    if not group_id or group_id == TEMPLATE_GROUP_ID:
        return default
    try:
        from db.entitlements import group_has_entitlement

        if not group_has_entitlement(group_id, FOOTER_ENTITLEMENT):
            return default
        return pick_footer(_footer_rows(group_id), notification_type, default)
    except Exception as exc:
        print(f"Couldn't resolve the {notification_type} footer for group {group_id}: {exc}")
        return default


def apply_footer(embed, group_id: Optional[int], notification_type: str,
                 values: Optional[dict] = None):
    """Give ``embed`` the group's footer for this type, placeholders filled.

    Call it last, on the finished embed: it replaces whatever footer the
    template or builder set. Returns the embed.
    """
    template = footer_template(group_id, notification_type)
    text = render_text(template, values or {}) if template and "{" in template else (template or "")
    text = (text or "")[:FOOTER_MAX]
    if text.strip():
        embed.set_footer(text)
    else:
        # Discord rejects an empty footer text; no footer is the empty state.
        embed.footer = None
    return embed


def invalidate(group_id: Optional[int] = None) -> None:
    """Drop this process's cached text and footers (web_api calls it on save)."""
    if group_id is None:
        _content_cache.clear()
        _footer_cache.clear()
        return
    _footer_cache.pop(group_id, None)
    for key in [k for k in _content_cache if k[0] == group_id]:
        _content_cache.pop(key, None)
