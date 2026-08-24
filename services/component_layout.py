"""Group-editable Components V2 layouts for notifications.

Groups can already customise the *embed* a notification uses. This is the same
idea for Discord's Components V2, which can do things an embed cannot — put an
image beside text rather than under it, stack several images, add link buttons.

Both systems coexist deliberately. A notification type either has a components
layout marked active for that group, or it falls through to the embed path
exactly as before, so nothing changes for a group that never opts in. Discord
also forbids mixing them: a message carrying the V2 flag may not carry `content`
or `embeds`, so the choice is genuinely either/or per message.

**The layout document.** A flat list of blocks inside one container, rather than
an arbitrary tree. Discord's nesting rules are fiddly (sections take one
accessory, containers cannot hold containers) and a tree would put those rules
in front of a user editing a notification. A flat list covers what these
messages actually look like, and every block maps to exactly one Discord
component::

    {
      "accent_color": "#c8aa6e",
      "blocks": [
        {"type": "text",      "content": "**{player_name}** did a thing"},
        {"type": "separator", "divider": true},
        {"type": "section",   "content": "### {npc_name}", "thumbnail": "{gear_image_url}"},
        {"type": "media",     "urls": ["{image_url}"]},
        {"type": "buttons",   "buttons": [{"label": "Profile", "url": "{player_url}"}]}
      ]
    }

Placeholders are the same ones the embed templates use, resolved with the same
function, so a group moving a template across does not have to relearn anything.

**Empty resolution removes the block.** If ``{gear_image_url}`` resolves to
nothing — most players have no character model — the thumbnail is dropped and
the section renders as plain text, rather than emitting a component with an
empty URL that Discord would reject and thereby losing the whole notification.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Discord message flag selecting the V2 renderer.
IS_COMPONENTS_V2 = 1 << 15

# Component type ids.
TYPE_ACTION_ROW = 1
TYPE_BUTTON = 2
TYPE_SECTION = 9
TYPE_TEXT_DISPLAY = 10
TYPE_THUMBNAIL = 11
TYPE_MEDIA_GALLERY = 12
TYPE_SEPARATOR = 14
TYPE_CONTAINER = 17

# ── Discord's limits ─────────────────────────────────────────────────────────
# Exceeding any of these makes Discord reject the whole message, which for a
# notification means the player silently never hears about their achievement.
# They are enforced on save (so the editor can explain) and again on render (so
# a template written before a limit changed cannot break sending).
MAX_BLOCKS = 30
MAX_TEXT_LENGTH = 3500
MAX_TOTAL_TEXT = 3900
MAX_MEDIA_ITEMS = 10
MAX_BUTTONS = 5
MAX_LABEL_LENGTH = 80

BLOCK_TYPES = {"text", "separator", "section", "media", "buttons"}

# Buttons in a notification are links out; interactive styles would need a
# handler on the other end, which a user-authored template cannot provide.
BUTTON_STYLE_LINK = 5

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_PLACEHOLDER_RE = re.compile(r"\{[a-z0-9_]+\}", re.IGNORECASE)


def _substitute_line(text: str, replacements: Dict[str, Any]) -> str:
    """Resolve placeholders the same way the embed templates do."""
    if not text:
        return ""
    try:
        from utils.format import replace_placeholders_in_text

        return replace_placeholders_in_text(text, replacements)
    except Exception:
        # Keeps this module usable (and testable) without the wider app; the
        # semantics are the same simple substitution.
        for key, value in replacements.items():
            try:
                text = text.replace(key, str(value))
            except Exception:
                continue
        return text


def _substitute(text: str, replacements: Dict[str, Any]) -> str:
    """Substitute per line, dropping any line left holding an unresolved token.

    Matches how the event layouts and `replace_placeholders` behave: a template
    line about a value this notification does not have (no group points, no
    previous best) disappears instead of rendering "Previous best: {best_time}".
    Losing a line is right; losing the block would throw away the lines that did
    resolve.
    """
    if not text:
        return ""
    kept = []
    for line in text.split("\n"):
        resolved = _substitute_line(line, replacements)
        if line.strip() and not resolved.strip():
            continue
        if _PLACEHOLDER_RE.search(resolved):
            continue
        kept.append(resolved)
    return "\n".join(kept).strip()


def _is_resolved_url(url: str) -> bool:
    """True when a URL is usable — non-empty and not a leftover placeholder.

    A placeholder that had no value stays in the string verbatim, and sending
    Discord ``{gear_image_url}`` as a URL fails the whole message.
    """
    if not url or not url.strip():
        return False
    if _PLACEHOLDER_RE.search(url):
        return False
    return url.startswith("http://") or url.startswith("https://") or url.startswith("attachment://")


def parse_accent(value: Any) -> Optional[int]:
    """``"#c8aa6e"`` or an int, to Discord's integer colour. None if unusable."""
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return value
    if isinstance(value, str):
        match = _HEX_RE.match(value.strip())
        if match:
            return int(match.group(1), 16)
    return None


