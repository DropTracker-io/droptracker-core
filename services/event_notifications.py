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
    "event_line": "completions",
    "event_blackout": "completions",
    "event_lead_change": "leaderboard",
    "event_pending": "admin",
    # Task 21: the scheduler sweep could not activate a scheduled draft.
    "event_activation_failed": "admin",
    # Interactive "Sign up" prompt (posted on demand by an admin) — carries a
    # button the notification sender attaches (services/notification_service).
    "event_signup_prompt": "announcements",
    # Partial task progress (opt-in via message_config.task_progress —
    # 'milestones' or 'all'; default off).
    "event_task_progress": "completions",
    # Board game (web44a): a team's dice roll + move + next task.
    "event_board_turn": "completions",
}

EVENT_NOTIFICATION_TYPES = tuple(KIND_FOR_TYPE)

_MEDALS = ("\U0001F947", "\U0001F948", "\U0001F949")  # gold / silver / bronze

# Embed accent colors, keeping the drop-embed visual language (0x00ff00 green
# for "something good happened"; distinct hues for the other families).
_COLORS = {
    "event_started": 0x00FF00,
    "event_ended": 0xFFD700,
    "event_completion": 0x00FF00,
    "event_line": 0x9B59B6,
    "event_blackout": 0x2C2F33,
    "event_lead_change": 0xFFD700,
    "event_pending": 0xE67E22,
    "event_activation_failed": 0xED4245,
    "event_signup_prompt": 0x5865F2,  # Discord blurple — a call to action
    "event_task_progress": 0x3498DB,  # informational blue — progress, not victory
    "event_board_turn": 0xF1C40F,  # dice gold — movement on the board
}


# --------------------------------------------------------------------------- #
# Per-event messaging verbosity (web_events.message_config)
# --------------------------------------------------------------------------- #
# Mirrors db.models.events.EVENT_TASK_PROGRESS_MODES / EVENT_MESSAGE_TOGGLE_KEYS
# (kept literal here so this module stays stdlib-only for the unit tests).
TASK_PROGRESS_MODES = ("off", "milestones", "all")

# The percent thresholds 'milestones' mode announces when a team crosses them.
PROGRESS_MILESTONES = (25, 50, 75)

# Queue types a group leader can toggle per event, with their defaults. The
# defaults reproduce today's behaviour exactly (everything that used to post
# still posts; task progress is the one new, default-off type).
DEFAULT_MESSAGE_TOGGLES = {
    "event_started": True,
    "event_ended": True,
    "event_completion": True,
    "event_task_progress": True,  # gated separately by task_progress mode
    "event_line": True,
    "event_blackout": True,
    "event_lead_change": True,
    "event_pending": True,
    "event_activation_failed": True,
    "event_board_turn": True,
}

DEFAULT_MESSAGE_CONFIG = {
    "toggles": dict(DEFAULT_MESSAGE_TOGGLES),
    "task_progress": "off",
    "leaderboard": {"live": True, "top_n": 10, "show_tasks": True},
}

LEADERBOARD_TOP_N_RANGE = (3, 25)


