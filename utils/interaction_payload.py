"""Reading a RAW Discord interaction payload safely.

interactions.py hands raw listeners (``raw_interaction_create``) and its own
parser SHALLOW copies of the gateway event, and its ``Member._process_dict``
pops ``member["user"]`` out of the shared nested dict, leaving ``member["id"]``
behind. Which task runs first is not fixed, so a raw listener may see either
shape — and a click relayed by another library's bot (services/discord_relay)
may arrive in either shape too. Every raw reader goes through here.
"""
from __future__ import annotations

from typing import Optional


def interaction_user_id(payload) -> Optional[str]:
    """The Discord id of whoever sent the interaction: ``member.user.id`` in
    a guild, top-level ``user.id`` in a DM, or the ``member.id`` the library
    leaves behind after popping ``member.user``."""
    if not isinstance(payload, dict):
        return None
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    found = user.get("id") or member.get("id")
    return str(found) if found else None
