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
    # Loot Sweep (loot_sweep kind): three independently-toggleable verbosity
    # levels — an individual scoring receipt, a subset (group) bonus completing,
    # and the whole-set bonus completing. All post to the completions channel
    # (falling back to announcements), each enriched with the item/points detail.
    "event_sweep_item": "completions",
    "event_sweep_group": "completions",
    "event_sweep_set": "completions",
    "event_lead_change": "leaderboard",
    "event_pending": "admin",
    # Task 21: the scheduler sweep could not activate a scheduled draft.
    "event_activation_failed": "admin",
    # P0-8: an event's scheduled end failed, or ended with incomplete wrap-up
    # (standings/announcement/Discord retirement) — an admin should look.
    "event_end_failed": "admin",
    # G7: players left off the whole-clan teams at activation because they
    # belong to more than one participating clan (need a manual team add).
    "event_multi_clan_skipped": "admin",
    # Interactive "Sign up" prompt (posted on demand by an admin) — carries a
    # button the notification sender attaches (services/notification_service).
    "event_signup_prompt": "announcements",
    # Prize pot (web52a): the manual "advertise the pot now" post (admin action,
    # no toggle — always sends). Renders a plain text pot line via its layout.
    "event_pot": "announcements",
    # Partial task progress (opt-in via message_config.task_progress —
    # 'milestones' or 'all'; default off).
    "event_task_progress": "completions",
    # Board game (web44a): a team's dice roll + move + next task.
    "event_board_turn": "completions",
    # Board game (web53a): "your task is done — roll the dice". Team-channel
    # first: the main-channel toggle defaults OFF (a whole-event channel does
    # not need every team's roll nag), the per-team channel default is ON
    # (services/event_team_discord.DEFAULT_TEAM_MESSAGE_TOGGLES).
    "event_board_roll_prompt": "completions",
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
    "event_sweep_item": 0x3498DB,   # informational blue — a drop scored
    "event_sweep_group": 0x1ABC9C,  # teal — a subset (boss) completed
    "event_sweep_set": 0x00FF00,    # green victory — the whole set completed
    "event_lead_change": 0xFFD700,
    "event_pending": 0xE67E22,
    "event_activation_failed": 0xED4245,
    "event_end_failed": 0xED4245,
    "event_multi_clan_skipped": 0xFAA61A,  # amber — an admin needs to act
    "event_signup_prompt": 0x5865F2,  # Discord blurple — a call to action
    "event_pot": 0xFFD700,  # gold — the prize pot
    "event_task_progress": 0x3498DB,  # informational blue — progress, not victory
    "event_board_turn": 0xF1C40F,  # dice gold — movement on the board
    "event_board_roll_prompt": 0xF1C40F,  # same dice family — a nudge to roll
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
    "event_end_failed": True,
    "event_board_turn": True,
    "event_board_roll_prompt": False,  # team-channel first; opt-in for main channels
    # Loot Sweep verbosity: subset + whole-set completions announce by default;
    # per-item receipts are opt-in (a game-wide sweep would flood the channel).
    "event_sweep_item": False,
    "event_sweep_group": True,
    "event_sweep_set": True,
}

# Loot Sweep messaging sub-config defaults (message_config["loot_sweep"]).
# ``item_min_points`` only posts an ``event_sweep_item`` when the receipt scored
# at least this many points — a way to enable item posts but keep them to the
# notable drops. 0 = every scoring receipt (when the toggle is on).
DEFAULT_LOOT_SWEEP_CONFIG = {"item_min_points": 0}

