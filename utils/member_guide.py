"""The "how to join in" guide a clan posts for its members.

One wording for every place it is posted: the website's setup wizard (through
the Discord outbox, as an embed), the ``/group-setup`` finish step and the
``/send_player_faq`` command (Components V2, with live command mentions).
Pure: no interactions import, so the Web API can build it.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from utils.site_urls import WEBSITE_URL

PLUGIN_URL = f"{WEBSITE_URL}/runelite"
REGISTER_URL = f"{WEBSITE_URL}/register"
DOCS_URL = f"{WEBSITE_URL}/docs/getting-started"

TITLE = "Getting started with The DropTracker"


def _plain(name: str) -> str:
    return f"`/{name}`"


def sections(mention: Optional[Callable[[str], str]] = None) -> List[Tuple[str, str]]:
    """[(heading, markdown body)]. ``mention(name)`` renders a slash command
    (a live ``</name:id>`` in the bot); plain code text otherwise."""
    m = mention or _plain
    return [
        ("1. Install the plugin",
         f"Search **DropTracker** in the [RuneLite Plugin Hub]({PLUGIN_URL}). "
         "Tracking starts as soon as it's on."),
        ("2. Turn on \"Use API\"",
         "In the plugin's settings, under **Advanced**. Events, live boards and lookups need it."),
        ("3. Link your account",
         f"[Sign in on the website]({REGISTER_URL}) or use {m('claim-rsn')} here. "
         "Your drops then ping you and show on your profile."),
        ("4. Be in the clan's Wise Old Man group",
         "That's how your drops count for the clan. Ask a leader if you're missing."),
        ("Good to know",
         f"{m('pingme')} and {m('hideme')} control pings and privacy. "
         f"Missed a drop? Use {m('submit drop')}. Your points: {m('my-points')}."),
    ]


def intro(group_name: Optional[str]) -> str:
    who = f"**{group_name}**" if group_name else "This clan"
    return (f"{who} uses The DropTracker to post drops, personal bests, collection "
            "logs, pets and events. Here's how to join in.")


def guide_embed(group_name: Optional[str]) -> Dict:
    """Discord embed dict (for ``discord_outbox.enqueue(embed=...)``)."""
    return {
        "title": TITLE,
        "description": intro(group_name),
        "color": 0xC9A14A,
        "fields": [{"name": h, "value": b, "inline": False} for h, b in sections()],
        "footer": {"text": "The DropTracker | droptracker.io"},
    }


def guide_buttons(group_id: Optional[int]) -> List[Dict[str, str]]:
    """Link buttons (outbox ``components`` format)."""
    out = [
        {"label": "Get the plugin", "url": PLUGIN_URL},
        {"label": "Link your account", "url": REGISTER_URL},
    ]
    if group_id:
        out.append({"label": "Our clan page", "url": f"{WEBSITE_URL}/groups/{group_id}"})
    return out
