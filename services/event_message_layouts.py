"""Components-V2 layout templates for event Discord messages.

The component analogue of the custom-embed system (db/models/embed.py +
db/ops.get_group_embed): every event message type has a layout — a small
block DSL stored as JSON in ``web_event_message_layouts`` — resolved
group row -> template group 1 row -> code default, with ``{placeholder}``
substitution at render time. The DSL keeps the future web layout editor
honest: it is data, not code, so the editor can round-trip it.

Block DSL (``layout`` JSON: ``{"accent_color": "#RRGGBB"?, "blocks": [...]}``):

- ``{"type": "text", "content": "..."}``
    -> ``TextDisplayComponent``. Content is markdown; ``{tokens}`` are
    substituted per line and any line still holding an unresolved token is
    dropped (the component version of replace_placeholders() dropping embed
    fields). A block whose content ends up empty is dropped.
- ``{"type": "section", "content": "...", "thumbnail": "{proof_url}"}``
    -> ``SectionComponent`` with a ``ThumbnailComponent`` accessory; falls
    back to a plain text block when the thumbnail token doesn't resolve.
- ``{"type": "separator"}``
    -> ``SeparatorComponent(divider=True)``.
- ``{"type": "standings", "limit": 5, "title": "**Standings**"}``
    -> medal-list text block built from the standings passed by the sender
    (never from tokens — it's the one block with structured input).
- ``{"type": "buttons", "buttons": [{"label": "...", "url": "..."}]}``
    -> ``ActionRow`` of URL buttons; a button whose url doesn't resolve is
    dropped, an empty row is dropped.

Module-level imports are stdlib-only (same deal as event_notifications.py)
so the unit tests can load it directly; interactions.py and db models are
lazy-imported inside the functions that need them.
"""
from __future__ import annotations

import json
import re
from typing import Optional

# Gold / silver / bronze — kept in sync with event_notifications._MEDALS
# (defined locally so this module stays stdlib-only at import time for the
# direct-file-loading unit tests).
_MEDALS = ("\U0001F947", "\U0001F948", "\U0001F949")

_TOKEN_RE = re.compile(r"\{[a-z_]+\}")

# Template group whose rows are the system defaults (== embeds' TEMPLATE_GROUP_ID).
TEMPLATE_GROUP_ID = 1

LAYOUT_SCHEMA_VERSION = 1


def _hex_to_int(color) -> Optional[int]:
    if not color or not isinstance(color, str):
        return None
    value = color.lstrip("#")
    if len(value) != 6:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def text_progress_bar(current, target, width: int = 10) -> str:
    """``▰▰▰▱▱▱▱▱▱▱`` — the classic text meter, clamped to [0, width]."""
    try:
        current, target = int(current or 0), int(target or 0)
    except (TypeError, ValueError):
        return ""
    if target <= 0:
        return ""
    filled = max(0, min(width, round(width * current / target)))
    return "▰" * filled + "▱" * (width - filled)


def standings_lines(standings, limit: int) -> str:
    """Medal list for a standings block — shared by every layout that shows
    scores (same shape event_embed_spec used, kept here so both paths agree)."""
    lines = []
    for i, team in enumerate((standings or [])[:limit]):
        medal = _MEDALS[i] if i < len(_MEDALS) else f"`#{i + 1}`"
        lines.append(f"{medal} **{team.get('name')}** — `{int(team.get('score') or 0)} pts`")
    return "\n".join(lines) if lines else "No teams yet."


# --------------------------------------------------------------------------- #
# Default layouts (code fallback + the seed source for group 1)
# --------------------------------------------------------------------------- #
_EVENT_BUTTON = {"type": "buttons", "buttons": [{"label": "View event", "url": "{event_url}"}]}