DEFAULT_MESSAGE_CONFIG = {
    "toggles": dict(DEFAULT_MESSAGE_TOGGLES),
    "task_progress": "off",
    # Verbose completion detail: the item that finished the task, how much of
    # the requirement it filled, and the requirement target — rendered on the
    # event_completion message on top of the always-present contributor list.
    # Default on (additive detail); a group can silence it per event.
    "item_details": True,
    "loot_sweep": dict(DEFAULT_LOOT_SWEEP_CONFIG),
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
        "item_details": DEFAULT_MESSAGE_CONFIG["item_details"],
        "loot_sweep": dict(DEFAULT_LOOT_SWEEP_CONFIG),
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

    if "item_details" in data:
        config["item_details"] = bool(data["item_details"])

    sweep = data.get("loot_sweep")
    if isinstance(sweep, dict):
        try:
            mp = int(sweep.get("item_min_points"))
        except (TypeError, ValueError):
            mp = None
        if mp is not None:
            config["loot_sweep"]["item_min_points"] = max(0, mp)

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


def _received_item_text(data: dict) -> Optional[str]:
    """The verbose "what finished the task" line for an ``event_completion``
    message: ``**3× Dragon bones** (+3 of 100)`` — the item that completed the
    task, its drop quantity, and how much of the requirement that drop filled
    (the progress delta) against the requirement target.

    ``points_based`` payloads (point_collection tasks) store point credits in
    the ledger quantities, not item counts — the multiplier would read as "2×
    Bandos chestplate" when the item was one chestplate worth 2 points. Those
    render the bare item name with the points it earned:
    ``**Bandos chestplate** (+2 of 10 pts)``.

    Returns None when the enriching fields aren't present — non-item
    completions carry no ``received_item``, and the enrichment is omitted at
    enqueue time when the event's ``item_details`` config is off — so callers
    can drop the line entirely. Shared by the legacy embed and the V2 layout
    token so both render identically."""
    item = (data or {}).get("received_item")
    if not item:
        return None
    points_based = bool(data.get("points_based"))
    qty = int(data.get("received_qty") or 0)
    label = (f"**{qty}× {item}**" if qty and qty != 1 and not points_based
             else f"**{item}**")
    unit = " pts" if points_based else ""
    contributed = data.get("contributed")
    target = data.get("target")
    if contributed and target:
        return f"{label} (+{format_gp(contributed)} of {format_gp(target)}{unit})"
    if contributed:
        return f"{label} (+{format_gp(contributed)}{unit})"
    return label


def fmt_pts(value) -> str:
    """Loot Sweep points are decimal-valued (a 1-pointer's 2nd receipt is 0.8),
    so render them tersely: ``9``, ``10.4`` — trailing ``.0`` dropped."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def sweep_contributor_lines(contributors, limit: int = 5) -> Optional[str]:
    """Ranked contributor list for a Loot Sweep group/set completion (points-
    based ledger). Medals for the top three; a compact comma list past five.
    None when there is nobody to show."""
    contributors = contributors or []
    if not contributors:
        return None

    def one(c):
        return f"`{c.get('player_name') or 'Unknown'}` ({fmt_pts(c.get('quantity') or 0)} pts)"

    if len(contributors) <= limit:
        rows = [f"{_MEDALS[i] if i < len(_MEDALS) else '•'} {one(c)}"
                for i, c in enumerate(contributors)]
        return "\n".join(rows)
    return ", ".join(one(c) for c in contributors)


def _sweep_embed(notification_type, spec, field, data, team, player) -> None:
    """Fill a loot_sweep receipt / subset / whole-set completion embed spec.
    Shared by the legacy embed path (here) — the V2 layout renders the same
    detail from :func:`services.event_message_layouts.notification_context`."""
    task_label = data.get("task_label")
    who = f"**{team}**" if team else "A team"
    item = data.get("received_item")
    qty = int(data.get("received_qty") or 0)
    item_label = f"**{qty}× {item}**" if item and qty and qty != 1 else f"**{item}**"

    if notification_type == "event_sweep_item":
        spec["title"] = f"\U0001F4E5 {team or 'A team'} received {item or 'a drop'}"
        npc = data.get("npc")
        at = f" from **{npc}**" if npc else ""
        spec["description"] = (f"{who} received {item_label}{at} "
                               f"— `+{fmt_pts(data.get('received_points'))} pts`")
        scored, mx = data.get("item_scored"), data.get("item_max")
        if scored is not None and mx:
            nxt = data.get("next_receipt_points")
            tail = (f" • next `+{fmt_pts(nxt)}`"
                    if nxt not in (None, 0, 0.0) else " • fully farmed")
            field("Copies scored", f"`{int(scored)}/{int(mx)}`{tail}", inline=False)
        gl, gh, gn = data.get("group_label"), data.get("group_have"), data.get("group_need")
        if gl and gh is not None and gn:
            field(gl, f"`{int(gh)}/{int(gn)} items` collected")
        if data.get("player_name"):
            field("From", f"`{data['player_name']}`")
        if data.get("progress") is not None:
            field("Set total", f"`{fmt_pts(data['progress'])} pts`")

    elif notification_type == "event_sweep_group":
        spec["title"] = f"✅ {team or 'A team'} completed {data.get('group_label') or 'a boss set'}!"
        n = int(data.get("group_completion_n") or 1)
        again = f" (×{n})" if n > 1 else ""
        spec["description"] = (f"{who} collected every item in "
                               f"**{data.get('group_label') or task_label or 'the set'}**{again}")
        if data.get("bonus_points"):
            field("Set bonus", f"`+{fmt_pts(data['bonus_points'])} pts`")

    else:  # event_sweep_set
        spec["title"] = f"\U0001F3C6 {team or 'A team'} swept {task_label or 'the set'}!"
        n = int(data.get("set_completion_n") or 1)
        again = f" (×{n})" if n > 1 else ""
        spec["description"] = (f"{who} completed **every** boss in "
                               f"**{task_label or 'the set'}**{again} — full sweep!")
        if data.get("bonus_points"):
            field("Full-set bonus", f"`+{fmt_pts(data['bonus_points'])} pts`")

    # Shared tail: contributors (group/set), running team total, standing, icon.
    if notification_type in ("event_sweep_group", "event_sweep_set"):
        contribs = sweep_contributor_lines(data.get("contributors"))
        if contribs:
            field("Contributors", contribs, inline=False)
    if data.get("team_score") is not None:
        field("Team total", f"`{fmt_pts(data['team_score'])} pts`")
    rank, tcount = data.get("team_rank"), data.get("team_count")
    if rank and tcount:
        field("Team position", f"`#{int(rank)}/{int(tcount)} teams`")
    thumb = data.get("completion_icon") or data.get("proof_url")
    if thumb:
        spec["thumbnail"] = thumb


def line_label(note) -> str:
    """Human label for one bonus-line note (``line:r0`` → ``Row 1``,
    ``line:c2`` → ``Column 3``, ``line:d0``/``line:d1`` → the diagonals,
    ``blackout`` → ``Blackout``). Falls back to the raw note so an unknown
    shape still renders something."""
    key = str(note or "").strip()
    if key.startswith("line:"):
        key = key[5:]
    if len(key) >= 2 and key[0] in ("r", "c") and key[1:].isdigit():
        kind = "Row" if key[0] == "r" else "Column"
        return f"{kind} {int(key[1:]) + 1}"
    if key == "d0":
        return "Diagonal ↘"
    if key == "d1":
        return "Diagonal ↙"
    if key == "blackout":
        return "Blackout"
    return str(note or "").strip()


def line_bonus_lines(data: dict) -> list:
    """The bonus-line notes carried by one ``event_line`` payload — the
    coalesced ``lines`` list when present, else the legacy single ``line``.
    [] when the payload predates line identification entirely."""
    lines = (data or {}).get("lines")
    if isinstance(lines, list) and lines:
        return [str(n) for n in lines if n]
    single = (data or {}).get("line")
    return [str(single)] if single else []


def line_bonus_summary(data: dict) -> str:
    """The "completed …" clause of a line-bonus message, naming the line(s):
    ``completed a full line — **Row 2**`` or ``completed **3** full lines at
    once — **Row 1**, **Column 4**, **Diagonal ↘**``. Payloads without line
    identity keep the old wording so nothing renders blank."""
    labels = [line_label(n) for n in line_bonus_lines(data)]
    if not labels:
        return "completed a full line"
    if len(labels) == 1:
        return f"completed a full line — **{labels[0]}**"
    joined = ", ".join(f"**{label}**" for label in labels)
    return f"completed **{len(labels)}** full lines at once — {joined}"


def _completion_item_redundant(data: dict) -> bool:
    """True when a completion's received item should NOT get its own "Finished
    with" line — the task is essentially named after that single item, so the
    "{team} completed {task}" title already says it. Only fires for a
    single-item requirement (target <= 1) whose label matches the item name
    (ignoring surrounding markdown/count decoration like ``##`` or ``**``)."""
    item = (data or {}).get("received_item")
    if not item:
        return False
    try:
        target = int(data.get("target") or 0)
    except (TypeError, ValueError):
        target = 0
    if target > 1:
        return False

    def _n(s):
        return " ".join(str(s or "").strip().lower().split()).strip("#*• ").strip()

    ni, nl = _n(item), _n(data.get("task_label"))
    return bool(ni) and bool(nl) and (ni == nl or ni in nl or nl in ni)


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


def load_event_channels(session, event_id: int, group_id=None) -> dict:
    """{kind: channel_id} for one event (lazy db import — see module docstring).

    ``group_id`` selects a clan's own per-group channel set (web48a,
    Event.per_group_discord); None returns the shared/host rows (the only
    shape before web48a). Rows are partitioned — a group NEVER inherits
    individual kinds from the shared set here; fallback to the whole shared
    set is the caller's call (see notification_service per-group fan-out)."""
    from db.models import EventChannel

    query = session.query(EventChannel).filter(EventChannel.event_id == event_id)
    if group_id is None:
        query = query.filter(EventChannel.group_id.is_(None))
    else:
        query = query.filter(EventChannel.group_id == group_id)
    return {r.kind: str(r.channel_id) for r in query.all() if r.channel_id}


def per_group_discord_enabled(event) -> bool:
    """Whether this event fans notifications out per participating clan
    (clan_vs_clan + the per_group_discord flag)."""
    return bool(getattr(event, "per_group_discord", False)) and (
        (getattr(event, "mode", None) or "standard") == "clan_vs_clan")


def load_group_destinations(session, event) -> list:
    """Per-clan send destinations for a per-group-discord event: one entry per
    ACCEPTED participating clan — ``{"group_id", "channels", "message_config"}``
    where ``channels`` falls back to the event's shared rows when the clan
    hasn't configured its own, and ``message_config`` is the clan's effective
    verbosity (its own override or the event's). The caller dedupes by
    resolved channel id so two clans pointing at the same channel (e.g. both
    falling back to shared) never double-post."""
    from db.models import EventGroup

    shared = load_event_channels(session, event.id)
    groups = (session.query(EventGroup)
              .filter(EventGroup.event_id == event.id,
                      EventGroup.status == "accepted")
              .order_by(EventGroup.id.asc())
              .all())
    destinations = []
    for g in groups:
        own = load_event_channels(session, event.id, group_id=g.group_id)
        destinations.append({
            "group_id": g.group_id,
            "channels": own or shared,
            "own_channels": bool(own),
            "message_config": effective_message_config(
                g.message_config if g.message_config
                else getattr(event, "message_config", None)),
        })
    return destinations


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
        # The item that finished the task + how much of the requirement it
        # filled (item_details config, on by default). Skipped when the task is
        # named after that one item (title already says it), for non-item
        # completions, and when the toggle is off.
        if not _completion_item_redundant(data):
            received = _received_item_text(data)
            if received:
                field("Received", received, inline=False)
        # Who did it: a single person collapses to "Completed by"; several get
        # the full "Contributors" breakdown. Falls back to the completer when
        # the ledger lookup came up empty (e.g. a manually-awarded row).
        contributors = data.get("contributors") or []
        if len(contributors) > 1:
            # point_collection ledgers count points, not items — label them.
            unit = " pts" if data.get("points_based") else ""

            def _contrib(c):
                line = (f"`{c.get('player_name') or 'Unknown'}` "
                        f"({format_gp(c.get('quantity') or 0)}{unit})")
                share = c.get("points_share")  # task points × net share
                if share:
                    line += f" +{share:g} pts"
                return line
            if len(contributors) <= 5:
                # One contributor per line, ranked by contribution (medals for
                # the top three) so the breakdown reads like a table.
                medals = ("\U0001F947", "\U0001F948", "\U0001F949")  # 🥇 🥈 🥉
                rows = [f"{medals[i] if i < len(medals) else '•'} {_contrib(c)}"
                        for i, c in enumerate(contributors)]
                field("Contributors", "\n".join(rows), inline=False)
            else:
                # A big pile of contributors reads better as a compact list
                # than a six-plus-row table.
                field("Contributors", ", ".join(_contrib(c) for c in contributors),
                      inline=False)
        else:
            solo = (contributors[0].get("player_name") if contributors else None) or player
            if solo:
                field("Completed by", f"`{solo}`")
        # Team standing. Bingo events summarize the board (tiles done / total
        # points / position); every other kind shows the running team total.
        tiles = data.get("tiles_completed")
        rank, tcount = data.get("team_rank"), data.get("team_count")
        if tiles is not None or rank is not None:
            if tiles is not None:
                field("Total tiles completed", f"`{int(tiles)}`")
            if data.get("team_score") is not None:
                field("Total points earned", f"`{int(data['team_score'])} pts`")
            if rank and tcount:
                field("Team position", f"`#{int(rank)}/{int(tcount)} teams`")
        elif data.get("team_score") is not None:
            field("Team total", f"`{int(data['team_score'])} pts`")
        # Item icon (resolved by the sender), else the proof screenshot.
        thumb = data.get("completion_icon") or data.get("proof_url")
        if thumb:
            spec["thumbnail"] = thumb

    elif notification_type in ("event_sweep_item", "event_sweep_group",
                               "event_sweep_set"):
        _sweep_embed(notification_type, spec, field, data, team, player)

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

    elif notification_type == "event_board_roll_prompt":
        spec["title"] = f"\U0001F3B2 {team or 'Your team'} can roll!"
        spec["description"] = (
            f"**{team or 'Your team'}** finished **{task_label or 'its task'}**"
            f"{f' (thanks {player})' if player else ''} — time to roll the dice "
            f"and move on."
        )
        if data.get("coins_awarded"):
            field("Coins", f"`+{int(data['coins_awarded'])}`")
        if data.get("coin_balance") is not None:
            field("Wallet", f"`{int(data['coin_balance'])} coins`")

    elif notification_type == "event_line":
        spec["title"] = "\U0001F4CF Line bonus!"
        spec["description"] = f"**{team or 'A team'}** {line_bonus_summary(data)}"
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
        # Loot Sweep names the drop (+ points) that took the lead.
        item = data.get("received_item")
        if item:
            pts = data.get("points")
            spec["description"] += (f"\n-# with **{item}**"
                                    + (f" (+{fmt_pts(pts)} pts)" if pts else ""))
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

    elif notification_type == "event_end_failed":
        spec["title"] = f"⚠️ {event_name} needs attention at the finish"
        reason = data.get("reason") or "The end-of-event wrap-up did not complete."
        spec["description"] = (
            f"Something went wrong while ending the event: {reason}\n"
            f"Check the final standings and announcements — some may need "
            f"to be posted by hand."
        )
        ends = _fmt_ts(data.get("ends_at"))
        if ends:
            field("Scheduled end", ends)
        if url:
            field("Review it", f"[Open the event manager]({url})", inline=False)

    elif notification_type == "event_multi_clan_skipped":
        count = data.get("skipped_count") or 0
        who = data.get("skipped_players") or f"{count} player(s)"
        spec["title"] = f"⚠️ {count} player(s) need a team"
        spec["description"] = (
            f"These players are in more than one clan competing in "
            f"**{event_name}**, so they weren't auto-added to a whole-clan "
            f"team. Add each to a specific team to include them:\n{who}"
        )
        if url:
            field("Manage teams", f"[Open the event manager]({url})", inline=False)

    else:
        # Unknown event type — generic card so nothing crashes.
        spec["description"] = f"[View the event]({url})" if url else None

    return spec
