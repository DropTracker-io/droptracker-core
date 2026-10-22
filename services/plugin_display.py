"""Server-composed display for in-game event notifications.

The plugin used to turn every typed envelope into chat lines and pop-up cards
itself: one hand-written renderer per type, plus a pass that filled the card's
empty corners from its cached ``/event_state``. That was ~4k of the plugin's
200k-token plugin-hub budget, so the composition now happens here and the
envelope carries the result as ``display`` (see :func:`compose`). Older
plugins ignore the field and keep rendering ``data`` themselves; ``data`` is
unchanged and stays on every envelope.

What still holds from the original safety contract
(docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md):

* **Plain text in fixed slots.** No markup, colours, format strings or links.
  Every string is tag-stripped and length-capped here, and again in the
  plugin. Colour is a ``tone`` from a closed set the plugin maps to its own
  palette; layout is a card ``kind`` from a closed set the plugin draws.
* **Images** are an item id (the client draws the sprite) or an
  ``/img``-relative path the plugin anchors to its own hardcoded host.
* **Nothing executes.** A display the plugin cannot read is dropped.

What moved: wording, and the per-team context ("Your team: In the lead!",
"Behind by: 1.2M pts"). That context depends on who is reading, so
:func:`compose` is called once per recipient team at fan-out time with a
``ctx`` from :func:`team_contexts`. Time left is the one thing that must be
computed at display time, so it travels as ``ends_at`` and the plugin writes
the "2d 4h left" note itself.

Formatting mirrors the plugin's ``ValueFormat`` exactly (Java HALF_UP
rounding), so a server-composed card reads the same as a client-composed one.

Pure and stdlib-only at module level, like plugin_notifications: the unit
tests load this file directly.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

DISPLAY_VERSION = 1

# Matches the plugin's own cap (EventNotificationService.MAX_TEXT_LENGTH).
MAX_TEXT = 120
MAX_SHORT = 24

# Chat accents. The plugin maps each to a colour from its palette; an unknown
# tone renders uncoloured there.
TONE_COMPLETE = "complete"
TONE_TILE = "tile"
TONE_LEAD = "lead"
TONE_LINE = "line"
TONE_BLACKOUT = "blackout"
TONE_ACTION = "action"
TONE_INFO = "info"
TONE_MUTED = "muted"

# Card kinds: EventPopupCard.Kind on the plugin, by name.
KIND_COMPLETE = "COMPLETE"
KIND_TILE = "TILE"
KIND_PROGRESS = "PROGRESS"
KIND_LEAD = "LEAD"
KIND_LINE = "LINE"
KIND_BLACKOUT = "BLACKOUT"
KIND_STARTED = "STARTED"
KIND_ENDED = "ENDED"
KIND_BOARD = "BOARD"
KIND_ROLL = "ROLL"

# Kinds about a team rather than a task, so the team's icon fits.
_TEAM_NEWS = {KIND_LEAD, KIND_LINE, KIND_BLACKOUT, KIND_STARTED, KIND_ENDED, KIND_ROLL}

_TAG_RE = re.compile(r"<[^>]*>")
_SLOTS = (("leftLabel", "leftValue"), ("rightLabel", "rightValue"),
          ("left2Label", "left2Value"), ("right2Label", "right2Value"))


# --------------------------------------------------------------------------- #
# Text helpers (ports of the plugin's)
# --------------------------------------------------------------------------- #
def clean(value, max_length: int = MAX_TEXT):
    """Tag-strip and cap one user- or event-supplied string; None if empty."""
    if value is None:
        return None
    stripped = _TAG_RE.sub("", str(value)).replace(" ", " ").strip()
    if not stripped:
        return None
    return stripped if len(stripped) <= max_length else stripped[: max_length - 1] + "…"


def _trim(value: float, decimals: int) -> str:
    # Java's String.format("%.Nf") rounds HALF_UP on the double's shortest
    # decimal form (12.35 -> "12.4"), not on its exact binary value.
    out = str(Decimal(repr(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))
    while "." in out and (out.endswith("0") or out.endswith(".")):
        out = out[:-1]
    return out


def abbrev(value) -> str:
    """ValueFormat.abbrev: 10000000 → "10M", 1250000 → "1.25M", 813636 → "813.6K"."""
    value = int(value)
    a = abs(value)
    if a >= 1_000_000_000:
        return _trim(value / 1_000_000_000.0, 2) + "B"
    if a >= 1_000_000:
        return _trim(value / 1_000_000.0, 2) + "M"
    if a >= 10_000:
        return _trim(value / 1_000.0, 1) + "K"
    return f"{value:,}"


def ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _int(value):
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def _pos(value):
    n = _int(value)
    return n if n is not None and n > 0 else None


# --------------------------------------------------------------------------- #
# The card: the same slots as the plugin's EventPopupCard
# --------------------------------------------------------------------------- #
class _Card(dict):
    def __init__(self, kind, caption, headline):
        super().__init__(kind=kind, caption=clean(caption), headline=clean(headline))

    def detail(self, value):
        self["detail"] = clean(value)
        return self

    def slot(self, index, label, value):
        if value is not None:
            self[_SLOTS[index][0]] = label
            self[_SLOTS[index][1]] = clean(value, MAX_SHORT * 2)
        return self

    def left(self, label, value):
        return self.slot(0, label, value)

    def right(self, label, value):
        return self.slot(1, label, value)

    def left2(self, label, value):
        return self.slot(2, label, value)

    def extra(self, label, value):
        """Fills the first empty stat slot, like EventPopupCard.extra."""
        if value is None:
            return self
        for index, (_, value_key) in enumerate(_SLOTS):
            if self.get(value_key) is None:
                return self.slot(index, label, value)
        return self

    def progress(self, have, need):
        have, need = _int(have), _int(need)
        if have is not None and need is not None and need > 0:
            self["have"], self["need"] = have, need
        return self

    def has_right_label(self, prefix):
        label = self.get("rightLabel")
        return label is not None and label.startswith(prefix)


def _display(title, text, tag, tone, card=None, emphasis=None, icon_item_id=None, toast=True):
    return {
        "v": DISPLAY_VERSION,
        "title": clean(title),
        "text": clean(text, MAX_TEXT * 2),
        "chat": {"tag": tag, "tone": tone, "emphasis": clean(emphasis)},
        "toast": toast,
        "icon_item_id": _pos(icon_item_id),
        "card": card,
    }


# --------------------------------------------------------------------------- #
# Per-type wording (port of EventNotificationService.renderTyped)
# --------------------------------------------------------------------------- #
def _qty_suffix(data) -> str:
    if data.get("points_based") is True:
        return ""
    qty = _int(data.get("received_qty"))
    return f" ×{qty}" if qty is not None and qty > 1 else ""


def _tile_standing(data):
    parts = []
    tiles = _pos(data.get("tiles_completed"))
    if tiles:
        parts.append(plural(tiles, "tile"))
    rank, count = _pos(data.get("team_rank")), _pos(data.get("team_count"))
    if rank and count:
        parts.append(f"{ordinal(rank)} of {count}")
    return ", ".join(parts) if parts else None


def _short_standing(data):
    rank, count = _pos(data.get("team_rank")), _pos(data.get("team_count"))
    if rank and count:
        return f"{ordinal(rank)} of {count}"
    tiles = _pos(data.get("tiles_completed"))
    return plural(tiles, "tile") if tiles else None


def _base(notification_type, event_name, data):
    team = clean(data.get("team_name"))
    player = clean(data.get("player_name"))
    task = clean(data.get("task_label"))
    or_task = task or "a task"
    points = _pos(data.get("points"))

    if notification_type == "event_completion":
        who = player or team or "Your team"
        item = clean(data.get("received_item"))
        qty = _qty_suffix(data)
        if item:
            text = f"{who} received {item}{qty}, completing: {or_task}"
        else:
            text = f"{who} completed: {or_task}"
        if points:
            text += f" (+{abbrev(points)} pts)"
        raw_cells = [c for c in (clean(c) for c in (data.get("cell_labels") or [])) if c]
        filled = bool(raw_cells) or bool(data.get("cell_idxs"))
        # A cell usually inherits its task's label; naming it twice reads badly.
        cells = [c for c in raw_cells if not task or c.lower() != task.lower()]
        if cells:
            text += f" — tile: {cells[0]}" + (f" +{len(cells) - 1}" if len(cells) > 1 else "")
        if filled:
            standing = _tile_standing(data)
            if standing:
                text += f" — {standing}"
        manual = data.get("source_type") == "manual"
        if manual:
            text += " (manual award)"
        short = _short_standing(data) if filled else None
        card = (_Card(KIND_TILE if filled else KIND_COMPLETE,
                      "Tile complete" if filled else "Task complete",
                      cells[0] if cells else or_task)
                .detail(f"{item}{qty}" if item else (task if cells else event_name))
                .left("Obtained by:" if item else "Completed by:", who))
        if short:
            card.right("Standing:", short)
        else:
            card.right("Points:", f"+{abbrev(points)}" if points else None)
        card.progress(data.get("progress"), data.get("target"))
        if manual and card.get("rightValue") is None:
            card.right("Credit:", "Manual")
        return _display("Tile complete!" if filled else "Task complete!", text,
                        "TILE COMPLETE" if filled else "COMPLETE",
                        TONE_TILE if filled else TONE_COMPLETE,
                        card, emphasis=item, icon_item_id=data.get("icon_item_id"))

    if notification_type == "event_task_progress":
        who = player or "A teammate"
        item = clean(data.get("received_item"))
        qty = _qty_suffix(data)
        have, need, pct = _int(data.get("progress")), _int(data.get("target")), _int(data.get("milestone_pct"))
        if have is not None and need is not None:
            progress = f"{abbrev(have)}/{abbrev(need)}"
        elif pct is not None:
            progress = f"{pct}%"
        else:
            progress = None
        suffix = f" ({progress})" if progress else ""
        text = (f"{who} received {item}{qty}, progressing {or_task}{suffix}" if item
                else f"{who} progressed {or_task}{suffix}")
        card = (_Card(KIND_PROGRESS, "Task progress", or_task)
                .detail(f"{item}{qty}" if item else event_name)
                .left("Obtained by:" if item else "Progressed by:", who)
                .right("Progress:", progress)
                .progress(have, need))
        if have is None and pct is not None:
            card.progress(pct, 100)
        return _display("Task progress", text, "PROGRESS", TONE_MUTED, card,
                        icon_item_id=data.get("icon_item_id"))

    if notification_type == "event_lead_change":
        leader = team or "A team"
        score = _int(data.get("team_score"))
        text = f"{leader} took the lead" + (f" ({abbrev(score)} pts)" if score is not None else "")
        text += (f" in {event_name}" if event_name else "") + "!"
        card = (_Card(KIND_LEAD, "New leader", leader).detail(event_name)
                .right("Score:", f"{abbrev(score)} pts" if score is not None else None))
        return _display("Lead change!", text, "LEAD CHANGE", TONE_LEAD, card, emphasis=team)

    if notification_type in ("event_started", "event_ended", "event_window_opened", "event_window_closed"):
        name = event_name or "Your event"
        kind, title, suffix, tag, tone, detail = {
            "event_started": (KIND_STARTED, "Event started", " has started!", "EVENT STARTED",
                              TONE_ACTION, "Good luck, and have fun!"),
            "event_ended": (KIND_ENDED, "Event ended", " has ended.", "EVENT ENDED",
                            TONE_INFO, "Thanks for playing!"),
            # Recurring schedules (web82a); plugins before server display
            # never rendered these at all.
            "event_window_opened": (KIND_STARTED, "Scoring open", ": scoring is open!", "SCORING OPEN",
                                    TONE_ACTION, "Submissions count again."),
            "event_window_closed": (KIND_ENDED, "Scoring paused", ": scoring is paused.",
                                    "SCORING PAUSED", TONE_INFO, "Submissions won't count until it reopens."),
        }[notification_type]
        return _display(title, name + suffix, tag, tone, _Card(kind, title, name).detail(detail),
                        emphasis=event_name)

    if notification_type in ("event_line", "event_blackout"):
        who = team or "Your team"
        bonus = _int(data.get("bonus_points"))
        bonus_text = f" (+{abbrev(bonus)} pts)" if bonus is not None else ""
        line = notification_type == "event_line"
        card = (_Card(KIND_LINE if line else KIND_BLACKOUT, "Bingo line" if line else "Blackout!", who)
                .detail(event_name if line else "The whole board is complete")
                .right("Bonus:", f"+{abbrev(bonus)} pts" if bonus is not None else None))
        return _display("Bingo line!" if line else "Blackout!",
                        f"{who} completed a line{bonus_text}!" if line
                        else f"{who} blacked out the board{bonus_text}!",
                        "BINGO LINE" if line else "BLACKOUT",
                        TONE_LINE if line else TONE_BLACKOUT, card, emphasis=team)

    if notification_type == "event_board_turn":
        who = player or team or "Your team"
        dice = clean(data.get("dice_str"))
        tile_to = _int(data.get("tile_to"))
        next_task = clean(data.get("next_task_label"))
        text = f"{who} rolled" + (f" {dice}" if dice else "") + (f" to tile {tile_to}" if tile_to is not None else "")
        if next_task:
            text += f": {next_task}"
        rolled = None
        if dice:
            rolled = clean(dice, MAX_SHORT) + (f" > {tile_to}" if tile_to is not None else "")
        elif tile_to is not None:
            rolled = f"Tile {tile_to}"
        card = (_Card(KIND_BOARD, "Board roll",
                      next_task or (f"Tile {tile_to}" if tile_to is not None else "Moved on"))
                .detail(event_name).left("Rolled by:", who).right("Rolled:", rolled))
        return _display("Board roll", text, "BOARD", TONE_INFO, card)

    if notification_type == "event_board_roll_prompt":
        coins = _pos(data.get("coins_awarded"))
        card = (_Card(KIND_ROLL, "Your turn", "Roll the dice!").detail(event_name)
                .right("Coins:", f"+{abbrev(coins)}" if coins else None))
        return _display("Roll the dice!",
                        "Task complete — your team can roll the dice!"
                        + (f" (+{abbrev(coins)} coins)" if coins else ""),
                        "YOUR TURN", TONE_ACTION, card)
    return None


# --------------------------------------------------------------------------- #
# Reader context (port of EventNotificationService.enrich)
# --------------------------------------------------------------------------- #
def _enrich(display, notification_type, data, ctx):
    card = display.get("card")
    if not card or not ctx:
        return
    kind = card["kind"]
    own = ctx.get("own") or {}
    own_name = clean(own.get("name"))
    team = clean(data.get("team_name"))
    team_id = _int(data.get("team_id"))
    if own:
        about_own = (team_id == own.get("id")) if team_id is not None else (
            team is None or (own_name is not None and team.lower() == own_name.lower()))
    else:
        about_own = False
    task = _task_for(ctx.get("tasks") or {}, None if kind == KIND_BOARD else _int(data.get("task_id")),
                     clean(data.get("next_task_label")))

    # The picture, when the envelope named no item.
    if not display.get("icon_item_id"):
        if _pos(task.get("icon_item_id")):
            display["icon_item_id"] = _pos(task.get("icon_item_id"))
        elif task.get("icon_path"):
            card["iconPath"] = task["icon_path"]
        elif about_own and kind in _TEAM_NEWS:
            if _pos(own.get("icon_item_id")):
                display["icon_item_id"] = _pos(own.get("icon_item_id"))
            elif own.get("icon_path"):
                card["iconPath"] = own["icon_path"]

    score = _int(own.get("score"))
    own_score = f"{abbrev(score)} pts" if own and score is not None else None
    rank, count = _pos(own.get("rank")), _pos(own.get("team_count"))
    own_standing = f"{ordinal(rank)} of {count}" if rank and count and count > 1 else None
    total, done = _pos(ctx.get("tasks_total")), _int(ctx.get("tasks_completed"))
    tasks_done = f"{done or 0} / {total}" if total else None
    worth = f"{abbrev(task['points'])} pts" if _pos(task.get("points")) else None
    points = _pos(data.get("points"))
    team_score = _int(data.get("team_score"))
    time_left = ctx.get("time_left")

    if kind in (KIND_COMPLETE, KIND_TILE):
        if card.get("rightLabel") is not None and not card.has_right_label("Points") and points:
            card.extra("Points:", f"+{abbrev(points)}")
        card.extra("Team:", team or own_name)
        card.extra("Tiles done:" if ctx.get("has_bingo") else "Tasks done:", tasks_done)
        card.extra("Team score:", f"{abbrev(team_score)} pts" if team_score is not None
                   else own_score if about_own else None)
    elif kind == KIND_PROGRESS:
        card.extra("Team:", team or own_name)
        card.extra("Worth:", worth)
        have, need = _int(data.get("progress")), _int(data.get("target"))
        if have is not None and need is not None and need > have:
            card.extra("Still needed:", abbrev(need - have))
    elif kind == KIND_LEAD and own:
        card.left("Your team:", "In the lead!" if about_own else own_standing)
        if not about_own and team_score is not None and score is not None and score < team_score:
            card.left2("Behind by:", f"{abbrev(team_score - score)} pts")
        elif not about_own:
            card.left2("Your score:", own_score)
    elif kind in (KIND_LINE, KIND_BLACKOUT):
        card.extra("Standing:", _short_standing(data) or (own_standing if about_own else None))
        if about_own:
            card.extra("Team score:", own_score)
            card.extra("Tiles done:", tasks_done)
    elif kind == KIND_STARTED:
        card.extra("Your team:", own_name)
        card.extra("Teams:", str(count) if count and count > 1 else None)
        card.extra("Tasks:", str(total) if total else None)
        card.extra("Ends in:", time_left)
    elif kind == KIND_ENDED:
        card.extra("Your team:", own_name)
        card.extra("Final place:", own_standing)
        card.extra("Score:", own_score)
        card.extra("Tasks done:", tasks_done)
    elif kind == KIND_BOARD:
        card.extra("Worth:", worth)
        balance = _int(data.get("coin_balance"))
        card.extra("Coins:", abbrev(balance) if balance is not None else None)
    elif kind == KIND_ROLL:
        card.extra("Your team:", own_name)
        balance = _int(data.get("coin_balance"))
        card.extra("Coin balance:", abbrev(balance) if balance is not None else None)
        card.extra("Standing:", own_standing)


def _task_for(tasks, task_id, label):
    """The task an envelope is about: by id, else (board rolls) by the next task's label."""
    if task_id is not None and task_id in tasks:
        return tasks[task_id]
    if label:
        for task in tasks.values():
            if (clean(task.get("label")) or "").lower() == label.lower():
                return task
    return {}


