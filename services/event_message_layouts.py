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
    -> ``ActionRow`` of buttons. A button is either a URL button
    (``{"label", "url"}``; dropped when its url doesn't resolve) or a launch
    button (``{"label", "launch": true, "view"?: "review"}``) that opens the
    Discord Activity deep-linked to ``{event_id}`` from the context — an
    optional ``view`` targets a specific in-app screen (``"review"`` = the
    event's pending-completions queue). Launch buttons are dropped
    when deep-linking is disabled (:func:`deeplink_enabled`) or there is no
    event id, so a row falling back to just its URL button still renders; an
    empty row is dropped.

Module-level imports are stdlib-only (same deal as event_notifications.py)
so the unit tests can load it directly; interactions.py and db models are
lazy-imported inside the functions that need them.
"""
from __future__ import annotations

import json
import os
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

# Loot Sweep verbosity message types (loot_sweep kind) — they build their own
# enriched standing/contributor tokens rather than the generic bingo path.
_SWEEP_TYPES = ("event_sweep_item", "event_sweep_group", "event_sweep_set")


def deeplink_enabled() -> bool:
    """Whether "Open in Discord" launch buttons are rendered on event messages.

    Gated by ``ACTIVITY_DEEPLINK_ENABLED`` (default off): a launch button only
    works once the primary Discord app has Activities enabled + a root URL
    mapping, so until that portal step is live the buttons must fall back to
    the website URL button. Flip this on the same deploy the Activity goes
    live on the verified app."""
    return os.environ.get("ACTIVITY_DEEPLINK_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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
            # Prize pot (web52a): a single pre-composed line, so the whole block
            # drops via the token-drop rule when the pot is off / not advertised.
            {"type": "text", "content": "{pot_started_line}"},
            {"type": "separator"},
            {
                "type": "buttons",
                "buttons": [
                    {"label": "\U0001F4F2 Open live board", "launch": True},
                    {"label": "Follow the event live", "url": "{event_url}"},
                ],
            },
        ],
    },
    "event_ended": {
        "accent_color": "#FFD700",
        "blocks": [
            {"type": "text", "content": "## \U0001F3C6 {event_name} has ended!"},
            {"type": "separator"},
            {"type": "standings", "limit": 5, "title": "**Final standings**"},
            # Prize pot result (web52a) — "🏆 {winner} takes the {pot} pot" etc.
            {"type": "text", "content": "{pot_result_line}"},
            {"type": "separator"},
            {
                "type": "buttons",
                "buttons": [
                    {"label": "\U0001F4F2 Final standings", "launch": True},
                    {"label": "Full results", "url": "{event_url}"},
                ],
            },
        ],
    },
    "event_completion": {
        "accent_color": "#57F287",
        "blocks": [
            {"type": "text", "content": "### ✅ {team_name} completed **{task_label}**"},
            {
                # Thumbnail is the received item's icon (completion_icon,
                # resolved by the sender), the proof screenshot rides below as
                # the full image. Each line drops when its token is unresolved:
                # received_line is suppressed when the task is named after the
                # item; team_total_line renders for non-bingo events while
                # bingo events show the {bingo_stats} block instead; the
                # completed_by / contributors lines are mutually exclusive.
                "type": "section",
                "content": "**Finished with** {received_line}\n"
                           "**Points** `+{points}`\n"
                           "{team_total_line}\n"
                           "{completed_by_line}\n"
                           "{contributors_block}\n"
                           "{note_line}",
                "thumbnail": "{completion_icon}",
            },
            {"type": "text", "content": "{bingo_stats}"},
        ],
    },
    "event_task_progress": {
        "accent_color": "#3498DB",
        "blocks": [
            {
                # One "task tile": progress text on the left, the target item/
                # NPC/skill icon (task_icon) as the thumbnail on the right.
                # Degrades to a plain text block when the icon can't resolve.
                "type": "section",
                "content": "### \U0001F4C8 {team_name} — {task_label}\n"
                           "-# Passed **{milestone_pct}%**\n"
                           "{progress_bar} `{progress} / {target}`\n"
                           "-# by {player_name}",
                "thumbnail": "{task_icon}",
            },
        ],
    },
    # Loot Sweep (loot_sweep kind) — three verbosity levels, each enriched with
    # the item/points detail that used to live only on the website. Composite
    # line-tokens (sweep_*) are pre-resolved in notification_context so an
    # absent piece drops its own line cleanly.
    "event_sweep_item": {
        "accent_color": "#3498DB",
        "blocks": [
            {
                "type": "section",
                "content": "### \U0001F4E5 {team_name} received {received_display}\n"
                           "{sweep_npc_line}\n"
                           "**Scored** `+{received_points} pts`\n"
                           "{sweep_copies_line}\n"
                           "{sweep_group_progress_line}\n"
                           "{sweep_set_total_line}\n"
                           "{sweep_by_line}",
                "thumbnail": "{completion_icon}",
            },
        ],
    },
    "event_sweep_group": {
        "accent_color": "#1ABC9C",
        "blocks": [
            {"type": "text", "content": "## ✅ {team_name} completed {group_label}!"},
            {"type": "text", "content": "{sweep_group_again_line}"},
            {
                "type": "section",
                "content": "**Set bonus** `+{sweep_bonus} pts`\n"
                           "{sweep_standing_line}\n"
                           "{contributors_block}",
                "thumbnail": "{completion_icon}",
            },
        ],
    },
    "event_sweep_set": {
        "accent_color": "#57F287",
        "blocks": [
            {"type": "text", "content": "## \U0001F3C6 {team_name} swept {task_label}!"},
            {"type": "text", "content": "-# Completed every boss in the set{sweep_set_again_suffix}"},
            {
                "type": "section",
                "content": "**Full-set bonus** `+{sweep_bonus} pts`\n"
                           "{sweep_standing_line}\n"
                           "{contributors_block}",
                "thumbnail": "{completion_icon}",
            },
        ],
    },
    "event_line": {
        "accent_color": "#9B59B6",
        "blocks": [
            # line_summary names the line(s): "completed a full line — **Row
            # 2**", or the coalesced "completed **2** full lines at once — …"
            # when one cell finished several lines in the same evaluation.
            {"type": "text", "content": "### \U0001F4CF Line bonus! **{team_name}** {line_summary}"},
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
            # Loot Sweep names the drop that took the lead (drops for other
            # kinds, whose lead-change payload carries no received item).
            {"type": "text", "content": "{lead_via_line}"},
            {"type": "separator"},
            {"type": "standings", "limit": 3, "title": "**Standings**"},
        ],
    },
    "event_board_turn": {
        "accent_color": "#F1C40F",
        "blocks": [
            {"type": "text", "content": "### \U0001F3B2 {team_name} rolled `{dice_str}`"},
            {
                "type": "text",
                "content": "Tile `{tile_from}` → `{tile_to}`\n"
                           "**Next task** {next_task_label}\n"
                           "**Coins** `+{coins_awarded}` (wallet `{coin_balance}`)\n"
                           "-# Turn #{turn}, rolled after {player_name}'s completion",
            },
        ],
    },
    # Board-game victory (layout-only key: queued as event_board_turn with
    # data.won, remapped in render_event_components). Without it the primary
    # layout rendered a winning roll as a mundane "rolled 3+4 — Next task"
    # line — the trophy copy lived only in the legacy embed (audit).
    "event_board_win": {
        "accent_color": "#FFD700",
        "blocks": [
            {"type": "text", "content": "## \U0001F3C6 {team_name} reached the finish!"},
            {
                "type": "text",
                "content": "**{team_name}** rolled `{dice_str}` and crossed the "
                           "finish line — the board is theirs!\n"
                           "-# Final standings follow in the wrap-up message.",
            },
            _EVENT_BUTTON,
        ],
    },
    "event_board_action": {
        "accent_color": "#E74C3C",
        "blocks": [
            {"type": "text", "content": "### ⚔️ Board skirmish"},
            {"type": "text", "content": "{action_line}"},
        ],
    },
    "event_board_roll_prompt": {
        "accent_color": "#F1C40F",
        "blocks": [
            {"type": "text", "content": "### \U0001F3B2 {team_name} can roll!"},
            {
                "type": "text",
                "content": "**{task_label}** is done{roll_thanks_line} — roll the "
                           "dice to move on.\n"
                           "**Coins** `+{coins_awarded}` (wallet `{coin_balance}`)",
            },
            _EVENT_BUTTON,
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
                "buttons": [
                    {"label": "\U0001F4F2 Review in app", "launch": True, "view": "review"},
                    {"label": "Open the review queue", "url": "{review_url}"},
                ],
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
    "event_end_failed": {
        "accent_color": "#ED4245",
        "blocks": [
            {"type": "text", "content": "## ⚠️ {event_name} needs attention at the finish"},
            {
                "type": "text",
                "content": "Something went wrong while ending the event: {reason}\nCheck the final standings and announcements — some may need to be posted by hand.",
            },
            {"type": "text", "content": "**Scheduled end** {ends_at}"},
            {
                "type": "buttons",
                "buttons": [{"label": "Open the event manager", "url": "{event_url}"}],
            },
        ],
    },
    "event_multi_clan_skipped": {
        "accent_color": "#FAA61A",
        "blocks": [
            {"type": "text", "content": "## ⚠️ {skipped_count} player(s) need a team"},
            {
                "type": "text",
                "content": "These players are in more than one clan competing in **{event_name}**, so they weren't auto-added to a whole-clan team:\n{skipped_players}",
            },
            {"type": "text", "content": "Add each to a specific team to include them."},
            {
                "type": "buttons",
                "buttons": [{"label": "Manage teams", "url": "{event_url}"}],
            },
        ],
    },
    "event_pot": {
        "accent_color": "#FFD700",
        "blocks": [
            {"type": "text", "content": "## \U0001F4B0 {event_name} — Prize Pot"},
            {"type": "text", "content": "{pot_announce_line}"},
            {"type": "text", "content": "{pot_contributors_block}"},
            {
                "type": "buttons",
                "buttons": [
                    {"label": "\U0001F4F2 Open the event", "launch": True},
                    {"label": "Buy in / donate", "url": "{event_url}"},
                ],
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
            # web70a: the real deadline — the event's start unless the event
            # allows late sign-ups, in which case it is the event's end. The
            # old copy always pointed at {ends_at}, promising a window that
            # had in fact shut when the event began.
            {"type": "text", "content": "**Sign-ups close** {signup_close_at}"},
        ],
    },
    # The same post after sign-ups close (web70a): the retire sweep edits this
    # layout over the original prompt message and drops the button, so a live
    # "Sign up" control never outlives the window it advertised.
    "event_signup_closed": {
        "accent_color": "#4E5058",
        "blocks": [
            {"type": "text", "content": "## \U0001F512 Sign-ups closed — {event_name}"},
            {"type": "text", "content": "{signup_closed_line}"},
            {
                "type": "buttons",
                "buttons": [
                    {"label": "\U0001F4F2 Open the event", "launch": True},
                    {"label": "View on droptracker.io", "url": "{event_url}"},
                ],
            },
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
            # Prize pot (web52a): one pre-composed token, so the block drops
            # cleanly when the pot is off / not advertised. Read fresh each sweep.
            {"type": "text", "content": "{pot_line}"},
            {"type": "text", "content": "-# Updated {updated_ts} • refreshes automatically"},
            {
                "type": "buttons",
                "buttons": [
                    {"label": "\U0001F4F2 Open interactive board", "launch": True},
                    {"label": "Full standings", "url": "{event_url}"},
                ],
            },
        ],
    },
}


# --------------------------------------------------------------------------- #
# Editor metadata (web66a): validation limits, token docs and per-type
# capabilities, served to the web layout editor by
# web_api/routes/event_layouts.py GET /event-layouts/meta. Kept next to
# DEFAULT_LAYOUTS so a new token/type is added in one file.
# --------------------------------------------------------------------------- #
MAX_BLOCKS = 15
MAX_TEXT_LEN = 2000
MAX_TITLE_LEN = 200
MAX_URL_LEN = 500
MAX_BUTTONS = 5
MAX_LABEL_LEN = 80

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_BLOCK_TYPES = ("text", "section", "separator", "standings", "buttons")

# {token: {"help", "sample"}} — the sample values drive the editor's live
# preview ("fill with sample data"), so they should read like a real event.
TOKEN_DOCS = {
    "event_name": {"help": "The event's name", "sample": "Summer Loot Sweep"},
    "event_url": {"help": "Link to the event page on droptracker.io",
                  "sample": "https://www.droptracker.io/events/42"},
    "description": {"help": "The event's description",
                    "sample": "Six weeks of loot-fueled chaos across the whole clan."},
    "starts_at": {"help": "Start time (rendered as a Discord timestamp)",
                  "sample": "July 22, 2026 5:00 PM"},
    "ends_at": {"help": "End time (rendered as a Discord timestamp)",
                "sample": "August 5, 2026 5:00 PM"},
    "team_count": {"help": "Number of teams in the event", "sample": "8"},
    "team_name": {"help": "The team this message is about", "sample": "Team Bandos"},
    "player_name": {"help": "The player who triggered this message", "sample": "Zezima"},
    "task_label": {"help": "The task's display name", "sample": "Bandos chestplate"},
    "points": {"help": "Points awarded", "sample": "25"},
    "bonus_points": {"help": "Bonus points awarded", "sample": "50"},
    "team_score": {"help": "The team's total score", "sample": "120"},
    "received_line": {"help": "The item that finished the task and how much of the "
                              "requirement it filled (drops when the task is named "
                              "after the item)",
                      "sample": "**Bandos chestplate** — filled the whole requirement"},
    "team_total_line": {"help": "Running team total (non-bingo events)",
                        "sample": "**Team total** `120 pts`"},
    "completed_by_line": {"help": "\"Completed by\" line (single contributor)",
                          "sample": "**Completed by** `Zezima`"},
    "note_line": {"help": "Organizer's note on a manual award (drops when absent)",
                  "sample": "-# Note: credited retroactively — joined mid-event"},
    "contributors_block": {"help": "Ranked contributor breakdown (several contributors)",
                           "sample": "**Contributors**\n\U0001F947 **Zezima** `3`\n\U0001F948 **Durial321** `1`"},
    "contributors_line": {"help": "Compact comma-joined contributor list",
                          "sample": "**Zezima** `3`, **Durial321** `1`"},
    "bingo_stats": {"help": "Team board summary (bingo events)",
                    "sample": "**Total tiles completed** `5`\n**Total points earned** `120 pts`\n**Team position** #1/8 teams"},
    "completion_icon": {"help": "Icon URL for the received item (falls back to the task icon)",
                        "sample": "https://www.droptracker.io/img/itemdb/11832.png"},
    "task_icon": {"help": "Icon URL for the task's target item/NPC/skill",
                  "sample": "https://www.droptracker.io/img/itemdb/11832.png"},
    "proof_url": {"help": "Proof screenshot URL",
                  "sample": "https://www.droptracker.io/img/proofs/sample.png"},
    "review_url": {"help": "Link to the pending-review queue",
                   "sample": "https://www.droptracker.io/groups/2/events/42"},
    "milestone_pct": {"help": "The milestone percentage just crossed", "sample": "50"},
    "progress_bar": {"help": "Text progress meter", "sample": "▰▰▰▰▰▱▱▱▱▱"},
    "progress": {"help": "Current progress (abbreviated K/M/B)", "sample": "5.00M"},
    "target": {"help": "The task's target (abbreviated K/M/B)", "sample": "10.00M"},
    "cell_label": {"help": "The bingo tile's label", "sample": "B2 — Bandos chestplate"},
    "cell_list": {"help": "List of bingo tiles involved", "sample": "**B2 — Bandos chestplate**"},
    "cell_plural": {"help": "\"s\" when several tiles are involved, empty otherwise",
                    "sample": ""},
    "line_summary": {"help": "Which full line(s) the team completed",
                     "sample": "completed a full line — **Row 2**"},
    "lead_via_line": {"help": "The drop that took the lead (Loot Sweep only)",
                      "sample": "-# with **Twisted bow** (`+120 pts`)"},
    "dice_str": {"help": "The dice roll", "sample": "3 + 4"},
    "tile_from": {"help": "Board tile moved from", "sample": "12"},
    "tile_to": {"help": "Board tile landed on", "sample": "19"},
    "turn": {"help": "The team's turn number", "sample": "7"},
    "next_task_label": {"help": "The next task drawn for the team", "sample": "Zulrah kill"},
    "coins_awarded": {"help": "Coins earned this turn", "sample": "3"},
    "coin_balance": {"help": "The team's coin wallet after the turn", "sample": "11"},
    "action_line": {"help": "What happened in the board skirmish",
                    "sample": "**Team Bandos** froze **Team Zamorak** for 2 turns"},
    "roll_thanks_line": {"help": "\" (thanks **player**)\" credit on the roll prompt",
                         "sample": " (thanks **Zezima**)"},
    "reason": {"help": "Why the lifecycle step failed", "sample": "no team has any members"},
    "pot_started_line": {"help": "Prize-pot line on the start announcement (drops when "
                                 "the pot is off or not advertised)",
                         "sample": "\U0001F4B0 **250M GP** prize pot on the line!"},
    "pot_result_line": {"help": "Prize-pot result line on the end announcement",
                        "sample": "\U0001F3C6 **Team Bandos** takes the **250M GP** pot!"},
    "pot_announce_line": {"help": "The pot advertisement line",
                          "sample": "The pot stands at **250M GP** — buy-ins open until the start."},
    "pot_contributors_block": {"help": "Pot contributor list",
                               "sample": "**Contributors**\n**Zezima** `100M`\n**Durial321** `50M`"},
    "pot_line": {"help": "Prize-pot line on the live board",
                 "sample": "\U0001F4B0 Prize pot: **250M GP**"},
    "signup_instructions": {"help": "How to sign up (matches the event's formation mode)",
                            "sample": "Pick your account, then choose your team."},
    "signup_close_at": {"help": "When sign-ups close — the event's start, or its end "
                                "when the event allows late sign-ups (Discord timestamp)",
                        "sample": "July 22, 2026 5:00 PM"},
    "signup_closed_line": {"help": "Why the sign-up post is closed (shown on the retired prompt)",
                           "sample": "The event is underway — sign-ups closed July 22, 2026 5:00 PM."},
    "board_status_line": {"help": "The live board's status line",
                          "sample": "Day 3 of 14 — 12 tasks completed"},
    "tasks_summary": {"help": "Task-completion summary on the live board",
                      "sample": "✅ 12/25 tasks completed"},
    "updated_ts": {"help": "When the live board last refreshed",
                   "sample": "July 22, 2026 5:04 PM"},
    "received_display": {"help": "The received item (with quantity when stacked)",
                         "sample": "3× Zenyte shard"},
    "received_points": {"help": "Points scored by this receipt", "sample": "4.5"},
    "sweep_npc_line": {"help": "Which NPC dropped it", "sample": "-# from **Zulrah**"},
    "sweep_copies_line": {"help": "Scoring copies so far for this item",
                          "sample": "-# `2/3` scoring copies • next `+2.25`"},
    "sweep_group_progress_line": {"help": "Progress through the item's subset",
                                  "sample": "**Zulrah uniques** ▰▰▰▱▱▱▱▱▱▱ `2/6 items`"},
    "sweep_set_total_line": {"help": "The set's running total",
                             "sample": "-# Set running total `48.75 pts`"},
    "sweep_by_line": {"help": "Who received it", "sample": "-# by **Zezima**"},
    "group_label": {"help": "The completed subset's label", "sample": "Zulrah uniques"},
    "sweep_group_again_line": {"help": "Repeat-completion note (bonus decays)",
                               "sample": "-# Completed ×2 — the bonus decays each time"},
    "sweep_bonus": {"help": "The subset/set completion bonus", "sample": "25"},
    "sweep_standing_line": {"help": "Team total + position",
                            "sample": "**Team total** `120.5 pts` • `#1/8`"},
    "sweep_set_again_suffix": {"help": "\" (×n)\" on repeat set sweeps", "sample": ""},
}

# Tokens available on every message type (the notification_context basics).
_COMMON_TOKENS = ("event_name", "event_url", "description", "starts_at", "ends_at")

# {message_type: {"label", "group", "description", "tokens", "standings"}} —
# ``tokens`` extends _COMMON_TOKENS; ``standings`` marks the types whose
# senders pass real standings (a standings block anywhere else renders "No
# teams yet."). Order here is the editor's display order.
TYPE_META = {
    "event_started": {
        "label": "Event started", "group": "Lifecycle",
        "description": "Posted to the announcements channel when the event goes live.",
        "tokens": ("team_count", "pot_started_line"), "standings": False,
    },
    "event_ended": {
        "label": "Event ended", "group": "Lifecycle",
        "description": "The wrap-up announcement with final standings.",
        "tokens": ("pot_result_line",), "standings": True,
    },
    "event_activation_failed": {
        "label": "Activation failed", "group": "Lifecycle",
        "description": "Admin alert: the scheduled start passed but the event could not activate.",
        "tokens": ("reason",), "standings": False,
    },
    "event_end_failed": {
        "label": "End failed", "group": "Lifecycle",
        "description": "Admin alert: something went wrong while ending the event.",
        "tokens": ("reason",), "standings": False,
    },
    "event_completion": {
        "label": "Task completion", "group": "Progress",
        "description": "A team completed a task.",
        "tokens": ("team_name", "player_name", "task_label", "points", "received_line",
                   "team_total_line", "completed_by_line", "contributors_block",
                   "contributors_line", "bingo_stats", "completion_icon", "task_icon",
                   "cell_label", "cell_list", "cell_plural", "proof_url", "note_line"),
        "standings": False,
    },
    "event_task_progress": {
        "label": "Task progress", "group": "Progress",
        "description": "A team crossed a progress milestone on a task.",
        "tokens": ("team_name", "player_name", "task_label", "milestone_pct",
                   "progress_bar", "progress", "target", "task_icon"),
        "standings": False,
    },
    "event_line": {
        "label": "Bingo line bonus", "group": "Progress",
        "description": "A team completed a full bingo line.",
        "tokens": ("team_name", "line_summary", "bonus_points", "cell_list", "cell_plural"),
        "standings": False,
    },
    "event_blackout": {
        "label": "Bingo blackout", "group": "Progress",
        "description": "A team completed the entire bingo board.",
        "tokens": ("team_name", "bonus_points"), "standings": False,
    },
    "event_lead_change": {
        "label": "Lead change", "group": "Progress",
        "description": "A new team took the overall lead.",
        "tokens": ("team_name", "task_label", "lead_via_line"), "standings": True,
    },
    "event_pending": {
        "label": "Completion pending review", "group": "Progress",
        "description": "A submission needs an admin's confirmation before it counts.",
        "tokens": ("team_name", "player_name", "task_label", "proof_url", "review_url"),
        "standings": False,
    },
    "event_sweep_item": {
        "label": "Sweep: item received", "group": "Loot Sweep",
        "description": "A team received a scoring item (very chatty — off by default).",
        "tokens": ("team_name", "player_name", "received_display", "received_points",
                   "sweep_npc_line", "sweep_copies_line", "sweep_group_progress_line",
                   "sweep_set_total_line", "sweep_by_line", "completion_icon"),
        "standings": False,
    },
    "event_sweep_group": {
        "label": "Sweep: subset completed", "group": "Loot Sweep",
        "description": "A team completed one subset of the sweep.",
        "tokens": ("team_name", "group_label", "sweep_group_again_line", "sweep_bonus",
                   "sweep_standing_line", "contributors_block", "completion_icon"),
        "standings": False,
    },
    "event_sweep_set": {
        "label": "Sweep: full set", "group": "Loot Sweep",
        "description": "A team swept the entire set.",
        "tokens": ("team_name", "task_label", "sweep_set_again_suffix", "sweep_bonus",
                   "sweep_standing_line", "contributors_block", "completion_icon"),
        "standings": False,
    },
    "event_board_turn": {
        "label": "Board: turn", "group": "Board game",
        "description": "A team rolled the dice and moved.",
        "tokens": ("team_name", "player_name", "dice_str", "tile_from", "tile_to",
                   "turn", "next_task_label", "coins_awarded", "coin_balance"),
        "standings": False,
    },
    "event_board_win": {
        "label": "Board: victory", "group": "Board game",
        "description": "A team crossed the finish line.",
        "tokens": ("team_name", "dice_str"), "standings": False,
    },
    "event_board_roll_prompt": {
        "label": "Board: roll prompt", "group": "Board game",
        "description": "\"Task done — roll the dice\" nudge.",
        "tokens": ("team_name", "task_label", "roll_thanks_line", "coins_awarded",
                   "coin_balance"),
        "standings": False,
    },
    "event_board_action": {
        "label": "Board: skirmish", "group": "Board game",
        "description": "A team used an item on a rival (or a defense blocked one).",
        "tokens": ("team_name", "action_line"), "standings": False,
    },
    "event_signup_prompt": {
        "label": "Sign-up prompt", "group": "Announcements",
        "description": "The interactive sign-up post (the Sign up button is always attached).",
        "tokens": ("signup_instructions", "signup_close_at"), "standings": False,
    },
    "event_signup_closed": {
        "label": "Sign-up prompt (closed)", "group": "Announcements",
        "description": "What the sign-up post becomes once sign-ups close — the button "
                       "is removed and the message is edited in place.",
        "tokens": ("signup_closed_line",), "standings": False,
    },
    "event_pot": {
        "label": "Prize pot", "group": "Announcements",
        "description": "The manual \"advertise the pot\" post.",
        "tokens": ("pot_announce_line", "pot_contributors_block"), "standings": False,
    },
    "event_board": {
        "label": "Live standings board", "group": "Live board",
        "description": "The auto-refreshing standings message in the leaderboard channel.",
        "tokens": ("board_status_line", "tasks_summary", "pot_line", "updated_ts"),
        "standings": True,
    },
}

# Sample standings for the editor preview (matches standings_lines() input).
SAMPLE_STANDINGS = [
    {"name": "Team Bandos", "score": 120},
    {"name": "Team Zamorak", "score": 95},
    {"name": "Team Saradomin", "score": 80},
]


def validate_layout_spec(layout) -> list:
    """Validate one layout document (``{"accent_color"?, "blocks": [...]}``)
    for saving; returns a list of human-readable problems (empty = valid).

    This is the save-time gate for the web editor — stricter than the
    renderer, which tolerates anything (and the notification sender falls
    back to the legacy embed on a render error anyway). Stdlib-only, like
    the rest of the module's pure layer."""
    errors = []
    if not isinstance(layout, dict):
        return ["Layout must be an object."]

    accent = layout.get("accent_color")
    if accent not in (None, "") and (
            not isinstance(accent, str) or not _HEX_COLOR_RE.match(accent)):
        errors.append("accent_color must be a hex color like #FFD700.")

    blocks = layout.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        errors.append("The layout needs at least one block.")
        return errors
    if len(blocks) > MAX_BLOCKS:
        errors.append(f"At most {MAX_BLOCKS} blocks are allowed.")

    def check_text(value, what, i, required=True, limit=MAX_TEXT_LEN):
        if value is None or value == "":
            if required:
                errors.append(f"Block #{i + 1}: {what} is required.")
            return
        if not isinstance(value, str):
            errors.append(f"Block #{i + 1}: {what} must be a string.")
        elif len(value) > limit:
            errors.append(f"Block #{i + 1}: {what} must be at most {limit} characters.")

    def check_url(value, what, i):
        if value is None or value == "":
            return
        if not isinstance(value, str):
            errors.append(f"Block #{i + 1}: {what} must be a string.")
            return
        if len(value) > MAX_URL_LEN:
            errors.append(f"Block #{i + 1}: {what} must be at most {MAX_URL_LEN} characters.")
        elif not _TOKEN_RE.search(value) and not value.lower().startswith(
                ("http://", "https://")):
            errors.append(
                f"Block #{i + 1}: {what} must be an http(s) URL or contain a "
                "token like {proof_url}.")

    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            errors.append(f"Block #{i + 1} must be an object.")
            continue
        kind = block.get("type")
        if kind not in _BLOCK_TYPES:
            errors.append(
                f"Block #{i + 1}: unknown type '{kind}' "
                f"(valid: {', '.join(_BLOCK_TYPES)}).")
            continue
        if kind == "text":
            check_text(block.get("content"), "content", i)
        elif kind == "section":
            check_text(block.get("content"), "content", i)
            check_url(block.get("thumbnail"), "thumbnail", i)
        elif kind == "standings":
            limit = block.get("limit")
            if limit is not None and (
                    not isinstance(limit, int) or isinstance(limit, bool)
                    or not 1 <= limit <= 25):
                errors.append(f"Block #{i + 1}: standings limit must be 1–25.")
            check_text(block.get("title"), "title", i, required=False,
                       limit=MAX_TITLE_LEN)
        elif kind == "buttons":
            buttons = block.get("buttons")
            if not isinstance(buttons, list) or not buttons:
                errors.append(f"Block #{i + 1}: a buttons block needs at least one button.")
                continue
            if len(buttons) > MAX_BUTTONS:
                errors.append(f"Block #{i + 1}: at most {MAX_BUTTONS} buttons per row.")
            for j, btn in enumerate(buttons):
                if not isinstance(btn, dict):
                    errors.append(f"Block #{i + 1} button #{j + 1} must be an object.")
                    continue
                label = btn.get("label")
                if not isinstance(label, str) or not label.strip():
                    errors.append(f"Block #{i + 1} button #{j + 1} needs a label.")
                elif len(label) > MAX_LABEL_LEN:
                    errors.append(
                        f"Block #{i + 1} button #{j + 1}: label must be at most "
                        f"{MAX_LABEL_LEN} characters.")
                if btn.get("launch"):
                    view = btn.get("view")
                    if view is not None and (not isinstance(view, str) or len(view) > 32):
                        errors.append(
                            f"Block #{i + 1} button #{j + 1}: 'view' must be a short string.")
                else:
                    url = btn.get("url")
                    if not url:
                        errors.append(
                            f"Block #{i + 1} button #{j + 1} needs a 'url' "
                            "(or 'launch': true).")
                    else:
                        check_url(url, f"button #{j + 1} url", i)
    return errors


# --------------------------------------------------------------------------- #
# Loader (event row -> group row -> group 1 row -> code default)
# --------------------------------------------------------------------------- #
def layout_candidates(group_id, event_id, entitled: bool) -> list:
    """The ``(group_id, event_id)`` row keys to try, highest priority first
    (pure — the unit-testable half of :func:`load_layout`'s resolution).

    A per-event override lives under the event's host group (template group
    for global events) and obeys the same entitlement gate as the group's
    default row, so a lapsed subscription reverts the whole event."""
    try:
        eid = int(event_id or 0)
    except (TypeError, ValueError):
        eid = 0
    candidates = []
    if eid:
        owner = group_id if (group_id and group_id != TEMPLATE_GROUP_ID) else TEMPLATE_GROUP_ID
        if owner == TEMPLATE_GROUP_ID or entitled:
            candidates.append((owner, eid))
    if group_id and group_id != TEMPLATE_GROUP_ID and entitled:
        candidates.append((group_id, 0))
    candidates.append((TEMPLATE_GROUP_ID, 0))
    return candidates


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


def load_layout(session, group_id, message_type: str, event_id=None) -> dict:
    """The effective layout for one (group, message type[, event]).

    Resolution (web66a): the event's own override row -> the group's row ->
    the template group's row -> the code default. Group-scoped rows (both the
    per-event override and the group default) win only when the group has the
    ``custom_embeds`` entitlement (same premium gate as custom embeds —
    layouts are the same perk, component-shaped), so a lapsed subscription
    reverts every event to the system defaults. Per-event overrides live
    under the event's host group — template group 1 for global events, whose
    overrides need no entitlement (superadmin-authored). Corrupt rows fall
    through rather than break sends."""
    from db.models import EventMessageLayout

    default = DEFAULT_LAYOUTS.get(message_type) or {"blocks": []}
    if session is None:
        return default

    entitled = False
    if group_id and group_id != TEMPLATE_GROUP_ID:
        try:
            from db.entitlements import has_custom_embeds

            entitled = has_custom_embeds(group_id)
        except Exception:
            entitled = False

    candidates = layout_candidates(group_id, event_id, entitled)

    try:
        rows = (
            session.query(EventMessageLayout)
            .filter(
                EventMessageLayout.group_id.in_({gid for gid, _ in candidates}),
                EventMessageLayout.message_type == message_type,
                EventMessageLayout.event_id.in_({e for _, e in candidates}),
            )
            .all()
        )
    except Exception:
        return default
    by_key = {(r.group_id, getattr(r, "event_id", 0) or 0): r for r in rows}
    for key in candidates:
        row = by_key.get(key)
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


def render_message_spec(
    layout: dict, context: dict, standings=None, deep_link: bool = True,
    launch_link: bool = False, footer: Optional[str] = None,
) -> dict:
    """Resolve one layout against its context into primitive blocks —
    pure data in, pure data out (the unit-testable half of rendering).

    Returns ``{"accent_color": int|None, "blocks": [...]}`` where blocks are
    ``{"type": "text", "content"}``, ``{"type": "section", "content",
    "thumbnail"}``, ``{"type": "separator"}`` or ``{"type": "buttons",
    "buttons": [...]}`` (each button ``{label, url}`` or ``{label, launch:
    True, event_id}``) with every token resolved and every unresolvable piece
    dropped. ``deep_link`` False drops launch buttons (the deploy gate lives in
    :func:`deeplink_enabled`; explicit here so the resolver stays pure) —
    unless ``launch_link`` is True, which renders them as Activity Link URL
    buttons instead (the destination channel can't host a LAUNCH_ACTIVITY
    callback — threads/announcement channels — but a client-side app link
    still opens the Activity from there).

    ``footer`` (an already-resolved line — see
    :func:`services.event_notifications.event_footer_line`) is appended as a
    trailing text block behind its own separator so every event message ends
    with the same event-anchoring footer, regardless of the group's custom
    layout. It is not token-substituted (it holds ``<t:…>`` tokens, not
    ``{placeholders}``)."""
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
                if not label:
                    continue
                if btn.get("launch"):
                    # Deep-link into the Discord Activity; the event id comes
                    # from the context, not a URL. An optional "view" opens a
                    # specific in-app screen (e.g. "review" — the pending-
                    # completions queue). Dropped when deep-linking is off or
                    # no event is in scope.
                    event_id = (context or {}).get("event_id")
                    if event_id in (None, "", 0):
                        continue
                    view = btn.get("view") if isinstance(btn.get("view"), str) else None
                    if deep_link:
                        button = {"label": label[:80], "launch": True,
                                  "event_id": str(event_id)}
                        if view:
                            button["view"] = view
                        buttons.append(button)
                    elif launch_link:
                        from services.activity_launch_core import activity_link_url

                        buttons.append({"label": label[:80],
                                        "url": activity_link_url(event_id, view)})
                    continue
                url = _substitute(str(btn.get("url") or ""), context)
                if url.startswith("http"):
                    buttons.append({"label": label[:80], "url": url})
            if buttons:
                blocks.append({"type": "buttons", "buttons": buttons})
    # Trim leading/trailing separators left by dropped neighbours.
    while blocks and blocks[0]["type"] == "separator":
        blocks.pop(0)
    while blocks and blocks[-1]["type"] == "separator":
        blocks.pop()
    # Universal event footer: separated from the body, always last.
    if footer:
        if blocks:
            blocks.append({"type": "separator"})
        blocks.append({"type": "text", "content": footer})
    return {"accent_color": _hex_to_int((layout or {}).get("accent_color")), "blocks": blocks}


def build_components(spec: dict, ping_text: Optional[str] = None, extra_rows=None,
                     image_ref: Optional[str] = None) -> list:
    """Resolved spec -> ``[ContainerComponent]`` ready for ``channel.send``.

    ``ping_text`` (role mentions) becomes the first text display — V2
    components cannot carry ``content=``, but mentions inside a text display
    still notify under the send's allowed_mentions. ``extra_rows`` appends
    interactive rows the sender owns (e.g. the signup button). ``image_ref``
    — an ``attachment://filename`` reference for a screenshot the sender has
    already attached to the send, or a plain URL — renders as a full-size
    media gallery item after the layout's own blocks, giving completion/
    progress messages the same prominent screenshot the submission-processing
    notifications (drop, pb, ...) show, on top of any small task-tile
    thumbnail a section block already carries."""
    from interactions import ActionRow, Button, ButtonStyle
    from interactions.models import (
        ContainerComponent,
        MediaGalleryComponent,
        MediaGalleryItem,
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
            from services.activity_launch_core import launch_button_custom_id

            row = []
            for b in block["buttons"]:
                if b.get("launch"):
                    row.append(
                        Button(
                            style=ButtonStyle.BLURPLE,
                            label=b["label"],
                            custom_id=launch_button_custom_id(b["event_id"], b.get("view")),
                        )
                    )
                else:
                    row.append(Button(style=ButtonStyle.URL, label=b["label"], url=b["url"]))
            children.append(ActionRow(*row))
    if image_ref:
        children.append(MediaGalleryComponent(items=[MediaGalleryItem(media=image_ref)]))
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
    allow_launch: bool = True,
    image_ref: Optional[str] = None,
    event_id=None,
) -> list:
    """One-stop: load the effective layout, resolve it, build components.

    ``allow_launch`` False (the destination is a thread/announcement channel,
    where Discord refuses LAUNCH_ACTIVITY) renders launch buttons as Activity
    Link URL buttons — a client-side launch that works from those channels.
    ``image_ref`` is passed straight through to :func:`build_components`."""
    from services.event_notifications import event_footer_line

    # Board-game victory gets its own layout — the turn layout is a static
    # token template with no conditional branch, so a winning roll would
    # otherwise render as an ordinary dice move (audit).
    if message_type == "event_board_turn" and context.get("board_won"):
        message_type = "event_board_win"
    # Same flip for the sign-up post once its window shuts (web70a): the retire
    # sweep re-renders the original message through the closed layout.
    if message_type == "event_signup_prompt" and context.get("signups_closed"):
        message_type = "event_signup_closed"
    layout = load_layout(session, group_id, message_type,
                         event_id=event_id or (context or {}).get("event_id"))
    enabled = deeplink_enabled()
    footer = event_footer_line(
        context.get("event_name"),
        context.get("starts_at_unix"),
        context.get("ends_at_unix"),
    )
    spec = render_message_spec(
        layout, context, standings=standings,
        deep_link=enabled and allow_launch,
        launch_link=enabled and not allow_launch,
        footer=footer,
    )
    return build_components(spec, ping_text=ping_text, extra_rows=extra_rows, image_ref=image_ref)


# --------------------------------------------------------------------------- #
# Context building (queue payload -> flat {token: value})
# --------------------------------------------------------------------------- #
def notification_context(notification_type: str, data: dict) -> dict:
    """Flatten one (enriched) notification_queue payload into the token dict
    the layouts substitute from. Values that are None/empty/zero are omitted
    so their lines drop out of the rendered message."""
    from services.event_notifications import (
        _completion_item_redundant, _fmt_ts, _received_item_text,
        event_url, fmt_pts, format_gp, line_bonus_summary,
        sweep_contributor_lines,
    )

    data = data or {}
    context = {}

    def put(key, value):
        if value is not None and str(value) != "" and value != 0:
            context[key] = value

    event_id = data.get("event_id")
    put("event_id", event_id)  # raw id — powers launch (deep-link) buttons
    put("event_name", data.get("event_name") or "Event")
    put("event_url", event_url(event_id) if event_id else None)
    put("description", data.get("description"))
    put("team_name", data.get("team_name") or (
        f"Team {data.get('team_id')}" if data.get("team_id") else None))
    put("player_name", data.get("player_name"))
    put("task_label", data.get("task_label"))
    put("points", int(data.get("points") or 0))
    if data.get("team_score") is not None:
        # fmt_pts, not int(): loot_sweep team scores carry 2dp decimals —
        # int() showed a 100.8-point leader as "100 pts" (audit).
        put("team_score", fmt_pts(data["team_score"]))
    put("bonus_points", int(data.get("bonus_points") or data.get("points") or 0))
    if notification_type == "event_line":
        # Always resolvable (falls back to "completed a full line" on legacy
        # payloads without line identity) so the title line never drops.
        context["line_summary"] = line_bonus_summary(data)
    if notification_type == "event_lead_change" and data.get("received_item"):
        # Loot Sweep: name the drop (+ points) that took the lead.
        pts = data.get("points")
        context["lead_via_line"] = (
            f"-# with **{data['received_item']}**"
            + (f" (`+{fmt_pts(pts)} pts`)" if pts else ""))
    put("starts_at", _fmt_ts(data.get("starts_at")))
    put("ends_at", _fmt_ts(data.get("ends_at")))
    # Raw unix seconds for the universal footer line (event_footer_line);
    # kept separate from the pre-formatted starts_at/ends_at tokens above.
    put("starts_at_unix", data.get("starts_at"))
    put("ends_at_unix", data.get("ends_at"))
    put("team_count", data.get("team_count"))
    put("proof_url", data.get("proof_url"))
    put("review_url", data.get("review_url"))
    put("reason", data.get("reason"))
    put("skipped_players", data.get("skipped_players"))
    put("skipped_count", data.get("skipped_count"))
    # Prize pot (web52a): pre-composed single-token lines for the lifecycle
    # announcements, set by the sender only when the pot is advertised — so the
    # {pot_started_line}/{pot_result_line} blocks drop out otherwise.
    put("pot_started_line", data.get("pot_started_line"))
    put("pot_result_line", data.get("pot_result_line"))
    put("pot_announce_line", data.get("pot_announce_line"))
    put("pot_contributors_block", data.get("pot_contributors_block"))
    # Task-tile icon (the item/NPC/skill the task targets) for completion /
    # progress messages; completion_icon prefers a real proof screenshot and
    # falls back to the task icon. Both resolved by the sender.
    put("task_icon", data.get("task_icon"))
    put("completion_icon", data.get("completion_icon"))

    # Task progress — abbreviated K/M/B above 100,000 (a 10,000,000 GP target
    # reads as "10.00M") so large loot_value/xp_target tasks stay readable;
    # the bar itself is computed from the raw numbers before formatting.
    progress, target = data.get("progress"), data.get("target")
    if progress is not None and target:
        put("progress_bar", text_progress_bar(progress, target))
        put("progress", format_gp(progress))
        put("target", format_gp(target))
    put("milestone_pct", data.get("milestone_pct"))

    # Verbose completion detail — the item that finished the task + how much of
    # the requirement it filled (event_completion only; enriched at enqueue
    # when the event's item_details config is on). Suppressed when the task is
    # essentially named after this one item (the title already says it). Same
    # text as the legacy embed's "Received" field so both renderers agree.
    if not _completion_item_redundant(data):
        put("received_line", _received_item_text(data))

    # Manual awards: the organizer's reason for granting credit (enqueued
    # only when one was written) — same text as the legacy embed's "Note".
    if data.get("note"):
        put("note_line", f"-# Note: {data['note']}")

    # Bingo cells — labels are the readable "tile" identity; fall back to the
    # raw index for callers that only have that (legacy queued rows).
    cell = data.get("cell_label") or (
        f"Cell {data.get('cell_idx')}" if data.get("cell_idx") is not None else None)
    put("cell_label", cell)
    cell_labels = data.get("cell_labels") or []
    cells = data.get("cell_idxs") or []
    if cell_labels:
        put("cell_list", ", ".join(f"**{c}**" for c in cell_labels))
        context["cell_plural"] = "s" if len(cell_labels) != 1 else ""
    elif cells:
        put("cell_list", "`" + ", ".join(str(c) for c in cells) + "`")
        context["cell_plural"] = "s" if len(cells) != 1 else ""

    # Contributors — everyone who fed the task, largest contribution first
    # (event_completion only; see services.event_engine._task_contributors).
    contributors = data.get("contributors") or []
    # point_collection ledgers count points, not items — label the quantities.
    contrib_unit = " pts" if data.get("points_based") else ""

    def _contrib(c):
        line = (f"**{c.get('player_name') or 'Unknown'}** "
                f"`{format_gp(c.get('quantity') or 0)}{contrib_unit}`")
        # Contribution-share points (task points × net share, floats) —
        # see services.event_engine._award_contribution_points.
        share = c.get("points_share")
        if share:
            line += f" (+{share:g} pts)"
        return line

    if contributors:
        # Raw comma-joined list, kept for custom layouts that reference it.
        put("contributors_line", ", ".join(_contrib(c) for c in contributors))
    # Presentation for the default layout: one person collapses to a single
    # "Completed by" line; up to five get a "Contributors" header with one
    # contributor per line (ranked by contribution, medals for the top three)
    # so the breakdown reads like a table; a bigger pile falls back to the
    # compact comma list so the message doesn't turn into a wall of rows.
    if len(contributors) == 1:
        put("completed_by_line",
            f"**Completed by** `{contributors[0].get('player_name') or 'Unknown'}`")
    elif 1 < len(contributors) <= 5:
        medals = ("\U0001F947", "\U0001F948", "\U0001F949")  # 🥇 🥈 🥉
        rows = [f"{medals[i] if i < len(medals) else '•'} {_contrib(c)}"
                for i, c in enumerate(contributors)]
        put("contributors_block", "**Contributors**\n" + "\n".join(rows))
    elif len(contributors) > 5:
        put("contributors_block",
            "**Contributors**\n" + ", ".join(_contrib(c) for c in contributors))
    elif data.get("player_name"):
        put("completed_by_line", f"**Completed by** `{data['player_name']}`")

    # Team standing block. A bingo completion summarizes the team's board
    # (total tiles / total points / position); every other kind keeps the
    # simple running team total. Mutually exclusive so only one line renders.
    tiles = data.get("tiles_completed")
    rank, tcount = data.get("team_rank"), data.get("team_count")
    score = data.get("team_score")
    if notification_type in _SWEEP_TYPES:
        pass  # sweep types compose their own standing line below
    elif tiles is not None or rank is not None:
        parts = []
        if tiles is not None:
            parts.append(f"**Total tiles completed** `{int(tiles)}`")
        if score is not None:
            parts.append(f"**Total points earned** `{int(score)} pts`")
        if rank and tcount:
            parts.append(f"**Team position** #{int(rank)}/{int(tcount)} teams")
        if parts:
            context["bingo_stats"] = "\n".join(parts)
    elif score is not None:
        context["team_total_line"] = f"**Team total** `{int(score)} pts`"

    # Board game (web44a/web53a): dice + movement + wallet tokens for the
    # event_board_turn / event_board_roll_prompt layouts. Explicit context
    # assignment where 0 is a legitimate value (tile 0, an empty wallet) —
    # put() would drop it and take the whole line with it.
    dice = data.get("dice") or []
    put("dice_str", data.get("dice_str")
        or (" + ".join(str(d) for d in dice) if dice else None))
    if data.get("won"):
        context["board_won"] = True  # flips the layout to event_board_win
    for key in ("tile_from", "tile_to", "turn", "coin_balance"):
        if data.get(key) is not None:
            context[key] = data[key]
    put("next_task_label", data.get("next_task_label"))
    put("coins_awarded", data.get("coins_awarded"))
    if notification_type == "event_board_roll_prompt" and data.get("player_name"):
        context["roll_thanks_line"] = f" (thanks **{data['player_name']}**)"
    elif notification_type == "event_board_roll_prompt":
        context["roll_thanks_line"] = ""

    if notification_type in ("event_signup_prompt", "event_signup_closed"):
        context["signup_instructions"] = {
            "self_join": "Pick your account, then choose your team.",
            "auto_assign": "Pick your account — you'll be placed on a team automatically.",
            "signup_pool": "Pick your account to join the sign-up pool; "
                           "admins will sort teams later.",
        }.get(data.get("formation_mode"), "Pick your account to enter.")
        context.setdefault("description", "This event is open for sign-ups!")
        # When the window shuts (web70a). Falls back to the event's end so a
        # legacy payload (queued before this field existed) still renders its
        # deadline line instead of dropping it.
        put("signup_close_at",
            _fmt_ts(data.get("signup_close_at") or data.get("ends_at")))
        # Set by the retire sweep only — flips the whole message to the closed
        # layout in render_event_components.
        if data.get("signups_closed"):
            context["signups_closed"] = True
            put("signup_closed_line", data.get("signup_closed_line"))

    # Loot Sweep verbosity messages (services.event_engine loot_sweep enrichment).
    # Compose the enriched detail lines here so an absent field drops only its
    # own line under the layout's per-line token-drop rule.
    if notification_type in _SWEEP_TYPES:
        item = data.get("received_item")
        qty = int(data.get("received_qty") or 0)
        put("received_display",
            f"{qty}× {item}" if item and qty and qty != 1 else item)
        put("group_label", data.get("group_label"))
        if data.get("progress") is not None:
            context["sweep_set_total_line"] = (
                f"-# Set running total `{fmt_pts(data['progress'])} pts`")
        standing = []
        if data.get("team_score") is not None:
            standing.append(f"**Team total** `{fmt_pts(data['team_score'])} pts`")
        if data.get("team_rank") and data.get("team_count"):
            standing.append(f"`#{int(data['team_rank'])}/{int(data['team_count'])}`")
        if standing:
            context["sweep_standing_line"] = " • ".join(standing)
        # Contributors, always medal-ranked (even a solo finisher), overriding
        # the generic completion path so group/set posts read consistently.
        contrib = sweep_contributor_lines(data.get("contributors"))
        if contrib:
            context["contributors_block"] = "**Contributors**\n" + contrib

        if notification_type == "event_sweep_item":
            context["received_points"] = fmt_pts(data.get("received_points"))
            if data.get("npc"):
                context["sweep_npc_line"] = f"-# from **{data['npc']}**"
            scored, mx = data.get("item_scored"), data.get("item_max")
            if scored is not None and mx:
                nxt = data.get("next_receipt_points")
                tail = (f" • next `+{fmt_pts(nxt)}`" if nxt not in (None, 0, 0.0)
                        else " • fully farmed")
                context["sweep_copies_line"] = (
                    f"-# `{int(scored)}/{int(mx)}` scoring copies{tail}")
            gl, gh, gn = data.get("group_label"), data.get("group_have"), data.get("group_need")
            if gl and gh is not None and gn:
                context["sweep_group_progress_line"] = (
                    f"**{gl}** {text_progress_bar(gh, gn)} `{int(gh)}/{int(gn)} items`")
            if data.get("player_name"):
                context["sweep_by_line"] = f"-# by **{data['player_name']}**"
        else:  # group / set completion
            context["sweep_bonus"] = fmt_pts(data.get("bonus_points"))
            n = int(data.get("completion_n") or 1)
            if notification_type == "event_sweep_group" and n > 1:
                context["sweep_group_again_line"] = (
                    f"-# Completed ×{n} — the bonus decays each time")
            if notification_type == "event_sweep_set":
                context["sweep_set_again_suffix"] = f" (×{n})" if n > 1 else ""

    return context
