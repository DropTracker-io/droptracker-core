"""The plugin's event window (``/plugin/events...``).

The RuneLite side panel shows the events a player is on a team of; the event
window opens any of them, plus the events run by the player's groups, in a
pop-out that aims to show what the event page on droptracker.io shows.

Pages are composed here from the website's own read models (``_detail``, the
players leaderboard, the completion history, the SOTW/BOTW standings) and sent
as a list of blocks from a closed set the plugin draws itself:

    heading  {text}
    text     {text, tone?}
    stats    {items: [{label, value, tip?}]}
    table    {columns: [{label, width?, right?}], rows: [{cells, color?, page?,
             highlight?, tip?, icon_item_id?}], empty?}
    tasks    {items: [{label, detail?, tip?, icon_item_id?, points?, page?,
             bars: [{label, color?, have, need, done, text}]}], empty?}
    board    {team_id?}   (the plugin loads board.png for that team's view)

Same rules as the pop-up cards and panel cards: plain text only (tags are
stripped and lengths capped), colours are a team's own hex, tones come from a
closed set (only ``muted`` today), and the only action is ``page``, which
opens another page of the same window (``team:<id>``, ``board:<id>``,
``task:<id>``). Nothing here is markup.

Who may look: anyone on the event's roster, the event's admins, and members of
a group taking part (the owning group, or an accepted clan of a clan-vs-clan
event). Everyone else gets the same 404 as a missing event. What a page shows
follows the website's rules for that viewer: hidden tasks stay hidden, hidden
players stay masked, effort stays with admins when the event says so.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta

from quart import Blueprint, jsonify, request
from quart_rate_limiter import rate_limit

from api.core import get_db_session

plugin_events_bp = Blueprint("plugin_events", __name__)


# services.plugin_display is imported lazily, like every api route imports
# services/ (the api package has a services/ directory of its own).
def clean(value, max_length=120):
    from services.plugin_display import clean as _clean
    return _clean(value, max_length)


def abbrev(value):
    from services.plugin_display import abbrev as _abbrev
    return _abbrev(value)


def ordinal(n):
    from services.plugin_display import ordinal as _ordinal
    return _ordinal(n)


def plural(count, noun):
    from services.plugin_display import plural as _plural
    return _plural(count, noun)


def time_left(ends_at, now):
    from services.plugin_display import time_left as _time_left
    return _time_left(ends_at, now)

#: Ended events stay in the list this long.
RECENT_PAST = timedelta(days=7)
LIST_LIMIT = 30
PAGE_CACHE_SECONDS = 15
TASKS_LIMIT = 150
TEAMS_PER_TASK = 12
ROWS_LIMIT = 100
HISTORY_LIMIT = 50

KIND_LABELS = {
    "standard": "Event", "bingo": "Bingo", "board_game": "Board Game",
    "loot_sweep": "Loot Sweep", "conquest": "Conquest",
    "sotw": "Skill of the Week", "botw": "Boss of the Week",
}
COMPETITION_KINDS = ("sotw", "botw")
#: Kinds whose task list the website replaces with their own board view.
BOARD_ONLY_KINDS = ("loot_sweep", "conquest") + COMPETITION_KINDS

NOT_FOUND = "That event isn't available to this account."


class _Missing(Exception):
    pass


# --------------------------------------------------------------------------- #
# Identity and access
# --------------------------------------------------------------------------- #
def _player(s, player_name, acc_hash):
    """The Player behind the plugin's identity, hash first (like every plugin
    endpoint), or None."""
    from db import Player

    if not player_name or not acc_hash:
        return None
    player = s.query(Player).filter(Player.account_hash == str(acc_hash)).first()
    if player is None:
        player = (s.query(Player)
                  .filter(Player.player_name == player_name,
                          Player.account_hash == str(acc_hash))
                  .first())
    return player


def _group_ids(s, player) -> set:
    """Groups this account belongs to: its own membership rows, plus its site
    user's (the website's member set), when it has one."""
    from db.models.associations import user_group_association as uga
    from web_api.routes.events import _member_group_ids

    gids = {gid for (gid,) in s.query(uga.c.group_id)
            .filter(uga.c.player_id == player.player_id).all() if gid is not None}
    # user_id 0 is a real account: never truth-test it.
    if player.user_id is not None:
        gids |= _member_group_ids(s, int(player.user_id))
    return gids


def _team_of(s, event_id: int, player_id: int):
    """The EventTeam this player is rostered on, or None."""
    from db.models import EventTeam, EventTeamMember

    return (s.query(EventTeam)
            .join(EventTeamMember, EventTeamMember.team_id == EventTeam.id)
            .filter(EventTeam.event_id == event_id,
                    EventTeamMember.player_id == player_id)
            .first())


def can_view(s, ev, player, user_id, group_ids=None) -> bool:
    """Roster, event admin, or a member of a group taking part."""
    from web_api.routes.events import _is_event_admin, participating_group_ids

    if _team_of(s, ev.id, player.player_id) is not None:
        return True
    if user_id is not None and _is_event_admin(s, user_id, ev):
        return True
    gids = _group_ids(s, player) if group_ids is None else group_ids
    return bool(participating_group_ids(s, ev) & gids)


def _user_id(player):
    return int(player.user_id) if player.user_id is not None else None


# --------------------------------------------------------------------------- #
# Wording
# --------------------------------------------------------------------------- #
def _when(ev, status, now) -> str | None:
    if status == "draft":
        left = time_left(ev.starts_at, now) if ev.starts_at else None
        return f"Starts in {left}" if left else "Upcoming"
    if status == "past":
        ended = ev.ended_at or ev.ends_at
        if ended is None:
            return "Ended"
        days = max((now - ended).days, 0)
        return "Ended today" if days == 0 else f"Ended {plural(days, 'day')} ago"
    left = time_left(ev.ends_at, now) if ev.ends_at else None
    return f"Ends in {left}" if left else None


def _subtitle(ev, status, now) -> str:
    kind = KIND_LABELS.get(ev.kind or "standard", "Event")
    when = _when(ev, status, now)
    return f"{kind}  ·  {when}" if when else kind


def _num(value) -> str:
    """Scores and points: whole numbers with commas, else up to 2 decimals."""
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if f.is_integer():
        return f"{int(f):,}"
    return f"{f:,.2f}".rstrip("0").rstrip(".")


def _date(dt) -> str | None:
    return dt.strftime("%d %b %Y, %H:%M") if dt else None


def _ago(unix, now) -> str:
    if not unix:
        return ""
    seconds = int(now.timestamp()) - int(unix)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _color(value):
    value = (value or "").strip()
    return value if len(value) == 7 and value.startswith("#") else None


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #
def heading(text):
    return {"type": "heading", "text": clean(text, 60) or ""}


def text_block(text, tone=None, max_length=600):
    block = {"type": "text", "text": clean(text, max_length) or ""}
    if tone:
        block["tone"] = tone
    return block


def stats(items):
    return {"type": "stats", "items": [
        {k: v for k, v in (("label", clean(label, 24) or ""), ("value", clean(value, 32) or "—"),
                           ("tip", clean(tip, 200))) if v is not None}
        for label, value, tip in items]}


def column(label, width=1, right=False):
    col = {"label": label, "width": width}
    if right:
        col["right"] = True
    return col


def row(cells, *, color=None, page=None, team_id=None, tip=None, cap=48, icon_item_id=None):
    out = {"cells": [clean(c, cap) or "" for c in cells]}
    if isinstance(icon_item_id, int) and icon_item_id > 0:
        out["icon_item_id"] = icon_item_id
    if _color(color):
        out["color"] = _color(color)
    if page:
        out["page"] = page
    if tip:
        out["tip"] = clean(tip, 200)
    if team_id is not None:
        out["_team"] = int(team_id)  # highlight marker; resolved per viewer
    return out


def table(columns, rows, empty="Nothing here yet."):
    return {"type": "table", "columns": columns, "rows": rows[:ROWS_LIMIT], "empty": empty}


# --------------------------------------------------------------------------- #
# Page composition
# --------------------------------------------------------------------------- #
def _need(task, cell) -> int:
    """Completion threshold, as the website's EventTaskBoard reads it."""
    if cell and isinstance(cell.get("target"), (int, float)):
        return max(int(cell["target"]), 1)
    if task.get("type") == "skill_target":
        return 1
    if task.get("type") == "pb_target":
        cfg = task.get("config") if isinstance(task.get("config"), dict) else {}
        if cfg.get("mode") == "whole_team":
            return 1
        need = cfg.get("need")
        return max(int(need), 1) if isinstance(need, (int, float)) else 1
    return max(int(task.get("target_value") or 0), 1)