def compose(notification_type, event, data, ctx=None):
    """The ``display`` block for one envelope, read from one team's side.

    ``ctx`` (optional) is that team's context from :func:`team_contexts`.
    Returns None for types with no display (the plugin then drops the
    envelope, exactly as it drops an unknown type). Never raises: a bug here
    must not cost anyone their notification, so it degrades to the
    context-free composition, then to None.
    """
    data = data or {}
    event_name = clean((event or {}).get("name")) if isinstance(event, dict) else None
    try:
        display = _base(notification_type, event_name, data)
    except Exception as e:
        print(f"[plugin_display] compose failed for {notification_type}: {e}")
        return None
    if display is None:
        return None
    try:
        _enrich(display, notification_type, data, ctx)
    except Exception as e:
        print(f"[plugin_display] enrich failed for {notification_type}: {e}")
    if ctx and ctx.get("ends_at") and display["card"] and display["card"]["kind"] not in (KIND_STARTED, KIND_ENDED):
        display["ends_at"] = ctx["ends_at"]
    display["card"] = dict(display["card"]) if display["card"] else None
    return display


def time_left(ends_at, now) -> str:
    """"2d 4h" / "3h 12m" / "5m" until ``ends_at`` (aware or naive-UTC
    datetime), or None once past. Mirrors the plugin's timeLeft; used for the
    "Ends in:" corner on start cards, which the plugin cannot recompute."""
    if ends_at is None:
        return None
    seconds = int((ends_at - now).total_seconds())
    if seconds <= 0:
        return None
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 1)}m"
