"""Components V2 layout for a personal best notification.

An embed can only put an image in two places — a small thumbnail in the corner,
or a full-width panel pinned to the bottom — so a character render either gets
lost or gets dumped underneath the text with nothing beside it. Components V2
allows a section with an accessory, which is what this is for: the character
sits *next to* the time and the ranks, the way it reads in game.

Returned as plain dicts rather than interactions.py objects so this is testable
without a gateway connection and usable from anything that can POST JSON — the
notification service, the outbox, or a one-off script.

Discord requires the ``IS_COMPONENTS_V2`` message flag for these, and a message
carrying that flag may not also carry ``content`` or ``embeds``. Everything the
message says must therefore live inside the components.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Message flag telling Discord to interpret the components as V2.
IS_COMPONENTS_V2 = 1 << 15

# Component type ids from the Discord docs.
TYPE_SECTION = 9
TYPE_TEXT_DISPLAY = 10
TYPE_THUMBNAIL = 11
TYPE_MEDIA_GALLERY = 12
TYPE_SEPARATOR = 14
TYPE_CONTAINER = 17

# Accent stripe down the side of the container — the site's gold.
ACCENT_COLOUR = 0xC8AA6E


def _text(content: str) -> Dict[str, Any]:
    return {"type": TYPE_TEXT_DISPLAY, "content": content}


def _separator(divider: bool = True, spacing: int = 1) -> Dict[str, Any]:
    return {"type": TYPE_SEPARATOR, "divider": divider, "spacing": spacing}


def _rank(value: Optional[int], total: Optional[int]) -> str:
    """"#3 of 48", or an em dash when we do not know."""
    if value is None:
        return "—"
    if total:
        return f"#{value:,} of {total:,}"
    return f"#{value:,}"


def build_pb_message(
    *,
    player_name: str,
    boss: str,
    time_display: str,
    previous_best: Optional[str] = None,
    team_size: str = "Solo",
    kill_count: Optional[int] = None,
    group_rank: Optional[int] = None,
    group_total: Optional[int] = None,
    global_rank: Optional[int] = None,
    global_total: Optional[int] = None,
    points_awarded: Optional[int] = None,
    points_total: Optional[int] = None,
    character_image_url: Optional[str] = None,
    screenshot_url: Optional[str] = None,
    profile_url: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """The message payload for a new personal best.

    ``character_image_url`` is the player's rendered model. It becomes the
    accessory of the headline section, so it sits beside the time rather than
    below the whole message. Absent, the section degrades to plain text — most
    players will not have uploaded a model.

    ``screenshot_url`` is the player's own capture of the kill, which stays a
    full-width gallery item underneath: it is the evidence, and shrinking it to
    a thumbnail would defeat the point.
    """
    headline_lines = [
        f"### {boss}",
        f"# {time_display}",
    ]
    if previous_best:
        # The improvement is the story; showing only the new time buries it.
        headline_lines.append(f"-# Previous best: {previous_best}")

    headline: Dict[str, Any] = {
        "type": TYPE_SECTION,
        "components": [_text("\n".join(headline_lines))],
    }
    if character_image_url:
        headline["accessory"] = {
            "type": TYPE_THUMBNAIL,
            "media": {"url": character_image_url},
            "description": f"{player_name}'s character",
        }

    # Two columns of stats read better than six one-line fields, and Components
    # V2 has no field primitive — this is markdown doing the same job.
    left: List[str] = [f"**Team size** {team_size}"]
    if kill_count is not None:
        left.append(f"**Kill count** {kill_count:,}")
    if points_awarded is not None:
        left.append(f"**Points earned** `{points_awarded:,}`")

    right: List[str] = []
    if group_rank is not None:
        right.append(f"**Group rank** {_rank(group_rank, group_total)}")
    if global_rank is not None:
        right.append(f"**Global rank** {_rank(global_rank, global_total)}")
    if points_total is not None:
        right.append(f"**Total points** `{points_total:,}`")

    stats_lines = []
    for i in range(max(len(left), len(right))):
        parts = []
        if i < len(left):
            parts.append(left[i])
        if i < len(right):
            parts.append(right[i])
        stats_lines.append("  •  ".join(parts))

    children: List[Dict[str, Any]] = [
        _text(f"**{player_name}** has achieved a new personal best"),
        _separator(),
        headline,
    ]
    if stats_lines:
        children.append(_separator(divider=False))
        children.append(_text("\n".join(stats_lines)))

    if screenshot_url:
        children.append(_separator())
        children.append({
            "type": TYPE_MEDIA_GALLERY,
            "items": [{"media": {"url": screenshot_url}}],
        })

    if profile_url:
        children.append(_separator(divider=False))
        children.append(_text(f"-# [View {player_name}'s profile]({profile_url})"))

    if note:
        # Small print for anything the reader needs to know about the message
        # itself — a pending-review marker, or that this is a sample.
        children.append(_text(f"-# {note}"))

    return {
        # V2 messages carry no content or embeds; the container is the message.
        "flags": IS_COMPONENTS_V2,
        "components": [
            {
                "type": TYPE_CONTAINER,
                "accent_color": ACCENT_COLOUR,
                "components": children,
            }
        ],
    }