# ── Validation ───────────────────────────────────────────────────────────────

def validate_layout(layout: Any) -> Tuple[bool, List[str]]:
    """Check a layout document, returning ``(ok, human-readable errors)``.

    Errors are phrased for the person editing the template, not for a log.
    """
    errors: List[str] = []

    if not isinstance(layout, dict):
        return False, ["The layout must be an object."]

    blocks = layout.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return False, ["Add at least one block to the layout."]
    if len(blocks) > MAX_BLOCKS:
        errors.append(f"Too many blocks ({len(blocks)}); Discord allows up to {MAX_BLOCKS}.")

    if layout.get("accent_color") not in (None, "") and parse_accent(layout.get("accent_color")) is None:
        errors.append("Accent colour must be a hex value like #c8aa6e.")

    total_text = 0
    for index, block in enumerate(blocks[:MAX_BLOCKS], start=1):
        where = f"Block {index}"
        if not isinstance(block, dict):
            errors.append(f"{where} is not a valid block.")
            continue

        block_type = block.get("type")
        if block_type not in BLOCK_TYPES:
            errors.append(f"{where} has an unknown type '{block_type}'.")
            continue

        if block_type in ("text", "section"):
            content = block.get("content")
            if not isinstance(content, str) or not content.strip():
                errors.append(f"{where} ({block_type}) needs some text.")
                continue
            if len(content) > MAX_TEXT_LENGTH:
                errors.append(
                    f"{where} text is {len(content)} characters; the limit is {MAX_TEXT_LENGTH}."
                )
            total_text += len(content)

        if block_type == "section":
            thumbnail = block.get("thumbnail")
            if thumbnail is not None and (not isinstance(thumbnail, str) or not thumbnail.strip()):
                errors.append(f"{where}: the thumbnail needs an image URL or placeholder.")

        if block_type == "media":
            urls = block.get("urls")
            if not isinstance(urls, list) or not urls:
                errors.append(f"{where} (media) needs at least one image URL.")
            elif len(urls) > MAX_MEDIA_ITEMS:
                errors.append(
                    f"{where} has {len(urls)} images; Discord allows up to {MAX_MEDIA_ITEMS}."
                )

        if block_type == "buttons":
            buttons = block.get("buttons")
            if not isinstance(buttons, list) or not buttons:
                errors.append(f"{where} (buttons) needs at least one button.")
                continue
            if len(buttons) > MAX_BUTTONS:
                errors.append(
                    f"{where} has {len(buttons)} buttons; Discord allows up to {MAX_BUTTONS} per row."
                )
            for button in buttons[:MAX_BUTTONS]:
                if not isinstance(button, dict):
                    errors.append(f"{where}: a button is not valid.")
                    continue
                label = button.get("label")
                if not isinstance(label, str) or not label.strip():
                    errors.append(f"{where}: every button needs a label.")
                elif len(label) > MAX_LABEL_LENGTH:
                    errors.append(f"{where}: a button label is over {MAX_LABEL_LENGTH} characters.")
                if not isinstance(button.get("url"), str) or not button["url"].strip():
                    errors.append(f"{where}: every button needs a link.")

    if total_text > MAX_TOTAL_TEXT:
        errors.append(
            f"The layout has {total_text} characters of text; Discord allows about {MAX_TOTAL_TEXT}."
        )

    return (not errors), errors