def _bar(task, cell, label, color):
    need = _need(task, cell)
    progress = int((cell or {}).get("progress") or 0)
    done = bool((cell or {}).get("completed"))
    laps = (cell or {}).get("completions")
    have = progress
    if isinstance(laps, int) and not done:
        have = max(progress - laps * need, 0)
    have = min(have, need)
    if done:
        text = "Done"
    elif need == 1:
        text = "Not yet"
    else:
        text = f"{abbrev(have)} / {abbrev(need)}"
    if isinstance(laps, int) and laps > 0:
        text += f"  ×{laps}"
    return {"label": clean(label, 24) or "", "color": _color(color), "have": have,
            "need": need, "done": done, "text": text}


#: describe_task's requirement cap (services.plugin_notifications).
REQUIREMENTS_LIMIT = 12
#: Team columns on a task's checklist: the viewer's team, then the leaders.
TASK_TEAM_COLUMNS = 4
DONE_COLOR = "#4caf50"


def _needs_line(reqs) -> str | None:
    """"Counts: Dragon axe, 3× Bones, ..." from describe_task's requirement
    list, so a card says which items move it without opening it."""
    names = []
    for r in reqs or []:
        name = clean(r.get("name"), 40)
        if not name:
            continue
        qty = r.get("quantity")
        names.append(f"{abbrev(qty)}× {name}" if isinstance(qty, int) and qty > 1 else name)
    if not names:
        return None
    line, shown = "Counts: ", 0
    for name in names:
        if shown and len(line) + len(name) > 100:
            break
        line += (", " if shown else "") + name
        shown += 1
    if len(reqs) >= REQUIREMENTS_LIMIT:
        line += " and more"  # describe_task caps the list, so no count
    elif shown < len(names):
        line += f" +{len(names) - shown} more"
    return line


