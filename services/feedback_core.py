"""Pure pieces of clan leader feedback (web155a): the staff Discord posts and
their buttons.

Every answer is posted to the staff "raw" channel with buttons:

    dtfb:p:<response_id>   Publish: repost the quote in the testimonials channel
    dtfb:d:<response_id>   Dismiss: reviewed, not for publishing
    dtfb:u:<response_id>   Unpublish: delete the testimonials post again
    dtfb:r:<response_id>   Reopen: undo a dismiss

``services/feedback_buttons_discord.py`` answers them; only site superadmins
may press them. Publish is offered only when the person agreed to be quoted
and wrote something under "What's working best".

Nothing here touches Discord or the database.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

PREFIX = "dtfb:"
ACTIONS = {"p": "publish", "d": "dismiss", "u": "unpublish", "r": "reopen"}

#: Staff channel every answer lands in first.
RAW_CHANNEL_ID = os.getenv("DISCORD_FEEDBACK_RAW_CHANNEL_ID", "1537408563826397276")
#: Testimonials channel a published quote goes to.
PUBLISHED_CHANNEL_ID = os.getenv("DISCORD_FEEDBACK_PUBLISHED_CHANNEL_ID", "1567153105928323134")

RATING_MIN = 1
RATING_MAX = 10
TEXT_MAX = 500
ATTRIBUTIONS = ("clan", "full", "anon")

ANON_LABEL = "A DropTracker clan leader"
NO_CLAN_LABEL = "A DropTracker user"

_GOLD = 0xD4AF37


def parse_custom_id(custom_id) -> Optional[dict]:
    """``{"action", "response_id"}`` or None."""
    if not isinstance(custom_id, str) or not custom_id.startswith(PREFIX):
        return None
    parts = custom_id[len(PREFIX):].split(":")
    if len(parts) != 2 or parts[0] not in ACTIONS:
        return None
    try:
        return {"action": ACTIONS[parts[0]], "response_id": int(parts[1])}
    except ValueError:
        return None


def custom_id(action: str, response_id: int) -> str:
    code = {v: k for k, v in ACTIONS.items()}[action]
    return f"{PREFIX}{code}:{int(response_id)}"


def is_quotable(quote_consent, liked_text) -> bool:
    return bool(quote_consent) and bool((liked_text or "").strip())


def rating_color(rating: int) -> int:
    if rating >= 8:
        return 0x3BA55C  # green
    if rating >= 5:
        return 0xFAA61A  # amber
    return 0xED4245  # red


def attribution_label(attribution: str, *, username: Optional[str], group_name: Optional[str]) -> str:
    """How a published quote is signed, following the person's choice."""
    if attribution == "anon":
        return ANON_LABEL
    if attribution == "full":
        if username and group_name:
            return f"{username}, {group_name}"
        return username or group_name or ANON_LABEL
    return group_name or NO_CLAN_LABEL


_ATTRIBUTION_WORDS = {"clan": "Clan name only", "full": "Name and clan", "anon": "Anonymous"}


def _since(dt: Optional[datetime]) -> Optional[str]:
    return f"on DT since {dt.strftime('%b %Y')}" if dt else None


def raw_embed(
    *,
    response_id: int,
    rating: int,
    liked_text: Optional[str],
    improve_text: Optional[str],
    quote_consent: bool,
    attribution: str,
    contact_ok: bool,
    username: Optional[str],
    user_id: int,
    discord_id: Optional[str],
    group_name: Optional[str],
    role: Optional[str],
    tier_name: Optional[str],
    member_count: Optional[int],
    group_since: Optional[datetime],
    source: Optional[str],
) -> dict:
    """The staff copy of an answer: everything, including who sent it."""
    context = [
        (role or "").capitalize() or None,
        f"{tier_name} tier" if tier_name else None,
        f"{member_count:,} members" if member_count is not None else None,
        _since(group_since),
    ]
    who = f"From **{username or f'User {user_id}'}** (user #{user_id})"
    description = "\n".join(filter(None, [" · ".join(c for c in context if c) or None, who]))

    def _text(value: Optional[str]) -> str:
        value = (value or "").strip()
        return value[:1024] if value else "*Nothing written*"

    sharing = (
        f"✅ OK to quote ({_ATTRIBUTION_WORDS.get(attribution, 'Clan name only')})"
        if quote_consent else "🚫 Not for quoting"
    )
    if contact_ok:
        sharing += f"\n💬 OK to contact: <@{discord_id}>" if discord_id else "\n💬 OK to contact (no Discord linked)"

    return {
        "title": f"{rating}/10 · {group_name or 'No clan picked'}"[:256],
        "description": description[:4096],
        "color": rating_color(rating),
        "fields": [
            {"name": "Working best", "value": _text(liked_text), "inline": False},
            {"name": "Would change", "value": _text(improve_text), "inline": False},
            {"name": "Sharing", "value": sharing[:1024], "inline": False},
        ],
        "footer": {"text": f"Response #{response_id} · {source or 'Share feedback page'}"[:2048]},
    }


def raw_buttons(response_id: int, quotable: bool) -> list:
    """Outbox component entries for a fresh raw post (see
    ``services/discord_outbox._build_components``)."""
    buttons = []
    if quotable:
        buttons.append({"label": "Publish", "custom_id": custom_id("publish", response_id),
                        "style": "success"})
    buttons.append({"label": "Dismiss", "custom_id": custom_id("dismiss", response_id),
                    "style": "secondary"})
    return buttons


def _row(*buttons) -> list:
    return [{"type": 1, "components": list(buttons)}]


def _button(label: str, cid: str, style: int) -> dict:
    return {"type": 2, "style": style, "label": label, "custom_id": cid}


def open_components(response_id: int, quotable: bool) -> list:
    """Raw JSON components for a raw post waiting for a decision."""
    buttons = []
    if quotable:
        buttons.append(_button("Publish", custom_id("publish", response_id), 3))
    buttons.append(_button("Dismiss", custom_id("dismiss", response_id), 2))
    return _row(*buttons)


def published_components(response_id: int, message_url: Optional[str]) -> list:
    buttons = [_button("Unpublish", custom_id("unpublish", response_id), 4)]
    if message_url:
        buttons.append({"type": 2, "style": 5, "label": "View post", "url": message_url})
    return _row(*buttons)


def dismissed_components(response_id: int) -> list:
    return _row(_button("Reopen", custom_id("reopen", response_id), 2))


def published_embed(*, rating: int, quote: str, attribution_text: str) -> dict:
    """The public testimonial: the quote, the score and the chosen signature.
    Never the suggestions text, contact details, plan or member count."""
    return {
        "author": {"name": f"⭐ {rating}/10"},
        "description": f"“{quote.strip()[:3900]}”",
        "color": _GOLD,
        "footer": {"text": attribution_text[:2048]},
    }


def message_url(guild_id, channel_id, message_id) -> Optional[str]:
    if not (guild_id and channel_id and message_id):
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