DEFAULT_LAYOUTS = {
    "event_started": {
        "accent_color": "#57F287",
        "blocks": [
            {"type": "text", "content": "## \U0001F3C1 {event_name} has started!"},
            {"type": "separator"},
            {"type": "text", "content": "{description}"},
            {
                "type": "text",
                "content": "**Started** {starts_at}\n**Ends** {ends_at}\n**Teams** `{team_count}`",
            },
            {"type": "separator"},
            {
                "type": "buttons",
                "buttons": [{"label": "Follow the event live", "url": "{event_url}"}],
            },
        ],
    },
    "event_ended": {
        "accent_color": "#FFD700",
        "blocks": [
            {"type": "text", "content": "## \U0001F3C6 {event_name} has ended!"},
            {"type": "separator"},
            {"type": "standings", "limit": 5, "title": "**Final standings**"},
            {"type": "separator"},
            {"type": "buttons", "buttons": [{"label": "Full results", "url": "{event_url}"}]},
        ],
    },
    "event_completion": {
        "accent_color": "#57F287",
        "blocks": [
            {"type": "text", "content": "### ✅ {team_name} completed **{task_label}**"},
            {
                "type": "section",
                "content": "**Points** `+{points}`\n**Team total** `{team_score} pts`\n-# by {player_name}",
                "thumbnail": "{proof_url}",
            },
            {"type": "text", "content": "-# Bingo: cell{cell_plural} {cell_list} marked"},
        ],
    },
    "event_task_progress": {
        "accent_color": "#3498DB",
        "blocks": [
            {"type": "text", "content": "### \U0001F4C8 {team_name} — {task_label}"},
            {"type": "text", "content": "-# Passed **{milestone_pct}%**"},
            {
                "type": "text",
                "content": "{progress_bar} `{progress} / {target}`\n-# by {player_name}",
            },
        ],
    },
    "event_cell": {
        "accent_color": "#9B59B6",
        "blocks": [
            {"type": "text", "content": "### \U0001F3AF {team_name} marked **{cell_label}**"},
            {"type": "text", "content": "**Points** `+{points}`"},
        ],
    },
    "event_line": {
        "accent_color": "#9B59B6",
        "blocks": [
            {"type": "text", "content": "### \U0001F4CF Line bonus! **{team_name}** completed a full line"},
            {"type": "text", "content": "**Bonus** `+{bonus_points} pts`"},
        ],
    },
    "event_blackout": {
        "accent_color": "#2C2F33",
        "blocks": [
            {"type": "text", "content": "## \U0001F311 BLACKOUT!"},
            {"type": "text", "content": "**{team_name}** completed the entire board"},
            {"type": "text", "content": "**Bonus** `+{bonus_points} pts`"},
        ],
    },
    "event_lead_change": {
        "accent_color": "#FFD700",
        "blocks": [
            {"type": "text", "content": "## \U0001F451 New leader: {team_name}"},
            {"type": "text", "content": "-# after **{task_label}**"},
            {"type": "separator"},
            {"type": "standings", "limit": 3, "title": "**Standings**"},
        ],
    },
    "event_pending": {
        "accent_color": "#E67E22",
        "blocks": [
            {"type": "text", "content": "### \U0001F50D Completion awaiting review"},
            {
                "type": "section",
                "content": "**{task_label}** needs an admin's confirmation.\n**Player** `{player_name}`\n**Team** {team_name}",
                "thumbnail": "{proof_url}",
            },
            {
                "type": "buttons",
                "buttons": [{"label": "Open the review queue", "url": "{review_url}"}],
            },
        ],
    },
    "event_activation_failed": {
        "accent_color": "#ED4245",
        "blocks": [
            {"type": "text", "content": "## ⚠️ {event_name} could not start"},
            {
                "type": "text",
                "content": "The scheduled start passed, but the event could not be activated: {reason}",
            },
            {"type": "text", "content": "**Scheduled start** {starts_at}"},
            {
                "type": "buttons",
                "buttons": [{"label": "Open the event manager", "url": "{event_url}"}],
            },
        ],
    },
    "event_signup_prompt": {
        "accent_color": "#5865F2",
        "blocks": [
            {"type": "text", "content": "## \U0001F4E3 Sign up for {event_name}"},
            {"type": "text", "content": "{description}"},
            {
                "type": "text",
                "content": "{signup_instructions}\n-# One account per person. Not linked yet? Sign in at droptracker.io first.",
            },
            {"type": "text", "content": "**Sign-ups close** {ends_at}"},
        ],
    },
    "event_board": {
        "accent_color": "#FFD700",
        "blocks": [
            {"type": "text", "content": "## \U0001F3C6 {event_name}"},
            {"type": "text", "content": "-# {board_status_line}"},
            {"type": "separator"},
            {"type": "standings", "limit": 10, "title": "**Standings**"},
            {"type": "separator"},
            {"type": "text", "content": "{tasks_summary}"},
            {"type": "text", "content": "-# Updated {updated_ts} • refreshes automatically"},
            {"type": "buttons", "buttons": [{"label": "Full standings", "url": "{event_url}"}]},
        ],
    },
}


# --------------------------------------------------------------------------- #
# Loader (group row -> group 1 row -> code default)
# --------------------------------------------------------------------------- #
def _parse_layout_row(row) -> Optional[dict]:
    try:
        layout = json.loads(row.layout)
    except (ValueError, TypeError):
        return None
    if not isinstance(layout, dict) or not isinstance(layout.get("blocks"), list):
        return None
    if row.accent_color:
        layout["accent_color"] = row.accent_color
    return layout