def _task_items(detail, team_ids=None):
    """One ``tasks`` item per task with a bar per team (or just ``team_ids``)."""
    from services.plugin_notifications import describe_task

    teams = [t for t in detail["teams"] if team_ids is None or t["id"] in team_ids]
    cells = {(p["task_id"], p["team_id"]): p for p in detail.get("progress") or []}
    items = []
    for task in detail["tasks"][:TASKS_LIMIT]:
        if task.get("managed"):
            continue
        tile = task.get("tile") or {}
        description, reqs = describe_task(task)
        item = {"label": clean(task.get("label"), 80) or "Task",
                "page": f"task:{task['id']}",
                "bars": []}
        # The "Counts: ..." line rides in detail, which every client draws.
        detail_bits = [str(b) for b in (tile.get("badge"), tile.get("value"), _needs_line(reqs)) if b]
        if detail_bits:
            item["detail"] = clean("  ·  ".join(detail_bits), 140)
        if description:
            item["tip"] = clean(description, 200)
        if task.get("points"):
            item["points"] = plural(int(task["points"]), "pt")
        icon = tile.get("icon_item_id")
        if isinstance(icon, int):
            item["icon_item_id"] = icon
        shown = teams
        if len(teams) > TEAMS_PER_TASK:
            # Busiest teams first so a 30-team event still fits on screen.
            shown = sorted(teams, key=lambda t: -int(
                (cells.get((task["id"], t["id"])) or {}).get("progress") or 0))[:TEAMS_PER_TASK]
        for team in shown:
            bar = _bar(task, cells.get((task["id"], team["id"])), team["name"], team.get("color"))
            bar["_team"] = team["id"]
            item["bars"].append(bar)
        items.append(item)
    return items


def team_colors(teams) -> dict:
    """{team_id: hex}: the team's own colour, else the site palette entry for
    its place in roster (id) order, as the event page shows it."""
    from services.event_team_discord import effective_team_color

    ordered = sorted(teams, key=lambda t: t["id"])
    return {t["id"]: _color(effective_team_color(t.get("color"), i))
            for i, t in enumerate(ordered)}


def _ranked_teams(detail):
    return sorted(detail["teams"], key=lambda t: (-float(t.get("score") or 0), t["id"]))


def _standings_rows(detail, with_members=True):
    rows = []
    for rank, team in enumerate(_ranked_teams(detail), start=1):
        cells = [ordinal(rank), team["name"], _num(team.get("score"))]
        if with_members:
            cells.append(str(team.get("member_count") or 0))
        rows.append(row(cells, color=team.get("color"), page=f"team:{team['id']}",
                        team_id=team["id"], tip="Open this team"))
    return rows


def _overview(s, ev, detail, now):
    from web_api.routes.events import _effective_status

    status = _effective_status(ev)
    blocks = []
    if detail.get("description"):
        blocks.append(text_block(detail["description"]))
    if detail.get("tasks_hidden"):
        blocks.append(text_block("The organisers are keeping this event's tasks to "
                                 "themselves for now. Scores and standings still update.", "muted"))
    status_word = {"draft": "Upcoming", "past": "Ended"}.get(status, "Live")
    items = [("Status", status_word, _when(ev, status, now))]
    if ev.starts_at:
        items.append(("Starts", _date(ev.starts_at), None))
    if ev.ends_at:
        items.append(("Ends", _date(ev.ends_at), None))
    teams = detail["teams"]
    members = sum(int(t.get("member_count") or 0) for t in teams)
    if teams and (ev.kind or "standard") not in COMPETITION_KINDS:
        items.append(("Teams", str(len(teams)), None))
    items.append(("Players", f"{members:,}", None))
    if not detail.get("tasks_hidden") and detail["tasks"]:
        items.append(("Tasks", str(len([t for t in detail["tasks"] if not t.get("managed")])), None))
    blocks.append(stats(items))
    pot = detail.get("prize_pot") or {}
    if pot.get("enabled"):
        total = pot.get("total")
        amount = total.get("value") if isinstance(total, dict) else total
        blocks.append(stats([("Prize pot", abbrev(int(amount or 0)) + " GP", None)]))
    if detail.get("schedule_summary"):
        blocks.append(text_block("Schedule: " + detail["schedule_summary"], "muted"))
    if (ev.kind or "standard") in COMPETITION_KINDS:
        blocks += _competition_blocks(s, ev, limit=10)
    elif teams:
        blocks.append(heading("Standings"))
        blocks.append(table([column("#", 1), column("Team", 5), column("Score", 2, True)],
                            _standings_rows(detail, with_members=False),
                            "No teams yet."))
    return blocks


