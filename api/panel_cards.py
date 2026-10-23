"""Detail cards for the plugin's Groups and Players pages.

The plugin used to assemble these pages itself from the raw search payloads
(GroupPanel / PlayerStatsPanel), which cost plugin-hub token budget. It now
draws a ``card`` built here, added to the existing responses so older plugins,
which read the raw fields, are unaffected.

The card is data, not markup: plain text in fixed slots, a tone from a closed
set, and actions from a closed set the plugin implements itself:

* ``player`` (value: a player name): open that player on the Players page;
* ``web`` (value: a droptracker.io path such as ``players/12/view``): the
  plugin builds the URL on its own hardcoded host;
* ``discord`` (value: an invite code): the plugin builds the discord.gg link;
* ``lootboard`` (value: a group id): show that group's lootboard;
* ``refresh``: search again.

``icon_path`` is an ``/img``-relative path, again on the plugin's own host.
Pure and stdlib-only so the unit tests load it directly.
"""
from __future__ import annotations

NOT_REGISTERED_TIP = ("This account has not been claimed on DropTracker.\n"
                      "Claim your in-game name in our Discord to register.")
POINTS_TIP = "View more info about points (click to open wiki)"
NO_RECENT = "No recent submissions available"


def _action(kind, value=None):
    return {"type": kind, "value": None if value is None else str(value)}


def _stat(label, value, action=None, tip=None):
    return {"label": label, "value": value, "action": action, "tip": tip}


def _text(value, fallback="—"):
    text = "" if value is None else str(value).strip()
    return text or fallback


def group_card(*, name, group_id, description=None, icon_path=None, members=None, rank=None,
               loot=None, top_player=None, recent=None, invite_code=None, partial=False):
    """A group's detail card. ``partial`` is the instant preview built from a
    leaderboard row while the full search loads: no description or recent
    activity yet, so those slots say they're loading."""
    top = _text(top_player, "")
    stats = [
        _stat("Members", _text(members)),
        _stat("Global Rank", f"#{rank}" if rank not in (None, "") else "—"),
        _stat("Monthly Loot", f"{loot} GP" if loot not in (None, "") else "—"),
        _stat("Top Player", top or "—", _action("player", top) if top else None),
    ]
    actions = []
    if invite_code:
        actions.append({"label": "Discord", "action": _action("discord", invite_code), "icon": None})
    actions.append({"label": "View Lootboard", "action": _action("lootboard", group_id), "icon": "lootboard"})
    return {
        "title": name,
        "group_id": group_id,
        "icon_path": icon_path,
        "subtitle": None,
        "description": "Loading details…" if partial else (description or ""),
        "list_title": None,
        "list": [],
        "stats": stats,
        "recent": None if partial else (recent or []),
        "recent_group": True,
        "recent_empty": "Loading recent activity…" if partial else NO_RECENT,
        "actions": actions,
    }


def player_card(*, name, player_id, registered, total_loot, rank, points, groups=None, recent=None):
    """A player's detail card."""
    subtitle = ({"text": "Registered Player", "tone": "good", "tip": None} if registered
                else {"text": "Not registered!", "tone": "warn", "tip": NOT_REGISTERED_TIP})
    lines = []
    for group in groups or []:
        line = f"- {group.get('name')}"
        if group.get("members") is not None:
            line += f" ({group.get('members')} members)"
        lines.append(line)
    profile = f"players/{player_id}/view" if player_id is not None else f"players/{name}/view"
    return {
        "title": name,
        "group_id": None,
        "icon_path": None,
        "subtitle": subtitle,
        "description": None,
        "list_title": "Groups:" if lines else None,
        "list": lines,
        "stats": [
            _stat("Total Loot", f"{total_loot} GP"),
            _stat("Global Rank", f"#{rank}"),
            _stat("Lifetime Points", f"{points} pts", _action("web", "wiki/points"), POINTS_TIP),
        ],
        "recent": recent or [],
        "recent_group": False,
        "recent_empty": NO_RECENT,
        "actions": [
            {"label": "Refresh Stats", "action": _action("refresh"), "icon": None},
            {"label": "View Profile", "action": _action("web", profile), "icon": "external"},
        ],
    }