def load_layout(session, group_id, message_type: str) -> dict:
    """The effective layout for one (group, message type).

    The group's own row wins only when the group has the ``custom_embeds``
    entitlement (same premium gate as custom embeds — layouts are the same
    perk, component-shaped); otherwise the template group's row; otherwise
    the code default. Corrupt rows fall through rather than break sends."""
    from db.models import EventMessageLayout

    default = DEFAULT_LAYOUTS.get(message_type) or {"blocks": []}
    if session is None:
        return default

    candidates = [TEMPLATE_GROUP_ID]
    if group_id and group_id != TEMPLATE_GROUP_ID:
        try:
            from db.entitlements import has_custom_embeds

            if has_custom_embeds(group_id):
                candidates.insert(0, group_id)
        except Exception:
            pass

    try:
        rows = (
            session.query(EventMessageLayout)
            .filter(
                EventMessageLayout.group_id.in_(candidates),
                EventMessageLayout.message_type == message_type,
            )
            .all()
        )
    except Exception:
        return default
    by_group = {r.group_id: r for r in rows}
    for gid in candidates:
        row = by_group.get(gid)
        if row is not None:
            layout = _parse_layout_row(row)
            if layout is not None:
                return layout
    return default


# --------------------------------------------------------------------------- #
# Rendering: layout + context -> resolved spec -> interactions components
# --------------------------------------------------------------------------- #
def _substitute(text: str, context: dict) -> str:
    """Per-line token substitution: a line still containing an unresolved
    ``{token}`` after substitution is dropped (a None/absent context value
    does not substitute, so its lines vanish — the field-drop behaviour;
    an explicit empty string DOES substitute, e.g. ``{cell_plural}``)."""
    resolved_lines = []
    for line in (text or "").split("\n"):
        for key, value in (context or {}).items():
            token = "{" + key + "}"
            if token in line and value is not None:
                line = line.replace(token, str(value))
        if _TOKEN_RE.search(line):
            continue
        resolved_lines.append(line)
    return "\n".join(resolved_lines).strip()


def render_message_spec(layout: dict, context: dict, standings=None) -> dict:
    """Resolve one layout against its context into primitive blocks —
    pure data in, pure data out (the unit-testable half of rendering).

    Returns ``{"accent_color": int|None, "blocks": [...]}`` where blocks are
    ``{"type": "text", "content"}``, ``{"type": "section", "content",
    "thumbnail"}``, ``{"type": "separator"}`` or ``{"type": "buttons",
    "buttons": [{label, url}]}`` with every token resolved and every
    unresolvable piece dropped."""
    blocks = []
    for block in (layout or {}).get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "separator":
            # Collapse doubled separators (blocks between them may have dropped).
            if blocks and blocks[-1]["type"] == "separator":
                continue
            blocks.append({"type": "separator"})
        elif kind == "text":
            content = _substitute(block.get("content") or "", context)
            if content:
                blocks.append({"type": "text", "content": content})
        elif kind == "section":
            content = _substitute(block.get("content") or "", context)
            if not content:
                continue
            thumbnail = _substitute(block.get("thumbnail") or "", context)
            if thumbnail:
                blocks.append({"type": "section", "content": content, "thumbnail": thumbnail})
            else:
                blocks.append({"type": "text", "content": content})
        elif kind == "standings":
            try:
                limit = max(1, min(25, int(block.get("limit") or 10)))
            except (TypeError, ValueError):
                limit = 10
            title = _substitute(block.get("title") or "", context)
            body = standings_lines(standings, limit)
            blocks.append({"type": "text", "content": f"{title}\n{body}" if title else body})
        elif kind == "buttons":
            buttons = []
            for btn in block.get("buttons") or []:
                if not isinstance(btn, dict):
                    continue
                label = _substitute(str(btn.get("label") or ""), context)
                url = _substitute(str(btn.get("url") or ""), context)
                if label and url.startswith("http"):
                    buttons.append({"label": label[:80], "url": url})
            if buttons:
                blocks.append({"type": "buttons", "buttons": buttons})
    # Trim leading/trailing separators left by dropped neighbours.
    while blocks and blocks[0]["type"] == "separator":
        blocks.pop(0)
    while blocks and blocks[-1]["type"] == "separator":
        blocks.pop()
    return {"accent_color": _hex_to_int((layout or {}).get("accent_color")), "blocks": blocks}