def _tasks_page(detail):
    if detail.get("tasks_hidden"):
        return [text_block("The organisers are keeping this event's tasks to themselves for now.",
                           "muted")]
    items = _task_items(detail)
    return [{"type": "tasks", "items": items, "empty": "No tasks yet."}]



# --------------------------------------------------------------------------- #
# One task: what counts, and who has what
# --------------------------------------------------------------------------- #
def _group_title(group) -> str:
    mode, need = group.get("mode"), int(group.get("need") or 0)
    count = f"{abbrev(group.get('obtained') or 0)} / {abbrev(need)}"
    if mode == "all_of":
        title = "All of these"
    elif mode == "points":
        title = f"Earn {abbrev(need)} points from these"
    elif mode == "count":
        title = "Collect"
    elif need > 1:
        title = f"Any {abbrev(need)} {'different ' if group.get('distinct') else ''}of these"
    else:
        title = "Any one of these"
    return f"{title}  ({count})"


def _item_cell(item, mode) -> str:
    """One team's state on one requirement row."""
    got, required = int(item.get("obtained") or 0), int(item.get("required") or 1)
    if mode == "points":
        return f"{abbrev(got)}×" if got else "-"
    if item.get("satisfied"):
        return "Done"
    if item.get("pending_satisfied") or item.get("pending"):
        return "Pending review"
    if required > 1:
        return f"{abbrev(got)} / {abbrev(required)}"
    return "-"


def _need_cell(item, mode) -> str:
    if mode == "points":
        return plural(int(item.get("points") or 0), "pt")
    required = int(item.get("required") or 1)
    return f"{abbrev(required)}×" if required > 1 else "1"


def _checklist_blocks(groups_by_team, teams, prefix=""):
    """Heading + table per requirement group: an item per row, a column per
    team. ``groups_by_team`` is aligned with ``teams`` (same task, same
    group order); the first team's state colours the row."""
    blocks = []
    first = groups_by_team[0] if groups_by_team else []
    for gi, group in enumerate(first):
        mode = group.get("mode")
        blocks.append(heading(prefix + _group_title(group)))
        cols = [column("Item", 5), column("Points" if mode == "points" else "Need", 1, True)]
        cols += [column(t["name"], 2, True) for t in teams]
        rows = []
        for ii, item in enumerate(group.get("items") or []):
            cells = [item.get("name") or "Item", _need_cell(item, mode)]
            for groups in groups_by_team:
                peer = groups[gi]["items"][ii] if gi < len(groups) and ii < len(groups[gi].get("items") or []) else {}
                cells.append(_item_cell(peer, mode))
            icon = item.get("icon") or {}
            rows.append(row(cells, color=DONE_COLOR if item.get("satisfied") else None,
                            icon_item_id=icon.get("id") if icon.get("type") == "item" else None,
                            tip=item.get("name")))
        blocks.append(table(cols, rows, "Nothing listed."))
    return blocks


def _path(breakdown, index) -> dict:
    paths = breakdown.get("paths") or []
    return paths[index] if index < len(paths) else {}


def _contributor_blocks(breakdown, team_name):
    rows = []
    for c in breakdown.get("contributors") or []:
        bits = [f"{abbrev(i['quantity'])}× {i['name']}" if i.get("quantity", 1) > 1 else i["name"]
                for i in c.get("items") or [] if i.get("name")]
        what = ", ".join(bits) or c.get("note") or f"{abbrev(c.get('quantity') or 0)} counted"
        rows.append(row([c.get("player_name") or "Organiser", what], cap=120, tip=what))
    if not rows:
        return []
    return [heading(f"Who has helped ({team_name})"),
            table([column("Player", 2), column("Counted", 5)], rows)]


