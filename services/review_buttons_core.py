"""Pure pieces of the Discord review buttons (suggestion #198).

Review messages carry Approve / Reject (and, after a decision, Undo) buttons:

    dtrv:m:<a|r|u>:<group_id>:<drop_id>   held manual drop (drop_group_moderation),
                                          posted by data/submissions/manual_policy.py
    dtrv:e:<a|r>:<completion_id>          pending event completion, on the
                                          "awaiting review" card the notification
                                          service posts

``services/review_buttons_discord.py`` answers them. Who may press one is
decided by :func:`may_review`: the group's admins, members with Manage Server
in the group's own server, the group's configured staff role
(``review_role_id``), and, for event cards, the group's event managers. The
click must come from the group's own server, so a role or permission in some
other guild never counts.

Nothing here touches Discord or the database.
"""
from __future__ import annotations

from typing import Iterable, Optional

PREFIX = "dtrv:"
REVIEW_ROLE_KEY = "review_role_id"

MANAGE_GUILD = 1 << 5

ACTIONS = {"a": "approve", "r": "reject", "u": "undo"}


def parse_custom_id(custom_id) -> Optional[dict]:
    """``{"kind": "drop"|"event", "action", ...ids}`` or None."""
    if not isinstance(custom_id, str) or not custom_id.startswith(PREFIX):
        return None
    parts = custom_id[len(PREFIX):].split(":")
    try:
        if len(parts) == 4 and parts[0] == "m" and parts[1] in ACTIONS:
            return {"kind": "drop", "action": ACTIONS[parts[1]],
                    "group_id": int(parts[2]), "drop_id": int(parts[3])}
        if len(parts) == 3 and parts[0] == "e" and parts[1] in ("a", "r"):
            return {"kind": "event", "action": ACTIONS[parts[1]],
                    "completion_id": int(parts[2])}
    except ValueError:
        return None
    return None


def drop_custom_id(action: str, group_id: int, drop_id: int) -> str:
    code = {v: k for k, v in ACTIONS.items()}[action]
    return f"{PREFIX}m:{code}:{int(group_id)}:{int(drop_id)}"


def has_manage_guild(permissions) -> bool:
    """Whether an interaction member's permission bitfield (a decimal string
    in the raw payload) includes Manage Server."""
    try:
        return bool(int(permissions or 0) & MANAGE_GUILD)
    except (TypeError, ValueError):
        return False


def may_review(*, interaction_guild_id, group_guild_id, is_group_admin: bool,
               is_event_manager: bool = False, member_permissions=None,
               member_role_ids: Iterable = (), review_role_id=None) -> bool:
    """The permission rule, given the facts (see the module docstring).

    A linked group admin may review from anywhere the card is. Everything
    that comes from Discord itself (Manage Server, a role) only counts when
    the click happened in the group's own server.
    """
    if is_group_admin or is_event_manager:
        return True
    if not interaction_guild_id or not group_guild_id:
        return False
    if str(interaction_guild_id) != str(group_guild_id):
        return False
    if has_manage_guild(member_permissions):
        return True
    role = str(review_role_id or "").strip()
    return bool(role) and role in {str(r) for r in (member_role_ids or ())}


def _strip(components) -> list:
    """Drop every action row holding one of our buttons, at any depth."""
    out = []
    for comp in components or []:
        if not isinstance(comp, dict):
            continue
        children = comp.get("components")
        if comp.get("type") == 1 and any(
                isinstance(c, dict) and str(c.get("custom_id") or "").startswith(PREFIX)
                for c in (children or [])):
            continue
        if isinstance(children, list):
            comp = {**comp, "components": _strip(children)}
        out.append(comp)
    return out


_COMPONENTS_V2 = 1 << 15


def decided_message(message: dict, status_line: str, undo_custom_id: Optional[str] = None) -> dict:
    """The edit for a review message once someone decided: the review buttons
    removed, a status line added, and an Undo button when one applies.

    Components V2 messages (event cards) get the line as a text display at
    the end of their container; plain messages (manual-review pings) get it
    appended to their content. Link buttons (the review queue) survive.
    """
    message = message or {}
    components = _strip(message.get("components"))
    undo_row = None
    if undo_custom_id:
        undo_row = {"type": 1, "components": [
            {"type": 2, "style": 2, "label": "Undo", "custom_id": undo_custom_id}]}
    if int(message.get("flags") or 0) & _COMPONENTS_V2:
        text = {"type": 10, "content": status_line}
        if components and components[-1].get("type") == 17:
            container = dict(components[-1])
            container["components"] = list(container.get("components") or []) + [text]
            if undo_row:
                container["components"].append(undo_row)
            components[-1] = container
        else:
            components.append(text)
            if undo_row:
                components.append(undo_row)
        return {"components": components}
    content = (message.get("content") or "").rstrip()
    # A re-decision (undo, then approve again) replaces the last status line.
    lines = content.split("\n")
    if lines and lines[-1].startswith(("✅", "❌", "↩️")):
        lines = lines[:-1]
    content = "\n".join(lines + [status_line]).strip()
    if undo_row:
        components = components + [undo_row]
    return {"content": content[:2000], "components": components}


def drop_review_buttons(group_id: int, drop_id: int) -> list:
    """Approve / Reject row restored on a manual-review ping after an Undo."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 3, "label": "Approve",
         "custom_id": drop_custom_id("approve", group_id, drop_id)},
        {"type": 2, "style": 4, "label": "Reject",
         "custom_id": drop_custom_id("reject", group_id, drop_id)},
    ]}]


def undone_message(message: dict, status_line: str, group_id: int, drop_id: int) -> dict:
    """A manual-review ping after Undo: back to Approve / Reject, with a line
    saying who took the decision back. (Event cards have no Undo: a
    confirmed completion is corrected by revoking it on the site.)"""
    edit = decided_message(message, status_line)
    edit["components"] = drop_review_buttons(group_id, drop_id) + list(edit.get("components") or [])
    return edit