def build_components(spec: dict, ping_text: Optional[str] = None, extra_rows=None) -> list:
    """Resolved spec -> ``[ContainerComponent]`` ready for ``channel.send``.

    ``ping_text`` (role mentions) becomes the first text display — V2
    components cannot carry ``content=``, but mentions inside a text display
    still notify under the send's allowed_mentions. ``extra_rows`` appends
    interactive rows the sender owns (e.g. the signup button)."""
    from interactions import ActionRow, Button, ButtonStyle
    from interactions.models import (
        ContainerComponent,
        SectionComponent,
        SeparatorComponent,
        TextDisplayComponent,
        ThumbnailComponent,
        UnfurledMediaItem,
    )

    children = []
    if ping_text:
        children.append(TextDisplayComponent(content=ping_text))
    for block in spec.get("blocks") or []:
        kind = block["type"]
        if kind == "separator":
            children.append(SeparatorComponent(divider=True))
        elif kind == "text":
            children.append(TextDisplayComponent(content=block["content"]))
        elif kind == "section":
            children.append(
                SectionComponent(
                    components=[TextDisplayComponent(content=block["content"])],
                    accessory=ThumbnailComponent(media=UnfurledMediaItem(url=block["thumbnail"])),
                )
            )
        elif kind == "buttons":
            children.append(
                ActionRow(
                    *[
                        Button(style=ButtonStyle.URL, label=b["label"], url=b["url"])
                        for b in block["buttons"]
                    ]
                )
            )
    for row in extra_rows or []:
        children.append(row)
    if not children:
        children.append(TextDisplayComponent(content="​"))
    kwargs = {}
    if spec.get("accent_color") is not None:
        kwargs["accent_color"] = spec["accent_color"]
    return [ContainerComponent(*children, **kwargs)]


def render_event_components(
    session,
    group_id,
    message_type: str,
    context: dict,
    standings=None,
    ping_text: Optional[str] = None,
    extra_rows=None,
) -> list:
    """One-stop: load the effective layout, resolve it, build components."""
    layout = load_layout(session, group_id, message_type)
    spec = render_message_spec(layout, context, standings=standings)
    return build_components(spec, ping_text=ping_text, extra_rows=extra_rows)


# --------------------------------------------------------------------------- #
# Context building (queue payload -> flat {token: value})
# --------------------------------------------------------------------------- #
def notification_context(notification_type: str, data: dict) -> dict:
    """Flatten one (enriched) notification_queue payload into the token dict
    the layouts substitute from. Values that are None/empty/zero are omitted
    so their lines drop out of the rendered message."""
    from services.event_notifications import _fmt_ts, event_url

    data = data or {}
    context = {}

    def put(key, value):
        if value is not None and str(value) != "" and value != 0:
            context[key] = value

    event_id = data.get("event_id")
    put("event_name", data.get("event_name") or "Event")
    put("event_url", event_url(event_id) if event_id else None)
    put("description", data.get("description"))
    put("team_name", data.get("team_name") or (
        f"Team {data.get('team_id')}" if data.get("team_id") else None))
    put("player_name", data.get("player_name"))
    put("task_label", data.get("task_label"))
    put("points", int(data.get("points") or 0))
    if data.get("team_score") is not None:
        put("team_score", int(data["team_score"]))
    put("bonus_points", int(data.get("bonus_points") or data.get("points") or 0))
    put("starts_at", _fmt_ts(data.get("starts_at")))
    put("ends_at", _fmt_ts(data.get("ends_at")))
    put("team_count", data.get("team_count"))
    put("proof_url", data.get("proof_url"))
    put("review_url", data.get("review_url"))
    put("reason", data.get("reason"))

    # Task progress
    progress, target = data.get("progress"), data.get("target")
    if progress is not None and target:
        put("progress", int(progress))
        put("target", int(target))
        put("progress_bar", text_progress_bar(progress, target))
    put("milestone_pct", data.get("milestone_pct"))

    # Bingo cells
    cell = data.get("cell_label") or (
        f"Cell {data.get('cell_idx')}" if data.get("cell_idx") is not None else None)
    put("cell_label", cell)
    cells = data.get("cell_idxs") or []
    if cells:
        put("cell_list", "`" + ", ".join(str(c) for c in cells) + "`")
        context["cell_plural"] = "s" if len(cells) != 1 else ""

    if notification_type == "event_signup_prompt":
        context["signup_instructions"] = {
            "self_join": "Pick your account, then choose your team.",
            "auto_assign": "Pick your account — you'll be placed on a team automatically.",
            "signup_pool": "Pick your account to join the sign-up pool; "
                           "admins will sort teams later.",
        }.get(data.get("formation_mode"), "Pick your account to enter.")
        context.setdefault("description", "This event is open for sign-ups!")

    return context