def _task_page(s, ev, detail, task_id, viewer_team):
    """``task:<id>``: the task card with every team's bar, then the website's
    item checklist (web_api.event_breakdown) for the viewer's team beside the
    leading teams, then who on the first team has added to it."""
    from db.models import EventTask, EventTeam
    from web_api.routes.events import team_task_breakdown

    if detail.get("tasks_hidden") or (ev.kind or "standard") in BOARD_ONLY_KINDS:
        raise _Missing()
    card = next((t for t in detail["tasks"] if t["id"] == task_id and not t.get("managed")), None)
    task = s.query(EventTask).filter(EventTask.id == task_id, EventTask.event_id == ev.id).first()
    if card is None or task is None:
        raise _Missing()

    item = _task_items({**detail, "tasks": [card]})[0]
    item.pop("page", None)
    # The checklist below names everything; keep just the tile's badge/value.
    bits = [str(b) for b in ((card.get("tile") or {}).get("badge"), (card.get("tile") or {}).get("value")) if b]
    item.pop("detail", None)
    if bits:
        item["detail"] = clean("  ·  ".join(bits), 60)
    blocks = [heading(item["label"]), {"type": "tasks", "items": [item]}]
    if item.get("tip"):
        # describe_task's "(+N more)" counts past its cap; every item is listed below.
        blocks.append(text_block(re.sub(r" \(\+\d+ more\)", "", item["tip"])))

    ranked = _ranked_teams(detail)
    first = next((t for t in ranked if viewer_team is not None and t["id"] == viewer_team.id), None)
    shown = ([first] if first else []) + [t for t in ranked if t is not first]
    shown = shown[:TASK_TEAM_COLUMNS]
    if not shown:
        blocks.append(text_block("Once teams are set up, each team's progress on this task shows here.",
                                 "muted"))
        return blocks
    rows = {t.id: t for t in s.query(EventTeam).filter(EventTeam.id.in_([t["id"] for t in shown]))}
    breakdowns = [team_task_breakdown(s, ev.id, task, rows[t["id"]]) for t in shown if t["id"] in rows]
    shown = [t for t in shown if t["id"] in rows]
    main = breakdowns[0]
    teams = [{"id": t["id"], "name": clean(t["name"], 16) or "Team"} for t in shown]
    if main.get("structure") == "checklist":
        blocks += _checklist_blocks([b.get("groups") or [] for b in breakdowns], teams)
    elif main.get("structure") == "paths":
        blocks.append(text_block("Finish any one of these to complete the task.", "muted"))
        for pi, path in enumerate(main.get("paths") or []):
            label = f"Option {pi + 1}: {path.get('label') or ''}".rstrip(": ")
            if path.get("groups"):
                blocks += _checklist_blocks([_path(b, pi).get("groups") or [] for b in breakdowns],
                                            teams, prefix=label + "  ·  ")
            else:
                cells = [label, f"{abbrev(path.get('need') or 0)} {path.get('unit') or ''}".strip()]
                for b in breakdowns:
                    peer = _path(b, pi)
                    cells.append(f"{abbrev(peer.get('got') or 0)} / {abbrev(peer.get('need') or 0)}")
                npcs = ", ".join(n["name"] for n in path.get("npcs") or [] if n.get("name"))
                blocks.append(heading(label))
                blocks.append(table([column("Goal", 5), column("Need", 1, True)]
                                    + [column(t["name"], 2, True) for t in teams], [row(cells)]))
                if npcs:
                    blocks.append(text_block("Counts at: " + npcs, "muted", max_length=300))
    blocks += _contributor_blocks(main, teams[0]["name"])
    return blocks

def _board_page(ev, detail, viewer_team, team_id=None):
    """The board image, with a row per team to switch to its view (``board:<id>``;
    ``board:0`` is the all-teams view). Starts on the viewer's own team."""
    if (ev.kind or "standard") in COMPETITION_KINDS or not detail["teams"]:
        return [{"type": "board"}]
    if team_id is None:
        team_id = viewer_team.id if viewer_team is not None else 0
    if team_id and team_id not in {t["id"] for t in detail["teams"]}:
        raise _Missing()
    rows = [row(["All teams"], page="board:0")]
    rows += [row([t["name"]], color=t.get("color"), page=f"board:{t['id']}")
             for t in _ranked_teams(detail)]
    for r, tid in zip(rows, [0] + [t["id"] for t in _ranked_teams(detail)]):
        if tid == team_id:
            r["highlight"] = True
            r.pop("page")
    return [{"type": "board", "team_id": team_id or None},
            table([column("Show the board for")], rows)]


def _teams_page(detail):
    return [table([column("#", 1), column("Team", 5), column("Score", 2, True),
                   column("Players", 2, True)],
                  _standings_rows(detail), "No teams yet.")]