def effective_message_config(raw_json) -> dict:
    """The full messaging config for one event: defaults overlaid with the
    stored ``web_events.message_config`` JSON. Unknown keys and corrupt JSON
    are ignored (a bad config must never break a send) — callers always get
    every key of :data:`DEFAULT_MESSAGE_CONFIG` back, values normalized."""
    import json

    config = {
        "toggles": dict(DEFAULT_MESSAGE_TOGGLES),
        "task_progress": DEFAULT_MESSAGE_CONFIG["task_progress"],
        "leaderboard": dict(DEFAULT_MESSAGE_CONFIG["leaderboard"]),
    }
    if not raw_json:
        return config
    if isinstance(raw_json, dict):
        data = raw_json
    else:
        try:
            data = json.loads(raw_json)
        except (ValueError, TypeError):
            return config
    if not isinstance(data, dict):
        return config

    toggles = data.get("toggles")
    if isinstance(toggles, dict):
        for key, value in toggles.items():
            if key in DEFAULT_MESSAGE_TOGGLES:
                config["toggles"][key] = bool(value)

    mode = data.get("task_progress")
    if mode in TASK_PROGRESS_MODES:
        config["task_progress"] = mode

    board = data.get("leaderboard")
    if isinstance(board, dict):
        if "live" in board:
            config["leaderboard"]["live"] = bool(board["live"])
        if "show_tasks" in board:
            config["leaderboard"]["show_tasks"] = bool(board["show_tasks"])
        try:
            top_n = int(board.get("top_n"))
        except (TypeError, ValueError):
            top_n = None
        if top_n is not None:
            lo, hi = LEADERBOARD_TOP_N_RANGE
            config["leaderboard"]["top_n"] = max(lo, min(hi, top_n))

    return config


def should_send_event_message(message_config: dict, notification_type: str) -> bool:
    """Whether one queue type is enabled by an event's (effective) messaging
    config. Types without a toggle (event_signup_prompt — an explicit admin
    action) always send. ``event_task_progress`` additionally requires the
    task_progress mode to be on."""
    toggles = (message_config or {}).get("toggles") or {}
    if notification_type not in DEFAULT_MESSAGE_TOGGLES:
        return True
    if not toggles.get(notification_type, DEFAULT_MESSAGE_TOGGLES[notification_type]):
        return False
    if notification_type == "event_task_progress":
        return (message_config or {}).get("task_progress", "off") != "off"
    return True


def progress_milestones_crossed(previous: int, current: int, target: int) -> list:
    """The :data:`PROGRESS_MILESTONES` percentages newly crossed when a team's
    task progress moves ``previous`` -> ``current`` toward ``target``.
    Empty when the target is unset/reached-before or nothing was crossed;
    100% is excluded (that's the completion notification's job)."""
    try:
        previous, current, target = int(previous or 0), int(current or 0), int(target or 0)
    except (TypeError, ValueError):
        return []
    if target <= 0 or current <= previous:
        return []
    crossed = []
    for pct in PROGRESS_MILESTONES:
        threshold = target * pct / 100.0
        if previous < threshold <= current < target:
            crossed.append(pct)
    return crossed


