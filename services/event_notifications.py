"""Task 19 — per-event Discord notification routing + embed content specs.

The pieces of the event notification pipeline that are pure logic:

- which channel *kind* each ``notification_queue`` type posts to, with the
  spec'd fallback (completions/leaderboard/admin -> announcements, nothing
  configured -> skip),
- the content of each embed as a plain dict "spec" (title/description/fields/
  url/color/thumbnail), converted into a real ``interactions.Embed`` by
  ``utils.embeds.build_event_embed``.

Module-level imports are stdlib-only on purpose: the unit tests load this file
directly (like ``tests/unit/test_event_engine_matcher.py`` does for the
engine) so the conftest ``db``/``services`` stubs never interfere. Anything
DB-shaped is lazy-imported inside functions.
"""
from __future__ import annotations

from typing import Optional

EVENT_BASE_URL = "https://www.droptracker.io/events"

# Which channel kind each queue type posts to (19-event-discord.md spec table).
KIND_FOR_TYPE = {
    "event_started": "announcements",
    "event_ended": "announcements",
    "event_completion": "completions",
    "event_cell": "completions",
    "event_line": "completions",
    "event_blackout": "completions",
    "event_lead_change": "leaderboard",
    "event_pending": "admin",
    # Task 21: the scheduler sweep could not activate a scheduled draft.
    "event_activation_failed": "admin",
    # Interactive "Sign up" prompt (posted on demand by an admin) — carries a
    # button the notification sender attaches (services/notification_service).
    "event_signup_prompt": "announcements",
}

EVENT_NOTIFICATION_TYPES = tuple(KIND_FOR_TYPE)

_MEDALS = ("\U0001F947", "\U0001F948", "\U0001F949")  # gold / silver / bronze

# Embed accent colors, keeping the drop-embed visual language (0x00ff00 green
# for "something good happened"; distinct hues for the other families).
_COLORS = {
    "event_started": 0x00FF00,
    "event_ended": 0xFFD700,
    "event_completion": 0x00FF00,
    "event_cell": 0x9B59B6,
    "event_line": 0x9B59B6,
    "event_blackout": 0x2C2F33,
    "event_lead_change": 0xFFD700,
    "event_pending": 0xE67E22,
    "event_activation_failed": 0xED4245,
    "event_signup_prompt": 0x5865F2,  # Discord blurple — a call to action
}


def event_url(event_id) -> str:
    return f"{EVENT_BASE_URL}/{event_id}"