def _competition_blocks(s, ev, limit=ROWS_LIMIT):
    from services.competition import CompetitionConfig, score_text, team_score_text
    from services.competition_setup import competition_task
    from web_api.routes.events import competition_payload

    data = competition_payload(s, ev)
    task = competition_task(s, ev.id)
    config = CompetitionConfig(task.config if task is not None else None)
    blocks = []
    if data.get("teams"):
        blocks.append(heading("Teams"))
        blocks.append(table(
            [column("#", 1), column("Team", 5), column("Score", 3, True)],
            [row([ordinal(i), t.get("name") or "Team",
                  t.get("score_text") or team_score_text(t.get("score") or 0, config)],
                 team_id=t.get("team_id"))
             for i, t in enumerate(data["teams"], start=1)]))
    totals = data.get("totals") or {}
    blocks.append(heading("Leaderboard"))
    rows = []
    for i, r in enumerate((data.get("standings") or [])[:limit], start=1):
        value = r.get("points") if config.ranking_mode == "points" else r.get("gained")
        rows.append(row([ordinal(int(r.get("rank") or i)), r.get("player_name") or "Player",
                         score_text(int(value or 0), config)]))
    blocks.append(table([column("#", 1), column("Player", 5), column("Score", 3, True)],
                        rows, "Nobody has gained anything yet."))
    if totals.get("participants"):
        blocks.append(text_block(plural(int(totals["participants"]), "participant"), "muted"))
    return blocks


def _players_page(s, ev, viewer_id, colors):
    from web_api.routes.events import event_players_payload

    data = event_players_payload(s, ev, viewer_id)
    rows = []
    for i, p in enumerate(data.get("players") or [], start=1):
        loot = p.get("loot_gp")
        loot_value = loot.get("value") if isinstance(loot, dict) else loot
        rows.append(row([ordinal(i), p.get("player_name") or "Player",
                         p.get("team_name") or "—", _num(p.get("points")),
                         f"{int(p.get('completions') or 0):,}",
                         abbrev(int(loot_value or 0))],
                        color=colors.get(p.get("team_id")), team_id=p.get("team_id")))
    totals = data.get("totals") or {}
    return [
        stats([("Contributors", f"{int(totals.get('contributors') or 0):,}", None),
               ("Completions", f"{int(totals.get('completions') or 0):,}", None),
               ("Points", _num(totals.get("points")), None)]),
        table([column("#", 1), column("Player", 4), column("Team", 3),
               column("Points", 2, True), column("Tasks", 2, True), column("Loot", 2, True)],
              rows, "Nobody has scored yet."),
    ]


def _history_page(s, ev, viewer_id, now, colors):
    from web_api.routes.events import completion_history_payload

    data = completion_history_payload(s, ev, viewer_id, mode="completions",
                                      page=1, limit=HISTORY_LIMIT)
    rows = []
    last_key = None
    for e in data.get("entries") or []:
        who = e.get("player_name") or "Someone"
        what = e.get("task_label") or "a task"
        line = f"{who} completed {what}"
        if e.get("matched_target"):
            line += f" ({e['matched_target']})"
        # A repeatable task credits one row per lap: fold a run of identical
        # lines into one with a count.
        key = (line, e.get("team_id"), e.get("points"))
        if key == last_key and rows:
            rows[-1]["_n"] = rows[-1].get("_n", 1) + 1
            continue
        last_key = key
        points = f"+{_num(e['points'])}" if e.get("points") else ""
        rows.append(row([line, e.get("team_name") or "", points, _ago(e.get("created_at"), now)],
                        color=colors.get(e.get("team_id")), team_id=e.get("team_id"),
                        tip=line, cap=120))
    for r in rows:
        n = r.pop("_n", 1)
        if n > 1:
            r["cells"][0] = clean(f"{r['cells'][0]}  ×{n}", 120)
    total = (data.get("meta") or {}).get("total") or 0
    shown = len(data.get("entries") or [])
    blocks = [table([column("Completed", 7), column("Team", 3), column("Points", 1, True),
                     column("When", 2, True)], rows, "Nothing completed yet.")]
    if total > shown:
        blocks.append(text_block(f"Showing the latest {shown} of {total:,}. "
                                 "The full history is on the website.", "muted"))
    return blocks


def _team_page(ev, detail, team_id, now):
    teams = _ranked_teams(detail)
    team = next((t for t in teams if t["id"] == team_id), None)
    if team is None:
        raise _Missing()
    rank = teams.index(team) + 1
    blocks = [heading(team["name"]),
              stats([("Standing", f"{ordinal(rank)} of {len(teams)}", None),
                     ("Score", _num(team.get("score")), None),
                     ("Players", str(team.get("member_count") or 0), None)])]
    members = team.get("members") or []
    show_effort = any("effort_ehb" in m for m in members)
    cols = [column("Player", 5), column("Role", 2)]
    if show_effort:
        cols.append(column("EHB", 2, True))
    rows = []
    for m in members:
        cells = [m.get("player_name") or "Player", (m.get("role") or "member").capitalize()]
        if show_effort:
            cells.append(_num(round(float(m.get("effort_ehb") or 0), 1)))
        rows.append(row(cells))
    blocks.append(heading("Roster"))
    blocks.append(table(cols, rows, "Nobody on this team yet."))
    if not detail.get("tasks_hidden") and (ev.kind or "standard") not in BOARD_ONLY_KINDS:
        blocks.append(heading("Tasks"))
        blocks.append({"type": "tasks", "items": _task_items(detail, {team_id}),
                       "empty": "No tasks yet."})
    return blocks


