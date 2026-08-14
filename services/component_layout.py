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


# ── Defaults ─────────────────────────────────────────────────────────────────
# Starting points a group can edit, rather than a blank editor. The personal
# best layout mirrors services/pb_components.py, so switching a group over
# produces what the samples showed.
DEFAULT_LAYOUTS: Dict[str, Dict[str, Any]] = {
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