def event_ping_role_ids(ping_config_json, key: str) -> list:
    """Role ids configured for one ping key in ``web_events.ping_config``
    (JSON ``{key: [role ids]}``). [] on unset/corrupt config — pings must
    never break a send."""
    import json

    if not ping_config_json:
        return []
    try:
        data = json.loads(ping_config_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    roles = data.get(key)
    if not isinstance(roles, list):
        return []
    return [str(r) for r in roles if r]


def ping_content(role_ids) -> Optional[str]:
    """`<@&id>` mention prefix for a notification message, or None when no
    roles are configured (embed-only send, exactly as before)."""
    mentions = " ".join(f"<@&{rid}>" for rid in role_ids)
    return mentions or None


def resolve_event_channel(channels_by_kind: dict, notification_type: str) -> Optional[str]:
    """The channel id a queue type should post to, or None to skip silently.

    ``channels_by_kind`` is {kind: channel_id} from ``web_event_channels``.
    completions/leaderboard/admin fall back to announcements when their own
    kind isn't configured; announcements has no fallback.
    """
    kind = KIND_FOR_TYPE.get(notification_type)
    if kind is None:
        return None
    channel_id = (channels_by_kind or {}).get(kind)
    if not channel_id and kind != "announcements":
        channel_id = (channels_by_kind or {}).get("announcements")
    return str(channel_id) if channel_id else None


def load_event_channels(session, event_id: int) -> dict:
    """{kind: channel_id} for one event (lazy db import — see module docstring)."""
    from db.models import EventChannel

    rows = session.query(EventChannel).filter(EventChannel.event_id == event_id).all()
    return {r.kind: str(r.channel_id) for r in rows if r.channel_id}


# --------------------------------------------------------------------------- #
# Embed content specs
# --------------------------------------------------------------------------- #
def _standings_lines(standings, limit: int) -> str:
    lines = []
    for i, team in enumerate((standings or [])[:limit]):
        medal = _MEDALS[i] if i < len(_MEDALS) else "•"
        lines.append(f"{medal} **{team.get('name')}** — `{int(team.get('score') or 0)} pts`")
    return "\n".join(lines) if lines else "No teams yet."


def _fmt_ts(unix) -> Optional[str]:
    try:
        return f"<t:{int(unix)}:f>" if unix else None
    except (TypeError, ValueError):
        return None


def event_embed_spec(notification_type: str, data: dict, standings=None) -> dict:
    """Plain-dict embed description for one event notification.

    Returns {title, url, description, color, fields: [{name, value, inline}],
    thumbnail?, author_name?}. ``data`` is the (enriched) notification_queue
    JSON payload; ``standings`` is [{name, score}] best-first when the type
    needs them (lead change, event end).
    """
    data = data or {}
    event_id = data.get("event_id")
    event_name = data.get("event_name") or "Event"
    url = event_url(event_id) if event_id else None
    team = data.get("team_name") or (f"Team {data.get('team_id')}" if data.get("team_id") else None)
    player = data.get("player_name")
    task_label = data.get("task_label")
    points = int(data.get("points") or 0)

    spec = {
        "title": event_name,
        "url": url,
        "description": None,
        "color": _COLORS.get(notification_type, 0x00FF00),
        "fields": [],
        "thumbnail": None,
        "author_name": event_name,
    }

    def field(name, value, inline=True):
        if value:
            spec["fields"].append({"name": name, "value": value, "inline": inline})

    if notification_type == "event_started":
        spec["title"] = f"\U0001F3C1 {event_name} has started!"
        desc = data.get("description") or "The event is live — good luck!"
        spec["description"] = f"{desc}\n\n[Follow the event live]({url})" if url else desc
        starts, ends = _fmt_ts(data.get("starts_at")), _fmt_ts(data.get("ends_at"))
        if starts:
            field("Started", starts)
        if ends:
            field("Ends", ends)
        if data.get("team_count"):
            field("Teams", f"`{data['team_count']}`")

    elif notification_type == "event_ended":
        spec["title"] = f"\U0001F3C6 {event_name} has ended!"
        spec["description"] = f"[Full results]({url})" if url else None
        field("Final standings", _standings_lines(standings, 5), inline=False)

    elif notification_type == "event_completion":
        spec["title"] = f"✅ Task complete: {task_label or 'Task'}"
        who = f"**{team}**" if team else "A team"
        by = f" (by `{player}`)" if player else ""
        spec["description"] = f"{who} completed **{task_label or 'a task'}**{by}"
        if points:
            field("Points", f"`+{points}`")
        if data.get("team_score") is not None:
            field("Team total", f"`{int(data['team_score'])} pts`")
        cells = data.get("cell_idxs") or []
        if cells:
            field("Bingo", f"Cell{'s' if len(cells) != 1 else ''} `{', '.join(str(c) for c in cells)}` marked")
        if data.get("proof_url"):
            spec["thumbnail"] = data["proof_url"]

    elif notification_type == "event_cell":
        spec["title"] = "\U0001F3AF Bingo cell completed"
        cell = data.get("cell_label") or (
            f"Cell {data.get('cell_idx')}" if data.get("cell_idx") is not None else "A cell")
        spec["description"] = f"**{team or 'A team'}** marked **{cell}**"
        if points:
            field("Points", f"`+{points}`")

    elif notification_type == "event_line":
        spec["title"] = "\U0001F4CF Line bonus!"
        spec["description"] = f"**{team or 'A team'}** completed a full line"
        bonus = int(data.get("bonus_points") or points or 0)
        if bonus:
            field("Bonus", f"`+{bonus} pts`")

    elif notification_type == "event_blackout":
        spec["title"] = "\U0001F311 BLACKOUT!"
        spec["description"] = f"**{team or 'A team'}** completed the entire board"
        bonus = int(data.get("bonus_points") or points or 0)
        if bonus:
            field("Bonus", f"`+{bonus} pts`")

    elif notification_type == "event_lead_change":
        spec["title"] = f"\U0001F451 New leader: {team or 'a new team'}"
        via = f" after **{task_label}**" if task_label else ""
        spec["description"] = f"**{team or 'A team'}** takes first place{via}!"
        field("Standings", _standings_lines(standings, 3), inline=False)

    elif notification_type == "event_pending":
        spec["title"] = "\U0001F50D Completion awaiting review"
        spec["description"] = f"**{task_label or 'A task'}** needs an admin's confirmation."
        if player:
            field("Player", f"`{player}`")
        if team:
            field("Team", f"**{team}**")
        review_url = data.get("review_url") or url
        if review_url:
            field("Review", f"[Open the review queue]({review_url})", inline=False)
        if data.get("proof_url"):
            spec["thumbnail"] = data["proof_url"]

    elif notification_type == "event_signup_prompt":
        spec["title"] = f"\U0001F4E3 Sign up for {event_name}"
        how = {
            "self_join": "Pick your account, then choose your team.",
            "auto_assign": "Pick your account — you'll be placed on a team automatically.",
            "signup_pool": "Pick your account to join the sign-up pool; "
                           "admins will sort teams later.",
        }.get(data.get("formation_mode"), "Pick your account to enter.")
        spec["description"] = (
            f"{data.get('description') or 'This event is open for sign-ups!'}\n\n"
            f"{how}\n-# One account per person. "
            f"Not linked yet? Sign in at droptracker.io first."
        )
        ends = _fmt_ts(data.get("ends_at"))
        if ends:
            field("Sign-ups close", ends)

    elif notification_type == "event_activation_failed":
        spec["title"] = f"⚠️ {event_name} could not start"
        reason = data.get("reason") or "It failed the activation checks."
        spec["description"] = (
            f"The scheduled start passed, but the event could not be "
            f"activated: {reason}"
        )
        starts = _fmt_ts(data.get("starts_at"))
        if starts:
            field("Scheduled start", starts)
        if url:
            field("Fix it", f"[Open the event manager]({url})", inline=False)

    else:
        # Unknown event type — generic card so nothing crashes.
        spec["description"] = f"[View the event]({url})" if url else None

    return spec