def _pages(ev, detail, is_admin):
    from services.event_board_image import board_kept_to_admins
    from web_api.routes.events import _effective_status

    kind = ev.kind or "standard"
    pages = [("overview", "Overview")]
    if kind not in BOARD_ONLY_KINDS:
        pages.append(("tasks", "Tasks"))
    visual = bool(ev.has_bingo) or kind in ("board_game",) + BOARD_ONLY_KINDS
    # board.png renders the public view, so a held-back board has no page.
    if visual and not board_kept_to_admins(ev):
        pages.append(("board", "Board" if kind not in COMPETITION_KINDS else "Snapshot"))
    if kind in COMPETITION_KINDS:
        pages.append(("standings", "Leaderboard"))
    elif detail["teams"]:
        pages.append(("standings", "Teams"))
    if kind not in COMPETITION_KINDS:
        pages.append(("players", "Players"))
    if _effective_status(ev) != "draft":
        pages.append(("history", "History"))
    return pages


def compose_page(s, ev, viewer_id, page: str, viewer_team, now=None) -> dict:
    """The window payload for one page (before the per-viewer highlight)."""
    from web_api.routes.events import _detail, _effective_status, _is_event_admin

    now = now or datetime.now()
    detail = _detail(s, ev, viewer_id=viewer_id)
    colors = team_colors(detail["teams"])
    for team in detail["teams"]:
        team["color"] = colors.get(team["id"])
    is_admin = bool(detail.get("can_manage")) or (
        viewer_id is not None and _is_event_admin(s, viewer_id, ev))
    pages = _pages(ev, detail, is_admin)
    keys = {k for k, _ in pages}
    if page.startswith(("team:", "board:", "task:")):
        prefix, _, raw = page.partition(":")
        try:
            team_id = int(raw)
        except ValueError:
            raise _Missing()
        if prefix == "team":
            blocks = _team_page(ev, detail, team_id, now)
        elif prefix == "task":
            blocks = _task_page(s, ev, detail, team_id, viewer_team)
        elif "board" in keys:
            blocks = _board_page(ev, detail, viewer_team, team_id)
        else:
            raise _Missing()
    elif page not in keys:
        page = "overview"
        blocks = _overview(s, ev, detail, now)
    elif page == "overview":
        blocks = _overview(s, ev, detail, now)
    elif page == "tasks":
        blocks = _tasks_page(detail)
    elif page == "board":
        blocks = _board_page(ev, detail, viewer_team)
    elif page == "standings":
        blocks = (_competition_blocks(s, ev) if (ev.kind or "standard") in COMPETITION_KINDS
                  else _teams_page(detail))
    elif page == "players":
        blocks = _players_page(s, ev, viewer_id, colors)
    else:
        blocks = _history_page(s, ev, viewer_id, now, colors)
    status = _effective_status(ev)
    return {
        "event": {"id": ev.id, "name": clean(ev.name, 80) or "Event",
                  "subtitle": _subtitle(ev, status, now), "status": status},
        "pages": [{"key": k, "label": label} for k, label in pages],
        "page": page,
        "blocks": blocks,
    }


def apply_viewer(payload: dict, viewer_team_id) -> dict:
    """Swap the internal ``_team`` markers for ``highlight`` on the viewer's
    own team's rows and bars. Runs after the shared cache."""
    def walk(node):
        if isinstance(node, dict):
            team = node.pop("_team", None)
            if team is not None and viewer_team_id is not None and team == viewer_team_id:
                node["highlight"] = True
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(payload)
    return payload