def format_gp(value) -> str:
    """Human-friendly quantity for a task's progress/target/contribution:
    plain comma-grouped below 100,000 (item counts like "3 / 5" stay exact),
    abbreviated K/M/B above that ("10000000" -> "10.00M") — matches the
    site's ``formatGp``/backend's ``format_number`` abbreviation exactly, just
    gated to only kick in once a value is large enough to need it."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value)
    abs_value = abs(value)
    if abs_value <= 100_000:
        return f"{value:,}"
    sign = "-" if value < 0 else ""
    if abs_value >= 1_000_000_000:
        return f"{sign}{abs_value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1_000_000:.2f}M"
    return f"{sign}{abs_value / 1_000:.2f}K"


def event_url(event_id) -> str:
    return f"{EVENT_BASE_URL}/{event_id}"


def event_footer_line(event_name, starts_at=None, ends_at=None) -> Optional[str]:
    """The universal ``-# {name} - Starts: <t:R> - Ends: <t:R>`` footer line
    appended to every event Discord message so a reader can tie the message
    back to its event at a glance.

    ``starts_at`` / ``ends_at`` are unix seconds (the ``_ts`` convention:
    ``int(dt.timestamp())``); each half is dropped when its timestamp is
    missing, and the whole line is ``None`` when there is no event name to
    anchor it. Rendered as ``-#`` subtext so it reads like a footer while the
    ``<t:…:R>`` tokens still resolve to relative times in a Discord message.
    """
    if not event_name:
        return None
    parts = [str(event_name)]
    for label, ts in (("Starts", starts_at), ("Ends", ends_at)):
        try:
            if ts:
                parts.append(f"{label}: <t:{int(ts)}:R>")
        except (TypeError, ValueError):
            continue
    return "-# " + " - ".join(parts)


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
        spec["description"] = f"{who} completed **{task_label or 'a task'}**"
        if points:
            field("Points", f"`+{points}`")
        if data.get("team_score") is not None:
            field("Team total", f"`{int(data['team_score'])} pts`")
        # Everyone who contributed, not just whoever's submission completed
        # it — falls back to the single completer when the ledger lookup
        # came up empty (e.g. a manually-awarded row).
        contributors = data.get("contributors") or []
        if contributors:
            def _contrib(c):
                line = (f"`{c.get('player_name') or 'Unknown'}` "
                        f"({format_gp(c.get('quantity') or 0)})")
                share = c.get("points_share")  # task points × net share
                if share:
                    line += f" +{share:g} pts"
                return line
            names = ", ".join(_contrib(c) for c in contributors)
            field("Contributors", names, inline=False)
        elif player:
            field("Completed by", f"`{player}`")
        cell_labels = data.get("cell_labels") or []
        cells = data.get("cell_idxs") or []
        if cell_labels:
            plural = "s" if len(cell_labels) != 1 else ""
            field("Bingo", f"Tile{plural} " + ", ".join(f"**{c}**" for c in cell_labels), inline=False)
        elif cells:
            field("Bingo", f"Cell{'s' if len(cells) != 1 else ''} `{', '.join(str(c) for c in cells)}` marked")
        # Proof screenshot if there is one, else the task's item/NPC icon.
        thumb = data.get("completion_icon") or data.get("proof_url")
        if thumb:
            spec["thumbnail"] = thumb

    elif notification_type == "event_task_progress":
        spec["title"] = f"\U0001F4C8 Progress: {task_label or 'Task'}"
        who = f"**{team}**" if team else "A team"
        by = f" (`{player}`)" if player else ""
        current = int(data.get("progress") or 0)
        target = int(data.get("target") or 0)
        milestone = data.get("milestone_pct")
        if milestone:
            spec["description"] = (
                f"{who} passed **{int(milestone)}%** of **{task_label or 'a task'}**{by}"
            )
        else:
            spec["description"] = f"{who} progressed **{task_label or 'a task'}**{by}"
        if target:
            spec["fields"].append(
                {"name": "Progress", "value": f"`{format_gp(current)} / {format_gp(target)}`", "inline": True}
            )
        if data.get("task_icon"):
            spec["thumbnail"] = data["task_icon"]

    elif notification_type == "event_board_turn":
        dice = data.get("dice") or []
        dice_str = " + ".join(str(d) for d in dice) if dice else "?"
        total = sum(int(d) for d in dice) if dice else 0
        if data.get("won"):
            spec["title"] = f"\U0001F3C6 {team or 'A team'} reached the finish!"
            spec["description"] = (
                f"**{team or 'A team'}** rolled `{dice_str}` and crossed the "
                f"finish line!"
            )
        else:
            spec["title"] = f"\U0001F3B2 {team or 'A team'} rolled the dice"
            spec["description"] = (
                f"**{team or 'A team'}** rolled `{dice_str}`"
                + (f" (**{total}**)" if len(dice) > 1 else "")
                + f" — tile `{data.get('tile_from')}` → `{data.get('tile_to')}`"
            )
            nxt = data.get("next_task_label")
            if nxt:
                field("Next task", f"**{nxt}**", inline=False)
        if data.get("coins_awarded"):
            field("Coins", f"`+{int(data['coins_awarded'])}`")
        if data.get("coin_balance") is not None:
            field("Wallet", f"`{int(data['coin_balance'])} coins`")
        if data.get("turn") is not None:
            field("Turn", f"`#{int(data['turn'])}`")

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