# ── Rendering ────────────────────────────────────────────────────────────────

def render_layout(layout: Dict[str, Any], replacements: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve a layout into a Discord message payload.

    Returns None when nothing renderable survives — an all-image layout for a
    player with no images, say — so the caller can fall back to the embed rather
    than send an empty container.
    """
    if not isinstance(layout, dict):
        return None
    blocks = layout.get("blocks")
    if not isinstance(blocks, list):
        return None

    children: List[Dict[str, Any]] = []

    for block in blocks[:MAX_BLOCKS]:
        if not isinstance(block, dict):
            continue
        rendered = _render_block(block, replacements)
        if rendered is not None:
            children.append(rendered)

    # Separators alone are not a message.
    if not any(c.get("type") != TYPE_SEPARATOR for c in children):
        return None

    # A separator that ended up first or last is a rule against nothing.
    while children and children[0].get("type") == TYPE_SEPARATOR:
        children.pop(0)
    while children and children[-1].get("type") == TYPE_SEPARATOR:
        children.pop()

    if not children:
        return None

    container: Dict[str, Any] = {"type": TYPE_CONTAINER, "components": children}
    accent = parse_accent(layout.get("accent_color"))
    if accent is not None:
        container["accent_color"] = accent

    return {"flags": IS_COMPONENTS_V2, "components": [container]}


def _render_block(block: Dict[str, Any], replacements: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    block_type = block.get("type")

    if block_type == "separator":
        return {
            "type": TYPE_SEPARATOR,
            "divider": bool(block.get("divider", True)),
            "spacing": 2 if block.get("spacing") == "large" else 1,
        }

    if block_type == "text":
        content = _substitute(block.get("content") or "", replacements).strip()
        if not content:
            return None
        return {"type": TYPE_TEXT_DISPLAY, "content": content[:MAX_TEXT_LENGTH]}

    if block_type == "section":
        content = _substitute(block.get("content") or "", replacements).strip()
        if not content:
            return None
        section: Dict[str, Any] = {
            "type": TYPE_SECTION,
            "components": [{"type": TYPE_TEXT_DISPLAY, "content": content[:MAX_TEXT_LENGTH]}],
        }
        thumbnail = block.get("thumbnail")
        if isinstance(thumbnail, str) and thumbnail.strip():
            url = _substitute_line(thumbnail, replacements).strip()
            # Unresolved means the player has no such image; the section is
            # still worth sending without it.
            if _is_resolved_url(url):
                section["accessory"] = {"type": TYPE_THUMBNAIL, "media": {"url": url}}
        return section

    if block_type == "media":
        urls = block.get("urls")
        if not isinstance(urls, list):
            return None
        items = []
        for raw in urls[:MAX_MEDIA_ITEMS]:
            if not isinstance(raw, str):
                continue
            url = _substitute_line(raw, replacements).strip()
            if _is_resolved_url(url):
                items.append({"media": {"url": url}})
        if not items:
            return None
        return {"type": TYPE_MEDIA_GALLERY, "items": items}

    if block_type == "buttons":
        buttons = block.get("buttons")
        if not isinstance(buttons, list):
            return None
        rendered = []
        for button in buttons[:MAX_BUTTONS]:
            if not isinstance(button, dict):
                continue
            label = _substitute_line(button.get("label") or "", replacements).strip()
            url = _substitute_line(button.get("url") or "", replacements).strip()
            if not label or not _is_resolved_url(url):
                continue
            rendered.append({
                "type": TYPE_BUTTON,
                "style": BUTTON_STYLE_LINK,
                "label": label[:MAX_LABEL_LENGTH],
                "url": url,
            })
        if not rendered:
            return None
        return {"type": TYPE_ACTION_ROW, "components": rendered}

    return None


# ── Notification types ───────────────────────────────────────────────────────
# The types a group may author a layout for. Deliberately the embed template
# types minus "lb": the lootboard message is an image the bot edits in place,
# not a notification, and has no send path to branch.
NOTIFICATION_TYPES = (
    "drop",
    "clog",
    "pb",
    "ca",
    "pet",
    "level_up",
    "quest",
    "death",
    "diary",
)


# ── Editor metadata ──────────────────────────────────────────────────────────
# What the builder needs to know about the DSL, served by
# web_api/routes/notification_layouts.py so the editor and the renderer cannot
# drift. Same shape as event_message_layouts.TOKEN_DOCS/TYPE_META, because a
# group admin should be looking at one system rather than two dialects.
#
# ``sample`` drives the editor's live preview. ``optional`` marks a token that
# is frequently absent in production — no screenshot, no character model, no
# points awarded — which is what makes a line vanish; the editor blanks those
# in its "missing values" preview so an author can see the sparse message
# before their members do.
_COMMON_TOKENS = ("player_name", "player_name_plain", "plugin_version")

_MEDIA_TOKENS = ("image_url", "video_url", "video_link")

_POINTS_TOKENS = (
    "group_points_awarded",
    "group_points_receiver_total",
    "group_points_member_count",
    "group_points_members_awarded",
)

TOKEN_DOCS: Dict[str, Dict[str, Any]] = {
    # Shared
    "player_name": {
        "help": "The player, as a link to their profile",
        "sample": "[RuneLite Ron](https://www.droptracker.io/players/1)",
    },
    "player_name_plain": {"help": "The player's name with no link", "sample": "RuneLite Ron"},
    "plugin_version": {
        "help": "Plugin version that sent this (empty for manual submissions)",
        "sample": "5.5.0",
        "optional": True,
    },
    "image_url": {
        "help": "Screenshot (or video, when one was recorded). Empty when the player sent neither",
        "sample": "https://www.droptracker.io/img/proofs/sample.png",
        "optional": True,
    },
    "video_url": {
        "help": "Recorded video, when there is one",
        "sample": "https://www.droptracker.io/vid/sample.mp4",
        "optional": True,
    },
    "video_link": {
        "help": "Markdown link to the video",
        "sample": "[Video](https://www.droptracker.io/vid/sample.mp4)",
        "optional": True,
    },
    "gear_image_url": {
        "help": "The player's rendered character, when they have uploaded one",
        "sample": "https://www.droptracker.io/img/gear/sample.png",
        "optional": True,
    },
    # Group points (only present when points were actually awarded)
    "group_points_awarded": {
        "help": "Points this event awarded",
        "sample": "12",
        "optional": True,
    },
    "group_points_receiver_total": {
        "help": "The receiver's running point total",
        "sample": "340",
        "optional": True,
    },
    "group_points_member_count": {
        "help": "How many members shared the award",
        "sample": "4",
        "optional": True,
    },
    "group_points_members_awarded": {
        "help": "The members who shared the award",
        "sample": "RuneLite Ron, Zezima",
        "optional": True,
    },
    # Drop
    "item_name": {"help": "Item name (linked to the wiki)", "sample": "Twisted bow"},
    "item_id": {
        "help": "OSRS item id — an icon is https://www.droptracker.io/img/itemdb/{item_id}.png",
        "sample": "20997",
    },
    "item_value": {"help": "Value of the drop, stacks spelled out", "sample": "`1.2B`"},
    "quantity": {"help": "How many were received", "sample": "`1`"},
    "total_value": {"help": "Stack value (value x quantity)", "sample": "`1204000000`"},
    "npc_name": {"help": "The source — NPC, raid or chest", "sample": "Chambers of Xeric"},
    "npc_id": {
        "help": "NPC id — an icon is https://www.droptracker.io/img/npcdb/{npc_id}.png",
        "sample": "7530",
    },
    "kill_count": {"help": "Kill count at the drop", "sample": "1,204"},
    "month_name": {"help": "The current month", "sample": "August"},
    "player_total_month": {"help": "The player's loot this month", "sample": "`48.2M`"},
    "group_total_month": {"help": "The group's loot this month", "sample": "`1.2B`"},
    "group_total": {"help": "The group's all-time tracked loot", "sample": "`14.7B`"},
    "global_rank": {"help": "The player's rank across every group", "sample": "`142`/`8,204`"},
    "group_rank": {"help": "The player's rank inside the group", "sample": "`3`/`86`"},
    "group_to_group_rank": {"help": "The group's rank against other groups", "sample": "`17`/`420`"},
    "user_count": {"help": "Tracked members in the group", "sample": "`86`"},
    # Collection log
    "collection_name": {"help": "The collection the item belongs to", "sample": "Chambers of Xeric"},
    "kc_received": {"help": "Kill count when the slot was filled", "sample": "412"},
    "player_loot_month": {"help": "The player's loot this month", "sample": "48.2M"},
    "total_tracked": {"help": "Tracked members in the group", "sample": "86"},
    # Personal best
    "personal_best": {"help": "The new best time", "sample": "14:23.40"},
    "team_size": {"help": "Team size for the kill", "sample": "4"},
    "total_ranked_global": {"help": "Players ranked globally at this boss", "sample": "4120"},
    "total_ranked_group": {"help": "Group members ranked at this boss", "sample": "22"},
    # Combat achievements
    "task_name": {"help": "The combat achievement completed", "sample": "Perfect Zulrah"},
    "task_tier": {"help": "The task's tier", "sample": "Elite"},
    "points_awarded": {"help": "Combat achievement points from this task", "sample": "4"},
    "total_points": {"help": "The player's total combat achievement points", "sample": "2085"},
    "current_tier": {"help": "The player's current tier", "sample": "Master"},
    "next_tier": {"help": "The next tier up", "sample": "Grandmaster"},
    "next_tier_points": {"help": "Points needed for the next tier", "sample": "2672"},
    "points_left": {"help": "Points still to go", "sample": "587"},
    "progress": {"help": "Percent toward the next tier, unsuffixed", "sample": "19.81"},
    # Pets
    "pet_name": {"help": "The pet's name", "sample": "Olmlet"},
    "source": {"help": "Where it came from", "sample": "Chambers of Xeric"},
    "killcount": {"help": "Kill count at the pet", "sample": "1432"},
    "milestone": {"help": "Milestone text (same as the kill count)", "sample": "1432"},
    "duplicate": {"help": "Whether this pet was already owned", "sample": "No"},
    "previously_owned": {"help": "Whether the player has owned it before", "sample": "No"},
    # Level ups
    "skill_name": {"help": "The skill that levelled", "sample": "Slayer"},
    "skills_names": {"help": "Every skill that levelled at once", "sample": "Slayer, Attack"},
    "skills_text": {"help": "Formatted level-up summary", "sample": "Slayer 99 (+1)"},
    "new_level": {"help": "The new level", "sample": "99"},
    "levels_gained": {"help": "Levels gained", "sample": "1"},
    "xp_total": {"help": "XP in that skill", "sample": "13,034,431"},
    "total_level": {"help": "The player's total level", "sample": "2154"},
    "total_xp": {"help": "The player's total XP", "sample": "312,441,092"},
    "combat_level": {"help": "The player's combat level", "sample": "126"},
    # Quests
    "quest_name": {"help": "The quest completed", "sample": "Desert Treasure II"},
    "quests_completed": {"help": "Quests completed", "sample": "158"},
    "total_quests": {"help": "Quests in the game", "sample": "165"},
    "completion_percentage": {"help": "Quest completion", "sample": "96%"},
    "quest_points": {"help": "Quest points from this quest", "sample": "5"},
    "total_quest_points": {"help": "The player's total quest points", "sample": "293"},
    "qp_percentage": {"help": "Quest point completion", "sample": "94%"},
    "timestamp": {"help": "When it happened", "sample": "today", "optional": True},
    # Deaths
    "killer": {"help": "What killed the player", "sample": "Vorkath"},
    "location": {"help": "Where they died", "sample": "Ungael"},
    "region_id": {"help": "OSRS region id", "sample": "9023", "optional": True},
    # Diaries
    "diary_name": {"help": "The diary area", "sample": "Kourend & Kebos"},
    "diary_tier": {"help": "The tier completed", "sample": "Elite"},
}

# {type: {"label", "group", "description", "tokens"}} — ``tokens`` extends
# _COMMON_TOKENS and is the editor's display order.
TYPE_META: Dict[str, Dict[str, Any]] = {
    "drop": {
        "label": "Drop",
        "group": "Loot",
        "description": "Posted when a member receives a drop worth announcing.",
        "tokens": ("item_name", "item_id", "item_value", "quantity", "total_value",
                   "npc_name", "npc_id", "kill_count", "month_name", "player_total_month",
                   "group_total_month", "group_total", "global_rank", "group_rank",
                   "group_to_group_rank", "user_count") + _POINTS_TOKENS + _MEDIA_TOKENS,
    },
    "clog": {
        "label": "Collection log",
        "group": "Loot",
        "description": "Posted when a member fills a collection log slot.",
        "tokens": ("item_name", "item_id", "collection_name", "npc_name", "kc_received",
                   "player_loot_month", "total_tracked") + _POINTS_TOKENS + _MEDIA_TOKENS,
    },
    "pb": {
        "label": "Personal best",
        "group": "Achievements",
        "description": "Posted when a member sets a new personal best time.",
        "tokens": ("npc_name", "npc_id", "personal_best", "team_size", "global_rank",
                   "total_ranked_global", "group_rank", "total_ranked_group",
                   "gear_image_url") + _POINTS_TOKENS + _MEDIA_TOKENS,
    },
    "ca": {
        "label": "Combat achievement",
        "group": "Achievements",
        "description": "Posted when a member completes a combat achievement task.",
        "tokens": ("task_name", "task_tier", "points_awarded", "total_points", "current_tier",
                   "next_tier", "next_tier_points", "points_left",
                   "progress") + _POINTS_TOKENS + _MEDIA_TOKENS,
    },
    "pet": {
        "label": "Pet",
        "group": "Achievements",
        "description": "Posted when a member receives a pet.",
        "tokens": ("pet_name", "source", "npc_name", "killcount", "milestone", "duplicate",
                   "previously_owned") + _POINTS_TOKENS + _MEDIA_TOKENS,
    },
    "level_up": {
        "label": "Level up",
        "group": "Progress",
        "description": "Posted when a member reaches a new level.",
        "tokens": ("skill_name", "skills_names", "skills_text", "new_level", "levels_gained",
                   "xp_total", "total_level", "total_xp", "combat_level") + _MEDIA_TOKENS,
    },
    "quest": {
        "label": "Quest",
        "group": "Progress",
        "description": "Posted when a member completes a quest.",
        "tokens": ("quest_name", "quests_completed", "total_quests", "completion_percentage",
                   "quest_points", "total_quest_points", "qp_percentage",
                   "timestamp") + _MEDIA_TOKENS,
    },
    "death": {
        "label": "Death",
        "group": "Progress",
        "description": "Posted when a member dies.",
        "tokens": ("source", "killer", "location", "region_id", "timestamp") + _MEDIA_TOKENS,
    },
    "diary": {
        "label": "Achievement diary",
        "group": "Progress",
        "description": "Posted when a member completes an achievement diary.",
        "tokens": ("diary_name", "diary_tier", "timestamp") + _MEDIA_TOKENS,
    },
}


def tokens_for(notification_type: str) -> List[Dict[str, Any]]:
    """Documented tokens for one type, in the editor's display order."""
    meta = TYPE_META.get(notification_type) or {}
    out: List[Dict[str, Any]] = []
    seen = set()
    for token in tuple(_COMMON_TOKENS) + tuple(meta.get("tokens") or ()):
        if token in seen:
            continue
        seen.add(token)
        doc = TOKEN_DOCS.get(token) or {}
        out.append({
            "token": token,
            "help": doc.get("help") or "",
            "sample": doc.get("sample", ""),
            "optional": bool(doc.get("optional")),
        })
    return out


# ── Defaults ─────────────────────────────────────────────────────────────────
# Starting points a group can edit, rather than a blank editor. The personal
# best layout mirrors services/pb_components.py, so switching a group over
# produces what the samples showed; the rest follow the same shape — a headline
# line, the subject beside its icon, the stats worth reading, then the proof.
#
# Each is written so that a player with no screenshot, no character model and
# no points awarded still produces a message: every line that can be absent
# holds its token alone, so losing the value loses the line and nothing else.
_ITEM_ICON = "https://www.droptracker.io/img/itemdb/{item_id}.png"

DEFAULT_LAYOUTS: Dict[str, Dict[str, Any]] = {
    "drop": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** received a drop"},
            {"type": "separator", "divider": True},
            {
                "type": "section",
                "content": "### {item_name}\n## {item_value}\nfrom **{npc_name}**",
                "thumbnail": _ITEM_ICON,
            },
            {"type": "text", "content": "**Kill count** {kill_count}"},
            {"type": "text", "content": "**Points** {group_points_awarded}"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "clog": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** filled a collection log slot"},
            {"type": "separator", "divider": True},
            {
                "type": "section",
                "content": "### {item_name}\n{collection_name}",
                "thumbnail": _ITEM_ICON,
            },
            {"type": "text", "content": "**Kill count** {kc_received}"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "pb": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** has achieved a new personal best"},
            {"type": "separator", "divider": True},
            {
                "type": "section",
                "content": "### {npc_name}\n# {personal_best}",
                "thumbnail": "{gear_image_url}",
            },
            {"type": "text", "content": "**Team size** {team_size}  •  **Global rank** {global_rank}"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "ca": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** completed a combat achievement"},
            {"type": "separator", "divider": True},
            {"type": "text", "content": "### {task_name}\n{task_tier} • **{points_awarded}** points"},
            {"type": "text", "content": "-# {progress}% of the way to {next_tier}"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "pet": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** has a new pet"},
            {"type": "separator", "divider": True},
            {"type": "text", "content": "### {pet_name}\nfrom **{source}** at **{killcount}** kc"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "level_up": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** levelled up"},
            {"type": "separator", "divider": True},
            {"type": "text", "content": "### {skills_text}"},
            {"type": "text", "content": "**Total level** {total_level}  •  **Combat** {combat_level}"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "quest": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** completed a quest"},
            {"type": "separator", "divider": True},
            {"type": "text", "content": "### {quest_name}"},
            {"type": "text", "content": "**{quests_completed}**/**{total_quests}** quests • **{total_quest_points}** quest points"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "death": {
        "accent_color": "#8b2f2f",
        "blocks": [
            {"type": "text", "content": "**{player_name}** died"},
            {"type": "separator", "divider": True},
            {"type": "text", "content": "### Killed by {killer}"},
            {"type": "text", "content": "-# {location}"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
    "diary": {
        "accent_color": "#c8aa6e",
        "blocks": [
            {"type": "text", "content": "**{player_name}** completed an achievement diary"},
            {"type": "separator", "divider": True},
            {"type": "text", "content": "### {diary_name}\n{diary_tier}"},
            {"type": "media", "urls": ["{image_url}"]},
        ],
    },
}


def default_layout(notification_type: str) -> Optional[Dict[str, Any]]:
    layout = DEFAULT_LAYOUTS.get(notification_type)
    # Copied so a caller editing the result cannot mutate the default.
    return _deep_copy(layout) if layout else None


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


# ── Resolution ───────────────────────────────────────────────────────────────
# Which groups may send notifications as components. Deliberately a hard-coded
# allowlist rather than an entitlement while this is being trialled: it changes
# what every member of a group receives, so it should not be switchable by
# anyone but us until the builder has been proven in the global group.
COMPONENTS_PILOT_GROUP_IDS = {2}


def components_enabled_for_group(group_id: int) -> bool:
    """True when a group is allowed to use component layouts at all.

    Separate from whether a layout exists or is active, so the editor can be
    hidden and the send path short-circuited by the same check.
    """
    try:
        return int(group_id) in COMPONENTS_PILOT_GROUP_IDS
    except (TypeError, ValueError):
        return False


def load_active_layout(session, group_id: int, notification_type: str) -> Optional[Dict[str, Any]]:
    """The layout a group's notification should use, or None to send an embed.

    Returns None for every group outside the pilot, for a type with no row, for
    a row that is authored but not marked active, and for a row whose JSON no
    longer parses — every one of those falls back to the embed path, so a broken
    layout costs the customisation rather than the notification.
    """
    if not components_enabled_for_group(group_id):
        return None

    try:
        from db.models import GroupComponentLayout

        row = (
            session.query(GroupComponentLayout)
            .filter(
                GroupComponentLayout.group_id == group_id,
                GroupComponentLayout.notification_type == notification_type,
            )
            .first()
        )
    except Exception as exc:
        print(f"Could not load component layout for group {group_id}: {exc}")
        return None

    if row is None or not row.active:
        return None

    import json

    try:
        layout = json.loads(row.layout)
    except (TypeError, ValueError):
        print(f"Component layout for group {group_id}/{notification_type} is not valid JSON")
        return None

    ok, _errors = validate_layout(layout)
    if not ok:
        # Saved layouts are validated, so this means the limits changed under a
        # stored template. Sending the embed is better than sending nothing.
        print(f"Component layout for group {group_id}/{notification_type} no longer validates")
        return None

    return layout


def to_interactions_components(payload: Dict[str, Any]) -> Optional[list]:
    """Convert a rendered payload into interactions.py component objects.

    ``render_layout`` deliberately produces plain dicts so the interesting logic
    (substitution, dropping, limits) is testable without a gateway. This is the
    thin adapter for the send path, which needs real objects — the same shape
    ``services/event_message_layouts.build_components`` returns.

    Returns None if the objects cannot be built, so the caller falls back to the
    embed rather than raising inside a notification.
    """
    if not payload:
        return None
    try:
        from interactions.models import (
            ActionRow,
            Button,
            ButtonStyle,
            ContainerComponent,
            MediaGalleryComponent,
            MediaGalleryItem,
            SectionComponent,
            SeparatorComponent,
            TextDisplayComponent,
            ThumbnailComponent,
            UnfurledMediaItem,
        )
    except Exception as exc:
        print(f"Could not import component models: {exc}")
        return None

    try:
        container = payload["components"][0]
        children = []
        for block in container.get("components", []):
            kind = block.get("type")
            if kind == TYPE_TEXT_DISPLAY:
                children.append(TextDisplayComponent(content=block["content"]))
            elif kind == TYPE_SEPARATOR:
                children.append(SeparatorComponent(divider=bool(block.get("divider", True))))
            elif kind == TYPE_SECTION:
                text = TextDisplayComponent(content=block["components"][0]["content"])
                accessory = block.get("accessory")
                if accessory:
                    children.append(SectionComponent(
                        components=[text],
                        accessory=ThumbnailComponent(
                            media=UnfurledMediaItem(url=accessory["media"]["url"])
                        ),
                    ))
                else:
                    # A section with no accessory is just text as far as Discord
                    # is concerned, and building one would be rejected.
                    children.append(text)
            elif kind == TYPE_MEDIA_GALLERY:
                children.append(MediaGalleryComponent(items=[
                    MediaGalleryItem(media=UnfurledMediaItem(url=item["media"]["url"]))
                    for item in block.get("items", [])
                ]))
            elif kind == TYPE_ACTION_ROW:
                children.append(ActionRow(*[
                    Button(style=ButtonStyle.URL, label=b["label"], url=b["url"])
                    for b in block.get("components", [])
                ]))

        if not children:
            return None

        kwargs = {}
        if "accent_color" in container:
            kwargs["accent_color"] = container["accent_color"]
        return [ContainerComponent(*children, **kwargs)]
    except Exception as exc:
        print(f"Could not build components from layout: {exc}")
        return None