# --------------------------------------------------------------------------- #
# The event list
# --------------------------------------------------------------------------- #
def list_events(s, player, now=None) -> list:
    """Events this account may open: its own (rostered), then its groups'.
    Live first, then upcoming, then those that ended in the last week."""
    from sqlalchemy import or_

    from db import Group
    from db.models import Event, EventGroup, EventTeam, EventTeamMember
    from web_api.routes.events import _effective_status

    now = now or datetime.now()
    since = now - RECENT_PAST
    recent = or_(Event.status.in_(("draft", "active")), Event.ends_at >= since,
                 Event.ended_at >= since)

    rostered = {}
    for team, ev in (s.query(EventTeam, Event)
                     .join(EventTeamMember, EventTeamMember.team_id == EventTeam.id)
                     .join(Event, Event.id == EventTeam.event_id)
                     .filter(EventTeamMember.player_id == player.player_id, recent)
                     .all()):
        rostered[ev.id] = (ev, team)

    gids = _group_ids(s, player)
    group_events = {}
    if gids:
        cvc_ids = [eid for (eid,) in s.query(EventGroup.event_id)
                   .filter(EventGroup.group_id.in_(gids), EventGroup.status == "accepted").all()]
        q = s.query(Event).filter(recent, or_(Event.group_id.in_(gids),
                                              Event.id.in_(cvc_ids or [-1])))
        for ev in q.limit(200).all():
            if ev.id not in rostered:
                group_events[ev.id] = ev

    group_names = {}
    owner_ids = {ev.group_id for ev, _ in rostered.values()} | {
        ev.group_id for ev in group_events.values()}
    owner_ids.discard(None)
    if owner_ids:
        group_names = dict(s.query(Group.group_id, Group.group_name)
                           .filter(Group.group_id.in_(owner_ids)).all())

    order = {"active": 0, "draft": 1, "past": 2}
    out = []
    for ev, team in list(rostered.values()) + [(ev, None) for ev in group_events.values()]:
        status = _effective_status(ev)
        entry = {"id": ev.id, "name": clean(ev.name, 80) or "Event",
                 "status": status, "subtitle": _subtitle(ev, status, now),
                 "group": clean(group_names.get(ev.group_id), 40)
                 or ("DropTracker" if ev.group_id is None else None)}
        if team is not None:
            entry["team"] = {"id": team.id, "name": clean(team.name, 40) or "Team",
                             "color": _color(team.color)}
        stamp = ev.activated_at or ev.starts_at or ev.ends_at or now
        out.append((order.get(status, 3), team is None, -stamp.timestamp(), entry))
    out.sort(key=lambda r: r[:3])
    return [r[3] for r in out[:LIST_LIMIT]]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _run(work):
    def _go():
        s = get_db_session()
        try:
            return work(s)
        finally:
            s.close()
    return asyncio.to_thread(_go)


@plugin_events_bp.get("/plugin/events")
@rate_limit(limit=20, period=timedelta(seconds=60))
async def plugin_event_list():
    player_name = request.args.get("player_name")
    acc_hash = request.args.get("acc_hash")

    def work(s):
        player = _player(s, player_name, acc_hash)
        return None if player is None else list_events(s, player)

    try:
        events = await _run(work)
    except Exception as e:
        print(f"/plugin/events error: {e}")
        return jsonify({"error": "Something went wrong. Try again in a minute."}), 500
    if events is None:
        return jsonify({"error": "This account isn't on the DropTracker yet.",
                        "code": "not_registered"}), 404
    return jsonify({"events": events}), 200


@plugin_events_bp.get("/plugin/events/<int:event_id>/view")
@rate_limit(limit=40, period=timedelta(seconds=60))
async def plugin_event_view(event_id: int):
    player_name = request.args.get("player_name")
    acc_hash = request.args.get("acc_hash")
    page = (request.args.get("page") or "overview").strip()[:24]

    def work(s):
        from db.models import Event
        from web_api.routes.events import _is_event_admin

        player = _player(s, player_name, acc_hash)
        if player is None:
            raise _Missing()
        ev = s.query(Event).filter(Event.id == event_id).first()
        if ev is None:
            raise _Missing()
        user_id = _user_id(player)
        if not can_view(s, ev, player, user_id):
            raise _Missing()
        team = _team_of(s, ev.id, player.player_id)
        # Pages differ by audience only (admins see hidden tasks/players);
        # the viewer's own team is marked after the cache.
        admin = user_id is not None and _is_event_admin(s, user_id, ev)
        key = f"plugin:event_view:v1:{ev.id}:{page}:{'a' if admin else 'p'}"
        if page.startswith("task:"):
            # The viewer's team leads a task's checklist.
            key += f":{team.id if team is not None else 0}"
        cached = None
        try:
            from utils.redis import redis_client

            raw = redis_client.client.get(key)
            cached = json.loads(raw) if raw else None
        except Exception:
            cached = None
        if cached is None:
            cached = compose_page(s, ev, user_id, page, team)
            try:
                from utils.redis import redis_client

                redis_client.client.set(key, json.dumps(cached, default=str),
                                        ex=PAGE_CACHE_SECONDS)
            except Exception:
                pass
        payload = apply_viewer(cached, team.id if team is not None else None)
        if team is not None:
            payload["team"] = {"id": team.id, "name": clean(team.name, 40) or "Team",
                               "color": _color(team.color)}
        return payload

    try:
        payload = await _run(work)
    except _Missing:
        return jsonify({"error": NOT_FOUND, "code": "not_found"}), 404
    except Exception as e:
        print(f"/plugin/events/{event_id}/view error: {e}")
        return jsonify({"error": "Something went wrong. Try again in a minute."}), 500
    return jsonify(payload), 200
